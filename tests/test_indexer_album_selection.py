import asyncio
import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from dndeezer.deezer.models import DeezerAlbum, DeezerArtist, DeezerTrack
from dndeezer.indexer import DeezerIndexer


def album(identifier, artist="Britney Spears", count=16):
    return DeezerAlbum(identifier, "...Baby One More Time", DeezerArtist(1, artist),
                       track_count=count)


def indexer(albums):
    instance = DeezerIndexer(SimpleNamespace(
        settings={"arl": "test"}, logger=logging.getLogger("test.indexer"),
    ))
    async def tracklist(identifier):
        release = next(a for a in albums if a.id == identifier)
        return release, [DeezerTrack(
            identifier * 100, "Before the Goodbye", release.artist,
            identifier, release.title, 230,
        )]

    client = SimpleNamespace(
        search_albums=AsyncMock(return_value=albums),
        get_album_with_tracklist=AsyncMock(side_effect=tracklist),
    )
    instance._client = lambda: client
    return instance


@pytest.mark.asyncio
async def test_album_search_rejects_covers_and_singles_and_ranks_editions():
    instance = indexer([
        album(1, "The Marías", 1), album(2, count=1),
        album(3, count=12), album(4, count=None), album(5),
    ])
    results = await instance.search_album("Britney Spears", "...Baby One More Time", track_count=16)
    assert [r.plugin.payload for r in results] == ["album:5", "album:3", "album:4"]
    assert results[0].plugin.score > results[1].plugin.score > results[2].plugin.score


@pytest.mark.asyncio
async def test_single_requests_and_artist_punctuation_are_supported():
    instance = indexer([album(1, "BRITNEY-SPEARS", 1)])
    assert await instance.search_album("Britney Spears", "...Baby One More Time", track_count=1)
    assert await instance.search_album("Britney Spears", "...Baby One More Time")


@pytest.mark.asyncio
async def test_missing_track_search_returns_independently_downloadable_files():
    instance = indexer([album(71570, count=15)])
    results = await instance.search_album("Britney Spears", "Britney", track_count=1)
    assert len(results) == 1
    release = results[0].plugin
    assert release.payload == "track:7157000"
    assert len(release.files) == 1
    assert release.files[0].filename == "Britney Spears - Before the Goodbye.flac"
    assert release.files[0].size == 0


@pytest.mark.asyncio
async def test_tracklist_failure_does_not_fall_back_to_whole_album():
    instance = indexer([album(1), album(2)])
    client = instance._client()
    original = client.get_album_with_tracklist.side_effect

    async def tracklist(identifier):
        if identifier == 1:
            raise RuntimeError("unavailable")
        return await original(identifier)

    client.get_album_with_tracklist.side_effect = tracklist
    results = await instance.search_album("Britney Spears", "Britney", track_count=1)
    assert [r.plugin.payload for r in results] == ["track:200"]


@pytest.mark.asyncio
async def test_tracklist_timeout_does_not_fall_back_to_whole_album():
    instance = indexer([album(1)])

    async def wait_for_tracklist(identifier):
        await asyncio.Event().wait()

    instance._client().get_album_with_tracklist.side_effect = wait_for_tracklist
    assert await instance.search_album("Britney Spears", "Britney", track_count=1, timeout=0.01) == []
