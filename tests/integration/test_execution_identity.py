"""Cancelled and replaced attempts cannot commit even ancillary metadata."""
import asyncio
import sqlite3
from threading import Event

import pytest

from app.downloads.execution import attempt_path, execution_scope, run_sync
from app.downloads.service import DownloadService
from tests.integration.test_queue_control import database, observed, pending


@pytest.mark.asyncio
@pytest.mark.parametrize("late_failure", [False, True])
async def test_cancelled_await_waits_for_actual_thread_exit(late_failure):
    entered, release, exited = Event(), Event(), Event()

    def blocking():
        entered.set()
        assert release.wait(5)
        exited.set()
        if late_failure:
            raise RuntimeError("late thread error")
        return 42

    operation = asyncio.create_task(run_sync(blocking))
    assert await asyncio.to_thread(entered.wait, 2)
    operation.cancel()
    await asyncio.sleep(0)
    assert not operation.done() and not exited.is_set()
    operation.cancel()  # A second stop request must not detach the thread.
    await asyncio.sleep(0)
    assert not operation.done()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(operation, 2)
    assert exited.is_set()


@pytest.mark.asyncio
@pytest.mark.parametrize("replacement", [False, True])
async def test_revoked_identity_rolls_back_metadata_and_late_failure(database, tmp_path, replacement):
    service = DownloadService(database, tmp_path)
    job_id = pending(database)
    first = service._claim_pending_job_sync()
    if replacement:
        service._mark_job_failed_sync(job_id, "TEMPORARY", "first attempt")
        await service.retry_job(job_id)
        second = service._claim_pending_job_sync()
        assert first["task_id"] != second["task_id"]
    else:
        await service.command(job_id, "cancel", **observed(database, job_id))
    with execution_scope(database.path, first):
        with pytest.raises(sqlite3.IntegrityError, match="QUEUE_STALE_EXECUTION"):
            attempt_path(tmp_path / "downloads" / "stale.zip")
        def stale_metadata():
            with database.connection() as c:
                c.execute("UPDATE candidates SET filter_reason = 'stale metadata' WHERE id = 1")
        with pytest.raises(sqlite3.IntegrityError, match="QUEUE_STALE_EXECUTION"):
            await run_sync(stale_metadata)
    with database.connection() as c:
        assert c.execute("SELECT filter_reason FROM candidates WHERE id = 1").fetchone()[0] != "stale metadata"
        if replacement:
            assert c.execute("SELECT state FROM download_jobs WHERE id = ?", (job_id,)).fetchone()[0] == "DOWNLOADING"


def test_priority_changes_do_not_revoke_execution_and_attempt_files_are_isolated(database, tmp_path):
    service = DownloadService(database, tmp_path)
    job_id = pending(database)
    first = service._claim_pending_job_sync()
    asyncio.run(service.command(job_id, "priority", priority=5, **observed(database, job_id)))
    destination = tmp_path / "downloads" / "archive.zip"
    with execution_scope(database.path, first):
        first_path = attempt_path(destination)
        with database.connection() as c:
            c.execute("UPDATE candidates SET filter_reason = 'current attempt' WHERE id = 1")
    service._mark_job_failed_sync(job_id, "TEMPORARY", "retry")
    asyncio.run(service.retry_job(job_id))
    second = service._claim_pending_job_sync()
    with execution_scope(database.path, second):
        second_path = attempt_path(destination)
    assert first_path != second_path
    assert first["attempt_id"] in first_path.parts
    assert second["attempt_id"] in second_path.parts
    assert attempt_path(destination) == destination
