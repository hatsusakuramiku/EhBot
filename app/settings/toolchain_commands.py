"""Durable tool installation admission; one owner drains the actual installer."""
import asyncio
from hashlib import sha256
import json
import logging
from uuid import uuid4

from app.archive.service import ArchiveSettingsError
from app.archive.toolchain import SEVEN_ZIP_VERSION
from app.downloads.execution import run_sync
from app.review.commands import WorkCommandError, WorkCommands


class ToolchainCommands:
    def __init__(self, database, archive, *, notify=None):
        self.database, self.archive, self.notify = database, archive, notify
        self._gate = asyncio.Lock()
        self._tasks = set()
        self._submissions = set()
        self._stopping = False

    async def snapshot(self):
        def read():
            with self.database.connection() as connection:
                revision = connection.execute('SELECT revision FROM toolchain_version WHERE singleton = 1').fetchone()[0]
                row = connection.execute('SELECT operation_id FROM toolchain_operations ORDER BY revision DESC LIMIT 1').fetchone()
                return {'revision': revision, 'operation': self._read(connection, row[0]) if row else None}
        return await run_sync(read)

    @staticmethod
    def _read(connection, operation_id):
        row = connection.execute('SELECT status, revision, error_code, version FROM toolchain_operations WHERE operation_id = ?',
                                 (operation_id,)).fetchone()
        if row is None:
            raise WorkCommandError('TOOLCHAIN_OPERATION_NOT_FOUND', '工具安装操作不存在', status_code=404)
        return {'toolchain_operation_id': operation_id, 'status': row[0], 'revision': row[1],
                'version': row[3], 'error_code': row[2]}

    async def result(self, operation_id):
        def read():
            with self.database.connection() as connection:
                return self._read(connection, operation_id)
        return await run_sync(read)

    def _begin(self, payload):
        _, revision, key = WorkCommands._preconditions({**payload, 'work_id': 'toolchain'})
        force = payload.get('force', True)
        if type(force) is not bool:
            raise WorkCommandError('TOOLCHAIN_FORCE_INVALID', 'force 必须为布尔值')
        digest = sha256(json.dumps({'revision': revision, 'force': force}, sort_keys=True).encode()).hexdigest()
        with self.database.connection() as connection:
            connection.execute('BEGIN IMMEDIATE')
            row = connection.execute('SELECT operation_id, payload_hash, revision FROM toolchain_operations '
                                     'WHERE idempotency_key = ?', (key,)).fetchone()
            if row:
                if row[1] != digest:
                    raise WorkCommandError('IDEMPOTENCY_CONFLICT', '此幂等键已用于不同的工具安装', status_code=409)
                return {'toolchain_operation_id': row[0], 'status': 'ACCEPTED', 'revision': row[2]}, False, force
            session = getattr(self.database, 'worker_session', None)
            if session is not None and (session.stopping or not connection.execute(
                    "SELECT 1 FROM worker_sessions WHERE session_id = ? AND status = 'LIVE'", (session.session_id,)).fetchone()):
                raise WorkCommandError('WORKER_STOPPING', '后台正在停止，请稍后重试', status_code=503)
            current = connection.execute('SELECT revision FROM toolchain_version WHERE singleton = 1').fetchone()[0]
            if revision != current:
                raise WorkCommandError('REVISION_CONFLICT', '工具安装状态已变化，请核对后重新提交',
                                       status_code=409, details={'revision': current})
            if connection.execute("SELECT 1 FROM toolchain_operations WHERE status = 'RUNNING'").fetchone():
                raise WorkCommandError('TOOLCHAIN_BUSY', '已有工具安装正在执行，请先核对操作结果', status_code=409)
            operation_id = uuid4().hex
            connection.execute('UPDATE toolchain_version SET revision = revision + 1 WHERE singleton = 1')
            connection.execute('INSERT INTO toolchain_operations(operation_id, version, idempotency_key, payload_hash, '
                "revision, session_id, status) VALUES(?, ?, ?, ?, ?, ?, 'RUNNING')",
                (operation_id, SEVEN_ZIP_VERSION, key, digest, current + 1, session.session_id if session else None))
            return {'toolchain_operation_id': operation_id, 'status': 'ACCEPTED', 'revision': current + 1}, True, force

    @staticmethod
    def _forget(tasks, task):
        tasks.discard(task)
        if not task.cancelled():
            task.exception()

    async def submit(self, payload):
        # A disconnected caller cannot interrupt the commit -> dispatch handoff.
        task = asyncio.create_task(self._accept(dict(payload)))
        self._submissions.add(task)
        task.add_done_callback(lambda item: self._forget(self._submissions, item))
        return await asyncio.shield(task)

    async def _accept(self, payload):
        async with self._gate:
            if self._stopping:
                raise WorkCommandError('WORKER_STOPPING', '后台正在停止，请稍后重试', status_code=503)
            result, dispatch, force = await run_sync(self._begin, payload)
            if dispatch:
                task = asyncio.create_task(self._run(result['toolchain_operation_id'], force))
                self._tasks.add(task)
                task.add_done_callback(lambda item: self._forget(self._tasks, item))
                self._notify(result['toolchain_operation_id'])
            return result

    def _notify(self, operation_id):
        if self.notify:
            self.notify(toolchain_operation_id=operation_id)

    def _finish(self, operation_id, status, code):
        with self.database.connection() as connection:
            connection.execute('BEGIN IMMEDIATE')
            connection.execute('UPDATE toolchain_operations SET status = ?, error_code = ?, finished_at = CURRENT_TIMESTAMP '
                "WHERE operation_id = ? AND status = 'RUNNING' AND (session_id IS NULL OR EXISTS "
                "(SELECT 1 FROM worker_sessions s WHERE s.session_id = toolchain_operations.session_id AND s.status = 'LIVE'))",
                (status, code, operation_id))

    async def _run(self, operation_id, force):
        status, code = 'COMPLETED', None
        try:
            def live():
                with self.database.connection() as connection:
                    return connection.execute("SELECT 1 FROM toolchain_operations o WHERE operation_id = ? "
                        "AND status = 'RUNNING' AND (session_id IS NULL OR EXISTS "
                        "(SELECT 1 FROM worker_sessions s WHERE s.session_id = o.session_id AND s.status = 'LIVE'))",
                        (operation_id,)).fetchone() is not None
            if not await run_sync(live):
                return
            await self.archive.install_toolchain(force=force)
        except ArchiveSettingsError as exc:
            status, code = 'FAILED', exc.code
        except asyncio.CancelledError:
            await run_sync(self._finish, operation_id, 'UNKNOWN', 'TOOLCHAIN_INTERRUPTED')
            self._notify(operation_id)
            raise
        except Exception:
            status, code = 'UNKNOWN', 'TOOLCHAIN_INSTALL_UNKNOWN'
            logging.getLogger(__name__).warning('toolchain_install_unknown', extra={'error_code': code})
        await run_sync(self._finish, operation_id, status, code)
        self._notify(operation_id)

    async def stop(self):
        async def drain():
            async with self._gate:
                self._stopping = True
                tasks = tuple(self._tasks)
            await asyncio.gather(*tuple(self._submissions), return_exceptions=True)
            await asyncio.gather(*tasks, return_exceptions=True)
        task = asyncio.create_task(drain())
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            while not task.done():
                try:
                    await asyncio.shield(task)
                except asyncio.CancelledError:
                    continue
            task.result()
            raise

    async def ensure(self):
        """Startup uses the same durable operation without manufacturing public versions."""
        status = await self.archive.toolchain_status()
        if status['available']:
            return
        version = await self.snapshot()
        if version['operation'] and version['operation']['status'] == 'UNKNOWN':
            logging.getLogger(__name__).warning('toolchain_install_requires_review',
                                               extra={'error_code': 'TOOLCHAIN_INTERRUPTED'})
            return
        await self.submit({'expected_revision': version['revision'], 'idempotency_key': 'startup-' + uuid4().hex, 'force': False})
        await asyncio.gather(*tuple(self._tasks), return_exceptions=True)
