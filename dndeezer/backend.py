from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Protocol

DownloadKind = Literal["album", "track"]
BackendState = Literal["queued, downloading, completed, failed, cancelled"]


@dataclass(frozen=True, slots=True)
class DownloadTarget:
    kind: DownloadKind
    deezer_id: int

    @property
    def payload(self) -> str:
        return f"{self.kind}:{self.deezer_id}"


@dataclass(frozen=True,slots=True)
class BackendJob:
    task_id: str 
    backend_id: str 
    target: DownloadTarget


@dataclass(frozen=True, slots=True)
class BackendStatus:
    state: BackendState

    progress_percent: float = 0.0
    error: str | None = None


@dataclass(frozen=True, slots=True)
class BackendHealth:
    ok: bool
    message: str = ""

class DownloadBackend(Protocol):
    def is_configured(self) -> bool:
        ...

    async def health_check(self) -> BackendHealth:
        ...

    async def enqueue(
        self,
        *,
        task_id: str,
        target: DownloadTarget,
    ) -> BackendJob:
        ...

    async def get_status(
        self,
        job: BackendJob,
    ) -> BackendStatus:
        ...

    async def abort(
        self,
        job: BackendJob,
    ) -> bool:
        ...

    async def list_completed_files(
        self,
        job: BackendJob,
    ) -> list[Path]:
        ...

    async def get_file_path(
        self,
        job: BackendJob,
        filename: str,
    ) -> Path | None:
        ...


def parse_payload(payload: str) -> DownloadTarget:
    kind, separator, raw_id = payload.partition(":")

    if not separator:
        raise ValueError(
            f"Invalid Deezer payload: {payload!r}"
        )

    if kind not in ("album", "track"):
        raise ValueError(
            f"Unsupported Deezer target type: {kind!r}"
        )

    try:
        deezer_id = int(raw_id)
    except ValueError as exc:
        raise ValueError(
            f"Invalid Deezer ID: {raw_id!r}"
        ) from exc

    if deezer_id <= 0:
        raise ValueError(
            "Deezer ID must be positive"
        )

    return DownloadTarget(
        kind=kind,
        deezer_id=deezer_id,
    )