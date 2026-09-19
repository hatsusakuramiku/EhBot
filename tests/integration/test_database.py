import sqlite3
from pathlib import Path

import pytest

from app.db.database import Database


@pytest.mark.asyncio
async def test_auto_approval_rule_case_sensitive_round_trips(
    tmp_path: Path,
) -> None:
    """`case_sensitive` survives a save, a read, and an update-overwrite.

    Migration 016 adds the column and clears the old rules, so a fresh database
    starts with neither rules nor a schema that could drop the flag.
    """
    path = tmp_path / "ehbot.db"
    database = Database(path)
    await database.initialize()

    with sqlite3.connect(path) as connection:
        columns = {
            row[1] for row in connection.execute("PRAGMA table_info(auto_approval_rules)")
        }
        leftover = connection.execute("SELECT COUNT(*) FROM auto_approval_rules").fetchone()[0]
    assert "case_sensitive" in columns
    assert leftover == 0

    condition = {"kind": "condition", "field": "Title", "operator": "LIKE", "value": "%miku%"}
    # Default is insensitive; the insertion path defaults the column to off.
    first = await database.save_auto_approval_rule(
        rule_id=None,
        name="Insensitive by default",
        enabled=True,
        priority=10,
        condition=condition,
        dsl_snapshot='{Title} LIKE "%miku%"',
    )
    assert first.case_sensitive is False

    # A case-sensitive rule round-trips its flag through a read...
    second = await database.save_auto_approval_rule(
        rule_id=None,
        name="Case sensitive",
        enabled=True,
        priority=20,
        condition=condition,
        dsl_snapshot='{Title} LIKE "%Miku%"',
        case_sensitive=True,
    )
    assert second.case_sensitive is True

    stored = await database.list_auto_approval_rules()
    by_id = {rule.rule_id: rule for rule in stored}
    assert by_id[first.rule_id].case_sensitive is False
    assert by_id[second.rule_id].case_sensitive is True

    # ...and through an overwrite, which is the UPDATE path that 编辑 exercises.
    updated = await database.save_auto_approval_rule(
        rule_id=second.rule_id,
        name="Case sensitive, flipped",
        enabled=True,
        priority=20,
        condition=condition,
        dsl_snapshot='{Title} LIKE "%Miku%"',
        case_sensitive=False,
    )
    assert updated.case_sensitive is False
    reread = await database.get_auto_approval_rule(second.rule_id)
    assert reread is not None
    assert reread.case_sensitive is False


@pytest.mark.asyncio
async def test_initial_migration_is_idempotent_and_enables_sqlite_safety(
    tmp_path: Path,
) -> None:
    path = tmp_path / "ehbot.db"
    database = Database(path)

    await database.initialize()
    await database.initialize()

    with sqlite3.connect(path) as connection:
        migration_count = connection.execute(
            "SELECT COUNT(*) FROM schema_migrations"
        ).fetchone()[0]
        journal_mode = connection.execute("PRAGMA journal_mode").fetchone()[0]
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
                )
            }
        update_columns = {
            row[1]
            for row in connection.execute("PRAGMA table_info(telegram_bot_updates)")
        }
        update_indexes = {
            row[1]
            for row in connection.execute("PRAGMA index_list(telegram_bot_updates)")
        }
        source_message_columns = {
            row[1] for row in connection.execute("PRAGMA table_info(source_messages)")
        }
        candidate_columns = {
            row[1] for row in connection.execute("PRAGMA table_info(candidates)")
        }
        metadata_columns = {
            row[1]
            for row in connection.execute("PRAGMA table_info(metadata_values)")
        }
        download_job_columns = {
            row[1]
            for row in connection.execute("PRAGMA table_info(download_jobs)")
        }
        artifact_columns = {
            row[1] for row in connection.execute("PRAGMA table_info(artifacts)")
        }
        thumbnail_columns = {
            row[1] for row in connection.execute("PRAGMA table_info(thumbnails)")
        }
        archive_path_columns = {
            row[1]
            for row in connection.execute(
                "PRAGMA table_info(work_archive_paths)"
            )
        }
        routing_rule_columns = {
            row[1]
            for row in connection.execute(
                "PRAGMA table_info(archive_path_rules)"
            )
        }
        indexes = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'index'"
            )
        }

    assert migration_count == 17
    assert "auto_approval_rules" in tables
    assert {
        "archive_tool_profiles",
        "archive_passwords",
        "archive_settings",
    } <= tables
    assert journal_mode == "wal"
    assert {
        "telegram_accounts",
        "telegram_sources",
        "source_messages",
        "candidates",
        "candidate_messages",
        "metadata_values",
        "review_actions",
        "download_jobs",
        "artifacts",
        "admin_users",
        "telegram_bot_updates",
        "thumbnails",
        "schema_migrations",
    } <= tables
    assert {"processed_at", "processing_result", "processing_reason"} <= update_columns
    assert "idx_telegram_bot_updates_pending" in update_indexes
    assert {"filter_result", "filter_reason"} <= source_message_columns
    assert "preview_urls_json" in source_message_columns
    assert {"preview_url", "torrent_count", "torrent_hash"} <= candidate_columns
    # Migration 012: cover proxy, field locking, queue ordering.
    assert "thumb_url" in candidate_columns
    assert "is_locked" in metadata_columns
    assert "priority" in download_job_columns
    assert "page_count" in artifact_columns
    # Migration 014: the 已下载内容 domain. `library_relative_path` is where a
    # repack has to land once the operator has renamed or moved the book, and
    # `removed_works` is why a removal is recorded rather than only performed --
    # deleting a terminal job row would otherwise make the history lie about its
    # own completeness.
    assert "library_relative_path" in artifact_columns
    assert "removed_works" in tables
    # Migration 015: explicit archive paths. A table keyed by candidate rather
    # than the 014 column, because an operator sets where a book belongs *before*
    # the first pack as readily as after it -- and an artifact row only exists
    # once a pack has produced one. The unique index is the guard behind
    # 「名称已存在」: two books pinned to one path would race at pack time and
    # the loser would be overwritten with no trace.
    assert "work_archive_paths" in tables
    assert "idx_work_archive_paths_relative" in indexes
    assert {
        "candidate_id",
        "relative_path",
        "is_manual",
        "operator_name",
    } <= archive_path_columns
    assert {
        "hash",
        "kind",
        "variant",
        "source_url",
        "state",
        "content_type",
        "byte_size",
        "width",
        "height",
        "error_code",
        "attempt_count",
    } <= thumbnail_columns
    # Migration 017: archive-path routing rules. A rule pairs an auto-approval
    # condition with its own layout template, so a work's path can be chosen by
    # matching instead of every book following one global template.
    assert "archive_path_rules" in tables
    assert "idx_archive_path_rules_enabled_priority" in indexes
    assert {
        "path_template",
        "condition_json",
        "dsl_snapshot",
        "case_sensitive",
    } <= routing_rule_columns


@pytest.mark.asyncio
async def test_migration_012_defaults_are_backfilled_on_existing_rows(
    tmp_path: Path,
) -> None:
    """The two NOT NULL columns must land on rows that predate them.

    `is_locked` and `priority` are added to populated tables, so a DEFAULT that
    SQLite failed to apply would leave existing candidates unschedulable rather
    than merely unlocked -- worth asserting rather than trusting.
    """
    database = Database(tmp_path / "ehbot.db")
    await database.initialize()

    with sqlite3.connect(tmp_path / "ehbot.db") as connection:
        connection.execute(
            "INSERT INTO candidates (id, status) VALUES (1, 'PENDING_REVIEW')"
        )
        connection.execute(
            "INSERT INTO metadata_values "
            "(candidate_id, field_name, field_value, value_source) "
            "VALUES (1, 'Title', 'A title', 'EXHENTAI')"
        )
        connection.execute(
            "INSERT INTO download_jobs "
            "(candidate_id, provider, idempotency_key, state) "
            "VALUES (1, 'TELEGRAM', 'key-1', 'PENDING')"
        )
        assert connection.execute(
            "SELECT is_locked FROM metadata_values WHERE candidate_id = 1"
        ).fetchone()[0] == 0
        assert connection.execute(
            "SELECT priority FROM download_jobs WHERE candidate_id = 1"
        ).fetchone()[0] == 100
        assert connection.execute(
            "SELECT thumb_url FROM candidates WHERE id = 1"
        ).fetchone()[0] is None


@pytest.mark.asyncio
async def test_telegram_updates_are_persisted_idempotently(tmp_path: Path) -> None:
    database = Database(tmp_path / "ehbot.db")
    await database.initialize()

    first_insert = await database.save_telegram_updates(
        [{"update_id": 100, "message": {"text": "first"}}]
    )
    duplicate_insert = await database.save_telegram_updates(
        [{"update_id": 100, "message": {"text": "first"}}]
    )

    assert first_insert == 1
    assert duplicate_insert == 0
    assert await database.latest_telegram_update_id() == 100


@pytest.mark.asyncio
async def test_connection_helper_closes_and_commits(tmp_path: Path) -> None:
    """`with sqlite3.connect(...)` ends the transaction; it does not close.

    Every query in the service layer used the bare connection, so each one left
    a handle for the garbage collector to find. On a WAL database an open
    handle holds its read snapshot, which is what keeps `-wal` from being
    checkpointed away.
    """
    database = Database(tmp_path / "ehbot.db")
    await database.initialize()

    with database.connection() as connection:
        connection.execute(
            "INSERT INTO admin_users (username, password_hash, password_changed) "
            "VALUES ('probe', 'hash', 0)"
        )

    # Closed on the way out: using it again is an error, not a silent success.
    with pytest.raises(sqlite3.ProgrammingError):
        connection.execute("SELECT 1")

    # And the write inside the block was committed.
    with database.connection() as verify:
        stored = verify.execute(
            "SELECT COUNT(*) FROM admin_users WHERE username = 'probe'"
        ).fetchone()[0]
    assert stored == 1


@pytest.mark.asyncio
async def test_connection_helper_rolls_back_and_still_closes(
    tmp_path: Path,
) -> None:
    """A raising block must not commit, and must not leak the handle either."""
    database = Database(tmp_path / "ehbot.db")
    await database.initialize()

    with pytest.raises(RuntimeError):
        with database.connection() as connection:
            connection.execute(
                "INSERT INTO admin_users (username, password_hash, password_changed) "
                "VALUES ('rolled-back', 'hash', 0)"
            )
            raise RuntimeError("boom")

    with pytest.raises(sqlite3.ProgrammingError):
        connection.execute("SELECT 1")

    with database.connection() as verify:
        stored = verify.execute(
            "SELECT COUNT(*) FROM admin_users WHERE username = 'rolled-back'"
        ).fetchone()[0]
    assert stored == 0
