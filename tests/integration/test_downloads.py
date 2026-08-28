import asyncio
import hashlib
from pathlib import Path
import tempfile

import httpx
import pytest
from fastapi.testclient import TestClient

from app.candidates.ingestor import CandidateIngestor
from app.config import Settings
from app.db.database import Database
from app.downloads.models import (
    DOWNLOAD_STATE_CANCELLED,
    DOWNLOAD_STATE_COMPLETED,
    DOWNLOAD_STATE_FAILED,
    DOWNLOAD_STATE_PAUSED,
    DOWNLOAD_STATE_PENDING,
)
from app.downloads.service import DownloadError, DownloadService
from app.main import create_app


def make_settings(root: Path) -> Settings:
    return Settings(
        data_path=root / "data",
        library_path=root / "library",
        work_path=root / "work",
        app_secret_key="test-secret-key-with-at-least-32-characters",
        tag_translation_enabled=False,
        archive_toolchain_auto_install=False,
    )


async def seed_archive(database: Database, *, file_name: str) -> int:
    await database.initialize()
    await database.configure_telegram_source(
        source_type="CHANNEL",
        chat_id=-100123,
        display_name="Archive Channel",
        enabled=True,
        allowed_archive_formats=("zip", "rar", "7z", "cbz"),
        max_attachment_size_mb=0,
    )
    await database.save_telegram_updates(
        [
            {
                "update_id": 100,
                "channel_post": {
                    "message_id": 1,
                    "date": 1_700_000_600,
                    "chat": {"id": -100123, "title": "Archive Channel"},
                    "caption": "Archive",
                    "document": {
                        "file_id": "tg-file-id",
                        "file_unique_id": "tg-file-unique",
                        "file_name": file_name,
                        "mime_type": "application/zip",
                        "file_size": 4096,
                    },
                },
            }
        ]
    )
    await CandidateIngestor(database).process_pending_updates()
    candidates = await database.list_candidates()
    assert candidates, "expected candidate"
    return candidates[0].candidate_id


@pytest.mark.asyncio
async def test_enqueue_requires_approved_candidate(tmp_path: Path) -> None:
    database = Database(tmp_path / "ehbot.db")
    candidate_id = await seed_archive(database, file_name="comic.cbz")
    service = DownloadService(database, tmp_path / "work")
    with pytest.raises(DownloadError):
        await service.enqueue_telegram_download(
            candidate_id,
            {"file_id": "x", "file_name": "x.cbz"},
        )


@pytest.mark.asyncio
async def test_enqueue_after_approval_is_idempotent(tmp_path: Path) -> None:
    database = Database(tmp_path / "ehbot.db")
    candidate_id = await seed_archive(database, file_name="comic.cbz")
    service = DownloadService(database, tmp_path / "work")
    # Mark the candidate approved for the test
    with database._connect() as connection:  # noqa: SLF001
        connection.execute(
            "UPDATE candidates SET status = 'APPROVED' WHERE id = ?",
            (candidate_id,),
        )
    first = await service.enqueue_telegram_download(
        candidate_id,
        {"file_id": "x", "file_name": "x.cbz"},
    )
    second = await service.enqueue_telegram_download(
        candidate_id,
        {"file_id": "x", "file_name": "x.cbz"},
    )
    assert first.created is True
    assert second.created is False
    assert first.job_id == second.job_id


@pytest.mark.asyncio
async def test_worker_failure_updates_candidate_status(tmp_path: Path) -> None:
    database = Database(tmp_path / "ehbot.db")
    candidate_id = await seed_archive(database, file_name="comic.cbz")
    with database._connect() as connection:  # noqa: SLF001
        connection.execute(
            "UPDATE candidates SET status = 'APPROVED' WHERE id = ?",
            (candidate_id,),
        )
    service = DownloadService(database, tmp_path / "work")
    await service.enqueue_telegram_download(
        candidate_id,
        {"file_id": "x", "file_name": "x.cbz"},
    )

    assert await service._process_one() is True  # noqa: SLF001

    candidate = await database.get_candidate(candidate_id)
    assert candidate is not None
    assert candidate.status == "FAILED"


@pytest.mark.asyncio
async def test_enqueue_rejects_non_archive_attachment(tmp_path: Path) -> None:
    database = Database(tmp_path / "ehbot.db")
    candidate_id = await seed_archive(database, file_name="comic.cbz")
    with database._connect() as connection:  # noqa: SLF001
        connection.execute(
            "UPDATE candidates SET status = 'APPROVED' WHERE id = ?",
            (candidate_id,),
        )
    service = DownloadService(database, tmp_path / "work")
    with pytest.raises(DownloadError):
        await service.enqueue_telegram_download(
            candidate_id,
            {"file_id": ""},
        )


class _TelegramTransport(httpx.MockTransport):
    def __init__(self, payload: bytes) -> None:
        super().__init__(self._handler)
        self._payload = payload

    async def _handler(self, request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/getFile"):
            return httpx.Response(
                200,
                json={
                    "ok": True,
                    "result": {
                        "file_id": "tg-file-id",
                        "file_unique_id": "tg-file-unique",
                        "file_path": "documents/tg-file-id",
                        "file_size": len(self._payload),
                    },
                },
            )
        if request.url.path.endswith("/getMe"):
            return httpx.Response(
                200,
                json={
                    "ok": True,
                    "result": {
                        "id": 1,
                        "username": "ehbot_test",
                        "first_name": "EhBot Test",
                    },
                },
            )
        if "/file/bot" in request.url.path:
            return httpx.Response(200, content=self._payload)
        return httpx.Response(200, json={"ok": True, "result": []})


def test_full_download_workflow_writes_artifact(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)

    async def seed() -> int:
        database = Database(settings.data_path / "ehbot.db")
        candidate_id = await seed_archive(database, file_name="comic.cbz")
        with database._connect() as connection:  # noqa: SLF001
            connection.execute(
                "UPDATE candidates SET status = 'APPROVED' WHERE id = ?",
                (candidate_id,),
            )
        return candidate_id

    candidate_id = asyncio.run(seed())
    payload = b"PK" + b"\x00" * 4094

    with TestClient(
        create_app(
            settings,
            telegram_transport=_TelegramTransport(payload),
        ),
        follow_redirects=False,
    ) as client:
        # Authenticate
        bootstrap = (
            settings.data_path / "bootstrap_admin_password"
        ).read_text(encoding="utf-8")
        page = client.get("/login")
        client.post(
            "/login",
            data={
                "password": bootstrap,
                "csrf_token": page.context["csrf_token"],
            },
        )
        page = client.get("/settings/passwords")
        client.post(
            "/change-password",
            data={
                "current_password": bootstrap,
                "new_password": "new-password-with-12-characters",
                "confirmation": "new-password-with-12-characters",
                "csrf_token": page.context["csrf_token"],
            },
        )
        # Connect Telegram bot
        page = client.get("/settings/connections")
        client.post(
            "/connections/telegram",
            data={"csrf_token": page.context["csrf_token"], "bot_token": "test"},
        )
        # Trigger download
        page = client.get(f"/works/{candidate_id}")
        response = client.post(
            f"/candidates/{candidate_id}/download",
            data={"csrf_token": page.context["csrf_token"]},
        )
        assert response.status_code == 303

        # Wait for worker
        asyncio.run(asyncio.sleep(2.0))

        database = Database(settings.data_path / "ehbot.db")
        service = DownloadService(database, settings.work_path)
        jobs = asyncio.run(service.list_jobs_for_candidate(candidate_id))
        assert jobs
        job = jobs[0]
        assert job.state == DOWNLOAD_STATE_COMPLETED
        assert job.artifact_path
        path = Path(job.artifact_path)
        assert path.exists()
        assert hashlib.sha256(path.read_bytes()).hexdigest() == (
            hashlib.sha256(payload).hexdigest()
        )
        candidate = asyncio.run(database.get_candidate(candidate_id))
        assert candidate is not None
        assert candidate.status == "DOWNLOADED"


def test_downloads_dashboard_renders(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)

    async def seed() -> None:
        database = Database(settings.data_path / "ehbot.db")
        await database.initialize()

    asyncio.run(seed())

    with TestClient(create_app(settings)) as client:
        bootstrap = (
            settings.data_path / "bootstrap_admin_password"
        ).read_text(encoding="utf-8")
        page = client.get("/login")
        client.post(
            "/login",
            data={
                "password": bootstrap,
                "csrf_token": page.context["csrf_token"],
            },
        )
        page = client.get("/settings/passwords")
        client.post(
            "/change-password",
            data={
                "current_password": bootstrap,
                "new_password": "new-password-with-12-characters",
                "confirmation": "new-password-with-12-characters",
                "csrf_token": page.context["csrf_token"],
            },
        )
        response = client.get("/downloads")
        assert response.status_code == 200
        assert "下载任务" in response.text


async def approved_job(database: Database, service: DownloadService) -> tuple[int, int]:
    """Seed an approved candidate with one queued Telegram job."""
    candidate_id = await seed_archive(database, file_name="comic.cbz")
    with database._connect() as connection:  # noqa: SLF001
        connection.execute(
            "UPDATE candidates SET status = 'APPROVED' WHERE id = ?",
            (candidate_id,),
        )
    result = await service.enqueue_telegram_download(
        candidate_id,
        {"file_id": "x", "file_name": "x.cbz"},
    )
    return candidate_id, result.job_id


def job_state(database: Database, job_id: int) -> str:
    with database._connect() as connection:  # noqa: SLF001
        row = connection.execute(
            "SELECT state FROM download_jobs WHERE id = ?", (job_id,)
        ).fetchone()
    return str(row[0])


@pytest.mark.asyncio
async def test_retry_requeues_a_failed_job_without_duplicating_it(
    tmp_path: Path,
) -> None:
    database = Database(tmp_path / "ehbot.db")
    service = DownloadService(database, tmp_path / "work")
    candidate_id, job_id = await approved_job(database, service)

    assert await service._process_one() is True  # noqa: SLF001
    assert job_state(database, job_id) == DOWNLOAD_STATE_FAILED

    assert await service.retry_job(job_id) == DOWNLOAD_STATE_PENDING
    assert job_state(database, job_id) == DOWNLOAD_STATE_PENDING

    with database._connect() as connection:  # noqa: SLF001
        count = connection.execute(
            "SELECT COUNT(*) FROM download_jobs WHERE candidate_id = ?",
            (candidate_id,),
        ).fetchone()[0]
    assert count == 1

    candidate = await database.get_candidate(candidate_id)
    assert candidate is not None
    assert candidate.status == "APPROVED"


@pytest.mark.asyncio
async def test_retry_is_refused_when_the_failure_is_permanent(
    tmp_path: Path,
) -> None:
    """A file over the Bot API size ceiling can never succeed on a retry."""
    database = Database(tmp_path / "ehbot.db")
    service = DownloadService(database, tmp_path / "work")
    _, job_id = await approved_job(database, service)
    with database._connect() as connection:  # noqa: SLF001
        connection.execute(
            "UPDATE download_jobs SET state = ?, error_code = ? WHERE id = ?",
            (DOWNLOAD_STATE_FAILED, "TELEGRAM_FILE_TOO_BIG", job_id),
        )

    with pytest.raises(DownloadError) as caught:
        await service.retry_job(job_id)
    assert caught.value.code == "JOB_PERMANENTLY_FAILED"
    assert job_state(database, job_id) == DOWNLOAD_STATE_FAILED


@pytest.mark.asyncio
async def test_paused_job_is_skipped_by_the_worker_until_resumed(
    tmp_path: Path,
) -> None:
    database = Database(tmp_path / "ehbot.db")
    service = DownloadService(database, tmp_path / "work")
    _, job_id = await approved_job(database, service)

    assert await service.pause_job(job_id) == DOWNLOAD_STATE_PAUSED
    assert await service._process_one() is False  # noqa: SLF001
    assert job_state(database, job_id) == DOWNLOAD_STATE_PAUSED

    assert await service.resume_job(job_id) == DOWNLOAD_STATE_PENDING
    assert await service._process_one() is True  # noqa: SLF001


@pytest.mark.asyncio
async def test_running_job_cannot_be_paused(tmp_path: Path) -> None:
    database = Database(tmp_path / "ehbot.db")
    service = DownloadService(database, tmp_path / "work")
    _, job_id = await approved_job(database, service)
    with database._connect() as connection:  # noqa: SLF001
        connection.execute(
            "UPDATE download_jobs SET state = 'DOWNLOADING' WHERE id = ?",
            (job_id,),
        )

    with pytest.raises(DownloadError) as caught:
        await service.pause_job(job_id)
    assert caught.value.code == "JOB_NOT_PAUSABLE"


@pytest.mark.asyncio
async def test_cancel_releases_the_candidate_back_to_review(
    tmp_path: Path,
) -> None:
    database = Database(tmp_path / "ehbot.db")
    service = DownloadService(database, tmp_path / "work")
    candidate_id, job_id = await approved_job(database, service)

    assert await service.cancel_job(job_id) == DOWNLOAD_STATE_CANCELLED
    assert job_state(database, job_id) == DOWNLOAD_STATE_CANCELLED
    assert await service._process_one() is False  # noqa: SLF001

    candidate = await database.get_candidate(candidate_id)
    assert candidate is not None
    assert candidate.status == "PENDING_REVIEW"


@pytest.mark.asyncio
async def test_completed_job_cannot_be_cancelled(tmp_path: Path) -> None:
    database = Database(tmp_path / "ehbot.db")
    service = DownloadService(database, tmp_path / "work")
    _, job_id = await approved_job(database, service)
    with database._connect() as connection:  # noqa: SLF001
        connection.execute(
            "UPDATE download_jobs SET state = ? WHERE id = ?",
            (DOWNLOAD_STATE_COMPLETED, job_id),
        )

    with pytest.raises(DownloadError) as caught:
        await service.cancel_job(job_id)
    assert caught.value.code == "JOB_ALREADY_COMPLETED"


@pytest.mark.asyncio
async def test_failed_job_stays_visible_on_the_dashboard(tmp_path: Path) -> None:
    """A failed job must remain listed, because that is where it is retried."""
    database = Database(tmp_path / "ehbot.db")
    service = DownloadService(database, tmp_path / "work")
    _, job_id = await approved_job(database, service)
    assert await service._process_one() is True  # noqa: SLF001

    listed = await service.list_active_jobs()
    assert [job.job_id for job in listed] == [job_id]
    assert listed[0].state == DOWNLOAD_STATE_FAILED
    assert listed[0].is_retryable is True
    assert listed[0].is_cancellable is True
    assert listed[0].is_pausable is False


def enqueue_raw(
    database: Database, candidate_id: int, key: str, priority: int
) -> int:
    """Insert a queued Telegram job directly, so only the ordering is tested."""
    with database._connect() as connection:  # noqa: SLF001
        cursor = connection.execute(
            "INSERT INTO download_jobs (candidate_id, idempotency_key, "
            "provider, state, priority, details_json) "
            "VALUES (?, ?, 'TELEGRAM', ?, ?, '{}')",
            (candidate_id, key, DOWNLOAD_STATE_PENDING, priority),
        )
    return int(cursor.lastrowid)


@pytest.mark.asyncio
async def test_the_queue_is_fifo_within_one_priority(tmp_path: Path) -> None:
    """Default priority must leave the existing behaviour untouched.

    Every job before this column existed is now priority 100, so if the sort
    were on `priority` alone the queue order would become arbitrary.
    """
    database = Database(tmp_path / "ehbot.db")
    candidate_id = await seed_archive(database, file_name="comic.cbz")
    service = DownloadService(database, tmp_path / "work")
    first = enqueue_raw(database, candidate_id, "a", 100)
    second = enqueue_raw(database, candidate_id, "b", 100)

    claimed = [
        service._claim_pending_job_sync()["job_id"]  # noqa: SLF001
        for _ in range(2)
    ]

    assert claimed == [first, second]


@pytest.mark.asyncio
async def test_a_lower_priority_value_is_claimed_first(tmp_path: Path) -> None:
    """Promotion reorders one job and nothing else.

    The urgent job was enqueued last, so an `id`-only sort would have run it
    third; the two default-priority jobs must still come out in FIFO order
    behind it.
    """
    database = Database(tmp_path / "ehbot.db")
    candidate_id = await seed_archive(database, file_name="comic.cbz")
    service = DownloadService(database, tmp_path / "work")
    normal = enqueue_raw(database, candidate_id, "a", 100)
    later = enqueue_raw(database, candidate_id, "b", 100)
    urgent = enqueue_raw(database, candidate_id, "c", 10)

    claimed = [
        service._claim_pending_job_sync()["job_id"]  # noqa: SLF001
        for _ in range(3)
    ]

    assert claimed == [urgent, normal, later]
    assert service._claim_pending_job_sync() is None  # noqa: SLF001
