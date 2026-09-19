from __future__ import annotations

import asyncio
import hashlib
from collections.abc import AsyncIterator, Callable
from pathlib import Path
from typing import Any, Protocol

from cryptography.hazmat.decrepit.ciphers import algorithms as decrepit_algorithms
from cryptography.hazmat.primitives.ciphers import Cipher, modes

from dndeezer.backend import DownloadTarget
from dndeezer.deezer.client import DeezerClient, DeezerError
from dndeezer.deezer.models import DeezerAlbum, DeezerSession

ProgressCallback = Callable[[float], None]

MEDIA_API = "https://media.deezer.com/v1/get_url"
BF_SECRET = b"g4el58wc0zvf9na1"
BF_IV = bytes(range(8))
CHUNK_SIZE = 2048
CIPHER = "BF_CBC_STRIPE"
QUALITY_PRIORITY = ("FLAC", "MP3_320", "MP3_128")
CDN_HEADERS = {"User-Agent": "Mozilla/5.0"}


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
        session: DeezerSession | None = None,
        stem: str | None = None,
    ) -> list[Path]:
        if session is None:
            session = await self.client.authenticate()
        track = await self.client._get(f"/track/{track_id}")
        used_id = _track_id(track)

        if not track.get("readable", True):
            page_data = await self._page_data(session, used_id)
            alternative_id = await self._find_alternative(
                track,
                page_data,
            )
            if alternative_id is None:
                raise MediaAcquisitionError(
                    f"Track {used_id} is not readable and has no alternative"
                )
            track = await self.client._get(f"/track/{alternative_id}")
            used_id = _track_id(track)

        page_data = await self._page_data(session, used_id)
        track_token = page_data.get("TRACK_TOKEN") or track.get("track_token")
        if not track_token:
            raise MediaAcquisitionError(
                f"No media token is available for track {used_id}"
            )

        media_response = await self.client._http.post(
            MEDIA_API,
            headers={"Cookie": f"arl={self.client._arl}"},
            json={
                "license_token": session.license_token,
                "media": [{
                    "type": "FULL",
                    "formats": [
                        {"cipher": CIPHER, "format": quality}
                        for quality in QUALITY_PRIORITY
                    ],
                }],
                "track_tokens": [track_token],
            },
        )

        try:
            media_response.raise_for_status()
            media_data = media_response.json()
        except Exception as exc:
            raise MediaAcquisitionError(
                f"Failed to resolve media for track {used_id}: {exc}"
            ) from exc
        finally:
            await _close_response(media_response)

        url, quality = _select_media_source(media_data)
        cdn_response = await self.client._http.get(url, headers=CDN_HEADERS)
        temporary_path = destination / f".{used_id}.part"
        output_path = destination / _filename(track, quality, stem=stem)

        try:
            cdn_response.raise_for_status()
            total_bytes = _content_length(cdn_response)
            written = 0

            with temporary_path.open("wb") as output:
                async for chunk in _decrypted_chunks(
                    cdn_response,
                    used_id,
                ):
                    output.write(chunk)
                    written += len(chunk)
                    if on_progress and total_bytes:
                        on_progress(min(written / total_bytes * 100, 100))

            temporary_path.replace(output_path)
            if on_progress:
                on_progress(100)
            return [output_path]
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            raise MediaAcquisitionError(
                f"Failed to acquire track {used_id}: {exc}"
            ) from exc
        finally:
            await _close_response(cdn_response)
            temporary_path.unlink(missing_ok=True)

    async def _page_data(
        self,
        session: DeezerSession,
        track_id: int,
    ) -> dict[str, Any]:
        try:
            data = await self.client._gw_call(
                "deezer.pageTrack",
                {"sng_id": str(track_id)},
                api_token=session.api_token,
            )
        except DeezerError:
            return {}

        results = data.get("results")
        if not isinstance(results, dict):
            return {}
        page_data = results.get("DATA")
        return page_data if isinstance(page_data, dict) else {}

    async def _find_alternative(
        self,
        track: dict[str, Any],
        page_data: dict[str, Any],
    ) -> int | None:
        original_id = track.get("id")

        fallback = (page_data.get("FALLBACK") or {}).get("SNG_ID")
        if fallback not in (None, "", "0", 0):
            try:
                fallback_id = int(fallback)
            except (TypeError, ValueError):
                fallback_id = None
            if fallback_id and fallback_id != original_id:
                return fallback_id

        isrc = page_data.get("ISRC") or track.get("isrc")
        if isrc:
            candidate = await self._readable_track_by_isrc(str(isrc))
            if candidate and candidate != original_id:
                return candidate

        artist = (track.get("artist") or {}).get("name", "")
        query = f"{artist} {track.get('title', '')}".strip()
        if query:
            results = await self.client._get(
                "/search/track",
                params={"q": query, "limit": 1},
            )
            matches = results.get("data")
            if isinstance(matches, list) and matches:
                candidate = _track_id(matches[0])
                if candidate != original_id:
                    return candidate
        return None

    async def _readable_track_by_isrc(self, isrc: str) -> int | None:
        try:
            data = await self.client._get(f"/track/isrc:{isrc}")
        except DeezerError:
            return None

        if not isinstance(data, dict) or data.get("error"):
            return None
        if not data.get("readable", False):
            return None
        try:
            return _track_id(data)
        except MediaAcquisitionError:
            return None


def _track_id(track: dict[str, Any]) -> int:
    raw_id = track.get("id")
    if raw_id is None:
        raise MediaAcquisitionError("Deezer returned an invalid track ID")

    try:
        value = int(raw_id)
    except (TypeError, ValueError) as exc:
        raise MediaAcquisitionError("Deezer returned an invalid track ID") from exc
    if value <= 0:
        raise MediaAcquisitionError("Deezer returned an invalid track ID")
    return value


def _select_media_source(data: Any) -> tuple[str, str]:
    items = data.get("data") if isinstance(data, dict) else None
    media = items[0].get("media") if isinstance(items, list) and items else None
    available: dict[str, str] = {}
    if isinstance(media, list):
        for item in media:
            if not isinstance(item, dict):
                continue
            sources = item.get("sources")
            if (
                item.get("format")
                and item.get("cipher") == CIPHER
                and isinstance(sources, list)
                and sources
            ):
                url = sources[0].get("url")
                if url:
                    available[str(item["format"])] = str(url)

    for quality in QUALITY_PRIORITY:
        if quality in available:
            return available[quality], quality
    if available:
        quality = next(iter(available))
        return available[quality], quality
    raise MediaAcquisitionError("Deezer returned no media sources")


def _safe_component(value: str) -> str:
    return "".join(
        char if char.isalnum() or char in " -()[]" else "_" for char in value
    ).strip()


def _filename(
    track: dict[str, Any],
    quality: str,
    *,
    stem: str | None = None,
) -> str:
    if stem is None:
        artist = (track.get("artist") or {}).get("name", "")
        title = track.get("title", "track")
        stem = " - ".join(str(value) for value in (artist, title) if value)

    safe = _safe_component(stem)
    return f"{safe or 'track'}{'.flac' if quality == 'FLAC' else '.mp3'}"


def _wrap_album_progress(
    on_progress: ProgressCallback | None,
    *,
    total: int,
    position: int,
) -> ProgressCallback | None:
    if on_progress is None:
        return None

    def report(percent: float) -> None:
        clamped = min(max(float(percent), 0.0), 100.0)
        overall = (position - 1 + clamped / 100.0) * 100.0 / total
        on_progress(min(overall, 100.0))

    return report


def _content_length(response: Any) -> int | None:
    headers = getattr(response, "headers", {})
    try:
        value = headers.get("content-length")
        return int(value) if value else None
    except (TypeError, ValueError):
        return None


def _blowfish_key(track_id: int) -> bytes:
    digest = hashlib.md5(str(track_id).encode("utf-8")).hexdigest()
    return bytes(
        ord(digest[index]) ^ ord(digest[index + 16]) ^ BF_SECRET[index]
        for index in range(16)
    )


async def _decrypted_chunks(response: Any, track_id: int) -> AsyncIterator[bytes]:
    decryptor = Cipher(
        decrepit_algorithms.Blowfish(_blowfish_key(track_id)),
        modes.CBC(BF_IV),
    ).decryptor()
    buffer = b""
    index = 0

    async for piece in _response_chunks(response):
        buffer += piece
        while len(buffer) >= CHUNK_SIZE:
            chunk, buffer = buffer[:CHUNK_SIZE], buffer[CHUNK_SIZE:]
            if index % 3 == 0:
                chunk = decryptor.update(chunk)
            yield chunk
            index += 1

    if buffer:
        yield buffer

    decryptor.finalize()


async def _response_chunks(response: Any) -> AsyncIterator[bytes]:
    aiter_raw = getattr(response, "aiter_raw", None)
    if aiter_raw is not None:
        async for chunk in aiter_raw(65536):
            yield chunk
        return

    content = getattr(response, "content", b"")
    if content:
        yield content


async def _close_response(response: Any) -> None:
    close = getattr(response, "aclose", None)
    if close is not None:
        await close()
        return
    close = getattr(response, "close", None)
    if close is not None:
        close()