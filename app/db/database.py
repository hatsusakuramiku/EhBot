from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path


class Database:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.initialized = False

    async def initialize(self) -> None:
        await asyncio.to_thread(self._initialize_sync)
        self.initialized = True

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=5)
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 5000")
        return connection

    def _initialize_sync(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        migrations_path = Path(__file__).with_name("migrations")
        with self._connect() as connection:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.execute("PRAGMA synchronous = NORMAL")
            connection.execute(
                "CREATE TABLE IF NOT EXISTS schema_migrations "
                "(version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)"
            )
            applied = {
                row[0]
                for row in connection.execute("SELECT version FROM schema_migrations")
            }
            for migration in sorted(migrations_path.glob("*.sql")):
                version = int(migration.stem.split("_", 1)[0])
                if version in applied:
                    continue
                migration_sql = migration.read_text(encoding="utf-8")
                connection.executescript(
                    "BEGIN IMMEDIATE;\n"
                    f"{migration_sql}\n"
                    "INSERT INTO schema_migrations (version, applied_at) "
                    f"VALUES ({version}, CURRENT_TIMESTAMP);\n"
                    "COMMIT;"
                )

    async def check_writable(self) -> bool:
        if not self.initialized:
            return False
        return await asyncio.to_thread(self._check_writable_sync)

    def _check_writable_sync(self) -> bool:
        try:
            with self._connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                connection.rollback()
            return True
        except sqlite3.Error:
            return False

    async def get_admin_auth(self, username: str) -> tuple[str, bool] | None:
        return await asyncio.to_thread(self._get_admin_auth_sync, username)

    def _get_admin_auth_sync(self, username: str) -> tuple[str, bool] | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT password_hash, password_changed "
                "FROM admin_users WHERE username = ?",
                (username,),
            ).fetchone()
        if row is None:
            return None
        return row[0], bool(row[1])

    async def set_bootstrap_admin(self, username: str, password_hash: str) -> None:
        await asyncio.to_thread(
            self._set_bootstrap_admin_sync, username, password_hash
        )

    def _set_bootstrap_admin_sync(self, username: str, password_hash: str) -> None:
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO admin_users (username, password_hash, password_changed) "
                "VALUES (?, ?, 0) "
                "ON CONFLICT(username) DO UPDATE SET "
                "password_hash = excluded.password_hash, "
                "updated_at = CURRENT_TIMESTAMP "
                "WHERE admin_users.password_changed = 0",
                (username, password_hash),
            )

    async def change_admin_password(
        self, username: str, password_hash: str
    ) -> None:
        await asyncio.to_thread(
            self._change_admin_password_sync, username, password_hash
        )

    def _change_admin_password_sync(self, username: str, password_hash: str) -> None:
        with self._connect() as connection:
            cursor = connection.execute(
                "UPDATE admin_users SET password_hash = ?, password_changed = 1, "
                "updated_at = CURRENT_TIMESTAMP WHERE username = ?",
                (password_hash, username),
            )
            if cursor.rowcount != 1:
                raise LookupError(f"Administrator account {username!r} does not exist")
