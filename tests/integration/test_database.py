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

    assert migration_count == 2
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
        "schema_migrations",
    } <= tables
