"""Adapter tests: DeezerDownloadClient against the real DirectDeezerBackend
with a fake media acquirer and fake host context."""

from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path

import pytest
from repositories.protocols.download_client import EnqueueRequest, TaskHandle

from dndeezer.deezer.media import MediaAcquisitionError
from dndeezer.download_client import DeezerDownloadClient

SOURCE = "plugin:deezer-download"


class FakeContext:
    def __init__(self, settings=None, http=None):
        self.settings = settings or {}
        self.http = http if http is not None else object()
        self.logger = logging.getLogger("test.dndeezer")


class FakeClient:
    async def authenticate(self):
        return object()


class SuccessfulMedia:
    async def acquire(self, target, destination, *, on_progress=None):
        if on_progress:
            on_progress(50)

        output = destination / "Daft Punk - Track.flac"
        output.write_bytes(b"fake audio")

        if on_progress:
            on_progress(100)

        return [output]


class FailingMedia:
    async def acquire(self, target, destination, *, on_progress=None):
        raise MediaAcquisitionError("no media for you")


class SlowMedia:
    async def acquire(self, target, destination, *, on_progress=None):
        await asyncio.sleep(60)
        return []


def make_client(tmp_path: Path, media) -> tuple[DeezerDownloadClient, object]:
    """Adapter wired to a real backend with injected media; skips only the
    Deezer client construction."""
    ctx = FakeContext(
        {"arl": "test-arl", "downloads_dir": str(tmp_path / "dl"),
         "state_dir": str(tmp_path / "state")}
    )
    adapter = DeezerDownloadClient(ctx)

    from dndeezer.backends.direct import DirectDeezerBackend

    backend = DirectDeezerBackend(
        client=FakeClient(),
        media=media,
        downloads_dir=tmp_path / "dl",
    )
    adapter._get_backend = lambda: backend

    return adapter, backend


async def wait_for_status(adapter, handle, wanted, timeout=2.0):
    deadline = time.monotonic() + timeout
    while True:
        status = await adapter.get_status(handle)
        if status.status in wanted:
            return status
        assert time.monotonic() < deadline, f"still {status.status!r}"
        await asyncio.sleep(0.01)


# -- identity and configuration --

@pytest.mark.asyncio
async def test_completed_job_recovers_after_restart_and_setting_change(tmp_path):
    adapter, _ = make_client(tmp_path, SuccessfulMedia())
    handle = await adapter.enqueue(
        EnqueueRequest(task_id="restart", source=SOURCE, payload="track:100")
    )
    await wait_for_status(adapter, handle, ("completed",))
    original = await adapter.inspect_materialization(handle)
    settings = dict(adapter.ctx.settings, downloads_dir=str(tmp_path / "different"))
    restarted = DeezerDownloadClient(FakeContext(settings))
    recovered = await restarted.inspect_materialization(handle)
    assert recovered.workspace_path == original.workspace_path
    assert recovered.file_paths == original.file_paths
    assert recovered.mount_healthy
    assert (await restarted.get_status(handle)).status == "completed"
    assert await restarted.get_file_path(handle, Path(original.file_paths[0]).name)
    assert await restarted.discard_client_artifacts(handle)
    assert not Path(original.workspace_path).exists()
    assert await restarted.discard_client_artifacts(handle)


@pytest.mark.asyncio
async def test_failed_cleanup_retains_record_for_retry(tmp_path, monkeypatch):
    adapter, _ = make_client(tmp_path, SuccessfulMedia())
    handle = await adapter.enqueue(
        EnqueueRequest(task_id="retry", source=SOURCE, payload="track:100")
    )
    await wait_for_status(adapter, handle, ("completed",))
    def denied(*args, **kwargs):
        raise PermissionError("denied")
    with monkeypatch.context() as patch:
        patch.setattr("dndeezer.download_client.shutil.rmtree", denied)
        assert not await adapter.discard_client_artifacts(handle)
    assert adapter._store.get(handle.job_name).state == "completed"
    assert await adapter.discard_client_artifacts(handle)


@pytest.mark.asyncio
async def test_persisted_interrupted_job_can_be_cleaned(tmp_path):
    adapter, _ = make_client(tmp_path, SuccessfulMedia())
    await adapter._ensure_store()
    root = tmp_path / "dl"
    workspace = root / "interrupted"
    workspace.mkdir(parents=True)
    (workspace / "partial").write_bytes(b"partial")
    adapter._store.create(job_name="old", task_id="old", backend_id="interrupted",
                          payload="track:1", mount_root=root, workspace_path=workspace)
    restarted = DeezerDownloadClient(adapter.ctx)
    handle = TaskHandle(source=SOURCE, job_name="old")
    assert (await restarted.get_status(handle)).status == "failed"
    assert (await restarted.inspect_materialization(handle)).mount_healthy
    assert await restarted.discard_client_artifacts(handle)
    assert not workspace.exists()


@pytest.mark.asyncio
async def test_immediate_abort_persists_terminal_state(tmp_path):
    adapter, _ = make_client(tmp_path, SlowMedia())
    handle = await adapter.enqueue(
        EnqueueRequest(task_id="cancel", source=SOURCE, payload="track:1")
    )
    assert await adapter.abort(handle)
    assert adapter._store.get(handle.job_name).state == "cancelled"
    # Mount availability is required even when a cancelled job wrote no files.
    (tmp_path / "dl").mkdir(exist_ok=True)
    assert await adapter.discard_client_artifacts(handle)

def test_client_name(tmp_path):
    adapter = DeezerDownloadClient(FakeContext())
    assert adapter.client_name == SOURCE


@pytest.mark.parametrize(
    ("arl", "downloads_dir", "expected"),
    [
        ("test-arl", "dl", True),
        ("", "dl", False),
        ("test-arl", "", False),
    ],
)
def test_is_configured(tmp_path, arl, downloads_dir, expected):
    adapter = DeezerDownloadClient(
        FakeContext({"arl": arl, "downloads_dir": downloads_dir})
    )
    assert adapter.is_configured() is expected


def test_get_backend_builds_from_settings(tmp_path):
    from dndeezer.backends.direct import DirectDeezerBackend

    adapter = DeezerDownloadClient(
        FakeContext({"arl": "test-arl", "downloads_dir": str(tmp_path / "dl")})
    )
    backend = adapter._get_backend()

    assert isinstance(backend, DirectDeezerBackend)
    assert backend.downloads_dir == (tmp_path / "dl").resolve()
    assert adapter._get_backend() is backend  # built once


@pytest.mark.asyncio
async def test_health_check_reports_missing_settings():
    adapter = DeezerDownloadClient(FakeContext({}))
    status = await adapter.health_check()
    assert status.status == "error"
    assert "ARL" in status.message

    adapter = DeezerDownloadClient(FakeContext({"arl": "test-arl"}))
    status = await adapter.health_check()
    assert status.status == "error"
    assert "downloads_dir" in status.message


@pytest.mark.asyncio
async def test_health_check_ok(tmp_path):
    adapter, _ = make_client(tmp_path, SuccessfulMedia())
    status = await adapter.health_check()

    assert status.status == "ok"
    assert (tmp_path / "dl").is_dir()


# -- job lifecycle --

@pytest.mark.asyncio
async def test_enqueue_returns_folder_mode_handle(tmp_path):
    adapter, _ = make_client(tmp_path, SuccessfulMedia())

    handle = await adapter.enqueue(
        EnqueueRequest(task_id="t1", source=SOURCE, payload="track:100")
    )

    assert handle.source == SOURCE
    assert handle.job_name == "droppedneedle-t1"
    assert handle.plugin_token == "track:100"
    assert handle.filenames == []  # folder mode


@pytest.mark.asyncio
async def test_invalid_payload_raises(tmp_path):
    adapter, _ = make_client(tmp_path, SuccessfulMedia())

    with pytest.raises(ValueError):
        await adapter.enqueue(
            EnqueueRequest(task_id="t9", source=SOURCE, payload="playlist:9")
        )


@pytest.mark.asyncio
async def test_completed_job_reports_files_and_bytes(tmp_path):
    adapter, _ = make_client(tmp_path, SuccessfulMedia())
    handle = await adapter.enqueue(
        EnqueueRequest(task_id="t1", source=SOURCE, payload="track:100")
    )

    status = await wait_for_status(adapter, handle, ("completed",))

    assert status.task_id == "t1"
    assert status.files_total == 1
    assert status.files_completed == 1
    assert status.bytes_total == len(b"fake audio")
    assert status.bytes_downloaded == status.bytes_total
    assert status.progress_percent == 100.0
    assert status.succeeded_filenames == ["Daft Punk - Track.flac"]

    files = await adapter.list_completed_files(handle)
    assert [f.name for f in files] == ["Daft Punk - Track.flac"]
    assert all(f.is_relative_to(tmp_path / "dl") for f in files)

    path = await adapter.get_file_path(handle, "Daft Punk - Track.flac")
    assert path == files[0]
    assert await adapter.get_file_path(handle, "missing.flac") is None


@pytest.mark.asyncio
async def test_failed_job_maps_error(tmp_path):
    adapter, _ = make_client(tmp_path, FailingMedia())
    handle = await adapter.enqueue(
        EnqueueRequest(task_id="t1", source=SOURCE, payload="track:100")
    )

    status = await wait_for_status(adapter, handle, ("failed",))
    assert "no media for you" in status.error


@pytest.mark.asyncio
async def test_abort_maps_to_failed_aborted(tmp_path):
    adapter, _ = make_client(tmp_path, SlowMedia())
    handle = await adapter.enqueue(
        EnqueueRequest(task_id="t1", source=SOURCE, payload="track:100")
    )
    await asyncio.sleep(0.01)  # let the job task start, as a poll would

    assert await adapter.abort(handle) is True

    status = await adapter.get_status(handle)
    assert status.status == "failed"
    assert status.error == "aborted"


@pytest.mark.asyncio
async def test_unknown_handle_fails_cleanly(tmp_path):
    adapter, _ = make_client(tmp_path, SuccessfulMedia())
    handle = TaskHandle(source=SOURCE, job_name="droppedneedle-ghost")

    status = await adapter.get_status(handle)
    assert status.status == "failed"
    assert "unknown task" in status.error
    assert status.task_id == "ghost"

    assert await adapter.abort(handle) is False
    assert await adapter.list_completed_files(handle) == []
    assert await adapter.get_file_path(handle, "x.flac") is None


# -- materialization --

@pytest.mark.asyncio
async def test_inspect_materialization_states(tmp_path):
    adapter, _ = make_client(tmp_path, SuccessfulMedia())

    missing = await adapter.inspect_materialization(
        TaskHandle(source=SOURCE, job_name="droppedneedle-ghost")
    )
    assert missing.state == "missing"

    handle = await adapter.enqueue(
        EnqueueRequest(task_id="t1", source=SOURCE, payload="track:100")
    )
    await wait_for_status(adapter, handle, ("completed",))

    done = await adapter.inspect_materialization(handle)
    assert done.state == "completed"
    assert len(done.file_paths) == 1
    assert done.workspace_path and done.workspace_path in done.file_paths[0]


@pytest.mark.asyncio
async def test_discard_client_artifacts_removes_job_dir(tmp_path):
    adapter, _ = make_client(tmp_path, SuccessfulMedia())
    handle = await adapter.enqueue(
        EnqueueRequest(task_id="t1", source=SOURCE, payload="track:100")
    )
    await wait_for_status(adapter, handle, ("completed",))

    materialized = await adapter.inspect_materialization(handle)
    job_dir = Path(materialized.workspace_path)
    assert job_dir.is_dir()

    assert await adapter.discard_client_artifacts(handle) is True
    assert not job_dir.exists()

    # Cleanup identity survives as a tombstone for repeated host calls.
    status = await adapter.get_status(handle)
    assert status.status == "failed"
    assert status.error == "cleaned"
    assert await adapter.discard_client_artifacts(handle) is True


# -- composite entrypoint --

def test_dndeezer_exposes_both_protocol_surfaces(tmp_path):
    import plugin

    instance = plugin.DNDeezer(
        FakeContext({"arl": "a", "downloads_dir": str(tmp_path)})
    )

    indexer_surface = {
        "indexer_name",
        "is_configured",
        "health_check",
        "search_album",
        "search_track",
    }
    client_surface = {
        "client_name",
        "is_configured",
        "health_check",
        "enqueue",
        "get_status",
        "abort",
        "inspect_materialization",
        "discard_client_artifacts",
        "list_completed_files",
        "get_file_path",
        "diagnose_downloads_mount",
    }

    missing = (indexer_surface | client_surface) - set(dir(instance))
    assert not missing, f"missing protocol members: {missing}"
    assert instance.is_configured() is True
    assert instance.indexer_name == SOURCE


def test_dndeezer_requires_both_settings(tmp_path):
    import plugin

    search_only = plugin.DNDeezer(FakeContext({"arl": "a"}))
    assert search_only.is_configured() is False

    with pytest.raises(RuntimeError):
        plugin.DNDeezer(FakeContext({}))._get_backend()
