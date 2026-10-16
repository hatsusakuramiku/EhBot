"""Stable task URLs over the existing shared commands and durable identities."""
import json
import sqlite3

from fastapi import APIRouter, Query, Request
from fastapi.responses import JSONResponse

from app.api import accounts, actions, deps
from app.api.contracts import ApiError
from app.api.events import EVENT_CANDIDATE, EVENT_DOWNLOAD
from app.api.realtime import PROTOCOL
from app.api.works import configured_sources
from app.downloads.execution import run_sync
from app.downloads.models import DownloadJobSummary, PERMANENT_DOWNLOAD_ERRORS
from app.downloads.queue import DownloadError, QueueControl
from app.review.application import enqueue_work, remove_work


router = APIRouter(prefix='/api/v2', tags=['api-v2'])
TASK_ACTIONS = ('cancel', 'retry', 'pause', 'resume', 'switch-source', 'stop-seeding')


async def _payload(request):
    payload = dict(await actions._body(request)) if await request.body() else {}
    return deps.command_payload(request, payload)


def _response(result):
    return JSONResponse(result, status_code=202 if result.get('operation_id') or result.get('removal_id') else 200,
                        headers={'Cache-Control': 'no-store'})


@router.get('/capabilities', name='capabilities')
async def capabilities(request: Request):
    await deps.require_session(request)
    prefix = request.scope.get('root_path', '')
    manager = deps.connection_manager(request)
    accounts_snapshot = await manager.registry.list_accounts()
    torrent = await deps.archive_settings_service(request).torrent_client()
    sources = []
    for provider in actions.SOURCE_ENQUEUE:
        kind = {'TELEGRAM': 'TG_BOT', 'TELEGRAM_USER': 'TG_USER', 'EXHENTAI': 'EH', 'EH_TORRENT': 'EH'}.get(provider)
        members = [row for row in accounts_snapshot if row['kind'] == kind]
        configured = bool(members) if kind else True
        available = any(row['enabled'] and row['verified'] and not row['stop_operation_id'] and
                        manager.account_status(row['account_id'])['state'] == 'connected' for row in members) if kind else True
        if provider == 'EH_TORRENT':
            configured = configured and torrent.is_configured
            available = available and torrent.is_configured
        sources.append({'provider': provider, 'supported': True, 'configured': configured,
                        'available': available, 'explicit_only': provider == 'EXHENTAI',
                        'may_spend_gp': provider == 'EXHENTAI'})
    return JSONResponse({'api_versions': ['v1', 'v2'], 'task_api': prefix + '/api/v2',
        'settings_api': prefix + '/api/v1/settings', 'accounts_api': prefix + '/api/v2/accounts',
        'deployment_prefix': prefix, 'administrator': {'single': True, 'password_login_requires_username': True},
        'realtime': {'protocol': PROTOCOL, 'path': prefix + '/ws/ui', 'replay': False,
                     'resync': 'snapshot', 'accepted_frame_is_durable': False},
        'sources': sources,
        'tasks': {'identity': 'task_id', 'revision_required': True, 'idempotency_header': 'Idempotency-Key',
                  'actions': list(TASK_ACTIONS) + ['priority'], 'pause_running': False,
                  'switch_targets': ['EXHENTAI', 'TELEGRAPH']},
        'secret_responses': {'account_reveal': 'http_only', 'api_key_generation': 'first_response_only'}},
        headers={'Cache-Control': 'no-store'})


def _task_view(task):
    """Expose lifecycle facts without opaque provider payloads or secret envelopes."""
    fields = ('task_id', 'legacy_job_id', 'candidate_id', 'kind', 'provider', 'status', 'revision',
              'generation', 'priority', 'current_attempt_id', 'created_at', 'updated_at')
    result = {key: task[key] for key in fields}
    result['stage'] = 'pack' if task['kind'] == 'PACK' else 'download'
    details = json.loads(task['details_json'] or '{}')
    state = task['status']
    policy_state = state.removeprefix('CONVERSION_')
    summary = DownloadJobSummary(job_id=task['legacy_job_id'], candidate_id=task['candidate_id'],
        provider=task['provider'], state='DOWNLOADING' if policy_state == 'RUNNING' else policy_state,
        attempt_count=len(task['attempts']), error_code=task['error_code'], error_message=None,
        artifact_path=None, artifact_size=None, created_at=task['created_at'], updated_at=task['updated_at'], details=details)
    result['progress'] = {'percent': summary.progress_percent, 'download_speed': summary.download_speed,
                          'upload_speed': summary.upload_speed, 'num_seeds': summary.num_seeds, 'eta_seconds': summary.eta_seconds}
    result['error_code'] = summary.error_code
    blocking = [row for row in task['operations'] if row['status'] not in {'COMPLETED', 'FAILED'}]
    result['actions'] = {'pause': summary.is_pausable, 'resume': state == 'PAUSED',
        'retry': policy_state in {'FAILED', 'CANCELLED', 'PAUSED'} and summary.error_code not in PERMANENT_DOWNLOAD_ERRORS,
        'cancel': state not in {'COMPLETED', 'CONVERSION_COMPLETED', 'CANCELLED', 'REMOVED', 'STOPPING'} or
                  any(row['action'] == 'replace-account' for row in blocking),
        'stop_seeding': summary.is_seeding and not blocking,
        'switch_source': task['kind'] == 'DOWNLOAD' and state not in {'COMPLETED', 'CANCELLED', 'REMOVED', 'STOPPING'} and not blocking,
        'priority': state not in {'COMPLETED', 'CONVERSION_COMPLETED', 'FAILED', 'CONVERSION_FAILED', 'CANCELLED', 'STOPPING', 'REMOVED'}}
    result['attempts'] = [{key: row[key] for key in ('attempt_id', 'generation', 'attempt_no', 'kind', 'provider',
        'status', 'error_code', 'created_at', 'finished_at')} for row in task['attempts']]
    result['operations'] = task['operations']
    result['removals'] = [{'operation_id': row['removal_id'], 'status': row['status'], 'delete_files': bool(row['delete_files'])}
                          for row in task['removals']]
    result['artifacts'] = [{key: row[key] for key in ('artifact_id', 'artifact_type', 'producer_attempt_id',
        'sha256', 'size_bytes', 'page_count')} for row in task['artifacts']]
    result['pack_intent'] = task['pack_intent']
    result['blocking_operations'] = blocking
    return result


@router.get('/downloads', name='downloads.list')
async def list_downloads(request: Request, limit: int = Query(50, ge=1, le=100), offset: int = Query(0, ge=0)):
    await deps.require_session(request)
    database = deps.database(request)
    def read():
        with database.connection() as connection:
            connection.execute('BEGIN')
            total = connection.execute('SELECT count(*) FROM queue_tasks').fetchone()[0]
            ids = [row[0] for row in connection.execute('SELECT task_id FROM queue_tasks '
                'ORDER BY created_at DESC, task_id DESC LIMIT ? OFFSET ?', (limit, offset))]
            control = QueueControl(database)
            return {'items': [_task_view(control._get_task_sync(identity, connection=connection)) for identity in ids],
                    'total': total, 'limit': limit, 'offset': offset}
    return JSONResponse(await run_sync(read), headers={'Cache-Control': 'no-store'})


@router.get('/downloads/{task_id}', name='downloads.get')
async def get_download(request: Request, task_id: str):
    await deps.require_session(request)
    try:
        task = await deps.download_service(request).get_task(task_id)
    except DownloadError as exc:
        raise actions._translate(exc) from exc
    return JSONResponse(_task_view(task), headers={'Cache-Control': 'no-store'})


async def _command(request, task_id, action):
    await actions._guard(request)
    payload = await _payload(request)
    if payload.get('task_id', task_id) != task_id:
        raise ApiError('TASK_ID_CONFLICT', '路径与请求体任务身份不一致', status_code=409)
    if type(payload.get('expected_revision')) is not int or payload['expected_revision'] < 1:
        raise ApiError('PRECONDITION_REQUIRED', '请提交已读取的任务版本', status_code=428)
    if not isinstance(payload.get('idempotency_key'), str) or not 1 <= len(payload['idempotency_key']) <= 200:
        raise ApiError('IDEMPOTENCY_KEY_REQUIRED', '请提交幂等键', status_code=428)
    if action == 'switch-source':
        target = payload.get('target_provider', payload.get('provider'))
        if 'provider' in payload and payload['provider'] != target:
            raise ApiError('PROVIDER_CONFLICT', '切换来源参数不一致')
        payload['provider'] = target
    def resolve():
        with deps.database(request).connection() as connection:
            row = connection.execute('SELECT legacy_job_id FROM queue_tasks WHERE task_id = ?', (task_id,)).fetchone()
            if row is None:
                raise ApiError('TASK_NOT_FOUND', '任务不存在', status_code=404)
            return row[0]
    job_id = await run_sync(resolve)
    result = await actions.apply_job_command(request, job_id, action, {**payload, 'task_id': task_id})
    return _response(result)


def _register_command(action):
    async def execute(request: Request, task_id: str):
        return await _command(request, task_id, action)
    router.add_api_route('/downloads/{task_id}/' + action, execute, methods=['POST'], name='downloads.' + action)


for _action in TASK_ACTIONS:
    _register_command(_action)


@router.patch('/downloads/{task_id}', name='downloads.priority')
async def set_priority(request: Request, task_id: str):
    return await _command(request, task_id, 'priority')


@router.post('/works/{candidate_id}/downloads', name='works.download')
async def enqueue_download(request: Request, candidate_id: int):
    await actions._guard(request)
    payload = await _payload(request)
    provider = payload.get('provider')
    if provider not in actions.SOURCE_ENQUEUE:
        raise ApiError('PROVIDER_UNKNOWN', '请指定支持的下载来源', details={'allowed': list(actions.SOURCE_ENQUEUE)})
    try:
        result = await enqueue_work(deps.database(request), deps.download_service(request), candidate_id, provider,
                                    payload, available=configured_sources(request))
    except Exception as exc:
        raise actions._translate(exc) from exc
    actions._publish(request, EVENT_DOWNLOAD, job_id=result['job_id'])
    return _response(result)


@router.post('/works/{candidate_id}/pack', name='works.pack')
async def enqueue_pack(request: Request, candidate_id: int):
    await actions._guard(request)
    payload = await _payload(request)
    if 'artifact_id' in payload:
        if payload.get('input_artifact_id', payload['artifact_id']) != payload['artifact_id']:
            raise ApiError('ARTIFACT_INVALID', '原档产物参数不一致')
        payload['input_artifact_id'] = payload.pop('artifact_id')
    if not payload.get('input_artifact_id'):
        raise ApiError('ARTIFACT_REQUIRED', '请指定本次打包的原档产物', status_code=428)
    try:
        result = await enqueue_work(deps.database(request), deps.conversion_service(request), candidate_id, 'CONVERSION', payload)
    except Exception as exc:
        raise actions._translate(exc) from exc
    actions._publish(request, EVENT_DOWNLOAD, job_id=result['job_id'])
    return _response(result)


@router.post('/works/{candidate_id}/purge', name='works.purge')
async def purge_work(request: Request, candidate_id: int):
    operator = await actions._guard(request)
    payload = await _payload(request)
    if type(payload.get('delete_files')) is not bool:
        raise ApiError('DELETE_FILES_REQUIRED', '请明确是否删除文件')
    try:
        result = await remove_work(deps.database(request), deps.archived_work_service(request), candidate_id,
                                   'delete-files' if payload['delete_files'] else 'delete', payload, operator)
    except Exception as exc:
        raise actions._translate(exc) from exc
    actions._publish(request, EVENT_CANDIDATE, candidate_id=candidate_id)
    return _response(result)


@router.post('/works/{candidate_id}/remove', name='works.remove')
async def remove_downloaded_files(request: Request, candidate_id: int):
    operator = await actions._guard(request)
    payload = await _payload(request)
    if type(payload.get('delete_files')) is not bool:
        raise ApiError('DELETE_FILES_REQUIRED', '请明确是否删除文件')
    try:
        result = await remove_work(deps.database(request), deps.archived_work_service(request), candidate_id,
            'remove-files' if payload['delete_files'] else 'remove', payload, operator)
    except Exception as exc:
        raise actions._translate(exc) from exc
    actions._publish(request, EVENT_CANDIDATE, candidate_id=candidate_id)
    return _response(result)


@router.get('/operations/{operation_id}', name='operations.get')
async def get_operation(request: Request, operation_id: str):
    await deps.require_session(request)
    def read():
        with deps.database(request).connection() as connection:
            connection.row_factory = sqlite3.Row
            row = connection.execute('SELECT operation_id, task_id, attempt_id, generation, action, status, '
                'error_code, created_at, updated_at FROM queue_operations WHERE operation_id = ?', (operation_id,)).fetchone()
            if row:
                return dict(row)
            row = connection.execute('SELECT removal_id AS operation_id, status, delete_files, created_at, updated_at '
                                     'FROM queue_removals WHERE removal_id = ?', (operation_id,)).fetchone()
            if row:
                return dict(row)
        raise ApiError('OPERATION_NOT_FOUND', '操作不存在', status_code=404)
    return JSONResponse(await run_sync(read), headers={'Cache-Control': 'no-store'})


# Same endpoint functions; unique names prevent url_for from changing old Web URLs.
for _route in accounts.router.routes:
    router.add_api_route(_route.path, _route.endpoint, methods=list(_route.methods), name='accounts.' + _route.name)
