"""Per-rule CAS and receipts shared by JSON, WS and the original forms."""
from hashlib import sha256
import json
import sqlite3

from app.auto_approval.rules import RuleValidationError, render_rule_dsl
from app.archive.service import ArchiveSettingsError, ArchiveSettingsService
from app.downloads.execution import run_sync
from app.review.commands import WorkCommandError
from app.settings.auto_approval import rule_payload
from app.settings.forms import checkbox
from app.settings.paths import path_rule_payload


class RuleCommands:
    def __init__(self, database):
        self.database = database

    async def execute(self, domain, action, payload, *, rule_id=None):
        if domain not in ('auto-approval', 'paths') or action not in ('save', 'toggle', 'delete'):
            raise WorkCommandError('RULE_ACTION_UNKNOWN', '未知规则操作')
        revision, key = payload.get('expected_revision'), payload.get('idempotency_key')
        if type(revision) is not int or revision < 0:
            raise WorkCommandError('PRECONDITION_REQUIRED', '请提交已读取的规则版本；新增规则使用版本 0', status_code=428)
        if not isinstance(key, str) or not key.strip() or len(key) > 128:
            raise WorkCommandError('IDEMPOTENCY_KEY_REQUIRED', '请提交 1–128 字符的幂等键', status_code=428)
        identity = payload.get('rule_uuid') or None
        # Hash the submitted AST, not validation results: replay precedes current
        # rule existence and validators, even after the original rule was deleted.
        values = {}
        if action in ('save', 'toggle'):
            values['enabled'] = checkbox(payload.get('enabled'))
        if action == 'save':
            values.update(name=str(payload.get('name') or '').strip(),
                          priority=str(payload.get('priority') or '100'),
                          condition=payload.get('condition'), case_sensitive=checkbox(payload.get('case_sensitive')))
            if domain == 'paths':
                values['path_template'] = str(payload.get('path_template') or '').strip()
            else:
                values['action'] = str(payload.get('action') or 'APPROVE')
        if action == 'save':
            raw_id = payload.get('rule_id' if domain == 'auto-approval' else 'path_rule_id')
            try:
                rule_id = int(raw_id) if raw_id not in (None, '') else None
            except (ValueError, TypeError):
                raise WorkCommandError('RULE_INVALID', '规则编号无效') from None
        digest = sha256(json.dumps({'domain': domain, 'action': action, 'rule_id': rule_id,
                                   'rule_uuid': identity, 'revision': revision, 'values': values},
                                  sort_keys=True, separators=(',', ':')).encode()).hexdigest()
        if action == 'save':
            values['rule_id' if domain == 'auto-approval' else 'path_rule_id'] = rule_id
        return await run_sync(self._execute, domain, action, rule_id, identity, revision, key, digest, values)

    def _execute(self, domain, action, rule_id, identity, revision, key, digest, payload):
        table = 'auto_approval_rules' if domain == 'auto-approval' else 'archive_path_rules'
        with self.database.connection() as connection:
            connection.execute('BEGIN IMMEDIATE')
            previous = connection.execute('SELECT payload_hash, result_json FROM rule_command_receipts '
                                          'WHERE idempotency_key = ?', (key,)).fetchone()
            if previous is not None:
                if previous[0] != digest:
                    raise WorkCommandError('IDEMPOTENCY_CONFLICT', '此幂等键已用于不同的规则操作', status_code=409)
                return json.loads(previous[1])
            if rule_id is None:
                if action != 'save' or revision != 0 or identity:
                    raise WorkCommandError('PRECONDITION_REQUIRED', '新增规则须使用版本 0，且不携带旧规则身份', status_code=428)
            else:
                if not isinstance(identity, str) or not identity or revision < 1:
                    raise WorkCommandError('PRECONDITION_REQUIRED', '请提交已读取的规则身份和版本', status_code=428)
                current = connection.execute(f'SELECT rule_uuid, version FROM {table} WHERE id = ?', (rule_id,)).fetchone()
                if current is None or current[0] != identity:
                    raise WorkCommandError('RULE_NOT_FOUND', '原规则已删除或编号已被复用，请重新读取规则', status_code=410)
                if current[1] != revision:
                    raise WorkCommandError('REVISION_CONFLICT', '这条规则已被其他端修改，请核对原草稿后重新保存',
                                           status_code=409, details={'rule_uuid': identity, 'revision': current[1],
                                                                   'snapshot_url': '/api/v1/settings/' + domain})
            try:
                if action == 'save':
                    reader = rule_payload if domain == 'auto-approval' else path_rule_payload
                    values = reader(payload, condition=payload.get('condition'))
                    if domain == 'paths':
                        values['path_template'] = ArchiveSettingsService.validate_path_rule_template(values['path_template'])
                        values['dsl_snapshot'] = render_rule_dsl(values['condition'])
                        rule = self.database._save_archive_path_rule_sync(**values, connection=connection)
                    else:
                        rule = self.database._save_auto_approval_rule_sync(**values, connection=connection)
                    rule_id, identity, revision = rule.rule_id, rule.rule_uuid, rule.version
                elif action == 'toggle':
                    connection.execute(f'UPDATE {table} SET enabled = ?, version = version + 1, '
                                       'updated_at = CURRENT_TIMESTAMP WHERE id = ?',
                                       (int(checkbox(payload.get('enabled'))), rule_id))
                    revision += 1
                else:
                    connection.execute(f'DELETE FROM {table} WHERE id = ?', (rule_id,))
                    revision += 1
            except ArchiveSettingsError as exc:
                raise WorkCommandError(exc.code, exc.public_message) from exc
            except (RuleValidationError, ValueError, TypeError) as exc:
                raise WorkCommandError('RULE_INVALID', str(exc)) from exc
            except sqlite3.IntegrityError as exc:
                raise WorkCommandError('RULE_CONFLICT', '规则名称已存在或规则内容无效', status_code=409) from exc
            result = {'domain': domain, 'rule_id': rule_id, 'rule_uuid': identity, 'revision': revision,
                      'action': action, 'status': 'COMPLETED'}
            self._save_receipt(connection, key, digest, result)
            return result

    @staticmethod
    def _save_receipt(connection, key, digest, result):
        connection.execute('INSERT INTO rule_command_receipts(idempotency_key, payload_hash, result_json) '
                           'VALUES(?, ?, ?)', (key, digest, json.dumps(result)))
