"""Short ingestion transactions; supplier calls must run outside these locks."""
from __future__ import annotations

import json
import sqlite3
import time
from dataclasses import dataclass, replace

from app.candidates.identity import ingest_key
from app.candidates.models import ParsedSourceMessage
from app.db.database import Database


class IngestError(ValueError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True, slots=True)
class IngestClaim:
    entry_id: int
    revision: int
    generation: int
    owner: str
    source_message_id: int | None
    observation_revision: int | None


def snapshot(connection: sqlite3.Connection, entry_id: int) -> dict:
    row = connection.execute(
        "SELECT id, unique_key, candidate_id, processing_state, revision, generation, "
        "owner, lease_until, input_source_message_id, attempts, retry_at, error_code, extra_json "
        "FROM ingest_entries WHERE id = ?", (entry_id,),
    ).fetchone()
    if row is None:
        raise IngestError("INGEST_NOT_FOUND")
    return dict(zip(("id", "unique_key", "candidate_id", "state", "revision", "generation",
                     "owner", "lease_until", "input_source_message_id", "attempts", "retry_at",
                     "error_code", "extra_json"), row))


def observing_account(connection: sqlite3.Connection, account_id: str | None) -> int:
    """Map a proven account UUID; leave historical/unknown accounts unassigned."""
    if account_id is None:
        return Database._ensure_bot_account(connection)
    account = connection.execute(
        "SELECT kind, label FROM external_accounts WHERE account_id = ? AND deleted_at IS NULL",
        (account_id,),
    ).fetchone()
    if account is None or account[0] not in {"TG_BOT", "TG_USER"}:
        raise IngestError("INGEST_ACCOUNT_INVALID")
    connection.execute(
        "INSERT INTO telegram_accounts(account_type, display_name, session_path, external_account_id) "
        "VALUES (?, ?, ?, ?) ON CONFLICT(external_account_id) "
        "WHERE external_account_id IS NOT NULL DO NOTHING",
        ("BOT" if account[0] == "TG_BOT" else "USER", account[1], f"account://{account_id}", account_id),
    )
    return int(connection.execute(
        "SELECT id FROM telegram_accounts WHERE external_account_id = ?", (account_id,),
    ).fetchone()[0])


def message_key(message: ParsedSourceMessage, legacy_account_id: int | None = None) -> str:
    """Use the same identity normalizer for every transport and legacy input."""
    return ingest_key(
        gallery_id=message.ex_gid,
        origin_channel_id=message.origin_channel_id, origin_message_id=message.origin_message_id,
        channel_id=message.chat_id if message.peer_type in {"channel", "supergroup"} else None,
        message_id=message.message_id, peer_id=message.chat_id,
        account_id=message.account_id or (f"legacy:{legacy_account_id}" if legacy_account_id is not None else None),
    )


def resolve_message_identity(connection: sqlite3.Connection, message: ParsedSourceMessage) -> ParsedSourceMessage:
    """Resolve a complete original-post reference through its already-recorded gallery key."""
    if message.ex_gid is not None or message.origin_channel_id is None or message.origin_message_id is None:
        return message
    origin_key = ingest_key(channel_id=message.origin_channel_id, message_id=message.origin_message_id)
    channel_id = int(origin_key.split(":")[2])
    rows = connection.execute(
        "SELECT DISTINCT e.unique_key, sm.ex_gallery_token FROM source_messages sm "
        "JOIN ingest_entries e ON e.id = sm.ingest_entry_id WHERE "
        "((sm.peer_type IN ('channel', 'supergroup') AND sm.chat_id IN (?, ?) AND sm.message_id = ?) "
        "OR (sm.origin_channel_id IN (?, ?) AND sm.origin_message_id = ?)) "
        "AND e.unique_key LIKE 'eh:gallery:%' ORDER BY sm.id",
        (channel_id, -(1000000000000 + channel_id), message.origin_message_id,
         channel_id, -(1000000000000 + channel_id), message.origin_message_id),
    ).fetchall()
    keys = {row[0] for row in rows}
    if len(keys) > 1:
        raise IngestError("INGEST_ORIGIN_IDENTITY_CONFLICT")
    if not rows:
        return message
    return replace(message, ex_gid=int(rows[0][0].split(":")[-1]), ex_gallery_token=rows[0][1])


def register_observation(connection: sqlite3.Connection, message: ParsedSourceMessage) -> dict:
    """Persist an eligible observation and match its one processing entry.

    Caller holds BEGIN IMMEDIATE after local subscription/attachment checks.
    A linked duplicate adds its account-specific source without changing review,
    metadata or jobs. Pending duplicates retain all inputs for one processor.
    Known old observations keep their recorded generation, including removals.
    Identity-changing edits require the explicit upgrade/move operation.
    """
    account = observing_account(connection, message.account_id)
    message = resolve_message_identity(connection, message)
    key = message_key(message, account)
    old = connection.execute(
        "SELECT id, ingest_entry_id, ingest_generation FROM source_messages "
        "WHERE account_id = ? AND chat_id = ? AND message_id = ?",
        (account, message.chat_id, message.message_id),
    ).fetchone()
    if old is not None and old[1] is not None:
        entry = snapshot(connection, int(old[1]))
        if old[2] != entry["generation"]:
            return {**entry, "state": "REMOVED", "candidate_id": None,
                    "source_message_id": int(old[0]), "new_observation": False}
        if message.is_edit:
            from app.candidates.persistence import edit_observation
            edit_observation(connection, message, int(old[0]), entry, key)
            linked = connection.execute("SELECT ingest_entry_id FROM source_messages WHERE id = ?", (old[0],)).fetchone()[0]
            entry = snapshot(connection, int(linked))
        return {**entry, "source_message_id": int(old[0]), "new_observation": False}
    connection.execute("INSERT INTO ingest_entries(unique_key) VALUES(?) ON CONFLICT(unique_key) DO NOTHING", (key,))
    entry_id = int(connection.execute("SELECT id FROM ingest_entries WHERE unique_key = ?", (key,)).fetchone()[0])
    entry = snapshot(connection, entry_id)
    generation = _observation_generation(connection, entry, message, account)
    if entry["state"] == "REMOVED" and generation > entry["generation"]:
        connection.execute(
            "UPDATE ingest_entries SET processing_state = 'PENDING', generation = ?, revision = revision + 1, "
            "input_source_message_id = NULL, attempts = 0, retry_at = NULL, error_code = NULL WHERE id = ?",
            (generation, entry_id),
        )
        entry = snapshot(connection, entry_id)
    connection.execute(
        "INSERT INTO source_messages(account_id, chat_id, message_id, sender_id, reply_to_message_id, "
        "media_group_id, message_text, attachment_json, file_unique_id, message_date, filter_result, "
        "filter_reason, preview_urls_json, ingest_entry_id, ingest_generation, peer_type, "
        "origin_channel_id, origin_message_id, ex_gid, ex_gallery_token, gallery_from_preview, rule_source_id, is_forwarded, chat_title) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(account_id, chat_id, message_id) DO UPDATE SET "
        "ingest_entry_id = excluded.ingest_entry_id, ingest_generation = excluded.ingest_generation",
        (account, message.chat_id, message.message_id, message.sender_id, message.reply_to_message_id,
         message.media_group_id, message.message_text, json.dumps(message.attachments, ensure_ascii=False),
         message.file_unique_id, message.message_date, message.filter_result, message.filter_reason,
         json.dumps(message.preview_urls), entry_id, generation, message.peer_type,
         message.origin_channel_id, message.origin_message_id, message.ex_gid, message.ex_gallery_token,
         int(message.gallery_from_preview), message.rule_source_id, int(message.is_forwarded), message.chat_title),
    )
    source_id = int(connection.execute(
        "SELECT id FROM source_messages WHERE account_id = ? AND chat_id = ? AND message_id = ?",
        (account, message.chat_id, message.message_id),
    ).fetchone()[0])
    connection.execute(
        "UPDATE ingest_entries SET input_source_message_id = ? WHERE id = ? AND input_source_message_id IS NULL AND generation = ?",
        (source_id, entry_id, generation),
    )
    if generation != entry["generation"]:
        return {**entry, "state": "REMOVED", "candidate_id": None,
                "source_message_id": source_id, "new_observation": old is None}
    if entry["state"] == "LINKED":
        connection.execute("INSERT OR IGNORE INTO candidate_messages(candidate_id, source_message_id) VALUES(?, ?)",
                           (entry["candidate_id"], source_id))
    return {**snapshot(connection, entry_id), "source_message_id": source_id, "new_observation": old is None}


def _observation_generation(connection, entry: dict, message: ParsedSourceMessage, account: int) -> int:
    """Old posts retain their generation; only proven new direct posts revive."""
    removed_at = json.loads(entry["extra_json"]).get("removed_at")
    if removed_at is None:
        return entry["generation"]
    received_key = message_key(replace(message, ex_gid=None), account)
    for sid, legacy_account, generation in connection.execute(
        "SELECT id, account_id, ingest_generation FROM source_messages WHERE ingest_entry_id = ? ORDER BY id", (entry["id"],),
    ).fetchall():
        original = load_message(connection, sid)
        if message_key(replace(original, ex_gid=None), legacy_account) == received_key:
            return int(generation)
    posted_at = connection.execute("SELECT unixepoch(?)", (message.message_date,)).fetchone()[0]
    is_new = (not message.is_edit and not message.is_forwarded
              and message.peer_type in {"channel", "supergroup"}
              and posted_at is not None and posted_at > removed_at)
    if entry["state"] == "REMOVED":
        return entry["generation"] + 1 if is_new else entry["generation"]
    # After an explicit or automatic re-add, a delayed old/uncertain copy still
    # cannot acquire the new work as a downloadable source.
    return entry["generation"] if is_new else max(1, entry["generation"] - 1)


def claim_entry(connection: sqlite3.Connection, entry_id: int, *, owner: str,
                lease_seconds: int = 120, now: int | None = None, max_attempts: int = 3,
                allow_source_less: bool = False) -> IngestClaim | None:
    """Claim pending work or an expired processor with a fresh revision.

    lease_seconds and max_attempts are positive integers. A live lease, linked,
    ignored, removed, blocked or superseded entry is never claimed. At the retry
    limit an expired claim becomes BLOCKED rather than being paid for forever.
    """
    if not owner or not isinstance(lease_seconds, int) or isinstance(lease_seconds, bool) or lease_seconds <= 0:
        raise ValueError("owner and positive lease_seconds required")
    if not isinstance(max_attempts, int) or isinstance(max_attempts, bool) or max_attempts <= 0:
        raise ValueError("positive max_attempts required")
    now = int(time.time()) if now is None else now
    entry = snapshot(connection, entry_id)
    if connection.execute(
        "SELECT 1 FROM ingest_reference_changes WHERE target_key = ? AND status = 'PENDING' "
        "UNION ALL SELECT 1 FROM ingest_observation_changes WHERE target_key = ? AND status = 'PENDING' LIMIT 1",
        (entry['unique_key'], entry['unique_key']),
    ).fetchone() is not None:
        return None
    eligible = (entry["state"] == "PENDING" and (entry["retry_at"] is None or entry["retry_at"] <= now)
                or entry["state"] == "PROCESSING" and entry["lease_until"] <= now)
    if not eligible:
        return None
    if entry["attempts"] >= max_attempts:
        connection.execute(
            "UPDATE ingest_entries SET processing_state = 'BLOCKED', revision = revision + 1, "
            "owner = NULL, lease_until = NULL, error_code = 'INGEST_RETRY_EXHAUSTED' WHERE id = ?",
            (entry_id,),
        )
        return None
    source = connection.execute(
        "SELECT id, observation_revision FROM source_messages WHERE id = ? "
        "AND ingest_entry_id = ? AND ingest_generation = ? AND message_state = 'ACTIVE'",
        (entry["input_source_message_id"], entry_id, entry["generation"]),
    ).fetchone()
    if source is None and not (allow_source_less and entry["input_source_message_id"] is None
                               and entry["unique_key"].startswith("eh:gallery:")):
        raise IngestError("INGEST_INPUT_INVALID")
    cursor = connection.execute(
        "UPDATE ingest_entries SET processing_state = 'PROCESSING', revision = revision + 1, "
        "owner = ?, lease_until = ?, attempts = attempts + 1, retry_at = NULL, error_code = NULL, "
        "updated_at = CURRENT_TIMESTAMP WHERE id = ? AND revision = ?",
        (owner, now + lease_seconds, entry_id, entry["revision"]),
    )
    if cursor.rowcount != 1:
        return None
    return IngestClaim(entry_id, entry["revision"] + 1, entry["generation"], owner,
                       int(source[0]) if source is not None else None, int(source[1]) if source is not None else None)


def check_claim(connection: sqlite3.Connection, claim: IngestClaim, *, now: int | None = None) -> dict:
    """Reject late results after reclaim, removal, edit or owner replacement."""
    now = int(time.time()) if now is None else now
    entry = snapshot(connection, claim.entry_id)
    source = connection.execute(
        "SELECT observation_revision, ingest_entry_id, ingest_generation, message_state "
        "FROM source_messages WHERE id = ?", (claim.source_message_id,),
    ).fetchone()
    if (entry["state"] != "PROCESSING" or entry["revision"] != claim.revision
            or entry["generation"] != claim.generation or entry["owner"] != claim.owner
            or entry["lease_until"] <= now
            or (claim.source_message_id is not None and (entry["input_source_message_id"] != claim.source_message_id
                or source is None or tuple(source) != (claim.observation_revision, claim.entry_id, claim.generation, "ACTIVE")))):
        raise IngestError("INGEST_STALE_PROCESSOR")
    return entry


def claim_manual_gallery(connection: sqlite3.Connection, gallery_id: int, *, owner: str,
                         active_entry_ids=()) -> IngestClaim:
    """Reserve the same key before manual validation, including pending receivers."""
    from app.db.database import Database
    key = ingest_key(gallery_id=gallery_id)
    duplicate = Database._manual_duplicate_sync(connection, gallery_id, None)
    if duplicate is not None:
        raise duplicate
    connection.execute("INSERT INTO ingest_entries(unique_key) VALUES(?) ON CONFLICT(unique_key) DO NOTHING", (key,))
    eid = int(connection.execute("SELECT id FROM ingest_entries WHERE unique_key = ?", (key,)).fetchone()[0])
    if eid in active_entry_ids:
        raise IngestError("INGEST_ALREADY_PROCESSING")
    entry = snapshot(connection, eid)
    if entry["state"] == "PROCESSING" and entry["lease_until"] > int(time.time()):
        raise IngestError("INGEST_ALREADY_PROCESSING")
    # An explicit manual add may retry ignored/removed work. Old observations
    # keep their generation and cannot be attached to this new command.
    if entry["state"] in {"REMOVED", "IGNORED", "BLOCKED", "MERGED"}:
        connection.execute(
            "UPDATE ingest_entries SET processing_state = 'PENDING', generation = generation + 1, "
            "revision = revision + 1, input_source_message_id = NULL, attempts = 0, retry_at = NULL, "
            "error_code = NULL, candidate_id = NULL, owner = NULL, lease_until = NULL WHERE id = ?", (eid,),
        )
    claim = claim_entry(connection, eid, owner=owner, allow_source_less=True)
    if claim is None:
        raise IngestError("INGEST_ALREADY_PROCESSING")
    return claim


def renew_claim(connection: sqlite3.Connection, claim: IngestClaim, *, lease_seconds: int = 120,
                now: int | None = None) -> None:
    """Extend only a still-live owner; an expired owner cannot revive its lease."""
    if isinstance(lease_seconds, bool) or not isinstance(lease_seconds, int) or lease_seconds <= 0:
        raise ValueError("positive lease_seconds required")
    now = int(time.time()) if now is None else now
    check_claim(connection, claim, now=now)
    connection.execute("UPDATE ingest_entries SET lease_until = ? WHERE id = ?", (now + lease_seconds, claim.entry_id))


def finish_claim(connection: sqlite3.Connection, claim: IngestClaim, *, candidate_id: int | None,
                 now: int | None = None) -> None:
    """Link every eligible observation, or ignore the input, in the caller's transaction.

    candidate_id=None means an explicit admission rejection (IGNORED); a positive
    id links the existing/just-created candidate. Candidate insertion and this
    guard must share one write transaction so a stale result rolls back both.
    """
    check_claim(connection, claim, now=now)
    if candidate_id is not None:
        if isinstance(candidate_id, bool) or not isinstance(candidate_id, int) or candidate_id <= 0:
            raise ValueError("candidate_id must be positive or None")
        candidate = connection.execute("SELECT ex_gid FROM candidates WHERE id = ?", (candidate_id,)).fetchone()
        if candidate is None:
            raise IngestError("INGEST_CANDIDATE_NOT_FOUND")
        key = snapshot(connection, claim.entry_id)["unique_key"]
        if key.startswith("eh:gallery:") and (candidate[0] is None or key != ingest_key(gallery_id=candidate[0])):
            raise IngestError("INGEST_GALLERY_CONFLICT")
        connection.execute(
            "INSERT OR IGNORE INTO candidate_messages(candidate_id, source_message_id) "
            "SELECT ?, id FROM source_messages WHERE ingest_entry_id = ? AND ingest_generation = ? "
            "AND message_state = 'ACTIVE' AND filter_result <> 'IGNORE'",
            (candidate_id, claim.entry_id, claim.generation),
        )
    connection.execute(
        "UPDATE ingest_entries SET candidate_id = ?, processing_state = ?, revision = revision + 1, "
        "owner = NULL, lease_until = NULL, retry_at = NULL, error_code = NULL, updated_at = CURRENT_TIMESTAMP "
        "WHERE id = ?", (candidate_id, "IGNORED" if candidate_id is None else "LINKED", claim.entry_id),
    )


def fail_claim(connection: sqlite3.Connection, claim: IngestClaim, *, error_code: str,
               retry_seconds: int = 30, now: int | None = None) -> None:
    """Save a bounded retry intention; never publish a partial candidate."""
    if isinstance(retry_seconds, bool) or not isinstance(retry_seconds, int) or retry_seconds < 0:
        raise ValueError("retry_seconds must be a nonnegative integer")
    now = int(time.time()) if now is None else now
    check_claim(connection, claim, now=now)
    connection.execute(
        "UPDATE ingest_entries SET processing_state = 'PENDING', revision = revision + 1, owner = NULL, "
        "lease_until = NULL, retry_at = ?, error_code = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
        (now + retry_seconds, error_code, claim.entry_id),
    )


def load_message(connection: sqlite3.Connection, source_message_id: int) -> ParsedSourceMessage:
    """Read the fixed persisted input, rather than a later receiver's different caption/file ids."""
    row = connection.execute(
        "SELECT sm.chat_id, sm.message_id, sm.sender_id, sm.reply_to_message_id, sm.media_group_id, "
        "sm.message_text, sm.attachment_json, sm.file_unique_id, sm.message_date, sm.filter_result, "
        "sm.filter_reason, sm.preview_urls_json, sm.ex_gid, sm.ex_gallery_token, sm.gallery_from_preview, "
        "sm.peer_type, sm.origin_channel_id, sm.origin_message_id, ta.external_account_id, sm.rule_source_id, sm.is_forwarded, sm.chat_title "
        "FROM source_messages sm JOIN telegram_accounts ta ON ta.id = sm.account_id WHERE sm.id = ?",
        (source_message_id,),
    ).fetchone()
    if row is None:
        raise IngestError("INGEST_INPUT_INVALID")
    return ParsedSourceMessage(
        is_edit=False, chat_id=int(row[0]), chat_title=row[21], message_id=int(row[1]), sender_id=row[2],
        reply_to_message_id=row[3], media_group_id=row[4], message_text=row[5] or "",
        attachments=tuple(json.loads(row[6])), file_unique_id=row[7], message_date=row[8],
        filter_result=row[9], filter_reason=row[10], preview_urls=tuple(json.loads(row[11] or "[]")),
        ex_gid=row[12], ex_gallery_token=row[13], gallery_from_preview=bool(row[14]),
        peer_type=row[15], origin_channel_id=row[16], origin_message_id=row[17], account_id=row[18], rule_source_id=row[19],
        is_forwarded=bool(row[20]),
    )
