import sqlite3
from pathlib import Path

import pytest

from app.db.database import Database


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

    assert migration_count == 12
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
