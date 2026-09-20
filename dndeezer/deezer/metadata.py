"""Embed Deezer metadata in FLAC Vorbis comments before publishing a file."""

from pathlib import Path
from typing import Any

from mutagen.flac import FLAC

from .models import DeezerAlbum


def write_flac_metadata(
    path: Path, track: dict[str, Any], *, album: DeezerAlbum | None = None,
    position: int | None = None,
) -> None:
    audio = FLAC(path)
    album_data = track.get("album") or {}
    artist = (track.get("artist") or {}).get("name")
    contributors = [
        item["name"] for item in track.get("contributors", [])
        if item.get("name") and item.get("role") in (None, "Main")
    ]
    values = {
        "TITLE": track.get("title"),
        "ARTIST": list(dict.fromkeys(contributors)) or artist,
        "ALBUM": album.title if album else album_data.get("title"),
        "ALBUMARTIST": album.artist.name if album else (album_data.get("artist") or {}).get("name") or artist,
        "TRACKNUMBER": track.get("track_position") or position,
        "DISCNUMBER": track.get("disk_number"),
        "DATE": (album.release_date if album else None) or track.get("release_date"),
        "ISRC": track.get("isrc"),
        "DEEZER_TRACK_ID": track.get("id"),
        "DEEZER_ALBUM_ID": album.id if album else album_data.get("id"),
    }
    for key, value in values.items():
        if value is not None and value != "":
            audio[key] = value if isinstance(value, list) else [str(value)]
    audio.save()
