"""Real SQLite arbitration of observations, ownership and late results."""
import asyncio
import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from threading import Barrier

import pytest

from app.candidates.entries import (
    IngestError, check_claim, claim_entry, fail_claim, finish_claim,
    register_observation, renew_claim, snapshot,
)
from app.candidates.models import ParsedSourceMessage
from app.connections.accounts import AccountRegistry
from app.db.database import Database
from app.secrets import SecretStore


@pytest.fixture
def database(tmp_path):
    db = Database(tmp_path / "ingest.db")
    asyncio.run(db.initialize())
    return db


def accounts(database, tmp_path):
    registry = AccountRegistry(database, SecretStore(tmp_path / "private"))
    return [asyncio.run(registry.create(kind, kind, fields))["account_id"]
            for kind, fields in (("TG_BOT", {"bot_token": "fixture-token-a"}),
                                 ("TG_USER", {"api_id": "1", "api_hash": "fixture-hash-b"}))]


def message(account=None, *, gid=123, message_id=42, chat_id=-1000000000777):
    return ParsedSourceMessage(
        is_edit=False, chat_id=chat_id, chat_title="Fixture", message_id=message_id, sender_id=None,
        reply_to_message_id=None, media_group_id=None, message_text="fixture caption",
        attachments=({"type": "archive", "file_id": "fixture-file", "file_name": "a.zip"},),
        file_unique_id="fixture-unique", message_date="2026-10-14T00:00:00+00:00",
        filter_result="ACCEPT", filter_reason="local checks passed", ex_gid=gid, ex_gallery_token="fixture-token",
        account_id=account, peer_type="channel",
    )


def register(database, msg):
    with database.connection() as c:
        c.execute("BEGIN IMMEDIATE")
        return register_observation(c, msg)


def claim(database, entry_id, *, owner="worker", now=100):
    with database.connection() as c:
        c.execute("BEGIN IMMEDIATE")
        return claim_entry(c, entry_id, owner=owner, now=now, lease_seconds=10)


def candidate(c, gid=123):
    return c.execute("INSERT INTO candidates(status, ex_gid, ex_gallery_token) VALUES('APPROVED', ?, 'fixture-token')",
                     (gid,)).lastrowid


def test_concurrent_accounts_preserve_both_observations_and_one_pending_identity(database, tmp_path):
    ids = accounts(database, tmp_path)
    barrier = Barrier(2)
    def receive(index):
        barrier.wait(timeout=5)
        msg = replace(message(ids[index], chat_id=501 + index, message_id=50 + index), peer_type="private",
                      attachments=({"type": "archive", "file_id": f"account-file-{index}"},))
        return register(database, msg)
    with ThreadPoolExecutor(2) as pool:
        results = list(pool.map(receive, range(2)))
    assert results[0]["id"] == results[1]["id"]
    assert results[0]["candidate_id"] is None and results[1]["candidate_id"] is None
    with database.connection() as c:
        rows = c.execute("SELECT ta.external_account_id, sm.attachment_json FROM source_messages sm "
                         "JOIN telegram_accounts ta ON ta.id = sm.account_id ORDER BY sm.id").fetchall()
        assert {row[0] for row in rows} == set(ids)
        assert {json.loads(row[1])[0]["file_id"] for row in rows} == {"account-file-0", "account-file-1"}
        extra = json.loads(snapshot(c, results[0]["id"])["extra_json"])
        assert extra == {"key_version": 1}  # Complete observations live in rows, not an expanding JSON array.
        assert c.execute("SELECT count(*) FROM candidates").fetchone()[0] == 0


def test_two_processors_race_before_ai_and_only_one_claims(database):
    entry = register(database, message())
    barrier = Barrier(2)
    def take(owner):
        barrier.wait(timeout=5)
        return claim(database, entry["id"], owner=owner)
    with ThreadPoolExecutor(2) as pool:
        results = list(pool.map(take, ("bot-worker", "user-worker")))
    assert sum(value is not None for value in results) == 1
    with database.connection() as c:
        assert snapshot(c, entry["id"])["attempts"] == 1


def test_observation_arriving_during_processing_is_attached_atomically(database, tmp_path):
    a, b = accounts(database, tmp_path)
    entry = register(database, message(a))
    taken = claim(database, entry["id"])
    register(database, message(b, chat_id=-1000000000888))
    with database.connection() as c:
        c.execute("BEGIN IMMEDIATE")
        cid = candidate(c)
        finish_claim(c, taken, candidate_id=cid, now=101)
    register(database, replace(message(a, message_id=99), ex_gallery_token="other-spelling"))
    with database.connection() as c:
        assert c.execute("SELECT count(*) FROM candidates").fetchone()[0] == 1
        assert c.execute("SELECT count(*) FROM candidate_messages WHERE candidate_id = ?", (cid,)).fetchone()[0] == 3
        assert c.execute("SELECT status, ex_gallery_token FROM candidates").fetchone() == ("APPROVED", "fixture-token")
        assert c.execute("SELECT count(*) FROM candidate_approvals").fetchone()[0] == 1
        assert c.execute("SELECT count(*) FROM download_jobs").fetchone()[0] == 0
        assert snapshot(c, entry["id"])["attempts"] == 1


def test_complete_forward_and_original_share_the_same_pending_record(database, tmp_path):
    a, b = accounts(database, tmp_path)
    original = register(database, message(a, gid=None))
    forwarded = register(database, replace(message(b, gid=None, chat_id=-20, message_id=80), peer_type="group",
                                           origin_channel_id=777, origin_message_id=42))
    assert original["id"] == forwarded["id"]
    assert forwarded["unique_key"] == "tg:channel:777:42"


def test_private_message_numbers_are_not_a_cross_account_identity(database, tmp_path):
    ids = accounts(database, tmp_path)
    received = [register(database, replace(message(account, gid=None, chat_id=5), peer_type="private")) for account in ids]
    assert received[0]["id"] != received[1]["id"]


def test_expired_owner_cannot_commit_candidate_or_clear_successors_lease(database):
    entry = register(database, message())
    first = claim(database, entry["id"], owner="before-crash")
    assert claim(database, entry["id"], owner="too-soon", now=109) is None
    second = claim(database, entry["id"], owner="after-crash", now=110)
    assert second.revision > first.revision
    with pytest.raises(IngestError, match="INGEST_STALE_PROCESSOR"):
        with database.connection() as c:
            c.execute("BEGIN IMMEDIATE")
            cid = candidate(c)
            finish_claim(c, first, candidate_id=cid, now=111)
    with pytest.raises(IngestError, match="INGEST_STALE_PROCESSOR"):
        with database.connection() as c:
            c.execute("BEGIN IMMEDIATE")
            fail_claim(c, first, error_code="NETWORK", now=111)
    with database.connection() as c:
        assert c.execute("SELECT count(*) FROM candidates").fetchone()[0] == 0
        check_claim(c, second, now=111)


def test_heartbeat_only_renews_a_live_owner(database):
    entry = register(database, message())
    taken = claim(database, entry["id"])
    with database.connection() as c:
        c.execute("BEGIN IMMEDIATE")
        renew_claim(c, taken, now=109, lease_seconds=10)
    assert claim(database, entry["id"], owner="other", now=110) is None
    with pytest.raises(IngestError, match="INGEST_STALE_PROCESSOR"):
        with database.connection() as c:
            c.execute("BEGIN IMMEDIATE")
            renew_claim(c, taken, now=119)


def test_retry_backoff_and_attempt_limit_prevent_unbounded_processing(database):
    entry = register(database, message())
    for index in range(3):
        now = 100 + index * 30
        taken = claim(database, entry["id"], now=now)
        assert taken is not None
        with database.connection() as c:
            c.execute("BEGIN IMMEDIATE")
            fail_claim(c, taken, error_code="NETWORK_UNAVAILABLE", now=now, retry_seconds=30)
        assert claim(database, entry["id"], now=now + 29) is None
    assert claim(database, entry["id"], now=190) is None
    with database.connection() as c:
        saved = snapshot(c, entry["id"])
        assert saved["state"] == "BLOCKED" and saved["attempts"] == 3
        assert saved["error_code"] == "INGEST_RETRY_EXHAUSTED"


def test_ignored_identity_is_not_reprocessed_by_duplicate_receivers(database):
    entry = register(database, message())
    taken = claim(database, entry["id"])
    with database.connection() as c:
        c.execute("BEGIN IMMEDIATE")
        finish_claim(c, taken, candidate_id=None, now=101)
    duplicate = register(database, message(chat_id=-1000000000888))
    assert duplicate["state"] == "IGNORED" and duplicate["id"] == entry["id"]
    assert claim(database, duplicate["id"]) is None


def test_business_removal_invalidates_identity_and_old_deliveries_cannot_revive(database, tmp_path):
    a, b = accounts(database, tmp_path)
    original = message(a)
    entry = register(database, original)
    taken = claim(database, entry["id"])
    with database.connection() as c:
        c.execute("BEGIN IMMEDIATE")
        cid = candidate(c)
        finish_claim(c, taken, candidate_id=cid, now=101)
        c.execute("DELETE FROM candidates WHERE id = ?", (cid,))
    assert register(database, original)["state"] == "REMOVED"
    assert register(database, message(b))["state"] == "REMOVED"
    assert claim(database, entry["id"], now=120) is None
    with database.connection() as c:
        assert c.execute("SELECT count(*) FROM candidates").fetchone()[0] == 0
        assert snapshot(c, entry["id"])["revision"] > taken.revision


def test_changed_input_revision_rejects_late_ai_result(database):
    entry = register(database, message())
    taken = claim(database, entry["id"])
    with database.connection() as c:
        c.execute("UPDATE source_messages SET observation_revision = observation_revision + 1 WHERE id = ?",
                  (taken.source_message_id,))
    with pytest.raises(IngestError, match="INGEST_STALE_PROCESSOR"):
        with database.connection() as c:
            c.execute("BEGIN IMMEDIATE")
            cid = candidate(c)
            finish_claim(c, taken, candidate_id=cid, now=101)
    with database.connection() as c:
        assert c.execute("SELECT count(*) FROM candidates").fetchone()[0] == 0


def test_candidate_gallery_conflict_rolls_back_whole_finalization(database):
    entry = register(database, message())
    taken = claim(database, entry["id"])
    with pytest.raises(IngestError, match="INGEST_GALLERY_CONFLICT"):
        with database.connection() as c:
            c.execute("BEGIN IMMEDIATE")
            cid = candidate(c, gid=999)
            finish_claim(c, taken, candidate_id=cid, now=101)
    with database.connection() as c:
        assert c.execute("SELECT count(*) FROM candidates").fetchone()[0] == 0
        check_claim(c, taken, now=101)


def test_upgrade_preserves_unknown_legacy_identity_and_is_repeatable(tmp_path):
    path = tmp_path / "legacy.db"
    db = Database(path)
    import sqlite3
    with sqlite3.connect(path) as c:
        c.execute("CREATE TABLE schema_migrations(version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)")
        for migration in sorted(Path("app/db/migrations").glob("*.sql")):
            version = int(migration.name.split("_", 1)[0])
            if version >= 31:
                break
            c.executescript(migration.read_text(encoding="utf-8"))
            c.execute("INSERT INTO schema_migrations VALUES(?, CURRENT_TIMESTAMP)", (version,))
            c.commit()
        c.execute("INSERT INTO telegram_accounts(id, account_type, display_name, session_path) VALUES(1, 'BOT', 'old', 'bot-api://configured')")
        c.execute("INSERT INTO source_messages(account_id, chat_id, message_id, message_date) VALUES(1, 5, 9, '2026-10-13')")
    asyncio.run(db.initialize())
    asyncio.run(db.initialize())
    with db.connection() as c:
        assert c.execute("SELECT external_account_id FROM telegram_accounts WHERE id = 1").fetchone() == (None,)
        assert c.execute("SELECT e.unique_key, sm.peer_type, e.processing_state FROM source_messages sm "
                         "JOIN ingest_entries e ON e.id = sm.ingest_entry_id").fetchone() == (
                             "tg:account:legacy%3A1:peer:5:message:9", None, "PENDING")
        assert c.execute("SELECT count(*) FROM schema_migrations WHERE version = 31").fetchone()[0] == 1


def test_register_failure_leaves_no_partial_account_source_or_entry(database):
    with pytest.raises(IngestError, match="INGEST_ACCOUNT_INVALID"):
        register(database, message("unknown-account"))
    with database.connection() as c:
        for table in ("telegram_accounts", "source_messages", "ingest_entries"):
            assert c.execute(f"SELECT count(*) FROM {table}").fetchone()[0] == 0
