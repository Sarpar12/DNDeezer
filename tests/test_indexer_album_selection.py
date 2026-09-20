import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from dndeezer.deezer.models import DeezerAlbum, DeezerArtist
from dndeezer.indexer import DeezerIndexer


def album(identifier, artist="Britney Spears", count=16):
    return DeezerAlbum(identifier, "...Baby One More Time", DeezerArtist(1, artist),
                       track_count=count)


def indexer(albums):
    instance = DeezerIndexer(SimpleNamespace(
        settings={"arl": "test"}, logger=logging.getLogger("test.indexer"),
    ))
    instance._client = lambda: SimpleNamespace(search_albums=AsyncMock(return_value=albums))
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
