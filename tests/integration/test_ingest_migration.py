"""031 upgrades actual legacy schemas atomically and preserves work history."""
import asyncio
import json
import sqlite3
import shutil
from contextlib import closing
from pathlib import Path

import pytest

from app.candidates.entries import IngestError
from app.db.database import Database


def legacy_database(tmp_path):
    db = Database(tmp_path / "legacy.db")
    with closing(sqlite3.connect(db.path)) as c:
        c.execute("CREATE TABLE schema_migrations(version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)")
        for path in sorted(Path("app/db/migrations").glob("*.sql")):
            version = int(path.name.split("_", 1)[0])
            if version >= 31:
                break
            c.executescript(path.read_text(encoding="utf-8"))
            c.execute("INSERT INTO schema_migrations VALUES(?, CURRENT_TIMESTAMP)", (version,))
            c.commit()
    return db


def candidate(c, cid, token, *, gid=123, status="PENDING_REVIEW"):
    c.execute("INSERT INTO candidates(id, ex_gid, ex_gallery_token, status) VALUES(?, ?, ?, ?)", (cid, gid, token, status))


def source(c, sid, cid=None, *, chat=-1000000000777, account=1, text="", result="ACCEPT"):
    c.execute("INSERT OR IGNORE INTO telegram_accounts(id, account_type, display_name, session_path) VALUES(?, 'BOT', 'legacy', ?)",
              (account, f"legacy://{account}"))
    c.execute("INSERT INTO source_messages(id, account_id, chat_id, message_id, message_text, message_date, filter_result) "
              "VALUES(?, ?, ?, ?, ?, '2026-10-13', ?)", (sid, account, chat, sid, text, result))
    if cid is not None:
        c.execute("INSERT INTO candidate_messages VALUES(?, ?)", (cid, sid))


def job(c, jid, cid, state="COMPLETED"):
    c.execute("INSERT INTO download_jobs(id, candidate_id, idempotency_key, provider, state) VALUES(?, ?, ?, 'TELEGRAM', ?)",
              (jid, cid, f"job-{jid}", state))
    return c.execute("SELECT task_id FROM download_jobs WHERE id = ?", (jid,)).fetchone()[0]


def upgrade(db):
    asyncio.run(db.initialize())


def assert_rolled_back(db):
    with db.connection() as c:
        assert c.execute("SELECT max(version) FROM schema_migrations").fetchone() == (30,)
        assert c.execute("SELECT 1 FROM sqlite_master WHERE name = 'ingest_entries'").fetchone() is None
        assert "external_account_id" not in {row[1] for row in c.execute("PRAGMA table_info(telegram_accounts)")}
        assert c.execute("SELECT count(*) FROM candidates").fetchone() == (2,)


def test_duplicate_tokens_merge_sources_manual_metadata_and_terminal_history(tmp_path):
    db = legacy_database(tmp_path)
    with db.connection() as c:
        candidate(c, 1, "first", status="APPROVED")
        candidate(c, 2, "second", status="APPROVED")
        source(c, 1, 1)
        source(c, 2, 2, account=2)
        task = job(c, 7, 2)
        c.execute("INSERT INTO artifacts(job_id, artifact_type, path, sha256, size_bytes) VALUES(7, 'RAW_ARCHIVE', 'fixture.zip', 'fixture-sha', 7)")
        c.execute("INSERT INTO review_actions(candidate_id, action, operator_name) VALUES(1, 'APPROVE', 'administrator')")
        c.execute("INSERT INTO metadata_values(candidate_id, field_name, field_value, value_source, is_manual) "
                  "VALUES(1, 'Title', 'chosen', 'MANUAL', 1), (2, 'Title', 'automatic', 'MANUAL', 0)")
        c.execute("INSERT INTO work_archive_paths(candidate_id, relative_path, operator_name) VALUES(1, 'chosen.cbz', 'administrator')")
    upgrade(db)
    upgrade(db)
    with db.connection() as c:
        # The artifact owner survives; task and artifact identities are intact.
        assert c.execute("SELECT id, ex_gid FROM candidates").fetchall() == [(2, 123)]
        assert c.execute("SELECT task_id, candidate_id FROM download_jobs WHERE id = 7").fetchone() == (task, 2)
        assert c.execute("SELECT path, sha256 FROM artifacts").fetchone() == ("fixture.zip", "fixture-sha")
        assert c.execute("SELECT candidate_id, field_value, is_manual FROM metadata_values").fetchone() == (2, "chosen", 1)
        assert c.execute("SELECT candidate_id, relative_path FROM work_archive_paths").fetchone() == (2, "chosen.cbz")
        assert c.execute("SELECT candidate_id FROM candidate_messages ORDER BY source_message_id").fetchall() == [(2,), (2,)]
        assert c.execute("SELECT external_account_id FROM telegram_accounts").fetchall() == [(None,), (None,)]
        assert c.execute("SELECT unique_key, candidate_id, processing_state FROM ingest_entries").fetchall() == [("eh:gallery:123", 2, "LINKED")]
        audit = json.loads(c.execute("SELECT details_json FROM review_actions WHERE action = 'INGEST_MERGE'").fetchone()[0])
        assert audit["source_candidate"]["id"] == 1 and audit["metadata"][0][1] == "chosen"
        assert c.execute("PRAGMA foreign_key_check").fetchall() == []
        with pytest.raises(sqlite3.IntegrityError):
            candidate(c, 3, "third")


@pytest.mark.parametrize("conflict", ["manual_metadata", "manual_pin", "review", "two_live_tasks", "unconfirmed_remote"])
def test_conflicts_roll_back_ddl_data_and_version_then_allow_repaired_retry(tmp_path, conflict):
    db = legacy_database(tmp_path)
    with db.connection() as c:
        candidate(c, 1, "first", status="APPROVED")
        candidate(c, 2, "second", status="REJECTED" if conflict == "review" else "APPROVED")
        if conflict == "manual_metadata":
            c.execute("INSERT INTO metadata_values(candidate_id, field_name, field_value, value_source, is_manual) "
                      "VALUES(1, 'Title', 'one', 'MANUAL', 1), (2, 'Title', 'two', 'OTHER', 1)")
        elif conflict == "manual_pin":
            c.execute("INSERT INTO work_archive_paths(candidate_id, relative_path, operator_name) VALUES(1, 'one.cbz', 'administrator'), (2, 'two.cbz', 'administrator')")
        elif conflict == "two_live_tasks":
            job(c, 1, 1, "PENDING")
            job(c, 2, 2, "PENDING")
        elif conflict == "unconfirmed_remote":
            for cid in (1, 2):
                task = job(c, cid, cid)
                c.execute("INSERT INTO queue_remote_resources(resource_id, task_id, attempt_id, legacy_job_id, instance_url, digest, ownership_tag, ownership, status) "
                          "VALUES(?, ?, 'attempt', ?, 'fixture://remote', 'digest', 'tag', 'OWNED', 'REGISTERED')", (str(cid), task, cid))
    with pytest.raises(IngestError):
        upgrade(db)
    assert_rolled_back(db)
    with db.connection() as c:
        c.execute("UPDATE metadata_values SET field_value = 'one'")
        c.execute("UPDATE work_archive_paths SET is_manual = 0")
        c.execute("UPDATE candidates SET status = 'APPROVED'")
        c.execute("UPDATE download_jobs SET state = 'CANCELLED'")
        c.execute("UPDATE queue_remote_resources SET status = 'STOP_CONFIRMED'")
    upgrade(db)
    with db.connection() as c:
        assert c.execute("SELECT count(*) FROM candidates").fetchone() == (1,)
        assert c.execute("PRAGMA foreign_key_check").fetchall() == []


def test_one_live_owner_survives_without_restarting_or_rewriting_attempt(tmp_path):
    db = legacy_database(tmp_path)
    with db.connection() as c:
        candidate(c, 1, "first")
        candidate(c, 2, "second")
        job(c, 1, 1)
        task = job(c, 2, 2, "DOWNLOADING")
        attempt_before = c.execute("SELECT * FROM queue_attempts WHERE task_id = ?", (task,)).fetchone()
    upgrade(db)
    with db.connection() as c:
        assert c.execute("SELECT id FROM candidates").fetchall() == [(2,)]
        assert c.execute("SELECT state, task_id FROM download_jobs WHERE id = 2").fetchone() == ("DOWNLOADING", task)
        assert c.execute("SELECT * FROM queue_attempts WHERE task_id = ?", (task,)).fetchone() == attempt_before
        assert c.execute("SELECT candidate_id FROM queue_tasks").fetchall() == [(2,), (2,)]


@pytest.mark.asyncio
async def test_legacy_approval_without_job_is_preserved_for_explicit_enqueue(tmp_path):
    from app.candidates.followups import due_followups
    from app.downloads.service import DownloadService
    db = legacy_database(tmp_path)
    with db.connection() as c:
        candidate(c, 1, 'fixture-token', status='APPROVED')
        c.execute("INSERT INTO review_actions(candidate_id, action, operator_name) VALUES(1, 'APPROVE', 'auto-rule')")
    await db.initialize()
    await db.initialize()
    # This is also the shape left by an intentional removal of job records.
    # Migration cannot infer a new download intention from an approval alone.
    assert due_followups(db, 100) == []
    with db.connection() as c:
        assert c.execute('SELECT status FROM candidates').fetchone() == ('APPROVED',)
        assert c.execute('SELECT count(*) FROM candidate_approvals').fetchone() == (1,)
        assert c.execute('SELECT count(*) FROM download_jobs').fetchone() == (0,)
    result = await DownloadService(db, tmp_path / 'work').enqueue_exhentai_download(1)
    assert result.created
    with db.connection() as c:
        assert c.execute('SELECT count(*) FROM candidate_approvals').fetchone() == (1,)
        assert c.execute('SELECT count(*) FROM review_actions').fetchone() == (1,)


def test_no_gallery_sources_use_proven_channel_or_unknown_account_scope(tmp_path):
    db = legacy_database(tmp_path)
    with db.connection() as c:
        candidate(c, 1, "unused", gid=None)
        candidate(c, 2, "unused", gid=None)
        source(c, 1, 1)
        source(c, 2, 2, chat=-55)
        source(c, 3, chat=-55, account=2, result="IGNORE")
        candidate(c, 3, "unused", gid=None)  # No source: explicitly unknown identity.
    upgrade(db)
    with db.connection() as c:
        keys = {row[0] for row in c.execute("SELECT unique_key FROM ingest_entries")}
        assert keys == {"tg:channel:777:1", "tg:account:legacy%3A1:peer:-55:message:2",
                        "tg:account:legacy%3A2:peer:-55:message:3", "legacy:candidate:3"}
        assert c.execute("SELECT peer_type FROM source_messages WHERE id = 2").fetchone() == (None,)


def test_historical_source_label_is_backfilled_once_without_guessing_account(tmp_path):
    from app.candidates.entries import load_message
    db = legacy_database(tmp_path)
    with db.connection() as c:
        source(c, 1)
        source(c, 2, chat=-55)
        c.execute("INSERT INTO telegram_sources(account_id, source_type, chat_id, display_name) "
                  "VALUES(1, 'CHANNEL', -1000000000777, 'Historical configured label')")
    upgrade(db)
    with db.connection() as c:
        assert load_message(c, 1).chat_title == "Historical configured label"
        assert load_message(c, 2).chat_title == "-55"
        assert load_message(c, 1).account_id is None
        c.execute("UPDATE telegram_sources SET display_name = 'Renamed after migration'")
    upgrade(db)
    with db.connection() as c:
        assert load_message(c, 1).chat_title == "Historical configured label"
        assert load_message(c, 1).account_id is None


def test_proven_source_gallery_upgrades_unidentified_candidate(tmp_path):
    db = legacy_database(tmp_path)
    with db.connection() as c:
        candidate(c, 1, "unused", gid=None)
        source(c, 1, 1, text="https://e-hentai.org/g/987/linktoken/")
    upgrade(db)
    with db.connection() as c:
        assert c.execute("SELECT ex_gid, ex_gallery_token FROM candidates").fetchone() == (987, "linktoken")
        assert c.execute("SELECT unique_key, candidate_id FROM ingest_entries").fetchone() == ("eh:gallery:987", 1)


def test_conflicting_source_gallery_aborts_upgrade_instead_of_misassociating(tmp_path):
    db = legacy_database(tmp_path)
    with db.connection() as c:
        candidate(c, 1, "first")
        candidate(c, 2, "second", gid=456)
        source(c, 1, 1, text="https://exhentai.org/g/456/linktoken/")
    with pytest.raises(IngestError, match="INGEST_HISTORICAL_SOURCE_CONFLICT"):
        upgrade(db)
    assert_rolled_back(db)


def test_offline_snapshot_restores_030_identity_and_history_after_upgrade(tmp_path):
    db = legacy_database(tmp_path)
    with db.connection() as connection:
        candidate(connection, 1, "first", status="APPROVED")
        candidate(connection, 2, "second", status="APPROVED")
        source(connection, 1, 1)
        source(connection, 2, 2, account=2)
        task_id = job(connection, 7, 2)
    snapshot_path = tmp_path / "before-upgrade.db"
    # SQLite backup includes committed WAL pages without relying on a main-file
    # copy. The service and every supplier adapter remain stopped in this test.
    with closing(sqlite3.connect(db.path)) as live, closing(sqlite3.connect(snapshot_path)) as backup:
        live.backup(backup)
    upgrade(db)
    with db.connection() as connection:
        assert connection.execute("SELECT count(*) FROM candidates").fetchone() == (1,)
        connection.execute("UPDATE candidates SET filter_reason = 'post-upgrade-only'")
    restored_path = tmp_path / "restored-030.db"
    shutil.copy2(snapshot_path, restored_path)
    # Use an old-schema reader: initializing with current code would immediately
    # reapply every migration and would not prove the rollback boundary.
    with closing(sqlite3.connect(restored_path)) as restored:
        assert restored.execute("SELECT max(version) FROM schema_migrations").fetchone() == (30,)
        assert restored.execute("SELECT id, ex_gallery_token FROM candidates ORDER BY id").fetchall() == [(1, "first"), (2, "second")]
        assert restored.execute("SELECT task_id, candidate_id FROM download_jobs WHERE id = 7").fetchone() == (task_id, 2)
        assert restored.execute("SELECT candidate_id, source_message_id FROM candidate_messages ORDER BY source_message_id").fetchall() == [(1, 1), (2, 2)]
        assert restored.execute("SELECT 1 FROM sqlite_master WHERE name = 'ingest_entries'").fetchone() is None
        assert restored.execute("PRAGMA integrity_check").fetchone() == ("ok",)
        assert restored.execute("PRAGMA foreign_key_check").fetchall() == []
