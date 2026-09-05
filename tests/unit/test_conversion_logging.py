import asyncio
import logging
from pathlib import Path

import pytest

from app.archive.errors import ArchiveError, ArchiveVolumesMissing
from app.conversion.service import ConversionService
from app.conversion.naming import LibraryPathError
from app.db.database import Database



async def _make_service(tmp_path: Path) -> ConversionService:
    database = Database(tmp_path / "ehbot.db")
    await database.initialize()
    return ConversionService(
        database,
        tmp_path / "work",
        tmp_path / "library",
        data_path=tmp_path / "data",
    )


async def _seed_pending_job(database: Database, tmp_path: Path) -> tuple[int, int]:
    """Seed a candidate, a completed download row with an ARCHIVE artifact,
    and a CONVERSION_PENDING job. The artifact path is real so the worker
    passes the early-return guard in `_handle_job`."""
    def _seed() -> tuple[int, int]:
        artifact_path = tmp_path / "src.zip"
        artifact_path.write_bytes(b"x")
        with database._connect() as conn:
            cur = conn.execute(
                "INSERT INTO candidates (status, ex_gid, created_at, updated_at) "
                "VALUES ('APPROVED', 1, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
            )
            candidate_id = int(cur.lastrowid)
            cur = conn.execute(
                "INSERT INTO download_jobs "
                "(candidate_id, provider, state, priority, attempt_count, "
                "idempotency_key, details_json, created_at, updated_at) "
                "VALUES (?, 'EXHENTAI', 'COMPLETED', 100, 0, "
                "?, '{}', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)",
                (candidate_id, f"download-{candidate_id}"),
            )
            download_job_id = int(cur.lastrowid)
            conn.execute(
                "INSERT INTO artifacts "
                "(job_id, artifact_type, path, created_at) "
                "VALUES (?, 'ARCHIVE', ?, CURRENT_TIMESTAMP)",
                (download_job_id, str(artifact_path)),
            )
            cur = conn.execute(
                "INSERT INTO download_jobs "
                "(candidate_id, provider, state, priority, attempt_count, "
                "idempotency_key, details_json, created_at, updated_at) "
                "VALUES (?, 'CONVERSION', 'CONVERSION_PENDING', 100, 0, "
                "?, '{}', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)",
                (candidate_id, f"convert-{candidate_id}"),
            )
            return int(cur.lastrowid), candidate_id
    return await asyncio.to_thread(_seed)


async def _state_of(database: Database, job_id: int) -> str:
    def _read() -> str:
        with database._connect() as conn:
            row = conn.execute(
                "SELECT state FROM download_jobs WHERE id = ?", (job_id,)
            ).fetchone()
        return str(row[0])
    return await asyncio.to_thread(_read)


async def _set_state(database: Database, job_id: int, state: str) -> None:
    """Put a row in one state, connecting inside the worker thread.

    Separate from `_force_state` because that one opens the connection on the
    event loop and hands `.execute` to a thread, which sqlite3 refuses -- a
    connection may only be used by the thread that created it.
    """
    def _write() -> None:
        with database._connect() as conn:
            conn.execute(
                "UPDATE download_jobs SET state = ?, "
                "updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                (state, job_id),
            )
    await asyncio.to_thread(_write)


async def _force_state(database: Database, job_id: int, state: str,
                       code: str | None = None, message: str | None = None) -> None:
    """Transition a row to a terminal state directly, bypassing _handle_job.

    Tests use this to set up the post-`_handle_job` world; `_handle_job` is
    then stubbed to a no-op so the worker observes the seeded state.
    """
    await asyncio.to_thread(
        database._connect().execute,
        "UPDATE download_jobs SET state = ?, error_code = ?, error_message = ?, "
        "updated_at = CURRENT_TIMESTAMP WHERE id = ?",
        (state, code, message, job_id),
    )


@pytest.mark.asyncio
async def test_process_one_logs_completed(tmp_path, caplog):
    service = await _make_service(tmp_path)
    job_id, _ = await _seed_pending_job(service._database, tmp_path)
    async def fake_handle(job):
        await asyncio.to_thread(service._mark_completed_sync, job["job_id"], {"k": "v"})
    service._handle_job = fake_handle

    with caplog.at_level(logging.DEBUG, logger="app.conversion.service"):
        assert await service._process_one() is True

    events = [r.message for r in caplog.records if r.name == "app.conversion.service"]
    assert "conversion_job_claimed" in events
    assert "conversion_job_completed" in events
    assert "conversion_job_finished" not in events  # the misleading line


@pytest.mark.asyncio
async def test_process_one_logs_parked_for_waiting_path(tmp_path, caplog):
    service = await _make_service(tmp_path)
    job_id, _ = await _seed_pending_job(service._database, tmp_path)
    async def fake_handle(job):
        await asyncio.to_thread(
            service._mark_waiting_sync, job["job_id"],
            "CONVERSION_WAITING_PATH", "BAD_PATH", "contains illegal char", {}
        )
    service._handle_job = fake_handle

    with caplog.at_level(logging.DEBUG, logger="app.conversion.service"):
        assert await service._process_one() is True

    parked = [r for r in caplog.records if r.message == "conversion_job_parked"]
    assert parked, "expected a parked log line"
    record = parked[0]
    assert record.levelno == logging.INFO
    assert getattr(record, "status", None) == "CONVERSION_WAITING_PATH"
    assert getattr(record, "error_code", None) == "BAD_PATH"
    assert "conversion_job_finished" not in {r.message for r in caplog.records}


@pytest.mark.asyncio
async def test_process_one_logs_failed(tmp_path, caplog):
    service = await _make_service(tmp_path)
    job_id, _ = await _seed_pending_job(service._database, tmp_path)
    async def fake_handle(job):
        await asyncio.to_thread(
            service._mark_failed_sync, job["job_id"],
            "ARCHIVE_CORRUPT", "header is not a valid zip"
        )
    service._handle_job = fake_handle

    with caplog.at_level(logging.DEBUG, logger="app.conversion.service"):
        assert await service._process_one() is True

    failed = [r for r in caplog.records if r.message == "conversion_job_failed"]
    assert failed, "expected a failed log line"
    record = failed[0]
    assert record.levelno == logging.WARNING
    assert getattr(record, "status", None) == "CONVERSION_FAILED"
    assert getattr(record, "error_code", None) == "ARCHIVE_CORRUPT"
    assert getattr(record, "error_message", None) == "header is not a valid zip"


@pytest.mark.asyncio
async def test_handle_job_archive_error_keeps_original_traceback(tmp_path, caplog):
    """The original exception reaches the JSON payload, not just a public message.

    The user-reported bug: a packaging failure logged as `conversion_job_finished`
    with no error_code and no traceback. This test fixes the second half:
    when `_handle_job`'s ArchiveError catch fires, the original exception is
    preserved via `.exception(...)`.
    """
    service = await _make_service(tmp_path)
    job_id, candidate_id = await _seed_pending_job(service._database, tmp_path)

    # processor.process is called via asyncio.to_thread -> must be SYNC.
    class _BoomProcessor:
        def process(self, *args, **kwargs):
            raise ArchiveError("ARCHIVE_CORRUPT", "header is not a valid zip")
    async def build_processor(image_quality):
        return _BoomProcessor()
    service._build_processor = build_processor

    # _library_target succeeds (no pin -> no LibraryPathError).

    with caplog.at_level(logging.DEBUG, logger="app.conversion.service"):
        await service._handle_job({"job_id": job_id, "candidate_id": candidate_id})

    caught = [r for r in caplog.records if r.message == "conversion_archive_failed"]
    assert caught, "expected the archive-failed catch-site log line"
    assert caught[0].exc_info is not None, "original traceback must be preserved"
    tb_text = caught[0].exc_info[1].__class__.__name__
    assert tb_text == "ArchiveError"


@pytest.mark.asyncio
async def test_handle_job_pinned_path_rejection_keeps_original_traceback(
    tmp_path, caplog
):
    service = await _make_service(tmp_path)
    job_id, candidate_id = await _seed_pending_job(service._database, tmp_path)

    async def bad_target(*args, **kwargs):
        raise LibraryPathError("PATH_FORBIDDEN", "directory contains '..'")
    service._library_target = bad_target

    with caplog.at_level(logging.DEBUG, logger="app.conversion.service"):
        await service._handle_job({"job_id": job_id, "candidate_id": candidate_id})

    caught = [r for r in caplog.records if r.message == "conversion_pinned_path_rejected"]
    assert caught
    assert caught[0].exc_info is not None
    assert caught[0].exc_info[1].__class__.__name__ == "LibraryPathError"


class TestReclaimingAnInterruptedPack:
    """A `CONVERSION_RUNNING` row at startup belongs to nobody.

    There is no lease column on this queue, so what stands in for one is the
    fact that `reclaim_running_jobs` runs inside `start`, before this process's
    only conversion worker has claimed anything. Left alone, the row stayed
    `CONVERSION_RUNNING` forever and the four `WORK_PACK_RUNNING` guards refused
    remove, redownload, rename and re-path -- an interrupted pack locked the book
    out of its own detail page.
    """

    @pytest.mark.asyncio
    async def test_a_running_pack_is_requeued(self, tmp_path) -> None:
        service = await _make_service(tmp_path)
        job_id, _ = await _seed_pending_job(service._database, tmp_path)
        await _set_state(service._database, job_id, "CONVERSION_RUNNING")

        assert await service.reclaim_running_jobs() == 1

        assert await _state_of(service._database, job_id) == "CONVERSION_PENDING"
        # Requeued rather than failed: packing is idempotent, so doing it again
        # is the honest answer to 「不知道它做完了没有」.
        assert service._claim_pending_job_sync() is not None

    @pytest.mark.asyncio
    async def test_a_pending_pack_is_untouched(self, tmp_path) -> None:
        """Only the claimed state is reclaimed, so the count means something."""
        service = await _make_service(tmp_path)
        job_id, _ = await _seed_pending_job(service._database, tmp_path)

        assert await service.reclaim_running_jobs() == 0
        assert await _state_of(service._database, job_id) == "CONVERSION_PENDING"

    @pytest.mark.asyncio
    async def test_a_completed_pack_is_not_run_again(self, tmp_path) -> None:
        service = await _make_service(tmp_path)
        job_id, _ = await _seed_pending_job(service._database, tmp_path)
        await _set_state(service._database, job_id, "CONVERSION_COMPLETED")

        assert await service.reclaim_running_jobs() == 0
        assert await _state_of(service._database, job_id) == "CONVERSION_COMPLETED"

    @pytest.mark.asyncio
    async def test_starting_the_worker_reclaims_first(self, tmp_path) -> None:
        """`start` is the only caller, so the recovery cannot be forgotten."""
        service = await _make_service(tmp_path)
        job_id, _ = await _seed_pending_job(service._database, tmp_path)
        await _set_state(service._database, job_id, "CONVERSION_RUNNING")

        await service.start()
        try:
            assert await _state_of(service._database, job_id) != "CONVERSION_RUNNING"
        finally:
            await service.stop()
