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
    DOWNLOAD_STATE_COMPLETED,
    DOWNLOAD_STATE_FAILED,
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
        page = client.get("/change-password")
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
        page = client.get("/connections")
        client.post(
            "/connections/telegram",
            data={"csrf_token": page.context["csrf_token"], "bot_token": "test"},
        )
        # Trigger download
        page = client.get(f"/candidates/{candidate_id}")
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
        page = client.get("/change-password")
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
        assert "活跃下载任务" in response.text