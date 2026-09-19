import pytest

from dndeezer.backend import DownloadTarget, parse_payload


def test_parse_album_payload():
    target = parse_payload("album:302127")

    assert target == DownloadTarget(kind="album", deezer_id=302127)


def test_parse_track_payload():
    target = parse_payload("track:3135555")

    assert target.kind == "track"
    assert target.deezer_id == 3135555


def test_target_round_trip():
    target = DownloadTarget(kind="album", deezer_id=302127)

    assert target.payload == "album:302127"
    assert parse_payload(target.payload) == target


@pytest.mark.parametrize(
    "payload", ["", "302127", "artist:27", "album:nope", "track:-1"],
)

def test_invalid_payload(payload):
    with pytest.raises(ValueError):
        parse_payload(payload)