"""Durable manual acceptance; publication and its success result commit together."""
from contextvars import ContextVar
from hashlib import sha256
import json
from uuid import uuid4

from app.candidates import manual as ingest
from app.db.database import DuplicateGalleryCandidate
from app.downloads.execution import run_sync
from app.exhentai.downloader import ExHentaiDownloadError
from app.review.commands import WorkCommandError, WorkCommands, work_version
from app.review.service import ReviewError


current_manual_request = ContextVar('manual_work_request', default=None)


class ManualRequest:
    def __init__(self, database, operation_id):
        self.database, self.operation_id = database, operation_id

    def complete(self, connection, candidate_id):
        row = connection.execute('SELECT status, session_id FROM manual_work_requests WHERE operation_id = ?',
                                 (self.operation_id,)).fetchone()
        live = (row is not None and (row[1] is None or connection.execute(
            "SELECT 1 FROM worker_sessions WHERE session_id = ? AND status = 'LIVE'", (row[1],)).fetchone()))
        if row is None or row[0] != 'RUNNING' or not live:
            raise WorkCommandError('MANUAL_ADD_STALE', '手动添加已中断，请核对操作结果', status_code=409)
        result = {'candidate_id': candidate_id, **work_version(connection, candidate_id)}
        connection.execute("UPDATE manual_work_requests SET status = 'COMPLETED', result_json = ? "
                           'WHERE operation_id = ?', (json.dumps(result), self.operation_id))


def _read(connection, operation_id):
    row = connection.execute('SELECT status, result_json, session_id FROM manual_work_requests '
                             'WHERE operation_id = ?', (operation_id,)).fetchone()
    if row is None:
        raise WorkCommandError('MANUAL_ADD_NOT_FOUND', '手动添加操作不存在', status_code=404)
    status = row[0]
    if status == 'RUNNING' and row[2] is not None and not connection.execute(
            "SELECT 1 FROM worker_sessions WHERE session_id = ? AND status = 'LIVE'", (row[2],)).fetchone():
        connection.execute("UPDATE manual_work_requests SET status = 'UNKNOWN' WHERE operation_id = ?",
                           (operation_id,))
        status = 'UNKNOWN'
    return {'manual_add_operation_id': operation_id, 'status': status,
            **(json.loads(row[1]) if row[1] else {})}


async def manual_result(database, operation_id):
    def read():
        with database.connection() as connection:
            connection.execute('BEGIN IMMEDIATE')
            return _read(connection, operation_id)
    return await run_sync(read)


def _begin(database, raw, key, configured):
    WorkCommands._preconditions({'work_id': 'manual', 'expected_revision': 1, 'idempotency_key': key})
    if not isinstance(raw, str):
        raise WorkCommandError('INVALID_LINK', '请输入画廊链接字符串')
    raw = raw.strip()
    digest = sha256(json.dumps({'url': raw}, sort_keys=True).encode()).hexdigest()
    with database.connection() as connection:
        connection.execute('BEGIN IMMEDIATE')
        row = connection.execute('SELECT operation_id, payload_hash FROM manual_work_requests '
                                 'WHERE idempotency_key = ?', (key,)).fetchone()
        if row is not None:
            if row[1] != digest:
                raise WorkCommandError('IDEMPOTENCY_CONFLICT', '此幂等键已用于不同的手动添加', status_code=409)
            return _read(connection, row[0]), False
        if not configured:
            raise WorkCommandError('EXHENTAI_NOT_CONFIGURED', ingest.NOT_CONFIGURED_MESSAGE)
        if ingest.gallery_reference(raw) is None:
            raise WorkCommandError('INVALID_LINK', '仅支持 ExHentai 画廊链接（exhentai.org / e-hentai.org）或预览链接')
        session = getattr(database, 'worker_session', None)
        if session is not None and session.stopping:
            raise WorkCommandError('WORKER_STOPPING', '后台正在停止，请稍后重试', status_code=503)
        operation_id = uuid4().hex
        connection.execute('INSERT INTO manual_work_requests(operation_id, idempotency_key, payload_hash, session_id) '
                           'VALUES(?, ?, ?, ?)', (operation_id, key, digest, session.session_id if session else None))
        return _read(connection, operation_id), True


def _fail(database, operation_id, status, error):
    with database.connection() as connection:
        connection.execute("UPDATE manual_work_requests SET status = ?, result_json = ? "
            "WHERE operation_id = ? AND status = 'RUNNING'", (status, json.dumps(error), operation_id))


async def add_manual_work(database, exhentai, orchestrator, download, payload, *, configured):
    """Same-key concurrent requests read one operation and never repeat the fetch."""
    result, dispatch = await run_sync(_begin, database, payload.get('url'), payload.get('idempotency_key'), configured)
    operation_id = result['manual_add_operation_id']
    if dispatch:
        token = current_manual_request.set(ManualRequest(database, operation_id))
        try:
            await ingest.ingest_manual_link(payload['url'], database=database, exhentai_service=exhentai,
                                           orchestrator=orchestrator, download_service=download)
        except DuplicateGalleryCandidate as exc:
            await run_sync(_fail, database, operation_id, 'FAILED', {'error': {'code': 'CANDIDATE_DUPLICATE',
                'message': '作品已存在', 'status_code': 409, 'details': {
                    'existing_candidate_id': exc.existing_candidate_id, 'existing_work_id': exc.existing_work_id}}})
        except (ReviewError, ExHentaiDownloadError) as exc:
            await run_sync(_fail, database, operation_id, 'FAILED', {'error': {
                'code': exc.code, 'message': exc.public_message, 'status_code': getattr(exc, 'status_code', 400)}})
        except BaseException:
            await run_sync(_fail, database, operation_id, 'UNKNOWN', {})
            raise
        finally:
            current_manual_request.reset(token)
        result = await manual_result(database, operation_id)
    if result['status'] == 'FAILED':
        error = result['error']
        raise WorkCommandError(error['code'], error['message'], status_code=error['status_code'],
            details={**error.get('details', {}), 'manual_add_operation_id': operation_id})
    return result
