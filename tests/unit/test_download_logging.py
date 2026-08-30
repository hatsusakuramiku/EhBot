import asyncio
import logging
from pathlib import Path

import pytest

from app.db.database import Database
from app.downloads.models import (
    DOWNLOAD_STATE_CANCELLED,
    DOWNLOAD_STATE_COMPLETED,
    DOWNLOAD_STATE_FAILED,
    DOWNLOAD_STATE_WAITING_TORRENT,
)
from app.downloads.service import DownloadService


async def _make_service(tmp_path: Path) -> DownloadService:
    database = Database(tmp_path / "ehbot.db")
    await database.initialize()
    return DownloadService(database, tmp_path / "work")


async def _seed_pending_download(tmp_path: Path, service: DownloadService) -> int:
    """Seed an approved candidate and a PENDING download job, return job_id."""
    def _seed() -> int:
        with service._database._connect() as conn:
            cur = conn.execute(
                "INSERT INTO candidates (status, ex_gid, created_at, updated_at) "
                "VALUES ('APPROVED', 1, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
            )
            candidate_id = int(cur.lastrowid)
            cur = conn.execute(
                "INSERT INTO download_jobs "
                "(candidate_id, provider, state, priority, attempt_count, "
                "idempotency_key, details_json, created_at, updated_at) "
                "VALUES (?, 'TELEGRAM', 'PENDING', 100, 0, ?, '{}', "
                "CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)",
                (candidate_id, f"telegram-{candidate_id}"),
            )
            return int(cur.lastrowid)
    return await asyncio.to_thread(_seed)


@pytest.mark.asyncio
async def test_download_process_one_logs_completed(tmp_path, caplog):
    service = await _make_service(tmp_path)
    job_id = await _seed_pending_download(tmp_path, service)
    async def fake_handle(job):
        await asyncio.to_thread(service._mark_job_completed_sync, job["job_id"])
    service._handle_job = fake_handle

    with caplog.at_level(logging.DEBUG, logger="app.downloads.service"):
        assert await service._process_one() is True

    events = {r.message for r in caplog.records if r.name == "app.downloads.service"}
    assert "download_job_claimed" in events
    assert "download_job_completed" in events
    assert "download_job_finished" not in events


@pytest.mark.asyncio
async def test_download_process_one_logs_waiting_torrent(tmp_path, caplog):
    service = await _make_service(tmp_path)
    job_id = await _seed_pending_download(tmp_path, service)
    async def fake_handle(job):
        await asyncio.to_thread(
            service._park_job_sync, job["job_id"], {"hash": "abc"}
        )
    service._handle_job = fake_handle

    with caplog.at_level(logging.DEBUG, logger="app.downloads.service"):
        assert await service._process_one() is True

    waiting = [r for r in caplog.records if r.message == "download_job_waiting_torrent"]
    assert waiting
    assert waiting[0].levelno == logging.INFO
    assert getattr(waiting[0], "status", None) == DOWNLOAD_STATE_WAITING_TORRENT


@pytest.mark.asyncio
async def test_download_process_one_logs_failed(tmp_path, caplog):
    service = await _make_service(tmp_path)
    job_id = await _seed_pending_download(tmp_path, service)
    async def fake_handle(job):
        await asyncio.to_thread(
            service._mark_job_failed_sync, job["job_id"],
            "TELEGRAM_FILE_TOO_BIG", "File exceeds 20 MB"
        )
    service._handle_job = fake_handle

    with caplog.at_level(logging.DEBUG, logger="app.downloads.service"):
        assert await service._process_one() is True

    failed = [r for r in caplog.records if r.message == "download_job_failed"]
    assert failed
    assert failed[0].levelno == logging.WARNING
    assert getattr(failed[0], "error_code", None) == "TELEGRAM_FILE_TOO_BIG"
    assert getattr(failed[0], "error_message", None) == "File exceeds 20 MB"


@pytest.mark.asyncio
async def test_download_process_one_logs_cancelled(tmp_path, caplog):
    service = await _make_service(tmp_path)
    job_id = await _seed_pending_download(tmp_path, service)
    async def fake_handle(job):
        def _mark():
            with service._database._connect() as conn:
                conn.execute(
                    "UPDATE download_jobs SET state = ?, "
                    "updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                    (DOWNLOAD_STATE_CANCELLED, job["job_id"]),
                )
        await asyncio.to_thread(_mark)
    service._handle_job = fake_handle

    with caplog.at_level(logging.DEBUG, logger="app.downloads.service"):
        assert await service._process_one() is True

    cancelled = [r for r in caplog.records if r.message == "download_job_cancelled"]
    assert cancelled
    assert cancelled[0].levelno == logging.INFO
