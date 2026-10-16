"""Resumable review/removal batches with immutable per-work outcomes.

Network preparation stays outside transactions. A child success or refusal is
recorded under the same key, so concurrent dispatches cannot report contradictory
outcomes and an interrupted batch can resume after already committed children.
"""
from hashlib import sha256
import json

from app.downloads.execution import run_sync
from app.downloads.service import DownloadError
from app.downloads.archived import ArchivedWorkError
from app.review.commands import WorkCommandError, WorkCommands
from app.review.application import remove_work
from app.review.application import downloaded_task
from app.conversion.convert import ConversionError
from app.conversion.naming import LibraryPathError
from app.review.service import ReviewError


class ReviewBatches:
    def __init__(self, database, orchestrator=None, *, archived=None, conversion=None):
        self.database = database
        self.orchestrator = orchestrator
        self.archived = archived
        self.conversion = conversion
        self.commands = WorkCommands(database)

    async def run(self, action, candidate_ids, versions, key, operator, *, repack=False):
        if action not in {'approve', 'reject', 'delete', 'delete-files', 'remove', 'remove-files', 'repack', 'redownload'}:
            raise WorkCommandError('ACTION_UNKNOWN', '不支持的批量作品动作')
        if not isinstance(candidate_ids, list) or not candidate_ids or len(candidate_ids) > 100:
            raise WorkCommandError('CANDIDATE_IDS_INVALID', '每批请选择 1–100 件作品')
        if any(type(value) is not int or value < 1 for value in candidate_ids) or len(set(candidate_ids)) != len(candidate_ids):
            raise WorkCommandError('CANDIDATE_IDS_INVALID', '作品编号必须是互不重复的正整数')
        if not isinstance(versions, dict):
            raise WorkCommandError('PRECONDITION_REQUIRED', '请提交每件作品的所见身份和版本', status_code=428)
        # Do not resolve missing versions from current server state.
        children = []
        for candidate_id in candidate_ids:
            observed = versions.get(str(candidate_id))
            if not isinstance(observed, dict):
                raise WorkCommandError('PRECONDITION_REQUIRED', '请提交每件作品的所见身份和版本', status_code=428)
            payload = {'work_id': observed.get('work_id'), 'expected_revision': observed.get('expected_revision'),
                       'idempotency_key': key}
            if action == 'redownload':
                payload['repack'] = repack
            self.commands._preconditions(payload)
            child_key = 'review-batch:' + sha256((key + ':' + str(candidate_id)).encode()).hexdigest()
            children.append((candidate_id, {**payload, 'idempotency_key': child_key}))
        request = {'action': action, 'children': children}
        digest = sha256(json.dumps(request, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
        receipt = await run_sync(self._begin, key, digest, request)
        if receipt is not None:
            return receipt
        applied, skipped = [], []
        for candidate_id, payload in children:
            try:
                if action in {'approve', 'reject'}:
                    result = await self.orchestrator.work_command(candidate_id, action, payload, operator)
                elif action in {'repack', 'redownload'}:
                    result = await downloaded_task(self.database, self.archived, self.conversion,
                                                   candidate_id, action, payload, operator)
                else:
                    result = await remove_work(self.database, self.archived, candidate_id, action, payload, operator)
            except (ReviewError, DownloadError, ArchivedWorkError, ConversionError, LibraryPathError) as exc:
                result = await run_sync(self._refusal, candidate_id, action, payload, exc)
            if 'batch_error' in result:
                skipped.append({'candidate_id': candidate_id, **result['batch_error']})
            else:
                applied.append(result)
        result = {'action': action, 'requested': len(children), 'applied': applied, 'skipped': skipped}
        return await run_sync(self._complete, key, digest, result)

    def _begin(self, key, digest, request):
        with self.database.connection() as connection:
            connection.execute('BEGIN IMMEDIATE')
            row = connection.execute('SELECT payload_hash, response_json FROM work_review_batches '
                                     'WHERE idempotency_key = ?', (key,)).fetchone()
            if row is not None:
                if row[0] != digest:
                    raise WorkCommandError('IDEMPOTENCY_CONFLICT', '此幂等键已用于不同的批量作品操作', status_code=409)
                return json.loads(row[1]) if row[1] else None
            # Batch and individual commands share one administrator key scope.
            if connection.execute('SELECT 1 FROM work_command_receipts WHERE idempotency_key = ? '
                                  'UNION ALL SELECT 1 FROM work_review_refusals WHERE idempotency_key = ?',
                                  (key, key)).fetchone():
                raise WorkCommandError('IDEMPOTENCY_CONFLICT', '此幂等键已用于其他作品命令', status_code=409)
            connection.execute('INSERT INTO work_review_batches(idempotency_key, payload_hash, request_json) '
                               'VALUES(?, ?, ?)', (key, digest, json.dumps(request)))

    def _refusal(self, candidate_id, action, payload, error):
        identity = (self.commands._review_identity if action in {'approve', 'reject'} else
                    self.commands._downloaded_identity if action in {'repack', 'redownload'} else
                    self.commands._removal_identity)
        _, _, key, digest = identity(candidate_id, action, payload)
        with self.database.connection() as connection:
            connection.execute('BEGIN IMMEDIATE')
            receipt = self.commands._receipt(connection, key, digest)
            if receipt is not None:
                return receipt
            result = {'batch_error': {'code': error.code, 'message': error.public_message,
                      'status_code': getattr(error, 'status_code', 400), 'details': getattr(error, 'details', {})}}
            # Missing/incorrect UUIDs can be refused too; do not attach their
            # outcomes to a reused integer candidate row or invent work records.
            connection.execute('INSERT INTO work_review_refusals(idempotency_key, payload_hash, response_json) '
                               'VALUES(?, ?, ?)', (key, digest, json.dumps(result)))
            return result

    def _complete(self, key, digest, result):
        with self.database.connection() as connection:
            connection.execute('BEGIN IMMEDIATE')
            row = connection.execute('SELECT response_json FROM work_review_batches '
                'WHERE idempotency_key = ? AND payload_hash = ?', (key, digest)).fetchone()
            if row[0] is not None:
                return json.loads(row[0])
            connection.execute('UPDATE work_review_batches SET response_json = ? WHERE idempotency_key = ?',
                               (json.dumps(result), key))
            return result
