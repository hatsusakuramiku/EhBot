"""P6 account listeners keep source cursors and attachments independent."""
import asyncio

from app.candidates.entries import register_observation
from app.candidates.models import ParsedSourceMessage
from app.connections.accounts import AccountRegistry
from app.db.database import Database
from app.secrets import SecretStore


def test_same_chat_has_one_cursor_per_external_account(tmp_path):
    async def run():
        db = Database(tmp_path / "accounts.db")
        await db.initialize()
        registry = AccountRegistry(db, SecretStore(tmp_path / "private"))
        first = await registry.create("TG_USER", "first", {"api_id": "1", "api_hash": "hash-1"})
        second = await registry.create("TG_USER", "second", {"api_id": "2", "api_hash": "hash-2"})
        for account in (first, second):
            await db.configure_telegram_source(
                account_id=account["account_id"], source_type="CHANNEL", chat_id=-100123,
                display_name=account["label"], enabled=True, allowed_archive_formats=("zip",),
                max_attachment_size_mb=0,
            )
        assert (await db.telegram_ingest_targets(first["account_id"]))[0]["cursor"] == 0
        assert (await db.telegram_ingest_targets(second["account_id"]))[0]["cursor"] == 0
        await db.set_source_cursor(-100123, 10, first["account_id"])
        await db.set_source_cursor(-100123, 20, second["account_id"])
        assert (await db.telegram_ingest_targets(first["account_id"]))[0]["cursor"] == 10
        assert (await db.telegram_ingest_targets(second["account_id"]))[0]["cursor"] == 20

    asyncio.run(run())


def test_same_message_from_two_accounts_keeps_two_attachment_credentials(tmp_path):
    async def run():
        db = Database(tmp_path / "observations.db")
        await db.initialize()
        registry = AccountRegistry(db, SecretStore(tmp_path / "private"))
        accounts = [await registry.create("TG_BOT", label, {"bot_token": token})
                    for label, token in (("first", "token-1"), ("second", "token-2"))]
        messages = [ParsedSourceMessage(
            is_edit=False, chat_id=-100123, chat_title="same channel", message_id=7,
            sender_id=None, reply_to_message_id=None, media_group_id=None,
            message_text="https://e-hentai.org/g/123/token/", attachments=(
                {"type": "archive", "file_id": file_id, "file_unique_id": file_id, "file_name": "book.zip"},
            ), file_unique_id=file_id, message_date="2026-10-15", filter_result="ACCEPT",
            filter_reason="accepted", ex_gid=123, ex_gallery_token="token", account_id=account["account_id"],
            peer_type="channel") for account, file_id in zip(accounts, ("file-1", "file-2"))]
        with db.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            first = register_observation(connection, messages[0])
            second = register_observation(connection, messages[1])
        assert first["id"] == second["id"]
        with db.connection() as connection:
            assert connection.execute("SELECT count(*) FROM source_messages").fetchone() == (2,)
            files = {row[0] for row in connection.execute("SELECT file_unique_id FROM source_messages")}
            assert files == {"file-1", "file-2"}

    asyncio.run(run())
