from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class DeezerSession:
    user_id: int
    country: str
    api_token: str
    license_token: str = ""


@dataclass(frozen=True, slots=True)
class DeezerArtist:
    id: int
    name: str


@dataclass(frozen=True, slots=True)
class DeezerAlbum:
    id: int
    title: str
    artist: DeezerArtist
    cover_url: str | None = None
    release_date: str | None = None
    track_count: int | None = None


@dataclass(frozen=True, slots=True)
class DeezerTrack:
    id: int
    title: str
    artist: DeezerArtist
    album_id: int
    album_title: str
    duration_seconds: int
    track_number: int | None = None
    disc_number: int | None = None
    explicit: bool = False
