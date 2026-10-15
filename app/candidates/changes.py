"""Persist source edits until their selected download has physically stopped."""
from __future__ import annotations

from dataclasses import asdict, replace
import json

from app.candidates.entries import IngestError, load_message, message_key, snapshot
from app.candidates.models import ParsedSourceMessage
from app.candidates.persistence import deactivate_observation, edit_observation, observation_changed
from app.candidates.references import _stop_source
from app.downloads.queue import TERMINAL


def defer_observation_change(connection, database, message: ParsedSourceMessage) -> bool:
    """Save the latest edit and stop intent in the caller's admission transaction.

    True means admitted for later completion. False leaves the normal immediate
    edit path in charge. A gallery completion merges the whole unknown work;
    other edits stop only tasks that fixed this particular source observation.
    """
    source = connection.execute(
        "SELECT sm.id, sm.account_id, sm.ingest_entry_id, sm.ingest_generation, sm.observation_revision, "
        "cm.candidate_id, c.ex_gid FROM source_messages sm "
        "JOIN telegram_accounts ta ON ta.id = sm.account_id "
        "JOIN candidate_messages cm ON cm.source_message_id = sm.id JOIN candidates c ON c.id = cm.candidate_id "
        "WHERE sm.chat_id = ? AND sm.message_id = ? AND "
        "((? IS NULL AND ta.session_path = 'bot-api://configured') OR ta.external_account_id = ?)",
        (message.chat_id, message.message_id, message.account_id, message.account_id),
    ).fetchone()
    if source is None or source[2] is None:
        return False
    sid, account, eid, generation, revision, cid, gid = source
    entry = snapshot(connection, eid)
    if entry['generation'] != generation or entry['state'] == 'REMOVED':
        return False
    pending = connection.execute(
        "SELECT merge_all, task_ids_json FROM ingest_observation_changes WHERE source_message_id = ? AND status = 'PENDING'", (sid,),
    ).fetchone()
    if pending is None and message.filter_result != 'IGNORE' and not observation_changed(connection, message, sid, entry, message_key(message, account)):
        return False
    merge_all = bool(gid is None and message.ex_gid is not None and message.filter_result != 'IGNORE')
    task_ids = set(json.loads(pending[1])) if pending else set()
    merge_all = merge_all or bool(pending and pending[0])
    for task_id, state, details, provider in connection.execute(
        "SELECT task_id, state, details_json, provider FROM download_jobs WHERE candidate_id = ?", (cid,),
    ):
        selected = json.loads(details or '{}')
        if not merge_all and (provider not in {'TELEGRAM', 'TELEGRAM_USER'}
                              or sid not in (selected.get('observation_id'), selected.get('download_observation_id'))):
            continue
        remote = connection.execute(
            "SELECT 1 FROM queue_remote_resources WHERE task_id = ? AND status <> 'STOP_CONFIRMED'", (task_id,),
        ).fetchone()
        if state not in TERMINAL or remote is not None or connection.execute(
            "SELECT 1 FROM queue_operations WHERE task_id = ? AND status NOT IN ('COMPLETED', 'FAILED')", (task_id,),
        ).fetchone() is not None:
            task_ids.add(task_id)
    if not task_ids and pending is None:
        return False
    target_key = message_key(message, account) if merge_all and message.ex_gid is not None else None
    connection.execute(
        "INSERT INTO ingest_observation_changes(source_message_id, candidate_id, original_candidate_id, target_key, observation_revision, merge_all, message_json, task_ids_json) "
        "VALUES(?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT(source_message_id) DO UPDATE SET candidate_id = excluded.candidate_id, "
        "original_candidate_id = excluded.original_candidate_id, target_key = excluded.target_key, "
        "observation_revision = excluded.observation_revision, merge_all = excluded.merge_all, message_json = excluded.message_json, "
        "task_ids_json = excluded.task_ids_json, status = 'PENDING', error_code = NULL, result_candidate_id = NULL, "
        "revision = ingest_observation_changes.revision + 1, updated_at = CURRENT_TIMESTAMP",
        (sid, cid, cid, target_key, revision + 1, int(merge_all), json.dumps(asdict(message)), json.dumps(sorted(task_ids))),
    )
    connection.execute(
        "UPDATE ingest_entries SET processing_state = 'PENDING', revision = revision + 1, owner = NULL, "
        "lease_until = NULL, retry_at = NULL, error_code = 'INGEST_IDENTITY_CHANGING' "
        "WHERE unique_key = ? AND processing_state = 'PROCESSING'", (target_key,),
    )
    # Revoke publication immediately, while keeping the old attachment/identity
    # available to the stop processor until its real finally/remote cleanup ends.
    connection.execute('UPDATE source_messages SET observation_revision = observation_revision + 1 WHERE id = ?', (sid,))
    _stop_source(connection, database, cid, task_ids=task_ids)
    return True


def defer_deactivation(connection, database, chat_id, message_id, account_id) -> bool:
    row = connection.execute(
        "SELECT sm.id FROM source_messages sm JOIN telegram_accounts ta ON ta.id = sm.account_id "
        "WHERE sm.chat_id = ? AND sm.message_id = ? AND "
        "((? IS NULL AND ta.session_path = 'bot-api://configured') OR ta.external_account_id = ?)",
        (chat_id, message_id, account_id, account_id),
    ).fetchone()
    if row is None:
        return False
    message = replace(load_message(connection, row[0]), is_edit=True, filter_result='IGNORE')
    return defer_observation_change(connection, database, message)


def process_observation_changes(database) -> list[int]:
    """Reuse the P3 operation loop; pending source edits are restart recoverable."""
    with database.connection() as connection:
        ids = [row[0] for row in connection.execute(
            "SELECT source_message_id FROM ingest_observation_changes WHERE status = 'PENDING' ORDER BY updated_at, source_message_id LIMIT 100",
        )]
    changed = []
    for sid in ids:
        operation_revision = None
        try:
            with database.connection() as connection:
                connection.execute('BEGIN IMMEDIATE')
                pending = connection.execute(
                    "SELECT candidate_id, observation_revision, message_json, task_ids_json, revision FROM ingest_observation_changes "
                    "WHERE source_message_id = ? AND status = 'PENDING'", (sid,),
                ).fetchone()
                if pending is None:
                    continue
                cid, revision, payload, tasks, operation_revision = pending
                source = connection.execute(
                    'SELECT account_id, observation_revision, ingest_entry_id FROM source_messages WHERE id = ?', (sid,),
                ).fetchone()
                if source is None or source[1] != revision or connection.execute(
                    'SELECT 1 FROM candidate_messages WHERE source_message_id = ? AND candidate_id = ?', (sid, cid),
                ).fetchone() is None:
                    raise IngestError('INGEST_OBSERVATION_CHANGED')
                task_ids = set(json.loads(tasks))
                _stop_source(connection, database, cid, task_ids=task_ids)
                if any(_task_needs_stop(connection, task_id) for task_id in task_ids):
                    continue
                message = ParsedSourceMessage(**json.loads(payload))
                message = replace(message, attachments=tuple(message.attachments), preview_urls=tuple(message.preview_urls))
                if message.filter_result == 'IGNORE':
                    deactivate_observation(connection, message.chat_id, message.message_id, message.account_id)
                else:
                    entry = snapshot(connection, source[2])
                    edit_observation(connection, message, sid, entry, message_key(message, source[0]))
                connection.execute(
                    "UPDATE ingest_observation_changes SET status = 'COMPLETED', error_code = NULL, revision = revision + 1, "
                    "result_candidate_id = (SELECT candidate_id FROM candidate_messages WHERE source_message_id = ? LIMIT 1), "
                    "updated_at = CURRENT_TIMESTAMP WHERE source_message_id = ?", (sid, sid),
                )
                changed.append(cid)
                result = connection.execute(
                    'SELECT result_candidate_id FROM ingest_observation_changes WHERE source_message_id = ?', (sid,),
                ).fetchone()[0]
                if result is not None and result != cid:
                    changed.append(result)
        except IngestError as exc:
            with database.connection() as connection:
                updated = connection.execute(
                    "UPDATE ingest_observation_changes SET status = 'FAILED', error_code = ?, revision = revision + 1, updated_at = CURRENT_TIMESTAMP "
                    "WHERE source_message_id = ? AND status = 'PENDING' AND revision = ?", (exc.code, sid, operation_revision),
                ).rowcount
                if updated:
                    changed.append(cid)
    return changed


def _task_needs_stop(connection, task_id):
    row = connection.execute('SELECT status FROM queue_tasks WHERE task_id = ?', (task_id,)).fetchone()
    return (row is not None and row[0] not in TERMINAL | {'REMOVED'} or connection.execute(
        "SELECT 1 FROM queue_remote_resources WHERE task_id = ? AND status <> 'STOP_CONFIRMED' "
        "UNION ALL SELECT 1 FROM queue_operations WHERE task_id = ? AND status NOT IN ('COMPLETED', 'FAILED') LIMIT 1",
        (task_id, task_id),
    ).fetchone() is not None)
