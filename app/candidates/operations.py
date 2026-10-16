"""Inspect and retry durable ingestion changes without replaying stale input."""
from __future__ import annotations

import hashlib
import json

from app.candidates.entries import IngestError
from app.candidates.merging import candidate_record
from app.candidates.references import _stop_source, check_command_target


def _identity(operation_id):
    kind, separator, value = operation_id.partition(':')
    if not separator or not value or kind not in {'reference', 'observation'}:
        raise IngestError('INGEST_OPERATION_NOT_FOUND')
    if kind == 'observation' and (not value.isascii() or not value.isdecimal()):
        raise IngestError('INGEST_OPERATION_NOT_FOUND')
    return kind, value


def _read(connection, operation_id):
    kind, value = _identity(operation_id)
    if kind == 'reference':
        cursor = connection.execute('SELECT * FROM ingest_reference_changes WHERE operation_id = ?', (value,))
    else:
        cursor = connection.execute('SELECT * FROM ingest_observation_changes WHERE source_message_id = ?', (value,))
    row = cursor.fetchone()
    if row is None:
        raise IngestError('INGEST_OPERATION_NOT_FOUND')
    return kind, dict(zip((column[0] for column in cursor.description), row))


def _validate_retry(connection, kind, row):
    if row['status'] != 'FAILED':
        raise IngestError('INGEST_OPERATION_NOT_FAILED')
    source = candidate_record(connection, row['candidate_id'])
    if connection.execute(
        "SELECT 1 FROM queue_removals WHERE candidate_id = ? AND status = 'PENDING'", (row['candidate_id'],),
    ).fetchone():
        raise IngestError('INGEST_CANDIDATE_REMOVING')
    if kind == 'reference':
        check_command_target(connection, row['operation_id'], row['candidate_id'], row['gallery_id'])
        if (source['ex_gid'], source['ex_gallery_token']) != (row['old_gid'], row['old_token']):
            raise IngestError('INGEST_REFERENCE_CHANGED')
        if connection.execute(
            'SELECT 1 FROM ingest_reference_changes WHERE candidate_id = ? AND rowid > '
            '(SELECT rowid FROM ingest_reference_changes WHERE operation_id = ?) LIMIT 1',
            (row['candidate_id'], row['operation_id']),
        ).fetchone():
            raise IngestError('INGEST_REFERENCE_CHANGED')
    else:
        valid = connection.execute(
            'SELECT 1 FROM source_messages sm JOIN candidate_messages cm ON cm.source_message_id = sm.id '
            'JOIN ingest_entries e ON e.id = sm.ingest_entry_id '
            "WHERE sm.id = ? AND sm.observation_revision = ? AND cm.candidate_id = ? "
            "AND e.generation = sm.ingest_generation AND e.processing_state <> 'REMOVED'",
            (row['source_message_id'], row['observation_revision'], row['candidate_id']),
        ).fetchone()
        if not valid:
            raise IngestError('INGEST_OBSERVATION_CHANGED')


def _project(connection, operation_id, kind, row):
    retryable = False
    if row['status'] == 'FAILED':
        try:
            _validate_retry(connection, kind, row)
            retryable = True
        except IngestError:
            pass
    # Never expose stored message payloads, attachment handles or gallery tokens.
    return {'operation_id': operation_id, 'kind': kind, 'revision': row['revision'],
            'candidate_id': row['candidate_id'], 'status': row['status'],
            'original_candidate_id': row.get('original_candidate_id', row['candidate_id']),
            'result_candidate_id': row['result_candidate_id'], 'error_code': row['error_code'],
            'gallery_id': row.get('gallery_id'), 'retryable': retryable,
            'updated_at': row['updated_at']}


def get_operation(database, operation_id):
    """Read a safe result even after the original candidate was merged or removed."""
    with database.connection() as connection:
        return _project(connection, operation_id, *_read(connection, operation_id))


def list_operations(database, candidate_id):
    """Return this work's operations, including changes that merged into it."""
    with database.connection() as connection:
        ids = ['reference:' + row[0] for row in connection.execute(
            'SELECT operation_id FROM ingest_reference_changes WHERE candidate_id = ? OR result_candidate_id = ?',
            (candidate_id, candidate_id),
        )]
        ids.extend('observation:' + str(row[0]) for row in connection.execute(
            'SELECT source_message_id FROM ingest_observation_changes WHERE candidate_id = ? OR result_candidate_id = ? OR original_candidate_id = ?',
            (candidate_id, candidate_id, candidate_id),
        ))
        return sorted((_project(connection, oid, *_read(connection, oid)) for oid in ids),
                      key=lambda item: (item['updated_at'], item['operation_id']), reverse=True)


def retry_operation(database, operation_id, *, expected_revision, idempotency_key):
    """CAS retry plus receipt in one transaction; the existing P3 loop executes it.

    expected_revision is the positive operation version observed by the client.
    Only FAILED operations with unchanged source identity can return to PENDING.
    A repeated command returns its original receipt before checking current state.
    """
    if type(expected_revision) is not int or expected_revision < 1:
        raise IngestError('PRECONDITION_REQUIRED')
    if not isinstance(idempotency_key, str) or not idempotency_key.strip() or len(idempotency_key) > 200:
        raise IngestError('IDEMPOTENCY_KEY_REQUIRED')
    digest = hashlib.sha256(json.dumps([operation_id, expected_revision]).encode()).hexdigest()
    with database.connection() as connection:
        connection.execute('BEGIN IMMEDIATE')
        receipt = connection.execute(
            "SELECT payload_hash, response_json FROM ingest_command_receipts WHERE scope = 'administrator' AND idempotency_key = ?",
            (idempotency_key,),
        ).fetchone()
        if receipt:
            if receipt[0] != digest:
                raise IngestError('IDEMPOTENCY_CONFLICT')
            return json.loads(receipt[1])
        kind, row = _read(connection, operation_id)
        if row['revision'] != expected_revision:
            raise IngestError('REVISION_CONFLICT')
        _validate_retry(connection, kind, row)
        if kind == 'reference':
            # A competing identity command always wins over retrying old input.
            if connection.execute(
                "SELECT 1 FROM ingest_reference_changes WHERE candidate_id = ? AND status = 'PENDING'", (row['candidate_id'],),
            ).fetchone():
                raise IngestError('INGEST_IDENTITY_CHANGING')
            connection.execute(
                "UPDATE ingest_reference_changes SET status = 'PENDING', revision = revision + 1, error_code = NULL, "
                "updated_at = CURRENT_TIMESTAMP WHERE operation_id = ?", (row['operation_id'],),
            )
            _stop_source(connection, database, row['candidate_id'])
            connection.execute(
                "UPDATE ingest_entries SET processing_state = 'PENDING', revision = revision + 1, owner = NULL, "
                "lease_until = NULL, retry_at = NULL, error_code = 'INGEST_IDENTITY_CHANGING' "
                "WHERE unique_key = ? AND processing_state = 'PROCESSING'", (row['target_key'],),
            )
        else:
            connection.execute(
                "UPDATE ingest_observation_changes SET status = 'PENDING', revision = revision + 1, error_code = NULL, "
                "updated_at = CURRENT_TIMESTAMP WHERE source_message_id = ?", (row['source_message_id'],),
            )
            connection.execute(
                "UPDATE ingest_entries SET processing_state = 'PENDING', revision = revision + 1, owner = NULL, "
                "lease_until = NULL, retry_at = NULL, error_code = 'INGEST_IDENTITY_CHANGING' "
                "WHERE unique_key = ? AND processing_state = 'PROCESSING'", (row['target_key'],),
            )
            # Stop all current tasks when an identity merge was requested; other
            # edits still stop only the tasks bound to the selected observation.
            tasks = None if row['merge_all'] else {
                task_id for task_id, details in connection.execute(
                    "SELECT task_id, details_json FROM download_jobs WHERE candidate_id = ? AND provider IN ('TELEGRAM', 'TELEGRAM_USER')",
                    (row['candidate_id'],),
                ) if any(json.loads(details or '{}').get(field) == row['source_message_id']
                         for field in ('observation_id', 'download_observation_id'))
            }
            task_ids = set(json.loads(row['task_ids_json']))
            task_ids.update(task_id for task_id, in connection.execute(
                'SELECT task_id FROM download_jobs WHERE candidate_id = ?', (row['candidate_id'],),
            ) if tasks is None or task_id in tasks)
            connection.execute('UPDATE ingest_observation_changes SET task_ids_json = ? WHERE source_message_id = ?',
                               (json.dumps(sorted(task_ids)), row['source_message_id']))
            _stop_source(connection, database, row['candidate_id'], task_ids=task_ids)
        result = _project(connection, operation_id, *_read(connection, operation_id))
        connection.execute(
            'INSERT INTO ingest_command_receipts(idempotency_key, payload_hash, response_json) VALUES(?, ?, ?)',
            (idempotency_key, digest, json.dumps(result)),
        )
        return result
