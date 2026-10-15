"""Account listeners receive durably while business processing is slow."""
import asyncio
from dataclasses import replace
from datetime import UTC, datetime
import sqlite3
from types import SimpleNamespace
from threading import Event as ThreadEvent

import httpx
import pytest
import pytest_asyncio

from app.candidates.ingestor import CandidateIngestor
from app.candidates.entries import IngestError
from app.candidates.inbox import TelegramUserInbox
from app.candidates.models import ParsedSourceMessage
from app.connections.lifecycle import AccountLifecycle
from app.connections.listeners import AccountListener
from app.connections.models import ProviderConnectionError, TelegramUserAccount
from app.connections.runtime import AccountRuntime
from app.connections.service import AccountService
from tests.integration.test_account_registry import registry


CHATS = (-1000000000011, -1000000000022)


@pytest_asyncio.fixture
async def delivery(registry, monkeypatch):
    account = await registry.create("TG_USER", "Listener", {"api_id": "1234", "api_hash": "a" * 32})
    account = await registry.save_session(account["account_id"], expected_revision=account["revision"],
                                          session="fixture-session", remote_identity="42", platform_name="User")
    await registry.enable(account["account_id"], expected_revision=account["revision"])
    database = registry._database
    for chat in CHATS:
        await database.configure_telegram_source(
            account_id=account["account_id"], source_type="CHANNEL", chat_id=chat,
            display_name=str(chat), enabled=True, allowed_archive_formats=("zip",), max_attachment_size_mb=0,
        )
        await database.set_source_cursor(chat, 1, account["account_id"])
    async with httpx.AsyncClient() as http:
        ingestor = CandidateIngestor(database)
        listener = AccountListener(account["account_id"], AccountRuntime(registry, AccountLifecycle(registry)),
                                   database, telegram_client=http, candidate_ingestor=ingestor)
        listener._telegram_user = TelegramUserAccount("connected", True)
        calls = []

        class Client:
            async def load_dialogs(self):
                return dict.fromkeys(CHATS)

            async def fetch_channel_messages(self, chat, *, after_id, limit):
                calls.append(chat)
                if after_id >= 2:
                    return []
                return [SimpleNamespace(id=2, chat_id=chat, message=f"https://e-hentai.org/g/{abs(chat)}/token/",
                                        date=datetime.now(UTC))]

        client = Client()
        monkeypatch.setattr(listener, "_user_client", lambda *args: client)
        try:
            yield listener, ingestor, client, calls
        finally:
            await listener.stop()


@pytest.mark.asyncio
async def test_slow_admission_does_not_block_receiving_other_sources(delivery, monkeypatch):
    listener, ingestor, client, calls = delivery
    entered = asyncio.Event()

    class Admission:
        async def decide(self, message):
            entered.set()
            await asyncio.Event().wait()

    ingestor.set_admission(Admission())
    monkeypatch.setattr("app.connections.manager._USER_INGEST_INTERVAL_SECONDS", 0.01)
    listener._user_ingest_task = asyncio.create_task(listener._run_user_ingest())
    await asyncio.wait_for(entered.wait(), 2)

    async def received_both():
        while True:
            targets = await listener._database.telegram_ingest_targets(listener.account_id)
            if set(CHATS).issubset(calls) and {target["cursor"] for target in targets} == {2}:
                return
            await asyncio.sleep(0.01)

    await asyncio.wait_for(received_both(), 0.5)
    targets = await listener._database.telegram_ingest_targets(listener.account_id)
    assert {target["cursor"] for target in targets} == {2}


@pytest.mark.asyncio
async def test_floodwait_stops_all_sources_for_only_the_limited_account(delivery, monkeypatch):
    listener, _, client, calls = delivery

    async def limited(chat, **kwargs):
        calls.append(chat)
        raise ProviderConnectionError("TELEGRAM_USER_RATE_LIMITED", "fixture rate limit", retry_after=60)

    monkeypatch.setattr(client, "fetch_channel_messages", limited)
    await listener._ingest_with_user_account()
    assert calls == [CHATS[0]]
    await listener._ingest_with_user_account()
    assert calls == [CHATS[0]]


@pytest.mark.asyncio
async def test_floodwait_expires_without_resetting_on_source_save(delivery, monkeypatch):
    listener, _, client, calls = delivery
    clock = [100.0]
    monkeypatch.setattr("app.connections.manager.monotonic", lambda: clock[0])

    async def limited(chat, **kwargs):
        calls.append(chat)
        raise ProviderConnectionError("TELEGRAM_USER_RATE_LIMITED", "fixture rate limit", retry_after=60)

    monkeypatch.setattr(client, "fetch_channel_messages", limited)
    await listener._ingest_with_user_account()
    listener.note_sources_changed()
    clock[0] = 159.0
    await listener._ingest_with_user_account()
    assert calls == [CHATS[0]]
    clock[0] = 160.0
    await listener._ingest_with_user_account()
    assert len(calls) == 2


@pytest.mark.asyncio
async def test_dialog_floodwait_also_prevents_rechecking(delivery, monkeypatch):
    listener, _, client, calls = delivery
    checked = []

    async def limited():
        checked.append(True)
        raise ProviderConnectionError("TELEGRAM_USER_RATE_LIMITED", "fixture rate limit", retry_after=60)

    monkeypatch.setattr(client, "load_dialogs", limited)
    await listener._ingest_with_user_account()
    await listener._ingest_with_user_account()
    assert checked == [True]
    assert calls == []


@pytest.mark.asyncio
async def test_invalid_session_stops_source_requests_and_keeps_saved_configuration(delivery, monkeypatch):
    listener, _, client, calls = delivery

    async def invalid(chat, **kwargs):
        calls.append(chat)
        raise ProviderConnectionError("TELEGRAM_USER_UNAUTHORIZED", "fixture session revoked")

    monkeypatch.setattr(client, "fetch_channel_messages", invalid)
    await listener._ingest_with_user_account()
    await listener._ingest_with_user_account()
    assert calls == [CHATS[0]]
    assert listener.snapshot().telegram_user.state == "error"
    assert listener.snapshot().telegram_user.error == "fixture session revoked"
    assert (await listener._runtime._registry.context(listener.account_id)).values["session"] == "fixture-session"


@pytest.mark.asyncio
async def test_normalized_delivery_survives_new_consumer_and_replay(delivery):
    listener, _, _, _ = delivery
    await listener._ingest_with_user_account()
    db = listener._database
    assert await db.list_candidates() == []
    with db.connection() as connection:
        assert connection.execute("SELECT count(*) FROM telegram_user_inbox").fetchone() == (2,)
    listener._candidate_ingestor = CandidateIngestor(db)
    await listener._consume_pending()
    await listener._consume_pending()
    with db.connection() as connection:
        assert connection.execute("SELECT count(*) FROM candidates").fetchone() == (2,)
        assert connection.execute("SELECT count(*) FROM telegram_user_inbox").fetchone() == (0,)
        assert {row[0] for row in connection.execute(
            "SELECT ta.external_account_id FROM source_messages sm JOIN telegram_accounts ta ON ta.id = sm.account_id"
        )} == {listener.account_id}


@pytest.mark.asyncio
async def test_cursor_and_received_input_rollback_together(delivery):
    listener, _, _, _ = delivery
    db = listener._database
    with db.connection() as connection:
        connection.execute("CREATE TRIGGER fail_cursor BEFORE UPDATE OF last_message_id ON telegram_sources "
                           "BEGIN SELECT RAISE(ABORT, 'fixture cursor failure'); END")
    with pytest.raises(sqlite3.IntegrityError, match="fixture cursor failure"):
        await listener._ingest_with_user_account()
    with db.connection() as connection:
        assert connection.execute("SELECT count(*) FROM telegram_user_inbox").fetchone() == (0,)
    assert {row["cursor"] for row in await db.telegram_ingest_targets(listener.account_id)} == {1}
    with db.connection() as connection:
        connection.execute("DROP TRIGGER fail_cursor")
    await listener._ingest_with_user_account()
    await listener._consume_pending()
    with db.connection() as connection:
        assert connection.execute("SELECT count(*) FROM candidates").fetchone() == (2,)


@pytest.mark.asyncio
async def test_receiver_rotates_sources_under_a_per_pass_budget(delivery, monkeypatch):
    listener, _, _, calls = delivery
    monkeypatch.setattr("app.connections.manager._USER_SOURCES_PER_PASS", 1)
    await listener._ingest_with_user_account()
    assert calls == [CHATS[0]]
    await listener._ingest_with_user_account()
    assert calls == list(CHATS)
    await listener._ingest_with_user_account()
    assert calls == [*CHATS, CHATS[0]]


@pytest.mark.asyncio
@pytest.mark.parametrize("delay_storage", [False, True])
async def test_bot_receives_during_slow_ai_and_waits_for_storage_on_stop(registry, monkeypatch, delay_storage):
    db = registry._database
    account = await registry.create("TG_BOT", "Bot", {"bot_token": "fixture-token"})
    account = await registry.record_identity(account["account_id"], account["credential_revision"], "43", "Bot",
                                             expected_revision=account["revision"])
    await registry.enable(account["account_id"], expected_revision=account["revision"])
    await db.configure_telegram_source(
        account_id=account["account_id"], source_type="CHANNEL", chat_id=CHATS[0], display_name="Feed",
        enabled=True, allowed_archive_formats=("zip",), max_attachment_size_mb=0,
    )
    entered, next_poll = asyncio.Event(), asyncio.Event()
    ingestor = CandidateIngestor(db)

    class Admission:
        async def decide(self, message):
            entered.set()
            await asyncio.Event().wait()

    ingestor.set_admission(Admission())
    storage_entered, storage_release = ThreadEvent(), ThreadEvent()
    if delay_storage:
        save = db._save_telegram_updates_sync

        def blocked_save(updates):
            storage_entered.set()
            assert storage_release.wait(5)
            return save(updates)

        monkeypatch.setattr(db, "_save_telegram_updates_sync", blocked_save)

    async def handler(request):
        offset = request.url.params.get("offset")
        if offset is None:
            return httpx.Response(200, json={"ok": True, "result": [
                {"update_id": i, "channel_post": {"message_id": i, "chat": {"id": CHATS[0], "type": "channel"},
                 "date": 1792022400, "text": f"https://e-hentai.org/g/{i}/token/"}} for i in (1, 2)
            ]})
        assert offset == "3"
        next_poll.set()
        await asyncio.Event().wait()

    async with httpx.AsyncClient(base_url="https://api.telegram.org", transport=httpx.MockTransport(handler)) as http:
        listener = AccountListener(account["account_id"], AccountRuntime(registry, AccountLifecycle(registry)), db,
                                   telegram_client=http, candidate_ingestor=ingestor)
        listener._telegram_task = asyncio.create_task(listener._poll_telegram())
        try:
            if delay_storage:
                assert await asyncio.to_thread(storage_entered.wait, 2)
                stopping = asyncio.create_task(listener.stop())
                while not listener._telegram_task.cancelling():
                    await asyncio.sleep(0)
                assert not stopping.done()
                assert listener._runtime._leases
                assert not next_poll.is_set()
                storage_release.set()
                await asyncio.wait_for(stopping, 2)
            else:
                await asyncio.wait_for(entered.wait(), 2)
                await asyncio.wait_for(next_poll.wait(), 0.5)
            async with listener._runtime.use(listener.account_id):
                assert await db.latest_telegram_update_id() == 2
                assert len(await db.pending_telegram_updates(limit=100)) == 2
        finally:
            storage_release.set()
            await listener.stop()
        assert not listener._runtime._leases


@pytest.mark.asyncio
async def test_limited_account_does_not_cool_down_another_account(delivery, monkeypatch):
    listener, ingestor, client, calls = delivery
    registry = listener._runtime._registry
    db = listener._database
    second = await registry.create("TG_USER", "Second", {"api_id": "1234", "api_hash": "b" * 32})
    second = await registry.save_session(second["account_id"], expected_revision=second["revision"],
                                         session="second-fixture-session", remote_identity="43", platform_name="Second")
    await registry.enable(second["account_id"], expected_revision=second["revision"])
    await db.configure_telegram_source(
        account_id=second["account_id"], source_type="CHANNEL", chat_id=CHATS[0], display_name="Second feed",
        enabled=True, allowed_archive_formats=("zip",), max_attachment_size_mb=0,
    )
    await db.set_source_cursor(CHATS[0], 1, second["account_id"])
    other = AccountListener(second["account_id"], listener._runtime, db, telegram_client=listener._telegram_client,
                            candidate_ingestor=ingestor)
    other._telegram_user = TelegramUserAccount("connected", True)
    monkeypatch.setattr(other, "_user_client", lambda *args: client)
    listener._user_retry_at = float("inf")
    try:
        assert await other._ingest_with_user_account() == 1
        await other._consume_pending()
        assert calls == [CHATS[0]]
        assert {row["cursor"] for row in await db.telegram_ingest_targets(listener.account_id)} == {1}
        with db.connection() as connection:
            assert {row[0] for row in connection.execute("SELECT external_account_id FROM telegram_user_inbox")} == set()
            assert connection.execute("SELECT count(*) FROM candidates").fetchone() == (1,)
    finally:
        await other.stop()


@pytest.mark.asyncio
async def test_bounded_raw_backlog_is_fair_and_continues_after_restart(registry):
    db = registry._database
    await db.save_telegram_updates([
        {"update_id": i, "message": {"chat": {"id": CHATS[0] if i <= 150 else CHATS[1]}}}
        for i in range(1, 206)
    ])
    first = await db.pending_telegram_updates(limit=4)
    assert [item[0] for item in first] == [1, 151, 2, 152]
    ingestor = CandidateIngestor(db)
    assert (await ingestor.process_pending_updates()).processed_updates == 100
    with db.connection() as connection:
        assert connection.execute("SELECT count(*) FROM telegram_bot_updates WHERE processed_at IS NULL").fetchone() == (105,)
    async with httpx.AsyncClient() as http:
        service = AccountService(registry, telegram_client=http, exhentai_client=http,
                                 candidate_ingestor=CandidateIngestor(db))
        try:
            await asyncio.wait_for(service.start(), 1)

            async def drained():
                while await db.pending_telegram_updates(limit=1):
                    await asyncio.sleep(0.01)

            await asyncio.wait_for(drained(), 3)
        finally:
            await service.stop()


@pytest.mark.asyncio
async def test_inbox_refuses_cross_account_or_chat_payload(delivery):
    listener, _, _, _ = delivery
    await listener._ingest_with_user_account()
    inbox = TelegramUserInbox(listener._database)
    async with listener._runtime.use(listener.account_id):
        target = (await listener._database.telegram_ingest_targets(listener.account_id))[0]
        message = inbox.pending(limit=1)[0][1]
        for wrong in (replace(message, account_id="wrong-account"), replace(message, chat_id=123)):
            with pytest.raises(IngestError, match="INGEST_ACCOUNT_MISMATCH"):
                inbox.save_batch(target, 99, [wrong])
        assert (await listener._database.telegram_ingest_targets(listener.account_id))[0]["cursor"] == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("stop_account", [False, True])
async def test_listener_stop_waits_for_admission_cleanup_and_retains_input(delivery, stop_account):
    listener, ingestor, _, _ = delivery
    entered, cancelled, release = asyncio.Event(), asyncio.Event(), asyncio.Event()

    class Admission:
        async def decide(self, message):
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()
                await release.wait()

    ingestor.set_admission(Admission())
    listener._user_ingest_task = asyncio.create_task(listener._run_user_ingest())
    await asyncio.wait_for(entered.wait(), 2)
    async def received_both_sources():
        while True:
            with listener._database.connection() as connection:
                if connection.execute("SELECT count(*) FROM telegram_user_inbox").fetchone() == (2,):
                    return
            await asyncio.sleep(0.01)
    await asyncio.wait_for(received_both_sources(), 2)
    async def stop():
        if stop_account:
            account = await listener._runtime._registry.get(listener.account_id)
            await listener._runtime._lifecycle.request_stop(
                listener.account_id, "disable", expected_revision=account["revision"], idempotency_key="stop-listener",
            )
            await listener._runtime.stop(listener.account_id, account["credential_revision"])
        await listener.stop()

    stopping = asyncio.create_task(stop())
    try:
        await asyncio.wait_for(cancelled.wait(), 2)
        assert not stopping.done()
        assert listener._runtime._leases
    finally:
        release.set()
        await asyncio.wait_for(stopping, 2)
    assert not listener._runtime._leases
    with listener._database.connection() as connection:
        assert connection.execute("SELECT count(*) FROM telegram_user_inbox").fetchone() == (2,)
        assert connection.execute("SELECT count(*) FROM candidates").fetchone() == (0,)
        connection.execute("UPDATE ingest_entries SET retry_at = NULL")
    if stop_account:
        # A durable stop correctly prevents any new borrower/publication.
        assert (await listener._runtime._registry.get(listener.account_id))["stop_operation_id"] is not None
        return
    listener._candidate_ingestor = CandidateIngestor(listener._database)
    await listener._consume_pending()
    with listener._database.connection() as connection:
        assert connection.execute("SELECT count(*) FROM candidates").fetchone() == (2,)


@pytest.mark.asyncio
async def test_user_inbox_round_robins_chats_and_acknowledges_only_own_account(delivery):
    listener, _, _, _ = delivery
    inbox = TelegramUserInbox(listener._database)
    async with listener._runtime.use(listener.account_id):
        targets = await listener._database.telegram_ingest_targets(listener.account_id)
        for target in targets:
            messages = [ParsedSourceMessage(
                is_edit=False, chat_id=target["chat_id"], chat_title="Feed", message_id=i,
                sender_id=None, reply_to_message_id=None, media_group_id=None, message_text="fixture",
                attachments=(), file_unique_id=None, message_date="2026-10-15", filter_result="ACCEPT", filter_reason="fixture",
            ) for i in range(2, 105)]
            inbox.save_batch(target, 104, messages)
        items = inbox.pending(limit=4)
        assert [message.chat_id for _, message in items] == [CHATS[0], CHATS[1], CHATS[0], CHATS[1]]
    # An unscoped legacy consumer cannot see or delete these account deliveries.
    assert inbox.pending(limit=100) == []
    inbox.acknowledge(items[0][0])
    async with listener._runtime.use(listener.account_id):
        assert inbox.pending(limit=4) == items


@pytest.mark.asyncio
async def test_published_delivery_replay_after_ack_failure_does_not_repeat_ai(delivery, monkeypatch):
    listener, ingestor, _, _ = delivery
    calls = []

    class Admission:
        async def decide(self, message):
            calls.append(message.ex_gid)
            return SimpleNamespace(verdict="accept", override=True, reason="fixture accepted")

    admission = Admission()
    ingestor.set_admission(admission)
    await listener._ingest_with_user_account()
    original = TelegramUserInbox.acknowledge

    def fail_ack(self, inbox_id):
        raise sqlite3.OperationalError("fixture interrupted acknowledgement")

    monkeypatch.setattr(TelegramUserInbox, "acknowledge", fail_ack)
    with pytest.raises(sqlite3.OperationalError, match="fixture interrupted acknowledgement"):
        await listener._consume_pending()
    assert len(calls) == 1
    listener._candidate_ingestor = CandidateIngestor(listener._database, admission=admission)
    monkeypatch.setattr(TelegramUserInbox, "acknowledge", original)
    await listener._consume_pending()
    assert len(calls) == 2
    assert len(set(calls)) == 2
    with listener._database.connection() as connection:
        assert connection.execute("SELECT count(*) FROM candidates").fetchone() == (2,)
        assert connection.execute("SELECT count(*) FROM telegram_user_inbox").fetchone() == (0,)


@pytest.mark.asyncio
async def test_034_upgrade_is_repeatable_and_keeps_existing_cursors(delivery):
    listener, _, _, _ = delivery
    db = listener._database
    with db.connection() as connection:
        connection.execute("DROP TABLE telegram_user_inbox")
        connection.execute("DELETE FROM schema_migrations WHERE version = 34")
    await db.initialize()
    await db.initialize()
    assert {row["cursor"] for row in await db.telegram_ingest_targets(listener.account_id)} == {1}
    await listener._ingest_with_user_account()
    await listener._consume_pending()
    with db.connection() as connection:
        assert connection.execute("SELECT count(*) FROM schema_migrations WHERE version = 34").fetchone() == (1,)
        assert connection.execute("SELECT count(*) FROM candidates").fetchone() == (2,)
