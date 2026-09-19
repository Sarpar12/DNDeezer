import time

import pytest

from dndeezer.deezer.client import DeezerApiError, DeezerClient


class FakeResponse:
    def __init__(self, data, status_code=200):
        self._data = data
        self.status_code = status_code

    def json(self):
        return self._data

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

class FakeHttp:
    def __init__(self):
        self.responses = []
        self.requested_urls = []

    def queue(self, data, status_code=200):
        self.responses.append(
            FakeResponse(data, status_code)
        )

    async def get(self, url, **kwargs):
        self.requested_urls.append(url)
        return self.responses.pop(0)

    async def post(self, url, **kwargs):
        self.requested_urls.append(url)
        return self.responses.pop(0)

@pytest.mark.asyncio
async def test_authenticate_valid_arl():
    http = FakeHttp()

    http.queue({
        "error": {},
        "results": {
            "checkForm": "csrf-token",
            "COUNTRY": "US",
            "USER": {
                "USER_ID": 12345
            },
        },
    })

    client = DeezerClient(http, "fake-arl", min_interval=0)
    session = await client.authenticate()

    assert session.user_id == 12345
    assert session.country == "US"
    assert session.api_token == "csrf-token"

@pytest.mark.asyncio
async def test_search_album():
    http = FakeHttp() 
    http.queue({
        "data": [{
            "id": "302127",
            "title": "Discovery",
            "cover_xl": "https://example.test/cover.jpg",
            "artist": {
                "id": 27,
                "name": "Daft Punk:",
            },
        }]
    })

    client = DeezerClient(http, "fake-arl", min_interval=0)
    albums = await client.search_albums("Daft Punk","Discovery")

    assert len(albums) == 1
    assert albums[0].id == 302127
    assert albums[0].title == "Discovery"
    assert albums[0].artist.name

@pytest.mark.asyncio
async def test_get_album_tracklist_follows_pagination():
    http = FakeHttp()

    # Page 1: album response pointing at the tracklist endpoint.
    http.queue({
        "id": 302127,
        "title": "Discovery",
        "nb_tracks": 3,
        "tracklist": "https://api.deezer.com/album/302127/tracks",
    })
    # Tracklist page 1: "next" links to page 2.
    http.queue({
        "data": [{"id": 1, "title": "One"}, {"id": 2, "title": "Two"}],
        "next": "https://api.deezer.com/album/302127/tracks?limit=1000&index=2",
    })
    # Tracklist page 2: no "next", pagination ends.
    http.queue({
        "data": [{"id": 3, "title": "Three"}],
    })

    client = DeezerClient(http, "fake-arl", min_interval=0)
    tracks = await client.get_album_tracklist(302127)

    assert [t["id"] for t in tracks] == [1, 2, 3]


@pytest.mark.asyncio
async def test_get_album_tracklist_requests_high_limit():
    http = FakeHttp()
    http.queue({"tracklist": "https://api.deezer.com/album/1/tracks"})
    http.queue({"data": []})

    client = DeezerClient(http, "fake-arl", min_interval=0)
    await client.get_album_tracklist(1)

    assert http.requested_urls == [
        "https://api.deezer.com/album/1",
        "https://api.deezer.com/album/1/tracks?limit=1000",
    ]


@pytest.mark.asyncio
async def test_get_album_tracklist_without_tracklist_field_raises():
    http = FakeHttp()
    http.queue({"id": 1, "title": "No Tracklist"})

    client = DeezerClient(http, "fake-arl", min_interval=0)

    with pytest.raises(DeezerApiError):
        await client.get_album_tracklist(1)


@pytest.mark.asyncio
async def test_get_album_tracklist_skips_non_dict_entries():
    http = FakeHttp()
    http.queue({"tracklist": "https://api.deezer.com/album/1/tracks"})
    http.queue({"data": [{"id": 1}, "junk", None]})

    client = DeezerClient(http, "fake-arl", min_interval=0)
    tracks = await client.get_album_tracklist(1)

    assert tracks == [{"id": 1}]
@pytest.mark.asyncio
async def test_get_retries_rate_limited_request():
    http = FakeHttp()
    http.queue({"error": "slow down"}, status_code=429)
    http.queue({"id": 1, "title": "Ok"})

    client = DeezerClient(http, "fake-arl", min_interval=0, retry_delay=0)
    data = await client._get("/track/1")

    assert data["title"] == "Ok"
    assert len(http.requested_urls) == 2


@pytest.mark.asyncio
async def test_get_gives_up_after_max_retries():
    http = FakeHttp()
    for _ in range(3):
        http.queue({"error": "unavailable"}, status_code=503)

    client = DeezerClient(
        http,
        "fake-arl",
        min_interval=0,
        max_retries=3,
        retry_delay=0,
    )

    with pytest.raises(DeezerApiError):
        await client._get("/track/1")

    assert len(http.requested_urls) == 3


@pytest.mark.asyncio
async def test_gw_call_retries_rate_limited_request():
    http = FakeHttp()
    http.queue({"error": "slow down"}, status_code=503)
    http.queue({"results": {"checkForm": "csrf", "USER": {"USER_ID": 1}}})

    client = DeezerClient(http, "fake-arl", min_interval=0, retry_delay=0)
    session = await client.authenticate()

    assert session.api_token == "csrf"


@pytest.mark.asyncio
async def test_min_interval_spaces_requests():
    http = FakeHttp()
    http.queue({"id": 1})
    http.queue({"id": 2})

    client = DeezerClient(http, "fake-arl", min_interval=0.05)

    start = time.monotonic()
    await client._get("/track/1")
    await client._get("/track/2")
    elapsed = time.monotonic() - start

    assert elapsed >= 0.04
