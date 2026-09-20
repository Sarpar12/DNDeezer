"""DroppedNeedle ``download_client`` capability surface for Deezer.

This is the only module that knows about host download-client types. It
adapts ``EnqueueRequest``/``TaskHandle`` calls onto the host-agnostic
``DirectDeezerBackend`` (built via ``build_direct_backend``), which owns the
async job lifecycle and the media pipeline.

Folder mode: results are returned with empty ``files`` lists, so the engine
imports whole releases through ``list_completed_files``.
"""

from __future__ import annotations

import asyncio
import errno
import os
import shutil
from pathlib import Path
from uuid import uuid4

from infrastructure.plugins.protocols import (
    DownloadMaterialization,
    DownloadTaskStatus,
    EnqueueRequest,
    MountDiagnosis,
    TaskHandle,
)

# DroppedNeedle v2.13.0 does not re-export ServiceStatus in the public API.
from models.common import ServiceStatus

from db import JobStore

from ._async import run_blocking
from .backend import BackendJob, parse_payload
from .backends.direct import DirectDeezerBackend, build_direct_backend
from .indexer import SOURCE

JOB_NAME_PREFIX = "droppedneedle-"


class DeezerDownloadClient:
    """Host download-client surface backed by ``DirectDeezerBackend``.

    Handles are correlated by an attempt-specific ``job_name``. Preserve the
    host's candidate suffix and never reuse a name for another backend job.
    Live jobs are held in memory; SQLite retains ownership and completed
    evidence across restarts. Interrupted workers are reported as failed.
    """

    def __init__(self, context) -> None:
        self.ctx = context
        self._backend: DirectDeezerBackend | None = None
        self._handles: dict[str, BackendJob] = {}
        self._issued_handle_names: set[str] = set()
        state_dir = Path(str(context.settings.get("state_dir") or "/app/config/dndeezer"))
        self._store = JobStore(state_dir / "jobs.sqlite3")
        self._store_ready = False
        self._store_lock = asyncio.Lock()

    async def _ensure_store(self) -> None:
        async with self._store_lock:
            if not self._store_ready:
                await run_blocking(self._store.initialize)
                # Old async workers must be stopped before replacing this instance.
                recovered = await run_blocking(self._store.recover_interrupted)
                self.ctx.logger.info("DNDeezer job registry ready: %s; interrupted=%s",
                                     self._store.path, recovered)
                self._store_ready = True

    async def _record(self, handle: TaskHandle):
        if handle.source != SOURCE:
            return None
        await self._ensure_store()
        return await run_blocking(self._store.get, handle.job_name)

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
        try:
            await self._ensure_store()
        except Exception as exc:  # noqa: BLE001 - report storage health to the host
            return ServiceStatus(status="error", message=f"Job database unavailable: {exc}")

        health = await self._get_backend().health_check()
        if not health.ok:
            return ServiceStatus(status="error", message=health.message)

        return ServiceStatus(status="ok", message=health.message)

    # -- DownloadClientProtocol: job lifecycle --

    async def enqueue(self, request: EnqueueRequest) -> TaskHandle:
        payload = (getattr(request, "payload", "") or "").strip()
        target = parse_payload(payload)  # ValueError -> orchestration error

        await self._ensure_store()
        job_name = request.job_name or f"{JOB_NAME_PREFIX}{request.task_id}"
        if job_name in self._issued_handle_names or await run_blocking(self._store.get, job_name):
            job_name = f"{job_name}-{uuid4().hex}"
        self._issued_handle_names.add(job_name)
        backend = self._get_backend()

        async def before_start(job):
            await run_blocking(
                self._store.create, job_name=job_name, task_id=job.task_id,
                backend_id=job.backend_id, payload=payload,
                mount_root=backend.downloads_dir,
                workspace_path=backend.downloads_dir / job.backend_id,
            )

        async def on_finished(status, files):
            await run_blocking(self._store.update, job_name, state=status.state,
                               file_paths=tuple(files), error=status.error)

        job = await backend.enqueue(task_id=request.task_id, target=target,
                                    before_start=before_start, on_finished=on_finished)
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
            record = await self._record(handle)
            if record is not None:
                return DownloadTaskStatus(
                    task_id=record.task_id,
                    status="completed" if record.state == "completed" else "failed",
                    error=record.error or (None if record.state == "completed" else record.state),
                    files_total=len(record.file_paths),
                    files_completed=len(record.file_paths) if record.state == "completed" else 0,
                    progress_percent=100.0 if record.state == "completed" else 0.0,
                )
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
            status="queued" if status.state == "queued" else "downloading",
            progress_percent=status.progress_percent,
            matched_transfers=1,
            has_active_transfer=status.state == "downloading",
            bytes_downloaded=status.bytes_downloaded,
        )

    async def abort(self, handle: TaskHandle) -> bool:
        job = self._resolve(handle)
        if job is None:
            return await self._record(handle) is not None

        aborted = await self._get_backend().abort(job)
        if aborted:
            await run_blocking(self._store.update, handle.job_name, state="cancelled")
        return aborted

    # -- DownloadClientProtocol: materialization and files --

    async def inspect_materialization(
        self,
        handle: TaskHandle,
    ) -> DownloadMaterialization:
        job = self._resolve(handle)
        if job is None:
            record = await self._record(handle)
            if record is not None:
                healthy = await run_blocking(self._mount_healthy, record.mount_root)
                self.ctx.logger.info(
                    "DNDeezer recovered materialization: job=%s state=%s mount_root=%s mount_healthy=%s",
                    handle.job_name, record.state, record.mount_root, healthy,
                )
                return DownloadMaterialization(
                    state=("missing" if record.state == "cleaned" else
                           "completed" if record.state == "completed" else "failed"),
                    mount_root=str(record.mount_root), mount_healthy=healthy,
                    workspace_path=str(record.workspace_path),
                    file_paths=[str(p) for p in record.file_paths] if record.state != "cleaned" else [],
                )
            self.ctx.logger.warning(
                "DNDeezer inspect_materialization: unknown handle job=%s "
                "(known=%s) - reporting missing",
                getattr(handle, "job_name", ""),
                sorted(self._handles),
            )
            root = self._downloads_dir()
            healthy = bool(root and await run_blocking(self._mount_healthy, root))
            return DownloadMaterialization(state="missing", mount_root=str(root) if root else "",
                                           mount_healthy=healthy)

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
        record = await self._record(handle)
        if record is None:
            self.ctx.logger.warning(
                "DNDeezer discard_client_artifacts: unknown handle job=%s "
                "(known=%s) - host will retry cleanup",
                getattr(handle, "job_name", ""),
                sorted(self._handles),
            )
            return False

        job = self._resolve(handle)
        if job is not None:
            status = await self._get_backend().get_status(job)
            if status.state in ("queued", "downloading"):
                self.ctx.logger.warning("DNDeezer discard refused for active job=%s", handle.job_name)
                return False

        def discard() -> None:
            root = record.mount_root
            directory = record.workspace_path
            if (root.resolve() != root or directory.resolve() != directory
                    or directory.parent != root or directory.name != record.backend_id):
                raise OSError("Workspace ownership/confinement check failed")
            # An absent mount is not proof that a workspace has been removed.
            with os.scandir(root) as entries:
                next(entries, None)
            try:
                directory.lstat()
            except FileNotFoundError:
                return
            if record.state == "cleaned":
                raise OSError("Previously cleaned workspace has reappeared")
            shutil.rmtree(directory)
            if directory.exists():
                raise OSError("Workspace still exists after removal")

        self.ctx.logger.info("DNDeezer discard attempt: job=%s workspace=%s",
                             handle.job_name, record.workspace_path)
        try:
            await run_blocking(discard)
        except OSError as exc:
            self.ctx.logger.warning(
                "DNDeezer discard failed: job=%s workspace=%s error=%s",
                handle.job_name, record.workspace_path, exc,
            )
            return False

        await run_blocking(self._store.mark_cleaned, handle.job_name)
        self.ctx.logger.info("DNDeezer discard complete: job=%s", handle.job_name)

        for key, entry in list(self._handles.items()):
            if entry == job:
                del self._handles[key]

        return True

    async def list_completed_files(self, handle: TaskHandle) -> list[Path]:
        job = self._resolve(handle)
        if job is None:
            record = await self._record(handle)
            return list(record.file_paths) if record and record.state == "completed" else []

        return await self._get_backend().list_completed_files(job)

    async def get_file_path(
        self,
        handle: TaskHandle,
        remote_filename: str,
        size: int | None = None,
    ) -> Path | None:
        job = self._resolve(handle)
        if job is None:
            for file in await self.list_completed_files(handle):
                if file.name == Path(remote_filename).name:
                    record = await self._record(handle)
                    if await run_blocking(lambda file=file, record=record: file.resolve().is_relative_to(record.workspace_path)):
                        return file
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
