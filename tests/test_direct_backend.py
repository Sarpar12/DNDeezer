from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from dndeezer.backend import DownloadTarget
from dndeezer.backends.direct import (
    DirectDeezerBackend,
)


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