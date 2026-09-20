from __future__ import annotations

import asyncio
import logging
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from dndeezer.backend import DownloadTarget
from dndeezer.backends.direct import (
    DirectDeezerBackend,
)
from dndeezer.download_client import DeezerDownloadClient


class FakeClient:
    async def authenticate(self):
        return object()


class SuccessfulMedia:
    async def acquire(
        self,
        target,
        destination,
        *,
        on_progress=None,
    ):
        if on_progress:
            on_progress(20)
            on_progress(75)

        output = destination / "track.flac"
        output.write_bytes(b"fake audio")

        if on_progress:
            on_progress(100)

        return [output]


class FailingMedia:
    async def acquire(
        self,
        target,
        destination,
        *,
        on_progress=None,
    ):
        raise RuntimeError("boom")


class SlowMedia:
    async def acquire(
        self,
        target,
        destination,
        *,
        on_progress=None,
    ):
        await asyncio.sleep(60)
        return []


@pytest.mark.asyncio
async def test_successful_job(
    tmp_path: Path,
):
    backend = DirectDeezerBackend(
        client=FakeClient(),
        media=SuccessfulMedia(),
        downloads_dir=tmp_path,
    )

    job = await backend.enqueue(
        task_id="task-123",
        target=DownloadTarget(
            kind="track",
            deezer_id=3135555,
        ),
    )

    await asyncio.sleep(0.05)

    status = await backend.get_status(job)

    assert status.state == "completed"
    assert status.progress_percent == 100

    files = await backend.list_completed_files(
        job
    )

    assert len(files) == 1
    assert files[0].name == "track.flac"


@pytest.mark.asyncio
async def test_completed_file_lookup(
    tmp_path: Path,
):
    backend = DirectDeezerBackend(
        client=FakeClient(),
        media=SuccessfulMedia(),
        downloads_dir=tmp_path,
    )

    job = await backend.enqueue(
        task_id="task-123",
        target=DownloadTarget(
            kind="track",
            deezer_id=3135555,
        ),
    )

    await asyncio.sleep(0.05)

    path = await backend.get_file_path(
        job,
        "track.flac",
    )

    assert path is not None
    assert path.name == "track.flac"


@pytest.mark.asyncio
async def test_unknown_filename_returns_none(
    tmp_path: Path,
):
    backend = DirectDeezerBackend(
        client=FakeClient(),
        media=SuccessfulMedia(),
        downloads_dir=tmp_path,
    )

    job = await backend.enqueue(
        task_id="task-123",
        target=DownloadTarget(
            kind="track",
            deezer_id=3135555,
        ),
    )

    await asyncio.sleep(0.05)

    path = await backend.get_file_path(
        job,
        "../../etc/passwd",
    )

    assert path is None


@pytest.mark.asyncio
async def test_failed_job(
    tmp_path: Path,
):
    backend = DirectDeezerBackend(
        client=FakeClient(),
        media=FailingMedia(),
        downloads_dir=tmp_path,
    )

    job = await backend.enqueue(
        task_id="task-123",
        target=DownloadTarget(
            kind="album",
            deezer_id=302127,
        ),
    )

    await asyncio.sleep(0.05)

    status = await backend.get_status(job)

    assert status.state == "failed"
    assert status.error is not None

    files = await backend.list_completed_files(
        job
    )

    assert files == []


@pytest.mark.asyncio
async def test_cancel_job(
    tmp_path: Path,
):
    backend = DirectDeezerBackend(
        client=FakeClient(),
        media=SlowMedia(),
        downloads_dir=tmp_path,
    )

    job = await backend.enqueue(
        task_id="task-123",
        target=DownloadTarget(
            kind="album",
            deezer_id=302127,
        ),
    )

    await asyncio.sleep(0)

    aborted = await backend.abort(job)

    assert aborted is True

    status = await backend.get_status(job)

    assert status.state == "cancelled"


@pytest.mark.asyncio
async def test_health_check(
    tmp_path: Path,
):
    backend = DirectDeezerBackend(
        client=FakeClient(),
        media=SuccessfulMedia(),
        downloads_dir=tmp_path,
    )

    health = await backend.health_check()

    assert health.ok is True


@pytest.mark.asyncio
async def test_filesystem_operations_stay_off_event_loop(tmp_path, monkeypatch):
    from infrastructure.plugins.protocols import EnqueueRequest

    class WorkerMedia:
        async def acquire(self, target, destination, *, on_progress=None):
            output = destination / "track.flac"
            await asyncio.to_thread(output.write_bytes, b"audio")
            return [output]

    loop_thread = threading.get_ident()
    calls = set()

    def guard(name, original):
        def checked(*args, **kwargs):
            assert threading.get_ident() != loop_thread, f"{name} ran on event loop"
            calls.add(name)
            return original(*args, **kwargs)
        return checked

    # Scope guards to the plugin lifecycle, excluding pytest's own filesystem I/O.
    with monkeypatch.context() as patch:
        for name in ("resolve", "expanduser", "stat", "is_file", "mkdir", "open", "unlink"):
            patch.setattr(Path, name, guard(name, getattr(Path, name)))

        adapter = DeezerDownloadClient(SimpleNamespace(
            settings={"arl": "test", "downloads_dir": str(tmp_path / "downloads"),
                      "state_dir": str(tmp_path / "state")},
            http=object(), logger=logging.getLogger("test.filesystem"),
        ))
        backend = adapter._get_backend()
        backend.client = FakeClient()
        backend.media = WorkerMedia()
        assert (await adapter.health_check()).status == "ok"
        handle = await adapter.enqueue(EnqueueRequest(
            task_id="thread-check", source="plugin:deezer-download", payload="track:1",
        ))
        job = adapter._handles[handle.job_name]
        await backend._jobs[job.backend_id].task
        status = await adapter.get_status(handle)
        assert status.status == "completed", status.error
        assert status.bytes_total == 5
        assert await adapter.get_file_path(handle, "track.flac") is not None
        assert await adapter.discard_client_artifacts(handle)

    assert {"resolve", "is_file", "mkdir", "open", "stat"} <= calls
    assert not list((tmp_path / "downloads").iterdir())


@pytest.mark.asyncio
async def test_validation_rejects_symlink_outside_job(tmp_path):
    outside = tmp_path / "outside.flac"
    outside.write_bytes(b"outside")

    class SymlinkMedia:
        async def acquire(self, target, destination, *, on_progress=None):
            output = destination / "track.flac"
            await asyncio.to_thread(output.symlink_to, outside)
            return [output]

    backend = DirectDeezerBackend(
        client=FakeClient(), media=SymlinkMedia(), downloads_dir=tmp_path / "downloads",
    )
    job = await backend.enqueue(task_id="symlink", target=DownloadTarget("track", 1))
    await backend._jobs[job.backend_id].task
    status = await backend.get_status(job)
    assert status.state == "failed"
    assert "outside its job directory" in status.error
    assert outside.read_bytes() == b"outside"
    assert not list(backend.downloads_dir.iterdir())
