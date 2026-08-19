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
)


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
                    "filter_result = 'ACCEPT', filter_reason = ?, "
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
            connection.execute(
                "INSERT INTO telegram_accounts "
                "(account_type, display_name, session_path, status) "
                "VALUES ('BOT', 'Telegram Bot API', 'bot-api://configured', "
                "'CONNECTED') ON CONFLICT(session_path) DO NOTHING"
            )
            account_id = int(
                connection.execute(
                    "SELECT id FROM telegram_accounts "
                    "WHERE session_path = 'bot-api://configured'"
                ).fetchone()[0]
            )
            connection.execute(
                "INSERT INTO telegram_sources "
                "(account_id, source_type, chat_id, display_name) "
                "VALUES (?, ?, ?, ?) ON CONFLICT(account_id, chat_id) "
                "DO UPDATE SET display_name = excluded.display_name",
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
                "attachment_json, file_unique_id, message_date) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
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
                ),
            )
            created_message = cursor.rowcount == 1
            if not created_message and message.is_edit:
                connection.execute(
                    "UPDATE source_messages SET sender_id = ?, "
                    "reply_to_message_id = ?, message_text = ?, "
                    "attachment_json = ?, file_unique_id = ?, "
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
                    candidate_cursor = connection.execute(
                        "INSERT INTO candidates "
                        "(status, ex_gid, ex_gallery_token, filter_result, "
                        "filter_reason) VALUES ('PENDING_REVIEW', ?, ?, ?, ?)",
                        (
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
            connection.execute(
                "UPDATE telegram_bot_updates SET processed_at = CURRENT_TIMESTAMP, "
                "processing_result = 'ACCEPT', processing_reason = ? "
                "WHERE update_id = ?",
                (message.filter_reason, update_id),
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
