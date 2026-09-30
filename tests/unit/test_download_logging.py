import asyncio
import json
import logging
from pathlib import Path

import pytest

from app.connections.telegram_user import TelegramUserError
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


async def _seed_telegram_user_job(service: DownloadService) -> int:
    """Seed an approved candidate and a PENDING Telegram-user job."""
    def _seed() -> int:
        with service._database._connect() as conn:
            cur = conn.execute(
                "INSERT INTO candidates (status, ex_gid, created_at, updated_at) "
                "VALUES ('APPROVED', 2, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
            )
            candidate_id = int(cur.lastrowid)
            cur = conn.execute(
                "INSERT INTO download_jobs "
                "(candidate_id, provider, state, priority, attempt_count, "
                "idempotency_key, details_json, created_at, updated_at) "
                "VALUES (?, 'TELEGRAM_USER', 'PENDING', 100, 0, ?, ?, "
                "CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)",
                (
                    candidate_id,
                    f"telegram-user-{candidate_id}",
                    json.dumps(
                        {
                            "chat_id": -100123,
                            "message_id": 5001,
                            "file_name": "big.zip",
                        }
                    ),
                ),
            )
            return int(cur.lastrowid)
    return await asyncio.to_thread(_seed)


class _RefusingUserClient:
    """An MTProto client whose download fails the way the operator's did."""

    def __init__(self, cause: Exception) -> None:
        self._cause = cause

    async def download_message_media(self, chat_id, message_id, destination):
        raise TelegramUserError(
            "TELEGRAM_USER_FAILED", "用户账户操作失败，请稍后重试"
        ) from self._cause


@pytest.mark.asyncio
async def test_download_failure_logs_the_untranslated_cause(tmp_path, caplog):
    """A catch-all refusal has to name what it was translated from.

    The operator's only evidence for a large-file download that failed every
    time was `error_code=TELEGRAM_USER_FAILED` with「请稍后重试」-- which names
    nothing at all. The refusal came from `_translate`'s catch-all and the
    exception behind it (`TypeError: download_media() got an unexpected keyword
    argument 'part_size_kb'`) lived only in the `__cause__` chain. The terminal
    line is the one place a finished job is reported, so the cause travels back
    from `_handle_job` into it -- the same fix R42 made for ingest.
    """
    database = Database(tmp_path / "ehbot.db")
    await database.initialize()
    cause = TypeError(
        "download_media() got an unexpected keyword argument 'part_size_kb'"
    )

    async def _client():
        return _RefusingUserClient(cause)

    service = DownloadService(
        database, tmp_path / "work", telegram_user_client=_client
    )
    await _seed_telegram_user_job(service)

    with caplog.at_level(logging.DEBUG, logger="app.downloads.service"):
        assert await service._process_one() is True

    failed = [r for r in caplog.records if r.message == "download_job_failed"]
    assert failed
    assert failed[0].error_code == "TELEGRAM_USER_FAILED"
    assert failed[0].error_detail == (
        "TypeError: download_media() got an unexpected keyword "
        "argument 'part_size_kb'"
    )


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
