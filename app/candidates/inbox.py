"""Durable MTProto delivery; business identity/claims remain in ingest_entries."""
from dataclasses import asdict, replace
import json

from app.candidates.entries import IngestError
from app.candidates.models import ParsedSourceMessage
from app.connections.runtime import current_account_context


class TelegramUserInbox:
    def __init__(self, database):
        self._database = database

    @staticmethod
    def _account():
        context = current_account_context.get()
        if context is not None:
            if context.kind != "TG_USER":
                raise IngestError("INGEST_ACCOUNT_MISMATCH")
            return context.account_id
        return None

    def save_batch(self, target, highest, messages):
        """Commit received input and its cursor together; no AI or claims here."""
        account = self._account()
        if target.get("account_id") != account:
            raise IngestError("INGEST_ACCOUNT_MISMATCH")
        values = []
        for message in messages:
            if message.account_id not in {None, account} or message.chat_id != int(target["chat_id"]):
                raise IngestError("INGEST_ACCOUNT_MISMATCH")
            message = replace(message, account_id=account)
            values.append((account or "legacy", account, message.chat_id, message.message_id,
                           json.dumps(asdict(message), ensure_ascii=False, separators=(",", ":"))))
        with self._database.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.executemany(
                "INSERT INTO telegram_user_inbox(account_scope, external_account_id, chat_id, message_id, payload_json) "
                "VALUES(?, ?, ?, ?, ?) ON CONFLICT(account_scope, chat_id, message_id) DO NOTHING", values,
            )
            connection.execute(
                "UPDATE telegram_sources SET last_message_id = MAX(COALESCE(last_message_id, 0), ?) "
                "WHERE chat_id = ? AND account_id = ?",
                (highest, target["chat_id"], self._database._ensure_telegram_account(connection, account)),
            )

    def pending(self, *, limit):
        """Interleave chats while preserving each chat's message order."""
        with self._database.connection() as connection:
            rows = connection.execute(
                "SELECT id, payload_json FROM telegram_user_inbox WHERE account_scope = ? "
                "ORDER BY ROW_NUMBER() OVER (PARTITION BY chat_id ORDER BY id), id LIMIT ?",
                (self._account() or "legacy", limit),
            ).fetchall()
        result = []
        for inbox_id, payload in rows:
            values = json.loads(payload)
            values["attachments"] = tuple(values["attachments"])
            values["preview_urls"] = tuple(values["preview_urls"])
            result.append((inbox_id, ParsedSourceMessage(**values)))
        return result

    def acknowledge(self, inbox_id):
        """Publication or a recoverable ingest entry must exist before deletion."""
        with self._database.connection() as connection:
            connection.execute("DELETE FROM telegram_user_inbox WHERE id = ? AND account_scope = ?",
                               (inbox_id, self._account() or "legacy"))
