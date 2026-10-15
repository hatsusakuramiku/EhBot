"""Actual process ownership, rapid restart and leases beyond long transfers."""
import asyncio
from pathlib import Path
import sqlite3
import subprocess
import sys
from types import SimpleNamespace

import pytest

from app.db.database import Database
from app.downloads.execution import Execution
from app.downloads.service import DownloadService
from app.downloads.worker import WorkerSession
from tests.integration.test_artifact_pipeline import prepare, packer
from tests.integration.test_archive_workflow import write_zip


@pytest.mark.asyncio
@pytest.mark.parametrize('kind', ['DOWNLOAD', 'PACK'])
async def test_real_process_death_reclaims_before_lease_expiry(tmp_path, kind):
    db, _, work_id = await prepare(tmp_path)
    if kind == 'PACK':
        await packer(db, tmp_path).enqueue_for_candidate(work_id)
    else:
        DownloadService(db, tmp_path / 'work')._queue_control.internal_command(1, 'restart')
    code = '''
import asyncio, os, sys
from pathlib import Path
from app.db.database import Database
from app.downloads.worker import WorkerSession
from app.downloads.service import DownloadService
from app.conversion.service import ConversionService
root = Path(sys.argv[1])
db = Database(root / 'queue.db')
asyncio.run(WorkerSession(db).start())
service = (DownloadService(db, root / 'work') if sys.argv[2] == 'DOWNLOAD'
    else ConversionService(db, root / 'work', root / 'library'))
assert service._claim_pending_job_sync() is not None
os._exit(75)
'''
    result = subprocess.run([sys.executable, '-c', code, str(tmp_path), kind], capture_output=True, timeout=15)
    assert result.returncode == 75, result.stderr.decode(errors='replace')
    with db.connection() as c:
        old_owner, old_attempt = c.execute('SELECT lease_owner, attempt_id FROM queue_attempts WHERE kind = ?', (kind,)).fetchone()
        assert c.execute('SELECT lease_expires_at > CURRENT_TIMESTAMP FROM queue_attempts WHERE attempt_id = ?', (old_attempt,)).fetchone()[0] == 1
    restarted = WorkerSession(db)
    await restarted.start()
    try:
        service = DownloadService(db, tmp_path / 'work') if kind == 'DOWNLOAD' else packer(db, tmp_path)
        count = await (service.reclaim_expired_leases() if kind == 'DOWNLOAD' else service.reclaim_running_jobs())
        assert count == 1
        successor = service._claim_pending_job_sync()
        assert successor['attempt_id'] != old_attempt
        assert successor['worker_session_id'] == restarted.session_id
        with db.connection() as c:
            assert c.execute('SELECT status FROM worker_sessions WHERE session_id = ?', (old_owner,)).fetchone()[0] == 'ABANDONED'
            assert c.execute('SELECT status, error_code, finished_at IS NOT NULL FROM queue_attempts WHERE attempt_id = ?',
                             (old_attempt,)).fetchone() == ('INTERRUPTED', 'WORKER_SESSION_LOST', 1)
        # A caller holding an abandoned session never regains publication rights.
        stale = Execution(db.path, successor['task_id'], successor['attempt_id'], successor['generation'],
                          worker_session_id=old_owner)
        with db.connection() as c, pytest.raises(sqlite3.IntegrityError, match='QUEUE_STALE_EXECUTION'):
            stale.check(c)
    finally:
        await restarted.stop()


@pytest.mark.asyncio
async def test_os_lock_refuses_second_process_and_releases_after_shutdown(tmp_path):
    db = Database(tmp_path / 'queue.db')
    await db.initialize()
    first = WorkerSession(db)
    await first.start()
    code = '''
import asyncio, sys
from pathlib import Path
from app.db.database import Database
from app.downloads.worker import WorkerSession
try:
    asyncio.run(WorkerSession(Database(Path(sys.argv[1]))).start())
except OSError as exc:
    assert 'WORKER_ALREADY_RUNNING' in str(exc)
    sys.exit(76)
'''
    try:
        result = subprocess.run([sys.executable, '-c', code, str(db.path)], capture_output=True, timeout=15)
        assert result.returncode == 76, result.stderr.decode(errors='replace')
        with db.connection() as c:
            assert c.execute('SELECT count(*) FROM worker_sessions').fetchone()[0] == 1
    finally:
        await first.stop()
    second = WorkerSession(db)
    await second.start()
    await second.stop()


@pytest.mark.asyncio
async def test_heartbeat_and_reconcile_keep_actual_long_download(tmp_path):
    db, _, work_id = await prepare(tmp_path)
    session = WorkerSession(db)
    await session.start()
    entered, release = asyncio.Event(), asyncio.Event()

    class Api:
        async def get_file(self, file_id):
            return SimpleNamespace(file_unique_id='stable', file_path='file.zip')

        async def download_file(self, remote, destination):
            entered.set()
            await release.wait()
            write_zip(destination, ('01.jpg',))
            return destination.stat().st_size

    downloads = DownloadService(db, tmp_path / 'work')
    async def api():
        return Api()
    downloads._telegram_api = api
    await downloads.enqueue_telegram_download(work_id, {'file_id': 'a', 'file_name': 'file.zip'}, redownload=True)
    worker = asyncio.create_task(downloads._process_one())
    try:
        await asyncio.wait_for(entered.wait(), 3)
        with db.connection() as c:
            before = c.execute('SELECT revision, current_attempt_id FROM queue_tasks WHERE task_id = (SELECT task_id FROM download_jobs WHERE id = 1)').fetchone()
            c.execute("UPDATE download_jobs SET lease_expires_at = datetime('now', '-1 minute') WHERE id = 1")
            c.execute("UPDATE queue_attempts SET lease_expires_at = datetime('now', '-1 minute') WHERE attempt_id = ?", (before[1],))
        assert await downloads.reclaim_expired_leases() == 0
        session.heartbeat()
        with db.connection() as c:
            assert c.execute('SELECT lease_expires_at > CURRENT_TIMESTAMP FROM download_jobs WHERE id = 1').fetchone()[0] == 1
            assert c.execute('SELECT lease_expires_at > CURRENT_TIMESTAMP FROM queue_attempts WHERE attempt_id = ?', (before[1],)).fetchone()[0] == 1
            assert c.execute('SELECT revision FROM queue_tasks WHERE task_id = (SELECT task_id FROM download_jobs WHERE id = 1)').fetchone()[0] == before[0]
    finally:
        release.set()
        await worker
        await session.stop()
    assert not session._active


@pytest.mark.asyncio
async def test_periodic_reconcile_recovers_lost_claim_without_restarting_service(tmp_path):
    db, _, _ = await prepare(tmp_path)
    session = WorkerSession(db)
    await session.start()
    downloads = DownloadService(db, tmp_path / 'work')
    downloads._queue_control.internal_command(1, 'restart')
    claim = downloads._claim_pending_job_sync()
    # A committed claim must survive the gap before its task is dispatched.
    assert await downloads.reclaim_expired_leases() == 0
    with db.connection() as c:
        c.execute("UPDATE download_jobs SET lease_expires_at = datetime('now', '-1 minute') WHERE id = 1")
    loop = asyncio.create_task(downloads._run_operations())
    try:
        async with asyncio.timeout(4):
            while True:
                with db.connection() as c:
                    if c.execute('SELECT state FROM download_jobs WHERE id = 1').fetchone()[0] == 'PENDING':
                        break
                await asyncio.sleep(0.01)
        with db.connection() as c:
            assert c.execute('SELECT status FROM queue_attempts WHERE attempt_id = ?', (claim['attempt_id'],)).fetchone()[0] == 'INTERRUPTED'
    finally:
        loop.cancel()
        await asyncio.gather(loop, return_exceptions=True)
        await session.stop()


def test_second_backend_refuses_before_migrations_or_api_startup(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from app.main import create_app
    from app.downloads.worker import WorkerAlreadyRunning
    from tests.integration.test_authentication import make_settings
    settings = make_settings(tmp_path)
    with TestClient(create_app(settings)):
        second = create_app(settings)
        async def forbidden():
            pytest.fail('a second backend may not migrate the live database')
        monkeypatch.setattr(second.state.database, 'initialize', forbidden)
        with pytest.raises(WorkerAlreadyRunning):
            with TestClient(second):
                pytest.fail('a second backend may not accept API commands')


@pytest.mark.asyncio
async def test_startup_cancellation_releases_worker_lock(tmp_path, monkeypatch):
    from app.main import create_app
    from tests.integration.test_authentication import make_settings
    app = create_app(make_settings(tmp_path))
    entered = asyncio.Event()
    async def maintenance():
        entered.set()
        await asyncio.Event().wait()
    monkeypatch.setattr(app.state.database, 'maintain_titles', maintenance)
    async def start():
        async with app.router.lifespan_context(app):
            pytest.fail('startup should have been cancelled')
    task = asyncio.create_task(start())
    await asyncio.wait_for(entered.wait(), 4)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, 4)
    assert app.state.database.worker_session is None
    next_session = WorkerSession(app.state.database)
    await next_session.start()
    await next_session.stop()


@pytest.mark.asyncio
async def test_file_conflict_does_not_block_other_pack_claims(tmp_path):
    from tests.integration.test_refile_cancellation import packaged
    from tests.integration.test_relocation_recovery import crash_move
    db, work_id, _, pack = await packaged(tmp_path)
    await pack.enqueue_for_candidate(work_id, refile=True)
    crash_move(db, tmp_path, 'replace', 'worker')
    target = tmp_path / 'library' / 'Changed' / 'Book.cbz'
    target.write_bytes(b'unknown external bytes')
    session = WorkerSession(db)
    await session.start()
    try:
        assert await pack.reclaim_running_jobs() == 1
        assert target.read_bytes() == b'unknown external bytes'
        with db.connection() as c:
            assert c.execute("SELECT state, error_code FROM download_jobs WHERE provider = 'CONVERSION'").fetchone() == (
                'CONVERSION_FAILED', 'PUBLICATION_RECOVERY_REQUIRED')
            assert c.execute("SELECT status FROM queue_relocations").fetchone()[0] == 'RECOVERY_REQUIRED'
            c.execute("INSERT INTO candidates(id, status) VALUES(2, 'APPROVED')")
            c.execute("INSERT INTO download_jobs(candidate_id, idempotency_key, provider, state, details_json) "
                "VALUES(2, 'other-pack', 'CONVERSION', 'CONVERSION_PENDING', '{\"refile\":true}')")
        assert pack._claim_pending_job_sync()['candidate_id'] == 2
    finally:
        await session.stop()


@pytest.mark.asyncio
async def test_hard_backend_exit_stops_archive_tool_and_descendant(tmp_path):
    import os
    import time
    from tests.integration.test_archive_process_cancellation import assert_child_exited
    if os.name != 'nt' and sys.platform != 'linux':
        pytest.skip('supported production platforms are Windows and Linux')
    db, _, _ = await prepare(tmp_path)
    code = '''
import asyncio, os, pathlib, subprocess, sys, time
from app.archive.processes import managed_process
from app.db.database import Database
from app.downloads.execution import execution_scope
from app.downloads.service import DownloadService
from app.downloads.worker import WorkerSession
root = pathlib.Path(sys.argv[1])
db = Database(root / 'queue.db')
asyncio.run(WorkerSession(db).start())
downloads = DownloadService(db, root / 'work')
downloads._queue_control.internal_command(1, 'restart')
claim = downloads._claim_pending_job_sync()
marker = root / 'tool-tree.txt'
tool = ("import pathlib, subprocess, sys, os, time; "
    "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)']); "
    f"pathlib.Path({str(marker)!r}).write_text(str(os.getpid()) + ',' + str(child.pid)); time.sleep(30)")
with execution_scope(db.path, claim), managed_process([sys.executable, '-c', tool], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL):
    deadline = time.monotonic() + 5
    while not marker.exists():
        assert time.monotonic() < deadline
        time.sleep(.01)
    os._exit(77)
'''
    process = subprocess.run([sys.executable, '-c', code, str(tmp_path)], capture_output=True, timeout=12)
    assert process.returncode == 77, process.stderr.decode(errors='replace')
    pids = [int(value) for value in (tmp_path / 'tool-tree.txt').read_text().split(',')]
    deadline = time.monotonic() + 4
    for pid in pids:
        while True:
            try:
                assert_child_exited(pid)
                break
            except AssertionError:
                assert time.monotonic() < deadline
                await asyncio.sleep(.01)
    session = WorkerSession(db)
    await session.start()
    await session.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize('kind', ['DOWNLOAD', 'PACK'])
async def test_repeated_orphan_recovery_is_bounded_until_explicit_retry(tmp_path, kind):
    db, _, work_id = await prepare(tmp_path)
    session = WorkerSession(db)
    await session.start()
    downloads = DownloadService(db, tmp_path / 'work')
    service = downloads if kind == 'DOWNLOAD' else packer(db, tmp_path)
    if kind == 'DOWNLOAD':
        downloads._queue_control.internal_command(1, 'restart')
    else:
        await service.enqueue_for_candidate(work_id)
    try:
        for _ in range(4):
            job = service._claim_pending_job_sync()
            assert job is not None
            with db.connection() as c:
                c.execute("UPDATE download_jobs SET lease_expires_at = datetime('now', '-1 minute') WHERE id = ?", (job['job_id'],))
            assert await (service.reclaim_expired_leases() if kind == 'DOWNLOAD' else service.reclaim_running_jobs()) == 1
        with db.connection() as c:
            assert c.execute('SELECT state, error_code, recovery_count FROM download_jobs WHERE id = ?', (job['job_id'],)).fetchone() == (
                'FAILED' if kind == 'DOWNLOAD' else 'CONVERSION_FAILED', 'WORKER_RECOVERY_EXHAUSTED', 4)
        assert service._claim_pending_job_sync() is None
        downloads._queue_control.internal_command(job['job_id'], 'retry')
        with db.connection() as c:
            assert c.execute('SELECT recovery_count FROM download_jobs WHERE id = ?', (job['job_id'],)).fetchone()[0] == 0
        assert service._claim_pending_job_sync() is not None
    finally:
        await session.stop()
