"""Admission limits include unresolved pushes and real cancellation cleanup."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest

from app.downloads.execution import Execution
from app.downloads.service import DownloadService
from app.downloads.worker import WorkerSession
from app.conversion.service import ConversionService
from app.torrent.service import TorrentService
from tests.integration.test_account_lifecycle import pending
from tests.integration.test_artifact_pipeline import prepare


@pytest.mark.asyncio
@pytest.mark.parametrize('kind', ['DOWNLOAD', 'PACK'])
async def test_concurrent_claimers_share_one_local_slot(tmp_path, kind):
    db, _, _ = await prepare(tmp_path)
    with db.connection() as c:
        c.execute("INSERT INTO candidates(id, status) VALUES(2, 'APPROVED'), (3, 'APPROVED')")
        for work_id in (2, 3):
            c.execute('INSERT INTO download_jobs(candidate_id, idempotency_key, provider, state, details_json) VALUES(?, ?, ?, ?, ?)',
                (work_id, f'fixture-{work_id}', 'TELEGRAM' if kind == 'DOWNLOAD' else 'CONVERSION',
                 'PENDING' if kind == 'DOWNLOAD' else 'CONVERSION_PENDING', '{"refile":true}'))
    service = DownloadService(db, tmp_path / 'work') if kind == 'DOWNLOAD' else ConversionService(db, tmp_path / 'work', tmp_path / 'library')
    with ThreadPoolExecutor(2) as workers:
        results = list(workers.map(lambda _: service._claim_pending_job_sync(), range(2)))
    assert sum(result is not None for result in results) == 1


@pytest.mark.asyncio
async def test_unconfirmed_remote_holds_budget_and_other_source_can_run(tmp_path):
    db, _, _ = await prepare(tmp_path)
    first = pending(db, 'EH_TORRENT', 2)
    downloads = DownloadService(db, tmp_path / 'work')
    job = downloads._claim_pending_job_sync()
    assert job['job_id'] == first
    remote = TorrentService(db, tmp_path / 'work', config_provider=None, credentials_provider=None)
    execution = Execution(db.path, job['task_id'], job['attempt_id'], job['generation'])
    remote._save_resource_sync(execution, 'https://qb.invalid', 'a' * 40, 'fixture-tag', 'UNKNOWN', 'INTENDED')
    with db.connection() as c:
        c.execute("UPDATE download_jobs SET state = 'FAILED' WHERE id = ?", (first,))
    second = pending(db, 'EH_TORRENT', 3)
    third = pending(db, 'TELEGRAM', 4)
    selected = downloads._claim_pending_job_sync()
    assert selected['job_id'] == third
    with db.connection() as c:
        c.execute("UPDATE download_jobs SET state = 'CANCELLED' WHERE id = ?", (third,))
    assert downloads._claim_pending_job_sync() is None
    remote._confirm_resource_sync(job['attempt_id'])
    assert downloads._claim_pending_job_sync()['job_id'] == second


@pytest.mark.asyncio
async def test_local_slot_retained_through_actual_stop_cleanup(tmp_path):
    db, _, work_id = await prepare(tmp_path)
    session = WorkerSession(db)
    await session.start()
    entered, cleaning, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
    class Api:
        async def get_file(self, file_id):
            return SimpleNamespace(file_unique_id='stable', file_path='file.zip')
        async def download_file(self, remote, destination):
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                cleaning.set()
                await release.wait()
    downloads = DownloadService(db, tmp_path / 'work')
    async def api():
        return Api()
    downloads._telegram_api = api
    job_id = await downloads.enqueue_telegram_download(work_id, {'file_id': 'fixture', 'file_name': 'file.zip'}, redownload=True)
    worker = asyncio.create_task(downloads._process_one())
    stopping = None
    try:
        await asyncio.wait_for(entered.wait(), 3)
        second = pending(db, 'TELEGRAM', 2)
        downloads._queue_control.internal_command(job_id.job_id, 'cancel')
        stopping = asyncio.create_task(downloads.process_operations())
        await asyncio.wait_for(cleaning.wait(), 3)
        assert downloads._claim_pending_job_sync() is None
        with pytest.raises(RuntimeError, match='WORKER_EXECUTIONS_STILL_RUNNING'):
            await session.stop()
        assert session._lock_file is not None
    finally:
        release.set()
        if not worker.done():
            worker.cancel()
        await asyncio.gather(worker, return_exceptions=True)
        if stopping is not None:
            await stopping
        await session.stop()
    assert downloads._claim_pending_job_sync()['job_id'] == second


@pytest.mark.asyncio
async def test_exhausted_remote_recovery_still_confirms_stop_before_failure(tmp_path):
    db, _, _ = await prepare(tmp_path)
    job_id = pending(db, 'EH_TORRENT', 2)
    session = WorkerSession(db)
    await session.start()
    remote = TorrentService(db, tmp_path / 'work', config_provider=None, credentials_provider=None)
    stopped = []
    async def confirm(operation):
        with db.connection() as c:
            assert c.execute('SELECT state FROM download_jobs WHERE id = ?', (job_id,)).fetchone()[0] == 'STOPPING'
        remote._confirm_resource_sync(operation['attempt_id'])
        stopped.append(True)
    downloads = DownloadService(db, tmp_path / 'work', torrent_stop=confirm)
    try:
        job = downloads._claim_pending_job_sync()
        execution = Execution(db.path, job['task_id'], job['attempt_id'], job['generation'])
        remote._save_resource_sync(execution, 'https://qb.invalid', 'b' * 40, 'fixture-tag', 'UNKNOWN', 'INTENDED')
        with db.connection() as c:
            c.execute("UPDATE download_jobs SET recovery_count = 3, lease_expires_at = datetime('now', '-1 minute') WHERE id = ?", (job_id,))
        assert await downloads.reclaim_expired_leases() == 1
        assert downloads._claim_pending_job_sync() is None and not stopped
        assert await downloads.process_operations() == 1
        with db.connection() as c:
            assert c.execute('SELECT state, error_code, recovery_count FROM download_jobs WHERE id = ?', (job_id,)).fetchone() == (
                'FAILED', 'WORKER_RECOVERY_EXHAUSTED', 4)
        assert stopped == [True]
    finally:
        await session.stop()
