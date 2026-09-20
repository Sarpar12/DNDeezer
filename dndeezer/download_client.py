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
import shutil
from pathlib import Path

from models.common import ServiceStatus
from repositories.protocols.download_client import (
    DownloadMaterialization,
    DownloadTaskStatus,
    EnqueueRequest,
    MountDiagnosis,
    TaskHandle,
)

from .backend import BackendJob, parse_payload
from .backends.direct import DirectDeezerBackend, build_direct_backend
from .indexer import SOURCE

JOB_NAME_PREFIX = "droppedneedle-"


class DeezerDownloadClient:
    """Host download-client surface backed by ``DirectDeezerBackend``.

    ``TaskHandle`` has no field for internal job id, so handles are
    correlated by ``job_name`` (the engine's own
    ``droppedneedle-<task_id>`` convention). The map is in-memory: after a
    host restart the asyncio jobs are gone too, so unknown handles are
    reported as failed instead of hanging the engine.
    """

    def __init__(self, context) -> None:
        self.ctx = context
        self._backend: DirectDeezerBackend | None = None
        self._handles: dict[str, BackendJob] = {}

    # -- config helpers --

    def _arl(self) -> str:
        return str(self.ctx.settings.get("arl") or "").strip()

    def _downloads_dir(self) -> Path | None:
        raw = str(self.ctx.settings.get("downloads_dir") or "").strip()
        return Path(raw).expanduser() if raw else None

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

        handle = TaskHandle(
            source=SOURCE,
            job_name=f"{JOB_NAME_PREFIX}{request.task_id}",
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
            return DownloadMaterialization(state="missing")

        backend = self._get_backend()
        status = await backend.get_status(job)
        workspace = str(backend.downloads_dir / job.backend_id)

        if status.state == "completed":
            files = await backend.list_completed_files(job)

            return DownloadMaterialization(
                state="completed",
                workspace_path=workspace,
                file_paths=[str(file) for file in files],
            )

        if status.state in ("failed", "cancelled"):
            return DownloadMaterialization(
                state="failed",
                workspace_path=workspace,
            )

        return DownloadMaterialization(
            state="active",
            workspace_path=workspace,
        )

    async def discard_client_artifacts(self, handle: TaskHandle) -> bool:
        job = self._resolve(handle)
        if job is None:
            return False

        backend = self._get_backend()
        directory = (backend.downloads_dir / job.backend_id).resolve()

        if directory.is_relative_to(backend.downloads_dir):
            await asyncio.to_thread(shutil.rmtree, directory, True)

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
        job = self._handles.get(getattr(handle, "job_name", "") or "")
        if job is not None:
            return job

        # Recover by task id when the persisted job_name survived but the
        # map was rebuilt (e.g. tests or handle round-trips).
        job_name = getattr(handle, "job_name", "") or ""
        if job_name.startswith(JOB_NAME_PREFIX):
            task_id = job_name.removeprefix(JOB_NAME_PREFIX)
            for entry in self._handles.values():
                if entry.task_id == task_id:
                    return entry

        return None

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

        return await asyncio.to_thread(_sum)
