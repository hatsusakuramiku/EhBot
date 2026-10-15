"""Registered listeners retain legacy subscriptions while sharing admission."""
import asyncio
from datetime import UTC, datetime
from types import SimpleNamespace

import httpx
import pytest

from app.candidates.ingestor import CandidateIngestor
from app.connections.accounts import AccountError, AccountRegistry
from app.connections.lifecycle import AccountLifecycle
from app.connections.listeners import AccountListener
from app.connections.models import ProviderStatus, TelegramUserAccount
from app.connections.runtime import AccountRuntime, current_account_context
from app.connections.service import AccountService
from app.db.database import Database
from app.secrets import SecretStore


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["TG_BOT", "TG_USER"])
async def test_imported_listener_reuses_subscription_and_admits_gallery_once(tmp_path, monkeypatch, kind):
    database = Database(tmp_path / "runtime.db")
    await database.initialize()
    chat = -1000000000123
    await database.configure_telegram_source(source_type="CHANNEL", chat_id=chat,
        display_name="Legacy fixture", enabled=True, allowed_archive_formats=("zip",), max_attachment_size_mb=0)
    await database.set_source_cursor(chat, 1)
    secrets = SecretStore(tmp_path / "private")
    if kind == "TG_BOT":
        secrets.write("telegram_bot_token", "fixture-bot")
    else:
        secrets.write("telegram_user_api", "1234:" + "a" * 32)
        secrets.write("telegram_user_session", "fixture-session")
    registry = AccountRegistry(database, secrets)
    account_id = (await registry.import_legacy())[kind]
    account = await registry.get(account_id)
    account = await registry.record_identity(account_id, account["credential_revision"], "42", "Fixture")
    await registry.enable(account_id, expected_revision=account["revision"])
    ingestor = CandidateIngestor(database)
    admitted = []

    class Admission:
        async def decide(self, message):
            admitted.append((message.account_id, message.ex_gid))
            return SimpleNamespace(verdict="accept", override=True, reason="fixture")

    ingestor.set_admission(Admission())
    sent = False

    async def respond(request):
        nonlocal sent
        if sent:
            await asyncio.sleep(0.01)
            return httpx.Response(200, json={"ok": True, "result": []})
        sent = True
        return httpx.Response(200, json={"ok": True, "result": [
            {"update_id": mid, "channel_post": {"message_id": mid, "date": 1,
             "chat": {"id": chat, "type": "channel", "title": "Fixture"},
             "text": "https://e-hentai.org/g/123/token/"}} for mid in (2, 3)]})

    class UserClient:
        async def load_dialogs(self):
            return {chat: "Fixture"}

        async def fetch_channel_messages(self, chat_id, *, after_id, limit):
            return [SimpleNamespace(id=mid, chat_id=chat_id, date=datetime.now(UTC),
                message="https://e-hentai.org/g/123/token/") for mid in (2, 3) if mid > after_id]

    monkeypatch.setattr("app.connections.manager._USER_INGEST_INTERVAL_SECONDS", 0.01)
    async with httpx.AsyncClient(base_url="https://api.telegram.org", transport=httpx.MockTransport(respond)) as http:
        runtime = AccountRuntime(registry, AccountLifecycle(registry))
        listener = AccountListener(account_id, runtime, database, telegram_client=http, candidate_ingestor=ingestor)
        listener._telegram_user = TelegramUserAccount("connected", True)
        monkeypatch.setattr(listener, "_user_client", lambda *args: UserClient())
        task = asyncio.create_task(listener._poll_telegram() if kind == "TG_BOT" else listener._run_user_ingest())
        if kind == "TG_BOT":
            listener._telegram_task = task
        else:
            listener._user_ingest_task = task
        try:
            async def wait_for_observations():
                while True:
                    with database.connection() as connection:
                        if connection.execute("SELECT count(*) FROM candidate_messages").fetchone() == (2,):
                            return
                    if task.done():
                        task.result()
                    await asyncio.sleep(0.01)
            await asyncio.wait_for(wait_for_observations(), 3)
        finally:
            await listener.stop()
            await asyncio.gather(task, return_exceptions=True)
    assert admitted == [(account_id, 123)]
    with database.connection() as connection:
        assert connection.execute("SELECT count(*) FROM candidates").fetchone() == (1,)
        assert connection.execute("SELECT DISTINCT ta.external_account_id FROM source_messages sm "
                                  "JOIN telegram_accounts ta ON ta.id = sm.account_id").fetchall() == [(account_id,)]


@pytest.mark.asyncio
async def test_startup_drains_legacy_inbox_beyond_one_batch_without_registered_accounts(tmp_path):
    database = Database(tmp_path / "backlog.db")
    await database.initialize()
    await database.save_telegram_updates([
        {"update_id": index, "message": {"chat": {"id": -1000000000123}}}
        for index in range(1, 206)
    ])
    registry = AccountRegistry(database, SecretStore(tmp_path / "private"))
    async def offline(request):
        pytest.fail("Recovering durable input must not request a provider")
    async with httpx.AsyncClient(transport=httpx.MockTransport(offline)) as http:
        service = AccountService(registry, telegram_client=http, exhentai_client=http,
                                 candidate_ingestor=CandidateIngestor(database))
        try:
            await asyncio.wait_for(service.start(), 1)
            async def drained():
                while await database.pending_telegram_updates(limit=1):
                    await asyncio.sleep(0.01)
            await asyncio.wait_for(drained(), 10)
        finally:
            await service.stop()
    with database.connection() as connection:
        assert connection.execute("SELECT count(*), count(processed_at) FROM telegram_bot_updates").fetchone() == (205, 205)
        assert connection.execute("SELECT count(*) FROM external_accounts").fetchone() == (0,)


@pytest.mark.asyncio
async def test_explicit_source_account_scope_checks_kind_and_releases_credentials(tmp_path):
    database = Database(tmp_path / "scopes.db")
    await database.initialize()
    registry = AccountRegistry(database, SecretStore(tmp_path / "private"))
    account = await registry.create("TG_BOT", "Fixture", {"bot_token": "fixture-bot"})
    account = await registry.record_identity(account["account_id"], account["credential_revision"], "42", "Fixture")
    await registry.enable(account["account_id"], expected_revision=account["revision"])
    async with httpx.AsyncClient() as http:
        service = AccountService(registry, telegram_client=http, exhentai_client=http)
        service._states[account["account_id"]] = ProviderStatus("connected", True)
        async with service.use_kind("TG_BOT", account_id=account["account_id"]):
            assert current_account_context.get().account_id == account["account_id"]
        assert current_account_context.get() is None
        with pytest.raises(AccountError) as error:
            async with service.use_kind("TG_USER", account_id=account["account_id"]):
                pytest.fail("A Bot account cannot provide user credentials")
        assert error.value.code == "ACCOUNT_KIND_INVALID"
        assert current_account_context.get() is None
        await service.stop()
