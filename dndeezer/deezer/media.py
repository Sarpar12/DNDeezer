from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
from collections.abc import AsyncIterator, Callable
from pathlib import Path
from typing import Any, Protocol

from cryptography.hazmat.decrepit.ciphers import algorithms as decrepit_algorithms
from cryptography.hazmat.primitives.ciphers import Cipher, modes

from dndeezer._async import run_blocking as _run_blocking
from dndeezer.backend import DownloadTarget
from dndeezer.deezer.client import DeezerClient, DeezerError
from dndeezer.deezer.metadata import write_flac_metadata
from dndeezer.deezer.models import DeezerAlbum, DeezerSession

ProgressCallback = Callable[[float], None]

MEDIA_API = "https://media.deezer.com/v1/get_url"
BF_SECRET = b"g4el58wc0zvf9na1"
BF_IV = bytes(range(8))
CHUNK_SIZE = 2048
CIPHER = "BF_CBC_STRIPE"
QUALITY_PRIORITY = ("FLAC", "MP3_320", "MP3_128")
CDN_HEADERS = {"User-Agent": "Mozilla/5.0"}
logger = logging.getLogger(__name__)


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
        # Do not return temporary/partial files.
        session = await self.client.authenticate()
        album, tracks = await self.client.get_album_with_tracklist(album_id)

        if not tracks:
            raise MediaAcquisitionError(
                f"Deezer album {album_id} has no tracks"
            )

        album_dir = await _run_blocking(self._album_directory, destination, album)

        total = len(tracks)
        width = max(2, len(str(total)))
        files: list[Path] = []
        failures: list[str] = []

        for position, track in enumerate(tracks, start=1):
            try:
                track_files = await self._acquire_track(
                    _track_id(track),
                    album_dir,
                    session=session,
                    album=album,
                    position=position,
                    stem=f"{position:0{width}d} - {track.get('title') or 'track'}",
                    on_progress=_wrap_album_progress(
                        on_progress,
                        total=total,
                        position=position,
                    ),
                )
            except (MediaAcquisitionError, DeezerError) as exc:
                failures.append(f"track {position}: {exc}")
                cause = exc.__cause__ or exc
                logger.warning(
                    "Deezer album track failed: album_id=%s position=%s/%s "
                    "track_id=%s error_type=%s",
                    album_id, position, total, track.get("id"), type(cause).__name__,
                )
                continue

            files.extend(track_files)

        if not files:
            raise MediaAcquisitionError(
                f"Deezer album {album_id} produced no files"
                + (f"; first failure: {failures[0]}" if failures else "")
            )

        return files

    def _album_directory(self, destination: Path, album: DeezerAlbum) -> Path:
        dest = Path(destination).expanduser().resolve()

        if not dest.is_dir():
            raise MediaAcquisitionError(
                f"Album destination does not exist: {dest}"
            )

        folder = _safe_component(f"{album.artist.name} - {album.title}")
        album_dir = dest / (folder or "album")
        album_dir.mkdir(parents=True, exist_ok=True)

        return album_dir

    async def _acquire_track(
        self,
        track_id: int,
        destination: Path,
        *,
        on_progress: ProgressCallback | None = None,
        session: DeezerSession | None = None,
        stem: str | None = None,
        album: DeezerAlbum | None = None,
        position: int | None = None,
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

        try:
            url, quality = _select_media_source(media_data)
        except MediaAcquisitionError:
            diagnostic = await _run_blocking(
                _sanitized_media_response,
                media_data,
                (self.client._arl, session.license_token, session.api_token, str(track_token)),
            )
            logger.warning(
                "Deezer media selection failed: track_id=%s http_status=%s response=%s",
                used_id, media_response.status_code, diagnostic,
            )
            raise
        temporary_path = destination / f".{used_id}.part"
        output_path = destination / _filename(track, quality, stem=stem)

        try:
            async with self.client._http.stream(
                "GET", url, headers=CDN_HEADERS,
            ) as cdn_response:
                cdn_response.raise_for_status()
                await _write_media(
                    cdn_response, used_id, temporary_path, on_progress,
                )

            if quality == "FLAC":
                await _run_blocking(
                    write_flac_metadata, temporary_path, track,
                    album=album, position=position,
                )
            await _run_blocking(temporary_path.replace, output_path)
            if on_progress:
                on_progress(100)
            return [output_path]
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            raise MediaAcquisitionError(
                f"Failed to acquire track {used_id}: {type(exc).__name__}: {exc}"
            ) from exc
        finally:
            await _run_blocking(lambda: temporary_path.unlink(missing_ok=True))

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


def _sanitized_media_response(data: Any, secrets: tuple[str, ...]) -> str:
    """Bounded response diagnostics; retain schema/error fields, never credentials.

    Unknown fields (including their names) are omitted. Known request secrets
    and URLs are also scrubbed from free-text errors before JSON encoding.
    """
    allowed = {
        "data", "error", "errors", "code", "message", "media", "format",
        "cipher", "type", "media_type", "sources", "provider", "url",
    }
    secret_values = sorted({value for value in secrets if value}, key=len, reverse=True)

    def sanitize(value: Any, depth: int = 0) -> Any:
        if depth > 8:
            return "[truncated]"
        if isinstance(value, dict):
            result = {}
            omitted = 0
            for key, item in value.items():
                if key not in allowed:
                    omitted += 1
                else:
                    result[key] = "[redacted]" if key == "url" else sanitize(item, depth + 1)
            if omitted:
                result["_omitted_fields"] = omitted
            return result
        if isinstance(value, list):
            result = [sanitize(item, depth + 1) for item in value[:10]]
            if len(value) > 10:
                result.append("[truncated]")
            return result
        if isinstance(value, str):
            for secret in secret_values:
                value = value.replace(secret, "[redacted]")
            value = re.sub(r"https?://[^\s\"<>]+", "[redacted-url]", value, flags=re.IGNORECASE)
            value = re.sub(
                r"\b(?:arl|[\w-]*token|cookie|authorization)\b[\"']?\s*[:=]\s*[^\r\n]+",
                "[redacted-credential]", value, flags=re.IGNORECASE,
            )
            return value[:500] + ("[truncated]" if len(value) > 500 else "")
        if value is None or isinstance(value, (bool, int, float)):
            return value
        return "[unsupported value]"

    text = json.dumps(sanitize(data), ensure_ascii=True)
    return text[:8000] + ("[truncated]" if len(text) > 8000 else "")


def _select_media_source(data: Any) -> tuple[str, str]:
    items = data.get("data") if isinstance(data, dict) else None
    first = items[0] if isinstance(items, list) and items else None
    media = first.get("media") if isinstance(first, dict) else None
    available: dict[str, str] = {}
    if isinstance(media, list):
        for item in media:
            if not isinstance(item, dict):
                continue
            sources = item.get("sources")
            cipher = item.get("cipher")
            if isinstance(cipher, dict):
                cipher = cipher.get("type")
            if (
                item.get("format")
                and cipher == CIPHER
                and isinstance(sources, list)
                and sources
            ):
                first_source = sources[0]
                url = first_source.get("url") if isinstance(first_source, dict) else None
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


class _StripeDecoder:
    def __init__(self, track_id: int) -> None:
        self.cipher = Cipher(
            decrepit_algorithms.Blowfish(_blowfish_key(track_id)),
            modes.CBC(BF_IV),
        )
        self.buffer = b""
        self.index = 0

    def update(self, piece: bytes) -> bytes:
        data = self.buffer + piece
        end = len(data) - len(data) % CHUNK_SIZE
        output = bytearray()
        for offset in range(0, end, CHUNK_SIZE):
            chunk = data[offset:offset + CHUNK_SIZE]
            if self.index % 3 == 0:
                # Each encrypted stripe starts a new CBC message with BF_IV.
                # Chaining across stripes corrupts the next stripe's first block.
                decryptor = self.cipher.decryptor()
                chunk = decryptor.update(chunk) + decryptor.finalize()
            output.extend(chunk)
            self.index += 1
        self.buffer = data[end:]
        return bytes(output)

    def finish(self) -> bytes:
        tail, self.buffer = self.buffer, b""
        return tail


async def _write_media(
    response: Any,
    track_id: int,
    path: Path,
    on_progress: ProgressCallback | None,
) -> None:
    output = None
    decoder = None

    def open_output() -> None:
        nonlocal output, decoder
        decoder = _StripeDecoder(track_id)
        output = path.open("wb")

    def write_batch(piece: bytes) -> int:
        data = decoder.update(piece)
        output.write(data)
        return len(data)

    def finish() -> None:
        output.write(decoder.finish())

    try:
        await _run_blocking(open_output)
        total_bytes = _content_length(response)
        written = 0
        # One awaited worker batch at a time bounds memory and preserves cipher
        # order. Progress callbacks stay on the host's event-loop thread.
        async for piece in _response_chunks(response):
            written += await _run_blocking(write_batch, piece)
            if on_progress and total_bytes:
                on_progress(min(written / total_bytes * 100, 100))
        await _run_blocking(finish)
    finally:
        if output is not None:
            await _run_blocking(output.close)


async def _decrypted_chunks(response: Any, track_id: int) -> AsyncIterator[bytes]:
    decoder = await _run_blocking(_StripeDecoder, track_id)
    async for piece in _response_chunks(response):
        yield await _run_blocking(decoder.update, piece)
    yield await _run_blocking(decoder.finish)


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
        await _run_blocking(close)
