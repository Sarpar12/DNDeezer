"""End-to-end wiring tests: payload string through the real client, media
service, and backend, with only the HTTP transport faked."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager

import pytest

from dndeezer.backend import parse_payload
from dndeezer.backends.direct import build_direct_backend
from dndeezer.deezer.client import DeezerClient
from dndeezer.deezer.media import CIPHER


class FakeResponse:
    def __init__(self, data=None, status_code=200, pieces=None):
        self._data = data
        self.status_code = status_code
        self._pieces = pieces
        self.headers = {}

    def json(self):
        return self._data

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    async def aiter_raw(self, size=65536):
        for piece in self._pieces or []:
            yield piece


class FakeHttp:
    def __init__(self):
        self.get_queue: list[FakeResponse] = []
        self.post_queue: list[FakeResponse] = []
        self.calls: list[tuple[str, dict]] = []

    def queue_get(self, response):
        self.get_queue.append(response)

    def queue_post(self, response):
        self.post_queue.append(response)

    async def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return self.get_queue.pop(0)

    @asynccontextmanager
    async def stream(self, method, url, **kwargs):
        assert method == "GET"
        yield await self.get(url, **kwargs)

    async def post(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return self.post_queue.pop(0)


def _user_data():
    return FakeResponse({
        "results": {
            "checkForm": "csrf",
            "COUNTRY": "US",
            "USER": {"USER_ID": 1, "OPTIONS": {"license_token": "lic"}},
        },
    })


def _track_json(track_id):
    return FakeResponse({
        "id": track_id,
        "title": f"Track {track_id}",
        "artist": {"name": "Daft Punk"},
        "readable": True,
        "track_token": "public-token",
    })


def _page_track():
    return FakeResponse({"results": {"DATA": {"TRACK_TOKEN": "session-token"}}})


def _get_url_media(track_id):
    return FakeResponse({
        "data": [{
            "media": [{
                "format": "FLAC",
                "cipher": CIPHER,
                "sources": [{"url": f"https://cdn.test/{track_id}"}],
            }],
        }],
    })


def _queue_track(http, track_id):
    http.queue_get(_track_json(track_id))
    http.queue_post(_page_track())
    http.queue_post(_get_url_media(track_id))
    http.queue_get(FakeResponse(pieces=[]))


def _make_backend(http, tmp_path):
    return build_direct_backend(
        http=http,
        arl="test-arl",
        downloads_dir=tmp_path,
        client=DeezerClient(http, "test-arl", min_interval=0),
    )


async def _await_done(backend, job, timeout=5.0):
    deadline = asyncio.get_event_loop().time() + timeout
    while True:
        status = await backend.get_status(job)
        if status.state in ("completed", "failed", "cancelled"):
            return status
        if asyncio.get_event_loop().time() > deadline:
            pytest.fail(f"job did not finish: {status.state}")
        await asyncio.sleep(0.01)


def _auth_calls(http):
    return [
        kwargs
        for _, kwargs in http.calls
        if (kwargs.get("params") or {}).get("method") == "deezer.getUserData"
    ]


@pytest.mark.asyncio
async def test_track_payload_end_to_end(tmp_path):
    http = FakeHttp()
    http.queue_post(_user_data())
    _queue_track(http, 100)

    backend = _make_backend(http, tmp_path)

    job = await backend.enqueue(
        task_id="task-1",
        target=parse_payload("track:100"),
    )

    status = await _await_done(backend, job)
    assert status.state == "completed", status.error
    assert status.progress_percent == 100

    files = await backend.list_completed_files(job)
    assert [f.name for f in files] == ["Daft Punk - Track 100.flac"]
    assert all(f.is_relative_to(tmp_path) for f in files)

    path = await backend.get_file_path(job, "Daft Punk - Track 100.flac")
    assert path == files[0]


@pytest.mark.asyncio
async def test_album_payload_end_to_end(tmp_path):
    http = FakeHttp()
    http.queue_post(_user_data())
    http.queue_get(FakeResponse({
        "id": 9,
        "title": "Discovery",
        "artist": {"id": 27, "name": "Daft Punk"},
        "tracklist": "https://api.deezer.com/album/9/tracks",
    }))
    http.queue_get(FakeResponse({"data": [
        {"id": 101, "title": "One"},
        {"id": 102, "title": "Two"},
    ]}))
    _queue_track(http, 101)
    _queue_track(http, 102)

    backend = _make_backend(http, tmp_path)

    job = await backend.enqueue(
        task_id="task-2",
        target=parse_payload("album:9"),
    )

    status = await _await_done(backend, job)
    assert status.state == "completed", status.error

    files = await backend.list_completed_files(job)
    assert [f.name for f in files] == ["01 - One.flac", "02 - Two.flac"]

    album_dir = tmp_path / job.backend_id / "Daft Punk - Discovery"
    assert all(f.parent == album_dir for f in files)

    # One getUserData for the whole album; pageTrack runs per track.
    assert len(_auth_calls(http)) == 1


@pytest.mark.asyncio
async def test_invalid_payload_never_reaches_backend(tmp_path):
    with pytest.raises(ValueError):
        parse_payload("playlist:9")
