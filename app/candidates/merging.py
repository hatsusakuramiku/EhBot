"""Merge proven identities without discarding operator or execution history."""
from __future__ import annotations

import json

from app.candidates.entries import IngestError


def candidate_record(connection, candidate_id: int) -> dict:
    cursor = connection.execute("SELECT * FROM candidates WHERE id = ?", (candidate_id,))
    row = cursor.fetchone()
    if row is None:
        raise IngestError("INGEST_CANDIDATE_NOT_FOUND")
    return dict(zip((column[0] for column in cursor.description), row))


def needs_stop(connection, candidate_id: int) -> bool:
    """Include live tasks, unresolved remote resources and durable cleanup."""
    return connection.execute(
        "SELECT 1 FROM queue_tasks WHERE candidate_id = ? AND status NOT IN "
        "('COMPLETED', 'FAILED', 'CANCELLED', 'REMOVED', 'CONVERSION_COMPLETED', 'CONVERSION_FAILED') "
        "UNION ALL SELECT 1 FROM queue_remote_resources r JOIN queue_tasks t ON t.task_id = r.task_id "
        "WHERE t.candidate_id = ? AND r.status <> 'STOP_CONFIRMED' "
        "UNION ALL SELECT 1 FROM queue_operations WHERE candidate_id = ? AND status NOT IN ('COMPLETED', 'FAILED') "
        "UNION ALL SELECT 1 FROM queue_removals WHERE candidate_id = ? AND status = 'PENDING' LIMIT 1",
        (candidate_id,) * 4,
    ).fetchone() is not None


def merge_candidates(connection, source_id: int, target_id: int, *, operator: str) -> None:
    """Move stopped source history in a caller-owned write transaction.

    The target's current execution is retained. The source must have no live
    task or unconfirmed resource; the runtime coordinator must stop it first.
    Conflicting manual fields, pins or approve/reject decisions refuse the
    entire transaction. Original candidate and derived facts remain in one
    audit row, while jobs, artifacts and immutable attempt identities survive.
    """
    if source_id == target_id:
        return
    source = candidate_record(connection, source_id)
    target = candidate_record(connection, target_id)
    if source["ex_gid"] is not None and target["ex_gid"] not in (None, source["ex_gid"]):
        raise IngestError("INGEST_GALLERY_CONFLICT")
    if needs_stop(connection, source_id):
        raise IngestError("INGEST_MERGE_NEEDS_STOP")
    approved = {row[0] for row in connection.execute(
        "SELECT candidate_id FROM candidate_approvals WHERE candidate_id IN (?, ?)", (source_id, target_id))}
    if approved and (source["status"] == "REJECTED" or target["status"] == "REJECTED"):
        raise IngestError("INGEST_REVIEW_CONFLICT")
    if connection.execute(
        "SELECT 1 FROM metadata_values a JOIN metadata_values b ON a.field_name = b.field_name "
        "WHERE a.candidate_id = ? AND b.candidate_id = ? AND (a.is_manual = 1 OR a.is_locked = 1) "
        "AND (b.is_manual = 1 OR b.is_locked = 1) "
        "AND a.field_value <> b.field_value LIMIT 1", (source_id, target_id),
    ).fetchone() is not None:
        raise IngestError("INGEST_MANUAL_METADATA_CONFLICT")
    pins = {row[0]: row[1:] for row in connection.execute(
        "SELECT candidate_id, relative_path, is_manual, operator_name, created_at, updated_at "
        "FROM work_archive_paths WHERE candidate_id IN (?, ?)", (source_id, target_id))}
    if source_id in pins and target_id in pins and pins[source_id][1] and pins[target_id][1] and pins[source_id][0] != pins[target_id][0]:
        raise IngestError("INGEST_MANUAL_PATH_CONFLICT")
    metadata = connection.execute(
        "SELECT field_name, field_value, value_source, confidence, is_manual, is_locked FROM metadata_values WHERE candidate_id = ?",
        (source_id,),
    ).fetchall()
    suggestion = connection.execute("SELECT * FROM ai_path_suggestions WHERE candidate_id = ?", (source_id,)).fetchone()
    connection.execute(
        "INSERT INTO review_actions(candidate_id, action, operator_name, details_json) VALUES(?, 'INGEST_MERGE', ?, ?)",
        (target_id, operator, json.dumps({"source_candidate": source, "metadata": metadata,
                                         "archive_path": pins.get(source_id), "ai_path": suggestion}, ensure_ascii=False)),
    )
    connection.execute(
        "INSERT INTO metadata_values(candidate_id, field_name, field_value, value_source, confidence, is_manual, is_locked, created_at) "
        "SELECT ?, field_name, field_value, value_source, confidence, is_manual, is_locked, created_at FROM metadata_values WHERE candidate_id = ? "
        "ON CONFLICT(candidate_id, field_name, value_source) DO UPDATE SET field_value = excluded.field_value, "
        "confidence = excluded.confidence, is_manual = excluded.is_manual, "
        "is_locked = max(metadata_values.is_locked, excluded.is_locked) "
        "WHERE excluded.is_manual > metadata_values.is_manual OR "
        "(metadata_values.is_manual = 0 AND excluded.is_locked > metadata_values.is_locked) OR "
        "(excluded.field_value = metadata_values.field_value AND excluded.is_locked > metadata_values.is_locked)",
        (target_id, source_id),
    )
    connection.execute("DELETE FROM work_archive_paths WHERE candidate_id = ?", (source_id,))
    if source_id in pins and (target_id not in pins or pins[source_id][1] > pins[target_id][1]):
        connection.execute(
            "INSERT INTO work_archive_paths(candidate_id, relative_path, is_manual, operator_name, created_at, updated_at) "
            "VALUES(?, ?, ?, ?, ?, ?) ON CONFLICT(candidate_id) DO UPDATE SET relative_path = excluded.relative_path, "
            "is_manual = excluded.is_manual, operator_name = excluded.operator_name, updated_at = excluded.updated_at",
            (target_id, *pins[source_id]),
        )
    columns = "fingerprint, prompt_hash, relative_path, directory, filename, provider_id, model_name, attempts, created_at, updated_at"
    connection.execute(f"INSERT OR IGNORE INTO ai_path_suggestions(candidate_id, {columns}) SELECT ?, {columns} "
                       "FROM ai_path_suggestions WHERE candidate_id = ?", (target_id, source_id))
    connection.execute("INSERT OR IGNORE INTO candidate_approvals(candidate_id, evidence, approved_at) "
                       "SELECT ?, evidence, approved_at FROM candidate_approvals WHERE candidate_id = ?", (target_id, source_id))
    connection.execute("INSERT OR IGNORE INTO candidate_messages(candidate_id, source_message_id) "
                       "SELECT ?, source_message_id FROM candidate_messages WHERE candidate_id = ?", (target_id, source_id))
    connection.execute("UPDATE review_actions SET candidate_id = ? WHERE candidate_id = ?", (target_id, source_id))
    connection.execute("UPDATE download_jobs SET candidate_id = ? WHERE candidate_id = ?", (target_id, source_id))
    connection.execute("UPDATE queue_tasks SET candidate_id = ? WHERE candidate_id = ?", (target_id, source_id))
    connection.execute("UPDATE queue_operations SET candidate_id = ? WHERE candidate_id = ?", (target_id, source_id))
    connection.execute("UPDATE ingest_observation_changes SET candidate_id = ?, revision = revision + 1 WHERE candidate_id = ? AND status = 'PENDING'",
                       (target_id, source_id))
    connection.execute("UPDATE ingest_entries SET candidate_id = ?, revision = revision + 1 WHERE candidate_id = ?", (target_id, source_id))
    if source_id in approved and target["status"] in {"DISCOVERED", "PENDING_REVIEW", "NEEDS_INFO"}:
        connection.execute("UPDATE candidates SET status = 'APPROVED' WHERE id = ?", (target_id,))
    for column in ("preview_url", "thumb_url", "torrent_count", "torrent_hash", "magnet_url"):
        if target.get(column) is None and source.get(column) is not None:
            connection.execute(f"UPDATE candidates SET {column} = ? WHERE id = ?", (source[column], target_id))
    connection.execute("DELETE FROM candidates WHERE id = ?", (source_id,))
    if target["ex_gid"] is None and source["ex_gid"] is not None:
        connection.execute("UPDATE candidates SET ex_gid = ?, ex_gallery_token = ? WHERE id = ?",
                           (source["ex_gid"], source["ex_gallery_token"], target_id))
    elif target["ex_gallery_token"] is None and source["ex_gid"] == target["ex_gid"]:
        connection.execute("UPDATE candidates SET ex_gallery_token = ? WHERE id = ?", (source["ex_gallery_token"], target_id))
