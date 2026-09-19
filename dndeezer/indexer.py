from __future__ import annotations

import asyncio
from difflib import SequenceMatcher

from infrastructure.plugins.protocols import IndexerResult, PluginSearchResult
from models.common import ServiceStatus

from .deezer.client import DeezerClient
from .deezer.models import DeezerAlbum, DeezerTrack

SOURCE = "plugin:deezer-download"


class DeezerIndexer:
    def __init__(self, context):
        self.ctx = context

    @property
    def indexer_name(self) -> str:
        return SOURCE

    def _arl(self) -> str:
        return str(self.ctx.settings.get("arl") or "").strip()

    def is_configured(self) -> bool:
        return bool(self._arl())

    def _client(self) -> DeezerClient:
        return DeezerClient(self.ctx.http, self._arl())

    async def health_check(self) -> ServiceStatus:
        if not self.is_configured():
            return ServiceStatus(status="error", message="Deezer ARL is not configured")

        try:
            async with asyncio.timeout(10):
                session = await self._client().authenticate()
        except Exception as exc:  # noqa: BLE001 - error boundary, never crash the host
            return ServiceStatus(
                status="error", message=f"Deezer authentication failed: {exc}"
            )

        return ServiceStatus(
            status="ok", message=f"Authenticated with Deezer - {session.country}"
        )

    async def search_album(
        self,
        artist_name: str,
        album_title: str,
        year : int | None = None,
        track_count: int | None = None,
        *,
        timeout: float = 30.0,
    ) -> list[IndexerResult]:
        if not self.is_configured():
            return []

        try:
            async with asyncio.timeout(timeout):
                albums = await self._client().search_albums(
                    artist_name, album_title
                )
        except Exception as exc:  # noqa: BLE001 - search must degrade to empty results
            self.ctx.logger.warning("Deezer album search failed: %s", exc)
            return []

        results: list[IndexerResult] = []

        for album in albums:
            title = f"{album.artist.name} - {album.title}"
            results.append(IndexerResult(
                source=SOURCE,
                plugin=PluginSearchResult(
                    title=title,
                    score=self._album_score(
                        artist_name,
                        album_title,
                        album
                    ),
                    files = [],
                    payload=f"album:{album.id}",
                ),
            ))

        return results

    async def search_track(
        self,
        artist_name: str,
        track_title: str,
        album_title: str | None = None,
        duration_seconds: int | None = None,
        *,
        timeout: float = 30.0
    ) -> list[IndexerResult]:
        if not self.is_configured():
            return []

        try:
            async with asyncio.timeout(timeout):
                tracks = await self._client().search_tracks(
                    artist_name, track_title
                )
        except Exception as exc:  # noqa: BLE001 - search must degrade to empty results
            self.ctx.logger.warning("Deezer track search failed: %s", exc,)
            return []

        results: list[IndexerResult] = []

        for track in tracks:
            title = (f"{track.artist.name} - {track.title} ({track.album_title})")

            results.append(IndexerResult(
                source=SOURCE,
                plugin=PluginSearchResult(
                    title=title,
                    score=self._track_score(
                        artist_name, track_title, track
                    ),
                    files=[],
                    payload=f"track:{track.id}",
                ),
            ))

        return results

    def _album_score(
        self,
        artist_name: str,
        album_title: str,
        album: DeezerAlbum,
    ) -> float:
        candidate = f"{album.artist.name} - {album.title}"
        # Prefer DroppedNeedle own scorer if possible

        scoring = getattr(self.ctx, "scoring", None)

        if scoring is not None:
            return scoring.album_match(artist_name, album_title, candidate)
        # Fallback
        return (_similarity(artist_name, album.artist.name) * 0.5 + _similarity(album_title, album.title) * 0.5)

    def _track_score(
        self,
        artist_name: str,
        track_title: str,
        track: DeezerTrack,
    ) -> float:
        candidate = f"{track.artist.name} - {track.title}"
        scoring = getattr(self.ctx, "scoring", None)

        if scoring is not None:
            return scoring.track_match(artist_name, track_title, candidate)

        return (_similarity(artist_name, track.artist.name) * 0.5 + _similarity(track_title, track.title) * 0.5) 

def _similarity(left: str, right: str) -> float:
    left = left.strip().casefold()
    right = right.strip().casefold()

    if left == right:
        return 1.0

    return SequenceMatcher(None, left, right).ratio()