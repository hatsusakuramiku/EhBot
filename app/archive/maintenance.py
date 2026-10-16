"""Whole-library rearchive with a fixed work set and resumable item receipts."""
from copy import copy
from dataclasses import asdict
from hashlib import sha256
import json

from app.archive.rearchive import plan_rearchive, _preview_report
from app.conversion.relocation import Relocations
from app.downloads.execution import run_sync
from app.review.batches import ReviewBatches
from app.review.commands import WorkCommands, WorkCommandError, require_work_version, work_version
from app.settings.commands import SettingsCommands


def _conditions(payload):
    revision, key = payload.get('expected_revision'), payload.get('idempotency_key')
    if type(revision) is not int or revision < 1:
        raise WorkCommandError('PRECONDITION_REQUIRED', '请提交已读取的重新归档范围版本', status_code=428)
    if not isinstance(key, str) or not key.strip() or len(key) > 128:
        raise WorkCommandError('IDEMPOTENCY_KEY_REQUIRED', '请提交 1–128 字符的幂等键', status_code=428)
    if any(type(payload.get(name, False)) is not bool for name in ('force', 'dry_run')):
        raise WorkCommandError('BODY_INVALID', 'force 与 dry_run 必须是布尔值')
    digest = sha256(json.dumps({'action': 'rearchive', 'revision': revision,
        'force': payload.get('force', False), 'dry_run': payload.get('dry_run', False)}, sort_keys=True).encode()).hexdigest()
    return revision, key, digest


def _policy(connection):
    scopes = ('template', 'directories', 'path-ai', 'path-model-source', 'ai-master',
              'ai-chain:default', 'ai-chain:archive_path', 'ai-catalogue')
    return {'versions': {row[0]: row[1] for row in connection.execute('SELECT scope, revision FROM settings_versions')
                         if row[0] in scopes},
            'rules': [list(row) for row in connection.execute('SELECT rule_uuid, version FROM archive_path_rules ORDER BY id')]}


def _path_answer(connection, candidate_id):
    row = connection.execute('SELECT fingerprint, prompt_hash, relative_path FROM ai_path_suggestions WHERE candidate_id = ?',
                             (candidate_id,)).fetchone()
    return list(row) if row else None


class RearchiveCommands:
    def __init__(self, database, archived, conversion):
        self.database, self.archived, self.conversion = database, archived, conversion
        self.batches = ReviewBatches(database)

    async def receipt(self, key):
        """Read committed outcomes only; an incomplete batch needs explicit replay."""
        return await run_sync(self._receipt, key)

    def _receipt(self, key):
        with self.database.connection() as connection:
            connection.execute('BEGIN')
            row = connection.execute('SELECT request_json, response_json FROM work_review_batches WHERE idempotency_key = ?',
                                     (key,)).fetchone()
            request = json.loads(row[0]) if row else {}
            if request.get('action') != 'rearchive':
                raise WorkCommandError('RECEIPT_NOT_FOUND', '没有此重新归档回执', status_code=404)
            outcomes = []
            for item in request['items'] if not request['dry_run'] else []:
                receipt = connection.execute('SELECT response_json FROM work_command_receipts WHERE idempotency_key = ? '
                    'UNION ALL SELECT response_json FROM work_review_refusals WHERE idempotency_key = ?',
                    (item['idempotency_key'], item['idempotency_key'])).fetchone()
                if receipt:
                    outcomes.append(json.loads(receipt[0]))
            return {'action': 'rearchive', 'idempotency_key': key, 'status': 'COMPLETED' if row[1] else 'INCOMPLETE',
                    'requested': len(request['items']), 'completed': len(outcomes), 'outcomes': outcomes,
                    'result': json.loads(row[1]) if row[1] else None}

    async def execute(self, payload, operator):
        revision, key, digest = _conditions(payload)
        request, result = await run_sync(self._inspect, revision, key, digest)
        if result is not None:
            return result
        if request is None:
            # A preview must be local and read-only. Workers fetch any missing
            # metadata later; never do duplicate network work while planning.
            planning = copy(self.conversion)
            planning._metadata_enricher = None
            plan = await plan_rearchive(self.database, self.archived, planning,
                                       force=payload.get('force', False), dry_run=True)
            request = await run_sync(self._begin, revision, key, digest, plan, payload.get('dry_run', False))
        if request.get('result') is not None:
            return request['result']
        if request['dry_run']:
            return await run_sync(self.batches._complete, key, digest, request['report'])
        report = {**request['report'], 'dry_run': False,
                  'queued': [], 'refiling': [], 'moved': [], 'unchanged': [], 'skipped': [row for row in request['report']['skipped']
                    if row['candidate_id'] not in {item['task']['candidate_id'] for item in request['items'] if item['task']['mode'] == 'park'}]}
        for item in request['items']:
            command = RearchiveItem(self.database, self.conversion, item, request['policy'])
            result = await command.execute(self.archived, operator)
            category = result['category']
            report[category].append(result['item'])
        return await run_sync(self.batches._complete, key, digest, report)

    def _inspect(self, revision, key, digest):
        with self.database.connection() as connection:
            connection.execute('BEGIN')
            row = connection.execute('SELECT payload_hash, request_json, response_json FROM work_review_batches '
                                     'WHERE idempotency_key = ?', (key,)).fetchone()
            if row:
                if row[0] != digest:
                    raise WorkCommandError('IDEMPOTENCY_CONFLICT', '此幂等键已用于其他批次', status_code=409)
                return json.loads(row[1]), json.loads(row[2]) if row[2] else None
            SettingsCommands._require(connection, 'rearchive', revision)
            return None, None

    def _begin(self, revision, key, digest, plan, dry_run):
        with self.database.connection() as connection:
            connection.execute('BEGIN IMMEDIATE')
            row = connection.execute('SELECT payload_hash, request_json, response_json FROM work_review_batches '
                                     'WHERE idempotency_key = ?', (key,)).fetchone()
            if row:
                if row[0] != digest:
                    raise WorkCommandError('IDEMPOTENCY_CONFLICT', '此幂等键已用于其他批次', status_code=409)
                return {**json.loads(row[1]), 'result': json.loads(row[2]) if row[2] else None}
            SettingsCommands._require(connection, 'rearchive', revision)
            if connection.execute('SELECT 1 FROM work_command_receipts WHERE idempotency_key = ? '
                'UNION ALL SELECT 1 FROM work_review_refusals WHERE idempotency_key = ?', (key, key)).fetchone():
                raise WorkCommandError('IDEMPOTENCY_CONFLICT', '此幂等键已用于其他作品命令', status_code=409)
            items = []
            tasks = [asdict(task) for task in plan.tasks]
            tasks.extend({'candidate_id': skipped.candidate_id, 'title': skipped.title, 'mode': 'park',
                          'code': skipped.code, 'message': skipped.message} for skipped in plan.skipped if skipped.park)
            for task in tasks:
                version = work_version(connection, task['candidate_id'])
                artifact = connection.execute('SELECT a.artifact_id FROM artifacts a JOIN download_jobs j ON j.id = a.job_id '
                    'WHERE j.candidate_id = ? AND a.artifact_type = ? ORDER BY a.id DESC LIMIT 1',
                    (task['candidate_id'], 'ARCHIVE' if task['mode'] == 'queue' else 'CBZ')).fetchone()
                items.append({'task': task, **version, 'input_artifact_id': artifact[0] if artifact else None,
                              'path_answer': _path_answer(connection, task['candidate_id']),
                              'idempotency_key': 'rearchive:' + sha256((key + ':' + str(task['candidate_id'])).encode()).hexdigest()})
            request = {'action': 'rearchive', 'items': items, 'policy': _policy(connection),
                       'dry_run': dry_run, 'report': _preview_report(plan)}
            request = json.loads(json.dumps(request))
            connection.execute('INSERT INTO work_review_batches(idempotency_key, payload_hash, request_json) VALUES(?, ?, ?)',
                               (key, digest, json.dumps(request)))
            return request


class RearchiveItem:
    """Use the existing relocation journal for moves and one transaction for enqueue."""
    strict = False

    def __init__(self, database, conversion, item, policy):
        self.database, self.conversion, self.item, self.policy = database, conversion, item, policy
        self.commands = WorkCommands(database)
        self.task = item['task']
        self.candidate_id = self.task['candidate_id']
        self.key = item['idempotency_key']
        self.digest = sha256(json.dumps({'action': 'rearchive-item', 'item': item, 'policy': policy}, sort_keys=True).encode()).hexdigest()

    def inspect(self, connection):
        receipt = self.commands._receipt(connection, self.key, self.digest)
        if receipt is not None:
            return receipt
        require_work_version(connection, self.candidate_id, self.item['work_id'], self.item['revision'])
        if _policy(connection) != self.policy:
            raise WorkCommandError('REVISION_CONFLICT', '路径配置已变化，本次未继续采用新配置', status_code=409)
        if _path_answer(connection, self.candidate_id) != self.item['path_answer']:
            raise WorkCommandError('REVISION_CONFLICT', '此作品的 AI 路径答案已变化，请重新核对', status_code=409)

    def complete(self, connection, moved):
        category = 'moved' if moved['moved'] else 'unchanged'
        result = {'category': category, 'item': {'candidate_id': self.candidate_id, 'title': self.task['title'],
            'from': self.task['current'], 'to': moved['relative_path'], 'target': moved['relative_path']}}
        self._save(connection, result)
        return result

    def _save(self, connection, result):
        if work_version(connection, self.candidate_id)['revision'] == self.item['revision']:
            connection.execute('UPDATE work_versions SET revision = revision + 1 WHERE candidate_id = ?', (self.candidate_id,))
        self.commands._save_receipt(connection, self.key, self.item['work_id'], self.digest, result)

    def _read(self):
        with self.database.connection() as connection:
            connection.execute('BEGIN')
            return self.inspect(connection)

    async def execute(self, archived, operator):
        try:
            receipt = await run_sync(self._read)
            if receipt is not None:
                return receipt
            if self.task['mode'] == 'move':
                return await archived.refile_work(self.candidate_id, self.task['target'], operator_name=operator, command=self)
            return await run_sync(self._enqueue, archived, operator)
        except Exception as exc:
            if not hasattr(exc, 'code') or not hasattr(exc, 'public_message'):
                raise
            if exc.code == 'PUBLICATION_RECOVERY_REQUIRED':
                # A concurrent copy may own the prepared move. Do not freeze a
                # refusal while that copy can still commit the same child.
                raise WorkCommandError(exc.code, exc.public_message, status_code=409) from exc
            return await run_sync(self._refuse, exc)

    def _enqueue(self, archived, operator):
        with self.database.connection() as connection:
            connection.execute('BEGIN IMMEDIATE')
            receipt = self.inspect(connection)
            if receipt is not None:
                return receipt
            Relocations(self.database).check(candidate_id=self.candidate_id, connection=connection)
            if connection.execute("SELECT 1 FROM download_jobs WHERE candidate_id = ? AND provider = 'CONVERSION' "
                                  "AND state IN ('CONVERSION_PENDING', 'CONVERSION_RUNNING', 'STOPPING')", (self.candidate_id,)).fetchone():
                raise WorkCommandError('PACK_IN_FLIGHT', '该作品已有打包或重算任务，本次不动它', status_code=409)
            if self.task['mode'] == 'park':
                archived._park_for_invalid_path_sync(self.candidate_id, self.task['code'], self.task['message'], connection)
                result = {'category': 'skipped', 'item': {key: self.task[key] for key in ('candidate_id', 'title', 'code', 'message')}}
                self._save(connection, result)
                return result
            if not self.item['input_artifact_id']:
                raise WorkCommandError('ARTIFACT_REQUIRED', '没有本次重新归档需要的产物', status_code=409)
            task = self.task
            if task['clear_pin']:
                connection.execute('DELETE FROM work_archive_paths WHERE candidate_id = ?', (self.candidate_id,))
            if task['target'] is not None:
                self.database.write_archive_path_pin(connection, self.candidate_id, task['target'], False, operator)
            job_id = self.conversion._enqueue_sync(self.candidate_id, refile=task['mode'] == 'refile',
                refresh_ai_path=task['refresh'], input_artifact_id=self.item['input_artifact_id'], connection=connection)
            result = {'category': 'refiling' if task['mode'] == 'refile' else 'queued',
                'item': {'candidate_id': self.candidate_id, 'title': task['title'], 'target': task['target'],
                         'from': task['current'], 'job_id': job_id}}
            self._save(connection, result)
            return result

    def _refuse(self, error):
        with self.database.connection() as connection:
            connection.execute('BEGIN IMMEDIATE')
            receipt = self.commands._receipt(connection, self.key, self.digest)
            if receipt is not None:
                return receipt
            result = {'category': 'skipped', 'item': {'candidate_id': self.candidate_id, 'title': self.task['title'],
                'code': error.code, 'message': error.public_message}}
            connection.execute('INSERT INTO work_review_refusals(idempotency_key, payload_hash, response_json) VALUES(?, ?, ?)',
                               (self.key, self.digest, json.dumps(result)))
            return result
