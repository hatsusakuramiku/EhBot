from __future__ import annotations

import asyncio
import json
import re
import sqlite3
from pathlib import Path

from app.candidates.models import (
    CandidateDetail,
    CandidateListItem,
    CandidateMessage,
    ParsedSourceMessage,
    TelegramSourceConfig,
)

from app.review.models import MetadataEntry, ReviewActionEntry


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

    @staticmethod
    def _ensure_bot_account(connection: sqlite3.Connection) -> int:
        connection.execute(
            "INSERT INTO telegram_accounts "
            "(account_type, display_name, session_path, status) "
            "VALUES ('BOT', 'Telegram Bot API', 'bot-api://configured', "
            "'DISCONNECTED') ON CONFLICT(session_path) DO NOTHING"
        )
        return int(
            connection.execute(
                "SELECT id FROM telegram_accounts "
                "WHERE session_path = 'bot-api://configured'"
            ).fetchone()[0]
        )

    @staticmethod
    def _source_from_row(row: sqlite3.Row | tuple) -> TelegramSourceConfig:
        enabled = bool(row[4])
        try:
            rules = json.loads(row[5])
            if not isinstance(rules, dict):
                raise ValueError("source rules must be an object")
            raw_formats = rules.get("allowed_archive_formats", [])
            if not isinstance(raw_formats, list) or any(
                value not in {"zip", "rar", "7z", "cbz"}
                for value in raw_formats
            ):
                raise ValueError("invalid archive formats")
            max_attachment_size_mb = int(
                rules.get("max_attachment_size_mb", 0)
            )
            if max_attachment_size_mb < 0:
                raise ValueError("invalid attachment size")
        except (TypeError, ValueError, json.JSONDecodeError):
            enabled = False
            raw_formats = []
            max_attachment_size_mb = 0
        return TelegramSourceConfig(
            source_id=int(row[0]),
            source_type=str(row[1]),
            chat_id=int(row[2]),
            display_name=str(row[3]),
            enabled=enabled,
            allowed_archive_formats=tuple(
                str(value).lower() for value in raw_formats
            ),
            max_attachment_size_mb=max_attachment_size_mb,
        )

    @staticmethod
    def _update_candidate_filter_state(
        connection: sqlite3.Connection,
        candidate_id: int,
        fallback_reason: str,
    ) -> None:
        has_title = connection.execute(
            "SELECT 1 FROM metadata_values WHERE candidate_id = ? "
            "AND field_name = 'Title' LIMIT 1",
            (candidate_id,),
        ).fetchone()
        needs_rows = connection.execute(
            "SELECT sm.filter_reason FROM candidate_messages cm "
            "JOIN source_messages sm ON sm.id = cm.source_message_id "
            "WHERE cm.candidate_id = ? AND sm.filter_result = 'NEEDS_INFO' "
            "ORDER BY sm.id",
            (candidate_id,),
        ).fetchall()
        needs_reason = next(
            (
                str(row[0])
                for row in needs_rows
                if row[0] != "缺少可识别标题" or has_title is None
            ),
            None,
        )
        connection.execute(
            "UPDATE candidates SET status = ?, filter_result = ?, "
            "filter_reason = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
            (
                "NEEDS_INFO" if needs_reason is not None else "PENDING_REVIEW",
                "NEEDS_INFO" if needs_reason is not None else "ACCEPT",
                needs_reason or fallback_reason,
                candidate_id,
            ),
        )

    async def configure_telegram_source(
        self,
        *,
        source_type: str,
        chat_id: int,
        display_name: str,
        enabled: bool,
        allowed_archive_formats: tuple[str, ...],
        max_attachment_size_mb: int,
    ) -> None:
        await asyncio.to_thread(
            self._configure_telegram_source_sync,
            source_type,
            chat_id,
            display_name,
            enabled,
            allowed_archive_formats,
            max_attachment_size_mb,
        )

    def _configure_telegram_source_sync(
        self,
        source_type: str,
        chat_id: int,
        display_name: str,
        enabled: bool,
        allowed_archive_formats: tuple[str, ...],
        max_attachment_size_mb: int,
    ) -> None:
        rules_json = json.dumps(
            {
                "allowed_archive_formats": list(allowed_archive_formats),
                "max_attachment_size_mb": max_attachment_size_mb,
            },
            separators=(",", ":"),
        )
        with self._connect() as connection:
            account_id = self._ensure_bot_account(connection)
            connection.execute(
                "INSERT INTO telegram_sources "
                "(account_id, source_type, chat_id, display_name, enabled, rules_json) "
                "VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT(account_id, chat_id) "
                "DO UPDATE SET source_type = excluded.source_type, "
                "display_name = excluded.display_name, enabled = excluded.enabled, "
                "rules_json = excluded.rules_json",
                (
                    account_id,
                    source_type,
                    chat_id,
                    display_name,
                    int(enabled),
                    rules_json,
                ),
            )

    async def list_telegram_sources(self) -> list[TelegramSourceConfig]:
        return await asyncio.to_thread(self._list_telegram_sources_sync)

    def _list_telegram_sources_sync(self) -> list[TelegramSourceConfig]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT ts.id, ts.source_type, ts.chat_id, ts.display_name, "
                "ts.enabled, ts.rules_json FROM telegram_sources ts "
                "JOIN telegram_accounts ta ON ta.id = ts.account_id "
                "WHERE ta.session_path = 'bot-api://configured' "
                "ORDER BY ts.enabled DESC, ts.id DESC"
            ).fetchall()
        return [self._source_from_row(row) for row in rows]

    async def discover_telegram_source(
        self, message: ParsedSourceMessage
    ) -> TelegramSourceConfig:
        return await asyncio.to_thread(self._discover_telegram_source_sync, message)

    def _discover_telegram_source_sync(
        self, message: ParsedSourceMessage
    ) -> TelegramSourceConfig:
        with self._connect() as connection:
            account_id = self._ensure_bot_account(connection)
            connection.execute(
                "INSERT INTO telegram_sources "
                "(account_id, source_type, chat_id, display_name, enabled) "
                "VALUES (?, ?, ?, ?, 0) ON CONFLICT(account_id, chat_id) "
                "DO NOTHING",
                (
                    account_id,
                    "CHANNEL" if message.chat_id < 0 else "PRIVATE_CHAT",
                    message.chat_id,
                    message.chat_title,
                ),
            )
            row = connection.execute(
                "SELECT id, source_type, chat_id, display_name, enabled, rules_json "
                "FROM telegram_sources WHERE account_id = ? AND chat_id = ?",
                (account_id, message.chat_id),
            ).fetchone()
        return self._source_from_row(row)

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

    async def save_telegram_updates(self, updates: list[dict]) -> int:
        return await asyncio.to_thread(self._save_telegram_updates_sync, updates)

    def _save_telegram_updates_sync(self, updates: list[dict]) -> int:
        with self._connect() as connection:
            changes_before = connection.total_changes
            connection.executemany(
                "INSERT OR IGNORE INTO telegram_bot_updates "
                "(update_id, payload_json) VALUES (?, ?)",
                [
                    (
                        int(update["update_id"]),
                        json.dumps(update, ensure_ascii=False, separators=(",", ":")),
                    )
                    for update in updates
                ],
            )
            return connection.total_changes - changes_before

    async def latest_telegram_update_id(self) -> int | None:
        return await asyncio.to_thread(self._latest_telegram_update_id_sync)

    def _latest_telegram_update_id_sync(self) -> int | None:
        with self._connect() as connection:
            value = connection.execute(
                "SELECT MAX(update_id) FROM telegram_bot_updates"
            ).fetchone()[0]
        return int(value) if value is not None else None

    async def pending_telegram_updates(
        self, *, limit: int
    ) -> list[tuple[int, dict]]:
        return await asyncio.to_thread(self._pending_telegram_updates_sync, limit)

    def _pending_telegram_updates_sync(self, limit: int) -> list[tuple[int, dict]]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT update_id, payload_json FROM telegram_bot_updates "
                "WHERE processed_at IS NULL ORDER BY update_id LIMIT ?",
                (limit,),
            ).fetchall()
        return [(int(row[0]), json.loads(row[1])) for row in rows]

    async def mark_telegram_update_result(
        self, update_id: int, result: str, reason: str
    ) -> None:
        await asyncio.to_thread(
            self._mark_telegram_update_result_sync,
            update_id,
            result,
            reason,
        )

    def _mark_telegram_update_result_sync(
        self, update_id: int, result: str, reason: str
    ) -> None:
        with self._connect() as connection:
            connection.execute(
                "UPDATE telegram_bot_updates SET processed_at = CURRENT_TIMESTAMP, "
                "processing_result = ?, processing_reason = ? "
                "WHERE update_id = ?",
                (result, reason, update_id),
            )

    async def deactivate_candidate_message(self, chat_id: int, message_id: int) -> None:
        await asyncio.to_thread(
            self._deactivate_candidate_message_sync, chat_id, message_id
        )

    def _deactivate_candidate_message_sync(
        self, chat_id: int, message_id: int
    ) -> None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT sm.id FROM source_messages sm "
                "JOIN telegram_accounts ta ON ta.id = sm.account_id "
                "WHERE ta.session_path = 'bot-api://configured' "
                "AND sm.chat_id = ? AND sm.message_id = ?",
                (chat_id, message_id),
            ).fetchone()
            if row is None:
                return
            source_message_id = int(row[0])
            candidate_ids = [
                int(candidate_row[0])
                for candidate_row in connection.execute(
                    "SELECT candidate_id FROM candidate_messages "
                    "WHERE source_message_id = ?",
                    (source_message_id,),
                ).fetchall()
            ]
            connection.execute(
                "UPDATE source_messages SET message_state = 'INACTIVE', "
                "message_text = '', attachment_json = '[]', file_unique_id = NULL "
                "WHERE id = ?",
                (source_message_id,),
            )
            connection.execute(
                "DELETE FROM candidate_messages WHERE source_message_id = ?",
                (source_message_id,),
            )
            for candidate_id in candidate_ids:
                remaining_messages = connection.execute(
                    "SELECT sm.message_text, sm.attachment_json "
                    "FROM candidate_messages cm JOIN source_messages sm "
                    "ON sm.id = cm.source_message_id WHERE cm.candidate_id = ? "
                    "ORDER BY sm.message_date DESC, sm.id DESC",
                    (candidate_id,),
                ).fetchall()
                if not remaining_messages:
                    connection.execute(
                        "DELETE FROM candidates WHERE id = ?", (candidate_id,)
                    )
                    continue
                best_title = None
                best_title_source = None
                best_title_confidence = -1.0
                ex_gid = None
                ex_gallery_token = None
                filter_reason = "包含候选内容"
                for message_text, attachment_json in remaining_messages:
                    text = str(message_text or "").strip()
                    gallery_match = re.search(
                        r"https?://(?:exhentai\.org|e-hentai\.org)/g/"
                        r"(\d+)/([A-Za-z0-9]+)/?",
                        text,
                        flags=re.IGNORECASE,
                    )
                    if gallery_match is not None and ex_gid is None:
                        ex_gid = int(gallery_match.group(1))
                        ex_gallery_token = gallery_match.group(2)
                    attachments = json.loads(attachment_json)
                    explicit_title = next(
                        (
                            line.strip()
                            for line in text.splitlines()
                            if line.strip()
                            and not line.strip().lower().startswith("http")
                        ),
                        None,
                    )
                    archive = next(
                        (
                            attachment
                            for attachment in attachments
                            if attachment.get("type") == "archive"
                        ),
                        None,
                    )
                    if explicit_title is not None:
                        title = explicit_title
                        title_source = "TELEGRAM"
                        title_confidence = 0.9
                    elif archive is not None:
                        title = Path(str(archive.get("file_name") or "")).stem
                        title_source = "FILENAME"
                        title_confidence = 0.5
                    elif gallery_match is not None:
                        title = f"ExHentai #{gallery_match.group(1)}"
                        title_source = "INFERRED"
                        title_confidence = 0.2
                    else:
                        continue
                    if title and title_confidence > best_title_confidence:
                        best_title = title
                        best_title_source = title_source
                        best_title_confidence = title_confidence
                    if any(item.get("type") == "photo" for item in attachments):
                        filter_reason = "包含图片预览"
                    elif archive is not None and filter_reason != "包含图片预览":
                        filter_reason = "包含压缩包附件"
                    elif gallery_match is not None and filter_reason == "包含候选内容":
                        filter_reason = "包含 ExHentai 画廊链接"
                connection.execute(
                    "UPDATE candidates SET ex_gid = NULL, ex_gallery_token = NULL, "
                    "filter_reason = ?, "
                    "updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                    (filter_reason, candidate_id),
                )
                connection.execute(
                    "DELETE FROM metadata_values WHERE candidate_id = ? "
                    "AND is_manual = 0",
                    (candidate_id,),
                )
                connection.execute(
                    "UPDATE candidates SET ex_gid = ?, ex_gallery_token = ? "
                    "WHERE id = ?",
                    (ex_gid, ex_gallery_token, candidate_id),
                )
                if best_title is not None:
                    connection.execute(
                        "INSERT OR IGNORE INTO metadata_values "
                        "(candidate_id, field_name, field_value, value_source, "
                        "confidence) VALUES (?, 'Title', ?, ?, ?)",
                        (
                            candidate_id,
                            best_title,
                            best_title_source,
                            best_title_confidence,
                        ),
                    )
                self._update_candidate_filter_state(
                    connection, candidate_id, filter_reason
                )

    async def save_candidate_message(
        self, update_id: int, message: ParsedSourceMessage
    ) -> bool:
        return await asyncio.to_thread(
            self._save_candidate_message_sync, update_id, message
        )

    def _save_candidate_message_sync(
        self, update_id: int, message: ParsedSourceMessage
    ) -> bool:
        with self._connect() as connection:
            account_id = self._ensure_bot_account(connection)
            connection.execute(
                "INSERT INTO telegram_sources "
                "(account_id, source_type, chat_id, display_name, enabled) "
                "VALUES (?, ?, ?, ?, 0) ON CONFLICT(account_id, chat_id) "
                "DO NOTHING",
                (
                    account_id,
                    "CHANNEL" if message.chat_id < 0 else "PRIVATE_CHAT",
                    message.chat_id,
                    message.chat_title,
                ),
            )
            cursor = connection.execute(
                "INSERT OR IGNORE INTO source_messages "
                "(account_id, chat_id, message_id, sender_id, "
                "reply_to_message_id, media_group_id, message_text, "
                "attachment_json, file_unique_id, message_date, filter_result, "
                "filter_reason) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    account_id,
                    message.chat_id,
                    message.message_id,
                    message.sender_id,
                    message.reply_to_message_id,
                    message.media_group_id,
                    message.message_text,
                    json.dumps(
                        message.attachments,
                        ensure_ascii=False,
                        separators=(",", ":"),
                    ),
                    message.file_unique_id,
                    message.message_date,
                    message.filter_result,
                    message.filter_reason,
                ),
            )
            created_message = cursor.rowcount == 1
            if not created_message and message.is_edit:
                connection.execute(
                    "UPDATE source_messages SET sender_id = ?, "
                    "reply_to_message_id = ?, message_text = ?, "
                    "attachment_json = ?, file_unique_id = ?, "
                    "filter_result = ?, filter_reason = ?, "
                    "message_state = 'ACTIVE' WHERE account_id = ? "
                    "AND chat_id = ? AND message_id = ?",
                    (
                        message.sender_id,
                        message.reply_to_message_id,
                        message.message_text,
                        json.dumps(
                            message.attachments,
                            ensure_ascii=False,
                            separators=(",", ":"),
                        ),
                        message.file_unique_id,
                        message.filter_result,
                        message.filter_reason,
                        account_id,
                        message.chat_id,
                        message.message_id,
                    ),
                )
            source_message_id = int(
                connection.execute(
                    "SELECT id FROM source_messages "
                    "WHERE account_id = ? AND chat_id = ? AND message_id = ?",
                    (account_id, message.chat_id, message.message_id),
                ).fetchone()[0]
            )
            created_candidate = False
            if created_message or message.is_edit:
                candidate_id = None
                existing_candidate_id = None
                if message.is_edit and not created_message:
                    row = connection.execute(
                        "SELECT candidate_id FROM candidate_messages "
                        "WHERE source_message_id = ? LIMIT 1",
                        (source_message_id,),
                    ).fetchone()
                    existing_candidate_id = (
                        int(row[0]) if row is not None else None
                    )
                    candidate_id = existing_candidate_id
                if candidate_id is None and message.media_group_id is not None:
                    row = connection.execute(
                        "SELECT cm.candidate_id FROM candidate_messages cm "
                        "JOIN source_messages sm ON sm.id = cm.source_message_id "
                        "WHERE sm.account_id = ? AND sm.chat_id = ? "
                        "AND sm.media_group_id = ? LIMIT 1",
                        (account_id, message.chat_id, message.media_group_id),
                    ).fetchone()
                    candidate_id = int(row[0]) if row is not None else None
                if candidate_id is None and message.reply_to_message_id is not None:
                    row = connection.execute(
                        "SELECT cm.candidate_id FROM candidate_messages cm "
                        "JOIN source_messages sm ON sm.id = cm.source_message_id "
                        "WHERE sm.account_id = ? AND sm.chat_id = ? "
                        "AND sm.message_id = ? LIMIT 1",
                        (
                            account_id,
                            message.chat_id,
                            message.reply_to_message_id,
                        ),
                    ).fetchone()
                    candidate_id = int(row[0]) if row is not None else None
                if candidate_id is None and message.title and message.attachments:
                    current_type = str(message.attachments[0].get("type", ""))
                    rows = connection.execute(
                        "SELECT cm.candidate_id, sm.attachment_json, "
                        "(SELECT mv.field_value FROM metadata_values mv "
                        " WHERE mv.candidate_id = cm.candidate_id "
                        " AND mv.field_name = 'Title' ORDER BY mv.is_manual DESC, "
                        " mv.confidence DESC LIMIT 1) FROM candidate_messages cm "
                        "JOIN source_messages sm ON sm.id = cm.source_message_id "
                        "WHERE sm.account_id = ? AND sm.chat_id = ? "
                        "AND sm.sender_id IS ? AND ABS(strftime('%s', sm.message_date) "
                        "- strftime('%s', ?)) <= 180 "
                        "AND ABS(sm.message_id - ?) = 1 ORDER BY sm.message_date DESC "
                        "LIMIT 10",
                        (
                            account_id,
                            message.chat_id,
                            message.sender_id,
                            message.message_date,
                            message.message_id,
                        ),
                    ).fetchall()
                    matching_candidate_ids = set()
                    for adjacent_row in rows:
                        adjacent_attachments = json.loads(adjacent_row[1])
                        adjacent_type = (
                            str(adjacent_attachments[0].get("type", ""))
                            if adjacent_attachments
                            else ""
                        )
                        adjacent_title = str(adjacent_row[2] or "")
                        if {
                            current_type,
                            adjacent_type,
                        } == {"photo", "archive"} and (
                            adjacent_title.strip().casefold()
                            == message.title.strip().casefold()
                        ):
                            matching_candidate_ids.add(int(adjacent_row[0]))
                    if len(matching_candidate_ids) == 1:
                        candidate_id = matching_candidate_ids.pop()
                ex_candidate_id = None
                if message.ex_gid is not None:
                    row = connection.execute(
                        "SELECT id FROM candidates WHERE ex_gid = ? "
                        "AND ex_gallery_token = ? LIMIT 1",
                        (message.ex_gid, message.ex_gallery_token),
                    ).fetchone()
                    ex_candidate_id = int(row[0]) if row is not None else None
                if candidate_id is None:
                    candidate_id = ex_candidate_id
                elif ex_candidate_id is not None and ex_candidate_id != candidate_id:
                    connection.execute(
                        "INSERT OR IGNORE INTO candidate_messages "
                        "(candidate_id, source_message_id) SELECT ?, "
                        "source_message_id FROM candidate_messages "
                        "WHERE candidate_id = ?",
                        (candidate_id, ex_candidate_id),
                    )
                    connection.execute(
                        "DELETE FROM candidate_messages WHERE candidate_id = ?",
                        (ex_candidate_id,),
                    )
                    connection.execute(
                        "INSERT OR IGNORE INTO metadata_values "
                        "(candidate_id, field_name, field_value, value_source, "
                        "confidence, is_manual, created_at) SELECT ?, field_name, "
                        "field_value, value_source, confidence, is_manual, created_at "
                        "FROM metadata_values WHERE candidate_id = ?",
                        (candidate_id, ex_candidate_id),
                    )
                    connection.execute(
                        "DELETE FROM metadata_values WHERE candidate_id = ?",
                        (ex_candidate_id,),
                    )
                    connection.execute(
                        "UPDATE review_actions SET candidate_id = ? "
                        "WHERE candidate_id = ?",
                        (candidate_id, ex_candidate_id),
                    )
                    connection.execute(
                        "UPDATE download_jobs SET candidate_id = ? "
                        "WHERE candidate_id = ?",
                        (candidate_id, ex_candidate_id),
                    )
                    connection.execute(
                        "DELETE FROM candidates WHERE id = ?", (ex_candidate_id,)
                    )
                if candidate_id is None:
                    candidate_status = (
                        "NEEDS_INFO"
                        if message.filter_result == "NEEDS_INFO"
                        else "PENDING_REVIEW"
                    )
                    candidate_cursor = connection.execute(
                        "INSERT INTO candidates "
                        "(status, ex_gid, ex_gallery_token, filter_result, "
                        "filter_reason) VALUES (?, ?, ?, ?, ?)",
                        (
                            candidate_status,
                            message.ex_gid,
                            message.ex_gallery_token,
                            message.filter_result,
                            message.filter_reason,
                        ),
                    )
                    candidate_id = int(candidate_cursor.lastrowid)
                    created_candidate = True
                elif message.is_edit and existing_candidate_id is not None:
                    connection.execute(
                        "UPDATE candidates SET ex_gid = ?, ex_gallery_token = ?, "
                        "updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                        (
                            message.ex_gid,
                            message.ex_gallery_token,
                            candidate_id,
                        ),
                    )
                elif message.ex_gid is not None:
                    connection.execute(
                        "UPDATE candidates SET ex_gid = ?, ex_gallery_token = ?, "
                        "updated_at = CURRENT_TIMESTAMP WHERE id = ? "
                        "AND ex_gid IS NULL",
                        (
                            message.ex_gid,
                            message.ex_gallery_token,
                            candidate_id,
                        ),
                    )
                connection.execute(
                    "INSERT OR IGNORE INTO candidate_messages "
                    "(candidate_id, source_message_id) VALUES (?, ?)",
                    (candidate_id, source_message_id),
                )
                if message.is_edit:
                    connection.execute(
                        "DELETE FROM candidate_messages WHERE source_message_id = ? "
                        "AND candidate_id != ?",
                        (source_message_id, candidate_id),
                    )
                    connection.execute(
                        "DELETE FROM metadata_values WHERE candidate_id = ? "
                        "AND field_name = 'Title' AND is_manual = 0",
                        (candidate_id,),
                    )
                if message.title:
                    if message.is_edit:
                        connection.execute(
                            "INSERT INTO metadata_values "
                            "(candidate_id, field_name, field_value, value_source, "
                            "confidence) VALUES (?, 'Title', ?, ?, ?) "
                            "ON CONFLICT(candidate_id, field_name, value_source) "
                            "DO UPDATE SET field_value = excluded.field_value, "
                            "confidence = excluded.confidence, "
                            "created_at = CURRENT_TIMESTAMP",
                            (
                                candidate_id,
                                message.title,
                                message.title_source,
                                message.title_confidence,
                            ),
                        )
                    else:
                        connection.execute(
                            "INSERT OR IGNORE INTO metadata_values "
                            "(candidate_id, field_name, field_value, value_source, "
                            "confidence) VALUES (?, 'Title', ?, ?, ?)",
                            (
                                candidate_id,
                                message.title,
                                message.title_source,
                                message.title_confidence,
                            ),
                        )
                self._update_candidate_filter_state(
                    connection, candidate_id, message.filter_reason
                )
            connection.execute(
                "UPDATE telegram_bot_updates SET processed_at = CURRENT_TIMESTAMP, "
                "processing_result = ?, processing_reason = ? "
                "WHERE update_id = ?",
                (message.filter_result, message.filter_reason, update_id),
            )
            return created_candidate

    async def list_candidates(
        self, *, status: str = "PENDING_REVIEW", limit: int = 100
    ) -> list[CandidateListItem]:
        return await asyncio.to_thread(
            self._list_candidates_sync, status, limit
        )

    def _list_candidates_sync(
        self, status: str, limit: int
    ) -> list[CandidateListItem]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT c.id, c.status, c.filter_result, "
                "(SELECT mv.field_value FROM metadata_values mv "
                " WHERE mv.candidate_id = c.id AND mv.field_name = 'Title' "
                " ORDER BY mv.is_manual DESC, mv.confidence DESC LIMIT 1), "
                "COUNT(cm.source_message_id), c.updated_at, c.ex_gid, "
                "c.ex_gallery_token "
                "FROM candidates c LEFT JOIN candidate_messages cm "
                "ON cm.candidate_id = c.id WHERE c.status = ? "
                "GROUP BY c.id ORDER BY c.id DESC LIMIT ?",
                (status, limit),
            ).fetchall()
        return [
            CandidateListItem(
                candidate_id=int(row[0]),
                status=str(row[1]),
                filter_result=str(row[2]),
                title=str(row[3]) if row[3] is not None else None,
                message_count=int(row[4]),
                updated_at=str(row[5]),
                ex_gid=int(row[6]) if row[6] is not None else None,
                ex_gallery_token=str(row[7]) if row[7] is not None else None,
            )
            for row in rows
        ]

    async def candidate_counts(self) -> dict[str, int]:
        return await asyncio.to_thread(self._candidate_counts_sync)

    def _candidate_counts_sync(self) -> dict[str, int]:
        counts = {
            "pending_review": 0,
            "needs_info": 0,
            "processing": 0,
            "failed": 0,
        }
        status_keys = {
            "PENDING_REVIEW": "pending_review",
            "NEEDS_INFO": "needs_info",
            "PROCESSING": "processing",
            "FAILED": "failed",
        }
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT status, COUNT(*) FROM candidates GROUP BY status"
            ).fetchall()
        for status, count in rows:
            key = status_keys.get(str(status))
            if key is not None:
                counts[key] = int(count)
        return counts

    async def get_candidate(self, candidate_id: int) -> CandidateDetail | None:
        return await asyncio.to_thread(self._get_candidate_sync, candidate_id)

    def _get_candidate_sync(self, candidate_id: int) -> CandidateDetail | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT c.id, c.status, c.filter_result, c.filter_reason, "
                "(SELECT mv.field_value FROM metadata_values mv "
                " WHERE mv.candidate_id = c.id AND mv.field_name = 'Title' "
                " ORDER BY mv.is_manual DESC, mv.confidence DESC LIMIT 1), "
                "c.ex_gid, c.ex_gallery_token FROM candidates c WHERE c.id = ?",
                (candidate_id,),
            ).fetchone()
            if row is None:
                return None
            message_rows = connection.execute(
                "SELECT COALESCE(ts.display_name, CAST(sm.chat_id AS TEXT)), "
                "sm.message_id, sm.message_text, sm.attachment_json, "
                "sm.message_date FROM candidate_messages cm "
                "JOIN source_messages sm ON sm.id = cm.source_message_id "
                "LEFT JOIN telegram_sources ts ON ts.account_id = sm.account_id "
                "AND ts.chat_id = sm.chat_id WHERE cm.candidate_id = ? "
                "ORDER BY sm.message_date, sm.message_id",
                (candidate_id,),
            ).fetchall()
        messages = tuple(
            CandidateMessage(
                chat_title=str(message_row[0]),
                message_id=int(message_row[1]),
                message_text=str(message_row[2] or ""),
                attachments=tuple(json.loads(message_row[3])),
                message_date=str(message_row[4]),
            )
            for message_row in message_rows
        )
        return CandidateDetail(
            candidate_id=int(row[0]),
            status=str(row[1]),
            filter_result=str(row[2]),
            filter_reason=str(row[3]),
            title=str(row[4]) if row[4] is not None else None,
            ex_gid=int(row[5]) if row[5] is not None else None,
            ex_gallery_token=str(row[6]) if row[6] is not None else None,
            messages=messages,
        )


    _REVIEWABLE_STATUSES = frozenset(
        {"PENDING_REVIEW", "NEEDS_INFO", "NEEDS_REVISION", "REJECTED"}
    )


    async def transition_candidate_status(
        self,
        candidate_id: int,
        operator_name: str,
        action: str,
        new_status: str,
        note: str | None,
    ) -> None:
        await asyncio.to_thread(
            self._transition_candidate_status_sync,
            candidate_id,
            operator_name,
            action,
            new_status,
            note,
        )


    def _transition_candidate_status_sync(
        self,
        candidate_id: int,
        operator_name: str,
        action: str,
        new_status: str,
        note: str | None,
    ) -> None:
        details = {"status": new_status}
        if note:
            details["note"] = note
        details_json = json.dumps(details, separators=(",", ":"))
        with self._connect() as connection:
            cursor = connection.execute(
                "SELECT status FROM candidates WHERE id = ?",
                (candidate_id,),
            )
            row = cursor.fetchone()
            if row is None:
                raise LookupError(f"Candidate {candidate_id} does not exist")
            current_status = str(row[0])
            if current_status not in self._REVIEWABLE_STATUSES:
                raise PermissionError(
                    f"Candidate in state {current_status} cannot be reviewed"
                )
            connection.execute(
                "UPDATE candidates SET status = ?, filter_reason = ?, "
                "updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                (new_status, note or "", candidate_id),
            )
            connection.execute(
                "INSERT INTO review_actions "
                "(candidate_id, action, operator_name, details_json) "
                "VALUES (?, ?, ?, ?)",
                (candidate_id, action, operator_name, details_json),
            )


    async def set_manual_metadata(
        self,
        candidate_id: int,
        operator_name: str,
        field_name: str,
        field_value: str,
    ) -> None:
        await asyncio.to_thread(
            self._set_manual_metadata_sync,
            candidate_id,
            operator_name,
            field_name,
            field_value,
        )


    def _set_manual_metadata_sync(
        self,
        candidate_id: int,
        operator_name: str,
        field_name: str,
        field_value: str,
    ) -> None:
        with self._connect() as connection:
            candidate_row = connection.execute(
                "SELECT 1 FROM candidates WHERE id = ?",
                (candidate_id,),
            ).fetchone()
            if candidate_row is None:
                raise LookupError(f"Candidate {candidate_id} does not exist")
            connection.execute(
                "INSERT INTO metadata_values "
                "(candidate_id, field_name, field_value, value_source, "
                "confidence, is_manual) VALUES (?, ?, ?, 'OPERATOR', 1.0, 1) "
                "ON CONFLICT(candidate_id, field_name, value_source) "
                "DO UPDATE SET field_value = excluded.field_value, "
                "confidence = 1.0, is_manual = 1, "
                "created_at = CURRENT_TIMESTAMP",
                (candidate_id, field_name, field_value),
            )
            connection.execute(
                "INSERT INTO review_actions "
                "(candidate_id, action, operator_name, details_json) "
                "VALUES (?, 'EDIT_METADATA', ?, ?)",
                (
                    candidate_id,
                    operator_name,
                    json.dumps(
                        {"field": field_name, "value": field_value},
                        separators=(",", ":"),
                    ),
                ),
            )


    async def list_metadata(
        self, candidate_id: int
    ) -> tuple[MetadataEntry, ...]:
        return await asyncio.to_thread(self._list_metadata_sync, candidate_id)


    def list_metadata_sync(
        self, candidate_id: int
    ) -> tuple[MetadataEntry, ...]:
        return self._list_metadata_sync(candidate_id)


    def _list_metadata_sync(self, candidate_id: int) -> tuple[MetadataEntry, ...]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT field_name, field_value, value_source, confidence, "
                "is_manual, created_at FROM metadata_values "
                "WHERE candidate_id = ? ORDER BY is_manual DESC, field_name",
                (candidate_id,),
            ).fetchall()
        return tuple(
            MetadataEntry(
                field_name=str(row[0]),
                field_value=str(row[1]),
                value_source=str(row[2]),
                confidence=float(row[3]) if row[3] is not None else None,
                is_manual=bool(row[4]),
                created_at=str(row[5]),
            )
            for row in rows
        )


    async def list_review_actions(
        self, candidate_id: int
    ) -> tuple[ReviewActionEntry, ...]:
        return await asyncio.to_thread(
            self._list_review_actions_sync, candidate_id
        )


    def list_review_actions_sync(
        self, candidate_id: int
    ) -> tuple[ReviewActionEntry, ...]:
        return self._list_review_actions_sync(candidate_id)


    def _list_review_actions_sync(
        self, candidate_id: int
    ) -> tuple[ReviewActionEntry, ...]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT action, operator_name, details_json, created_at "
                "FROM review_actions WHERE candidate_id = ? "
                "ORDER BY id DESC",
                (candidate_id,),
            ).fetchall()
        return tuple(
            ReviewActionEntry(
                action=str(row[0]),
                operator_name=str(row[1]),
                details=self._safe_json(row[2]),
                created_at=str(row[3]),
            )
            for row in rows
        )


    @staticmethod
    def _safe_json(value: str | None) -> dict:
        if not value:
            return {}
        try:
            decoded = json.loads(value)
        except (TypeError, ValueError):
            return {}
        return decoded if isinstance(decoded, dict) else {}
