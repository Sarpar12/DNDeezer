"""Exercise the host's mismatched candidate schemas with real msgspec IO."""

from types import SimpleNamespace

import msgspec
import pytest
from infrastructure.plugins.protocols import DownloadFileRef, PluginSearchResult

from dndeezer.deezer.models import DeezerArtist, DeezerTrack
from dndeezer.indexer import DeezerIndexer


class DownloadSearchResult(msgspec.Struct):
    # Required fields from the host's persisted ScoredCandidate.files schema.
    username: str
    filename: str
    parent_directory: str
    size: int
    extension: str


class StoredRelease(msgspec.Struct):
    files: list[DownloadFileRef]
    payload: str


class StoredCandidate(msgspec.Struct):
    files: list[DownloadSearchResult]
    plugin_release: StoredRelease


def round_trip(release):
    # The host puts the original plugin ref in both locations, encodes it,
    # then reads files as DownloadSearchResult and release.files as file refs.
    encoded = msgspec.json.encode([{
        "files": release.files, "plugin_release": release,
    }])
    decoded = msgspec.json.decode(encoded)
    return msgspec.convert(decoded, type=list[StoredCandidate], strict=False)[0]


def test_plain_file_ref_reproduces_host_persistence_failure():
    release = PluginSearchResult(
        title="Teenage Dream", payload="track:17135108",
        files=[DownloadFileRef(username="deezer", filename="Teenage Dream.flac", size=0)],
    )
    with pytest.raises(msgspec.ValidationError, match="parent_directory"):
        round_trip(release)


def test_plugin_track_candidate_survives_both_host_schemas():
    track = DeezerTrack(
        id=17135108, title="Teenage Dream", artist=DeezerArtist(144227, "Katy Perry"),
        album_id=1603030, album_title="Teenage Dream: The Complete Confection",
        duration_seconds=227,
    )
    release = DeezerIndexer(SimpleNamespace())._track_result(track, 1.0).plugin
    assert isinstance(release.files[0], DownloadFileRef)
    candidate = round_trip(release)
    assert candidate.files[0].parent_directory == release.title
    assert candidate.files[0].extension == "flac"
    assert candidate.files[0].filename == "Katy Perry - Teenage Dream.flac"
    assert candidate.plugin_release.payload == "track:17135108"
    assert candidate.plugin_release.files[0].filename == candidate.files[0].filename
