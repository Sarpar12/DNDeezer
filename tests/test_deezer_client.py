import pytest

from dndeezer.deezer.client import DeezerClient


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

    def queue(self, data, status_code=200):
        self.responses.append(
            FakeResponse(data, status_code)
        )

    async def get(self, url, **kwargs):
        return self.responses.pop(0)

    async def post(self, url, **kwargs):
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

    client = DeezerClient(http, "fake-arl")
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

    client = DeezerClient(http, "fake-arl")
    albums = await client.search_albums("Daft Punk","Discovery")

    assert len(albums) == 1
    assert albums[0].id == 302127
    assert albums[0].title == "Discovery"
    assert albums[0].artist.name