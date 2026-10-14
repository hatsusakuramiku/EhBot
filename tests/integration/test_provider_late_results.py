"""Original audit triggers use the real provider persistence, not a replacement stub."""
import asyncio
import sqlite3

import pytest

from app.downloads.archived import ArchivedWorkService
from app.downloads.execution import execution_scope, run_sync
from app.downloads.queue import STALE_EXECUTION_ERRORS
from app.downloads.service import DownloadService
from app.exhentai.service import ExHentaiService
from app.torrent.models import TorrentDelivery
from app.torrent.service import TorrentService
from tests.integration.test_queue_control import database, observed, pending


@pytest.mark.asyncio
@pytest.mark.parametrize("remove", [False, True])
async def test_late_eh_persistence_cannot_finish_or_recreate_revoked_job(database, tmp_path, remove):
    entered, cancelling, release = (asyncio.Event() for _ in range(3))
    payload = tmp_path / "existing.zip"
    payload.write_bytes(b"controlled archive bytes")
    provider = ExHentaiService(database, tmp_path, tmp_path, credentials_provider=None)
    rejected = []
    async def transfer(candidate_id):
        entered.set()
        try:
            await release.wait()
        except asyncio.CancelledError:
            # Model a response which arrives while cancellation is draining.
            cancelling.set()
            await release.wait()
        try:
            await run_sync(provider._record_artifact_sync, candidate_id, payload, payload.stat().st_size,
                           "https://example.invalid/archive")
        except sqlite3.IntegrityError as error:
            rejected.append(str(error))
            raise
    archive = ArchivedWorkService(database, tmp_path, tmp_path)
    service = DownloadService(database, tmp_path, exhentai_download=transfer,
                              cleanup_removals=archive.process_removals)
    job_id = pending(database, "EXHENTAI")
    worker = asyncio.create_task(service._process_one())
    await asyncio.wait_for(entered.wait(), 2)
    draft = observed(database, job_id)
    if remove:
        result = await archive.remove_work(1)
        assert result["removed_jobs"] == 1
    else:
        await service.command(job_id, "cancel", **draft)
    stopping = asyncio.create_task(service.process_operations())
    await asyncio.wait_for(cancelling.wait(), 2)
    try:
        assert not stopping.done()
    finally:
        release.set()
        await asyncio.gather(worker, stopping)
    assert stopping.result() == 1 and worker.result() is True
    assert len(rejected) == 1 and rejected[0] in STALE_EXECUTION_ERRORS
    with database.connection() as c:
        assert c.execute("SELECT count(*) FROM artifacts").fetchone()[0] == 0
        assert c.execute("SELECT count(*) FROM metadata_values").fetchone()[0] == 0
        if remove:
            assert c.execute("SELECT count(*) FROM download_jobs").fetchone()[0] == 0
        else:
            assert c.execute("SELECT state FROM download_jobs WHERE id = ?", (job_id,)).fetchone()[0] == "CANCELLED"


@pytest.mark.asyncio
@pytest.mark.parametrize("replacement", [False, True])
async def test_late_torrent_completion_cannot_commit_to_cancelled_or_reused_job(database, tmp_path, replacement):
    async def confirmed_stop(operation):
        return None
    archive = ArchivedWorkService(database, tmp_path, tmp_path)
    service = DownloadService(database, tmp_path, torrent_stop=confirmed_stop,
                              cleanup_removals=archive.process_removals)
    job_id = pending(database, "EH_TORRENT")
    original = service._claim_pending_job_sync()
    service._park_job_sync(job_id, {"hash": "a" * 40})
    if replacement:
        await archive.remove_work(1)
        assert await service.process_operations() == 1
        new_job = await service.enqueue_exhentai_download(1)
        assert new_job.job_id == job_id and new_job.task_id != original["task_id"]
        assert service._claim_pending_job_sync()["task_id"] == new_job.task_id
        expected_state = "DOWNLOADING"
    else:
        await service.command(job_id, "cancel", **observed(database, job_id))
        expected_state = "STOPPING"
    before = await service.get_task(observed(database, job_id)["task_id"])
    payload = tmp_path / "late-delivery.zip"
    payload.write_bytes(b"controlled delivery bytes")
    provider = TorrentService(database, tmp_path, config_provider=None, credentials_provider=None)
    with execution_scope(database.path, original):
        with pytest.raises(sqlite3.IntegrityError) as error:
            await run_sync(provider._complete_sync, job_id, 1,
                           TorrentDelivery(str(payload), payload.stat().st_size, False), {})
    assert str(error.value) in STALE_EXECUTION_ERRORS
    assert await service.get_task(before["task_id"]) == before
    with database.connection() as c:
        assert c.execute("SELECT state FROM download_jobs WHERE id = ?", (job_id,)).fetchone()[0] == expected_state
        assert c.execute("SELECT count(*) FROM artifacts").fetchone()[0] == 0
        assert c.execute("SELECT count(*) FROM metadata_values").fetchone()[0] == 0
        assert c.execute("SELECT status FROM candidates WHERE id = 1").fetchone()[0] != "DOWNLOADED"
