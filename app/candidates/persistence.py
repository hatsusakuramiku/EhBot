"""Candidate publication under the ingestion identity's short write transaction."""
from __future__ import annotations

import json
import time
from uuid import uuid4

from app.candidates.entries import (
    IngestError, check_claim, claim_entry, finish_claim, load_message, register_observation, snapshot,
)
from app.candidates.models import ParsedSourceMessage


def gallery_candidate(connection, gallery_id: int | None) -> int | None:
    """Find one gallery identity; ambiguous historical rows require reconciliation."""
    if gallery_id is None:
        return None
    rows = connection.execute("SELECT id FROM candidates WHERE ex_gid = ? ORDER BY id LIMIT 2", (gallery_id,)).fetchall()
    if len(rows) > 1:
        raise IngestError("INGEST_HISTORICAL_GALLERY_CONFLICT")
    return int(rows[0][0]) if rows else None


def _has_persistent_work(connection, candidate_id: int) -> bool:
    return connection.execute(
        "SELECT 1 FROM download_jobs WHERE candidate_id = ? UNION ALL "
        "SELECT 1 FROM candidate_approvals WHERE candidate_id = ? UNION ALL "
        "SELECT 1 FROM review_actions WHERE candidate_id = ? LIMIT 1",
        (candidate_id, candidate_id, candidate_id),
    ).fetchone() is not None


def observation_changed(connection, message: ParsedSourceMessage, source_id: int, entry: dict, key: str) -> bool:
    """Compare the fixed processing input, excluding replay-only transport flags."""
    saved = connection.execute(
        "SELECT message_text, attachment_json, preview_urls_json, ex_gid, ex_gallery_token, sender_id, "
        "reply_to_message_id, peer_type, origin_channel_id, origin_message_id, chat_title FROM source_messages WHERE id = ?",
        (source_id,),
    ).fetchone()
    previous_input = (saved[0] or "", tuple(json.loads(saved[1])), tuple(json.loads(saved[2] or '[]')), *saved[3:])
    new_input = (message.message_text, message.attachments, message.preview_urls, message.ex_gid, message.ex_gallery_token,
                 message.sender_id, message.reply_to_message_id, message.peer_type, message.origin_channel_id, message.origin_message_id,
                 message.chat_title)
    return key != entry['unique_key'] or previous_input != new_input


def edit_observation(connection, message: ParsedSourceMessage, source_id: int, entry: dict, key: str) -> None:
    """Move this source when its gallery changes; preserve reviewed works and other sources."""
    if entry["state"] == "PROCESSING":
        raise IngestError("INGEST_INPUT_BUSY")
    if entry["state"] == "REMOVED":
        return  # An edit of a removed original is not a genuinely new post.
    if not observation_changed(connection, message, source_id, entry, key):
        return
    previous_cid = entry["candidate_id"]
    previous_gid = None
    if previous_cid is not None:
        previous_gid = connection.execute("SELECT ex_gid FROM candidates WHERE id = ?", (previous_cid,)).fetchone()[0]
    target = entry
    if key != entry["unique_key"]:
        connection.execute("INSERT INTO ingest_entries(unique_key) VALUES(?) ON CONFLICT(unique_key) DO NOTHING", (key,))
        target_id = int(connection.execute("SELECT id FROM ingest_entries WHERE unique_key = ?", (key,)).fetchone()[0])
        target = snapshot(connection, target_id)
        existing = gallery_candidate(connection, message.ex_gid)
        if previous_cid is not None and previous_gid is None and message.ex_gid is not None:
            from app.candidates.merging import merge_candidates, needs_stop
            if needs_stop(connection, previous_cid):
                raise IngestError("INGEST_MERGE_NEEDS_STOP")
            if target['state'] == 'REMOVED':
                raise IngestError('INGEST_TARGET_REMOVED')
            if existing is not None:
                merge_candidates(connection, previous_cid, existing, operator='source-edit')
            else:
                connection.execute("UPDATE candidates SET ex_gid = ?, ex_gallery_token = ? WHERE id = ?",
                                   (message.ex_gid, message.ex_gallery_token, previous_cid))
                existing = previous_cid
            connection.execute(
                "UPDATE source_messages SET ingest_entry_id = ?, ingest_generation = ? WHERE ingest_entry_id = ? "
                "AND ingest_generation = ?", (target_id, target["generation"], entry["id"], entry["generation"]),
            )
            connection.execute(
                "UPDATE ingest_entries SET processing_state = 'MERGED', revision = revision + 1, owner = NULL, "
                "lease_until = NULL, extra_json = json_set(extra_json, '$.merged_into', ?) WHERE id = ?",
                (target_id, entry["id"]),
            )
            connection.execute(
                "UPDATE ingest_entries SET candidate_id = ?, processing_state = 'LINKED', revision = revision + 1, "
                "owner = NULL, lease_until = NULL, retry_at = NULL, error_code = NULL WHERE id = ?",
                (existing, target_id),
            )
        else:
            connection.execute("DELETE FROM candidate_messages WHERE source_message_id = ?", (source_id,))
            connection.execute("UPDATE source_messages SET ingest_entry_id = ?, ingest_generation = ? WHERE id = ?",
                               (target_id, target["generation"], source_id))
            # A reviewed/published work is kept even if its last observation moved.
            if previous_cid is not None and not _has_persistent_work(connection, previous_cid):
                count = connection.execute("SELECT count(*) FROM candidate_messages WHERE candidate_id = ?", (previous_cid,)).fetchone()[0]
                if count == 0:
                    connection.execute("DELETE FROM candidates WHERE id = ?", (previous_cid,))
        target = snapshot(connection, target_id)
    connection.execute(
        "UPDATE source_messages SET sender_id = ?, reply_to_message_id = ?, message_text = ?, "
        "attachment_json = ?, file_unique_id = ?, filter_result = ?, filter_reason = ?, preview_urls_json = ?, "
        "peer_type = ?, origin_channel_id = ?, origin_message_id = ?, ex_gid = ?, ex_gallery_token = ?, "
        "gallery_from_preview = ?, rule_source_id = ?, is_forwarded = ?, chat_title = ?, observation_revision = observation_revision + 1, message_state = 'ACTIVE' WHERE id = ?",
        (message.sender_id, message.reply_to_message_id, message.message_text,
         json.dumps(message.attachments, ensure_ascii=False), message.file_unique_id, message.filter_result, message.filter_reason,
         json.dumps(message.preview_urls), message.peer_type, message.origin_channel_id, message.origin_message_id,
         message.ex_gid, message.ex_gallery_token, int(message.gallery_from_preview), message.rule_source_id, int(message.is_forwarded),
         message.chat_title, source_id),
    )
    connection.execute("UPDATE ingest_entries SET input_source_message_id = ? WHERE id = ? AND input_source_message_id IS NULL",
                       (source_id, target["id"]))
    if target["state"] in {"IGNORED", "BLOCKED"}:
        connection.execute(
            "UPDATE ingest_entries SET processing_state = 'PENDING', revision = revision + 1, attempts = 0, "
            "retry_at = NULL, error_code = NULL, input_source_message_id = ? WHERE id = ?", (source_id, target["id"]),
        )
    if target["state"] == "LINKED":
        connection.execute(
            "INSERT OR IGNORE INTO candidate_messages(candidate_id, source_message_id) "
            "SELECT ?, id FROM source_messages WHERE ingest_entry_id = ? AND ingest_generation = ? "
            "AND message_state = 'ACTIVE' AND filter_result <> 'IGNORE'",
            (target['candidate_id'], target['id'], target['generation']),
        )
        # Preview is derived from still-active sources, not overwritten by an edit from another account.
        rows = connection.execute(
            "SELECT sm.preview_urls_json FROM source_messages sm JOIN candidate_messages cm ON cm.source_message_id = sm.id "
            "WHERE cm.candidate_id = ? AND sm.message_state = 'ACTIVE' ORDER BY sm.id", (target["candidate_id"],),
        ).fetchall()
        preview = next((urls[0] for row in rows if (urls := json.loads(row[0] or '[]'))), None)
        connection.execute("UPDATE candidates SET preview_url = ? WHERE id = ?", (preview, target["candidate_id"]))
        status = connection.execute("SELECT status FROM candidates WHERE id = ?", (target["candidate_id"],)).fetchone()[0]
        if status in {"DISCOVERED", "PENDING_REVIEW", "NEEDS_INFO"}:
            from app.db.database import Database
            Database._update_candidate_filter_state(connection, target["candidate_id"], message.filter_reason)


def _related_candidate(connection, message: ParsedSourceMessage, source_id: int) -> int | None:
    """Use supplemental relations only when they cannot contradict a gallery."""
    account = connection.execute("SELECT account_id FROM source_messages WHERE id = ?", (source_id,)).fetchone()[0]
    predicates = []
    parameters = [account, message.chat_id, source_id, message.ex_gid, message.ex_gid]
    if message.media_group_id is not None:
        predicates.append("sm.media_group_id = ?")
        parameters.append(message.media_group_id)
    if message.reply_to_message_id is not None:
        predicates.append("sm.message_id = ?")
        parameters.append(message.reply_to_message_id)
    if predicates:
        rows = connection.execute(
            "SELECT DISTINCT cm.candidate_id FROM candidate_messages cm "
            "JOIN source_messages sm ON sm.id = cm.source_message_id JOIN candidates c ON c.id = cm.candidate_id "
            "WHERE sm.account_id = ? AND sm.chat_id = ? AND sm.id <> ? AND sm.message_state = 'ACTIVE' "
            "AND (? IS NULL OR c.ex_gid IS NULL OR c.ex_gid = ?) AND (" + " OR ".join(predicates) + ")",
            parameters,
        ).fetchall()
        if len(rows) == 1:
            return int(rows[0][0])
        if rows:
            return None
    if not message.attachments:
        return None
    rows = connection.execute(
        "SELECT cm.candidate_id, sm.attachment_json FROM candidate_messages cm "
        "JOIN source_messages sm ON sm.id = cm.source_message_id JOIN candidates c ON c.id = cm.candidate_id "
        "WHERE sm.account_id = ? AND sm.chat_id = ? AND sm.sender_id IS ? AND sm.message_state = 'ACTIVE' "
        "AND ABS(sm.message_id - ?) = 1 AND ABS(strftime('%s', sm.message_date) - strftime('%s', ?)) <= 180 "
        "AND (? IS NULL OR c.ex_gid IS NULL OR c.ex_gid = ?) ORDER BY sm.id LIMIT 10",
        (account, message.chat_id, message.sender_id, message.message_id, message.message_date, message.ex_gid, message.ex_gid),
    ).fetchall()
    matches = set()
    kind = message.attachments[0].get("type")
    for cid, attachments in rows:
        parsed = json.loads(attachments)
        if parsed and {kind, parsed[0].get("type")} == {"photo", "archive"}:
            matches.add(int(cid))
    return matches.pop() if len(matches) == 1 else None


def prepare_observation(connection, message: ParsedSourceMessage, *, owner: str,
                        qualified: bool = True, active_entry_ids=()) -> tuple[dict, object | None]:
    """Register before paid processing; linked duplicates only supplement sources."""
    entry = register_observation(connection, message)
    if (entry["state"] == "LINKED" or gallery_candidate(connection, message.ex_gid) is not None) and not qualified:
        raise IngestError("INGEST_SOURCE_UNQUALIFIED")
    if entry["id"] in active_entry_ids and entry["state"] != "LINKED":
        return entry, None
    reselect_pending_input(connection, entry["id"])
    entry = {**snapshot(connection, entry["id"]), "source_message_id": entry["source_message_id"]}
    if entry["state"] == "PENDING":
        existing = gallery_candidate(connection, message.ex_gid)
        if existing is not None:
            claim = claim_entry(connection, entry["id"], owner=owner)
            if claim is not None:
                finish_claim(connection, claim, candidate_id=existing)
                entry = {**snapshot(connection, entry["id"]), "source_message_id": entry["source_message_id"]}
    if entry["state"] == "LINKED":
        return entry, None
    from app.connections.runtime import current_account_context
    context = current_account_context.get()
    if (context is not None and entry["input_source_message_id"] is not None
            and load_message(connection, entry["input_source_message_id"]).account_id != context.account_id):
        return entry, None
    return entry, claim_entry(connection, entry["id"], owner=owner)


def reselect_pending_input(connection, entry_id):
    """Keep an eligible original stable; a failed, now-ineligible input may yield.

    Caller holds the write transaction and excludes locally live processors,
    including their cleanup. Expired abandoned claims yield with a new revision;
    recovery and newly delivered observations use this same input choice.
    """
    entry = snapshot(connection, entry_id)
    if entry["state"] == "PROCESSING" and entry["lease_until"] <= int(time.time()):
        connection.execute(
            "UPDATE ingest_entries SET processing_state = 'PENDING', revision = revision + 1, "
            "owner = NULL, lease_until = NULL, retry_at = NULL WHERE id = ?", (entry_id,),
        )
        entry = snapshot(connection, entry_id)
    if (entry["state"] != "PENDING" or entry["input_source_message_id"] is None
            or source_still_eligible(connection, entry["input_source_message_id"])):
        return
    for (source_id,) in connection.execute(
        "SELECT id FROM source_messages WHERE ingest_entry_id = ? AND ingest_generation = ? "
        "AND message_state = 'ACTIVE' AND filter_result <> 'IGNORE' ORDER BY id",
        (entry_id, entry["generation"]),
    ).fetchall():
        if source_still_eligible(connection, source_id):
            connection.execute(
                "UPDATE ingest_entries SET input_source_message_id = ?, revision = revision + 1, "
                "attempts = 0, retry_at = NULL, error_code = NULL WHERE id = ?", (source_id, entry_id),
            )
            return


def source_still_eligible(connection, source_message_id):
    """Recheck account, subscription and attachment constraints without provider calls."""
    from app.db.database import Database
    from app.candidates.rules import evaluate_source_rules
    row = connection.execute(
        "SELECT ts.id, ts.source_type, ts.chat_id, ts.display_name, ts.enabled, ts.rules_json "
        "FROM source_messages sm JOIN telegram_sources ts ON (ts.id = sm.rule_source_id OR (sm.rule_source_id IS NULL AND ts.account_id = sm.account_id)) AND ts.chat_id = sm.chat_id "
        "JOIN telegram_accounts ta ON ta.id = sm.account_id "
        "LEFT JOIN external_accounts ea ON ea.account_id = ta.external_account_id "
        "WHERE sm.id = ? AND sm.message_state = 'ACTIVE' "
        "AND (ta.external_account_id IS NULL OR (ea.enabled = 1 AND ea.deleted_at IS NULL "
        "AND ea.stop_operation_id IS NULL AND ea.verified_credential_revision = ea.credential_revision))",
        (source_message_id,),
    ).fetchone()
    return row is not None and evaluate_source_rules(
        Database._source_from_row(row), load_message(connection, source_message_id),
    ).result != "IGNORE"


def publish_candidate(connection, database, claim, message: ParsedSourceMessage) -> tuple[int | None, bool]:
    """Publish the owner's decision and every observation atomically; never reapprove a duplicate."""
    check_claim(connection, claim)
    connection.execute(
        "UPDATE source_messages SET filter_result = ?, filter_reason = ? WHERE id = ?",
        (message.filter_result, message.filter_reason, claim.source_message_id),
    )
    if message.filter_result == "IGNORE":
        finish_claim(connection, claim, candidate_id=None)
        return None, False
    cid = gallery_candidate(connection, message.ex_gid)
    if cid is None:
        cid = _related_candidate(connection, message, claim.source_message_id)
        if cid is not None and message.ex_gid is not None:
            previous_gid = connection.execute('SELECT ex_gid FROM candidates WHERE id = ?', (cid,)).fetchone()[0]
            if previous_gid is None:
                from app.candidates.entries import fail_claim
                from app.candidates.references import request_reference_in_transaction
                fail_claim(connection, claim, error_code='INGEST_IDENTITY_CHANGING', retry_seconds=0)
                result = request_reference_in_transaction(connection, database, cid, message.ex_gid, message.ex_gallery_token)
                # The shared reference publisher either linked every observation
                # atomically or reserved P3 stops. Never relabel an active work.
                return result['result_candidate_id'], False
    created = cid is None
    if created:
        cid = int(connection.execute(
            "INSERT INTO candidates(status, ex_gid, ex_gallery_token, filter_result, filter_reason, preview_url) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            ("NEEDS_INFO" if message.filter_result == "NEEDS_INFO" else "PENDING_REVIEW",
             message.ex_gid, message.ex_gallery_token, message.filter_result, message.filter_reason,
             message.preview_urls[0] if message.preview_urls else None),
        ).lastrowid)
    else:
        connection.execute(
            "UPDATE candidates SET ex_gid = ?, ex_gallery_token = ?, updated_at = CURRENT_TIMESTAMP "
            "WHERE id = ? AND ex_gid IS NULL AND ? IS NOT NULL",
            (message.ex_gid, message.ex_gallery_token, cid, message.ex_gid),
        )
        if message.preview_urls:
            connection.execute("UPDATE candidates SET preview_url = ? WHERE id = ? AND preview_url IS NULL",
                               (message.preview_urls[0], cid))
    finish_claim(connection, claim, candidate_id=cid)
    status = connection.execute("SELECT status FROM candidates WHERE id = ?", (cid,)).fetchone()[0]
    if created or status in {"DISCOVERED", "PENDING_REVIEW", "NEEDS_INFO"}:
        database._update_candidate_filter_state(connection, cid, message.filter_reason)
    return cid, created


def save_decided_message(database, update_id: int | None, message: ParsedSourceMessage) -> bool:
    """Thin compatibility adapter for callers that already made the admission decision."""
    with database.connection() as connection:
        connection.execute("BEGIN IMMEDIATE")
        created = False
        from app.candidates.changes import defer_observation_change
        deferred = message.is_edit and defer_observation_change(connection, database, message)
        if not deferred:
            entry, claim = prepare_observation(connection, message, owner=uuid4().hex,
                                               active_entry_ids=database.ingest_processors.active_ids())
            if claim is not None:
                _, created = publish_candidate(connection, database, claim, message)
        connection.execute(
            "UPDATE telegram_bot_updates SET processed_at = CURRENT_TIMESTAMP, processing_result = ?, processing_reason = ? "
            "WHERE update_id = ? AND account_scope = ?",
            (message.filter_result, message.filter_reason, update_id, database._telegram_update_scope()[0]),
        )
        return created


def deactivate_observation(connection, chat_id: int, message_id: int, account_id: str | None) -> None:
    """Detach an invalidated source; preserve approvals, gallery identity and published work."""
    row = connection.execute(
        "SELECT sm.id, sm.ingest_entry_id FROM source_messages sm JOIN telegram_accounts ta ON ta.id = sm.account_id "
        "WHERE sm.chat_id = ? AND sm.message_id = ? AND "
        "((? IS NULL AND ta.session_path = 'bot-api://configured') OR ta.external_account_id = ?)",
        (chat_id, message_id, account_id, account_id),
    ).fetchone()
    if row is None:
        return
    entry = snapshot(connection, row[1]) if row[1] is not None else None
    if entry is not None and entry["state"] == "PROCESSING":
        raise IngestError("INGEST_INPUT_BUSY")
    candidates = connection.execute("SELECT candidate_id FROM candidate_messages WHERE source_message_id = ?", (row[0],)).fetchall()
    connection.execute("DELETE FROM candidate_messages WHERE source_message_id = ?", (row[0],))
    connection.execute(
        "UPDATE source_messages SET message_state = 'DELETED', observation_revision = observation_revision + 1, "
        "filter_result = 'IGNORE', filter_reason = 'Source edited out' WHERE id = ?", (row[0],),
    )
    for (cid,) in candidates:
        remaining = connection.execute("SELECT count(*) FROM candidate_messages WHERE candidate_id = ?", (cid,)).fetchone()[0]
        if remaining == 0 and not _has_persistent_work(connection, cid):
            connection.execute("DELETE FROM candidates WHERE id = ?", (cid,))
    if entry is not None and entry["input_source_message_id"] == row[0] and entry["state"] == "PENDING":
        connection.execute(
            "UPDATE ingest_entries SET processing_state = 'IGNORED', revision = revision + 1, retry_at = NULL WHERE id = ?",
            (entry["id"],),
        )
