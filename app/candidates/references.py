"""Gallery corrections use the same identity table and P3 stop processor."""
from __future__ import annotations

import json
from uuid import uuid4

from app.candidates.entries import IngestError, snapshot
from app.candidates.identity import ingest_key
from app.candidates.merging import candidate_record, merge_candidates, needs_stop
from app.downloads.queue import QueueControl, TERMINAL, task_snapshot


def _stop_source(connection, database, candidate_id, *, task_ids=None):
    """Reserve physical stops without losing existing remote cleanup handles."""
    queue = QueueControl(database)
    rows = connection.execute(
        "SELECT id, state, provider, task_id FROM download_jobs WHERE candidate_id = ? ORDER BY id", (candidate_id,),
    ).fetchall()
    for job_id, state, provider, task_id in rows:
        if task_ids is not None and task_id not in task_ids:
            continue
        if connection.execute(
            "SELECT 1 FROM queue_operations WHERE task_id = ? AND status NOT IN ('COMPLETED', 'FAILED')", (task_id,),
        ).fetchone() is not None:
            continue
        remote = connection.execute(
            "SELECT 1 FROM queue_remote_resources WHERE task_id = ? AND status <> 'STOP_CONFIRMED'", (task_id,),
        ).fetchone() is not None
        if state == 'COMPLETED' and provider == 'EH_TORRENT' and remote:
            queue._apply(connection, job_id, 'stop-seeding', task_snapshot(connection, job_id), {})
        elif state not in TERMINAL:
            queue._apply(connection, job_id, 'cancel', task_snapshot(connection, job_id), {})
        elif remote:
            # A terminal legacy row can still own an unconfirmed remote push.
            queue._operation(connection, job_id, 'cancel', task_snapshot(connection, job_id), {})
            connection.execute("UPDATE download_jobs SET state = 'STOPPING' WHERE id = ?", (job_id,))


def request_reference(database, candidate_id: int, gallery_id: int, token: str | None) -> dict:
    """Persist a correction and its stops together; finish immediately only if safe."""
    with database.connection() as connection:
        connection.execute('BEGIN IMMEDIATE')
        return request_reference_in_transaction(connection, database, candidate_id, gallery_id, token)


def request_reference_in_transaction(connection, database, candidate_id, gallery_id, token, *, command_guard=None):
    """Allow reply/media evidence to upgrade through the same stop/identity arbiter."""
    key = ingest_key(gallery_id=gallery_id)
    source = candidate_record(connection, candidate_id)
    pending = connection.execute(
        "SELECT operation_id, gallery_id, gallery_token FROM ingest_reference_changes "
        "WHERE candidate_id = ? AND status = 'PENDING'", (candidate_id,),
    ).fetchone()
    if pending is not None:
        if pending[1:] != (gallery_id, token):
            raise IngestError('INGEST_IDENTITY_CHANGING')
        if command_guard is not None:
            raise IngestError('INGEST_IDENTITY_CHANGING')
        return _result(connection, pending[0])
    if (source['ex_gid'], source['ex_gallery_token']) == (gallery_id, token) and connection.execute(
        "SELECT 1 FROM ingest_entries WHERE unique_key = ? AND candidate_id = ? AND processing_state = 'LINKED'",
        (key, candidate_id),
    ).fetchone() is not None:
        return {'operation_id': None, 'candidate_id': candidate_id, 'status': 'COMPLETED',
                'result_candidate_id': candidate_id, 'error_code': None}
    operation_id = uuid4().hex
    connection.execute(
        "INSERT INTO ingest_reference_changes(operation_id, candidate_id, old_gid, old_token, gallery_id, gallery_token, target_key) "
        "VALUES(?, ?, ?, ?, ?, ?, ?)",
        (operation_id, candidate_id, source['ex_gid'], source['ex_gallery_token'], gallery_id, token, key),
    )
    if command_guard is not None:
        connection.execute('UPDATE ingest_reference_changes SET command_guard_json = ? WHERE operation_id = ?',
                           (json.dumps(command_guard), operation_id))
    _stop_source(connection, database, candidate_id)
    if not needs_stop(connection, candidate_id):
        _finish(connection, operation_id)
    else:
        # A processor that already owned the target key must not publish and
        # start a competing download while the related old work is stopping.
        connection.execute(
            "UPDATE ingest_entries SET processing_state = 'PENDING', revision = revision + 1, owner = NULL, "
            "lease_until = NULL, retry_at = NULL, error_code = 'INGEST_IDENTITY_CHANGING' "
            "WHERE unique_key = ? AND processing_state = 'PROCESSING'", (key,),
        )
    return _result(connection, operation_id)


def _result(connection, operation_id):
    row = connection.execute(
        "SELECT operation_id, candidate_id, status, result_candidate_id, error_code "
        "FROM ingest_reference_changes WHERE operation_id = ?", (operation_id,),
    ).fetchone()
    return dict(zip(('operation_id', 'candidate_id', 'status', 'result_candidate_id', 'error_code'), row))


def check_command_target(connection, operation_id, candidate_id, gallery_id):
    guard = connection.execute('SELECT command_guard_json FROM ingest_reference_changes WHERE operation_id = ?',
                               (operation_id,)).fetchone()[0]
    if guard is not None:
        current = connection.execute('SELECT w.work_id, w.revision FROM candidates c JOIN work_versions w ON w.candidate_id = c.id '
                                     'WHERE c.ex_gid = ? AND c.id <> ?', (gallery_id, candidate_id)).fetchone()
        actual = dict(zip(('work_id', 'revision'), current)) if current else None
        if actual != json.loads(guard)['target']:
            raise IngestError('INGEST_REFERENCE_TARGET_CHANGED')


def _finish(connection, operation_id):
    row = connection.execute(
        "SELECT candidate_id, old_gid, old_token, gallery_id, gallery_token, target_key "
        "FROM ingest_reference_changes WHERE operation_id = ? AND status = 'PENDING'", (operation_id,),
    ).fetchone()
    if row is None:
        return
    source_id, old_gid, old_token, gid, token, key = row
    source = candidate_record(connection, source_id)
    if (source['ex_gid'], source['ex_gallery_token']) != (old_gid, old_token):
        raise IngestError('INGEST_REFERENCE_CHANGED')
    if needs_stop(connection, source_id):
        raise IngestError('INGEST_MERGE_NEEDS_STOP')
    connection.execute("INSERT INTO ingest_entries(unique_key) VALUES(?) ON CONFLICT(unique_key) DO NOTHING", (key,))
    target_entry = snapshot(connection, connection.execute(
        'SELECT id FROM ingest_entries WHERE unique_key = ?', (key,),
    ).fetchone()[0])
    target = connection.execute('SELECT id FROM candidates WHERE ex_gid = ?', (gid,)).fetchone()
    target_id = int(target[0]) if target is not None else source_id
    check_command_target(connection, operation_id, source_id, gid)
    old_entries = connection.execute(
        'SELECT id, unique_key, generation FROM ingest_entries WHERE candidate_id = ? AND id <> ?',
        (source_id, target_entry['id']),
    ).fetchall()
    # The audit records the previous reference even when an explicit operator
    # correction changes a previously known gallery rather than filling one in.
    connection.execute(
        "INSERT INTO review_actions(candidate_id, action, operator_name, details_json) VALUES(?, 'INGEST_REFERENCE', 'administrator', ?)",
        (source_id, json.dumps({'operation_id': operation_id, 'old_gid': old_gid, 'gallery_id': gid})),
    )
    if target_id != source_id:
        # This is an explicit correction. Automatic source edits may not use it
        # to relabel a different known gallery; they move only their observation.
        connection.execute('UPDATE candidates SET ex_gid = NULL WHERE id = ?', (source_id,))
        merge_candidates(connection, source_id, target_id, operator='administrator')
    else:
        connection.execute(
            "UPDATE candidates SET ex_gid = ?, ex_gallery_token = ?, torrent_count = NULL, torrent_hash = NULL, "
            "updated_at = CURRENT_TIMESTAMP WHERE id = ?", (gid, token, source_id),
        )
    generation = target_entry['generation'] + int(target_entry['state'] == 'REMOVED')
    for entry_id, old_key, old_generation in old_entries:
        connection.execute(
            'UPDATE source_messages SET ingest_entry_id = ?, ingest_generation = ? WHERE ingest_entry_id = ? AND ingest_generation = ?',
            (target_entry['id'], generation, entry_id, old_generation),
        )
        # An obsolete message key is an alias; an old *gallery* key must never
        # redirect newly received posts about that other gallery to this work.
        state = 'REMOVED' if old_key.startswith('eh:gallery:') else 'MERGED'
        connection.execute(
            "UPDATE ingest_entries SET candidate_id = NULL, processing_state = ?, revision = revision + 1, owner = NULL, "
            "lease_until = NULL, retry_at = NULL, extra_json = json_set(extra_json, '$.merged_into', ?, '$.removed_at', unixepoch()) "
            "WHERE id = ?", (state, target_entry['id'], entry_id),
        )
    connection.execute(
        "UPDATE ingest_entries SET candidate_id = ?, processing_state = 'LINKED', generation = ?, revision = revision + 1, "
        "owner = NULL, lease_until = NULL, retry_at = NULL, error_code = NULL, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
        (target_id, generation, target_entry['id']),
    )
    connection.execute(
        "INSERT OR IGNORE INTO candidate_messages(candidate_id, source_message_id) "
        "SELECT ?, id FROM source_messages WHERE ingest_entry_id = ? AND ingest_generation = ? "
        "AND message_state = 'ACTIVE' AND filter_result <> 'IGNORE'", (target_id, target_entry['id'], generation),
    )
    connection.execute(
        "UPDATE ingest_reference_changes SET status = 'COMPLETED', result_candidate_id = ?, error_code = NULL, revision = revision + 1, "
        "updated_at = CURRENT_TIMESTAMP WHERE operation_id = ?", (target_id, operation_id),
    )


def process_reference_changes(database) -> list[dict]:
    """Called by the existing operation loop, including after restart; never assume stop."""
    with database.connection() as connection:
        ids = [row[0] for row in connection.execute(
            "SELECT operation_id FROM ingest_reference_changes WHERE status = 'PENDING' ORDER BY created_at, operation_id LIMIT 100",
        )]
    results = []
    for operation_id in ids:
        operation_revision = None
        try:
            with database.connection() as connection:
                connection.execute('BEGIN IMMEDIATE')
                row = connection.execute(
                    "SELECT candidate_id, revision FROM ingest_reference_changes WHERE operation_id = ? AND status = 'PENDING'", (operation_id,),
                ).fetchone()
                if row is None:
                    continue
                operation_revision = row[1]
                candidate_record(connection, row[0])
                _stop_source(connection, database, row[0])
                if needs_stop(connection, row[0]):
                    continue
                _finish(connection, operation_id)
                results.append(_result(connection, operation_id))
        except IngestError as exc:
            with database.connection() as connection:
                connection.execute(
                    "UPDATE ingest_reference_changes SET status = 'FAILED', error_code = ?, revision = revision + 1, updated_at = CURRENT_TIMESTAMP "
                    "WHERE operation_id = ? AND status = 'PENDING' AND revision = ?", (exc.code, operation_id, operation_revision),
                )
                results.append(_result(connection, operation_id))
    return results
