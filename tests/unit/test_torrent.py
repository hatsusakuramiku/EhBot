"""Unit coverage for the torrent route's local decisions.

The pieces tested here are the ones a wrong answer would make expensive: an
infohash computed differently from the client's, a `torrents/add` request the
client silently reinterprets, or a state mapping that turns a recoverable
stall into a failure.
"""

from __future__ import annotations

from pathlib import Path
import zipfile

import httpx
import pytest

from app.torrent import bencode
from app.torrent.client import QBittorrentClient
from app.torrent.delivery import resolve_content_path, take_delivery
from app.torrent.fetcher import page_hashes, select_link, torrent_links
from app.torrent.models import (
    TorrentClientConfig,
    TorrentError,
    TorrentStatus,
)


JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 64


def build_torrent(name: bytes = b"book.zip", length: int = 1234) -> bytes:
    """A minimal single-file torrent, encoded canonically."""
    return bencode.encode(
        {
            b"announce": b"https://tracker.example/abcdef123456/announce",
            b"info": {
                b"length": length,
                b"name": name,
                b"piece length": 262144,
                b"pieces": b"\x00" * 20,
            },
        }
    )


def make_config(**overrides) -> TorrentClientConfig:
    values = {
        "base_url": "http://qb.example:8080",
        "username": "admin",
        "password": "secret",
        "category": "ehbot",
        "save_path": "/downloads/ehbot",
        "local_save_path": "/downloads/ehbot",
    }
    values.update(overrides)
    return TorrentClientConfig(**values)


def test_the_infohash_matches_a_canonical_reencode() -> None:
    payload = build_torrent()

    digest = bencode.infohash(payload)

    # Hashing the re-encoded `info` dict must agree with hashing the bytes as
    # they arrived, or the value would only ever match this code.
    start = payload.index(b"4:infod") + len(b"4:info")
    import hashlib

    assert digest == hashlib.sha1(payload[start:-1]).hexdigest()


def test_a_dictionary_with_unsorted_keys_is_refused() -> None:
    """A lenient parser would hash a normalized dict and get a different value."""
    payload = b"d4:infod6:lengthi1e4:name1:ae1:ai1ee"

    with pytest.raises(TorrentError) as excinfo:
        bencode.decode(payload)

    assert excinfo.value.code == "TORRENT_FILE_INVALID"


@pytest.mark.parametrize(
    "payload",
    [
        b"i-0e",
        b"i03e",
        b"d4:infod6:lengthi1ee",
        b"5:abc",
        b"",
        b"le" + b"e",
        b"i12",
        b"x",
    ],
)
def test_malformed_bencode_is_refused(payload: bytes) -> None:
    with pytest.raises(TorrentError):
        bencode.decode(payload)


def test_trailing_bytes_are_refused() -> None:
    with pytest.raises(TorrentError):
        bencode.decode(bencode.encode({b"a": 1}) + b"junk")


def test_announce_urls_are_readable_for_the_passkey_assertion() -> None:
    payload = bencode.encode(
        {
            b"announce": b"https://tracker.example/key/announce",
            b"announce-list": [
                [b"https://tracker.example/key/announce"],
                [b"https://backup.example/key/announce"],
            ],
            b"info": {b"length": 1, b"name": b"a", b"piece length": 1, b"pieces": b""},
        }
    )

    urls = bencode.announce_urls(payload)

    assert urls == (
        "https://tracker.example/key/announce",
        "https://backup.example/key/announce",
    )


def test_a_torrent_over_the_size_cap_is_never_parsed() -> None:
    with pytest.raises(TorrentError) as excinfo:
        bencode.decode(b"d" + b"0" * (bencode.MAX_TORRENT_BYTES + 1))

    assert excinfo.value.code == "TORRENT_FILE_INVALID"


PAGE = """
<table>
  <tr><td>4acbd66e5d0518977ece30c343eb75c4ca92b031</td>
      <td><a href="https://ehtracker.org/get/123/4acbd66e.torrent">Book.zip</a></td></tr>
  <tr><td>1111111111111111111111111111111111111111</td>
      <td><a href="https://ehtracker.org/get/456/other.torrent">Resample.zip</a></td></tr>
</table>
"""


def test_the_link_is_parsed_off_the_page_not_guessed() -> None:
    assert torrent_links(PAGE) == (
        "https://ehtracker.org/get/123/4acbd66e.torrent",
        "https://ehtracker.org/get/456/other.torrent",
    )
    assert page_hashes(PAGE) == (
        "4acbd66e5d0518977ece30c343eb75c4ca92b031",
        "1111111111111111111111111111111111111111",
    )


def test_the_hash_decides_which_link_is_taken() -> None:
    link = select_link(PAGE, "1111111111111111111111111111111111111111")

    # Matched by position on the page: EH does not put every hash in the URL,
    # and taking the first link would fetch a different torrent entirely.
    assert link == "https://ehtracker.org/get/456/other.torrent"


def test_a_hash_the_page_does_not_mention_is_refused() -> None:
    with pytest.raises(TorrentError) as excinfo:
        select_link(PAGE, "2222222222222222222222222222222222222222")

    assert excinfo.value.code == "TORRENT_FILE_FETCH_FAILED"


@pytest.mark.asyncio
async def test_the_add_request_carries_the_fields_that_decide_the_save_path() -> None:
    seen: dict = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/auth/login"):
            return httpx.Response(200, text="Ok.")
        seen["content_type"] = request.headers["content-type"]
        seen["body"] = request.content
        return httpx.Response(200, text="Ok.")

    client = QBittorrentClient(
        httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        make_config(),
    )

    await client.add_torrent(build_torrent(), "a" * 40)

    body = seen["body"]
    assert "multipart/form-data" in seen["content_type"]
    # autoTMM off is load-bearing: with automatic management on, a category
    # rule overrides savepath and EhBot reads from a directory never used.
    assert b'name="autoTMM"\r\n\r\nfalse' in body
    assert b'name="savepath"\r\n\r\n/downloads/ehbot' in body
    assert b'name="category"\r\n\r\nehbot' in body
    assert b'name="paused"\r\n\r\nfalse' in body
    # root_folder is absent so the torrent's own structure survives, which is
    # what lets delivery tell a single archive from a directory.
    assert b'name="root_folder"' not in body
    assert b"application/x-bittorrent" in body


@pytest.mark.asyncio
async def test_the_json_report_of_a_modern_client_counts_as_accepted() -> None:
    """WebAPI 2.11+ answers `torrents/add` with a JSON report, not `Ok.`.

    Treating anything that is not `Ok.` as a rejection failed every push
    against a current client even though the torrent had in fact started.
    """

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/auth/login"):
            return httpx.Response(200, text="Ok.")
        return httpx.Response(
            200,
            json={
                "added_torrent_ids": ["a" * 40],
                "failure_count": 0,
                "pending_count": 0,
                "success_count": 1,
            },
        )

    client = QBittorrentClient(
        httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        make_config(),
    )

    await client.add_torrent(build_torrent(), "a" * 40)


@pytest.mark.asyncio
async def test_a_json_report_of_only_failures_is_rejected() -> None:
    """The modern client answers HTTP 200 even when nothing was added."""

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/auth/login"):
            return httpx.Response(200, text="Ok.")
        return httpx.Response(
            200,
            json={
                "added_torrent_ids": [],
                "failure_count": 1,
                "pending_count": 0,
                "success_count": 0,
            },
        )

    client = QBittorrentClient(
        httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        make_config(),
    )

    with pytest.raises(TorrentError) as excinfo:
        await client.add_torrent(build_torrent(), "a" * 40)

    assert excinfo.value.code == "TORRENT_PUSH_REJECTED"


@pytest.mark.asyncio
async def test_a_torrent_the_client_already_holds_is_not_an_error() -> None:
    """`409 Conflict` means the hash is present, which is all the push needs.

    Re-pushing after a restart must stay safe, otherwise recovery would fail
    precisely on the jobs that were already parked correctly.
    """

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/auth/login"):
            return httpx.Response(200, text="Ok.")
        return httpx.Response(409, text="Conflict")

    client = QBittorrentClient(
        httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        make_config(),
    )

    await client.add_torrent(build_torrent(), "a" * 40)


@pytest.mark.asyncio
async def test_an_unparseable_torrent_is_a_permanent_failure() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/auth/login"):
            return httpx.Response(200, text="Ok.")
        return httpx.Response(415)

    client = QBittorrentClient(
        httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        make_config(),
    )

    with pytest.raises(TorrentError) as excinfo:
        await client.add_torrent(build_torrent(), "a" * 40)

    assert excinfo.value.code == "TORRENT_FILE_INVALID"


@pytest.mark.asyncio
async def test_an_expired_session_is_retried_once_after_relogin() -> None:
    calls: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        if request.url.path.endswith("/auth/login"):
            return httpx.Response(200, text="Ok.")
        if calls.count("/api/v2/torrents/add") == 1:
            return httpx.Response(403)
        return httpx.Response(200, text="Ok.")

    client = QBittorrentClient(
        httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        make_config(),
    )

    await client.add_torrent(build_torrent(), "a" * 40)

    # qBittorrent expires sessions on its own schedule, so exactly one silent
    # re-login is expected rather than a surfaced failure.
    assert calls.count("/api/v2/auth/login") == 2
    assert calls.count("/api/v2/torrents/add") == 2


@pytest.mark.asyncio
async def test_a_login_answered_with_204_is_accepted() -> None:
    """Some builds and reverse proxies answer `204 No Content` on success.

    The SID cookie is attached either way, so insisting on `200 Ok.` locks the
    operator out of a client that is in fact reachable and authenticated.
    """
    calls: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        if request.url.path.endswith("/auth/login"):
            return httpx.Response(204)
        return httpx.Response(200, text="v5.2.3")

    client = QBittorrentClient(
        httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        make_config(),
    )

    assert await client.version() == "v5.2.3"
    assert calls.count("/api/v2/auth/login") == 1


@pytest.mark.asyncio
async def test_a_login_rejected_with_401_is_an_auth_failure() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, text="Unauthorized")

    client = QBittorrentClient(
        httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        make_config(),
    )

    with pytest.raises(TorrentError) as excinfo:
        await client.login()

    assert excinfo.value.code == "TORRENT_CLIENT_AUTH"


@pytest.mark.asyncio
async def test_a_login_answered_with_a_server_error_is_retryable() -> None:
    """A 502 from a proxy is not the operator getting the password wrong."""

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(502, text="Bad Gateway")

    client = QBittorrentClient(
        httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        make_config(),
    )

    with pytest.raises(TorrentError) as excinfo:
        await client.login()

    assert excinfo.value.code == "TORRENT_CLIENT_UNREACHABLE"


@pytest.mark.asyncio
async def test_a_rejected_login_is_not_retried_forever() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="Fails.")

    client = QBittorrentClient(
        httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        make_config(),
    )

    with pytest.raises(TorrentError) as excinfo:
        await client.login()

    assert excinfo.value.code == "TORRENT_CLIENT_AUTH"


@pytest.mark.asyncio
async def test_an_unreachable_client_is_reported_as_retryable() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    client = QBittorrentClient(
        httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        make_config(),
    )

    with pytest.raises(TorrentError) as excinfo:
        await client.version()

    assert excinfo.value.code == "TORRENT_CLIENT_UNREACHABLE"


@pytest.mark.asyncio
async def test_a_hash_the_client_no_longer_holds_reads_as_missing() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/auth/login"):
            return httpx.Response(200, text="Ok.")
        return httpx.Response(200, json=[])

    client = QBittorrentClient(
        httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        make_config(),
    )

    assert await client.status("a" * 40) is None


@pytest.mark.asyncio
async def test_an_unknown_eta_is_not_shown_as_an_estimate() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/auth/login"):
            return httpx.Response(200, text="Ok.")
        return httpx.Response(
            200,
            json=[
                {
                    "hash": "A" * 40,
                    "state": "downloading",
                    "progress": 0.5,
                    "num_seeds": 3,
                    "dlspeed": 1024,
                    "eta": 8_640_000,
                    "content_path": "/downloads/ehbot/book.zip",
                    "size": 100,
                }
            ],
        )

    client = QBittorrentClient(
        httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        make_config(),
    )

    status = await client.status("a" * 40)

    assert status is not None
    assert status.eta is None
    assert status.hash == "a" * 40


@pytest.mark.parametrize(
    ("state", "seeds", "complete", "failed", "stalled"),
    [
        ("downloading", 5, False, False, False),
        ("stalledDL", 0, False, False, True),
        ("metaDL", 0, False, False, True),
        # A stall with a seeder in sight is just slow, not stalled.
        ("stalledDL", 2, False, False, False),
        ("error", 0, False, True, False),
        ("missingFiles", 0, False, True, False),
        ("uploading", 1, True, False, False),
        ("stalledUP", 0, True, False, False),
        ("pausedUP", 0, True, False, False),
    ],
)
def test_client_states_map_to_queue_meanings(
    state: str, seeds: int, complete: bool, failed: bool, stalled: bool
) -> None:
    status = TorrentStatus(
        hash="a" * 40,
        state=state,
        progress=0.5,
        num_seeds=seeds,
        dlspeed=0,
        eta=None,
        content_path="",
        size=0,
    )

    assert status.is_complete is complete
    assert status.is_failed is failed
    assert status.is_stalled is stalled


def test_full_progress_counts_as_complete_whatever_the_state_says() -> None:
    status = TorrentStatus(
        hash="a" * 40,
        state="downloading",
        progress=1.0,
        num_seeds=0,
        dlspeed=0,
        eta=None,
        content_path="",
        size=0,
    )

    assert status.is_complete is True


def test_the_client_path_is_translated_into_ehbots_view() -> None:
    resolved = resolve_content_path(
        "/downloads/ehbot/Book.zip", "/downloads/ehbot", "/mnt/qb/ehbot"
    )

    assert resolved == Path("/mnt/qb/ehbot/Book.zip")


def test_identical_roots_leave_the_path_alone() -> None:
    resolved = resolve_content_path(
        "/downloads/ehbot/Book.zip", "/downloads/ehbot", "/downloads/ehbot"
    )

    assert resolved == Path("/downloads/ehbot/Book.zip")


def test_a_missing_content_path_is_reported_not_guessed() -> None:
    with pytest.raises(TorrentError) as excinfo:
        resolve_content_path("", "/a", "/b")

    assert excinfo.value.code == "TORRENT_CONTENT_UNREACHABLE"


def test_a_single_archive_is_registered_without_repacking(
    tmp_path: Path,
) -> None:
    source = tmp_path / "seed" / "Book.zip"
    source.parent.mkdir(parents=True)
    with zipfile.ZipFile(source, "w") as archive:
        archive.writestr("0001.jpg", JPEG)

    delivery = take_delivery(source, tmp_path / "work", 42)

    assert delivery.was_directory is False
    assert Path(delivery.archive_path).name == "candidate-42.zip"
    # The seed must survive: a move would break seeding, so the original is
    # still exactly where the client left it.
    assert source.exists()
    assert source.read_bytes() == Path(delivery.archive_path).read_bytes()


def test_a_directory_of_images_is_packed(tmp_path: Path) -> None:
    source = tmp_path / "seed" / "Book"
    source.mkdir(parents=True)
    for index in (10, 2, 1):
        (source / f"{index}.jpg").write_bytes(JPEG)

    delivery = take_delivery(source, tmp_path / "work", 7)

    assert delivery.was_directory is True
    with zipfile.ZipFile(delivery.archive_path) as archive:
        # Natural order, so page 2 precedes page 10 as it does for every
        # other provider.
        assert archive.namelist() == ["0001.jpg", "0002.jpg", "0003.jpg"]
    assert (source / "1.jpg").exists()


def test_a_directory_holding_one_archive_is_not_repacked(
    tmp_path: Path,
) -> None:
    source = tmp_path / "seed" / "Book"
    source.mkdir(parents=True)
    inner = source / "Book.cbz"
    with zipfile.ZipFile(inner, "w") as archive:
        archive.writestr("0001.jpg", JPEG)

    delivery = take_delivery(source, tmp_path / "work", 3)

    assert delivery.was_directory is False
    assert Path(delivery.archive_path).suffix == ".cbz"


def test_content_that_is_neither_archive_nor_images_is_refused(
    tmp_path: Path,
) -> None:
    source = tmp_path / "seed" / "notes.txt"
    source.parent.mkdir(parents=True)
    source.write_text("hello", encoding="utf-8")

    with pytest.raises(TorrentError) as excinfo:
        take_delivery(source, tmp_path / "work", 1)

    assert excinfo.value.code == "TORRENT_CONTENT_UNEXPECTED"


def test_an_unreadable_save_directory_is_reported(tmp_path: Path) -> None:
    with pytest.raises(TorrentError) as excinfo:
        take_delivery(tmp_path / "missing", tmp_path / "work", 1)

    assert excinfo.value.code == "TORRENT_CONTENT_UNREACHABLE"