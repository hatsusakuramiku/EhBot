"""Saved-account runtime integration with controlled local transports only."""
import asyncio
import json
from types import SimpleNamespace

import httpx
import pytest
import pytest_asyncio

from app.connections.accounts import AccountError
from app.connections.models import ProviderConnectionError
from app.connections.runtime import current_account_context
from app.connections.service import AccountService
from tests.integration.test_account_registry import registry
from tests.integration.test_account_lifecycle import no_queue_operations


@pytest_asyncio.fixture
async def service(registry):
    async def handler(request):
        if request.url.path.endswith("getMe"):
            return httpx.Response(200, json={"ok": True, "result": {"id": 1234, "username": "test_bot", "first_name": "Bot"}})
        if request.url.path.endswith("getUpdates"):
            await asyncio.Event().wait()
        return httpx.Response(200, text="ExHentai")
    async with httpx.AsyncClient(base_url="https://api.telegram.org", transport=httpx.MockTransport(handler)) as client:
        result = AccountService(registry, telegram_client=client, exhentai_client=client, process_queue_operations=no_queue_operations)
        yield result
        await result.stop()


async def configured(service, kind="EH", member_id="1234"):
    fields = {"ipb_member_id": member_id, "ipb_pass_hash": "test-cookie", "igneous": "test-igneous"} if kind == "EH" else {"bot_token": "test-token"}
    account = await service.registry.create(kind, "Account", fields)
    account = await service.test_account(account["account_id"], expected_revision=account["revision"])
    return await service.enable_account(account["account_id"], expected_revision=account["revision"])


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["EH", "TG_BOT", "TG_USER"])
async def test_saved_disable_survives_restart_and_reconnect_reuses_credentials(service, monkeypatch, kind):
    if kind == "TG_USER":
        account = await service.registry.create(kind, "User", {"api_id":"1234", "api_hash":"test-api-hash", "phone":"+123456789"})
        account = await service.registry.save_session(account["account_id"], expected_revision=account["revision"],
            session="retained-private-session", remote_identity="42", platform_name="User")
        class UserClient:
            async def verify(self):
                return SimpleNamespace(user_id=42, label="User")
        monkeypatch.setattr(service, "_user_client", lambda *args, **kwargs: UserClient())
        account = await service.enable_account(account["account_id"], expected_revision=account["revision"])
        field, value = "session", "retained-private-session"
    else:
        account = await configured(service, kind)
        field, value = ("igneous", "test-igneous") if kind == "EH" else ("bot_token", "test-token")
    async with service.use_kind(kind) as context:
        assert context.values[field] == value
    receipt = await service.stop_account(account["account_id"], "disable", expected_revision=account["revision"], idempotency_key="disable")
    assert await service.process_operations() == 1
    assert (await service.lifecycle.get_operation(receipt["operation_id"]))["status"] == "COMPLETED"
    assert await service.selected_account(kind) is None
    disabled = await service.registry.get(account["account_id"])
    assert (await service.registry.context(account["account_id"])).values[field] == value
    restarted = AccountService(service.registry, telegram_client=service._telegram_client, exhentai_client=service._exhentai_client,
                               process_queue_operations=no_queue_operations)
    try:
        await restarted.start()
        assert await restarted.selected_account(kind) is None
        assert not (await service.registry.get(account["account_id"]))["enabled"]
        await restarted.enable_account(account["account_id"], expected_revision=disabled["revision"])
        assert (await restarted.selected_account(kind))["account_id"] == account["account_id"]
        async with restarted.use_kind(kind) as context:
            assert context.values[field] == value
    finally:
        await restarted.stop()


@pytest.mark.asyncio
async def test_legacy_import_startup_repeats_without_writing_old_private_files(service):
    originals = {"telegram_bot_token": "legacy-test-token", "exhentai_cookies": json.dumps({"ipb_member_id": "1234", "ipb_pass_hash": "test-cookie", "igneous": "test-igneous"})}
    for name, value in originals.items():
        service.registry._secrets.write(name, value)
    await service.registry._database.configure_telegram_source(
        source_type="CHANNEL", chat_id=-1000000000600, display_name="Legacy startup feed",
        enabled=True, allowed_archive_formats=("zip",), max_attachment_size_mb=128,
    )
    await service.start()
    accounts = await service.registry.list_accounts()
    assert len(accounts) == 2 and all(account["enabled"] for account in accounts)
    assert service.snapshot().telegram.state == "connected" and service.snapshot().exhentai.state == "connected"
    sources = await service.registry._database.list_telegram_sources()
    bot = next(account for account in accounts if account["kind"] == "TG_BOT")
    assert len(sources) == 1 and sources[0].account_id == bot["account_id"]
    assert (await service.registry._database.telegram_ingest_targets(bot["account_id"]))[0]["chat_id"] == -1000000000600
    await service.stop()
    await service.start()
    assert {row["account_id"] for row in await service.registry.list_accounts()} == {row["account_id"] for row in accounts}
    assert all(service.registry._secrets.read(name) == value for name, value in originals.items())
    assert await service.registry._database.list_telegram_sources() == sources


@pytest.mark.asyncio
async def test_multiple_same_kind_accounts_can_run_concurrently(service):
    first = await configured(service)
    second = await service.registry.create("EH", "Second", {"ipb_member_id": "5678", "ipb_pass_hash": "test-cookie", "igneous": "test-igneous"})
    second = await service.test_account(second["account_id"], expected_revision=second["revision"])
    enabled = await service.enable_account(second["account_id"], expected_revision=second["revision"])
    assert enabled["enabled"]
    assert (await service.registry.get(first["account_id"]))["enabled"]
    assert (await service.registry.get(second["account_id"]))["enabled"]


@pytest.mark.asyncio
async def test_failed_verification_keeps_editable_saved_fields(registry):
    async def handler(request):
        return httpx.Response(401, json={"ok": False})
    async with httpx.AsyncClient(base_url="https://api.telegram.org", transport=httpx.MockTransport(handler)) as client:
        service = AccountService(registry, telegram_client=client, exhentai_client=client)
        account = await registry.create("TG_BOT", "Bot", {"bot_token": "retained-test-token"})
        with pytest.raises(ProviderConnectionError):
            await service.test_account(account["account_id"], expected_revision=account["revision"])
        assert await registry.get(account["account_id"]) == account
        assert (await registry.reveal(account["account_id"]))["fields"]["bot_token"] == "retained-test-token"
        assert service.snapshot().telegram.state == "error"


@pytest.mark.asyncio
async def test_nested_selection_keeps_same_private_version_across_requests(service):
    account = await configured(service)
    async with service.use_kind("EH") as first:
        assert current_account_context.get() is first
        async with service.use_kind("EH") as second:
            assert second is first
    assert current_account_context.get() is None
    assert service.runtime._leases == {}


@pytest.mark.asyncio
async def test_bot_listener_stop_waits_for_http_exit_before_edit_binds_new_token(registry):
    entered, cancelling, release = (asyncio.Event() for _ in range(3))
    async def handler(request):
        if request.url.path.endswith("getMe"):
            return httpx.Response(200, json={"ok": True, "result": {"id": 1234, "username": "test_bot", "first_name": "Bot"}})
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelling.set()
            await release.wait()
    async with httpx.AsyncClient(base_url="https://api.telegram.org", transport=httpx.MockTransport(handler)) as client:
        service = AccountService(registry, telegram_client=client, exhentai_client=client, process_queue_operations=no_queue_operations)
        account = await configured(service, "TG_BOT")
        await asyncio.wait_for(entered.wait(), 3)
        await service.stop_account(account["account_id"], "edit", expected_revision=account["revision"], idempotency_key="bot-edit", fields={"bot_token": "next-test-token"})
        processing = asyncio.create_task(service.process_operations())
        await asyncio.wait_for(cancelling.wait(), 3)
        try:
            assert not processing.done()
            assert (await registry.reveal(account["account_id"]))["fields"]["bot_token"] == "test-token"
        finally:
            release.set()
            assert await processing == 1
            await service.stop()
        assert (await registry.reveal(account["account_id"]))["fields"]["bot_token"] == "next-test-token"


@pytest.mark.asyncio
async def test_tg_login_keeps_session_private_and_requires_correct_challenge_revision(service, monkeypatch):
    account = await service.registry.create("TG_USER", "User", {"api_id": "1234", "api_hash": "test-api-hash", "phone": "+123456789"})
    from app.connections.telegram_user import LoginChallenge
    class Client:
        async def send_code(self, phone):
            return LoginChallenge(phone, "test-code-hash", session="challenge-private-session")
        async def sign_in(self, challenge, **kwargs):
            assert challenge.session == "challenge-private-session"
            assert kwargs["code"] == "12345"
            return "completed-private-session", SimpleNamespace(user_id=42, label="Test User")
    monkeypatch.setattr(service, "_user_client", lambda *args, **kwargs: Client())
    login = await service.start_login(account["account_id"], expected_revision=account["revision"])
    with pytest.raises(AccountError) as wrong:
        await service.complete_login(account["account_id"], expected_revision=account["revision"], challenge_id="wrong", code="12345")
    assert wrong.value.code == "ACCOUNT_LOGIN_STALE"
    logged_in = await service.complete_login(account["account_id"], expected_revision=account["revision"], challenge_id=login["challenge_id"], code="12345")
    assert logged_in["verified"] and not logged_in["enabled"] and logged_in["credential_revision"] == 2
    assert (await service.registry.context(account["account_id"])).values["session"] == "completed-private-session"
    assert "session" not in (await service.registry.reveal(account["account_id"]))["fields"]
    edited = await service.registry.edit(account["account_id"], expected_revision=logged_in["revision"], fields={"phone": "+987654321"})
    assert not edited["verified"] and "session" not in (await service.registry.context(account["account_id"])).values


@pytest.mark.asyncio
async def test_login_reply_after_config_edit_cannot_save_session(service, monkeypatch):
    account = await service.registry.create("TG_USER", "User", {"api_id": "1234", "api_hash": "test-api-hash", "phone": "+123456789"})
    from app.connections.telegram_user import LoginChallenge
    entered, release = asyncio.Event(), asyncio.Event()
    class Client:
        async def send_code(self, phone):
            return LoginChallenge(phone, "test-code-hash")
        async def sign_in(self, *args, **kwargs):
            entered.set()
            await release.wait()
            return "late-private-session", SimpleNamespace(user_id=42, label="Test User")
    monkeypatch.setattr(service, "_user_client", lambda *args, **kwargs: Client())
    login = await service.start_login(account["account_id"], expected_revision=account["revision"])
    completing = asyncio.create_task(service.complete_login(account["account_id"], expected_revision=account["revision"], challenge_id=login["challenge_id"], code="12345"))
    await asyncio.wait_for(entered.wait(), 3)
    await service.registry.edit(account["account_id"], expected_revision=account["revision"], fields={"phone": "+987654321"})
    release.set()
    with pytest.raises(AccountError) as stale:
        await completing
    assert stale.value.code == "REVISION_CONFLICT"
    assert "session" not in (await service.registry.context(account["account_id"])).values


@pytest.mark.asyncio
async def test_pending_login_command_retries_never_resend_code_and_replay_survives_restart(service, monkeypatch):
    account = await service.registry.create("TG_USER", "User", {"api_id":"1234", "api_hash":"test-api-hash", "phone":"+123456789"})
    from app.connections.telegram_user import LoginChallenge
    entered, release = asyncio.Event(), asyncio.Event()
    calls = []
    class Client:
        async def send_code(self, phone):
            calls.append(phone)
            entered.set()
            await release.wait()
            return LoginChallenge(phone, "test-code-hash")
    monkeypatch.setattr(service, "_user_client", lambda *args, **kwargs: Client())
    params = dict(account_id=account["account_id"], expected_revision=account["revision"], idempotency_key="get-code-once")
    first = asyncio.create_task(service.command("login-start", **params))
    await asyncio.wait_for(entered.wait(), 3)
    pending_receipt = await service.command("login-start", **params)
    assert pending_receipt["status"] == "PENDING" and calls == ["+123456789"]
    release.set()
    completed = await first
    assert completed["status"] == "COMPLETED"
    await service.stop()
    assert await service.command("login-start", **params) == completed
    assert calls == ["+123456789"]
    with pytest.raises(AccountError) as stale:
        await service.complete_login(account["account_id"], expected_revision=account["revision"], challenge_id=completed["result"]["challenge_id"], code="12345")
    assert stale.value.code == "ACCOUNT_LOGIN_STALE"


@pytest.mark.asyncio
async def test_create_receipt_failure_rolls_back_account_and_cleans_private_file(service):
    import sqlite3
    with service.registry._database.connection() as c:
        c.execute("CREATE TRIGGER refuse_create_receipt BEFORE INSERT ON account_command_receipts "
                  "BEGIN SELECT RAISE(ABORT, 'CONTROLLED_REFUSAL'); END")
    with pytest.raises(sqlite3.IntegrityError, match="CONTROLLED_REFUSAL"):
        await service.command("create", idempotency_key="create-rollback", kind="TG_BOT", label="Bot", fields={"bot_token":"test-token"})
    assert await service.registry.list_accounts() == []
    assert list(service.registry._secrets.root.iterdir()) == []


@pytest.mark.asyncio
async def test_two_account_login_challenges_cancel_independently_and_reject_stale_cancel(service, monkeypatch):
    from app.connections.telegram_user import LoginChallenge
    class Client:
        async def send_code(self, phone):
            return LoginChallenge(phone, "test-code-hash", session="challenge-private-session")
    monkeypatch.setattr(service, "_user_client", lambda *args, **kwargs: Client())
    first = await service.registry.create("TG_USER", "First", {"api_id":"1234", "api_hash":"test-api-hash", "phone":"+123456789"})
    second = await service.registry.create("TG_USER", "Second", {"api_id":"5678", "api_hash":"other-test-api-hash", "phone":"+987654321"})
    flows = await asyncio.gather(*(service.start_login(account["account_id"], expected_revision=account["revision"])
                                   for account in (first, second)))
    assert flows[0]["challenge_id"] != flows[1]["challenge_id"]
    renamed = await service.registry.edit(first["account_id"], expected_revision=first["revision"], label="Renamed")
    with pytest.raises(AccountError) as stale:
        await service.cancel_login(first["account_id"], flows[0]["challenge_id"], expected_revision=first["revision"])
    assert stale.value.code == "REVISION_CONFLICT"
    assert first["account_id"] in service._logins
    await service.cancel_login(first["account_id"], flows[0]["challenge_id"], expected_revision=renamed["revision"])
    assert first["account_id"] not in service._logins
    assert service._logins[second["account_id"]].challenge_id == flows[1]["challenge_id"]
    assert (await service.registry.reveal(first["account_id"]))["fields"]["phone"] == "+123456789"
    assert (await service.registry.reveal(second["account_id"]))["fields"]["api_hash"] == "other-test-api-hash"


@pytest.mark.asyncio
async def test_active_connection_retest_keeps_runtime_selected(service):
    account = await configured(service)
    verified = await service.test_account(account["account_id"], expected_revision=account["revision"])
    assert verified["enabled"]
    assert service.account_status(account["account_id"])["state"] == "connected"
    assert (await service.selected_account("EH"))["account_id"] == account["account_id"]


@pytest.mark.asyncio
async def test_verification_cannot_upgrade_revision_over_concurrent_alias_edit(service, monkeypatch):
    from app.connections.telegram import TelegramBotApi
    account = await service.registry.create("TG_BOT", "Before", {"bot_token": "test-token"})
    entered, release = asyncio.Event(), asyncio.Event()
    async def verify(self):
        entered.set()
        await release.wait()
        return SimpleNamespace(bot_id=1234, username="test_bot")
    monkeypatch.setattr(TelegramBotApi, "verify", verify)
    verifying = asyncio.create_task(service.test_account(account["account_id"], expected_revision=account["revision"]))
    await asyncio.wait_for(entered.wait(), 3)
    renamed = await service.registry.edit(account["account_id"], expected_revision=account["revision"], label="Other client")
    release.set()
    with pytest.raises(AccountError) as stale:
        await verifying
    assert stale.value.code == "REVISION_CONFLICT"
    assert await service.registry.get(account["account_id"]) == renamed
    assert not renamed["verified"]


@pytest.mark.asyncio
async def test_delete_removes_runtime_projection_without_reimport(service):
    account = await configured(service)
    await service.stop_account(account["account_id"], "delete", expected_revision=account["revision"], idempotency_key="delete-state")
    assert await service.process_operations() == 1
    assert not service.snapshot().exhentai.configured
    assert account["account_id"] not in service._states
    assert await service.selected_account("EH") is None


@pytest.mark.asyncio
async def test_manual_ingest_keeps_account_lease_through_caller_commit(service, monkeypatch, tmp_path):
    from app.candidates.manual import ingest_manual_link
    from app.exhentai.service import ExHentaiService
    account = await configured(service)
    database = service.registry._database
    entered, release = asyncio.Event(), asyncio.Event()
    async def credentials():
        return service._eh_credentials(current_account_context.get().values)
    gallery_service = ExHentaiService(database, tmp_path, tmp_path, credentials, account_scope=service.use_kind)
    async def fetch(gid, token):
        # This stub represents the authenticated HTML fallback. Public gdata
        # deliberately holds no account through manual publication.
        await gallery_service._account_credentials()
        return {}, None
    async def publish(*args, **kwargs):
        assert current_account_context.get().account_id == account["account_id"]
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            await release.wait()
    monkeypatch.setattr(gallery_service, "fetch_metadata_for_gallery", fetch)
    monkeypatch.setattr(gallery_service, "publish_manual_gallery", publish)
    ingest = asyncio.create_task(ingest_manual_link("https://exhentai.org/g/1234/token/", database=database,
        exhentai_service=gallery_service, orchestrator=None, download_service=None))
    await asyncio.wait_for(entered.wait(), 3)
    await service.stop_account(account["account_id"], "disable", expected_revision=account["revision"], idempotency_key="stop-manual")
    stopping = asyncio.create_task(service.process_operations())
    try:
        await asyncio.sleep(0.05)
        assert not stopping.done()
    finally:
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await ingest
        assert await stopping == 1
