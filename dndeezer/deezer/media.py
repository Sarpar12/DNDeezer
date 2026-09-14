from __future__ import annotations

from pathlib import Path
from typing import Callable, Protocol

from dndeezer.backend import DownloadTarget
from dndeezer.deezer.client import DeezerClient


ProgressCallback = Callable[[float], None]


class MediaAcquisitionError(Exception):
    pass


class MediaAcquirer(Protocol):
    async def acquire(
        self,
        target: DownloadTarget,
        destination: Path,
        *,
        on_progress: ProgressCallback | None = None,
    ) -> list[Path]:
        ...


class DirectDeezerMediaService:
    def __init__(
        self,
        client: DeezerClient,
    ) -> None:
        self.client = client

    async def acquire(
        self,
        target: DownloadTarget,
        destination: Path,
        *,
        on_progress: ProgressCallback | None = None,
    ) -> list[Path]:
        if target.kind == "album":
            return await self._acquire_album(
                target.deezer_id,
                destination,
                on_progress=on_progress,
            )

        if target.kind == "track":
            return await self._acquire_track(
                target.deezer_id,
                destination,
                on_progress=on_progress,
            )

        raise MediaAcquisitionError(
            f"Unsupported target kind: {target.kind}"
        )

    async def _acquire_album(
        self,
        album_id: int,
        destination: Path,
        *,
        on_progress: ProgressCallback | None = None,
    ) -> list[Path]:
        # STEP 4 MEDIA WORK GOES HERE LATER.
        #
        # At a high level this method will:
        #
        # 1. Resolve album metadata.
        # 2. Determine its tracks.
        # 3. Acquire each permitted media item.
        # 4. Write completed files under `destination`.
        # 5. Report progress through `on_progress`.
        # 6. Return final file paths.
        #
        # Do not return temporary/partial files.

        raise NotImplementedError(
            "Direct Deezer album acquisition is not implemented"
        )

    async def _acquire_track(
        self,
        track_id: int,
        destination: Path,
        *,
        on_progress: ProgressCallback | None = None,
    ) -> list[Path]:
        # STEP 4 MEDIA WORK GOES HERE LATER.
        #
        # Resolve the track and produce its final completed
        # output under `destination`.

        raise NotImplementedError(
            "Direct Deezer track acquisition is not implemented"
        )