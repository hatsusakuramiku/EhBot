"""End-to-end coverage for the EH torrent download route.

qBittorrent and ExHentai are both stood up as `httpx.MockTransport` fakes, so
the whole chain runs — selection, `.torrent` fetch, push, poll, delivery, CBZ —
without a network or a real client.
"""

from __future__ import annotations

import asyncio
from dataclasses import replace
import hashlib
import json
from pathlib import Path
import re
import time
from urllib.parse import parse_qsl
import zipfile

import httpx
from fastapi.testclient import TestClient
import pytest

from app.candidates.ingestor import CandidateIngestor
from app.config import Settings
from app.connections.exhentai import ExHentaiCredentials
from app.conversion.service import ConversionService
from app.db.database import Database
from app.downloads.models import PROVIDER_EH_TORRENT
from app.downloads.service import DownloadError, DownloadService
from app.main import create_app
from app.secrets import SecretStore
from app.torrent import bencode
from app.torrent.models import TorrentClientConfig
from app.torrent.service import TorrentService


JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 512

DIGEST_NAME = b"[Sample] Book.zip"

TORRENT_PAGE = """
<table>
  <tr><td>{digest}</td>
      <td><a href="https://ehtracker.org/get/1/book.torrent">Book</a></td></tr>
</table>
"""


def build_torrent() -> tuple[bytes, str]:
    payload = bencode.encode(
        {
            # The announce URL carries the account passkey, which is why the
            # file never leaves the work directory.
            b"announce": b"https://ehtracker.org/PASSKEY/announce",
            b"info": {
                b"length": 1234,
                b"name": DIGEST_NAME,
                b"piece length": 262144,
                b"pieces": b"\x00" * 20,
            },
        }
    )
    return payload, bencode.infohash(payload)


TORRENT_PAYLOAD, TORRENT_DIGEST = build_torrent()


class FakeClient:
    """A qBittorrent stand-in that records what it was told to do."""

    def __init__(self, *, content_path: str = "", size: int = 1234) -> None:
        self.added: list[bytes] = []
        self.deleted: list[tuple[str, bool]] = []
        self.logins = 0
        self.rows: list[dict] = []
        self.content_path = content_path
        self.size = size
        self.add_status = 200
        #: Set to answer `torrents/add` the way a modern client does for a hash
        #: it already holds.
        self.add_conflicts = False

    def seed_status(self, **overrides) -> None:
        row = {
            "hash": TORRENT_DIGEST,
            "state": "downloading",
            "progress": 0.3,
            "num_seeds": 4,
            "dlspeed": 2048,
            "upspeed": 0,
            "eta": 600,
            "content_path": self.content_path,
            "size": self.size,
        }
        row.update(overrides)
        self.rows = [row]

    def transport(self) -> httpx.MockTransport:
        async def handler(request: httpx.Request) -> httpx.Response:
            path = request.url.path
            if path.endswith("/auth/login"):
                self.logins += 1
                return httpx.Response(200, text="Ok.")
            if path.endswith("/app/version"):
                return httpx.Response(200, text="v4.6.2")
            if path.endswith("/torrents/add"):
                self.added.append(request.content)
                if self.add_conflicts:
                    return httpx.Response(409, text="Conflict")
                return httpx.Response(self.add_status, text="Ok.")
            if path.endswith("/torrents/info"):
                return httpx.Response(200, json=self.rows)
            if path.endswith("/torrents/delete"):
                form = dict(
                    parse_qsl(request.content.decode("utf-8"))
                )
                self.deleted.append(
                    (
                        form.get("hashes", ""),
                        form.get("deleteFiles") == "true",
                    )
                )
                return httpx.Response(200, text="Ok.")
            return httpx.Response(404)

        return httpx.MockTransport(handler)


def exhentai_transport(
    *, page_status: int = 200, digest: str = TORRENT_DIGEST
) -> httpx.MockTransport:
    async def handler(request: httpx.Request) -> httpx.Response:
        if "gallerytorrents" in request.url.path:
            if page_status != 200:
                return httpx.Response(page_status)
            return httpx.Response(
                200, text=TORRENT_PAGE.format(digest=digest)
            )
        if request.url.path.endswith(".torrent"):
            return httpx.Response(200, content=TORRENT_PAYLOAD)
        return httpx.Response(404)

    return httpx.MockTransport(handler)


async def seed_torrent_candidate(
    database: Database,
    *,
    digest: str | None = TORRENT_DIGEST,
    torrent_count: int | None = 1,
    approved: bool = True,
    pages: int | None = None,
) -> int:
    await database.initialize()
    await database.configure_telegram_source(
        source_type="CHANNEL",
        chat_id=-100888,
        display_name="Torrent Channel",
        enabled=True,
        allowed_archive_formats=("zip",),
        max_attachment_size_mb=0,
    )
    await database.save_telegram_updates(
        [
            {
                "update_id": 700,
                "channel_post": {
                    "message_id": 700,
                    "date": 1_700_040_000,
                    "chat": {"id": -100888, "title": "Torrent Channel"},
                    "text": (
                        "Sample Book\n"
                        "https://exhentai.org/g/4108964/torrenttoken/"
                    ),
                },
            }
        ]
    )
    await CandidateIngestor(database).process_pending_updates()
    candidates = await database.list_candidates()
    candidate_id = candidates[0].candidate_id
    with database._connect() as connection:  # noqa: SLF001
        connection.execute(
            "UPDATE candidates SET torrent_count = ?, torrent_hash = ? "
            "WHERE id = ?",
            (torrent_count, digest, candidate_id),
        )
        if approved:
            connection.execute(
                "UPDATE candidates SET status = 'APPROVED' WHERE id = ?",
                (candidate_id,),
            )
        if pages is not None:
            connection.execute(
                "INSERT INTO metadata_values "
                "(candidate_id, field_name, field_value, value_source) "
                "VALUES (?, 'Pages', ?, 'EXHENTAI')",
                (candidate_id, str(pages)),
            )
    return candidate_id


def build_torrent_service(
    database: Database,
    tmp_path: Path,
    fake: FakeClient,
    *,
    keep_seeding: bool = True,
    configured: bool = True,
    auto_pack: bool = False,
    exhentai: httpx.MockTransport | None = None,
) -> TorrentService:
    config = TorrentClientConfig(
        base_url="http://qb.example:8080" if configured else "",
        username="admin",
        password="secret",
        category="ehbot",
        save_path="/downloads/ehbot",
        local_save_path=str(tmp_path / "seed"),
        keep_seeding=keep_seeding,
        auto_pack=auto_pack,
    )

    async def config_provider() -> TorrentClientConfig:
        return config

    async def credentials_provider() -> ExHentaiCredentials:
        return ExHentaiCredentials(
            ipb_member_id="1", ipb_pass_hash="2", igneous="3"
        )

    return TorrentService(
        database,
        tmp_path / "work",
        config_provider=config_provider,
        credentials_provider=credentials_provider,
        http_client=httpx.AsyncClient(
            transport=exhentai or exhentai_transport()
        ),
        client_http_client=httpx.AsyncClient(transport=fake.transport()),
    )


def build_download_service(
    database: Database, tmp_path: Path, torrent: TorrentService
) -> DownloadService:
    return DownloadService(
        database,
        tmp_path / "work",
        torrent_push=torrent.push_for_candidate,
        torrent_abandon=torrent.abandon,
        telegraph_download=lambda candidate_id: asyncio.sleep(0),
    )


def write_seed_archive(tmp_path: Path, name: str = "Book.zip") -> Path:
    """The payload qBittorrent would have produced, left where it saves it."""
    path = tmp_path / "seed" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as archive:
        for index in range(1, 4):
            archive.writestr(f"{index:04d}.jpg", JPEG)
    return path


@pytest.mark.asyncio
async def test_a_pushed_torrent_parks_instead_of_finishing(
    tmp_path: Path,
) -> None:
    """The transfer is the client's work, so the job must not hold a slot."""
    database = Database(tmp_path / "ehbot.db")
    candidate_id = await seed_torrent_candidate(database)
    fake = FakeClient()
    torrent = build_torrent_service(database, tmp_path, fake)
    downloads = build_download_service(database, tmp_path, torrent)
    await downloads.enqueue_torrent_download(candidate_id)

    assert await downloads._process_one() is True  # noqa: SLF001

    jobs = await downloads.list_jobs_for_candidate(candidate_id)
    assert jobs[0].provider == PROVIDER_EH_TORRENT
    assert jobs[0].state == "WAITING_TORRENT"
    assert jobs[0].details["hash"] == TORRENT_DIGEST
    assert jobs[0].is_waiting_for_peers is True
    assert len(fake.added) == 1
    # The worker must not claim it again; a parked job is the poller's.
    assert await downloads._process_one() is False  # noqa: SLF001


@pytest.mark.asyncio
async def test_the_infohash_is_verified_before_anything_is_pushed(
    tmp_path: Path,
) -> None:
    """A page that hands back the wrong file must not reach the client."""
    database = Database(tmp_path / "ehbot.db")
    other = "b" * 40
    candidate_id = await seed_torrent_candidate(database, digest=other)
    fake = FakeClient()
    torrent = build_torrent_service(
        database,
        tmp_path,
        fake,
        exhentai=exhentai_transport(digest=other),
    )
    downloads = build_download_service(database, tmp_path, torrent)
    await downloads.enqueue_torrent_download(candidate_id)

    await downloads._process_one()  # noqa: SLF001

    jobs = await downloads.list_jobs_for_candidate(candidate_id)
    assert jobs[0].state == "FAILED"
    assert jobs[0].error_code == "TORRENT_FILE_INVALID"
    assert jobs[0].is_retryable is False
    assert fake.added == []


@pytest.mark.asyncio
async def test_a_gallery_without_a_torrent_fails_permanently(
    tmp_path: Path,
) -> None:
    database = Database(tmp_path / "ehbot.db")
    candidate_id = await seed_torrent_candidate(
        database, digest=None, torrent_count=0
    )
    fake = FakeClient()
    torrent = build_torrent_service(database, tmp_path, fake)
    downloads = build_download_service(database, tmp_path, torrent)
    await downloads.enqueue_torrent_download(candidate_id)

    await downloads._process_one()  # noqa: SLF001

    jobs = await downloads.list_jobs_for_candidate(candidate_id)
    assert jobs[0].error_code == "TORRENT_NOT_AVAILABLE"
    assert jobs[0].is_retryable is False


@pytest.mark.asyncio
async def test_an_unconfigured_client_fails_without_touching_exhentai(
    tmp_path: Path,
) -> None:
    database = Database(tmp_path / "ehbot.db")
    candidate_id = await seed_torrent_candidate(database)
    fake = FakeClient()
    torrent = build_torrent_service(
        database, tmp_path, fake, configured=False
    )
    downloads = build_download_service(database, tmp_path, torrent)
    await downloads.enqueue_torrent_download(candidate_id)

    await downloads._process_one()  # noqa: SLF001

    jobs = await downloads.list_jobs_for_candidate(candidate_id)
    assert jobs[0].error_code == "TORRENT_CLIENT_NOT_CONFIG"
    assert fake.logins == 0


@pytest.mark.asyncio
async def test_a_failed_torrent_page_is_retryable(tmp_path: Path) -> None:
    """A Cookie that expired is worth another attempt once it is renewed."""
    database = Database(tmp_path / "ehbot.db")
    candidate_id = await seed_torrent_candidate(database)
    fake = FakeClient()
    torrent = build_torrent_service(
        database,
        tmp_path,
        fake,
        exhentai=exhentai_transport(page_status=403),
    )
    downloads = build_download_service(database, tmp_path, torrent)
    await downloads.enqueue_torrent_download(candidate_id)

    await downloads._process_one()  # noqa: SLF001

    jobs = await downloads.list_jobs_for_candidate(candidate_id)
    assert jobs[0].error_code == "TORRENT_FILE_FETCH_FAILED"
    assert jobs[0].is_retryable is True


@pytest.mark.asyncio
async def test_polling_records_progress_without_advancing_the_job(
    tmp_path: Path,
) -> None:
    database = Database(tmp_path / "ehbot.db")
    candidate_id = await seed_torrent_candidate(database)
    fake = FakeClient(content_path="/downloads/ehbot/Book.zip")
    fake.seed_status()
    torrent = build_torrent_service(database, tmp_path, fake)
    downloads = build_download_service(database, tmp_path, torrent)
    await downloads.enqueue_torrent_download(candidate_id)
    await downloads._process_one()  # noqa: SLF001

    assert await torrent.poll_once() == 1

    jobs = await downloads.list_jobs_for_candidate(candidate_id)
    assert jobs[0].state == "WAITING_TORRENT"
    assert jobs[0].progress_percent == 30
    assert jobs[0].details["num_seeds"] == 4
    assert jobs[0].stalled_minutes is None


@pytest.mark.asyncio
async def test_a_stalled_torrent_waits_and_reports_how_long(
    tmp_path: Path,
) -> None:
    """A seederless torrent may pick one up hours later, so it is not an error."""
    database = Database(tmp_path / "ehbot.db")
    candidate_id = await seed_torrent_candidate(database)
    fake = FakeClient()
    fake.seed_status(state="stalledDL", num_seeds=0, progress=0.0)
    torrent = build_torrent_service(database, tmp_path, fake)
    downloads = build_download_service(database, tmp_path, torrent)
    await downloads.enqueue_torrent_download(candidate_id)
    await downloads._process_one()  # noqa: SLF001

    await torrent.poll_once()
    # Backdate the stall so the elapsed-time reporting is observable.
    with database._connect() as connection:  # noqa: SLF001
        row = connection.execute(
            "SELECT id, details_json FROM download_jobs"
        ).fetchone()
        details = json.loads(row[1])
        details["stalled_since"] = time.time() - 25 * 60
        connection.execute(
            "UPDATE download_jobs SET details_json = ? WHERE id = ?",
            (json.dumps(details), row[0]),
        )

    jobs = await downloads.list_jobs_for_candidate(candidate_id)
    assert jobs[0].state == "WAITING_TORRENT"
    assert jobs[0].stalled_minutes == 25
    candidate = await database.get_candidate(candidate_id)
    assert candidate is not None
    assert candidate.status == "PROCESSING"


@pytest.mark.asyncio
async def test_a_stall_clears_once_a_seeder_appears(tmp_path: Path) -> None:
    database = Database(tmp_path / "ehbot.db")
    candidate_id = await seed_torrent_candidate(database)
    fake = FakeClient()
    fake.seed_status(state="stalledDL", num_seeds=0, progress=0.0)
    torrent = build_torrent_service(database, tmp_path, fake)
    downloads = build_download_service(database, tmp_path, torrent)
    await downloads.enqueue_torrent_download(candidate_id)
    await downloads._process_one()  # noqa: SLF001
    await torrent.poll_once()

    fake.seed_status(state="downloading", num_seeds=3, progress=0.4)
    await torrent.poll_once()

    jobs = await downloads.list_jobs_for_candidate(candidate_id)
    assert jobs[0].stalled_minutes is None
    assert "stalled_since" not in jobs[0].details


@pytest.mark.asyncio
async def test_a_finished_single_archive_is_delivered_by_hard_link(
    tmp_path: Path,
) -> None:
    database = Database(tmp_path / "ehbot.db")
    candidate_id = await seed_torrent_candidate(database)
    source = write_seed_archive(tmp_path)
    fake = FakeClient(
        content_path="/downloads/ehbot/Book.zip",
        size=source.stat().st_size,
    )
    fake.seed_status(state="uploading", progress=1.0)
    torrent = build_torrent_service(database, tmp_path, fake)
    downloads = build_download_service(database, tmp_path, torrent)
    await downloads.enqueue_torrent_download(candidate_id)
    await downloads._process_one()  # noqa: SLF001

    await torrent.poll_once()

    jobs = await downloads.list_jobs_for_candidate(candidate_id)
    assert jobs[0].state == "COMPLETED"
    assert jobs[0].artifact_path is not None
    assert Path(jobs[0].artifact_path).exists()
    # Seeding survives delivery: the client's copy is untouched.
    assert source.exists()
    assert fake.deleted == []
    candidate = await database.get_candidate(candidate_id)
    assert candidate is not None
    assert candidate.status == "DOWNLOADED"
    with database._connect() as connection:  # noqa: SLF001
        scan = connection.execute(
            "SELECT field_value, value_source FROM metadata_values "
            "WHERE candidate_id = ? AND field_name = 'ScanInformation'",
            (candidate_id,),
        ).fetchone()
    assert scan[0].startswith("EH_TORRENT original ")
    assert scan[1] == "EH_TORRENT"


@pytest.mark.asyncio
async def test_the_recorded_digest_matches_the_delivered_file(
    tmp_path: Path,
) -> None:
    database = Database(tmp_path / "ehbot.db")
    candidate_id = await seed_torrent_candidate(database)
    source = write_seed_archive(tmp_path)
    fake = FakeClient(content_path="/downloads/ehbot/Book.zip")
    fake.seed_status(state="stalledUP", progress=1.0)
    torrent = build_torrent_service(database, tmp_path, fake)
    downloads = build_download_service(database, tmp_path, torrent)
    await downloads.enqueue_torrent_download(candidate_id)
    await downloads._process_one()  # noqa: SLF001

    await torrent.poll_once()

    expected = hashlib.sha256(source.read_bytes()).hexdigest()
    with database._connect() as connection:  # noqa: SLF001
        row = connection.execute(
            "SELECT sha256, size_bytes FROM artifacts"
        ).fetchone()
    assert row[0] == expected
    assert row[1] == source.stat().st_size


@pytest.mark.asyncio
async def test_a_finished_directory_is_packed_in_natural_order(
    tmp_path: Path,
) -> None:
    database = Database(tmp_path / "ehbot.db")
    candidate_id = await seed_torrent_candidate(database)
    folder = tmp_path / "seed" / "Book"
    folder.mkdir(parents=True)
    for index in (10, 9, 1):
        (folder / f"page-{index}.jpg").write_bytes(JPEG)
    fake = FakeClient(content_path="/downloads/ehbot/Book")
    fake.seed_status(state="uploading", progress=1.0)
    torrent = build_torrent_service(database, tmp_path, fake)
    downloads = build_download_service(database, tmp_path, torrent)
    await downloads.enqueue_torrent_download(candidate_id)
    await downloads._process_one()  # noqa: SLF001

    await torrent.poll_once()

    jobs = await downloads.list_jobs_for_candidate(candidate_id)
    assert jobs[0].state == "COMPLETED"
    with zipfile.ZipFile(jobs[0].artifact_path) as archive:
        assert archive.namelist() == ["0001.jpg", "0002.jpg", "0003.jpg"]
    assert jobs[0].details["packed_directory"] is True
    assert (folder / "page-1.jpg").exists()


@pytest.mark.asyncio
async def test_seeding_is_dropped_only_when_the_operator_asked(
    tmp_path: Path,
) -> None:
    database = Database(tmp_path / "ehbot.db")
    candidate_id = await seed_torrent_candidate(database)
    write_seed_archive(tmp_path)
    fake = FakeClient(content_path="/downloads/ehbot/Book.zip")
    fake.seed_status(state="uploading", progress=1.0)
    torrent = build_torrent_service(
        database, tmp_path, fake, keep_seeding=False
    )
    downloads = build_download_service(database, tmp_path, torrent)
    await downloads.enqueue_torrent_download(candidate_id)
    await downloads._process_one()  # noqa: SLF001

    await torrent.poll_once()

    # Removed from the client, but never with deleteFiles: the payload EhBot
    # just hard-linked must survive.
    assert fake.deleted == [(TORRENT_DIGEST, False)]


@pytest.mark.asyncio
async def test_a_hash_that_vanished_from_the_client_is_retryable(
    tmp_path: Path,
) -> None:
    database = Database(tmp_path / "ehbot.db")
    candidate_id = await seed_torrent_candidate(database)
    fake = FakeClient()
    fake.rows = []
    torrent = build_torrent_service(database, tmp_path, fake)
    downloads = build_download_service(database, tmp_path, torrent)
    await downloads.enqueue_torrent_download(candidate_id)
    await downloads._process_one()  # noqa: SLF001

    await torrent.poll_once()

    jobs = await downloads.list_jobs_for_candidate(candidate_id)
    assert jobs[0].state == "FAILED"
    assert jobs[0].error_code == "TORRENT_VANISHED"
    assert jobs[0].is_retryable is True


@pytest.mark.asyncio
async def test_a_client_error_state_fails_the_job(tmp_path: Path) -> None:
    database = Database(tmp_path / "ehbot.db")
    candidate_id = await seed_torrent_candidate(database)
    fake = FakeClient()
    fake.seed_status(state="missingFiles")
    torrent = build_torrent_service(database, tmp_path, fake)
    downloads = build_download_service(database, tmp_path, torrent)
    await downloads.enqueue_torrent_download(candidate_id)
    await downloads._process_one()  # noqa: SLF001

    await torrent.poll_once()

    jobs = await downloads.list_jobs_for_candidate(candidate_id)
    assert jobs[0].error_code == "TORRENT_PUSH_REJECTED"
    assert jobs[0].is_retryable is True


@pytest.mark.asyncio
async def test_a_restart_reattaches_to_a_parked_torrent(
    tmp_path: Path,
) -> None:
    """Parked jobs are read from the database, so recovery needs no extra path."""
    database = Database(tmp_path / "ehbot.db")
    candidate_id = await seed_torrent_candidate(database)
    write_seed_archive(tmp_path)
    fake = FakeClient(content_path="/downloads/ehbot/Book.zip")
    torrent = build_torrent_service(database, tmp_path, fake)
    downloads = build_download_service(database, tmp_path, torrent)
    await downloads.enqueue_torrent_download(candidate_id)
    await downloads._process_one()  # noqa: SLF001

    # A brand-new service instance, as after a process restart.
    revived = build_torrent_service(database, tmp_path, fake)
    fake.seed_status(state="uploading", progress=1.0)
    assert await revived.poll_once() == 1

    jobs = await downloads.list_jobs_for_candidate(candidate_id)
    assert jobs[0].state == "COMPLETED"


@pytest.mark.asyncio
async def test_switching_to_the_preview_source_removes_the_torrent(
    tmp_path: Path,
) -> None:
    """The operator's answer to a stall, and it must be idempotent."""
    database = Database(tmp_path / "ehbot.db")
    candidate_id = await seed_torrent_candidate(database)
    fake = FakeClient()
    fake.seed_status(state="stalledDL", num_seeds=0)
    torrent = build_torrent_service(database, tmp_path, fake)
    downloads = build_download_service(database, tmp_path, torrent)
    await downloads.enqueue_torrent_download(candidate_id)
    await downloads._process_one()  # noqa: SLF001
    torrent_job = (await downloads.list_jobs_for_candidate(candidate_id))[0]

    await downloads.switch_source(torrent_job.job_id, "TELEGRAPH")

    assert fake.deleted == [(TORRENT_DIGEST, False)]
    jobs = await downloads.list_jobs_for_candidate(candidate_id)
    providers = {job.provider: job.state for job in jobs}
    assert providers["EH_TORRENT"] == "CANCELLED"
    assert providers["TELEGRAPH"] == "PENDING"


@pytest.mark.asyncio
async def test_cancelling_a_parked_job_takes_it_out_of_the_client(
    tmp_path: Path,
) -> None:
    database = Database(tmp_path / "ehbot.db")
    candidate_id = await seed_torrent_candidate(database)
    fake = FakeClient()
    torrent = build_torrent_service(database, tmp_path, fake)
    downloads = build_download_service(database, tmp_path, torrent)
    await downloads.enqueue_torrent_download(candidate_id)
    await downloads._process_one()  # noqa: SLF001
    job_id = (await downloads.list_jobs_for_candidate(candidate_id))[0].job_id

    await downloads.cancel_job(job_id)

    assert fake.deleted == [(TORRENT_DIGEST, False)]


@pytest.mark.asyncio
async def test_an_unreachable_client_does_not_block_cancelling(
    tmp_path: Path,
) -> None:
    """Otherwise a job could not be abandoned exactly when the client is broken."""
    database = Database(tmp_path / "ehbot.db")
    candidate_id = await seed_torrent_candidate(database)

    async def failing(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    fake = FakeClient()
    torrent = build_torrent_service(database, tmp_path, fake)
    downloads = build_download_service(database, tmp_path, torrent)
    await downloads.enqueue_torrent_download(candidate_id)
    await downloads._process_one()  # noqa: SLF001
    job_id = (await downloads.list_jobs_for_candidate(candidate_id))[0].job_id
    torrent._client_http_client = httpx.AsyncClient(  # noqa: SLF001
        transport=httpx.MockTransport(failing)
    )

    await downloads.cancel_job(job_id)

    jobs = await downloads.list_jobs_for_candidate(candidate_id)
    assert jobs[0].state == "CANCELLED"


@pytest.mark.asyncio
async def test_the_torrent_archive_converts_to_a_cbz_with_source_grade(
    tmp_path: Path,
) -> None:
    """The existing pipeline consumes the torrent payload with no special casing."""
    database = Database(tmp_path / "ehbot.db")
    candidate_id = await seed_torrent_candidate(database)
    write_seed_archive(tmp_path)
    with database._connect() as connection:  # noqa: SLF001
        connection.execute(
            "INSERT INTO metadata_values "
            "(candidate_id, field_name, field_value, value_source) "
            "VALUES (?, 'Title', 'Torrent Book', 'EXHENTAI')",
            (candidate_id,),
        )
    fake = FakeClient(content_path="/downloads/ehbot/Book.zip")
    fake.seed_status(state="uploading", progress=1.0)
    torrent = build_torrent_service(database, tmp_path, fake)
    downloads = build_download_service(database, tmp_path, torrent)
    await downloads.enqueue_torrent_download(candidate_id)
    await downloads._process_one()  # noqa: SLF001
    await torrent.poll_once()

    conversion = ConversionService(
        database,
        tmp_path / "work",
        tmp_path / "library",
        data_path=tmp_path / "data",
    )
    await conversion.enqueue_for_candidate(candidate_id)
    assert await conversion._process_one() is True  # noqa: SLF001

    published = list((tmp_path / "library").glob("*.cbz"))
    assert len(published) == 1
    with zipfile.ZipFile(published[0]) as archive:
        comicinfo = archive.read("ComicInfo.xml").decode("utf-8")
    assert "<ScanInformation>EH_TORRENT original" in comicinfo


def make_settings(root: Path) -> Settings:
    return Settings(
        data_path=root / "data",
        library_path=root / "library",
        work_path=root / "work",
        app_secret_key="test-secret-key-with-at-least-32-characters",
        tag_translation_enabled=False,
        archive_toolchain_auto_install=False,
    )


def store_exhentai_cookies(settings: Settings) -> None:
    """Fetching a `.torrent` needs a logged-in session, so the route needs this."""
    SecretStore(settings.data_path / "private").write(
        "exhentai_cookies",
        ExHentaiCredentials(
            ipb_member_id="1", ipb_pass_hash="2", igneous="3"
        ).to_json(),
    )


def authenticate(client: TestClient, settings: Settings) -> None:
    bootstrap_password = (
        settings.data_path / "bootstrap_admin_password"
    ).read_text(encoding="utf-8")
    login_page = client.get("/login")
    client.post(
        "/login",
        data={
            "password": bootstrap_password,
            "csrf_token": login_page.context["csrf_token"],
        },
    )
    change_page = client.get("/change-password")
    client.post(
        "/change-password",
        data={
            "current_password": bootstrap_password,
            "new_password": "new-password-with-12-characters",
            "confirmation": "new-password-with-12-characters",
            "csrf_token": change_page.context["csrf_token"],
        },
    )


def test_the_review_page_offers_the_torrent_and_queues_it_once(
    tmp_path: Path,
) -> None:
    settings = make_settings(tmp_path)
    database = Database(settings.data_path / "ehbot.db")
    candidate_id = asyncio.run(seed_torrent_candidate(database))
    fake = FakeClient()
    app = create_app(
        settings,
        exhentai_transport=exhentai_transport(),
        torrent_client_transport=fake.transport(),
    )

    with TestClient(app, follow_redirects=False) as client:
        authenticate(client, settings)
        detail = client.get(f"/works/{candidate_id}")
        csrf = detail.context["csrf_token"]
        # R6 names the source buttons from the provider vocabulary rather than
        # from page copy, so the button reads what every other surface calls it.
        assert "EH \u79cd\u5b50" in detail.text
        response = client.post(
            f"/candidates/{candidate_id}/torrent", data={"csrf_token": csrf}
        )
        assert response.status_code == 303
        # Idempotent: pressing twice must not create a second job.
        client.post(
            f"/candidates/{candidate_id}/torrent", data={"csrf_token": csrf}
        )

    jobs = asyncio.run(
        DownloadService(database, settings.work_path).list_jobs_for_candidate(
            candidate_id
        )
    )
    assert [job.provider for job in jobs] == [PROVIDER_EH_TORRENT]


def test_the_queue_shows_progress_and_offers_the_manual_actions(
    tmp_path: Path,
) -> None:
    settings = replace(make_settings(tmp_path), torrent_poll_seconds=1)
    database = Database(settings.data_path / "ehbot.db")
    candidate_id = asyncio.run(seed_torrent_candidate(database))
    (tmp_path / "seed").mkdir(parents=True, exist_ok=True)
    fake = FakeClient()
    fake.seed_status(state="stalledDL", num_seeds=0, progress=0.42)
    store_exhentai_cookies(settings)
    app = create_app(
        settings,
        exhentai_transport=exhentai_transport(),
        torrent_client_transport=fake.transport(),
    )

    with TestClient(app, follow_redirects=False) as client:
        authenticate(client, settings)
        page = client.get(f"/works/{candidate_id}")
        csrf = page.context["csrf_token"]
        client.post(
            "/archive-settings/torrent",
            data={
                "csrf_token": csrf,
                "base_url": "http://qb.example:8080",
                "username": "admin",
                "password": "secret",
                "save_path": "/downloads/ehbot",
                "local_save_path": str(tmp_path / "seed"),
                "keep_seeding": "on",
            },
        )
        client.post(
            f"/candidates/{candidate_id}/torrent",
            data={"csrf_token": csrf},
        )
        # The app runs its own worker and poller, so this waits for them
        # rather than driving them from a second event loop.
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            queue = client.get("/activity")
            if "42%" in queue.text:
                break
            time.sleep(0.2)

    assert queue.status_code == 200
    # The state reaches the operator as vocabulary, never as the enum. The old
    # page printed `job.state` raw, which is how `WAITING_TORRENT` got on screen.
    # The code is still in the markup as `data-code` for an inspector, so the
    # check is against the text with tags removed -- what a person actually sees,
    # and what a screen reader actually reads.
    visible = re.sub(r"<[^>]+>", " ", queue.text)
    assert "WAITING_TORRENT" not in visible
    assert "\u7b49\u5f85\u505a\u79cd" in visible
    # 42% and the seeder count arrive as one composed line, so the transfer
    # reads as a sentence rather than four unlabelled numbers.
    assert "42%" in queue.text
    assert "\u505a\u79cd\u8005 0" in queue.text
    # A stall is not a failure, so the row is filed under the needs-attention
    # section with its reason spelled out, and keeps every action it had.
    assert "\u79cd\u5b50\u65e0\u505a\u79cd\u8005" in queue.text
    assert "\u9700\u5e72\u9884" in queue.text
    # The three manual actions a stalled torrent needs, and no auto-degrade.
    assert "\u6539\u7528\u9884\u89c8\u56fe\u6e90" in queue.text
    assert "\u7528 Archive Download" in queue.text
    assert "\u53d6\u6d88" in queue.text
    # Progress used to arrive by reloading the whole document, which threw away
    # scroll position and any open menu with it. It now arrives through
    # `activity.js`, armed by `data-live` -- and the meta refresh is gone.
    assert 'http-equiv="refresh"' not in queue.text
    assert 'data-live="true"' in queue.text


def test_the_queue_keeps_showing_a_torrent_that_is_still_seeding(
    tmp_path: Path,
) -> None:
    """A completed job is normally gone, but a seed is still using resources."""
    settings = replace(make_settings(tmp_path), torrent_poll_seconds=1)
    database = Database(settings.data_path / "ehbot.db")
    candidate_id = asyncio.run(seed_torrent_candidate(database))
    write_seed_archive(tmp_path)
    fake = FakeClient(content_path="/downloads/ehbot/Book.zip")
    fake.seed_status(state="uploading", progress=1.0, upspeed=8192)
    store_exhentai_cookies(settings)
    app = create_app(
        settings,
        exhentai_transport=exhentai_transport(),
        torrent_client_transport=fake.transport(),
    )

    with TestClient(app, follow_redirects=False) as client:
        authenticate(client, settings)
        page = client.get(f"/works/{candidate_id}")
        csrf = page.context["csrf_token"]
        client.post(
            "/archive-settings/torrent",
            data={
                "csrf_token": csrf,
                "base_url": "http://qb.example:8080",
                "username": "admin",
                "password": "secret",
                "save_path": "/downloads/ehbot",
                "local_save_path": str(tmp_path / "seed"),
                "keep_seeding": "on",
            },
        )
        client.post(
            f"/candidates/{candidate_id}/torrent",
            data={"csrf_token": csrf},
        )
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            queue = client.get("/activity")
            if "\u6b63\u5728\u505a\u79cd" in queue.text:
                break
            time.sleep(0.2)

        # The lifecycle state is COMPLETED, and\u300c\u5df2\u5b8c\u6210\u300don its own would tell the
        # operator this job has stopped using their upstream -- which is exactly
        # what it has not done. The seeding note is a second badge beside it.
        assert "\u6b63\u5728\u505a\u79cd" in queue.text
        assert "\u505c\u6b62\u505a\u79cd" in queue.text
        job_id = int(
            asyncio.run(
                DownloadService(
                    database, settings.work_path
                ).list_jobs_for_candidate(candidate_id)
            )[0].job_id
        )
        stopped = client.post(
            f"/activity/jobs/{job_id}/stop-seeding", data={"csrf_token": csrf}
        )
        assert stopped.status_code == 303
        after = client.get("/activity")
        assert "\u6b63\u5728\u505a\u79cd" not in after.text

    # Stopping the seed removed the client entry without deleting files.
    assert fake.deleted == [(TORRENT_DIGEST, False)]


def test_the_settings_page_saves_a_client_without_echoing_the_password(
    tmp_path: Path,
) -> None:
    settings = make_settings(tmp_path)
    fake = FakeClient()
    app = create_app(settings, torrent_client_transport=fake.transport())

    with TestClient(app, follow_redirects=False) as client:
        authenticate(client, settings)
        page = client.get("/archive-settings")
        csrf = page.context["csrf_token"]
        saved = client.post(
            "/archive-settings/torrent",
            data={
                "csrf_token": csrf,
                "base_url": "http://qb.example:8080",
                "username": "admin",
                "password": "super-secret",
                "category": "ehbot",
                "save_path": "/downloads/ehbot",
                "local_save_path": str(tmp_path),
                "keep_seeding": "on",
            },
        )
        assert saved.status_code == 303
        reloaded = client.get("/archive-settings")
        assert "super-secret" not in reloaded.text
        assert "qb.example:8080" in reloaded.text
        tested = client.post(
            "/archive-settings/torrent-test", data={"csrf_token": csrf}
        )
        assert tested.status_code == 200
        assert "v4.6.2" in tested.text


def test_an_unreadable_local_save_path_is_refused_at_save_time(
    tmp_path: Path,
) -> None:
    """A typo found three hours into a torrent is a wasted transfer."""
    settings = make_settings(tmp_path)
    fake = FakeClient()
    app = create_app(settings, torrent_client_transport=fake.transport())

    with TestClient(app, follow_redirects=False) as client:
        authenticate(client, settings)
        page = client.get("/archive-settings")
        response = client.post(
            "/archive-settings/torrent",
            data={
                "csrf_token": page.context["csrf_token"],
                "base_url": "http://qb.example:8080",
                "local_save_path": str(tmp_path / "nope"),
            },
        )

    assert response.status_code == 400
    assert "\u8bfb\u4e0d\u5230" in response.text


def test_auto_pack_carries_a_delivered_torrent_into_the_library(
    tmp_path: Path,
) -> None:
    """With the switch on, a finished download reaches the library unattended.

    Wired through the real app rather than a stub so the conversion queue the
    poller hands off to is the one the operator would otherwise press a button
    for.
    """
    settings = replace(make_settings(tmp_path), torrent_poll_seconds=1)
    database = Database(settings.data_path / "ehbot.db")
    candidate_id = asyncio.run(seed_torrent_candidate(database))
    write_seed_archive(tmp_path)
    fake = FakeClient(content_path="/downloads/ehbot/Book.zip")
    fake.seed_status(state="uploading", progress=1.0)
    store_exhentai_cookies(settings)
    app = create_app(
        settings,
        exhentai_transport=exhentai_transport(),
        torrent_client_transport=fake.transport(),
    )

    with TestClient(app, follow_redirects=False) as client:
        authenticate(client, settings)
        page = client.get(f"/works/{candidate_id}")
        csrf = page.context["csrf_token"]
        saved = client.post(
            "/archive-settings/torrent",
            data={
                "csrf_token": csrf,
                "base_url": "http://qb.example:8080",
                "username": "admin",
                "password": "secret",
                "save_path": "/downloads/ehbot",
                "local_save_path": str(tmp_path / "seed"),
                "keep_seeding": "on",
                "auto_pack": "on",
            },
        )
        assert saved.status_code == 303
        client.post(
            f"/candidates/{candidate_id}/torrent",
            data={"csrf_token": csrf},
        )
        deadline = time.monotonic() + 30
        conversion_state = None
        while time.monotonic() < deadline:
            with database._connect() as connection:  # noqa: SLF001
                row = connection.execute(
                    "SELECT state FROM download_jobs "
                    "WHERE candidate_id = ? AND provider = 'CONVERSION'",
                    (candidate_id,),
                ).fetchone()
            if row is not None:
                conversion_state = str(row[0])
                break
            time.sleep(0.2)

    # The poller handed off to the conversion queue with nobody pressing
    # anything, which is the whole point of the switch.
    assert conversion_state is not None, "auto pack never queued a conversion"


def test_auto_pack_is_off_until_the_operator_turns_it_on(
    tmp_path: Path,
) -> None:
    """Packing publishes to the library, so it is never the default."""
    settings = make_settings(tmp_path)
    fake = FakeClient()
    app = create_app(settings, torrent_client_transport=fake.transport())

    with TestClient(app, follow_redirects=False) as client:
        authenticate(client, settings)
        page = client.get("/archive-settings")
        assert page.context["torrent"]["auto_pack"] is False
        client.post(
            "/archive-settings/torrent",
            data={
                "csrf_token": page.context["csrf_token"],
                "base_url": "http://qb.example:8080",
                "local_save_path": str(tmp_path),
                "auto_pack": "on",
            },
        )
        reloaded = client.get("/archive-settings")
        assert reloaded.context["torrent"]["auto_pack"] is True
        # Unchecking the box has to switch it back off, not just leave the
        # stored value alone.
        client.post(
            "/archive-settings/torrent",
            data={
                "csrf_token": page.context["csrf_token"],
                "base_url": "http://qb.example:8080",
                "local_save_path": str(tmp_path),
            },
        )
        assert client.get("/archive-settings").context["torrent"][
            "auto_pack"
        ] is False


def test_auto_pack_cannot_be_enabled_without_a_local_save_path(
    tmp_path: Path,
) -> None:
    """Automatic packing reads the payload with nobody watching.

    Without a proven directory the pack would fail hours later, so the
    requirement is enforced at save time instead.
    """
    settings = make_settings(tmp_path)
    fake = FakeClient()
    app = create_app(settings, torrent_client_transport=fake.transport())

    with TestClient(app, follow_redirects=False) as client:
        authenticate(client, settings)
        page = client.get("/archive-settings")
        response = client.post(
            "/archive-settings/torrent",
            data={
                "csrf_token": page.context["csrf_token"],
                "base_url": "http://qb.example:8080",
                "local_save_path": "",
                "auto_pack": "on",
            },
        )

        assert response.status_code == 400
        assert "\u5fc5\u987b\u586b\u5199\u4fdd\u5b58\u76ee\u5f55" in response.text
        # Nothing was stored, so the flag did not sneak on behind the error.
        assert client.get("/archive-settings").context["torrent"][
            "auto_pack"
        ] is False


@pytest.mark.asyncio
async def test_a_delivered_torrent_is_packed_only_when_auto_pack_is_on(
    tmp_path: Path,
) -> None:
    """The switch decides whether delivery continues into the library."""
    for auto_pack, expected in ((False, 0), (True, 1)):
        root = tmp_path / f"case-{auto_pack}"
        root.mkdir()
        database = Database(root / "ehbot.db")
        candidate_id = await seed_torrent_candidate(database)
        write_seed_archive(root)
        fake = FakeClient(content_path="/downloads/ehbot/Book.zip")
        packed: list[int] = []
        torrent = build_torrent_service(
            database, root, fake, auto_pack=auto_pack
        )
        torrent._auto_pack = packed.append  # noqa: SLF001
        downloads = build_download_service(database, root, torrent)
        await downloads.enqueue_torrent_download(candidate_id)
        await downloads._process_one()  # noqa: SLF001
        fake.seed_status(state="uploading", progress=1.0)

        await torrent.poll_once()

        jobs = await downloads.list_jobs_for_candidate(candidate_id)
        assert jobs[0].state == "COMPLETED"
        assert len(packed) == expected


@pytest.mark.asyncio
async def test_a_failed_auto_pack_does_not_fail_the_download(
    tmp_path: Path,
) -> None:
    """The archive is registered either way, so the operator can still convert.

    Reporting the download as failed would misstate what happened and hide a
    payload that is on disk and complete.
    """
    database = Database(tmp_path / "ehbot.db")
    candidate_id = await seed_torrent_candidate(database)
    write_seed_archive(tmp_path)
    fake = FakeClient(content_path="/downloads/ehbot/Book.zip")
    torrent = build_torrent_service(database, tmp_path, fake, auto_pack=True)

    async def explode(candidate: int) -> None:
        raise RuntimeError("conversion queue is down")

    torrent._auto_pack = explode  # noqa: SLF001
    downloads = build_download_service(database, tmp_path, torrent)
    await downloads.enqueue_torrent_download(candidate_id)
    await downloads._process_one()  # noqa: SLF001
    fake.seed_status(state="uploading", progress=1.0)

    await torrent.poll_once()

    jobs = await downloads.list_jobs_for_candidate(candidate_id)
    assert jobs[0].state == "COMPLETED"
    assert jobs[0].error_code is None
    assert Path(jobs[0].artifact_path).exists()


@pytest.mark.asyncio
async def test_a_duplicate_push_is_flagged_for_the_operator(
    tmp_path: Path,
) -> None:
    """A hash the client already holds parks, but says so.

    The push is not a failure — the payload is on its way — but the entry doing
    the work is not the one EhBot just described, so its save path and category
    may differ and delivery could look in the wrong directory.
    """
    database = Database(tmp_path / "ehbot.db")
    candidate_id = await seed_torrent_candidate(database)
    fake = FakeClient()
    fake.add_conflicts = True
    torrent = build_torrent_service(database, tmp_path, fake)
    downloads = build_download_service(database, tmp_path, torrent)
    await downloads.enqueue_torrent_download(candidate_id)

    await downloads._process_one()  # noqa: SLF001

    jobs = await downloads.list_jobs_for_candidate(candidate_id)
    assert jobs[0].state == "WAITING_TORRENT"
    assert jobs[0].was_already_in_client is True


@pytest.mark.asyncio
async def test_polling_records_the_progress_the_dashboard_reads(
    tmp_path: Path,
) -> None:
    """Each pass folds the client's numbers into the job for display."""
    database = Database(tmp_path / "ehbot.db")
    candidate_id = await seed_torrent_candidate(database)
    fake = FakeClient(content_path="/downloads/ehbot/Book.zip")
    torrent = build_torrent_service(database, tmp_path, fake)
    downloads = build_download_service(database, tmp_path, torrent)
    await downloads.enqueue_torrent_download(candidate_id)
    await downloads._process_one()  # noqa: SLF001
    fake.seed_status(state="downloading", progress=0.42, num_seeds=7, dlspeed=99000)

    assert await torrent.poll_once() == 1

    jobs = await downloads.list_jobs_for_candidate(candidate_id)
    assert jobs[0].state == "WAITING_TORRENT"
    assert jobs[0].progress_percent == 42
    assert jobs[0].torrent_state == "downloading"
    assert jobs[0].details["num_seeds"] == 7
    assert jobs[0].details["dlspeed"] == 99000
    assert jobs[0].stalled_minutes is None


@pytest.mark.asyncio
async def test_a_finished_torrent_stays_visible_while_it_seeds(
    tmp_path: Path,
) -> None:
    """Seeding uses the operator's bandwidth, so the job must not vanish.

    Every other provider is finished the moment the job completes; this one
    leaves the client uploading, which is something the dashboard has to show.
    """
    database = Database(tmp_path / "ehbot.db")
    candidate_id = await seed_torrent_candidate(database)
    write_seed_archive(tmp_path)
    fake = FakeClient(content_path="/downloads/ehbot/Book.zip")
    torrent = build_torrent_service(database, tmp_path, fake)
    downloads = build_download_service(database, tmp_path, torrent)
    await downloads.enqueue_torrent_download(candidate_id)
    await downloads._process_one()  # noqa: SLF001
    fake.seed_status(state="uploading", progress=1.0, upspeed=4096)

    await torrent.poll_once()

    jobs = await downloads.list_jobs_for_candidate(candidate_id)
    assert jobs[0].state == "COMPLETED"
    assert jobs[0].is_seeding is True
    assert jobs[0].upload_speed == 4096
    # The seed was left alone: a delete here would stop sharing the payload.
    assert fake.deleted == []
    assert any(
        job.job_id == jobs[0].job_id
        for job in await downloads.list_active_jobs()
    )


@pytest.mark.asyncio
async def test_stopping_the_seed_removes_the_entry_but_keeps_the_archive(
    tmp_path: Path,
) -> None:
    """The operator ends seeding; the archive the library registered stays."""
    database = Database(tmp_path / "ehbot.db")
    candidate_id = await seed_torrent_candidate(database)
    write_seed_archive(tmp_path)
    fake = FakeClient(content_path="/downloads/ehbot/Book.zip")
    torrent = build_torrent_service(database, tmp_path, fake)
    downloads = build_download_service(database, tmp_path, torrent)
    await downloads.enqueue_torrent_download(candidate_id)
    await downloads._process_one()  # noqa: SLF001
    fake.seed_status(state="uploading", progress=1.0, upspeed=4096)
    await torrent.poll_once()
    job_id = (await downloads.list_jobs_for_candidate(candidate_id))[0].job_id

    await downloads.stop_seeding(job_id)

    jobs = await downloads.list_jobs_for_candidate(candidate_id)
    assert jobs[0].state == "COMPLETED"
    assert jobs[0].is_seeding is False
    # Files are never deleted, so the archive conversion picked up survives.
    assert fake.deleted == [(TORRENT_DIGEST, False)]
    assert Path(jobs[0].artifact_path).exists()
    assert all(
        job.job_id != job_id for job in await downloads.list_active_jobs()
    )


@pytest.mark.asyncio
async def test_only_a_finished_torrent_can_have_its_seed_stopped(
    tmp_path: Path,
) -> None:
    """A job still waiting on peers is cancelled, not un-seeded."""
    database = Database(tmp_path / "ehbot.db")
    candidate_id = await seed_torrent_candidate(database)
    fake = FakeClient()
    torrent = build_torrent_service(database, tmp_path, fake)
    downloads = build_download_service(database, tmp_path, torrent)
    await downloads.enqueue_torrent_download(candidate_id)
    await downloads._process_one()  # noqa: SLF001
    job_id = (await downloads.list_jobs_for_candidate(candidate_id))[0].job_id

    with pytest.raises(DownloadError) as excinfo:
        await downloads.stop_seeding(job_id)

    assert excinfo.value.code == "JOB_NOT_SEEDING"
