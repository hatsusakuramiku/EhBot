import asyncio
import logging
from pathlib import Path

import pytest

from app.db.database import Database
from app.torrent.models import TorrentError
from app.torrent.service import PROVIDER_NAME, TorrentService


class _StubConfig:
    async def __call__(self):
        return None


class _StubCredentials:
    async def __call__(self):
        return None


def _make_service(database: Database, tmp_path: Path) -> TorrentService:
    return TorrentService(
        database=database,
        work_path=tmp_path / "work",
        config_provider=_StubConfig(),
        credentials_provider=_StubCredentials(),
    )


async def _seed_waiting_torrent(database: Database) -> tuple[int, int]:
    def _seed() -> tuple[int, int]:
        with database._connect() as conn:
            cur = conn.execute(
                "INSERT INTO candidates (status, ex_gid, created_at, updated_at) "
                "VALUES ('DOWNLOADED', 1, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
            )
            candidate_id = int(cur.lastrowid)
            cur = conn.execute(
                "INSERT INTO download_jobs "
                "(candidate_id, provider, state, priority, attempt_count, "
                "idempotency_key, details_json, created_at, updated_at) "
                "VALUES (?, ?, 'WAITING_TORRENT', 100, 0, ?, '{}', "
                "CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)",
                (candidate_id, PROVIDER_NAME, f"torrent-{candidate_id}"),
            )
            return int(cur.lastrowid), candidate_id
    return await asyncio.to_thread(_seed)


@pytest.mark.asyncio
async def test_torrent_poller_logs_provider_error(tmp_path, caplog):
    database = Database(tmp_path / "ehbot.db")
    await database.initialize()
    service = _make_service(database, tmp_path)
    job_id, _ = await _seed_waiting_torrent(database)

    async def boom(job):
        raise TorrentError("TORRENT_VANISHED", "qBittorrent has no such torrent")
    service._advance_job = boom

    with caplog.at_level(logging.DEBUG, logger="app.torrent.service"):
        await service.poll_once()

    failed = [r for r in caplog.records if r.message == "torrent_job_failed"]
    assert failed
    record = failed[0]
    assert record.levelno == logging.WARNING
    assert getattr(record, "error_code", None) == "TORRENT_VANISHED"
    assert "qBittorrent" in getattr(record, "error_message", "")

    # The job is marked FAILED.
    with database._connect() as conn:
        state, code = conn.execute(
            "SELECT state, error_code FROM download_jobs WHERE id = ?",
            (job_id,),
        ).fetchone()
    assert state == "FAILED"
    assert code == "TORRENT_VANISHED"


@pytest.mark.asyncio
async def test_torrent_poller_keeps_original_traceback_on_unexpected_error(
    tmp_path, caplog
):
    database = Database(tmp_path / "ehbot.db")
    await database.initialize()
    service = _make_service(database, tmp_path)
    job_id, _ = await _seed_waiting_torrent(database)

    # Raise an exception that is not a TorrentError so the catch-all branch
    # in `poll_once` is the one that fires -- the one that calls
    # `logger.exception(...)` and preserves the traceback.
    async def boom(job):
        raise RuntimeError("connection reset")
    service._advance_job = boom

    with caplog.at_level(logging.DEBUG, logger="app.torrent.service"):
        await service.poll_once()

    caught = [r for r in caplog.records if r.message == "torrent_job_exception"]
    assert caught, "expected the catch-all exception log line"
    record = caught[0]
    assert record.exc_info is not None, "original traceback must be preserved"
    assert record.exc_info[1].__class__.__name__ == "RuntimeError"
    assert "connection reset" in str(record.exc_info[1])
