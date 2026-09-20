import pytest
from flac_fixture import SILENT_FLAC
from mutagen.flac import FLAC, FLACNoHeaderError

from dndeezer.deezer.metadata import write_flac_metadata
from dndeezer.deezer.models import DeezerAlbum, DeezerArtist


def test_flac_tags_round_trip_without_changing_audio(tmp_path):
    path = tmp_path / "0102 Circus.flac"
    path.write_bytes(SILENT_FLAC)
    album = DeezerAlbum(9, "Circus", DeezerArtist(1, "Britney Spears"),
                        release_date="2008-12-02")
    write_flac_metadata(path, {
        "id": 100, "title": "Circus", "artist": {"name": "Britney Spears"},
        "track_position": 2, "disk_number": 1, "isrc": "USXX10800002",
    }, album=album, position=99)
    audio = FLAC(path)
    assert audio["title"] == ["Circus"]
    assert audio["artist"] == ["Britney Spears"]
    assert audio["albumartist"] == ["Britney Spears"]
    assert audio["album"] == ["Circus"]
    assert audio["tracknumber"] == ["2"]
    assert audio["discnumber"] == ["1"]
    assert audio["date"] == ["2008-12-02"]
    assert audio["isrc"] == ["USXX10800002"]
    # Strip FLAC metadata blocks to compare the actual encoded audio frames.
    def frames(data):
        offset = 4
        while True:
            last = data[offset] & 128
            offset += 4 + int.from_bytes(data[offset + 1:offset + 4], "big")
            if last:
                return data[offset:]
    assert frames(path.read_bytes()) == frames(SILENT_FLAC)


def test_invalid_flac_is_not_silently_published(tmp_path):
    path = tmp_path / "bad.flac"
    path.write_bytes(b"not a FLAC")
    with pytest.raises(FLACNoHeaderError):
        write_flac_metadata(path, {"title": "Circus"})
