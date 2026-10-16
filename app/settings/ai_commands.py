"""AI provider edits share one local transaction and a safe fixed receipt."""
from hashlib import sha256
import hmac
import json

from app.ai.errors import AiError, AI_PROVIDER_NAME_TAKEN
from app.ai.service import AiProviderService
from app.archive.vault import encrypt_password
from app.db.database import _params_document
from app.downloads.execution import run_sync
from app.review.commands import WorkCommandError
from app.settings.ai import provider_submission, chain_scope, chain_action, chain_model_id
from app.settings.commands import SettingsCommands
from app.settings.forms import checkbox
from app.settings.ai import model_names


class AiProviderCommands:
    def __init__(self, database, archive):
        self.database, self.archive = database, archive

    async def execute(self, action, payload, *, provider_id=None):
        if action not in ('save', 'delete'):
            raise WorkCommandError('AI_ACTION_UNKNOWN', '未知供应商操作')
        revision, key = payload.get('expected_revision'), payload.get('idempotency_key')
        if type(revision) is not int or revision < 1:
            raise WorkCommandError('PRECONDITION_REQUIRED', '请提交已读取的供应商版本', status_code=428)
        if not isinstance(key, str) or not key.strip() or len(key) > 128:
            raise WorkCommandError('IDEMPOTENCY_KEY_REQUIRED', '请提交 1–128 字符的幂等键', status_code=428)
        raw_id = provider_id if provider_id is not None else payload.get('provider_id')
        if raw_id not in (None, ''):
            if isinstance(raw_id, bool) or not str(raw_id).isdigit() or int(raw_id) < 1:
                raise WorkCommandError('AI_PROVIDER_INVALID', '供应商编号无效')
            provider_id = int(raw_id)
        identity = payload.get('provider_uuid') or ''
        if provider_id is not None and (not isinstance(identity, str) or not identity):
            raise WorkCommandError('PRECONDITION_REQUIRED', '请提交已读取的供应商身份', status_code=428)
        if action == 'delete' and provider_id is None:
            raise WorkCommandError('AI_PROVIDER_INVALID', '请选择原供应商')
        children = {name: payload.get(name) for name in ('keys_revision', 'models_revision')} if action == 'delete' else {}
        if any(type(value) is not int or value < 1 for value in children.values()):
            raise WorkCommandError('PRECONDITION_REQUIRED', '删除须提交已读取的 Key 与模型列表版本', status_code=428)
        # Local validation has no catalogue/network reads. Forms and JSON hash
        # the same normalized values, including defaulted numbers and booleans.
        values = provider_submission({**payload, 'provider_id': provider_id}) if action == 'save' else {}
        if action == 'save':
            values = AiProviderService.provider_values(values)
            values['custom_headers'] = json.dumps(values['custom_headers'], ensure_ascii=False, sort_keys=True)
            values['default_params'] = json.dumps(_params_document(values['default_params']), ensure_ascii=False, sort_keys=True)
        canonical = json.dumps(dict(action='ai-provider-' + action, provider_id=provider_id,
            identity=identity, revision=revision, children=children, values=values), sort_keys=True, separators=(',', ':')).encode()
        digest = hmac.new(await self.archive.master_key(), b'ai-provider-command\0' + canonical, sha256).hexdigest()
        return await run_sync(self._execute, action, provider_id, identity, revision, key, digest, values, children)

    def _execute(self, action, provider_id, identity, revision, key, digest, values, children):
        with self.database.connection() as connection:
            connection.execute('BEGIN IMMEDIATE')
            result = SettingsCommands._receipt(connection, key, digest)
            if result is not None:
                return result
            if provider_id is None:
                SettingsCommands._require(connection, 'ai-providers', revision)
            else:
                current = connection.execute('SELECT provider_uuid, revision, keys_revision, models_revision FROM ai_providers WHERE id = ?',
                                             (provider_id,)).fetchone()
                if current is None or current[0] != identity:
                    raise WorkCommandError('AI_PROVIDER_NOT_FOUND', '原供应商已不存在，请重新读取', status_code=410)
                if current[1] != revision or (children and current[2:] != (children['keys_revision'], children['models_revision'])):
                    raise WorkCommandError('REVISION_CONFLICT', '此供应商已被其他端修改，请核对原草稿', status_code=409,
                        details={'provider_uuid': identity, 'revision': current[1], 'keys_revision': current[2],
                                 'models_revision': current[3], 'snapshot_url': '/api/v1/settings/ai'})
            if action == 'delete':
                self.database._delete_ai_provider_sync(provider_id, connection=connection)
                result = dict(provider_id=provider_id, provider_uuid=identity, revision=revision,
                              **children, status='COMPLETED')
            else:
                try:
                    provider = self.database._save_ai_provider_sync(**values, connection=connection)
                except LookupError:
                    raise AiError(AI_PROVIDER_NAME_TAKEN, '供应商名称已被占用') from None
                if provider_id is not None:
                    # Even an accepted no-op consumes its edit version.
                    connection.execute('UPDATE ai_providers SET revision = revision + 1 WHERE id = ? AND revision = ?',
                                       (provider_id, revision))
                current_revision = connection.execute('SELECT revision FROM ai_providers WHERE id = ?',
                                                     (provider.provider_id,)).fetchone()[0]
                result = dict(provider_id=provider.provider_id, provider_uuid=provider.provider_uuid,
                              revision=current_revision, keys_revision=provider.keys_revision,
                              models_revision=provider.models_revision, status='COMPLETED')
            result['action'] = action
            result['collection_revision'] = connection.execute(
                "SELECT revision FROM settings_versions WHERE scope = 'ai-providers'").fetchone()[0]
            SettingsCommands._save_receipt(connection, key, digest, result)
            return result


class AiChainCommands:
    """Serialize one scoped chain edit, its observed identity and its receipt."""

    def __init__(self, database):
        self.database = database

    async def execute(self, scope, payload):
        scope = chain_scope(scope)
        action = chain_action(str(payload.get('action') or ''))
        model_id = chain_model_id(payload)
        identity = payload.get('model_uuid')
        revision, key = payload.get('expected_revision'), payload.get('idempotency_key')
        if type(revision) is not int or revision < 1 or not isinstance(identity, str) or not identity:
            raise WorkCommandError('PRECONDITION_REQUIRED', '请提交已读取的模型身份与模型链版本', status_code=428)
        if not isinstance(key, str) or not key.strip() or len(key) > 128:
            raise WorkCommandError('IDEMPOTENCY_KEY_REQUIRED', '请提交 1–128 字符的幂等键', status_code=428)
        delta = 0
        if action == 'shift':
            try:
                raw = payload.get('delta')
                if type(raw) not in (int, str):
                    raise ValueError
                delta = int(raw)
            except (TypeError, ValueError):
                raise WorkCommandError('CHAIN_SHIFT_INVALID', '移动方向无效，请提交整数步数（负数上移，正数下移）') from None
        digest = sha256(json.dumps(dict(scope=scope, action=action, model_id=model_id, identity=identity,
            revision=revision, delta=delta), sort_keys=True, separators=(',', ':')).encode()).hexdigest()
        return await run_sync(self._execute, scope, action, model_id, identity, revision, key, delta, digest)

    def _execute(self, scope, action, model_id, identity, revision, key, delta, digest):
        with self.database.connection() as connection:
            connection.execute('BEGIN IMMEDIATE')
            result = SettingsCommands._receipt(connection, key, digest)
            if result is not None:
                return result
            model = connection.execute('SELECT model_uuid FROM ai_provider_models WHERE id = ?', (model_id,)).fetchone()
            if model is None or model[0] != identity:
                raise WorkCommandError('AI_CONFIGURATION_NOT_FOUND', '原模型已不存在，请重新读取', status_code=410)
            version_scope = 'ai-chain:' + scope
            current = connection.execute('SELECT revision FROM settings_versions WHERE scope = ?', (version_scope,)).fetchone()[0]
            if current != revision:
                raise WorkCommandError('REVISION_CONFLICT', '此模型链已被其他端修改，请核对原操作', status_code=409,
                    details={'scope': scope, 'revision': current, 'snapshot_url': '/api/v1/settings/' +
                             {'default': 'ai', 'archive_path': 'paths', 'candidate_admission': 'parse'}[scope]})
            ids = [row[0] for row in connection.execute(
                'SELECT provider_model_id FROM ai_model_chain WHERE scope = ? ORDER BY position', (scope,))]
            ordered = AiProviderService.chain_order(ids, action, model_id, delta)
            self.database._save_ai_model_chain_sync(ordered, scope, connection=connection)
            connection.execute('UPDATE settings_versions SET revision = revision + 1 WHERE scope = ? AND revision = ?',
                               (version_scope, revision))
            current = connection.execute('SELECT revision FROM settings_versions WHERE scope = ?', (version_scope,)).fetchone()[0]
            result = dict(scope=scope, action=action, model_id=model_id, model_uuid=identity,
                          revision=current, status='COMPLETED')
            SettingsCommands._save_receipt(connection, key, digest, result)
            return result


class AiCatalogueCommands:
    """Local key/model commands; supplier network actions have separate lifetimes."""

    ACTIONS = ('keys-add', 'key-toggle', 'key-reset', 'key-delete',
               'models-add', 'model-toggle', 'model-params', 'model-delete')

    def __init__(self, database, archive):
        self.database, self.archive = database, archive

    async def execute(self, action, target_id, payload):
        if action not in self.ACTIONS:
            raise WorkCommandError('AI_ACTION_UNKNOWN', '未知 Key 或模型操作')
        revision, key = payload.get('expected_revision'), payload.get('idempotency_key')
        if type(revision) is not int or revision < 1:
            raise WorkCommandError('PRECONDITION_REQUIRED', '请提交已读取的配置版本', status_code=428)
        if not isinstance(key, str) or not key.strip() or len(key) > 128:
            raise WorkCommandError('IDEMPOTENCY_KEY_REQUIRED', '请提交 1–128 字符的幂等键', status_code=428)
        if type(target_id) is not int or target_id < 1:
            raise WorkCommandError('AI_TARGET_INVALID', '配置编号无效')
        kind = 'provider' if action in ('keys-add', 'models-add') else action.split('-')[0]
        identity = payload.get(kind + '_uuid')
        if not isinstance(identity, str) or not identity:
            raise WorkCommandError('PRECONDITION_REQUIRED', '请提交已读取的原配置身份', status_code=428)
        values = {}
        if action == 'keys-add':
            values['entries'] = AiProviderService.key_values(str(payload.get('api_keys') or ''))
        elif action == 'models-add':
            values['names'] = AiProviderService.model_values(model_names(payload))
            if 'discovery_operation_id' in payload:
                operation_id = payload['discovery_operation_id']
                if not isinstance(operation_id, str) or not 1 <= len(operation_id) <= 128:
                    raise WorkCommandError('AI_DISCOVERY_INVALID', '原模型拉取操作编号无效，请重新拉取')
                values['discovery_operation_id'] = operation_id
        elif action.endswith('-toggle'):
            values['enabled'] = checkbox(payload.get('enabled'))
        elif action == 'model-params':
            values['params'] = _params_document(AiProviderService.parse_params(payload.get('params')))
        elif action == 'key-reset':
            health_revision = payload.get('expected_health_revision')
            if type(health_revision) is not int or health_revision < 1:
                raise WorkCommandError('PRECONDITION_REQUIRED', '请提交已读取的 Key 健康版本', status_code=428)
            values['health_revision'] = health_revision
        canonical = json.dumps(dict(action=action, target_id=target_id, identity=identity,
                                    revision=revision, values=values), sort_keys=True, separators=(',', ':')).encode()
        master = await self.archive.master_key()
        digest = hmac.new(master, b'ai-catalogue-command\0' + canonical, sha256).hexdigest()
        # Encryption is CPU work with no DB lock; the eventual CAS still owns admission.
        if action == 'keys-add':
            values = {'entries': await run_sync(lambda: tuple(
                (label, encrypt_password(master, secret)) for label, secret in values['entries']))}
        return await run_sync(self._execute, action, target_id, identity, revision, key, digest, values)

    @staticmethod
    def _require(connection, kind, target_id, identity, revision, *, collection=None):
        table = {'provider': 'ai_providers', 'key': 'ai_provider_keys', 'model': 'ai_provider_models'}[kind]
        column = collection or 'revision'
        row = connection.execute(f'SELECT {kind}_uuid, {column} FROM {table} WHERE id = ?', (target_id,)).fetchone()
        if row is None or row[0] != identity:
            raise WorkCommandError('AI_CONFIGURATION_NOT_FOUND', '原配置已不存在，请重新读取', status_code=410)
        if row[1] != revision:
            raise WorkCommandError('REVISION_CONFLICT', '此配置已被其他端修改，请核对原草稿', status_code=409,
                details={kind + '_uuid': identity, 'revision': row[1], 'snapshot_url': '/api/v1/settings/ai'})

    def _execute(self, action, target_id, identity, revision, key, digest, values):
        with self.database.connection() as connection:
            connection.execute('BEGIN IMMEDIATE')
            result = SettingsCommands._receipt(connection, key, digest)
            if result is not None:
                return result
            if action in ('keys-add', 'models-add'):
                collection = 'keys_revision' if action == 'keys-add' else 'models_revision'
                self._require(connection, 'provider', target_id, identity, revision, collection=collection)
                if action == 'keys-add':
                    entries = self.database._add_ai_provider_keys_sync(target_id, values['entries'], connection=connection)
                    rows = [{'key_id': row.key_id, 'key_uuid': row.key_uuid, 'revision': row.revision} for row in entries]
                else:
                    if 'discovery_operation_id' in values:
                        self._require_discovery(connection, values['discovery_operation_id'], target_id, identity)
                    entries = self.database._add_ai_provider_models_sync(target_id, values['names'], connection=connection)
                    rows = [{'model_id': row.model_id, 'model_uuid': row.model_uuid, 'revision': row.revision} for row in entries]
                connection.execute(f'UPDATE ai_providers SET {collection} = {collection} + 1 WHERE id = ? AND {collection} = ?',
                                   (target_id, revision))
                current = connection.execute(f'SELECT {collection} FROM ai_providers WHERE id = ?', (target_id,)).fetchone()[0]
                result = {'provider_id': target_id, 'provider_uuid': identity, collection: current, 'entries': rows}
            else:
                kind = action.split('-')[0]
                table = 'ai_provider_keys' if kind == 'key' else 'ai_provider_models'
                self._require(connection, kind, target_id, identity, revision)
                provider = connection.execute(f'SELECT p.id, p.provider_uuid FROM ai_providers p '
                    f'JOIN {table} item ON item.provider_id = p.id WHERE item.id = ?', (target_id,)).fetchone()
                if action == 'key-toggle':
                    self.database._set_ai_provider_key_enabled_sync(target_id, values['enabled'], connection=connection)
                elif action == 'key-delete':
                    self.database._delete_ai_provider_key_sync(target_id, connection=connection)
                elif action == 'key-reset':
                    health = connection.execute('SELECT health_revision FROM ai_provider_keys WHERE id = ?', (target_id,)).fetchone()[0]
                    if health != values['health_revision']:
                        raise WorkCommandError('HEALTH_REVISION_CONFLICT', '此 Key 的冷却或用量已更新，请核对后重试', status_code=409,
                            details={'key_uuid': identity, 'health_revision': health, 'snapshot_url': '/api/v1/settings/ai'})
                    self.database._reset_ai_key_cooldown_sync(target_id, connection=connection)
                    connection.execute('UPDATE ai_provider_keys SET health_revision = health_revision + 1 WHERE id = ? AND health_revision = ?',
                                       (target_id, health))
                elif action == 'model-toggle':
                    self.database._set_ai_provider_model_enabled_sync(target_id, values['enabled'], connection=connection)
                elif action == 'model-params':
                    self.database._set_ai_provider_model_params_sync(target_id,
                        AiProviderService.parse_params(json.dumps(values['params'])), connection=connection)
                else:
                    self.database._delete_ai_provider_model_sync(target_id, connection=connection)
                current_revision = revision
                if not action.endswith('-delete'):
                    if action != 'key-reset':
                        connection.execute(f'UPDATE {table} SET revision = revision + 1 WHERE id = ? AND revision = ?',
                                           (target_id, revision))
                    current_revision = connection.execute(f'SELECT revision FROM {table} WHERE id = ?', (target_id,)).fetchone()[0]
                result = {kind + '_id': target_id, kind + '_uuid': identity, 'revision': current_revision,
                          'provider_id': provider[0], 'provider_uuid': provider[1]}
                if action == 'key-reset':
                    result['health_revision'] = connection.execute('SELECT health_revision FROM ai_provider_keys WHERE id = ?', (target_id,)).fetchone()[0]
            result.update(action=action, status='COMPLETED')
            SettingsCommands._save_receipt(connection, key, digest, result)
            return result

    @staticmethod
    def _require_discovery(connection, operation_id, provider_id, identity):
        row = connection.execute('SELECT action, status, target_json FROM ai_network_operations WHERE operation_id = ?',
                                 (operation_id,)).fetchone()
        target = json.loads(row[2]) if row else {}
        current = connection.execute('SELECT revision, keys_revision FROM ai_providers WHERE id = ?', (provider_id,)).fetchone()
        if (not row or row[:2] != ('fetch', 'COMPLETED') or target.get('provider_id') != provider_id
                or target.get('provider_uuid') != identity or current != (target.get('expected_revision'), target.get('keys_revision'))):
            raise WorkCommandError('AI_DISCOVERY_STALE', '拉取后的供应商配置或 Key 已变化，请重新拉取再勾选添加', status_code=409)
