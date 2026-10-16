"""Versioned settings saves: prepare with existing validators, commit once.

Preparation uses an isolated settings buffer and holds no SQLite transaction.
Only the explicitly listed preference saves can use that buffer; network,
password vault, rule and toolchain actions keep their own command contracts.
"""
from copy import copy
from dataclasses import asdict
from hashlib import sha256
import hmac
import json

from app.ai.prompt import DEFAULT_CANDIDATE_PROMPT
from app.ai.models import CHAIN_SCOPE_ARCHIVE_PATH, CHAIN_SCOPE_DEFAULT, MODEL_SOURCE_CUSTOM
from app.archive.service import ArchiveSettingsError
from app.downloads.execution import run_sync
from app.review.commands import WorkCommandError
from app.settings import parse, paths, archive
from app.settings.forms import checkbox
from app.settings.service import SystemSettingsError, DEFAULT_AI_ENABLED, _read_bool


SYSTEM_FIELDS = ('source_concurrency', 'poll_interval_ms', 'timezone', 'auto_approval_interval_minutes',
                 'mobile_access_ttl_seconds', 'mobile_refresh_ttl_seconds', 'log_level', 'download_source_priority')
ACTION_SCOPES = {'system': 'system', 'parse': 'parse', 'parse-reset': 'parse', 'admission': 'admission',
                 'limits': 'archive-limits', 'auto-pack': 'auto-pack', 'template': 'template', 'paths': 'directories',
                 'torrent': 'torrent', 'ai-master': 'ai-master', 'path-ai': 'path-ai', 'path-ai-prompt': 'path-ai',
                 'path-cache-clear': 'path-cache',
                 'path-model-source': 'path-model-source', 'candidate-model-source': 'candidate-model-source'}
SECTION_SCOPES = {'system': ('system',), 'parse': ('parse', 'admission', 'candidate-model-source', 'ai-catalogue', 'ai-chain:default', 'ai-chain:candidate_admission'),
                  'archive': ('archive-limits', 'auto-pack', 'torrent'),
                  'paths': ('template', 'directories', 'path-ai', 'path-model-source', 'ai-catalogue', 'ai-chain:default', 'ai-chain:archive_path', 'rearchive', 'path-cache'),
                  'ai': ('ai-master', 'ai-providers', 'ai-catalogue', 'ai-chain:default'),
                  'passwords': ('vault', 'api-key')}


class SettingsDraft:
    """A private buffer used by copies of the existing preference services."""

    def __init__(self, stored):
        self.stored = stored
        self.writes = {'system_settings': {}, 'archive_settings': {}}

    async def system_settings(self):
        return {**self.stored['system_settings'], **self.writes['system_settings']}

    async def archive_settings(self):
        return {**self.stored['archive_settings'], **self.writes['archive_settings']}

    async def save_system_settings(self, values):
        self.writes['system_settings'].update(values)

    async def save_archive_settings(self, values):
        self.writes['archive_settings'].update(values)


def submission(action, payload):
    """Normalize form and JSON fields before hashing the same command."""
    if action == 'system':
        return {key: str(payload[key]) for key in SYSTEM_FIELDS if key in payload and payload[key] is not None}
    if action == 'parse':
        return parse.parse_rules_submission(payload)
    if action == 'admission':
        return parse.candidate_admission(payload, default_prompt=DEFAULT_CANDIDATE_PROMPT)
    if action == 'limits':
        values = archive.limits_submission(payload)
        if 'keep_original' in payload:
            values['keep_original'] = checkbox(payload['keep_original'])
        if 'image_quality' in payload:
            values['image_quality'] = str(payload['image_quality'] or '')
        return values
    if action == 'auto-pack':
        return {'enabled': checkbox(payload.get('enabled'))}
    if action == 'ai-master':
        return {'enabled': checkbox(payload.get('enabled', payload.get('ai_enabled')))}
    if action == 'path-ai':
        values = paths.ai_path_submission(payload)
        for name in paths.AI_PATH_NUMBERS:
            if values[name] is not None:
                values[name] = str(values[name])
        return values
    if action == 'path-ai-prompt':
        return {'prompt': str(payload.get('prompt') or '')}
    if action in ('path-model-source', 'candidate-model-source'):
        return {'model_source': str(payload.get('model_source', payload.get('ai_model_source')) or '').strip().lower()}
    if action == 'template':
        return paths.template_payload(payload)
    if action == 'paths':
        return {key: str(payload.get(key) or '') for key in ('library_path', 'work_path')}
    if action == 'torrent':
        values = archive.torrent_submission(payload)
        if not values.get('password'):
            values.pop('password', None)
        return values
    return {}


class SettingsCommands:
    def __init__(self, database, system=None, archive=None):
        self.database, self.system, self.archive = database, system, archive

    async def versions(self, section):
        scopes = SECTION_SCOPES.get(section, ())
        def read():
            with self.database.connection() as connection:
                return {scope: revision for scope, revision in connection.execute(
                    'SELECT scope, revision FROM settings_versions') if scope in scopes}
        return await run_sync(read)

    @staticmethod
    def _receipt(connection, key, digest):
        row = connection.execute('SELECT payload_hash, result_json FROM settings_command_receipts '
                                 'WHERE idempotency_key = ?', (key,)).fetchone()
        if row is None:
            return None
        if row[0] != digest:
            raise WorkCommandError('IDEMPOTENCY_CONFLICT', '此幂等键已用于不同的设置操作', status_code=409)
        return json.loads(row[1])

    @staticmethod
    def _require(connection, scope, revision):
        current = connection.execute('SELECT revision FROM settings_versions WHERE scope = ?', (scope,)).fetchone()[0]
        if current != revision:
            section = next(section for section, scopes in SECTION_SCOPES.items() if scope in scopes)
            raise WorkCommandError('REVISION_CONFLICT', '此保存区已被其他端修改，请核对原草稿后重新保存',
                                   status_code=409, details={'scope': scope, 'revision': current,
                                                           'snapshot_url': '/api/v1/settings/' + section})

    def _inspect(self, scope, revision, key, digest):
        with self.database.connection() as connection:
            connection.execute('BEGIN')
            result = self._receipt(connection, key, digest)
            if result is not None:
                return result, None
            self._require(connection, scope, revision)
            stored = {table: dict(connection.execute(f'SELECT key, value FROM {table}'))
                      for table in ('system_settings', 'archive_settings')}
            return None, SettingsDraft(stored)

    async def execute(self, action, payload):
        if action not in ACTION_SCOPES:
            raise WorkCommandError('SETTINGS_ACTION_UNKNOWN', '未知设置操作')
        scope = ACTION_SCOPES[action]
        revision, key = payload.get('expected_revision'), payload.get('idempotency_key')
        if type(revision) is not int or revision < 1:
            raise WorkCommandError('PRECONDITION_REQUIRED', '请提交已读取的保存区版本', status_code=428)
        if not isinstance(key, str) or not key.strip() or len(key) > 128:
            raise WorkCommandError('IDEMPOTENCY_KEY_REQUIRED', '请提交 1–128 字符的幂等键', status_code=428)
        values = submission(action, payload)
        canonical = json.dumps({'action': action, 'expected_revision': revision, 'values': values},
                               sort_keys=True, separators=(',', ':')).encode()
        # Password-bearing commands must not leave offline-guessable hashes in
        # the ordinary receipt table. The vault key stays in private storage.
        digest = (hmac.new(await self.archive.master_key(), b'settings-command\0' + canonical, sha256).hexdigest()
                  if action == 'torrent' else sha256(canonical).hexdigest())
        result, draft = await run_sync(self._inspect, scope, revision, key, digest)
        if result is not None:
            return result
        system, archive_service = copy(self.system), copy(self.archive)
        if system is not None:
            system._database = draft
        if archive_service is not None:
            archive_service._database = draft
        try:
            if action == 'system':
                if not values:
                    raise WorkCommandError('SETTINGS_REQUIRED', '请提供要修改的设置项', details={'allowed': list(SYSTEM_FIELDS)})
                await system.save(values)
            elif action == 'parse':
                await system.save_parse_rules(values)
            elif action == 'parse-reset':
                await system.reset_parse_rules()
            elif action == 'admission':
                await system.save_candidate_admission(values)
            elif action == 'limits':
                await archive_service.save_limits(values)
                if 'keep_original' in values:
                    await archive_service.save_keep_original(values['keep_original'])
                if 'image_quality' in values:
                    await archive_service.save_image_quality(values['image_quality'])
            elif action == 'auto-pack':
                await archive_service.save_auto_pack_after_download(values['enabled'])
            elif action == 'ai-master':
                await system.save_ai_enabled(values['enabled'])
            elif action == 'path-ai':
                if values['auto_series']:
                    await run_sync(self._check_series)
                await archive_service.save_ai_batch_size(values['ai_batch_size'])
                await archive_service.save_ai_concurrency(values['ai_concurrency'])
                await archive_service.save_path_source(values['path_source'])
                await archive_service.save_ai_prompt(values['ai_prompt'])
                await archive_service.save_ai_fallback_to_rules(values['ai_fallback_to_rules'])
                await archive_service.save_ai_stream(values['ai_stream'])
                await archive_service.save_ai_default_include_current(values['ai_default_include_current'])
                await archive_service.save_auto_series(values['auto_series'])
            elif action == 'path-ai-prompt':
                await archive_service.save_ai_prompt(values['prompt'])
            elif action == 'path-model-source':
                await archive_service.save_ai_model_source(values['model_source'])
            elif action == 'candidate-model-source':
                await system.save_ai_candidate_model_source(values['model_source'])
            elif action == 'template':
                if 'title_source' in values:
                    await archive_service.save_title_source(values['title_source'])
                await archive_service.save_library_template(values['library_template'])
            elif action == 'paths':
                await archive_service.save_paths(values)
            elif action == 'torrent':
                if values.get('password') == '********':
                    raise WorkCommandError('SECRET_MASK_INVALID', '掩码不能作为密码保存，请留空保留或填写新密码')
                current = asdict(await archive_service.torrent_client())
                current['password'] = ''
                await archive_service.save_torrent_client({**current, **values})
        except (SystemSettingsError, ArchiveSettingsError) as exc:
            # A same-key peer may have committed while preparation was in flight.
            result, _ = await run_sync(self._inspect, scope, revision, key, digest)
            if result is not None:
                return result
            raise WorkCommandError(exc.code, exc.public_message) from exc
        return await run_sync(self._commit, scope, revision, key, digest, draft.writes)

    def _commit(self, scope, revision, key, digest, writes):
        with self.database.connection() as connection:
            connection.execute('BEGIN IMMEDIATE')
            result = self._receipt(connection, key, digest)
            if result is not None:
                return result
            self._require(connection, scope, revision)
            removed = self.database._clear_ai_path_suggestions_sync(connection) if scope == 'path-cache' else None
            if writes['archive_settings'].get('auto_series') == '1':
                self._require_series(connection)
            for table, values in writes.items():
                for name, value in values.items():
                    assigned = connection.execute('SELECT scope FROM settings_field_scopes WHERE table_name = ? AND key = ?',
                                                   (table, name)).fetchone()
                    if assigned != (scope,):
                        raise RuntimeError('Preference write escaped its save scope')
                    connection.execute(f'INSERT INTO {table}(key, value) VALUES(?, ?) ON CONFLICT(key) '
                                       'DO UPDATE SET value = excluded.value, updated_at = CURRENT_TIMESTAMP',
                                       (name, str(value)))
            # Even an accepted no-op consumes the observed revision.
            connection.execute('UPDATE settings_versions SET revision = revision + 1 WHERE scope = ? AND revision = ?',
                               (scope, revision))
            current = connection.execute('SELECT revision FROM settings_versions WHERE scope = ?', (scope,)).fetchone()[0]
            result = {'scope': scope, 'revision': current, 'status': 'COMPLETED'}
            if removed is not None:
                result['removed'] = removed
            self._save_receipt(connection, key, digest, result)
            return result

    def _check_series(self):
        with self.database.connection() as connection:
            connection.execute('BEGIN')
            self._require_series(connection)

    @staticmethod
    def _require_series(connection):
        """Recheck the original Series prerequisites in the final write transaction."""
        stored = dict(connection.execute('SELECT key, value FROM system_settings'))
        if not _read_bool(stored, 'ai_enabled', DEFAULT_AI_ENABLED):
            raise WorkCommandError('SERIES_AI_UNAVAILABLE', 'AI 总开关关闭，无法开启「自动添加系列」')
        stored = dict(connection.execute('SELECT key, value FROM archive_settings'))
        scope = (CHAIN_SCOPE_ARCHIVE_PATH if stored.get('ai_model_source', '').strip().lower() == MODEL_SOURCE_CUSTOM
                 else CHAIN_SCOPE_DEFAULT)
        exists = connection.execute('SELECT 1 FROM ai_model_chain ch '
            'JOIN ai_provider_models m ON m.id = ch.provider_model_id '
            'JOIN ai_providers p ON p.id = m.provider_id WHERE ch.scope = ? LIMIT 1', (scope,)).fetchone()
        if exists is None:
            raise WorkCommandError('SERIES_AI_UNAVAILABLE', '还没有可用的模型链，无法开启「自动添加系列」')

    @staticmethod
    def _save_receipt(connection, key, digest, result):
        connection.execute('INSERT INTO settings_command_receipts(idempotency_key, payload_hash, result_json) '
                           'VALUES(?, ?, ?)', (key, digest, json.dumps(result)))
