"""Atomic work edits with stable identities and administrator-wide receipts.

Network reads and CBZ rewriting belong outside these short transactions. The
result describes the committed database edit, so replay never edits fields again.
"""
import asyncio
from hashlib import sha256
import json
from uuid import uuid4

from app.conversion.comicinfo import EDITABLE_ELEMENTS
from app.downloads.execution import run_sync
from app.review.models import editable_metadata_field
from app.review.models import (REVIEW_APPROVE, REVIEW_REJECT, REVIEW_REQUEUE,
                               STATUS_APPROVED, STATUS_REJECTED, STATUS_PENDING_REVIEW)
from app.review.service import ReviewError, ReviewService


class WorkCommandError(ReviewError):
    def __init__(self, code, message, *, status_code=400, details=None):
        super().__init__(code, message)
        self.status_code = status_code
        self.details = details or {}


def work_version(connection, candidate_id):
    row = connection.execute(
        'SELECT work_id, revision FROM work_versions WHERE candidate_id = ?', (candidate_id,),
    ).fetchone()
    if row is None:
        raise WorkCommandError('CANDIDATE_NOT_FOUND', '作品不存在或已被删除', status_code=404)
    return {'work_id': row[0], 'revision': row[1]}


def require_work_version(connection, candidate_id, expected_work_id, expected_revision):
    current = work_version(connection, candidate_id)
    if current != {'work_id': expected_work_id, 'revision': expected_revision}:
        raise WorkCommandError('REVISION_CONFLICT', '作品已变化，请核对最新状态与草稿后再提交',
            status_code=409, details={'candidate_id': candidate_id, **current,
                                     'snapshot': f'/api/v1/works/{candidate_id}'})
    return current


class WorkCommands:
    def __init__(self, database):
        self.database = database

    async def version(self, candidate_id):
        return await run_sync(self._version, candidate_id)

    def _version(self, candidate_id):
        with self.database.connection() as connection:
            return work_version(connection, candidate_id)

    def rename(self, candidate_id, payload, conversion, *, strict=False):
        return RenameCommand(self, candidate_id, payload, conversion, strict=strict)

    @staticmethod
    def _preconditions(payload):
        work_id, revision, key = (payload.get(name) for name in
                                  ('work_id', 'expected_revision', 'idempotency_key'))
        if not isinstance(work_id, str) or not work_id or type(revision) is not int or revision < 1:
            raise WorkCommandError('PRECONDITION_REQUIRED', '请提交已读取的作品身份和版本', status_code=428)
        if not isinstance(key, str) or not key.strip() or len(key) > 128:
            raise WorkCommandError('IDEMPOTENCY_KEY_REQUIRED', '请提交 1–128 字符的幂等键', status_code=428)
        return work_id, revision, key

    @staticmethod
    def _receipt(connection, key, digest):
        row = connection.execute('SELECT payload_hash, response_json FROM work_command_receipts '
                                 'WHERE idempotency_key = ? UNION ALL '
                                 'SELECT payload_hash, response_json FROM work_review_refusals '
                                 'WHERE idempotency_key = ?', (key, key)).fetchone()
        if row is None:
            if connection.execute('SELECT 1 FROM work_review_batches WHERE idempotency_key = ?', (key,)).fetchone():
                raise WorkCommandError('IDEMPOTENCY_CONFLICT', '此幂等键已用于批量作品操作', status_code=409)
            return None
        if row[0] != digest:
            raise WorkCommandError('IDEMPOTENCY_CONFLICT', '此幂等键已用于不同的作品命令', status_code=409)
        return json.loads(row[1])

    @staticmethod
    def _save_receipt(connection, key, work_id, digest, result):
        connection.execute('INSERT INTO work_command_receipts '
            '(idempotency_key, work_id, payload_hash, response_json) VALUES(?, ?, ?, ?)',
            (key, work_id, digest, json.dumps(result, separators=(',', ':'))))

    def _review_identity(self, candidate_id, action, payload):
        work_id, revision, key = self._preconditions(payload)
        if not isinstance(action, str) or action not in {'approve', 'reject', 'requeue'}:
            raise WorkCommandError('ACTION_UNKNOWN', '不支持的审核动作')
        if payload.get('note') is not None and not isinstance(payload['note'], str):
            raise WorkCommandError('NOTE_INVALID', '审核说明必须是字符串')
        digest = sha256(json.dumps({'action': 'review:' + action, 'candidate_id': candidate_id,
            'work_id': work_id, 'expected_revision': revision, 'note': payload.get('note')},
            sort_keys=True, separators=(',', ':')).encode()).hexdigest()
        return work_id, revision, key, digest

    async def inspect_review(self, candidate_id, action, payload):
        identity = self._review_identity(candidate_id, action, payload)
        return self._review_result(await run_sync(self._inspect_review, candidate_id, identity))

    @staticmethod
    def _review_result(result):
        if result is not None and 'batch_error' in result:
            error = result['batch_error']
            raise WorkCommandError(error['code'], error['message'], status_code=error['status_code'], details=error['details'])
        return result

    def _inspect_review(self, candidate_id, identity):
        work_id, revision, key, digest = identity
        with self.database.connection() as connection:
            connection.execute('BEGIN')
            receipt = self._receipt(connection, key, digest)
            if receipt is None:
                require_work_version(connection, candidate_id, work_id, revision)
            return receipt

    async def review(self, candidate_id, action, payload, operator, *, download=None, prepared=None):
        identity = self._review_identity(candidate_id, action, payload)
        return self._review_result(await run_sync(self._review, candidate_id, action, payload.get('note'), operator,
                              identity, download, prepared))

    def _review(self, candidate_id, action, note, operator, identity, download, prepared):
        work_id, revision, key, digest = identity
        with self.database.connection() as connection:
            connection.execute('BEGIN IMMEDIATE')
            receipt = self._receipt(connection, key, digest)
            if receipt is not None:
                return receipt
            require_work_version(connection, candidate_id, work_id, revision)
            actions = {'approve': (REVIEW_APPROVE, STATUS_APPROVED), 'reject': (REVIEW_REJECT, STATUS_REJECTED),
                       'requeue': (REVIEW_REQUEUE, STATUS_PENDING_REVIEW)}
            try:
                self.database._transition_candidate_status_sync(candidate_id, operator, *actions[action],
                    note.strip() or None if note is not None else None, connection)
            except (LookupError, PermissionError) as exc:
                raise WorkCommandError('REVIEW_INVALID_TRANSITION', str(exc), status_code=409) from exc
            job_ids, task_ids = [], []
            if action == 'approve':
                if download is None or prepared is None:
                    raise WorkCommandError('CANDIDATE_NOT_DOWNLOADABLE', '候选没有可用的下载来源')
                result = download._enqueue_sync(candidate_id, **prepared, connection=connection)
                job_ids.append(result.job_id)
                task_ids.append(result.task_id)
            if work_version(connection, candidate_id)['revision'] == revision:
                connection.execute('UPDATE work_versions SET revision = revision + 1 WHERE candidate_id = ?',
                                   (candidate_id,))
            result = {'action': action, 'candidate_id': candidate_id, **work_version(connection, candidate_id),
                      'job_ids': job_ids, 'task_ids': task_ids}
            self._save_receipt(connection, key, work_id, digest, result)
            return result


    def _enqueue_identity(self, candidate_id, provider, payload):
        work_id, revision, key = self._preconditions(payload)
        artifact_id = payload.get('input_artifact_id')
        if artifact_id is not None and (not isinstance(artifact_id, str) or not artifact_id):
            raise WorkCommandError('ARTIFACT_INVALID', '请提供有效的原档产物 ID')
        if provider not in {'TELEGRAM', 'TELEGRAM_USER', 'EXHENTAI', 'TELEGRAPH', 'EH_TORRENT', 'CONVERSION'}:
            raise WorkCommandError('PROVIDER_UNKNOWN', '不支持的下载或打包来源')
        if provider != 'CONVERSION' and artifact_id is not None:
            raise WorkCommandError('ARTIFACT_INVALID', '来源下载不接受打包原档参数')
        command = {'action': 'enqueue:' + provider, 'candidate_id': candidate_id,
                   'work_id': work_id, 'expected_revision': revision, 'input_artifact_id': artifact_id}
        if 'pack_requested' in payload:
            if type(payload['pack_requested']) is not bool or provider == 'CONVERSION':
                raise WorkCommandError('PACK_INTENT_INVALID', '下载打包意图必须为布尔值且仅用于来源下载')
            command['pack_requested'] = payload['pack_requested']
        digest = sha256(json.dumps(command,
            sort_keys=True, separators=(',', ':')).encode()).hexdigest()
        return work_id, revision, key, digest

    def _removal_identity(self, candidate_id, action, payload):
        work_id, revision, key = self._preconditions(payload)
        if action not in {'delete', 'delete-files', 'remove', 'remove-files'}:
            raise WorkCommandError('ACTION_UNKNOWN', '不支持的移除动作')
        keeper = payload.get('keeper')
        if keeper is not None:
            if not isinstance(keeper, dict) or type(keeper.get('candidate_id')) is not int:
                raise WorkCommandError('DEDUP_PLAN_INVALID', '去重保留项无效')
            self._preconditions({**keeper, 'idempotency_key': key})
        digest = sha256(json.dumps({'action': action, 'candidate_id': candidate_id,
            'work_id': work_id, 'expected_revision': revision, 'keeper': keeper},
            sort_keys=True, separators=(',', ':')).encode()).hexdigest()
        return work_id, revision, key, digest

    async def inspect_removal(self, candidate_id, action, payload):
        identity = self._removal_identity(candidate_id, action, payload)
        return self._review_result(await run_sync(self._inspect_review, candidate_id, identity))

    async def remove(self, candidate_id, action, payload, operator, archived, roots, work):
        """Commit the existing removal intent and its immutable receipt together."""
        identity = self._removal_identity(candidate_id, action, payload)
        return self._review_result(await run_sync(self._remove, candidate_id, action, identity,
                                                  operator, archived, roots, work, payload.get('keeper')))

    def _remove(self, candidate_id, action, identity, operator, archived, roots, work, keeper=None):
        work_id, revision, key, digest = identity
        with self.database.connection() as connection:
            connection.execute('BEGIN IMMEDIATE')
            receipt = self._receipt(connection, key, digest)
            if receipt is not None:
                return receipt
            require_work_version(connection, candidate_id, work_id, revision)
            if keeper is not None:
                require_work_version(connection, keeper['candidate_id'], keeper['work_id'], keeper['expected_revision'])
                gids = connection.execute('SELECT ex_gid FROM candidates WHERE id IN (?, ?)',
                    (candidate_id, keeper['candidate_id'])).fetchall()
                if len(gids) != 2 or gids[0][0] is None or gids[0][0] != gids[1][0]:
                    raise WorkCommandError('DEDUP_PLAN_CONFLICT', '去重分组已变化，请重新预览', status_code=409)
            result = archived._begin_removal_sync(candidate_id, action.endswith('-files'), operator,
                action.startswith('delete'), *roots, work, connection=connection)
            connection.execute('UPDATE work_versions SET revision = revision + 1 WHERE work_id = ?', (work_id,))
            result.update(work_id=work_id, revision=connection.execute(
                'SELECT revision FROM work_versions WHERE work_id = ?', (work_id,)).fetchone()[0],
                cleanup_status='PENDING', deleted_files=[], failed_files=[])
            self._save_receipt(connection, key, work_id, digest, result)
            return result

    async def inspect_enqueue(self, candidate_id, provider, payload):
        identity = self._enqueue_identity(candidate_id, provider, payload)
        return await run_sync(self._inspect_review, candidate_id, identity)

    @staticmethod
    def _active_download(connection, candidate_id):
        row = connection.execute("SELECT task_id, legacy_job_id, revision, provider, status FROM queue_tasks "
            "WHERE candidate_id = ? AND kind = 'DOWNLOAD' AND status NOT IN "
            "('COMPLETED', 'FAILED', 'CANCELLED', 'REMOVED')", (candidate_id,)).fetchone()
        return tuple(row) if row else None

    async def enqueue_target(self, candidate_id, provider, payload):
        """Capture the execution to stop and target reference before network work."""
        identity = self._enqueue_identity(candidate_id, provider, payload)
        def read():
            from app.downloads.queue import QueueControl
            with self.database.connection() as connection:
                connection.execute('BEGIN')
                require_work_version(connection, candidate_id, identity[0], identity[1])
                active = self._active_download(connection, candidate_id) if provider != 'CONVERSION' else None
                reference = None
                if active is not None and active[3] != provider:
                    if active[4] == 'STOPPING':
                        raise WorkCommandError('OPERATION_IN_PROGRESS', '原任务仍在停止，请等待收尾', status_code=409)
                    if provider not in {'EXHENTAI', 'TELEGRAPH'}:
                        raise WorkCommandError('PROVIDER_UNSUPPORTED', '此来源不支持活动切换，请先取消原任务', status_code=409)
                    reference = QueueControl._switch_reference(connection, candidate_id, provider)
                return active, reference
        return await run_sync(read)

    async def enqueue(self, candidate_id, provider, payload, service, *, prepared=None, target=None):
        identity = self._enqueue_identity(candidate_id, provider, payload)
        return await run_sync(self._enqueue, candidate_id, provider, payload.get('input_artifact_id'),
                              identity, service, prepared, payload.get('pack_requested'), target)

    def _enqueue(self, candidate_id, provider, artifact_id, identity, service, prepared, pack_requested=None, target=None):
        from app.downloads.queue import QueueControl, task_snapshot
        work_id, revision, key, digest = identity
        with self.database.connection() as connection:
            connection.execute('BEGIN IMMEDIATE')
            receipt = self._receipt(connection, key, digest)
            if receipt is not None:
                return receipt
            require_work_version(connection, candidate_id, work_id, revision)
            active = self._active_download(connection, candidate_id) if provider != 'CONVERSION' else None
            if target is not None and active != target[0]:
                raise WorkCommandError('REVISION_CONFLICT', '下载任务已变化，请刷新核对后重试', status_code=409)
            switching = active is not None and active[3] != provider
            if active is not None and not switching and pack_requested is not None:
                intent = connection.execute('SELECT pack_requested FROM queue_pack_intents WHERE task_id = ?', (active[0],)).fetchone()
                if intent is None or bool(intent[0]) != pack_requested:
                    raise WorkCommandError('PACK_INTENT_CONFLICT', '活动任务的打包意图不同，请先取消原任务', status_code=409)
            operation = None
            if provider == 'CONVERSION':
                job_id = service._enqueue_sync(candidate_id, input_artifact_id=artifact_id, connection=connection)
            elif switching:
                if target is None or QueueControl._switch_reference(connection, candidate_id, provider) != target[1]:
                    raise WorkCommandError('SOURCE_CHANGED', '目标来源在预检期间已改变', status_code=409)
                if connection.execute('SELECT 1 FROM candidate_approvals WHERE candidate_id = ?', (candidate_id,)).fetchone() is None:
                    raise WorkCommandError('CANDIDATE_NOT_DOWNLOADABLE', '没有有效审批事实，不能切源', status_code=409)
                job_id = active[1]
                params = {'provider': provider}
                if pack_requested is not None:
                    params['pack_requested'] = pack_requested
                operation = service._queue_control._apply(connection, job_id, 'switch-source', task_snapshot(connection, job_id), params)
            else:
                queued = service._enqueue_sync(candidate_id, **prepared, redownload=True, connection=connection)
                job_id = queued.job_id
            task = task_snapshot(connection, job_id)
            if pack_requested is not None and active is None:
                connection.execute('UPDATE queue_pack_intents SET pack_requested = ? WHERE task_id = ?',
                                   (int(pack_requested), task['task_id']))
            if work_version(connection, candidate_id)['revision'] == revision:
                connection.execute('UPDATE work_versions SET revision = revision + 1 WHERE candidate_id = ?', (candidate_id,))
            result = {'candidate_id': candidate_id, 'work_id': work_id,
                      'work_revision': work_version(connection, candidate_id)['revision'],
                      'provider': provider, 'job_id': job_id, 'task_id': task['task_id'],
                      'revision': task['revision'], 'task_revision': task['revision']}
            if pack_requested is not None:
                result['pack_requested'] = pack_requested
            if operation is not None:
                result.update(operation_id=operation['operation_id'], state=operation['state'])
            self._save_receipt(connection, key, work_id, digest, result)
            return result

    async def begin_metadata_fetch(self, candidate_id, payload):
        work_id, revision, key = self._preconditions(payload)
        digest = sha256(json.dumps({'action': 'fetch-metadata', 'candidate_id': candidate_id,
            'work_id': work_id, 'expected_revision': revision}, sort_keys=True).encode()).hexdigest()
        operation_id = uuid4().hex
        try:
            return await run_sync(self._begin_metadata_fetch, candidate_id, work_id, revision, key, digest, operation_id)
        except asyncio.CancelledError:
            # run_sync drains the acceptance thread. Only this dispatch's own
            # operation can be failed; cancelling a replay must not cancel its owner.
            await self.fail_metadata_fetch(operation_id, 'METADATA_FETCH_CANCELLED',
                '元数据抓取已中断，请核对状态后发起新操作', 409)
            raise

    def _downloaded_identity(self, candidate_id, action, payload):
        work_id, revision, key = self._preconditions(payload)
        if action not in {'redownload', 'repack'}:
            raise WorkCommandError('ACTION_UNKNOWN', '不支持的已下载作品动作')
        repack = payload.get('repack', False)
        if type(repack) is not bool:
            raise WorkCommandError('REPACK_INVALID', '重新打包选项必须是布尔值')
        digest = sha256(json.dumps({'action': action, 'candidate_id': candidate_id,
            'work_id': work_id, 'expected_revision': revision, 'repack': repack if action == 'redownload' else False},
            sort_keys=True, separators=(',', ':')).encode()).hexdigest()
        return work_id, revision, key, digest

    async def inspect_downloaded(self, candidate_id, action, payload):
        return self._review_result(await run_sync(self._inspect_review, candidate_id,
                                                  self._downloaded_identity(candidate_id, action, payload)))

    async def downloaded_task(self, candidate_id, action, payload, operator, conversion, prepared=None):
        result = await run_sync(self._downloaded_task, candidate_id, action, payload.get('repack', False),
            self._downloaded_identity(candidate_id, action, payload), operator, conversion, prepared)
        return self._review_result(result)

    def _downloaded_task(self, candidate_id, action, repack, identity, operator, conversion, prepared):
        from app.downloads.queue import QueueControl, task_snapshot
        from app.conversion.relocation import Relocations
        work_id, revision, key, digest = identity
        with self.database.connection() as connection:
            connection.execute('BEGIN IMMEDIATE')
            receipt = self._receipt(connection, key, digest)
            if receipt is not None:
                return receipt
            require_work_version(connection, candidate_id, work_id, revision)
            Relocations(self.database).check(candidate_id=candidate_id, connection=connection)
            if action == 'redownload':
                work = self.database._downloaded_work_sync(candidate_id, connection)
                if work is None:
                    raise WorkCommandError('WORK_NOT_DOWNLOADED', '该作品没有已下载的档案')
                from app.downloads.models import OPEN_DOWNLOAD_STATES, CONVERSION_STATE_RUNNING
                if work.state in OPEN_DOWNLOAD_STATES:
                    raise WorkCommandError('WORK_STILL_RUNNING', '该作品已有下载任务在进行，无需重新下载', status_code=409)
                if work.pack_state == CONVERSION_STATE_RUNNING:
                    raise WorkCommandError('WORK_PACK_RUNNING', '该作品正在打包，请等待打包结束再重新下载', status_code=409)
                result = QueueControl(self.database)._apply(connection, work.job_id, 'restart',
                    task_snapshot(connection, work.job_id), {'pack_requested': repack})
                result.update(provider=work.provider, repack=repack)
            else:
                if connection.execute("SELECT 1 FROM download_jobs WHERE candidate_id = ? "
                    "AND provider = 'CONVERSION' AND state IN ('CONVERSION_RUNNING', 'STOPPING')", (candidate_id,)).fetchone():
                    raise WorkCommandError('WORK_PACK_RUNNING', '该作品正在执行或停止，请等待收尾后重新打包', status_code=409)
                job_id = conversion._enqueue_sync(candidate_id, connection=connection)
                if prepared and prepared.get('path') is not None:
                    self.database.write_archive_path_pin(connection, candidate_id, prepared['path'], False, operator)
                if prepared and prepared.get('error'):
                    error = prepared['error']
                    connection.execute("UPDATE download_jobs SET state = 'CONVERSION_WAITING_PATH', "
                        'error_code = ?, error_message = ? WHERE id = ?', (error['code'], error['message'], job_id))
                    result = {'batch_error': {'code': error['code'], 'message': error['message'],
                        'status_code': 400, 'details': {'job_id': job_id}}}
                else:
                    result = {'job_id': job_id, **task_snapshot(connection, job_id)}
            if work_version(connection, candidate_id)['revision'] == revision:
                connection.execute('UPDATE work_versions SET revision = revision + 1 WHERE candidate_id = ?', (candidate_id,))
            result.update(candidate_id=candidate_id, work_id=work_id,
                          work_revision=work_version(connection, candidate_id)['revision'])
            self._save_receipt(connection, key, work_id, digest, result)
            return result

    def _begin_metadata_fetch(self, candidate_id, work_id, revision, key, digest, operation_id):
        with self.database.connection() as connection:
            connection.execute('BEGIN IMMEDIATE')
            receipt = self._receipt(connection, key, digest)
            if receipt is not None:
                return receipt, False
            require_work_version(connection, candidate_id, work_id, revision)
            if connection.execute("SELECT 1 FROM work_metadata_fetches WHERE work_id = ? AND status = 'RUNNING'",
                                  (work_id,)).fetchone():
                raise WorkCommandError('WORK_BUSY', '作品正在抓取元数据，请等待该操作结束', status_code=409)
            session = getattr(self.database, 'worker_session', None)
            if session is not None and session.stopping:
                raise WorkCommandError('WORKER_STOPPING', '后台正在停止，请稍后重试', status_code=503)
            connection.execute('INSERT INTO work_metadata_fetches(operation_id, work_id, revision, session_id) '
                'VALUES(?, ?, ?, ?)', (operation_id, work_id, revision, session.session_id if session else None))
            result = {'candidate_id': candidate_id, 'work_id': work_id, 'input_revision': revision,
                      'metadata_fetch_operation_id': operation_id}
            self._save_receipt(connection, key, work_id, digest, result)
            return result, True

    async def metadata_fetch_result(self, operation_id):
        return await run_sync(self._metadata_fetch_result, operation_id)

    def _metadata_fetch_result(self, operation_id):
        with self.database.connection() as connection:
            row = connection.execute('SELECT status, error_code, result_json, work_id, revision FROM work_metadata_fetches '
                                     'WHERE operation_id = ?', (operation_id,)).fetchone()
        if row is None:
            raise WorkCommandError('METADATA_FETCH_NOT_FOUND', '元数据抓取操作不存在', status_code=404)
        return {'metadata_fetch_operation_id': operation_id, 'status': row[0], 'error_code': row[1],
                'work_id': row[3], 'input_revision': row[4], **(json.loads(row[2]) if row[2] else {})}

    async def fail_metadata_fetch(self, operation_id, error_code, message, status_code):
        await run_sync(self._fail_metadata_fetch, operation_id, error_code, message, status_code)

    def _fail_metadata_fetch(self, operation_id, error_code, message, status_code):
        with self.database.connection() as connection:
            connection.execute("UPDATE work_metadata_fetches SET status = 'FAILED', error_code = ?, result_json = ? "
                "WHERE operation_id = ? AND status = 'RUNNING'",
                (error_code, json.dumps({'error_message': message, 'http_status': status_code}), operation_id))

    async def patch_metadata(self, candidate_id, payload, operator):
        """Save all supplied values and locks, rule evaluation and receipt together."""
        work_id, revision, key = self._preconditions(payload)
        fields, locks, fetched = (payload.get(name, {}) for name in ('fields', 'locks', 'fetched_fields'))
        if not isinstance(fields, dict) or any(not isinstance(name, str) or not isinstance(value, str)
                                               for name, value in fields.items()):
            raise WorkCommandError('FIELDS_INVALID', 'fields 必须是字段到字符串的映射')
        if not isinstance(locks, dict) or any(not isinstance(name, str) or type(value) is not bool
                                             for name, value in locks.items()):
            raise WorkCommandError('LOCKS_INVALID', 'locks 必须是字段到布尔值的映射')
        if not isinstance(fetched, dict) or any(name not in EDITABLE_ELEMENTS or not isinstance(value, str)
                                               for name, value in fetched.items()):
            raise WorkCommandError('FETCHED_FIELDS_INVALID', 'fetched_fields 必须是可编辑归档字段到字符串的映射')
        if not fields and not locks and not fetched:
            raise WorkCommandError('FIELDS_REQUIRED', '请提供要修改的字段')
        fields = {name: ReviewService.validate_manual_metadata(name, value) for name, value in fields.items()}
        if any(not editable_metadata_field(name) for name in locks):
            raise WorkCommandError('METADATA_FIELD_INVALID', '包含不支持锁定的字段')
        # Hash the complete submitted values, including original whitespace.
        # A reused key with changed content must not be accepted as a replay.
        digest = sha256(json.dumps({'action': 'metadata', 'candidate_id': candidate_id,
            'work_id': work_id, 'expected_revision': revision, 'fields': payload.get('fields', {}),
            'locks': locks, 'fetched_fields': fetched}, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
        return await run_sync(self._patch_metadata, candidate_id, work_id, revision, key,
                              digest, fields, locks, fetched, operator)

    def _patch_metadata(self, candidate_id, work_id, revision, key, digest, fields, locks, fetched, operator):
        with self.database.connection() as connection:
            connection.execute('BEGIN IMMEDIATE')
            receipt = self._receipt(connection, key, digest)
            if receipt is not None:
                return receipt
            require_work_version(connection, candidate_id, work_id, revision)
            for name, value in fields.items():
                self.database._set_manual_metadata_sync(candidate_id, operator, name, value, connection)
            for name, locked in locks.items():
                try:
                    self.database._set_metadata_lock_sync(candidate_id, operator, name, locked, connection)
                except LookupError:
                    raise WorkCommandError('METADATA_VALUE_NOT_FOUND', f'作品没有可锁定的 {name} 值') from None
            self.database._re_evaluate_candidate_metadata_rules_sync(candidate_id, connection)
            # A file-only edit still advances the work version when accepted.
            # Otherwise two different fetched-field commands could both win.
            if work_version(connection, candidate_id)['revision'] == revision:
                connection.execute('UPDATE work_versions SET revision = revision + 1 WHERE candidate_id = ?',
                                   (candidate_id,))
            sync_id = uuid4().hex
            result = {'candidate_id': candidate_id, **work_version(connection, candidate_id),
                'updated': sorted(fields), 'locked': sorted(name for name, on in locks.items() if on),
                'unlocked': sorted(name for name, on in locks.items() if not on), 'metadata_sync_id': sync_id}
            connection.execute('INSERT INTO work_metadata_syncs '
                '(sync_id, work_id, revision, fields_json, fetched_fields_json, operator_name) VALUES(?, ?, ?, ?, ?, ?)',
                (sync_id, work_id, result['revision'], json.dumps(list(fields)), json.dumps(fetched), operator))
            self._save_receipt(connection, key, work_id, digest, result)
            return result

class RenameCommand:
    """The operator command committed inside the existing relocation journal."""

    def __init__(self, commands, candidate_id, payload, conversion, *, strict=False):
        self.commands, self.candidate_id, self.conversion = commands, candidate_id, conversion
        self.strict = strict
        self.work_id, self.revision, self.key = commands._preconditions(payload)
        if any(payload.get(name) is not None and not isinstance(payload[name], str)
               for name in ('filename', 'directory')) or type(payload.get('repack', False)) is not bool:
            raise WorkCommandError('RENAME_INVALID', '文件名、目录应为字符串，重新打包选项应为布尔值')
        self.filename = (payload.get('filename') or '').strip() or None
        self.directory = (payload.get('directory') or '').strip() or None
        self.repack = payload.get('repack', False)
        self.digest = sha256(json.dumps({'action': 'archive-path' if strict else 'rename', 'candidate_id': candidate_id,
            'work_id': self.work_id, 'expected_revision': self.revision, 'filename': self.filename,
            'directory': self.directory, 'repack': self.repack},
            sort_keys=True, separators=(',', ':')).encode()).hexdigest()

    def inspect(self, connection):
        receipt = self.commands._receipt(connection, self.key, self.digest)
        if receipt is None:
            require_work_version(connection, self.candidate_id, self.work_id, self.revision)
        return receipt

    def complete(self, connection, moved):
        job_id = self.conversion._enqueue_sync(self.candidate_id, connection=connection) if self.repack else None
        if work_version(connection, self.candidate_id)['revision'] == self.revision:
            connection.execute('UPDATE work_versions SET revision = revision + 1 WHERE candidate_id = ?',
                               (self.candidate_id,))
        result = {'candidate_id': self.candidate_id, 'moved': moved['moved'],
                  'relative_path': moved['relative_path'], 'repacked': self.repack,
                  **work_version(connection, self.candidate_id)}
        if job_id is not None:
            from app.downloads.queue import task_snapshot
            result.update(job_id=job_id, task=task_snapshot(connection, job_id))
        self.commands._save_receipt(connection, self.key, self.work_id, self.digest, result)
        return result
