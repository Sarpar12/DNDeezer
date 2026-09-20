from __future__ import annotations

import asyncio
import unicodedata
from difflib import SequenceMatcher

from infrastructure.plugins.protocols import (
    IndexerResult,
    PluginSearchResult,
)

# DroppedNeedle v2.13.0 does not re-export ServiceStatus in the public API.
from models.common import ServiceStatus

from .compat import SearchFileRef
from .deezer.client import DeezerClient, _parse_track
from .deezer.media import _filename
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

        deadline = asyncio.get_running_loop().time() + timeout
        client = self._client()
        try:
            async with asyncio.timeout(timeout):
                albums = await client.search_albums(
                    artist_name, album_title
                )
        except Exception as exc:  # noqa: BLE001 - search must degrade to empty results
            self.ctx.logger.warning("Deezer album search failed: %s", exc)
            return []

        results: list[IndexerResult] = []

        for album in albums:
            artist_match = _similarity(
                _identity_text(artist_name), _identity_text(album.artist.name),
            )
            reason = None
            if artist_name.strip() and artist_match < 0.8:
                reason = "artist_mismatch"
            elif track_count and track_count > 1 and album.track_count is not None and album.track_count <= 1:
                reason = "single_track_release"
            if reason:
                self.ctx.logger.info(
                    "DNDeezer album rejected: album_id=%s artist=%s title=%s "
                    "tracks=%s expected_tracks=%s reason=%s",
                    album.id, album.artist.name, album.title,
                    album.track_count, track_count, reason,
                )
                continue
            title = f"{album.artist.name} - {album.title}"
            identity_score = self._album_score(artist_name, album_title, album)
            # Keep alternate editions eligible, but prefer complete editions
            # matching the requested track count over short/expanded releases.
            count_match = 0.5
            if track_count and album.track_count and album.track_count > 0:
                count_match = min(track_count, album.track_count) / max(track_count, album.track_count)
            score = identity_score
            if track_count and track_count > 1:
                score = identity_score * (0.8 + 0.2 * count_match)
            self.ctx.logger.info(
                "DNDeezer album candidate: payload=album:%s artist=%s title=%s "
                "tracks=%s expected_tracks=%s score=%.3f",
                album.id, album.artist.name, album.title, album.track_count, track_count, score,
            )
            results.append(IndexerResult(
                source=SOURCE,
                plugin=PluginSearchResult(
                    title=title,
                    score=score,
                    files = [],
                    payload=f"album:{album.id}",
                ),
            ))

        results.sort(key=lambda result: result.plugin.score, reverse=True)
        if track_count == 1:
            # The host uses search_album for missing-track requests too. Expose
            # independently downloadable files so its track matcher can choose.
            # A genuine single works the same way: its sole track is the release.
            return await self._album_tracks(client, results, deadline)
        return results

    async def _album_tracks(self, client, albums, deadline) -> list[IndexerResult]:
        results: list[IndexerResult] = []
        seen: set[int] = set()
        try:
            async with asyncio.timeout_at(deadline):
                for result in albums:
                    release = result.plugin
                    album_id = int(release.payload.split(":", 1)[1])
                    try:
                        album, tracks = await client.get_album_with_tracklist(album_id)
                    except Exception as exc:  # noqa: BLE001 - isolate unavailable albums
                        self.ctx.logger.warning(
                            "DNDeezer track candidates unavailable: album_id=%s error=%s: %s",
                            album_id, type(exc).__name__, exc,
                        )
                        continue
                    for data in tracks:
                        # Album tracklist endpoints return raw dictionaries and
                        # commonly omit the parent album metadata.
                        track = _parse_track({
                            **data,
                            "album": {"id": album.id, "title": album.title},
                        })
                        if track.id in seen:
                            continue
                        seen.add(track.id)
                        results.append(self._track_result(track, release.score))
                        self.ctx.logger.info(
                            "DNDeezer track candidate: payload=track:%s album_id=%s "
                            "artist=%s title=%s album=%s",
                            track.id, album.id, track.artist.name, track.title, album.title,
                        )
        except TimeoutError:
            self.ctx.logger.warning(
                "DNDeezer track candidate search timed out: candidates=%s", len(results),
            )
        # Never fall back to album payloads on failure: that would download
        # whole releases without matching the requested song again.
        return results

    def _track_result(self, track: DeezerTrack, score: float) -> IndexerResult:
        filename = _filename({"artist": {"name": track.artist.name}, "title": track.title}, "FLAC")
        return IndexerResult(
            source=SOURCE,
            plugin=PluginSearchResult(
                title=f"{track.artist.name} - {track.album_title}",
                score=score,
                files=[SearchFileRef(
                    username=SOURCE, filename=filename, size=0,
                    parent_directory=f"{track.artist.name} - {track.album_title}",
                    extension="flac",
                )],
                payload=f"track:{track.id}",
            ),
        )

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
            results.append(self._track_result(
                track, self._track_score(artist_name, track_title, track),
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


def _identity_text(value: str) -> str:
    """Ignore punctuation, case and accents when comparing artist identities."""
    normalized = unicodedata.normalize("NFKD", value.casefold())
    return "".join(char for char in normalized if char.isalnum())
