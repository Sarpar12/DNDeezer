from __future__ import annotations

from typing import Any, Protocol

from .models import (
    DeezerAlbum,
    DeezerArtist,
    DeezerSession,
    DeezerTrack,
)


class ResponseLike(Protocol):
    status_code: int

    def json(self) -> Any: ...

    def raise_for_status(self) -> None: ...


class AsyncHttpClient(Protocol):
    async def get(
        self,
        url: str,
        **kwargs: Any,
    ) -> ResponseLike: ...

    async def post(
        self,
        url: str,
        **kwargs: Any,
    ) -> ResponseLike: ...


class DeezerError(Exception):
    pass


class DeezerAuthError(DeezerError):
    pass


class DeezerApiError(DeezerError):
    pass


class DeezerClient:
    API_BASE = "https://api.deezer.com"
    GW_URL = "https://www.deezer.com/ajax/gw-light.php"

    def __init__(
        self,
        http: AsyncHttpClient,
        arl: str,
    ) -> None:
        if not arl:
            raise ValueError("Deezer ARL is required")

        self._http = http
        self._arl = arl
        self._api_token: str | None = None

    @property
    def api_token(self) -> str | None:
        return self._api_token

    async def authenticate(self) -> DeezerSession:
        data = await self._gw_call(
            "deezer.getUserData",
            api_token="null",
        )

        results = data.get("results")
        if not isinstance(results, dict):
            raise DeezerAuthError("Invalid Deezer authentication response")

        user = results.get("USER")
        if not isinstance(user, dict):
            raise DeezerAuthError("Deezer response did not contain a user")

        user_id = _as_int(user.get("USER_ID"))

        if not user_id:
            raise DeezerAuthError("Invalid or expired Deezer ARL")

        api_token = str(results.get("checkForm") or "")
        if not api_token:
            raise DeezerAuthError("Deezer response did not contain an API token")

        self._api_token = api_token

        return DeezerSession(
            user_id=user_id,
            country=str(results.get("COUNTRY") or ""),
            api_token=api_token,
        )

    async def search_albums(
        self,
        artist: str,
        album: str,
        *,
        limit: int = 25,
    ) -> list[DeezerAlbum]:
        query = f"{artist} {album}"

        data = await self._get(
            "/search/album",
            params={
                "q": query,
                "limit": limit,
            },
        )

        results = data.get("data", [])

        if not isinstance(results, list):
            return []

        return [
            _parse_album(item)
            for item in results
            if isinstance(item, dict)
        ]

    async def search_tracks(
        self,
        artist: str,
        track: str,
        *,
        limit: int = 25,
    ) -> list[DeezerTrack]:
        query = f"{artist} {track}"

        data = await self._get(
            "/search/track",
            params={
                "q": query,
                "limit": limit,
            },
        )

        results = data.get("data", [])

        if not isinstance(results, list):
            return []

        return [
            _parse_track(item)
            for item in results
            if isinstance(item, dict)
        ]

    async def get_album(self, album_id: int | str) -> DeezerAlbum:
        data = await self._get(f"/album/{album_id}")
        return _parse_album(data)

    async def get_track(self, track_id: int | str) -> DeezerTrack:
        data = await self._get(f"/track/{track_id}")
        return _parse_track(data)

    async def _get(
        self,
        path: str,
        *,
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        response = await self._http.get(
            f"{self.API_BASE}{path}",
            params=params,
        )

        try:
            response.raise_for_status()
        except Exception as exc:
            raise DeezerApiError(
                f"Deezer HTTP error: {response.status_code}"
            ) from exc

        data = response.json()

        if not isinstance(data, dict):
            raise DeezerApiError("Unexpected Deezer response")

        error = data.get("error")
        if error:
            raise DeezerApiError(f"Deezer API error: {error}")

        return data

    async def _gw_call(
        self,
        method: str,
        payload: dict[str, Any] | None = None,
        *,
        api_token: str | None = None,
    ) -> dict[str, Any]:
        response = await self._http.post(
            self.GW_URL,
            params={
                "api_version": "1.0",
                "api_token": api_token or self._api_token or "null",
                "input": "3",
                "method": method,
            },
            json=payload or {},
            cookies={
                "arl": self._arl,
            },
        )

        try:
            response.raise_for_status()
        except Exception as exc:
            raise DeezerApiError(
                f"Deezer gateway HTTP error: {response.status_code}"
            ) from exc

        data = response.json()

        if not isinstance(data, dict):
            raise DeezerApiError("Unexpected Deezer gateway response")

        error = data.get("error")
        if error:
            raise DeezerApiError(f"Deezer gateway error: {error}")

        return data


def _parse_artist(data: Any) -> DeezerArtist:
    if not isinstance(data, dict):
        data = {}

    return DeezerArtist(
        id=_as_int(data.get("id")),
        name=str(data.get("name") or "").rstrip(":"),
    )


def _parse_album(data: dict[str, Any]) -> DeezerAlbum:
    artist = _parse_artist(data.get("artist"))

    track_count = data.get("nb_tracks")
    if track_count is None:
        track_count = data.get("track_count")

    return DeezerAlbum(
        id=_as_int(data.get("id")),
        title=str(data.get("title") or ""),
        artist=artist,
        cover_url=(
            data.get("cover_xl")
            or data.get("cover_big")
            or data.get("cover")
        ),
        release_date=data.get("release_date"),
        track_count=(
            _as_int(track_count)
            if track_count is not None
            else None
        ),
    )


def _parse_track(data: dict[str, Any]) -> DeezerTrack:
    artist = _parse_artist(data.get("artist"))

    album = data.get("album")
    if not isinstance(album, dict):
        album = {}

    return DeezerTrack(
        id=_as_int(data.get("id")),
        title=str(data.get("title") or ""),
        artist=artist,
        album_id=_as_int(album.get("id")),
        album_title=str(album.get("title") or ""),
        duration_seconds=_as_int(data.get("duration")),
        track_number=_optional_int(data.get("track_position")),
        disc_number=_optional_int(data.get("disk_number")),
        explicit=bool(data.get("explicit_lyrics", False)),
    )


def _as_int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _optional_int(value: Any) -> int | None:
    if value is None:
        return None

    result = _as_int(value)
    return result or None


def _escape_query(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')