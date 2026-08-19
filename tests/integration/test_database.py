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

    assert migration_count == 5
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
        "schema_migrations",
    } <= tables
    assert {"processed_at", "processing_result", "processing_reason"} <= update_columns
    assert "idx_telegram_bot_updates_pending" in update_indexes


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
