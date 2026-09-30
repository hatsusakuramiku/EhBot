import asyncio
from datetime import UTC, datetime
import logging
from pathlib import Path
import sqlite3
from types import SimpleNamespace

import httpx
import pytest

from app.candidates.ingestor import CandidateIngestor
from app.connections.manager import (
    TELEGRAM_USER_API_SECRET,
    TELEGRAM_USER_SESSION_SECRET,
    ConnectionManager,
)
from app.connections.models import ProviderConnectionError, TelegramUserAccount
from app.connections.exhentai import ExHentaiCredentials
from app.db.database import Database
from app.secrets import SecretStore


@pytest.mark.asyncio
async def test_configuring_telegram_starts_durable_update_polling(
    tmp_path: Path,
) -> None:
    first_poll_completed = False

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal first_poll_completed
        if request.url.path.endswith("/getMe"):
            return httpx.Response(
                200,
                json={
                    "ok": True,
                    "result": {
                        "id": 42,
                        "is_bot": True,
                        "first_name": "EhBot Intake",
                        "username": "ehbot_intake_bot",
                    },
                },
            )
        if not first_poll_completed:
            first_poll_completed = True
            return httpx.Response(
                200,
                json={
                    "ok": True,
                    "result": [
                        {
                            "update_id": 100,
                            "channel_post": {
                                "message_id": 80,
                                "date": 1_700_000_300,
                                "chat": {
                                    "id": -100123,
                                    "title": "Polling Channel",
                                },
                                "caption": "Polling Candidate",
                                "photo": [
                                    {
                                        "file_id": "poll-photo",
                                        "file_unique_id": "poll-photo-unique",
                                        "width": 800,
                                        "height": 1200,
                                    }
                                ],
                            },
                        }
                    ],
                },
            )
        await asyncio.Event().wait()
        raise AssertionError("unreachable")

    database = Database(tmp_path / "ehbot.db")
    await database.initialize()
    await database.configure_telegram_source(
        source_type="CHANNEL",
        chat_id=-100123,
        display_name="Polling Channel",
        enabled=True,
        allowed_archive_formats=("zip", "rar", "7z", "cbz"),
        max_attachment_size_mb=0,
    )
    store = SecretStore(tmp_path / "private")
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url="https://api.telegram.org",
        timeout=40,
    ) as client:
        manager = ConnectionManager(
            store,
            database,
            telegram_client=client,
            candidate_ingestor=CandidateIngestor(database),
        )
        await manager.configure_telegram("123:secret")
        for _ in range(100):
            if await database.list_candidates():
                break
            await asyncio.sleep(0.01)

        snapshot = manager.snapshot()
        await manager.stop()

    assert store.is_configured("telegram_bot_token") is True
    assert snapshot.telegram.state == "connected"
    assert snapshot.telegram.identity == "@ehbot_intake_bot"
    assert await database.latest_telegram_update_id() == 100
    assert (await database.list_candidates())[0].title == "Polling Candidate"


@pytest.mark.asyncio
async def test_configuring_exhentai_persists_verified_cookie_session(
    tmp_path: Path,
) -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.url == "https://exhentai.org/"
        return httpx.Response(200, text="<html><title>ExHentai.org</title></html>")

    database = Database(tmp_path / "ehbot.db")
    await database.initialize()
    store = SecretStore(tmp_path / "private")
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        timeout=15,
    ) as client:
        manager = ConnectionManager(
            store,
            database,
            telegram_client=client,
            exhentai_client=client,
        )
        await manager.configure_exhentai(
            ExHentaiCredentials("10001", "pass-secret", "igneous-secret")
        )
        snapshot = manager.snapshot()
        await manager.stop()

    assert store.is_configured("exhentai_cookies") is True
    assert snapshot.exhentai.state == "connected"
    assert snapshot.exhentai.identity == "Member 10001"


@pytest.mark.asyncio
async def test_saved_telegram_token_reconnects_on_startup(tmp_path: Path) -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/getMe"):
            return httpx.Response(
                200,
                json={
                    "ok": True,
                    "result": {
                        "id": 42,
                        "is_bot": True,
                        "first_name": "EhBot Intake",
                        "username": "ehbot_intake_bot",
                    },
                },
            )
        await asyncio.Event().wait()
        raise AssertionError("unreachable")

    database = Database(tmp_path / "ehbot.db")
    await database.initialize()
    store = SecretStore(tmp_path / "private")
    store.write("telegram_bot_token", "123:saved-secret")
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url="https://api.telegram.org",
        timeout=40,
    ) as client:
        manager = ConnectionManager(store, database, telegram_client=client)
        await manager.start()
        snapshot = manager.snapshot()
        await manager.stop()

    assert snapshot.telegram.state == "connected"
    assert snapshot.telegram.identity == "@ehbot_intake_bot"


@pytest.mark.asyncio
async def test_saved_exhentai_session_reconnects_without_telegram(
    tmp_path: Path,
) -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.url == "https://exhentai.org/"
        return httpx.Response(200, text="<title>ExHentai.org</title>")

    database = Database(tmp_path / "ehbot.db")
    await database.initialize()
    store = SecretStore(tmp_path / "private")
    store.write(
        "exhentai_cookies",
        ExHentaiCredentials("10001", "pass-secret", "igneous-secret").to_json(),
    )
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), timeout=15
    ) as client:
        manager = ConnectionManager(
            store,
            database,
            telegram_client=client,
            exhentai_client=client,
        )
        await manager.start()
        snapshot = manager.snapshot()
        await manager.stop()

    assert snapshot.telegram.state == "not_configured"
    assert snapshot.exhentai.state == "connected"
    assert snapshot.exhentai.identity == "Member 10001"


@pytest.mark.asyncio
async def test_disconnect_removes_saved_provider_credentials(tmp_path: Path) -> None:
    poll_started = asyncio.Event()

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/getMe"):
            return httpx.Response(
                200,
                json={
                    "ok": True,
                    "result": {
                        "id": 42,
                        "is_bot": True,
                        "first_name": "EhBot Intake",
                        "username": "ehbot_intake_bot",
                    },
                },
            )
        if request.url.path.endswith("/getUpdates"):
            poll_started.set()
            await asyncio.Event().wait()
        return httpx.Response(200, text="<title>ExHentai.org</title>")

    database = Database(tmp_path / "ehbot.db")
    await database.initialize()
    store = SecretStore(tmp_path / "private")
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url="https://api.telegram.org",
        timeout=40,
    ) as client:
        manager = ConnectionManager(
            store,
            database,
            telegram_client=client,
            exhentai_client=client,
        )
        await manager.configure_telegram("123:secret")
        await manager.configure_exhentai(
            ExHentaiCredentials("10001", "pass-secret", "igneous-secret")
        )
        await asyncio.wait_for(poll_started.wait(), timeout=1)

        await manager.disconnect_telegram()
        await manager.disconnect_exhentai()
        snapshot = manager.snapshot()

    assert store.is_configured("telegram_bot_token") is False
    assert store.is_configured("exhentai_cookies") is False
    assert snapshot.telegram.state == "not_configured"
    assert snapshot.exhentai.state == "not_configured"


@pytest.mark.asyncio
async def test_startup_processes_saved_updates_without_telegram_connection(
    tmp_path: Path,
) -> None:
    database = Database(tmp_path / "ehbot.db")
    await database.initialize()
    await database.configure_telegram_source(
        source_type="CHANNEL",
        chat_id=-100123,
        display_name="Offline Channel",
        enabled=True,
        allowed_archive_formats=("zip", "rar", "7z", "cbz"),
        max_attachment_size_mb=0,
    )
    await database.save_telegram_updates(
        [
            {
                "update_id": 400,
                "channel_post": {
                    "message_id": 70,
                    "date": 1_700_000_200,
                    "chat": {"id": -100123, "title": "Offline Channel"},
                    "caption": "Offline Candidate",
                    "photo": [
                        {
                            "file_id": "offline-photo",
                            "file_unique_id": "offline-photo-unique",
                            "width": 800,
                            "height": 1200,
                        }
                    ],
                },
            }
        ]
    )
    store = SecretStore(tmp_path / "private")
    async with httpx.AsyncClient() as client:
        manager = ConnectionManager(
            store,
            database,
            telegram_client=client,
            candidate_ingestor=CandidateIngestor(database),
        )
        await manager.start()
        candidates = await database.list_candidates()
        await manager.stop()

    assert len(candidates) == 1
    assert candidates[0].title == "Offline Candidate"


@pytest.mark.asyncio
async def test_candidate_storage_failure_sets_visible_connection_error(
    tmp_path: Path,
) -> None:
    update_sent = False

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal update_sent
        if request.url.path.endswith("/getMe"):
            return httpx.Response(
                200,
                json={
                    "ok": True,
                    "result": {
                        "id": 42,
                        "is_bot": True,
                        "first_name": "EhBot Intake",
                        "username": "ehbot_intake_bot",
                    },
                },
            )
        if not update_sent:
            update_sent = True
            return httpx.Response(
                200,
                json={
                    "ok": True,
                    "result": [
                        {
                            "update_id": 401,
                            "message": {
                                "message_id": 71,
                                "date": 1_700_000_201,
                                "chat": {"id": 900, "username": "fixture"},
                                "from": {"id": 900},
                                "text": "https://exhentai.org/g/99887/errorToken/",
                            },
                        }
                    ],
                },
            )
        await asyncio.Event().wait()
        raise AssertionError("unreachable")

    database = Database(tmp_path / "ehbot.db")
    await database.initialize()
    await database.configure_telegram_source(
        source_type="PRIVATE_CHAT",
        chat_id=900,
        display_name="Fixture Sender",
        enabled=True,
        allowed_archive_formats=("zip", "rar", "7z", "cbz"),
        max_attachment_size_mb=0,
    )
    with sqlite3.connect(database.path) as connection:
        connection.execute("DROP TABLE candidates")
    store = SecretStore(tmp_path / "private")
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url="https://api.telegram.org",
    ) as client:
        manager = ConnectionManager(
            store,
            database,
            telegram_client=client,
            candidate_ingestor=CandidateIngestor(database),
        )
        await manager.configure_telegram("123:secret")
        for _ in range(100):
            if manager.snapshot().telegram.state == "error":
                break
            await asyncio.sleep(0.01)
        snapshot = manager.snapshot()
        await manager.stop()

    assert snapshot.telegram.state == "error"
    assert snapshot.telegram.error == "消息处理失败，将自动重试"


class FakeUserDocument:
    def __init__(self, *, id: int = 7, size: int = 4096) -> None:
        self.id = id
        self.size = size
        self.mime_type = "application/zip"


class FakeUserMessageFile:
    def __init__(self, name: str) -> None:
        self.name = name
        self.size = 4096


class FakeUserMessage:
    """A Telethon message, reduced to the fields the translator reads."""

    def __init__(self, *, id: int, chat_id: int = -100123, caption: str = "A Book") -> None:
        self.id = id
        self.chat_id = chat_id
        self.message = caption
        self.entities: list = []
        self.document = FakeUserDocument()
        self.photo = None
        self.file = FakeUserMessageFile("book.zip")
        self.grouped_id = None
        self.reply_to_msg_id = None
        self.sender_id = 55
        self.date = datetime(2026, 9, 29, 12, 0, tzinfo=UTC)
        self.chat = None


class FakeUserDialog:
    """A dialog, reduced to the fields `load_dialogs` reads."""

    def __init__(self, chat_id: int, entity: object | None = None) -> None:
        self.id = chat_id
        # Telethon hands over an entity object; a bare chat id is the stub that
        # stands in for one when a test does not care what kind of chat it is.
        self.entity = chat_id if entity is None else entity


class FakeUserClient:
    """A Telethon client, reduced to the calls the ingester makes.

    Faked at the Telethon seam rather than at `TelegramUserClient`: the client
    methods under test (`fetch_channel_messages`, `latest_message_id`) are the
    translation layer, and a fake above them would skip the code being tested.
    """

    def __init__(
        self,
        messages: list[FakeUserMessage],
        *,
        unseen_chats: set[int] | None = None,
        dialogs: set[int] | None = None,
        entities: dict[int, object] | None = None,
    ) -> None:
        self.messages = sorted(messages, key=lambda item: item.id)
        self.latest_calls: list[int] = []
        #: Chat ids only resolvable after the dialog list has been read, like a
        #: session rebuilt from the stored string (it carries no access hashes).
        self.unseen_chats = set(unseen_chats or ())
        #: Chat ids the account's dialog list contains -- the membership test
        #: the capability check runs on. Defaults to the chats this fake holds
        #: messages for, which is the ordinary case of a readable source.
        self.dialogs = (
            {item.chat_id for item in self.messages}
            if dialogs is None
            else set(dialogs)
        )
        #: The entity behind each dialog id, for the source picker's
        #: classification. A chat without one answers with its own id.
        self.entities = dict(entities or {})
        #: Raised by `iter_dialogs`, standing in for an outage during a check.
        self.dialogs_error: Exception | None = None
        self.dialogs_loaded = False
        self.dialog_calls = 0

    async def connect(self) -> None:
        return None

    def disconnect(self) -> None:
        return None

    async def get_entity(self, chat_id: int) -> int:
        # Telethon refuses a peer the session has not seen until the dialog
        # walk fills the cache; after it, only the chats actually in the dialog
        # list resolve, so absence from it reads as「this account is not in the
        # channel」.
        if int(chat_id) in self.unseen_chats or (
            self.dialogs_loaded and int(chat_id) not in self.dialogs
        ):
            raise ValueError(
                "Could not find the input entity for PeerChannel("
                f"channel_id={abs(int(chat_id))}) (PeerChannel)."
            )
        return chat_id

    async def iter_dialogs(self, **kwargs):
        self.dialog_calls += 1
        if self.dialogs_error is not None:
            raise self.dialogs_error
        self.unseen_chats.clear()
        self.dialogs_loaded = True
        for chat_id in sorted(self.dialogs):
            yield FakeUserDialog(chat_id, self.entities.get(chat_id))

    async def iter_messages(
        self, entity: int, *, min_id=None, limit=None, reverse=False
    ):
        # Telethon's own contract: `min_id` is exclusive, `reverse` yields
        # oldest-first, and the default order is newest-first.
        self.latest_calls.append(entity)
        found = [
            item
            for item in self.messages
            if item.chat_id == entity and (min_id is None or item.id > min_id)
        ]
        found.sort(key=lambda item: item.id, reverse=not reverse)
        if limit is not None:
            found = found[:limit]
        for item in found:
            yield item


async def user_ingest_manager(
    tmp_path: Path,
    messages: list[FakeUserMessage],
    *,
    enable_source: bool = True,
    unseen_chats: set[int] | None = None,
    dialogs: set[int] | None = None,
    entities: dict[int, object] | None = None,
) -> tuple[ConnectionManager, Database, FakeUserClient]:
    """A manager whose user account is logged in and can read one channel."""
    database = Database(tmp_path / "ehbot.db")
    await database.initialize()
    if enable_source:
        await database.configure_telegram_source(
            source_type="CHANNEL",
            chat_id=-100123,
            display_name="Fixture Channel",
            enabled=True,
            allowed_archive_formats=("zip", "rar", "7z", "cbz"),
            max_attachment_size_mb=0,
        )
    store = SecretStore(tmp_path / "private")
    store.write(TELEGRAM_USER_API_SECRET, f"1234567:{'a' * 32}")
    store.write(TELEGRAM_USER_SESSION_SECRET, "stored-session-string")
    client = FakeUserClient(
        messages, unseen_chats=unseen_chats, dialogs=dialogs, entities=entities
    )
    manager = ConnectionManager(
        store,
        database,
        telegram_client=httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda request: httpx.Response(200, json={"ok": True, "result": []})
            )
        ),
        candidate_ingestor=CandidateIngestor(database),
        user_client_factory=lambda api_id, api_hash, session: client,
    )
    manager._telegram_user = TelegramUserAccount(
        state="connected", configured=True, identity="@operator"
    )
    return manager, database, client


@pytest.mark.asyncio
async def test_the_user_account_ingests_new_channel_messages(
    tmp_path: Path,
) -> None:
    """No bot, and works still arrive: the account reads the channel itself."""
    manager, database, client = await user_ingest_manager(
        tmp_path,
        [FakeUserMessage(id=11), FakeUserMessage(id=12, caption="Second Book")],
    )
    # A cursor from an earlier pass over this chat: message 11 is done.
    await database.set_source_cursor(-100123, 11)

    created = await manager._ingest_with_user_account()

    assert created == 1
    candidates = await database.list_candidates()
    assert [candidate.candidate_id for candidate in candidates] != []
    detail = await database.get_candidate(candidates[0].candidate_id)
    attachment = detail.messages[0].attachments[0]
    assert attachment["type"] == "archive"
    assert attachment["chat_id"] == -100123
    assert attachment["message_id"] == 12
    # No Bot API id: the attachment came in over MTProto, and the bot route
    # reads that as 「not fetchable through me」 rather than trying anyway.
    assert attachment["file_id"] == ""
    # The cursor moved past the batch, so the next pass is a no-op -- the
    # database's unique key is the real guard, and this keeps the poll cheap.
    assert (await database.telegram_ingest_targets())[0]["cursor"] == 12
    assert await manager._ingest_with_user_account() == 0
    await manager.stop()


@pytest.mark.asyncio
async def test_a_first_poll_seeds_the_cursor_instead_of_walking_the_archive(
    tmp_path: Path,
) -> None:
    """Enabling a source must not turn years of history into candidates."""
    manager, database, client = await user_ingest_manager(
        tmp_path,
        [FakeUserMessage(id=1), FakeUserMessage(id=2), FakeUserMessage(id=3)],
    )

    assert await manager._ingest_with_user_account() == 0
    # The newest message was listed once, to seed the cursor at it.
    assert client.latest_calls == [-100123]
    assert (await database.telegram_ingest_targets())[0]["cursor"] == 3
    assert await database.list_candidates() == []

    # From the seeded cursor on, new messages are ingested normally.
    client.messages.append(FakeUserMessage(id=4, caption="New Book"))
    assert await manager._ingest_with_user_account() == 1
    await manager.stop()


@pytest.mark.asyncio
async def test_a_channel_the_session_has_not_seen_is_read_after_dialogs(
    tmp_path: Path,
) -> None:
    """The operator's own case, end to end.

    The account was logged in fresh, so its session carried no access hash for
    the channel and every poll of it failed; reading the account's dialog list
    once resolves the id, and the resolved chat is reused on later passes.
    """
    manager, database, client = await user_ingest_manager(
        tmp_path,
        [FakeUserMessage(id=5, caption="Book")],
        unseen_chats={-100123},
    )
    await database.set_source_cursor(-100123, 4)

    assert await manager._ingest_with_user_account() == 1
    assert client.dialog_calls == 1
    assert len(await database.list_candidates()) == 1

    # Read once per process, not once per poll.
    assert await manager._ingest_with_user_account() == 0
    assert client.dialog_calls == 1
    await manager.stop()


@pytest.mark.asyncio
async def test_an_unreadable_source_is_reported_once_and_then_skipped(
    tmp_path: Path, caplog
) -> None:
    """The old loop asked the account to resolve a channel it is not in.

    Every ten-second pass retried the chat, failed, and re-logged the same
    line, so one unreadable source kept the log busy all day. Reading the
    account's own dialogs answers membership for every source in one walk;
    the verdict is remembered and later polls skip the chat in silence.
    """
    manager, database, client = await user_ingest_manager(
        tmp_path, [FakeUserMessage(id=5)], dialogs=set()
    )

    with caplog.at_level(logging.WARNING, logger="app.connections.manager"):
        assert await manager._ingest_with_user_account() == 0
        assert await manager._ingest_with_user_account() == 0

    warned = [
        item.getMessage()
        for item in caplog.records
        if item.name == "app.connections.manager"
    ]
    assert warned.count("telegram_user_source_unreadable") == 1
    # The per-source failure is no longer the story: the membership problem is
    # named once and nothing retries the chat behind it.
    assert "telegram_user_ingest_source_failed" not in warned
    assert client.dialog_calls == 1
    record = next(
        item
        for item in caplog.records
        if item.getMessage() == "telegram_user_source_unreadable"
    )
    assert record.error_code == "TELEGRAM_USER_ENTITY_UNRESOLVED"
    assert record.chat_id == -100123
    await manager.stop()


@pytest.mark.asyncio
async def test_saving_a_source_again_re_runs_the_check(
    tmp_path: Path, caplog
) -> None:
    """The operator's way back is to save the source, not to restart.

    The account was added to the channel between two polls, and the saved row
    is byte-identical, so nothing in the enabled set moved. The save is still
    the operator saying 「look again」, and the next poll has to obey.
    """
    manager, database, client = await user_ingest_manager(
        tmp_path, [FakeUserMessage(id=5)], dialogs=set()
    )
    await database.set_source_cursor(-100123, 4)
    assert await manager._ingest_with_user_account() == 0
    assert client.dialog_calls == 1

    # The account is in the channel now, and the same source is saved again:
    # what the dialogs contain changed, the row did not.
    client.dialogs.add(-100123)
    manager.note_sources_changed()
    caplog.clear()

    with caplog.at_level(logging.WARNING, logger="app.connections.manager"):
        assert await manager._ingest_with_user_account() == 1

    assert client.dialog_calls == 2
    assert "telegram_user_source_unreadable" not in [
        item.getMessage() for item in caplog.records
    ]
    assert len(await database.list_candidates()) == 1
    await manager.stop()


@pytest.mark.asyncio
async def test_a_check_that_cannot_run_abandons_the_pass(
    tmp_path: Path, caplog
) -> None:
    """A check that could not run must not read as「every source is bad」.

    An outage during the dialog walk would otherwise turn one connection
    problem into a per-source failure for every channel; the pass is dropped
    and the reason named, so the next poll can try the check again.
    """
    manager, database, client = await user_ingest_manager(
        tmp_path, [FakeUserMessage(id=5)]
    )
    client.dialogs_error = ConnectionError("network down")

    with caplog.at_level(logging.WARNING, logger="app.connections.manager"):
        assert await manager._ingest_with_user_account() == 0

    record = next(
        item
        for item in caplog.records
        if item.getMessage() == "telegram_user_sources_check_failed"
    )
    assert record.error_code == "TELEGRAM_USER_UNREACHABLE"
    assert record.error_detail.startswith("ConnectionError")
    warned = [item.getMessage() for item in caplog.records]
    assert "telegram_user_source_unreadable" not in warned
    assert "telegram_user_ingest_source_failed" not in warned
    # Nothing was learned, so the next poll re-checks instead of trusting a
    # verdict it never reached.
    assert manager._user_checked_chats is None
    await manager.stop()


@pytest.mark.asyncio
async def test_a_message_both_paths_saw_produces_one_candidate(
    tmp_path: Path,
) -> None:
    """Bot and account may both read the same post; it is still one work."""
    manager, database, client = await user_ingest_manager(
        tmp_path, [FakeUserMessage(id=21)]
    )
    await database.set_source_cursor(-100123, 20)

    assert await manager._ingest_with_user_account() == 1
    # The bot reading the identical message afterwards: same account, chat and
    # message id, so the shared unique key answers "already seen".
    from app.candidates.mtproto import parse_user_message

    again = parse_user_message(FakeUserMessage(id=21))
    assert again is not None
    assert await database.save_candidate_message(None, again) is False
    assert len(await database.list_candidates()) == 1
    await manager.stop()


@pytest.mark.asyncio
async def test_the_source_picker_lists_the_accounts_own_chats(
    tmp_path: Path,
) -> None:
    """「怎么知道 Chat ID」 answered without waiting for a message.

    The dialog list the ingest capability check already walks is the account's
    own view of what it is in, so the picker asks for the same thing: channel,
    group and person, each with the id the whitelist stores and a name to
    recognise. The order is the one the page renders, so two reads of the same
    account produce the same list.
    """
    manager, _, client = await user_ingest_manager(
        tmp_path,
        [],
        enable_source=False,
        dialogs={-100501, -100500, 500},
        entities={
            -100501: SimpleNamespace(title="Zed Group", megagroup=True),
            -100500: SimpleNamespace(
                title="Alpha Channel", username="alpha", broadcast=True
            ),
            500: SimpleNamespace(first_name="Ada", last_name="Lovelace"),
        },
    )

    dialogs = await manager.list_telegram_user_dialogs()

    assert [row["chat_id"] for row in dialogs] == [-100500, -100501, 500]
    assert [row["kind"] for row in dialogs] == [
        "CHANNEL",
        "GROUP",
        "PRIVATE_CHAT",
    ]
    assert [row["title"] for row in dialogs] == [
        "Alpha Channel",
        "Zed Group",
        "Ada Lovelace",
    ]
    # The walk is the same one the capability check makes, so it also teaches
    # the session the access hashes: a source saved from this list is readable
    # on the next poll rather than after one more retry.
    assert client.dialog_calls == 1
    assert manager._user_entities.dialogs_loaded is True
    await manager.stop()


@pytest.mark.asyncio
async def test_the_source_picker_refuses_without_a_logged_in_account(
    tmp_path: Path,
) -> None:
    """Half a credential is not an account, and an empty list would be a lie."""
    database = Database(tmp_path / "ehbot.db")
    await database.initialize()
    store = SecretStore(tmp_path / "private")
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, json={"ok": True, "result": []})
        )
    ) as client:
        manager = ConnectionManager(store, database, telegram_client=client)

        with pytest.raises(ProviderConnectionError) as refused:
            await manager.list_telegram_user_dialogs()
        assert refused.value.code == "TELEGRAM_USER_NOT_CONFIGURED"

        # The api pair without a session is still no account: the login was
        # started and never finished, and the picker must say so rather than
        # present「no chats」.
        store.write(TELEGRAM_USER_API_SECRET, f"1234567:{'a' * 32}")
        with pytest.raises(ProviderConnectionError) as half:
            await manager.list_telegram_user_dialogs()
        assert half.value.code == "TELEGRAM_USER_NOT_CONFIGURED"
