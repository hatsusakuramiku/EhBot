"""Single-process ownership and leases for actual running queue attempts."""
import asyncio
from contextlib import contextmanager
import logging
import os
from uuid import uuid4

from app.downloads.execution import run_sync


class WorkerAlreadyRunning(OSError):
    """A second backend must fail startup before accepting API commands."""


class WorkerSession:
    HEARTBEAT_SECONDS = 15
    LEASE_SECONDS = 120

    def __init__(self, database):
        self.database = database
        self.session_id = uuid4().hex
        self._lock_file = None
        self._heartbeat_task = None
        self._active = set()
        self._started = False
        self.stopping = False

    def _acquire(self):
        """OS releases the lock on process exit, including an unclean exit."""
        database_path = self.database.path.resolve()
        path = database_path.with_suffix(database_path.suffix + '.worker.lock')
        handle = path.open('a+b')
        try:
            if os.name == 'nt':
                import msvcrt
                if path.stat().st_size == 0:
                    handle.write(b'0')
                    handle.flush()
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            handle.close()
            raise WorkerAlreadyRunning('WORKER_ALREADY_RUNNING: another backend owns this database') from None
        self._lock_file = handle

    def _start_sync(self):
        if self._lock_file is None:
            self._acquire()
        try:
            with self.database.connection() as connection:
                connection.execute('BEGIN IMMEDIATE')
                connection.execute("UPDATE worker_sessions SET status = 'ABANDONED', stopped_at = CURRENT_TIMESTAMP "
                                   "WHERE status = 'LIVE'")
                connection.execute('INSERT INTO worker_sessions(session_id, process_id, status, expires_at) '
                    "VALUES(?, ?, 'LIVE', datetime('now', ?))", (self.session_id, os.getpid(), f'+{self.LEASE_SECONDS} seconds'))
                connection.execute("UPDATE eh_metadata_attempts SET status = 'UNKNOWN', "
                    "error_code = 'EH_METADATA_INTERRUPTED', finished_at = CURRENT_TIMESTAMP WHERE status = 'STARTED'")
                connection.execute("UPDATE eh_metadata_operations SET status = 'UNKNOWN', "
                    "error_code = 'EH_METADATA_INTERRUPTED', finished_at = CURRENT_TIMESTAMP WHERE status = 'RUNNING'")
                connection.execute("UPDATE account_command_receipts SET response_json = json_set(response_json, "
                    "'$.status', 'UNKNOWN', '$.error', json_object('code', 'ACCOUNT_COMMAND_INTERRUPTED', "
                    "'message', '请求被中断，请先核对账号状态再发起新操作')) "
                    "WHERE json_extract(response_json, '$.status') = 'PENDING'")
                connection.execute("UPDATE work_metadata_fetches SET status = 'UNKNOWN', "
                    "error_code = 'METADATA_FETCH_INTERRUPTED' WHERE status = 'RUNNING'")
                connection.execute("UPDATE toolchain_operations SET status = 'UNKNOWN', "
                    "error_code = 'TOOLCHAIN_INTERRUPTED', finished_at = CURRENT_TIMESTAMP WHERE status = 'RUNNING'")
                connection.execute("UPDATE ai_network_operations SET status = 'UNKNOWN', "
                    "error_code = 'AI_REQUEST_INTERRUPTED', finished_at = CURRENT_TIMESTAMP WHERE status = 'RUNNING'")
                connection.execute("DELETE FROM ai_network_claims WHERE operation_id IN "
                                   "(SELECT operation_id FROM ai_network_operations WHERE status != 'RUNNING')")
            self._started = True
        except BaseException:
            self._lock_file.close()
            self._lock_file = None
            raise

    async def start(self):
        try:
            await run_sync(self._start_sync)
        except BaseException:
            if self._lock_file is not None:
                self._lock_file.close()
                self._lock_file = None
            raise
        self.database.worker_session = self
        self._heartbeat_task = asyncio.create_task(self._run_heartbeat(), name='worker-session-heartbeat')

    async def _run_heartbeat(self):
        while True:
            try:
                await run_sync(self.heartbeat)
            except asyncio.CancelledError:
                raise
            except Exception:
                logging.getLogger(__name__).exception('worker_heartbeat_failed',
                    extra={'error_code': 'WORKER_HEARTBEAT_FAILED'})
            await asyncio.sleep(self.HEARTBEAT_SECONDS)

    def heartbeat(self):
        """Extend actual executions only; progress does not bump task revisions."""
        attempts = tuple(self._active)
        with self.database.connection() as connection:
            connection.execute('BEGIN IMMEDIATE')
            updated = connection.execute("UPDATE worker_sessions SET heartbeat_at = CURRENT_TIMESTAMP, "
                "expires_at = datetime('now', ?) WHERE session_id = ? AND status = 'LIVE'",
                (f'+{self.LEASE_SECONDS} seconds', self.session_id))
            if updated.rowcount != 1:
                raise OSError('WORKER_SESSION_LOST')
            for attempt_id in attempts:
                connection.execute("UPDATE queue_attempts SET lease_expires_at = datetime('now', ?) "
                    'WHERE attempt_id = ? AND lease_owner = ? AND finished_at IS NULL',
                    (f'+{self.LEASE_SECONDS} seconds', attempt_id, self.session_id))
                connection.execute("UPDATE download_jobs SET lease_expires_at = datetime('now', ?) "
                    "WHERE lease_owner = ? AND state IN ('DOWNLOADING', 'CONVERSION_RUNNING') "
                    'AND task_id IN (SELECT task_id FROM queue_tasks WHERE current_attempt_id = ?)',
                    (f'+{self.LEASE_SECONDS} seconds', self.session_id, attempt_id))

    def claim(self, connection, job_id, attempt_id):
        connection.execute("UPDATE download_jobs SET lease_owner = ?, lease_expires_at = datetime('now', ?) WHERE id = ?",
                           (self.session_id, f'+{self.LEASE_SECONDS} seconds', job_id))
        connection.execute("UPDATE queue_attempts SET lease_owner = ?, lease_expires_at = datetime('now', ?) WHERE attempt_id = ?",
                           (self.session_id, f'+{self.LEASE_SECONDS} seconds', attempt_id))

    def active(self, attempt_id):
        return attempt_id in self._active

    def reclaimable(self, connection, job_id, attempt_id):
        if self.active(attempt_id):
            return False
        # A claim may have committed just before its asyncio task is started.
        # Its short live lease protects that dispatch window; orphaned claims
        # from this session become recoverable when the lease expires.
        return connection.execute("SELECT 1 FROM download_jobs WHERE id = ? AND lease_owner = ? "
                                  'AND lease_expires_at > CURRENT_TIMESTAMP', (job_id, self.session_id)).fetchone() is None

    async def stop(self):
        """Called after providers, threads and processes have actually exited."""
        if self._active:
            raise RuntimeError('WORKER_EXECUTIONS_STILL_RUNNING')
        if self._heartbeat_task is not None:
            self._heartbeat_task.cancel()
            await asyncio.gather(self._heartbeat_task, return_exceptions=True)
            self._heartbeat_task = None
        if self._lock_file is None:
            return
        try:
            if self._started:
                with self.database.connection() as connection:
                    connection.execute("UPDATE worker_sessions SET status = 'STOPPED', stopped_at = CURRENT_TIMESTAMP "
                                       'WHERE session_id = ?', (self.session_id,))
        finally:
            self._lock_file.close()
            self._lock_file = None
            self.database.worker_session = None


@contextmanager
def track_execution(database, job):
    session = getattr(database, 'worker_session', None)
    if session is not None:
        session._active.add(job['attempt_id'])
    try:
        yield
    finally:
        if session is not None:
            session._active.discard(job['attempt_id'])


def bind_claim(database, connection, job_id, snapshot):
    session = getattr(database, 'worker_session', None)
    if session is not None:
        session.claim(connection, job_id, snapshot['attempt_id'])
        snapshot['worker_session_id'] = session.session_id
        snapshot['_worker_lock_fd'] = session._lock_file.fileno()
    return snapshot
