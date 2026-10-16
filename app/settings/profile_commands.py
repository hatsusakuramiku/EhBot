"""One registered archive profile, one edit version and one fixed receipt."""
from hashlib import sha256
import json

from app.downloads.execution import run_sync
from app.review.commands import WorkCommandError
from app.settings.commands import SettingsCommands
from app.settings.forms import checkbox


class ProfileCommands:
    def __init__(self, database):
        self.database = database

    async def execute(self, name, payload):
        identity, revision, key = (payload.get(field) for field in ('profile_uuid', 'expected_revision', 'idempotency_key'))
        if not isinstance(identity, str) or not identity or type(revision) is not int or revision < 1:
            raise WorkCommandError('PRECONDITION_REQUIRED', '请提交已读取的工具身份和版本', status_code=428)
        if not isinstance(key, str) or not key.strip() or len(key) > 128:
            raise WorkCommandError('IDEMPOTENCY_KEY_REQUIRED', '请提交 1–128 字符的幂等键', status_code=428)
        values = {}
        if 'enabled' in payload:
            values['enabled'] = checkbox(payload['enabled'])
        if 'executable_path' in payload:
            values['executable_path'] = str(payload['executable_path'] or '').strip() or None
        if 'timeout_seconds' in payload:
            raw = str(payload['timeout_seconds'] if payload['timeout_seconds'] is not None else '').strip()
            try:
                values['timeout_seconds'] = int(raw) if raw else None
            except ValueError:
                raise WorkCommandError('ARCHIVE_PROFILE_INVALID', '超时时长必须是整数') from None
        digest = sha256(json.dumps({'action': 'profile-save', 'name': name, 'identity': identity,
                                   'revision': revision, 'values': values},
                                  sort_keys=True, separators=(',', ':')).encode()).hexdigest()
        return await run_sync(self._execute, name, identity, revision, key, digest, values)

    def _execute(self, name, identity, revision, key, digest, values):
        with self.database.connection() as connection:
            connection.execute('BEGIN IMMEDIATE')
            result = SettingsCommands._receipt(connection, key, digest)
            if result is not None:
                return result
            current = connection.execute('SELECT profile_uuid, revision FROM archive_tool_profiles WHERE name = ?',
                                         (name,)).fetchone()
            if current is None or current[0] != identity:
                raise WorkCommandError('ARCHIVE_PROFILE_NOT_FOUND', '原工具配置已不存在，请重新读取', status_code=410)
            if current[1] != revision:
                raise WorkCommandError('REVISION_CONFLICT', '此工具配置已被其他端修改，请核对原草稿后保存',
                    status_code=409, details={'profile_uuid': identity, 'revision': current[1],
                                            'snapshot_url': '/api/v1/settings/archive'})
            try:
                self.database._set_archive_tool_profile_state_sync(name, values.get('enabled'),
                    values.get('executable_path'), values.get('timeout_seconds'), connection=connection)
            except ValueError as exc:
                raise WorkCommandError('ARCHIVE_PROFILE_INVALID', '超时时长必须大于 0') from exc
            connection.execute('UPDATE archive_tool_profiles SET revision = revision + 1 WHERE name = ? AND revision = ?',
                               (name, revision))
            current_revision = connection.execute('SELECT revision FROM archive_tool_profiles WHERE name = ?',
                                                  (name,)).fetchone()[0]
            result = {'profile_uuid': identity, 'name': name, 'revision': current_revision, 'status': 'COMPLETED'}
            SettingsCommands._save_receipt(connection, key, digest, result)
            return result
