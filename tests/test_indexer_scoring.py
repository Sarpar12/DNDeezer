import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from dndeezer.deezer.models import DeezerAlbum, DeezerArtist, DeezerTrack
from dndeezer.indexer import DeezerIndexer, _duration_score, _title_score


def test_title_score_exact_and_punctuation():
    assert _title_score("...Baby One More Time", "...Baby One More Time") == 1.0
    assert _title_score("Britney Spears", "BRITNEY-SPEARS") == 1.0


def test_title_score_parenthesized_transliteration_alias():
    # MB release group titles often carry "한글 (Latin)" like Deezer's do.
    assert _title_score("ChoOM", "춤 (CHOOM)") >= 0.95
    assert _title_score("춤 (CHOOM)", "ChoOM") >= 0.95


def test_title_score_word_reorder_and_unrelated():
    assert _title_score("Display of Affection", "Affection, Display of") >= 0.98
    assert _title_score("Teenage Dream", "The Teenage Dream Collection") < 0.9
    assert _title_score("CHOOM", "MOON") < 0.75


def test_duration_score_bands():
    assert _duration_score(230, 230) == 1.0
    assert _duration_score(230, 238) == 0.7
    assert _duration_score(230, 290) == 0.0


def _indexer(albums=(), tracks=()):
    instance = DeezerIndexer(SimpleNamespace(
        settings={"arl": "test"}, logger=logging.getLogger("test.indexer"),
    ))
    instance._client = lambda: SimpleNamespace(
        search_albums=AsyncMock(return_value=list(albums)),
        search_tracks=AsyncMock(return_value=list(tracks)),
    )
    return instance


def _album(identifier, title, artist="BABYMONSTER", year=None, count=4):
    return DeezerAlbum(identifier, title, DeezerArtist(1, artist),
                       release_date=year, track_count=count)


def _track(identifier, title, duration=178, artist="BABYMONSTER", album="춤 (CHOOM)"):
    return DeezerTrack(identifier, title, DeezerArtist(1, artist),
                       album_id=970200091, album_title=album,
                       duration_seconds=duration)


@pytest.mark.asyncio
async def test_album_search_prefers_requested_release_year():
    instance = _indexer(albums=[
        _album(2, "Bad", artist="Michael Jackson", year="2002"),  # compilation
        _album(1, "Bad", artist="Michael Jackson", year="1987"),
    ])
    results = await instance.search_album("Michael Jackson", "Bad", year=1987)
    assert [r.plugin.payload for r in results] == ["album:1", "album:2"]
    assert results[0].plugin.score > results[1].plugin.score


@pytest.mark.asyncio
async def test_track_search_uses_duration_to_discriminate_same_title():
    instance = _indexer(tracks=[
        _track(11, "Choice", duration=402),   # same title, wrong recording
        _track(12, "Choice", duration=179),
    ])
    results = await instance.search_track(
        "BABYMONSTER", "Choice", album_title="춤 (CHOOM)", duration_seconds=178,
    )
    assert [r.plugin.payload for r in results] == ["track:12", "track:11"]


@pytest.mark.asyncio
async def test_track_search_penalizes_version_mismatch():
    instance = _indexer(tracks=[
        _track(21, "Teenage Dream (Vandalism V8 Vocal Remix)", artist="Katy Perry"),
        _track(22, "Teenage Dream", artist="Katy Perry"),
    ])
    results = await instance.search_track("Katy Perry", "Teenage Dream")
    assert [r.plugin.payload for r in results] == ["track:22", "track:21"]
    assert results[0].plugin.score > 3 * results[1].plugin.score


@pytest.mark.asyncio
async def test_track_search_boosts_same_album():
    instance = _indexer(tracks=[
        _track(31, "CHOOM", album="Gold Collection 2026"),
        _track(32, "CHOOM", album="춤 (CHOOM)"),
    ])
    results = await instance.search_track(
        "BABYMONSTER", "CHOOM", album_title="춤 (CHOOM)", duration_seconds=178,
    )
    assert [r.plugin.payload for r in results] == ["track:32", "track:31"]
