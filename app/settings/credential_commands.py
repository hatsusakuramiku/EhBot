"""Atomic credential edits; durable receipts never contain reusable secrets."""
from hashlib import sha256
import hmac
import json

from app.archive.vault import encrypt_password
from app.credentials import KIND_API_KEY, new_token, hash_secret
from app.downloads.execution import run_sync
from app.review.commands import WorkCommandError
from app.settings.commands import SettingsCommands
from app.settings.forms import checkbox


class CredentialCommands:
    ACTIONS = ('add', 'delete', 'api-key-generate', 'api-key-revoke')

    def __init__(self, database, archive):
        self.database, self.archive = database, archive

    async def execute(self, action, payload):
        if action not in self.ACTIONS:
            raise WorkCommandError('ACTION_UNKNOWN', '未知密码库操作')
        revision, key = payload.get('expected_revision'), payload.get('idempotency_key')
        if type(revision) is not int or revision < 1:
            raise WorkCommandError('PRECONDITION_REQUIRED', '请提交已读取的凭据配置版本', status_code=428)
        if not isinstance(key, str) or not key.strip() or len(key) > 128:
            raise WorkCommandError('IDEMPOTENCY_KEY_REQUIRED', '请提交 1–128 字符的幂等键', status_code=428)
        scope = 'api-key' if action.startswith('api-key-') else 'vault'
        values = {}
        if action == 'add':
            password = str(payload.get('password') or '')
            values['name'] = self.archive.password_values(str(payload.get('name') or ''), password)
            try:
                priority = payload.get('priority', 100)
                if isinstance(priority, bool):
                    raise ValueError
                values['priority'] = int(str(priority).strip() or '100')
            except (TypeError, ValueError):
                raise WorkCommandError('PRIORITY_INVALID', '优先级必须是整数') from None
            values.update(password=password, enabled=checkbox(payload.get('enabled', True)))
        elif action == 'delete':
            password_id, identity = payload.get('password_id'), payload.get('password_uuid')
            if type(password_id) is not int or password_id < 1:
                raise WorkCommandError('PASSWORD_ID_REQUIRED', '请指定要删除的密码条目')
            if not isinstance(identity, str) or not identity:
                raise WorkCommandError('PRECONDITION_REQUIRED', '请提交已读取的密码条目身份', status_code=428)
            values.update(password_id=password_id, password_uuid=identity)
        master = await self.archive.master_key()
        canonical = json.dumps(dict(action=action, scope=scope, revision=revision, values=values),
                               sort_keys=True, separators=(',', ':')).encode()
        digest = hmac.new(master, b'credential-command\0' + canonical, sha256).hexdigest()
        replay = await run_sync(self._inspect, action, scope, revision, key, digest, values)
        if replay is not None:
            return replay, None
        plaintext = None
        if action == 'add':
            values['cipher'] = await run_sync(encrypt_password, master, values.pop('password'))
        elif action == 'api-key-generate':
            plaintext, public_id, secret = new_token(KIND_API_KEY)
            values.update(public_id=public_id, secret_hash=hash_secret(secret))
        result, created = await run_sync(self._commit, action, scope, revision, key, digest, values)
        return result, plaintext if created else None

    @staticmethod
    def _require(connection, action, scope, revision, values):
        if action == 'delete':
            row = connection.execute('SELECT password_uuid, revision FROM archive_passwords WHERE id = ?',
                                     (values['password_id'],)).fetchone()
            if row is None or row[0] != values['password_uuid']:
                raise WorkCommandError('ARCHIVE_PASSWORD_NOT_FOUND', '原密码条目已移除，请重新读取', status_code=410)
            if row[1] != revision:
                raise WorkCommandError('REVISION_CONFLICT', '此密码条目已修改，请核对后重试', status_code=409,
                                       details={'snapshot_url': '/api/v1/settings/passwords'})
        else:
            SettingsCommands._require(connection, scope, revision)

    def _inspect(self, action, scope, revision, key, digest, values):
        with self.database.connection() as connection:
            connection.execute('BEGIN')
            replay = SettingsCommands._receipt(connection, key, digest)
            if replay is None:
                self._require(connection, action, scope, revision, values)
            return replay

    def _commit(self, action, scope, revision, key, digest, values):
        with self.database.connection() as connection:
            connection.execute('BEGIN IMMEDIATE')
            replay = SettingsCommands._receipt(connection, key, digest)
            if replay is not None:
                return replay, False
            self._require(connection, action, scope, revision, values)
            result = dict(action=action, scope=scope, status='COMPLETED')
            if action == 'add':
                password_id = self.database._save_archive_password_sync(values['name'], values['cipher'],
                    values['priority'], values['enabled'], connection=connection)
                row = connection.execute('SELECT password_uuid, revision FROM archive_passwords WHERE id = ?', (password_id,)).fetchone()
                result.update(password_id=password_id, password_uuid=row[0], password_revision=row[1])
            elif action == 'delete':
                self.database._delete_archive_password_sync(values['password_id'], connection=connection)
                result.update(**values, password_revision=revision)
            elif action == 'api-key-generate':
                self.database._replace_api_key_sync('移动端', values['public_id'], values['secret_hash'], connection=connection)
                result['public_id'] = values['public_id']
            else:
                self.database._revoke_active_api_key_sync(connection=connection)
            # An accepted no-op revoke consumes the observed version too.
            if action != 'delete':
                connection.execute('UPDATE settings_versions SET revision = revision + 1 WHERE scope = ? AND revision = ?',
                                   (scope, revision))
            result['revision'] = connection.execute('SELECT revision FROM settings_versions WHERE scope = ?', (scope,)).fetchone()[0]
            SettingsCommands._save_receipt(connection, key, digest, result)
            return result, True
