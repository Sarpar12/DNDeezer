"""Tests for the decryption and media-selection helpers in dndeezer.deezer.media.

Blowfish key vectors follow the well-known Deezer scheme (MD5 hex of the
track id, XOR-folded with the shared secret), matching Octo-Fiesta's
DeezerDecryptedStream.GetBlowfishKey.
"""

import asyncio
import threading
from contextlib import asynccontextmanager

import httpx
import pytest
from cryptography.hazmat.decrepit.ciphers import algorithms as decrepit_algorithms
from cryptography.hazmat.primitives.ciphers import Cipher, modes

from dndeezer.deezer.client import DeezerClient
from dndeezer.deezer.media import (
    CHUNK_SIZE,
    CIPHER,
    DirectDeezerMediaService,
    MediaAcquisitionError,
    _blowfish_key,
    _content_length,
    _decrypted_chunks,
    _filename,
    _select_media_source,
    _track_id,
    _wrap_album_progress,
)
from dndeezer.deezer.models import DeezerAlbum, DeezerArtist, DeezerSession

BF_SECRET = b"g4el58wc0zvf9na1"
BF_IV = bytes(range(8))


# --- _blowfish_key -----------------------------------------------------------

def test_blowfish_key_known_vectors():
    # Hardcoded known-answer vectors, verified against Octo-Fiesta's
    # GetBlowfishKey (MD5 digest hex, digest[i] ^ digest[i+16] ^ secret[i]).
    assert _blowfish_key(3135555).hex() == "676b656c36697367317d233b306f6034"
    assert _blowfish_key(12345678).hex() == "3335376d636e25353f7824643d693034"


def test_blowfish_key_is_sixteen_bytes():
    assert len(_blowfish_key(1)) == 16


@pytest.fixture(autouse=True)
def isolate_tagging_from_synthetic_cipher_tests(monkeypatch):
    # These tests intentionally stream arbitrary bytes, not valid FLAC audio.
    # Real tagging is covered by test_metadata and the integration pipeline.
    monkeypatch.setattr("dndeezer.deezer.media.write_flac_metadata", lambda *a, **kw: None)


def test_blowfish_key_differs_per_track():
    assert _blowfish_key(3135555) != _blowfish_key(3135556)


# --- _decrypted_chunks -------------------------------------------------------

def _encrypt_plaintext(plaintext: bytes, track_id: int) -> bytes:
    """Produce a Deezer-style encrypted stream for the given plaintext."""
    cipher = Cipher(
        decrepit_algorithms.Blowfish(_blowfish_key(track_id)),
        modes.CBC(BF_IV),
    )

    out = bytearray()
    index = 0
    offset = 0
    while len(plaintext) - offset >= CHUNK_SIZE:
        chunk = plaintext[offset:offset + CHUNK_SIZE]
        if index % 3 == 0:
            encryptor = cipher.encryptor()
            chunk = encryptor.update(chunk) + encryptor.finalize()
        out += chunk
        offset += CHUNK_SIZE
        index += 1

    # Trailing partial chunk is stored unencrypted.
    out += plaintext[offset:]
    return bytes(out)


def test_stripes_reset_iv_across_network_boundaries():
    from dndeezer.deezer.media import _StripeDecoder

    # Identical independently encrypted stripes must decode identically,
    # regardless of the preceding stripe or HTTP chunk boundaries.
    plaintext = bytes(range(256)) * 8
    encryptor = Cipher(
        decrepit_algorithms.Blowfish(_blowfish_key(3135555)), modes.CBC(BF_IV),
    ).encryptor()
    encrypted = encryptor.update(plaintext) + encryptor.finalize()
    wire = encrypted + plaintext * 2 + encrypted + b"partial tail"
    expected = plaintext * 4 + b"partial tail"
    for size in (1, 997, 2048, 65536):
        decoder = _StripeDecoder(3135555)
        result = b"".join(decoder.update(wire[i:i + size]) for i in range(0, len(wire), size))
        assert result + decoder.finish() == expected


class FakeStreamedResponse:
    def __init__(self, pieces):
        self._pieces = list(pieces)

    async def aiter_raw(self, size=65536):
        for piece in self._pieces:
            yield piece


class FakeBufferedResponse:
    def __init__(self, content):
        self.content = content


async def _collect(response, track_id):
    return b"".join(
        [chunk async for chunk in _decrypted_chunks(response, track_id)]
    )


@pytest.mark.asyncio
async def test_decrypted_chunks_round_trip_single_piece():
    plaintext = bytes(range(256)) * 65  # 16640 bytes = 8 full chunks + 256 tail
    encrypted = _encrypt_plaintext(plaintext, 3135555)

    result = await _collect(
        FakeStreamedResponse([encrypted]),
        3135555,
    )

    assert result == plaintext


@pytest.mark.asyncio
async def test_decrypted_chunks_round_trip_misaligned_pieces():
    # Network reads rarely land on 2048-byte boundaries; the internal
    # buffer must reassemble chunks correctly.
    plaintext = bytes((i * 7) % 256 for i in range(CHUNK_SIZE * 5 + 17))
    encrypted = _encrypt_plaintext(plaintext, 3135555)

    pieces = [
        encrypted[offset:offset + 1000]
        for offset in range(0, len(encrypted), 1000)
    ]

    result = await _collect(FakeStreamedResponse(pieces), 3135555)

    assert result == plaintext


@pytest.mark.asyncio
async def test_decrypted_chunks_uses_key_for_the_id_actually_used():
    # A fallback/alternative track id must decrypt with its own key.
    plaintext = b"\x11" * CHUNK_SIZE
    encrypted = _encrypt_plaintext(plaintext, 999)

    assert await _collect(FakeStreamedResponse([encrypted]), 999) == plaintext
    assert await _collect(FakeStreamedResponse([encrypted]), 1000) != plaintext


@pytest.mark.asyncio
async def test_decrypted_chunks_partial_tail_needs_no_multiple_of_block():
    # Trailing bytes that do not complete a 2048-byte chunk pass through raw.
    plaintext = b"abc"
    encrypted = _encrypt_plaintext(plaintext, 12345678)

    assert await _collect(FakeStreamedResponse([encrypted]), 12345678) == plaintext


@pytest.mark.asyncio
async def test_decrypted_chunks_empty_stream():
    assert await _collect(FakeStreamedResponse([]), 1) == b""


@pytest.mark.asyncio
async def test_decrypted_chunks_falls_back_to_content_attribute():
    plaintext = bytes(200)
    encrypted = _encrypt_plaintext(plaintext, 42)

    result = await _collect(FakeBufferedResponse(encrypted), 42)

    assert result == plaintext


# --- _select_media_source ----------------------------------------------------

def _media_payload(entries):
    return {"data": [{"media": entries}]}


def _entry(fmt, url=None, cipher=CIPHER):
    return {
        "format": fmt,
        "cipher": cipher,
        "sources": [{"url": url or f"https://cdn.test/{fmt}"}] if url != "__none__" else [],
    }


def test_select_media_source_prefers_flac():
    payload = _media_payload([
        _entry("MP3_128"),
        _entry("MP3_320"),
        _entry("FLAC"),
    ])

    url, quality = _select_media_source(payload)

    assert quality == "FLAC"
    assert url == "https://cdn.test/FLAC"


def test_select_media_source_falls_back_to_mp3_320():
    payload = _media_payload([_entry("MP3_320"), _entry("MP3_128")])

    _url, quality = _select_media_source(payload)

    assert quality == "MP3_320"


def test_select_media_source_falls_back_to_mp3_128():
    payload = _media_payload([_entry("MP3_128")])

    _url, quality = _select_media_source(payload)

    assert quality == "MP3_128"


def test_select_media_source_skips_entries_without_sources():
    payload = _media_payload([
        _entry("FLAC", "__none__"),
        _entry("MP3_320"),
    ])

    _url, quality = _select_media_source(payload)

    assert quality == "MP3_320"


def test_select_media_source_skips_entries_without_format():
    payload = _media_payload([
        {"sources": [{"url": "https://cdn.test/anon"}]},
        _entry("MP3_128"),
    ])

    _url, quality = _select_media_source(payload)

    assert quality == "MP3_128"


def test_select_media_source_uses_any_format_when_none_match_priority():
    # Current documented behaviour: an unexpected but present format is
    # still preferred over failing outright.
    payload = _media_payload([_entry("MP3_1")])

    _url, quality = _select_media_source(payload)

    assert quality == "MP3_1"


def test_select_media_source_skips_unencrypted_offers():
    # Only BF_CBC_STRIPE media may be run through the decryptor; a NONE
    # cipher offer must not win over an encrypted one.
    payload = _media_payload([
        _entry("FLAC", cipher="NONE"),
        _entry("MP3_320"),
    ])

    url, quality = _select_media_source(payload)

    assert quality == "MP3_320"
    assert url == "https://cdn.test/MP3_320"


def test_select_media_source_raises_when_only_unencrypted_offers_exist():
    payload = _media_payload([
        _entry("FLAC", cipher="NONE"),
        _entry("MP3_128"),
    ])
    payload["data"][0]["media"][1].pop("cipher")

    with pytest.raises(MediaAcquisitionError):
        _select_media_source(payload)


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"data": []},
        {"data": [{}]},
        {"data": [{"media": []}]},
        {"data": [{"media": [_entry("FLAC", "__none__")]}]},
        None,
        "not-a-dict",
    ],
    ids=[
        "empty",
        "no-data-items",
        "no-media-key",
        "empty-media",
        "all-sources-empty",
        "none",
        "string",
    ],
)
def test_select_media_source_raises_without_usable_media(payload):
    with pytest.raises(MediaAcquisitionError):
        _select_media_source(payload)


# --- _track_id ---------------------------------------------------------------

def test_track_id_accepts_int_and_numeric_string():
    assert _track_id({"id": 3135555}) == 3135555
    assert _track_id({"id": "3135555"}) == 3135555


@pytest.mark.parametrize(
    "raw_id",
    [None, 0, -5, "", "abc"],
)
def test_track_id_rejects_invalid_values(raw_id):
    with pytest.raises(MediaAcquisitionError):
        _track_id({"id": raw_id})


def test_track_id_rejects_missing_id():
    with pytest.raises(MediaAcquisitionError):
        _track_id({})


# --- _filename ---------------------------------------------------------------

def test_filename_uses_flac_extension_for_flac():
    track = {"title": "Harder", "artist": {"name": "Daft Punk"}}

    assert _filename(track, "FLAC") == "Daft Punk - Harder.flac"


@pytest.mark.parametrize("quality", ["MP3_320", "MP3_128", "MP3_1"])
def test_filename_uses_mp3_extension_for_lossy_quality(quality):
    track = {"title": "Harder", "artist": {"name": "Daft Punk"}}

    assert _filename(track, quality) == "Daft Punk - Harder.mp3"


def test_filename_sanitizes_path_unsafe_characters():
    track = {"title": "Back/In Black", "artist": {"name": "AC/DC"}}

    name = _filename(track, "MP3_320")

    assert "/" not in name
    assert name == "AC_DC - Back_In Black.mp3"


def test_filename_without_artist():
    track = {"title": "Anchor", "artist": {}}

    assert _filename(track, "FLAC") == "Anchor.flac"


def test_filename_falls_back_to_track_stem():
    assert _filename({}, "MP3_128") == "track.mp3"


# --- _content_length ---------------------------------------------------------

class FakeHeadersResponse:
    def __init__(self, headers):
        self.headers = headers


def test_content_length_parses_header():
    response = FakeHeadersResponse({"content-length": "4096"})

    assert _content_length(response) == 4096


def test_content_length_missing_header():
    assert _content_length(FakeHeadersResponse({})) is None


def test_content_length_non_numeric_header():
    assert _content_length(FakeHeadersResponse({"content-length": "abc"})) is None


def test_content_length_response_without_headers():
    assert _content_length(object()) is None


# --- DirectDeezerMediaService.get_url request and CDN fetch ------------------

class FakeResponse:
    def __init__(self, data=None, status_code=200, pieces=None, headers=None):
        self._data = data
        self.status_code = status_code
        self._pieces = pieces
        self.headers = headers or {}

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
        self.get_calls = []
        self.post_calls = []
        self._get_queue = []
        self._post_queue = []

    def queue_get(self, response):
        self._get_queue.append(response)

    def queue_post(self, response):
        self._post_queue.append(response)

    async def get(self, url, **kwargs):
        self.get_calls.append((url, kwargs))
        return self._get_queue.pop(0)

    @asynccontextmanager
    async def stream(self, method, url, **kwargs):
        assert method == "GET"
        yield await self.get(url, **kwargs)

    async def post(self, url, **kwargs):
        self.post_calls.append((url, kwargs))
        return self._post_queue.pop(0)


def _make_service():
    http = FakeHttp()
    client = DeezerClient(http, "test-arl", min_interval=0)
    return DirectDeezerMediaService(client), http


def _user_data_response():
    return FakeResponse({
        "results": {
            "checkForm": "csrf",
            "COUNTRY": "US",
            "USER": {
                "USER_ID": 1,
                "OPTIONS": {"license_token": "lic-1"},
            },
        },
    })


def _page_track_response(track_token="session-token"):
    return FakeResponse({
        "results": {"DATA": {"TRACK_TOKEN": track_token}},
    })


def _media_api_response(url="https://cdn.test/flac"):
    return FakeResponse({
        "data": [{
            "media": [
                {
                    "format": "FLAC",
                    "cipher": CIPHER,
                    "sources": [{"url": url}],
                }
            ]
        }],
    })


def _track_response(track_id=100, readable=True):
    data = {
        "id": track_id,
        "title": "Harder",
        "artist": {"name": "Daft Punk"},
        "readable": readable,
    }
    if readable:
        data["track_token"] = "public-token"
    return data


@pytest.mark.asyncio
async def test_get_url_payload_uses_cipher_format_objects(tmp_path):
    service, http = _make_service()

    http.queue_post(_user_data_response())
    http.queue_get(FakeResponse(_track_response()))
    http.queue_post(_page_track_response())
    http.queue_post(_media_api_response())
    http.queue_get(FakeResponse(pieces=[]))

    await service._acquire_track(
        100,
        tmp_path,
    )

    media_url, kwargs = http.post_calls[-1]

    assert media_url == "https://media.deezer.com/v1/get_url"
    assert kwargs["json"]["media"] == [{
        "type": "FULL",
        "formats": [
            {"cipher": "BF_CBC_STRIPE", "format": "FLAC"},
            {"cipher": "BF_CBC_STRIPE", "format": "MP3_320"},
            {"cipher": "BF_CBC_STRIPE", "format": "MP3_128"},
        ],
    }]
    assert kwargs["json"]["license_token"] == "lic-1"
    assert kwargs["json"]["track_tokens"] == ["session-token"]


@pytest.mark.asyncio
async def test_cdn_get_sends_user_agent(tmp_path):
    service, http = _make_service()

    http.queue_post(_user_data_response())
    http.queue_get(FakeResponse(_track_response()))
    http.queue_post(_page_track_response())
    http.queue_post(_media_api_response())
    http.queue_get(FakeResponse(pieces=[]))

    await service._acquire_track(100, tmp_path)

    cdn_url, kwargs = http.get_calls[-1]

    assert cdn_url == "https://cdn.test/flac"
    assert kwargs["headers"] == {"User-Agent": "Mozilla/5.0"}


@pytest.mark.asyncio
async def test_acquire_track_with_session_skips_authenticate(tmp_path):
    service, http = _make_service()
    session = DeezerSession(
        user_id=1,
        country="US",
        api_token="csrf-shared",
        license_token="lic-shared",
    )

    # No getUserData queued: if the code tried to authenticate, the
    # response queue would run dry and the test would fail.
    http.queue_get(FakeResponse(_track_response()))
    http.queue_post(_page_track_response())
    http.queue_post(_media_api_response())
    http.queue_get(FakeResponse(pieces=[]))

    await service._acquire_track(100, tmp_path, session=session)

    auth_calls = [
        (url, kwargs)
        for url, kwargs in http.post_calls
        if (kwargs.get("params") or {}).get("method") == "deezer.getUserData"
    ]
    assert auth_calls == []

    media_url, kwargs = http.post_calls[-1]
    assert media_url == "https://media.deezer.com/v1/get_url"
    assert kwargs["json"]["license_token"] == "lic-shared"


@pytest.mark.asyncio
async def test_acquire_track_without_session_authenticates_once(tmp_path):
    service, http = _make_service()

    http.queue_post(_user_data_response())
    http.queue_get(FakeResponse(_track_response()))
    http.queue_post(_page_track_response())
    http.queue_post(_media_api_response())
    http.queue_get(FakeResponse(pieces=[]))

    await service._acquire_track(100, tmp_path)

    auth_calls = [
        kwargs
        for _, kwargs in http.post_calls
        if (kwargs.get("params") or {}).get("method") == "deezer.getUserData"
    ]
    assert len(auth_calls) == 1


@pytest.mark.asyncio
async def test_acquire_track_decrypts_stream_to_output(tmp_path):
    service, http = _make_service()

    plaintext = bytes((i * 3) % 256 for i in range(CHUNK_SIZE * 4 + 11))
    encrypted = _encrypt_plaintext(plaintext, 100)

    http.queue_post(_user_data_response())
    http.queue_get(FakeResponse(_track_response()))
    http.queue_post(_page_track_response())
    http.queue_post(_media_api_response())
    http.queue_get(FakeResponse(pieces=[encrypted]))

    files = await service._acquire_track(100, tmp_path)

    assert [f.name for f in files] == ["Daft Punk - Harder.flac"]
    assert files[0].read_bytes() == plaintext
    assert not list(tmp_path.glob(".*.part"))


@pytest.mark.asyncio
@pytest.mark.parametrize("fail_stream", [False, True])
async def test_acquire_track_real_httpx_stream(tmp_path, fail_stream):
    plaintext = bytes(range(256)) * 600 + b"tail"
    encrypted = _encrypt_plaintext(plaintext, 100)
    progress = []

    class MediaStream(httpx.AsyncByteStream):
        closed = False

        async def __aiter__(self):
            yield encrypted[:65536]
            # Processing starts before the whole response has been received.
            assert progress
            if fail_stream:
                raise httpx.ReadError("connection lost")
            for offset in range(65536, len(encrypted), 997):
                yield encrypted[offset:offset + 997]

        async def aclose(self):
            self.closed = True

    stream = MediaStream()

    def handler(request):
        if request.url.host == "cdn.test":
            return httpx.Response(
                200, stream=stream,
                headers={"content-length": str(len(encrypted))},
            )
        if request.url.host == "api.deezer.com":
            data = _track_response()
        elif request.url.host == "media.deezer.com":
            data = _media_api_response().json()
        else:
            data = _page_track_response().json()
        return httpx.Response(200, json=data)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        service = DirectDeezerMediaService(DeezerClient(http, "test-arl", min_interval=0))
        session = DeezerSession(
            user_id=1, country="US", api_token="csrf", license_token="lic",
        )
        if fail_stream:
            with pytest.raises(MediaAcquisitionError, match="connection lost"):
                await service._acquire_track(
                    100, tmp_path, session=session, on_progress=progress.append,
                )
            assert not list(tmp_path.iterdir())
        else:
            files = await service._acquire_track(
                100, tmp_path, session=session, on_progress=progress.append,
            )
            assert files[0].read_bytes() == plaintext
            assert progress == sorted(progress)
            assert progress[-1] == 100
        assert stream.closed
        assert not list(tmp_path.glob(".*.part"))


@pytest.mark.asyncio
async def test_cancel_waits_for_worker_before_cleanup(tmp_path, monkeypatch):
    from dndeezer.deezer.media import _StripeDecoder

    service, http = _make_service()
    http.queue_post(_user_data_response())
    http.queue_get(FakeResponse(_track_response()))
    http.queue_post(_page_track_response())
    http.queue_post(_media_api_response())
    http.queue_get(FakeResponse(pieces=[b"x" * 65536]))

    loop = asyncio.get_running_loop()
    started = asyncio.Event()
    release = threading.Event()
    original = _StripeDecoder.update
    loop_thread = threading.get_ident()

    def slow_update(self, piece):
        assert threading.get_ident() != loop_thread
        loop.call_soon_threadsafe(started.set)
        assert release.wait(5), "test did not release worker"
        return original(self, piece)

    monkeypatch.setattr(_StripeDecoder, "update", slow_update)
    task = asyncio.create_task(service._acquire_track(100, tmp_path))
    try:
        await asyncio.wait_for(started.wait(), timeout=2)
        # The event loop remains responsive while a decrypt/write batch stalls.
        task.cancel()
        await asyncio.sleep(0)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
        assert (tmp_path / ".100.part").exists()
    finally:
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task

    assert not list(tmp_path.iterdir())


# --- DirectDeezerMediaService._find_alternative -------------------------------

def _alt_track(track_id=7, title="Song", artist="Someone"):
    return {
        "id": track_id,
        "title": title,
        "artist": {"name": artist},
        "readable": False,
    }


@pytest.mark.asyncio
async def test_find_alternative_uses_fallback_id():
    service, _ = _make_service()

    result = await service._find_alternative(
        _alt_track(7),
        {"FALLBACK": {"SNG_ID": "8"}},
    )

    assert result == 8


@pytest.mark.asyncio
async def test_find_alternative_ignores_zero_fallback_id():
    # Deezer sends SNG_ID "0" when there is no fallback; it must not abort
    # the remaining strategies.
    service, http = _make_service()
    http.queue_get(FakeResponse({"data": []}))

    result = await service._find_alternative(
        _alt_track(7),
        {"FALLBACK": {"SNG_ID": "0"}},
    )

    assert result is None


@pytest.mark.asyncio
async def test_find_alternative_ignores_fallback_equal_to_original():
    service, http = _make_service()
    http.queue_get(FakeResponse({"data": []}))

    result = await service._find_alternative(
        _alt_track(7),
        {"FALLBACK": {"SNG_ID": "7"}},
    )

    assert result is None


@pytest.mark.asyncio
async def test_find_alternative_isrc_uses_dedicated_endpoint():
    service, http = _make_service()
    http.queue_get(FakeResponse({"id": 55, "readable": True}))

    result = await service._find_alternative(
        _alt_track(7),
        {"ISRC": "FRAB90000001"},
    )

    assert result == 55
    assert http.get_calls[0][0] == "https://api.deezer.com/track/isrc:FRAB90000001"


@pytest.mark.asyncio
async def test_find_alternative_skips_unreadable_isrc_match():
    service, http = _make_service()
    http.queue_get(FakeResponse({"id": 55, "readable": False}))
    http.queue_get(FakeResponse({"data": []}))

    result = await service._find_alternative(
        _alt_track(7),
        {"ISRC": "FRAB90000001"},
    )

    assert result is None


@pytest.mark.asyncio
async def test_find_alternative_skips_isrc_match_equal_to_original():
    service, http = _make_service()
    http.queue_get(FakeResponse({"id": 7, "readable": True}))
    http.queue_get(FakeResponse({"data": []}))

    result = await service._find_alternative(
        _alt_track(7),
        {"ISRC": "FRAB90000001"},
    )

    assert result is None


@pytest.mark.asyncio
async def test_find_alternative_falls_back_to_artist_title_search():
    service, http = _make_service()
    http.queue_get(FakeResponse({"data": [{"id": 42}]}))

    result = await service._find_alternative(_alt_track(7), {})

    assert result == 42
    assert http.get_calls[0][0] == "https://api.deezer.com/search/track"
    assert http.get_calls[0][1]["params"] == {"q": "Someone Song", "limit": 1}


@pytest.mark.asyncio
async def test_find_alternative_returns_none_when_no_strategy_matches():
    service, http = _make_service()
    http.queue_get(FakeResponse({"data": []}))

    result = await service._find_alternative(_alt_track(7), {})

    assert result is None


# --- album acquisition ---------------------------------------------------------

def _album_response():
    return {
        "id": 9,
        "title": "Discovery",
        "nb_tracks": 3,
        "artist": {"id": 27, "name": "Daft Punk"},
        "tracklist": "https://api.deezer.com/album/9/tracks",
    }


def _tracklist_items(count):
    return [
        {"id": 100 + number, "title": f"Track {number}"}
        for number in range(1, count + 1)
    ]


def _queue_track(http, track_id):
    http.queue_get(FakeResponse(_track_response(track_id)))
    http.queue_post(_page_track_response())
    http.queue_post(_media_api_response(url=f"https://cdn.test/{track_id}"))
    http.queue_get(FakeResponse(pieces=[]))


@pytest.mark.asyncio
async def test_acquire_album_end_to_end(tmp_path):
    service, http = _make_service()

    http.queue_post(_user_data_response())
    http.queue_get(FakeResponse(_album_response()))
    http.queue_get(FakeResponse({"data": _tracklist_items(3)}))
    for track_id in (101, 102, 103):
        _queue_track(http, track_id)

    progress: list[float] = []
    files = await service._acquire_album(9, tmp_path, on_progress=progress.append)

    assert [f.name for f in files] == [
        "01 - Track 1.flac",
        "02 - Track 2.flac",
        "03 - Track 3.flac",
    ]
    album_dir = tmp_path / "Daft Punk - Discovery"
    assert all(f.parent == album_dir and f.is_file() for f in files)

    # Authentication happens once for the whole album.
    auth_calls = [
        kwargs
        for _, kwargs in http.post_calls
        if (kwargs.get("params") or {}).get("method") == "deezer.getUserData"
    ]
    assert len(auth_calls) == 1

    # Progress is monotonic and ends at 100.
    assert progress == sorted(progress)
    assert progress[-1] == 100.0


@pytest.mark.asyncio
async def test_acquire_album_continues_after_track_failure(tmp_path):
    service, http = _make_service()

    http.queue_post(_user_data_response())
    http.queue_get(FakeResponse(_album_response()))
    http.queue_get(FakeResponse({"data": _tracklist_items(2)}))

    _queue_track(http, 101)

    # Track 2's get_url call fails.
    http.queue_get(FakeResponse(_track_response(102)))
    http.queue_post(_page_track_response())
    http.queue_post(FakeResponse({"error": "boom"}, status_code=500))

    files = await service._acquire_album(9, tmp_path)

    assert [f.name for f in files] == ["01 - Track 1.flac"]


@pytest.mark.asyncio
async def test_acquire_album_raises_when_every_track_fails(tmp_path):
    service, http = _make_service()

    http.queue_post(_user_data_response())
    http.queue_get(FakeResponse(_album_response()))
    http.queue_get(FakeResponse({"data": _tracklist_items(1)}))

    http.queue_get(FakeResponse(_track_response(101)))
    http.queue_post(_page_track_response())
    http.queue_post(FakeResponse({"error": "boom"}, status_code=500))

    with pytest.raises(MediaAcquisitionError, match="produced no files"):
        await service._acquire_album(9, tmp_path)

    # Only an empty (or absent) album directory may be left behind.
    album_dir = tmp_path / "Daft Punk - Discovery"
    assert not any(album_dir.iterdir()) if album_dir.exists() else True


@pytest.mark.asyncio
async def test_acquire_album_empty_tracklist_raises(tmp_path):
    service, http = _make_service()

    http.queue_post(_user_data_response())
    http.queue_get(FakeResponse(_album_response()))
    http.queue_get(FakeResponse({"data": []}))

    with pytest.raises(MediaAcquisitionError, match="no tracks"):
        await service._acquire_album(9, tmp_path)


def _make_album():
    return DeezerAlbum(
        id=9,
        title="Discovery",
        artist=DeezerArtist(id=27, name="Daft Punk"),
    )


def test_album_directory_creates_sanitized_folder(tmp_path):
    service, _ = _make_service()

    album_dir = service._album_directory(tmp_path, _make_album())

    assert album_dir == tmp_path / "Daft Punk - Discovery"
    assert album_dir.is_dir()


def test_album_directory_sanitizes_unsafe_names(tmp_path):
    service, _ = _make_service()
    album = DeezerAlbum(
        id=9,
        title="Greatest / Hits?",
        artist=DeezerArtist(id=27, name="Daft Punk"),
    )

    album_dir = service._album_directory(tmp_path, album)

    assert "/" not in album_dir.name
    assert album_dir.name == "Daft Punk - Greatest _ Hits_"


def test_album_directory_rejects_missing_destination(tmp_path):
    service, _ = _make_service()

    with pytest.raises(MediaAcquisitionError):
        service._album_directory(tmp_path / "does-not-exist", _make_album())


def test_wrap_album_progress_maps_track_percent():
    seen: list[float] = []
    report = _wrap_album_progress(seen.append, total=4, position=3)

    report(50)

    assert seen == [62.5]


def test_wrap_album_progress_clamps_out_of_range():
    seen: list[float] = []
    report = _wrap_album_progress(seen.append, total=2, position=1)

    report(150)
    report(-20)

    assert seen == [50.0, 0.0]


def test_wrap_album_progress_without_callback():
    assert _wrap_album_progress(None, total=4, position=1) is None


def test_filename_uses_explicit_stem():
    track = {"title": "Ignored", "artist": {"name": "Ignored"}}

    assert _filename(track, "FLAC", stem="01 - Real Song") == "01 - Real Song.flac"
