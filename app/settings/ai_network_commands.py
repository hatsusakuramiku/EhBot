"""Durable AI probes with fixed inputs, shared admission and no automatic resend."""
import asyncio
from dataclasses import dataclass, field
from hashlib import sha256
import json
import logging
from uuid import uuid4

from app.ai.errors import AiError
from app.ai.models import AiProvider
from app.db.database import (_AI_PROVIDER_COLUMNS, _AI_MODEL_COLUMNS, _ai_provider_from_row,
                             _ai_model_from_row, _ai_key_from_row)
from app.downloads.execution import run_sync
from app.review.commands import WorkCommandError


@dataclass(frozen=True)
class Probe:
    """Private request inputs; neither configuration nor envelopes enter receipts."""
    provider: AiProvider = field(repr=False)
    models: tuple = field(repr=False)
    keys: tuple = field(repr=False)


class AiNetworkCommands:
    def __init__(self, database, service, *, notify=None):
        self.database, self.service, self.notify = database, service, notify
        self._gate = asyncio.Lock()
        self._tasks, self._submissions = set(), set()
        self._stopping = False

    @staticmethod
    def _payload(action, payload, target_id):
        if action not in ('fetch', 'model', 'provider', 'all'):
            raise WorkCommandError('AI_ACTION_UNKNOWN', '未知 AI 网络操作')
        revision, key = payload.get('expected_revision'), payload.get('idempotency_key')
        if type(revision) is not int or revision < 1:
            raise WorkCommandError('PRECONDITION_REQUIRED', '请提交已读取的 AI 配置版本', status_code=428)
        if not isinstance(key, str) or not key.strip() or len(key) > 128:
            raise WorkCommandError('IDEMPOTENCY_KEY_REQUIRED', '请提交 1–128 字符的幂等键', status_code=428)
        target = dict(action=action, expected_revision=revision)
        if action != 'all':
            if type(target_id) is not int or target_id < 1:
                raise WorkCommandError('AI_TARGET_INVALID', '操作对象编号无效')
            kind = 'model' if action == 'model' else 'provider'
            target[kind + '_id'] = target_id
            required = ['provider_uuid', 'keys_revision']
            required += ['model_uuid', 'provider_revision'] if action == 'model' else ['models_revision'] if action == 'provider' else []
            for name in required:
                value = payload.get(name)
                valid = isinstance(value, str) and bool(value) if name.endswith('_uuid') else type(value) is int and value >= 1
                if not valid:
                    raise WorkCommandError('PRECONDITION_REQUIRED', '请提交已读取的 AI 身份和配置版本', status_code=428)
                target[name] = value
        return target, key

    @staticmethod
    def _admission(operation_id):
        return {'ai_operation_id': operation_id, 'status': 'ACCEPTED',
                'result_url': '/api/v1/settings/ai/operations/' + operation_id}

    @staticmethod
    def _read(connection, operation_id):
        row = connection.execute('SELECT action, target_json, status, result_json, error_code '
                                 'FROM ai_network_operations WHERE operation_id = ?', (operation_id,)).fetchone()
        if row is None:
            raise WorkCommandError('AI_OPERATION_NOT_FOUND', 'AI 操作不存在', status_code=404)
        return dict(ai_operation_id=operation_id, action=row[0], target=json.loads(row[1]), status=row[2],
                    result=json.loads(row[3]) if row[3] else None, error_code=row[4])

    async def result(self, operation_id):
        def read():
            with self.database.connection() as connection:
                return self._read(connection, operation_id)
        return await run_sync(read)

    async def recent(self):
        def read():
            with self.database.connection() as connection:
                connection.execute('BEGIN')
                ids = connection.execute('SELECT operation_id FROM ai_network_operations ORDER BY rowid DESC LIMIT 10').fetchall()
                return [self._read(connection, row[0]) for row in ids]
        return await run_sync(read)

    @staticmethod
    def _conflict():
        raise WorkCommandError('REVISION_CONFLICT', 'AI 配置已被其他端修改，请重新读取后核对操作', status_code=409,
                               details={'snapshot_url': '/api/v1/settings/ai'})

    def _capture(self, connection, target):
        action = target['action']
        model = None
        if action == 'model':
            row = connection.execute(f'SELECT {_AI_MODEL_COLUMNS} FROM ai_provider_models WHERE id = ?',
                                     (target['model_id'],)).fetchone()
            model = _ai_model_from_row(row) if row else None
            if model is None or model.model_uuid != target['model_uuid']:
                raise WorkCommandError('AI_MODEL_NOT_FOUND', '原模型已不存在，请重新读取', status_code=410)
            if model.revision != target['expected_revision']:
                self._conflict()
            provider_id = model.provider_id
        else:
            provider_id = target.get('provider_id')
        if action == 'all':
            version = connection.execute("SELECT revision FROM settings_versions WHERE scope = 'ai-catalogue'").fetchone()[0]
            if version != target['expected_revision']:
                self._conflict()
        rows = connection.execute(f'SELECT {_AI_PROVIDER_COLUMNS} FROM ai_providers ' +
            ('WHERE id = ? ' if provider_id is not None else '') + 'ORDER BY name, id',
            (provider_id,) if provider_id is not None else ()).fetchall()
        if action != 'all' and not rows:
            raise WorkCommandError('AI_PROVIDER_NOT_FOUND', '原供应商已不存在，请重新读取', status_code=410)
        probes = []
        for row in rows:
            provider = _ai_provider_from_row(row)
            if action != 'all':
                if provider.provider_uuid != target['provider_uuid']:
                    raise WorkCommandError('AI_PROVIDER_NOT_FOUND', '原供应商已不存在，请重新读取', status_code=410)
                revision = target['provider_revision'] if action == 'model' else target['expected_revision']
                if provider.revision != revision or provider.keys_revision != target['keys_revision']:
                    self._conflict()
                if action == 'provider' and provider.models_revision != target['models_revision']:
                    self._conflict()
            models = ()
            if action == 'model':
                models = (model,)
            elif action != 'fetch':
                models = tuple(_ai_model_from_row(row) for row in connection.execute(
                    f'SELECT {_AI_MODEL_COLUMNS} FROM ai_provider_models WHERE provider_id = ? AND enabled = 1 ORDER BY name, id',
                    (provider.provider_id,)))
            keys = tuple((_ai_key_from_row(row[:-1]), row[-1]) for row in connection.execute(
                'SELECT id, provider_id, label, enabled, failures, cooldown_until, last_used_at, created_at, '
                'key_uuid, revision, health_revision, cipher FROM ai_provider_keys WHERE provider_id = ? '
                'AND enabled = 1 AND (cooldown_until IS NULL OR cooldown_until <= CURRENT_TIMESTAMP) ORDER BY id',
                (provider.provider_id,)))
            probes.append(Probe(provider, models, keys))
        return tuple(probes)

    def _begin(self, target, key):
        digest = sha256(json.dumps(target, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
        with self.database.connection() as connection:
            connection.execute('BEGIN IMMEDIATE')
            old = connection.execute('SELECT operation_id, payload_hash FROM ai_network_operations WHERE idempotency_key = ?',
                                     (key,)).fetchone()
            if old:
                if old[1] != digest:
                    raise WorkCommandError('IDEMPOTENCY_CONFLICT', '此幂等键已用于不同的 AI 操作', status_code=409)
                return self._admission(old[0]), None
            session = getattr(self.database, 'worker_session', None)
            if session and (session.stopping or not connection.execute(
                    "SELECT 1 FROM worker_sessions WHERE session_id = ? AND status = 'LIVE'", (session.session_id,)).fetchone()):
                raise WorkCommandError('WORKER_STOPPING', '后台正在停止，请稍后重试', status_code=503)
            probes = self._capture(connection, target)
            resources = (['listing:' + probe.provider.provider_uuid for probe in probes] if target['action'] == 'fetch' else
                         ['model:' + model.model_uuid for probe in probes for model in probe.models])
            for resource in resources:
                if connection.execute('SELECT 1 FROM ai_network_claims WHERE resource = ?', (resource,)).fetchone():
                    raise WorkCommandError('AI_NETWORK_BUSY', '所选模型已有测试正在执行，请先查看操作结果', status_code=409)
            operation_id = uuid4().hex
            connection.execute('INSERT INTO ai_network_operations(operation_id, idempotency_key, payload_hash, action, '
                'target_json, session_id) VALUES(?, ?, ?, ?, ?, ?)',
                (operation_id, key, digest, target['action'], json.dumps(target), session.session_id if session else None))
            connection.executemany('INSERT INTO ai_network_claims(resource, operation_id) VALUES(?, ?)',
                                   [(resource, operation_id) for resource in resources])
            return self._admission(operation_id), probes

    async def submit(self, action, payload, *, target_id=None):
        target, key = self._payload(action, payload, target_id)
        task = asyncio.create_task(self._accept(target, key))
        self._submissions.add(task)
        task.add_done_callback(lambda item: self._forget(self._submissions, item))
        return await asyncio.shield(task)

    @staticmethod
    def _forget(tasks, task):
        tasks.discard(task)
        if not task.cancelled():
            task.exception()

    async def _accept(self, target, key):
        async with self._gate:
            if self._stopping:
                raise WorkCommandError('WORKER_STOPPING', '后台正在停止，请稍后重试', status_code=503)
            result, probes = await run_sync(self._begin, target, key)
            if probes is not None:
                task = asyncio.create_task(self._run(result['ai_operation_id'], target['action'], probes))
                self._tasks.add(task)
                task.add_done_callback(lambda item: self._forget(self._tasks, item))
                self._notify(result['ai_operation_id'])
            return result

    def _notify(self, operation_id):
        if self.notify:
            self.notify(ai_operation_id=operation_id)

    def _live(self, operation_id):
        with self.database.connection() as connection:
            return connection.execute("SELECT 1 FROM ai_network_operations o WHERE operation_id = ? AND status = 'RUNNING' "
                "AND (session_id IS NULL OR EXISTS (SELECT 1 FROM worker_sessions s "
                "WHERE s.session_id = o.session_id AND s.status = 'LIVE'))", (operation_id,)).fetchone() is not None

    def _finish(self, operation_id, status, result, code):
        with self.database.connection() as connection:
            connection.execute('BEGIN IMMEDIATE')
            changed = connection.execute('UPDATE ai_network_operations SET status = ?, result_json = ?, error_code = ?, '
                "finished_at = CURRENT_TIMESTAMP WHERE operation_id = ? AND status = 'RUNNING' AND "
                "(session_id IS NULL OR EXISTS (SELECT 1 FROM worker_sessions s "
                "WHERE s.session_id = ai_network_operations.session_id AND s.status = 'LIVE'))",
                (status, json.dumps(result) if result is not None else None, code, operation_id)).rowcount
            if changed:
                connection.execute('DELETE FROM ai_network_claims WHERE operation_id = ?', (operation_id,))

    async def _run(self, operation_id, action, probes):
        status, result, code = 'COMPLETED', None, None
        try:
            if not await run_sync(self._live, operation_id):
                return
            if action == 'fetch':
                names = await self.service.list_captured_models(probes[0].provider, probes[0].keys)
                result = {'names': list(names)}
            else:
                entries = []
                for probe in probes:
                    for model in probe.models:
                        if not await run_sync(self._live, operation_id):
                            return
                        item = await self.service.verify_captured(probe.provider, model, probe.keys, operation_id=operation_id)
                        entries.append(dict(model_uuid=model.model_uuid, provider_uuid=probe.provider.provider_uuid,
                                            ok=item.ok, code=item.code))
                result = {'entries': entries, 'ok': sum(item['ok'] for item in entries), 'total': len(entries)}
                if action == 'model' and entries and not entries[0]['ok']:
                    status, code = 'FAILED', entries[0]['code']
        except AiError as exc:
            status, code = 'FAILED', exc.code
        except asyncio.CancelledError:
            await run_sync(self._finish, operation_id, 'UNKNOWN', None, 'AI_REQUEST_INTERRUPTED')
            self._notify(operation_id)
            raise
        except Exception:
            status, code = 'UNKNOWN', 'AI_REQUEST_UNKNOWN'
            logging.getLogger(__name__).warning('ai_network_unknown', extra={'error_code': code})
        await run_sync(self._finish, operation_id, status, result, code)
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
