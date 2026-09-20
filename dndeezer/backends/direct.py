from __future__ import annotations

import asyncio
import logging
import shutil
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from uuid import uuid4

from dndeezer._async import run_blocking
from dndeezer.backend import (
    BackendHealth,
    BackendJob,
    BackendState,
    BackendStatus,
    DownloadTarget,
)
from dndeezer.deezer.client import DeezerClient
from dndeezer.deezer.media import (
    DirectDeezerMediaService,
    MediaAcquirer,
    MediaAcquisitionError,
)


@dataclass(slots=True)
class _InternalJob:
    job: BackendJob

    state: BackendState = "queued"
    progress_percent: float = 0.0
    error: str | None = None

    files: list[Path] = field(
        default_factory=list
    )

    task: asyncio.Task[None] | None = None
    on_finished: Callable[[BackendStatus, list[Path]], Awaitable[None]] | None = None


class DirectDeezerBackend:
    def __init__(
        self,
        *,
        client: DeezerClient,
        media: MediaAcquirer,
        downloads_dir: Path,
        logger: logging.Logger | None = None,
    ) -> None:
        self.client = client
        self.media = media

        # Resolve lazily from an async entry point, never on the host thread.
        self.downloads_dir = Path(downloads_dir)
        self._directory_ready = False
        self._directory_lock = asyncio.Lock()

        self.logger = logger or logging.getLogger(
            __name__
        )

        self._jobs: dict[str, _InternalJob] = {}

    def is_configured(self) -> bool:
        return bool(
            self.downloads_dir
        )

    async def _prepare_downloads_dir(self) -> None:
        async with self._directory_lock:
            if not self._directory_ready:
                self.downloads_dir = await run_blocking(
                    lambda: self.downloads_dir.expanduser().resolve()
                )
                self._directory_ready = True

    async def health_check(
        self,
    ) -> BackendHealth:
        try:
            await self.client.authenticate()
        except Exception as exc:  # noqa: BLE001 - health check reports, never raises
            return BackendHealth(
                ok=False,
                message=(
                    "Deezer authentication failed: "
                    f"{exc}"
                ),
            )

        try:
            await self._prepare_downloads_dir()
            await run_blocking(
                self.downloads_dir.mkdir,
                parents=True,
                exist_ok=True,
            )
        except OSError as exc:
            return BackendHealth(
                ok=False,
                message=(
                    "Downloads directory is not "
                    f"available: {exc}"
                ),
            )

        if not await run_blocking(
            self._directory_is_writable,
            self.downloads_dir,
        ):
            return BackendHealth(
                ok=False,
                message=(
                    "Downloads directory is not writable"
                ),
            )

        return BackendHealth(
            ok=True,
            message="Deezer backend is ready",
        )

    async def enqueue(
        self,
        *,
        task_id: str,
        target: DownloadTarget,
        before_start: Callable[[BackendJob], Awaitable[None]] | None = None,
        on_finished: Callable[[BackendStatus, list[Path]], Awaitable[None]] | None = None,
    ) -> BackendJob:
        if not task_id:
            raise ValueError(
                "task_id must not be empty"
            )

        await self._prepare_downloads_dir()
        backend_id = uuid4().hex

        job = BackendJob(
            task_id=task_id,
            backend_id=backend_id,
            target=target,
        )

        internal = _InternalJob(
            job=job,
            on_finished=on_finished,
        )

        if before_start is not None:
            await before_start(job)

        self._jobs[backend_id] = internal

        internal.task = asyncio.create_task(
            self._run_job(internal),
            name=f"dndeezer:{backend_id}",
        )

        return job

    async def get_status(
        self,
        job: BackendJob,
    ) -> BackendStatus:
        internal = self._get_job(job)

        if (internal.state in ("completed", "failed", "cancelled") and internal.task
                and not internal.task.cancelled()):
            await asyncio.shield(internal.task)

        return BackendStatus(
            state=internal.state,
            progress_percent=internal.progress_percent,
            error=internal.error,
        )

    async def abort(
        self,
        job: BackendJob,
    ) -> bool:
        internal = self._get_job(job)

        if internal.state in (
            "completed",
            "failed",
            "cancelled",
        ):
            return False

        task = internal.task

        if task is None:
            internal.state = "cancelled"

            await self._cleanup_job_directory(
                internal
            )

            return True

        task.cancel()

        try:
            await task
        except asyncio.CancelledError:
            pass

        # A task cancelled before its first step never enters _run_job's finally.
        if internal.state in ("queued", "downloading"):
            internal.state = "cancelled"
            await self._cleanup_job_directory(internal)
            if internal.on_finished is not None:
                await internal.on_finished(BackendStatus(state="cancelled"), [])

        return True

    async def list_completed_files(
        self,
        job: BackendJob,
    ) -> list[Path]:
        internal = self._get_job(job)

        if internal.state != "completed":
            return []

        return list(internal.files)

    async def get_file_path(
        self,
        job: BackendJob,
        filename: str,
    ) -> Path | None:
        internal = self._get_job(job)

        if internal.state != "completed":
            return None

        requested_name = Path(filename).name

        for file in internal.files:
            if file.name != requested_name:
                continue

            if not await run_blocking(
                self._is_within_downloads, file
            ):
                self.logger.warning(
                    "Refusing file outside downloads "
                    "directory: %s",
                    file,
                )
                return None

            return file

        return None

    async def _run_job(
        self,
        internal: _InternalJob,
    ) -> None:
        job = internal.job

        try:
            job_dir = await run_blocking(self._job_directory, job)
            internal.state = "downloading"
            internal.progress_percent = 0.0

            await run_blocking(
                job_dir.mkdir,
                parents=True,
                exist_ok=False,
            )

            files = await self.media.acquire(
                job.target,
                job_dir,
                on_progress=lambda progress: (
                    self._update_progress(
                        internal,
                        progress,
                    )
                ),
            )

            files = await run_blocking(
                self._normalize_completed_files, job_dir, files,
            )

            internal.files = files
            internal.progress_percent = 100.0
            internal.state = "completed"

            self.logger.info(
                "Deezer job %s completed with %d files",
                job.backend_id,
                len(files),
            )

        except asyncio.CancelledError:
            internal.state = "cancelled"
            internal.error = None

            self.logger.info(
                "Deezer job %s cancelled",
                job.backend_id,
            )

            await self._cleanup_job_directory(
                internal
            )

            raise

        except NotImplementedError as exc:
            internal.state = "failed"
            internal.error = str(exc)

            self.logger.warning(
                "Deezer job %s is not implemented: %s",
                job.backend_id,
                exc,
            )

            await self._cleanup_job_directory(
                internal
            )

        except MediaAcquisitionError as exc:
            internal.state = "failed"
            internal.error = str(exc)

            self.logger.warning(
                "Deezer job %s failed: %s",
                job.backend_id,
                exc,
            )

            await self._cleanup_job_directory(
                internal
            )

        except Exception as exc:
            internal.state = "failed"
            internal.error = str(exc) or "Unexpected backend error"

            self.logger.exception(
                "Unexpected failure in Deezer job %s",
                job.backend_id,
            )

            await self._cleanup_job_directory(
                internal
            )

        finally:
            if internal.on_finished is not None:
                await internal.on_finished(
                    BackendStatus(state=internal.state, error=internal.error),
                    list(internal.files),
                )

    def _update_progress(
        self,
        internal: _InternalJob,
        progress: float,
    ) -> None:
        if internal.state != "downloading":
            return

        internal.progress_percent = max(
            0.0,
            min(float(progress), 100.0),
        )

    def _get_job(
        self,
        job: BackendJob,
    ) -> _InternalJob:
        internal = self._jobs.get(
            job.backend_id
        )

        if internal is None:
            raise KeyError(
                f"Unknown backend job: "
                f"{job.backend_id}"
            )

        if internal.job.task_id != job.task_id:
            raise ValueError(
                "Task ID does not match backend job"
            )

        return internal

    def _job_directory(
        self,
        job: BackendJob,
    ) -> Path:
        return (
            self.downloads_dir
            / job.backend_id
        ).resolve()

    def _normalize_completed_files(
        self,
        job_dir: Path,
        files: list[Path],
    ) -> list[Path]:
        resolved = [file.expanduser().resolve() for file in files]
        self._validate_completed_files(job_dir, resolved)
        return resolved

    def _validate_completed_files(
        self,
        job_dir: Path,
        files: list[Path],
    ) -> None:
        if not files:
            raise MediaAcquisitionError(
                "Acquisition completed without "
                "producing any files"
            )

        resolved_job_dir = job_dir.resolve()

        for file in files:
            if not file.is_file():
                raise MediaAcquisitionError(
                    f"Completed file does not exist: "
                    f"{file}"
                )

            if not file.is_relative_to(
                resolved_job_dir
            ):
                raise MediaAcquisitionError(
                    "Acquisition returned a file "
                    "outside its job directory"
                )

    async def _cleanup_job_directory(
        self,
        internal: _InternalJob,
    ) -> None:
        def cleanup() -> None:
            directory = self._job_directory(internal.job)
            shutil.rmtree(directory, ignore_errors=True)

        await run_blocking(cleanup)

        internal.files.clear()

    def _is_within_downloads(
        self,
        path: Path,
    ) -> bool:
        try:
            return path.resolve().is_relative_to(
                self.downloads_dir
            )
        except OSError:
            return False

    @staticmethod
    def _directory_is_writable(
        directory: Path,
    ) -> bool:
        probe = directory / (
            f".dndeezer-write-test-{uuid4().hex}"
        )

        try:
            probe.touch(
                exist_ok=False,
            )
            probe.unlink()
            return True
        except OSError:
            return False


def build_direct_backend(
    *,
    http,
    arl: str,
    downloads_dir: Path,
    logger: logging.Logger | None = None,
    client: DeezerClient | None = None,
) -> DirectDeezerBackend:
    """Wire a DirectDeezerBackend from host-provided pieces.

    ``http`` and ``arl`` are the same host async HTTP client and Deezer ARL
    the indexer uses. Pass ``client`` to share an authenticated
    ``DeezerClient`` (and its request throttling) with other components.
    """
    if client is None:
        client = DeezerClient(http, arl)

    return DirectDeezerBackend(
        client=client,
        media=DirectDeezerMediaService(client),
        downloads_dir=downloads_dir,
        logger=logger,
    )
