"""Manual source corrections share work CAS, receipts and identity arbitration."""
from hashlib import sha256
import json

from app.candidates.entries import IngestError
from app.candidates.links import GALLERY_URL_PATTERN, find_magnet_ref, normalize_preview_url
from app.candidates.references import request_reference_in_transaction
from app.downloads.execution import run_sync
from app.review.commands import WorkCommands, WorkCommandError, require_work_version, work_version


class SourceCommands:
    def __init__(self, database):
        self.database = database

    async def execute(self, candidate_id, action, payload):
        identity, revision, key = WorkCommands._preconditions(payload)
        field = {'eh-ref': 'ex_gid', 'preview': 'preview_url', 'torrent': 'magnet'}.get(action)
        if field is None or not isinstance(payload.get(field), str):
            raise WorkCommandError('SOURCE_INVALID', '请提交有效的来源字段')
        value = payload[field].strip()
        target = {'work_id': payload.get('target_work_id'), 'revision': payload.get('target_revision')}
        digest = sha256(json.dumps({'action': 'source:' + action, 'candidate_id': candidate_id,
            'work_id': identity, 'revision': revision, 'value': value, 'target': target},
            sort_keys=True, separators=(',', ':')).encode()).hexdigest()
        return await run_sync(self._execute, candidate_id, action, value, identity, revision, key, digest, target)

    def _execute(self, candidate_id, action, value, identity, revision, key, digest, target):
        with self.database.connection() as connection:
            connection.execute('BEGIN IMMEDIATE')
            result = WorkCommands._receipt(connection, key, digest)
            if result is not None:
                return result
            require_work_version(connection, candidate_id, identity, revision)
            result = {'action': 'source:' + action, 'candidate_id': candidate_id, 'work_id': identity}
            if action == 'eh-ref':
                match = GALLERY_URL_PATTERN.fullmatch(value)
                if match is None:
                    raise WorkCommandError('GALLERY_INVALID', '无法识别画廊：请粘贴完整的 ExHentai/e-hentai 画廊链接（含编号与 token）')
                gallery_id, token = int(match.group(1)), match.group(2)
                other = connection.execute('SELECT id FROM candidates WHERE ex_gid = ? AND id <> ?',
                                           (gallery_id, candidate_id)).fetchone()
                expected = work_version(connection, other[0]) if other else None
                if target != (expected or {'work_id': None, 'revision': None}):
                    raise WorkCommandError('MERGE_TARGET_CHANGED', '目标画廊已有作品或已变化，请核对目标后明确确认合并',
                        status_code=409, details={'target': {'candidate_id': other[0], **expected} if other else None})
                try:
                    changed = request_reference_in_transaction(connection, self.database, candidate_id, gallery_id, token,
                                                               command_guard={'target': expected})
                except IngestError as exc:
                    raise WorkCommandError(exc.code, '画廊关联未修改，请核对当前作品、人工字段与停止状态', status_code=409) from exc
                result.update(changed)
                result['operation_id'] = 'reference:' + changed['operation_id'] if changed['operation_id'] else None
                result_id = changed['result_candidate_id']
                result['result_work_id'] = work_version(connection, result_id)['work_id'] if result_id else None
            else:
                if connection.execute("SELECT 1 FROM ingest_reference_changes WHERE candidate_id = ? AND status = 'PENDING'",
                                      (candidate_id,)).fetchone():
                    raise WorkCommandError('INGEST_IDENTITY_CHANGING', '画廊关联仍在等待停止确认，请稍后修改来源', status_code=409)
                if action == 'preview':
                    normalized = normalize_preview_url(value) if value else None
                    if value and normalized is None:
                        raise WorkCommandError('PREVIEW_INVALID', '无法识别预览链接：仅支持 telegra.ph / graph.org 预览页面')
                    self.database._set_candidate_preview_url_sync(candidate_id, normalized, connection=connection)
                else:
                    magnet = find_magnet_ref(value) if value else None
                    if value and magnet is None:
                        raise WorkCommandError('MAGNET_INVALID', '无法识别磁力链接：需包含 xt=urn:btih: 哈希')
                    self.database._set_candidate_manual_torrent_sync(candidate_id, value or None,
                        magnet[0] if magnet else None, connection=connection)
                result['status'] = 'COMPLETED'
            current = connection.execute('SELECT revision FROM work_versions WHERE work_id = ?', (identity,)).fetchone()[0]
            if current == revision:
                connection.execute('UPDATE work_versions SET revision = revision + 1 WHERE work_id = ?', (identity,))
                current += 1
            result['revision'] = current
            WorkCommands._save_receipt(connection, key, identity, digest, result)
            return result
