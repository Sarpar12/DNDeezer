"""DroppedNeedle ``download_client`` capability surface for Deezer.

This is the only module that knows about host download-client types. It
adapts ``EnqueueRequest``/``TaskHandle`` calls onto the host-agnostic
``DirectDeezerBackend`` (built via ``build_direct_backend``), which owns the
async job lifecycle and the media pipeline.

Folder mode: results are returned with empty ``files`` lists, so the engine
imports whole releases through ``list_completed_files``.
"""

from __future__ import annotations

import errno
import os
import shutil
from pathlib import Path

from infrastructure.plugins.protocols import (
    DownloadMaterialization,
    DownloadTaskStatus,
    EnqueueRequest,
    MountDiagnosis,
    TaskHandle,
)

# DroppedNeedle v2.13.0 does not re-export ServiceStatus in the public API.
from models.common import ServiceStatus

from ._async import run_blocking
from .backend import BackendJob, parse_payload
from .backends.direct import DirectDeezerBackend, build_direct_backend
from .indexer import SOURCE

JOB_NAME_PREFIX = "droppedneedle-"


class DeezerDownloadClient:
    """Host download-client surface backed by ``DirectDeezerBackend``.

    Handles are correlated by an attempt-specific ``job_name``. Preserve the
    host's candidate suffix and never reuse a name for another backend job.
    The map is in-memory: after a
    host restart the asyncio jobs are gone too, so unknown handles are
    reported as failed instead of hanging the engine.
    """

    def __init__(self, context) -> None:
        self.ctx = context
        self._backend: DirectDeezerBackend | None = None
        self._handles: dict[str, BackendJob] = {}
        self._issued_handle_names: set[str] = set()

    # -- config helpers --

    def _arl(self) -> str:
        return str(self.ctx.settings.get("arl") or "").strip()

    def _downloads_dir(self) -> Path | None:
        raw = str(self.ctx.settings.get("downloads_dir") or "").strip()
        return Path(raw) if raw else None

    def _get_backend(self) -> DirectDeezerBackend:
        if self._backend is None:
            downloads_dir = self._downloads_dir()
            if downloads_dir is None:
                raise RuntimeError("downloads_dir is not configured")

            self._backend = build_direct_backend(
                http=self.ctx.http,
                arl=self._arl(),
                downloads_dir=downloads_dir,
                logger=self.ctx.logger,
            )

        return self._backend

    # -- DownloadClientProtocol: identity and health --

    @property
    def client_name(self) -> str:
        return SOURCE  # forced by the host adapter anyway

    def is_configured(self) -> bool:
        return bool(self._arl()) and self._downloads_dir() is not None

    async def health_check(self) -> ServiceStatus:
        if not self._arl():
            return ServiceStatus(
                status="error",
                message="Deezer ARL is not configured",
            )

        if self._downloads_dir() is None:
            return ServiceStatus(
                status="error",
                message="Deezer downloads_dir is not configured",
            )

        # Backend health covers authentication AND directory writability.
        health = await self._get_backend().health_check()
        if not health.ok:
            return ServiceStatus(status="error", message=health.message)

        return ServiceStatus(status="ok", message=health.message)

    # -- DownloadClientProtocol: job lifecycle --

    async def enqueue(self, request: EnqueueRequest) -> TaskHandle:
        payload = (getattr(request, "payload", "") or "").strip()
        target = parse_payload(payload)  # ValueError -> orchestration error

        job = await self._get_backend().enqueue(
            task_id=request.task_id,
            target=target,
        )

        job_name = request.job_name or f"{JOB_NAME_PREFIX}{request.task_id}"
        if job_name in self._issued_handle_names:
            job_name = f"{job_name}-{job.backend_id}"
        self._issued_handle_names.add(job_name)
        handle = TaskHandle(
            source=SOURCE,
            job_name=job_name,
            filenames=[],  # folder mode
            plugin_token=payload,
        )
        self._handles[handle.job_name] = job

        return handle

    async def get_status(self, handle: TaskHandle) -> DownloadTaskStatus:
        job = self._resolve(handle)
        task_id = self._task_id_for(handle, job)

        if job is None:
            return DownloadTaskStatus(
                task_id=task_id,
                status="failed",
                error="unknown task (plugin reloaded?)",
            )

        backend = self._get_backend()
        status = await backend.get_status(job)

        if status.state == "completed":
            files = await backend.list_completed_files(job)
            total_bytes = await self._file_sizes(files)

            return DownloadTaskStatus(
                task_id=task_id,
                status="completed",
                files_total=len(files),
                files_completed=len(files),
                bytes_total=total_bytes,
                bytes_downloaded=total_bytes,
                progress_percent=100.0,
                succeeded_filenames=[file.name for file in files],
            )

        if status.state == "failed":
            return DownloadTaskStatus(
                task_id=task_id,
                status="failed",
                error=status.error or "download failed",
                progress_percent=status.progress_percent,
            )

        if status.state == "cancelled":
            return DownloadTaskStatus(
                task_id=task_id,
                status="failed",
                error="aborted",
                progress_percent=status.progress_percent,
            )

        return DownloadTaskStatus(
            task_id=task_id,
            status="downloading",
            progress_percent=status.progress_percent,
        )

    async def abort(self, handle: TaskHandle) -> bool:
        job = self._resolve(handle)
        if job is None:
            return False

        return await self._get_backend().abort(job)

    # -- DownloadClientProtocol: materialization and files --

    async def inspect_materialization(
        self,
        handle: TaskHandle,
    ) -> DownloadMaterialization:
        job = self._resolve(handle)
        if job is None:
            self.ctx.logger.warning(
                "DNDeezer inspect_materialization: unknown handle job=%s "
                "(known=%s) - reporting missing",
                getattr(handle, "job_name", ""),
                sorted(self._handles),
            )
            return DownloadMaterialization(state="missing")

        backend = self._get_backend()
        status = await backend.get_status(job)
        workspace = str(backend.downloads_dir / job.backend_id)
        mount_healthy = await run_blocking(self._mount_healthy, backend.downloads_dir)

        self.ctx.logger.info(
            "DNDeezer inspect_materialization: job=%s state=%s mount_root=%s "
            "mount_healthy=%s workspace=%s",
            handle.job_name,
            status.state,
            backend.downloads_dir,
            mount_healthy,
            workspace,
        )
        if not mount_healthy:
            self.ctx.logger.warning(
                "DNDeezer mount probe FAILED for %s: exists=%s is_dir=%s "
                "is_symlink=%s can_read=%s",
                backend.downloads_dir,
                backend.downloads_dir.exists(),
                backend.downloads_dir.is_dir(),
                backend.downloads_dir.is_symlink(),
                os.access(backend.downloads_dir, os.R_OK),
            )

        evidence = {
            "mount_root": str(backend.downloads_dir),
            "mount_healthy": mount_healthy,
            "workspace_path": workspace,
        }

        if status.state == "completed":
            files = await backend.list_completed_files(job)

            return DownloadMaterialization(
                state="completed",
                file_paths=[str(file) for file in files],
                **evidence,
            )

        if status.state in ("failed", "cancelled"):
            return DownloadMaterialization(
                state="failed",
                **evidence,
            )

        return DownloadMaterialization(
            state="active",
            **evidence,
        )

    def _mount_healthy(self, directory: Path) -> bool:
        try:
            with os.scandir(directory) as entries:
                next(entries, None)
            return True
        except OSError as exc:
            errno_name = errno.errorcode.get(exc.errno, str(exc.errno))
            self.ctx.logger.warning(
                "DNDeezer mount probe OSError on %s: %s (%s)",
                directory,
                errno_name,
                exc,
            )
            return False

    async def discard_client_artifacts(self, handle: TaskHandle) -> bool:
        job = self._resolve(handle)
        if job is None:
            self.ctx.logger.warning(
                "DNDeezer discard_client_artifacts: unknown handle job=%s "
                "(known=%s) - host will retry cleanup",
                getattr(handle, "job_name", ""),
                sorted(self._handles),
            )
            return False

        backend = self._get_backend()

        def discard() -> None:
            directory = (backend.downloads_dir / job.backend_id).resolve()
            if directory.is_relative_to(backend.downloads_dir):
                shutil.rmtree(directory, ignore_errors=True)
            else:
                self.ctx.logger.warning(
                    "DNDeezer discard REFUSED: %s escapes downloads_dir %s",
                    directory,
                    backend.downloads_dir,
                )

        await run_blocking(discard)

        def probe() -> tuple[bool, str | None]:
            directory = backend.downloads_dir / job.backend_id
            if directory.exists():
                try:
                    remaining = sorted(
                        entry.name for entry in os.scandir(directory)
                    )[:10]
                except OSError as exc:
                    return True, f"unreadable after discard: {exc}"
                return True, f"still exists, entries={remaining}"
            return False, None

        still_present, reason = await run_blocking(probe)
        if still_present:
            self.ctx.logger.warning(
                "DNDeezer discard_client_artifacts: workspace for job=%s at %s "
                "survived rmtree (%s) - host cleanup will keep retrying",
                handle.job_name,
                backend.downloads_dir / job.backend_id,
                reason,
            )
        else:
            self.ctx.logger.info(
                "DNDeezer discard_client_artifacts: removed workspace for job=%s "
                "at %s",
                handle.job_name,
                backend.downloads_dir / job.backend_id,
            )

        for key, entry in list(self._handles.items()):
            if entry == job:
                del self._handles[key]

        return True

    async def list_completed_files(self, handle: TaskHandle) -> list[Path]:
        job = self._resolve(handle)
        if job is None:
            return []

        return await self._get_backend().list_completed_files(job)

    async def get_file_path(
        self,
        handle: TaskHandle,
        remote_filename: str,
        size: int | None = None,
    ) -> Path | None:
        job = self._resolve(handle)
        if job is None:
            return None

        return await self._get_backend().get_file_path(
            job,
            remote_filename,
        )

    async def diagnose_downloads_mount(self) -> MountDiagnosis:
        return MountDiagnosis(supported=False)

    # -- handle correlation --

    def _resolve(self, handle: TaskHandle) -> BackendJob | None:
        # Task IDs and payloads can be shared by retries. Only the exact handle
        # identifies the attempt whose files the host is allowed to clean up.
        if handle.source != SOURCE:
            return None
        return self._handles.get(getattr(handle, "job_name", "") or "")

    @staticmethod
    def _task_id_for(handle: TaskHandle, job: BackendJob | None) -> str:
        if job is not None:
            return job.task_id

        job_name = getattr(handle, "job_name", "") or ""
        prefix, _, suffix = job_name.partition(JOB_NAME_PREFIX)
        return suffix if not prefix and suffix else job_name

    @staticmethod
    async def _file_sizes(files: list[Path]) -> int:
        def _sum() -> int:
            return sum(
                file.stat().st_size
                for file in files
                if file.is_file()
            )

        return await run_blocking(_sum)
