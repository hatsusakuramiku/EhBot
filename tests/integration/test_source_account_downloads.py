"""Real queue execution keeps the selected observation's account and revision."""
import asyncio
from dataclasses import replace
import json

import httpx
import pytest
import pytest_asyncio

from app.connections.accounts import AccountError, AccountRegistry
from app.connections.models import ProviderConnectionError, ProviderStatus
from app.connections.runtime import current_account_context
from app.connections.service import AccountService
from app.db.database import Database
from app.downloads.service import DownloadError, DownloadService
from app.review.orchestration import ReviewOrchestrator
from app.secrets import SecretStore
from tests.integration.test_ingest_entries import message


@pytest_asyncio.fixture
async def environment(tmp_path):
    db = Database(tmp_path / "source.db")
    await db.initialize()
    registry = AccountRegistry(db, SecretStore(tmp_path / "private"))
    requests = []
    async def handler(request):
        requests.append(request.url.path)
        if request.url.path.endswith("getUpdates"):
            await asyncio.Event().wait()
        if request.url.path.endswith("getFile"):
            return httpx.Response(200, json={"ok": True, "result": {
                "file_id": "file", "file_unique_id": "unique", "file_path": "book.zip", "file_size": 4}})
        return httpx.Response(200, content=b"book")
    async with httpx.AsyncClient(base_url="https://api.telegram.org", transport=httpx.MockTransport(handler)) as client:
        accounts = AccountService(registry, telegram_client=client, exhentai_client=client)
        async def create(kind, label):
            fields = {"bot_token": "fixture-" + label} if kind == "TG_BOT" else {"api_id": "1", "api_hash": "fixture-hash"}
            row = await registry.create(kind, label, fields)
            row = await registry.record_identity(row["account_id"], expected_revision=row["revision"],
                credential_revision=row["credential_revision"], remote_identity=label, platform_name=label)
            row = await registry.enable(row["account_id"], expected_revision=row["revision"])
            accounts._states[row["account_id"]] = ProviderStatus("connected", True)
            return row["account_id"]
        yield db, accounts, create, requests, client
        await accounts.stop()


async def source(db, account, *, file_id="file", gid=123, file_unique_id="unique", chat_id=-1000000000777,
                 peer_type="channel"):
    attachment = {"type": "archive", "file_id": file_id, "file_unique_id": file_unique_id, "file_name": "book.zip", "size_bytes": 4}
    await db.save_candidate_message(None, replace(message(account, gid=gid, chat_id=chat_id), attachments=(attachment,), peer_type=peer_type))
    with db.connection() as c:
        cid = c.execute("SELECT id FROM candidates WHERE ex_gid = ?", (gid,)).fetchone()[0]
        c.execute("UPDATE candidates SET status = 'APPROVED' WHERE id = ?", (cid,))
    return await db.get_candidate(cid)


def download_service(db, accounts, client, tmp_path):
    async def bot_context():
        context = current_account_context.get()
        return context.values["bot_token"], client
    return DownloadService(db, tmp_path / "work", account_scope=accounts.use_kind,
        telegram_client_factory=bot_context, telegram_user_client=accounts.telegram_user_context,
        telegram_user_source=accounts.resolve_telegram_user_source,
        telegram_bot_source=accounts.resolve_telegram_bot_source)


@pytest.mark.asyncio
async def test_second_bot_observation_uses_second_token_and_records_attempt_binding(environment, tmp_path):
    db, accounts, create, requests, client = environment
    a, b = await create("TG_BOT", "a"), await create("TG_BOT", "b")
    await source(db, a, file_id="a-file")
    candidate = await source(db, b, file_id="b-file")
    attachment = candidate.messages[1].attachments[0]
    service = download_service(db, accounts, client, tmp_path)
    result = await service.enqueue_telegram_download(candidate.candidate_id, attachment)
    job = service._claim_pending_job_sync()
    await service._execute_attempt(job)
    assert "/botfixture-b/getFile" in requests and "/botfixture-a/getFile" not in requests
    with db.connection() as c:
        details, state = c.execute("SELECT details_json, state FROM download_jobs WHERE id = ?", (result.job_id,)).fetchone()
        assert state == "COMPLETED"
        assert json.loads(details)["origin_account_id"] == b
        assert json.loads(details)["observation_id"] == candidate.messages[1].observation_id
        assert c.execute("SELECT account_id FROM account_execution_bindings").fetchall() == [(b,)]


@pytest.mark.asyncio
async def test_stale_observation_never_contacts_bot(environment, tmp_path):
    db, accounts, create, requests, client = environment
    a = await create("TG_BOT", "a")
    candidate = await source(db, a)
    attachment = candidate.messages[0].attachments[0]
    service = download_service(db, accounts, client, tmp_path)
    result = await service.enqueue_telegram_download(candidate.candidate_id, attachment)
    with db.connection() as c:
        c.execute("UPDATE source_messages SET observation_revision = observation_revision + 1")
    await service._execute_attempt(service._claim_pending_job_sync())
    assert not requests
    with db.connection() as c:
        assert c.execute("SELECT state, error_code FROM download_jobs WHERE id = ?", (result.job_id,)).fetchone() == (
            "FAILED", "TELEGRAM_SOURCE_CHANGED")


@pytest.mark.asyncio
@pytest.mark.parametrize('change', ['none', 'metadata', 'source-revision'])
async def test_automatic_user_preflight_allows_other_writes_and_rechecks_before_commit(environment, tmp_path, monkeypatch, change):
    db, accounts, create, _, client = environment
    account = await create('TG_USER', 'user')
    candidate = await source(db, account, file_id='')
    cid = candidate.candidate_id
    # source() creates an already-approved fixture; remove that evidence so this
    # exercises the automatic approval transaction rather than a manual retry.
    with db.connection() as c:
        c.execute('DELETE FROM candidate_approvals WHERE candidate_id = ?', (cid,))
        c.execute("UPDATE candidates SET status = 'PENDING_REVIEW' WHERE id = ?", (cid,))
        c.execute("INSERT INTO metadata_values(candidate_id, field_name, field_value, value_source) "
                  "VALUES(?, 'Title', 'ready', 'EXHENTAI')", (cid,))
    await db.save_auto_approval_rule(rule_id=None, name='preflight', enabled=True, priority=1,
        condition={'kind': 'condition', 'field': 'Title', 'operator': '=', 'value': 'ready'},
        dsl_snapshot='{Title} = "ready"')
    entered, release = asyncio.Event(), asyncio.Event()
    async def preflight(attachment):
        entered.set()
        await release.wait()
        return {'account_id': account, 'download_file_unique_id': 'unique'}
    monkeypatch.setattr(accounts, 'resolve_telegram_user_source', preflight)
    service = download_service(db, accounts, client, tmp_path)
    review = ReviewOrchestrator(db, lambda: service,
        torrent_available=lambda: False, telegraph_available=lambda: False,
        telegram_user_available=lambda: True, download_source_priority=lambda: ('TELEGRAM_USER',))
    pending = asyncio.create_task(review.apply_automatic_decision(cid))
    def concurrent_write():
        with db.connection() as c:
            c.execute('BEGIN IMMEDIATE')
            if change == 'metadata':
                c.execute("UPDATE metadata_values SET field_value = 'changed' WHERE candidate_id = ?", (cid,))
            elif change == 'source-revision':
                c.execute('UPDATE source_messages SET observation_revision = observation_revision + 1')
            else:
                c.execute("UPDATE external_accounts SET label = 'edited during supplier call' WHERE account_id = ?", (account,))
    try:
        await asyncio.wait_for(entered.wait(), 3)
        await asyncio.wait_for(asyncio.to_thread(concurrent_write), 3)
        release.set()
        assert await asyncio.wait_for(pending, 3) == ('APPROVE' if change == 'none' else None)
    finally:
        release.set()
        await asyncio.gather(pending, return_exceptions=True)
    with db.connection() as c:
        expected = int(change == 'none')
        assert c.execute('SELECT count(*) FROM download_jobs').fetchone() == (expected,)
        assert c.execute('SELECT count(*) FROM candidate_approvals').fetchone() == (expected,)
        assert c.execute("SELECT count(*) FROM review_actions WHERE action = 'AUTO_APPROVE'").fetchone() == (expected,)


@pytest.mark.asyncio
async def test_edit_during_transfer_rejects_artifact_and_completion(environment, tmp_path, monkeypatch):
    db, accounts, create, _, client = environment
    account = await create("TG_USER", "user")
    candidate = await source(db, account, file_id="")
    seen = []
    class User:
        async def download_message_media(self, chat, mid, destination, **kwargs):
            with db.connection() as c:
                c.execute("UPDATE source_messages SET observation_revision = observation_revision + 1")
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(b"book")
            seen.append(destination)
            return 4
    monkeypatch.setattr(accounts, "_user_client", lambda *args, **kwargs: User())
    service = download_service(db, accounts, client, tmp_path)
    result = await service.enqueue_telegram_user_download(candidate.candidate_id, candidate.messages[0].attachments[0])
    await service._execute_attempt(service._claim_pending_job_sync())
    assert seen
    with db.connection() as c:
        assert c.execute("SELECT count(*) FROM artifacts").fetchone() == (0,)
        assert c.execute("SELECT state, error_code FROM download_jobs WHERE id = ?", (result.job_id,)).fetchone() == (
            "FAILED", "TELEGRAM_SOURCE_CHANGED")


@pytest.mark.asyncio
async def test_disabling_bound_user_waits_for_transfer_cleanup_and_keeps_slot(environment, tmp_path, monkeypatch):
    db, accounts, create, _, client = environment
    account = await create("TG_USER", "user")
    candidate = await source(db, account, file_id="")
    entered, cleaning, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
    class User:
        async def download_message_media(self, *args, **kwargs):
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                cleaning.set()
                await release.wait()
    monkeypatch.setattr(accounts, "_user_client", lambda *args, **kwargs: User())
    service = download_service(db, accounts, client, tmp_path)
    accounts._process_queue_operations = service.process_operations
    result = await service.enqueue_telegram_user_download(candidate.candidate_id, candidate.messages[0].attachments[0])
    worker = asyncio.create_task(service._process_one())
    await asyncio.wait_for(entered.wait(), 5)
    row = await accounts.registry.get(account)
    await accounts.stop_account(account, "disable", expected_revision=row["revision"], idempotency_key="fixture-disable")
    stopping = asyncio.create_task(accounts.process_operations())
    try:
        await asyncio.wait_for(cleaning.wait(), 5)
        assert not stopping.done() and not worker.done()
        assert (await service.get_task(result.task_id))["status"] == "STOPPING"
    finally:
        release.set()
    assert await asyncio.wait_for(stopping, 5) == 1
    await worker
    assert (await service.get_task(result.task_id))["status"] == "FAILED"
    assert not accounts.runtime._leases


@pytest.mark.asyncio
async def test_spoofed_bot_file_or_account_is_rejected_before_enqueue(environment, tmp_path):
    db, accounts, create, _, client = environment
    a, b = await create("TG_BOT", "a"), await create("TG_BOT", "b")
    candidate = await source(db, a)
    attachment = candidate.messages[0].attachments[0]
    service = download_service(db, accounts, client, tmp_path)
    for changed in ({"file_id": "other-file"}, {"account_id": b}, {"observation_revision": 99}):
        with pytest.raises(DownloadError):
            await service.enqueue_telegram_download(candidate.candidate_id, {**attachment, **changed})
    with db.connection() as c:
        assert c.execute("SELECT count(*) FROM download_jobs").fetchone() == (0,)


@pytest.mark.asyncio
async def test_named_account_does_not_fall_back_when_disabled_or_wrong_kind(environment):
    db, accounts, create, _, _ = environment
    a, b = await create("TG_USER", "a"), await create("TG_USER", "b")
    async with accounts.use_kind("TG_USER", account_id=b) as context:
        assert context.account_id == b
    with pytest.raises(AccountError) as error:
        async with accounts.use_account(b, kind="TG_BOT"):
            pytest.fail("wrong type entered")
    assert error.value.code == "ACCOUNT_KIND_INVALID"
    with db.connection() as c:
        c.execute("UPDATE external_accounts SET enabled = 0 WHERE account_id = ?", (b,))
    with pytest.raises(AccountError) as error:
        async with accounts.use_kind("TG_USER", account_id=b):
            pytest.fail("must not select A")
    assert error.value.code == "ACCOUNT_UNAVAILABLE"
    assert (await accounts.selected_account("TG_USER"))["account_id"] == a


@pytest.mark.asyncio
async def test_user_route_prefers_its_own_observation_and_executes_selected_user(environment, tmp_path, monkeypatch):
    db, accounts, create, _, client = environment
    a, b = await create("TG_USER", "a"), await create("TG_USER", "b")
    bot = await create("TG_BOT", "bot")
    await source(db, bot)
    candidate = await source(db, b, file_id="")
    with db.connection() as c:
        c.execute("UPDATE source_messages SET attachment_json = json_set(attachment_json, '$[0].file_unique_id', 'mtproto:42') "
                  "WHERE account_id = (SELECT id FROM telegram_accounts WHERE external_account_id = ?)", (b,))
    candidate = await db.get_candidate(candidate.candidate_id)
    router = ReviewOrchestrator(db, lambda: None, torrent_available=lambda: False, telegraph_available=lambda: False,
        telegram_user_available=lambda: True, download_source_priority=lambda: ("TELEGRAM_USER",))
    selected = router.route_source(candidate).attachment
    assert selected["account_id"] == b
    seen = []
    class User:
        async def download_message_media(self, chat, mid, destination, **kwargs):
            seen.append((current_account_context.get().account_id, kwargs))
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(b"book")
            return 4
    monkeypatch.setattr(accounts, "_user_client", lambda *args, **kwargs: User())
    service = download_service(db, accounts, client, tmp_path)
    result = await service.enqueue_telegram_user_download(candidate.candidate_id, selected)
    await service._execute_attempt(service._claim_pending_job_sync())
    assert seen == [(b, {"expected_file_unique_id": "mtproto:42"})]
    with db.connection() as c:
        assert c.execute("SELECT state FROM download_jobs WHERE id = ?", (result.job_id,)).fetchone() == ("COMPLETED",)


@pytest.mark.asyncio
async def test_bot_large_file_access_check_is_per_user_and_private_peer_is_refused(environment, monkeypatch):
    _, accounts, create, _, _ = environment
    await create("TG_USER", "a")
    await create("TG_USER", "b")
    a, b = [row["account_id"] for row in await accounts.registry.list_accounts()]
    seen = []
    class User:
        async def inspect_message_media(self, chat, mid):
            current = current_account_context.get().account_id
            seen.append(current)
            return {"file_name": "wrong.zip" if current == a else "book.zip", "size_bytes": 4, "file_unique_id": "mtproto:42"}
    monkeypatch.setattr(accounts, "_user_client", lambda *args, **kwargs: User())
    attachment = {"peer_type": "channel", "chat_id": -1000000000777, "message_id": 42, "file_name": "book.zip", "size_bytes": 4,
                  "file_unique_id": "AgADKgAH"}
    result = await accounts.resolve_telegram_user_source(attachment)
    assert result == {"account_id": b, "download_file_unique_id": "mtproto:42"} and seen == [a, b]
    with pytest.raises(AccountError) as error:
        await accounts.resolve_telegram_user_source({**attachment, "peer_type": "private"})
    assert error.value.code == "TELEGRAM_SOURCE_ACCOUNT_REQUIRED"
    assert seen == [a, b]


@pytest.mark.asyncio
async def test_bot_to_user_rejects_same_name_and_size_with_a_different_document(environment, monkeypatch):
    _, accounts, create, _, _ = environment
    await create("TG_USER", "user")

    class User:
        async def inspect_message_media(self, chat, mid):
            return {"file_name": "book.zip", "size_bytes": 4, "file_unique_id": "mtproto:43"}

    monkeypatch.setattr(accounts, "_user_client", lambda *args, **kwargs: User())
    with pytest.raises(AccountError) as error:
        await accounts.resolve_telegram_user_source({"peer_type": "channel", "chat_id": -1000000000777,
            "message_id": 42, "file_name": "book.zip", "size_bytes": 4, "file_unique_id": "AgADKgAH"})
    assert error.value.code == "TELEGRAM_SOURCE_UNAVAILABLE"


@pytest.mark.asyncio
@pytest.mark.parametrize("same_document", [True, False])
@pytest.mark.parametrize("origin_kind", ["TG_USER", "TG_BOT"])
async def test_unavailable_user_uses_only_a_matching_observers_own_private_message(environment, tmp_path, monkeypatch, same_document, origin_kind):
    db, accounts, create, _, client = environment
    a, b = await create(origin_kind, "a"), await create("TG_USER", "b")
    await source(db, a, file_id="", file_unique_id="mtproto:42" if origin_kind == "TG_USER" else "AgADKgAH",
                 chat_id=111, peer_type="private")
    candidate = await source(db, b, file_id="", file_unique_id="mtproto:42" if same_document else "mtproto:43",
                             chat_id=222, peer_type="private")
    selected = candidate.messages[0].attachments[0]
    with db.connection() as connection:
        connection.execute("UPDATE external_accounts SET enabled = 0 WHERE account_id = ?", (a,))
    seen = []

    class User:
        async def inspect_message_media(self, chat, mid):
            seen.append(("inspect", current_account_context.get().account_id, chat, mid))
            return {"file_name": "book.zip", "size_bytes": 4, "file_unique_id": "mtproto:42"}

        async def download_message_media(self, chat, mid, destination, **kwargs):
            seen.append(("download", current_account_context.get().account_id, chat, mid))
            assert kwargs == {"expected_file_unique_id": "mtproto:42"}
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(b"book")
            return 4

    monkeypatch.setattr(accounts, "_user_client", lambda *args, **kwargs: User())
    service = download_service(db, accounts, client, tmp_path)
    if not same_document:
        with pytest.raises(DownloadError):
            await service.enqueue_telegram_user_download(candidate.candidate_id, selected)
        assert not seen
        return
    result = await service.enqueue_telegram_user_download(candidate.candidate_id, selected)
    await service._execute_attempt(service._claim_pending_job_sync())
    assert seen == [("inspect", b, 222, 42), ("download", b, 222, 42)]
    with db.connection() as connection:
        assert connection.execute("SELECT state FROM download_jobs WHERE id = ?", (result.job_id,)).fetchone() == ("COMPLETED",)
        details = json.loads(connection.execute("SELECT details_json FROM download_jobs WHERE id = ?", (result.job_id,)).fetchone()[0])
        assert details["origin_account_id"] == a and details["account_id"] == b


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["preflight", "transfer"])
@pytest.mark.parametrize("change", ["revision", "unlink", "document"])
async def test_fallback_observation_change_cannot_enqueue_or_publish(environment, tmp_path, monkeypatch, stage, change):
    db, accounts, create, _, client = environment
    a, b = await create("TG_USER", "a"), await create("TG_USER", "b")
    await source(db, a, file_unique_id="mtproto:42", chat_id=111, peer_type="private")
    candidate = await source(db, b, file_unique_id="mtproto:42", chat_id=222, peer_type="private")
    fallback_id = candidate.messages[1].observation_id
    with db.connection() as connection:
        connection.execute("UPDATE external_accounts SET enabled = 0 WHERE account_id = ?", (a,))

    def change_fallback():
        with db.connection() as connection:
            if change == "revision":
                connection.execute("UPDATE source_messages SET observation_revision = observation_revision + 1 WHERE id = ?", (fallback_id,))
            elif change == "unlink":
                connection.execute("DELETE FROM candidate_messages WHERE source_message_id = ?", (fallback_id,))
            else:
                connection.execute("UPDATE source_messages SET attachment_json = json_set(attachment_json, '$[0].file_unique_id', 'mtproto:43') WHERE id = ?", (fallback_id,))

    class User:
        async def inspect_message_media(self, chat, mid):
            if stage == "preflight":
                change_fallback()
            return {"file_name": "book.zip", "size_bytes": 4, "file_unique_id": "mtproto:42"}

        async def download_message_media(self, chat, mid, destination, **kwargs):
            assert stage == "transfer"
            change_fallback()
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(b"book")
            return 4

    monkeypatch.setattr(accounts, "_user_client", lambda *args, **kwargs: User())
    service = download_service(db, accounts, client, tmp_path)
    if stage == "preflight":
        with pytest.raises(DownloadError):
            await service.enqueue_telegram_user_download(candidate.candidate_id, candidate.messages[0].attachments[0])
    else:
        result = await service.enqueue_telegram_user_download(candidate.candidate_id, candidate.messages[0].attachments[0])
        await service._execute_attempt(service._claim_pending_job_sync())
        with db.connection() as connection:
            assert connection.execute("SELECT state FROM download_jobs WHERE id = ?", (result.job_id,)).fetchone() == ("FAILED",)
    with db.connection() as connection:
        assert connection.execute("SELECT count(*) FROM artifacts").fetchone() == (0,)
        if stage == "preflight":
            assert connection.execute("SELECT count(*) FROM download_jobs").fetchone() == (0,)


@pytest.mark.asyncio
@pytest.mark.parametrize("readable", [True, False])
async def test_disabled_user_can_use_a_verified_shared_peer_without_another_observation(environment, tmp_path, monkeypatch, readable):
    db, accounts, create, _, client = environment
    a, b = await create("TG_USER", "a"), await create("TG_USER", "b")
    candidate = await source(db, a, file_unique_id="mtproto:42")
    with db.connection() as connection:
        connection.execute("UPDATE external_accounts SET enabled = 0 WHERE account_id = ?", (a,))
    seen = []

    class User:
        async def inspect_message_media(self, chat, mid):
            seen.append(("inspect", current_account_context.get().account_id, chat, mid))
            if not readable:
                raise ProviderConnectionError("TELEGRAM_USER_SOURCE_UNREADABLE", "unreadable")
            return {"file_name": "book.zip", "size_bytes": 4, "file_unique_id": "mtproto:42"}

        async def download_message_media(self, chat, mid, destination, **kwargs):
            seen.append(("download", current_account_context.get().account_id, chat, mid))
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(b"book")
            return 4

    monkeypatch.setattr(accounts, "_user_client", lambda *args, **kwargs: User())
    service = download_service(db, accounts, client, tmp_path)
    if not readable:
        with pytest.raises(DownloadError) as error:
            await service.enqueue_telegram_user_download(candidate.candidate_id, candidate.messages[0].attachments[0])
        assert error.value.code == "TELEGRAM_SOURCE_UNAVAILABLE"
        assert seen == [("inspect", b, -1000000000777, 42)]
    else:
        result = await service.enqueue_telegram_user_download(candidate.candidate_id, candidate.messages[0].attachments[0])
        await service._execute_attempt(service._claim_pending_job_sync())
        assert seen == [("inspect", b, -1000000000777, 42), ("download", b, -1000000000777, 42)]
        with db.connection() as connection:
            assert connection.execute("SELECT state FROM download_jobs WHERE id = ?", (result.job_id,)).fetchone() == ("COMPLETED",)


@pytest.mark.asyncio
async def test_next_user_preflight_waits_for_previous_request_cleanup(environment, monkeypatch):
    _, accounts, create, _, _ = environment
    users = [await create("TG_USER", label) for label in ("a", "b")]
    # Account registry order is the explicit selection order.
    users = [row["account_id"] for row in await accounts.registry.list_accounts()]
    cleaning, release = asyncio.Event(), asyncio.Event()
    seen = []

    class User:
        async def inspect_message_media(self, chat, mid):
            account_id = current_account_context.get().account_id
            seen.append(account_id)
            if account_id == users[0]:
                try:
                    raise ProviderConnectionError("TELEGRAM_USER_SOURCE_UNREADABLE", "unreadable")
                finally:
                    cleaning.set()
                    await release.wait()
            return {"file_name": "book.zip", "size_bytes": 4, "file_unique_id": "mtproto:42"}

    monkeypatch.setattr(accounts, "_user_client", lambda *args, **kwargs: User())
    pending = asyncio.create_task(accounts.resolve_telegram_user_source({"peer_type": "channel", "chat_id": -1000000000777,
        "message_id": 42, "file_name": "book.zip", "size_bytes": 4, "file_unique_id": "AgADKgAH"}))
    try:
        await asyncio.wait_for(cleaning.wait(), 3)
        assert not pending.done() and seen == [users[0]]
    finally:
        release.set()
        await asyncio.wait_for(asyncio.gather(pending, return_exceptions=True), 3)
    assert pending.result()["account_id"] == users[1]
    assert seen == users and not accounts.runtime._leases
