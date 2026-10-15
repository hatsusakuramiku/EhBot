"""Account replacement keeps one logical task and never overlaps attempts."""
import asyncio
from contextlib import asynccontextmanager
from dataclasses import replace
import json
import sqlite3

import httpx
import pytest

from app.connections.models import ProviderConnectionError
from app.connections.runtime import current_account_context
from app.candidates.entries import load_message
from app.downloads.service import DownloadError
from app.downloads.execution import execution_scope
from tests.integration.test_source_account_downloads import environment, source, download_service


@pytest.mark.asyncio
@pytest.mark.parametrize("recovery", ["same-process", "restart", "upgrade"])
async def test_failed_user_download_replaces_account_in_same_task(environment, tmp_path, monkeypatch, recovery):
    db, accounts, create, _, client = environment
    a, b = await create("TG_USER", "a"), await create("TG_USER", "b")
    await source(db, a, file_unique_id="mtproto:42", chat_id=111, peer_type="private")
    candidate = await source(db, b, file_unique_id="mtproto:42", chat_id=222, peer_type="private")
    seen, paths = [], []

    class User:
        async def inspect_message_media(self, chat, mid):
            assert current_account_context.get().account_id == b and (chat, mid) == (222, 42)
            return {"file_name": "book.zip", "size_bytes": 4, "file_unique_id": "mtproto:42"}

        async def download_message_media(self, chat, mid, destination, **kwargs):
            account = current_account_context.get().account_id
            seen.append(account)
            paths.append(destination)
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(b"part" if account == a else b"book")
            if account == a:
                raise ProviderConnectionError("TELEGRAM_USER_UNREACHABLE", "controlled failure")
            return 4

    monkeypatch.setattr(accounts, "_user_client", lambda *args, **kwargs: User())
    service = download_service(db, accounts, client, tmp_path)
    result = await service.enqueue_telegram_user_download(candidate.candidate_id, candidate.messages[0].attachments[0])
    assert await service._process_one()
    assert (await service.get_task(result.task_id))["status"] == "STOPPING"
    if recovery == "upgrade":
        # Model the real 034 guard and upgrade it with a pending replacement.
        with db.connection() as connection:
            connection.execute("DROP TRIGGER queue_stopping_guard")
            connection.execute("CREATE TRIGGER queue_stopping_guard BEFORE UPDATE OF state ON download_jobs "
                               "WHEN OLD.state = 'STOPPING' AND NEW.state NOT IN ('STOPPING', 'CANCELLED') "
                               "BEGIN SELECT RAISE(ABORT, 'QUEUE_STOP_NOT_CONFIRMED'); END")
            connection.execute("DELETE FROM schema_migrations WHERE version = 35")
        await db.initialize()
        await db.initialize()
    with pytest.raises(sqlite3.IntegrityError, match="QUEUE_STOP_NOT_CONFIRMED"):
        with db.connection() as connection:
            connection.execute("UPDATE download_jobs SET state = 'PENDING' WHERE id = ?", (result.job_id,))
    if recovery != "same-process":
        service = download_service(db, accounts, client, tmp_path)
    assert await service.process_operations() == 1
    assert await service._process_one()
    task = await service.get_task(result.task_id)
    assert task["status"] == "COMPLETED" and seen == [a, b]
    assert [attempt["status"] for attempt in task["attempts"]] == ["FAILED", "COMPLETED"]
    assert task["attempts"][0]["error_code"] == "TELEGRAM_USER_UNREACHABLE"
    assert len({attempt["attempt_id"] for attempt in task["attempts"]}) == 2
    assert paths[0].parent != paths[1].parent and paths[0].read_bytes() == b"part"
    old_attempt = task["attempts"][0]
    with execution_scope(db.path, {"task_id": result.task_id, "attempt_id": old_attempt["attempt_id"],
                                   "generation": old_attempt["generation"]}):
        with pytest.raises(sqlite3.IntegrityError, match="QUEUE_STALE_EXECUTION"):
            service._mark_job_completed_sync(result.job_id)
    with db.connection() as connection:
        assert connection.execute("SELECT count(*) FROM queue_tasks WHERE legacy_job_id = ?", (result.job_id,)).fetchone() == (1,)
        details = json.loads(connection.execute("SELECT details_json FROM download_jobs WHERE id = ?", (result.job_id,)).fetchone()[0])
        assert details["origin_account_id"] == a and details["account_id"] == b
    assert await service.process_operations() == 0


@pytest.mark.asyncio
async def test_replacement_waits_for_old_account_scope_cleanup(environment, tmp_path, monkeypatch):
    db, accounts, create, _, client = environment
    a, b = await create("TG_USER", "a"), await create("TG_USER", "b")
    await source(db, a, file_unique_id="mtproto:42", chat_id=111, peer_type="private")
    candidate = await source(db, b, file_unique_id="mtproto:42", chat_id=222, peer_type="private")
    cleaning, release = asyncio.Event(), asyncio.Event()
    seen = []

    class User:
        async def inspect_message_media(self, chat, mid):
            seen.append("preflight-b")
            return {"file_name": "book.zip", "size_bytes": 4, "file_unique_id": "mtproto:42"}

        async def download_message_media(self, *args, **kwargs):
            raise ProviderConnectionError("TELEGRAM_USER_UNREACHABLE", "controlled failure")

    monkeypatch.setattr(accounts, "_user_client", lambda *args, **kwargs: User())
    service = download_service(db, accounts, client, tmp_path)
    original_scope = service._account_scope

    @asynccontextmanager
    async def scope(kind, **params):
        async with original_scope(kind, **params) as context:
            try:
                yield context
            finally:
                if context.account_id == a:
                    cleaning.set()
                    await release.wait()

    service._account_scope = scope
    result = await service.enqueue_telegram_user_download(candidate.candidate_id, candidate.messages[0].attachments[0])
    worker = asyncio.create_task(service._process_one())
    operations = None
    try:
        await asyncio.wait_for(cleaning.wait(), 3)
        assert (await service.get_task(result.task_id))["status"] == "STOPPING"
        operations = asyncio.create_task(service.process_operations())
        # The operation must reach its physical wait without starting B.
        for _ in range(10):
            await asyncio.sleep(0)
        assert not worker.done() and not operations.done() and not seen
        with pytest.raises(DownloadError) as error:
            await service.enqueue_telegraph_download(candidate.candidate_id)
        assert error.value.code == "WORK_SLOT_OCCUPIED"
    finally:
        release.set()
        await asyncio.wait_for(asyncio.gather(worker, *([operations] if operations else []), return_exceptions=True), 5)
    assert operations.result() == 1 and seen == ["preflight-b"]
    assert (await service.get_task(result.task_id))["status"] == "PENDING"


@pytest.mark.asyncio
@pytest.mark.parametrize("same_document", [True, False])
async def test_bot_replacement_uses_only_its_own_observed_file_id(environment, tmp_path, same_document):
    db, accounts, create, _, _ = environment
    a, b = await create("TG_BOT", "a"), await create("TG_BOT", "b")
    await source(db, a, file_id="a-file", file_unique_id="AgADKgAH")
    candidate = await source(db, b, file_id="b-file", file_unique_id="AgADKgAH" if same_document else "AgADKQAH")
    seen = []

    async def handler(request):
        seen.append((request.url.path, request.url.params.get("file_id")))
        if request.url.path == "/botfixture-a/getFile":
            assert request.url.params["file_id"] == "a-file"
            return httpx.Response(401, json={"ok": False, "description": "Unauthorized"})
        if request.url.path == "/botfixture-b/getFile":
            assert request.url.params["file_id"] == "b-file"
            return httpx.Response(200, json={"ok": True, "result": {
                "file_id": "b-file", "file_unique_id": "AgADKgAH", "file_path": "book.zip", "file_size": 4}})
        assert request.url.path == "/file/botfixture-b/book.zip"
        return httpx.Response(200, content=b"book")

    async with httpx.AsyncClient(base_url="https://api.telegram.org", transport=httpx.MockTransport(handler)) as client:
        service = download_service(db, accounts, client, tmp_path)
        result = await service.enqueue_telegram_download(candidate.candidate_id, candidate.messages[0].attachments[0])
        await service._process_one()
        assert (await service.get_task(result.task_id))["status"] == "STOPPING"
        assert await service.process_operations() == 1
        if same_document:
            assert await service._process_one()
        task = await service.get_task(result.task_id)
        assert task["status"] == ("COMPLETED" if same_document else "FAILED")
        assert len(task["attempts"]) == (2 if same_document else 1)
        assert seen == ([("/botfixture-a/getFile", "a-file"), ("/botfixture-b/getFile", "b-file"),
                         ("/file/botfixture-b/book.zip", None)] if same_document else [("/botfixture-a/getFile", "a-file")])


@pytest.mark.asyncio
async def test_bot_file_identity_change_never_downloads_or_publishes_bytes(environment, tmp_path):
    db, accounts, create, _, _ = environment
    account = await create("TG_BOT", "a")
    candidate = await source(db, account, file_id="a-file", file_unique_id="AgADKgAH")
    seen = []

    async def handler(request):
        seen.append(request.url.path)
        assert request.url.path == "/botfixture-a/getFile"
        return httpx.Response(200, json={"ok": True, "result": {
            "file_id": "a-file", "file_unique_id": "AgADKQAH", "file_path": "wrong.zip", "file_size": 4}})

    async with httpx.AsyncClient(base_url="https://api.telegram.org", transport=httpx.MockTransport(handler)) as client:
        service = download_service(db, accounts, client, tmp_path)
        result = await service.enqueue_telegram_download(candidate.candidate_id, candidate.messages[0].attachments[0])
        await service._process_one()
        assert await service.process_operations() == 1
        task = await service.get_task(result.task_id)
        assert task["status"] == "FAILED" and len(task["attempts"]) == 1
        assert task["attempts"][0]["error_code"] == "TELEGRAM_FILE_IDENTITY_CHANGED"
    assert seen == ["/botfixture-a/getFile"]
    with db.connection() as connection:
        assert connection.execute("SELECT count(*) FROM artifacts").fetchone() == (0,)


@pytest.mark.asyncio
async def test_failing_pool_is_finite_across_restart_and_does_not_add_new_accounts(environment, tmp_path, monkeypatch):
    db, accounts, create, _, client = environment
    a, b = await create("TG_USER", "a"), await create("TG_USER", "b")
    await source(db, a, file_unique_id="mtproto:42", chat_id=111, peer_type="private")
    candidate = await source(db, b, file_unique_id="mtproto:42", chat_id=222, peer_type="private")
    seen = []

    class User:
        async def inspect_message_media(self, *args):
            return {"file_name": "book.zip", "size_bytes": 4, "file_unique_id": "mtproto:42"}

        async def download_message_media(self, *args, **kwargs):
            seen.append(current_account_context.get().account_id)
            raise ProviderConnectionError("TELEGRAM_USER_UNREACHABLE", "controlled failure")

    monkeypatch.setattr(accounts, "_user_client", lambda *args, **kwargs: User())
    service = download_service(db, accounts, client, tmp_path)
    result = await service.enqueue_telegram_user_download(candidate.candidate_id, candidate.messages[0].attachments[0])
    await service._process_one()
    c = await create("TG_USER", "c")
    await source(db, c, file_unique_id="mtproto:42", chat_id=333, peer_type="private")
    for _ in range(2):
        service = download_service(db, accounts, client, tmp_path)
        assert await service.process_operations() == 1
        await service._process_one()
    task = await service.get_task(result.task_id)
    assert task["status"] == "FAILED" and seen == [a, b]
    assert len(task["attempts"]) == 2 and all(attempt["status"] == "FAILED" for attempt in task["attempts"])
    assert await service.process_operations() == 0
    assert not await service._process_one()


@pytest.mark.asyncio
@pytest.mark.parametrize("during_preflight", [False, True])
async def test_operator_cancel_supersedes_account_replacement(environment, tmp_path, monkeypatch, during_preflight):
    db, accounts, create, _, client = environment
    a, b = await create("TG_USER", "a"), await create("TG_USER", "b")
    await source(db, a, file_unique_id="mtproto:42", chat_id=111, peer_type="private")
    candidate = await source(db, b, file_unique_id="mtproto:42", chat_id=222, peer_type="private")
    entered, release = asyncio.Event(), asyncio.Event()
    seen = []

    class User:
        async def inspect_message_media(self, *args):
            entered.set()
            await release.wait()
            return {"file_name": "book.zip", "size_bytes": 4, "file_unique_id": "mtproto:42"}

        async def download_message_media(self, *args, **kwargs):
            seen.append(current_account_context.get().account_id)
            raise ProviderConnectionError("TELEGRAM_USER_UNREACHABLE", "controlled failure")

    monkeypatch.setattr(accounts, "_user_client", lambda *args, **kwargs: User())
    service = download_service(db, accounts, client, tmp_path)
    result = await service.enqueue_telegram_user_download(candidate.candidate_id, candidate.messages[0].attachments[0])
    await service._process_one()
    processing = None
    try:
        if during_preflight:
            processing = asyncio.create_task(service.process_operations())
            await asyncio.wait_for(entered.wait(), 3)
        task = await service.get_task(result.task_id)
        receipt = await service.command(result.job_id, "cancel", task_id=result.task_id,
            expected_revision=task["revision"], idempotency_key="cancel-replacement")
        assert receipt["state"] == "STOPPING"
    finally:
        release.set()
        if processing is not None:
            await asyncio.wait_for(processing, 3)
    if processing is None:
        assert await service.process_operations() == 1
    assert (await service.get_task(result.task_id))["status"] == "CANCELLED"
    assert not await service._process_one() and seen == [a]


@pytest.mark.asyncio
async def test_replacement_commit_failure_retains_stop_and_old_history(environment, tmp_path, monkeypatch):
    db, accounts, create, _, client = environment
    a, b = await create("TG_USER", "a"), await create("TG_USER", "b")
    await source(db, a, file_unique_id="mtproto:42", chat_id=111, peer_type="private")
    candidate = await source(db, b, file_unique_id="mtproto:42", chat_id=222, peer_type="private")

    class User:
        async def inspect_message_media(self, *args):
            return {"file_name": "book.zip", "size_bytes": 4, "file_unique_id": "mtproto:42"}

        async def download_message_media(self, *args, **kwargs):
            raise ProviderConnectionError("TELEGRAM_USER_UNREACHABLE", "controlled failure")

    monkeypatch.setattr(accounts, "_user_client", lambda *args, **kwargs: User())
    service = download_service(db, accounts, client, tmp_path)
    result = await service.enqueue_telegram_user_download(candidate.candidate_id, candidate.messages[0].attachments[0])
    await service._process_one()
    with db.connection() as connection:
        connection.execute("CREATE TRIGGER injected_replacement_failure BEFORE UPDATE OF state ON download_jobs "
                           "WHEN OLD.state = 'STOPPING' AND NEW.state = 'PENDING' BEGIN SELECT RAISE(ABORT, 'injected'); END")
    assert await service.process_operations() == 0
    task = await service.get_task(result.task_id)
    assert task["status"] == "STOPPING" and task["operations"][0]["status"] == "PENDING"
    assert task["attempts"][0]["error_code"] == "TELEGRAM_USER_UNREACHABLE"
    with db.connection() as connection:
        connection.execute("DROP TRIGGER injected_replacement_failure")
    assert await service.process_operations() == 1
    assert (await service.get_task(result.task_id))["status"] == "PENDING"


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["disable", "delete", "edit"])
async def test_account_stop_waits_then_replaces_user_without_restarting_cancelled_work(environment, tmp_path, monkeypatch, action):
    db, accounts, create, _, client = environment
    a, b = await create("TG_USER", "a"), await create("TG_USER", "b")
    await source(db, a, file_unique_id="mtproto:42", chat_id=111, peer_type="private")
    candidate = await source(db, b, file_unique_id="mtproto:42", chat_id=222, peer_type="private")
    entered, cleaning, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
    seen = []

    class User:
        async def inspect_message_media(self, chat, mid):
            assert release.is_set()
            return {"file_name": "book.zip", "size_bytes": 4, "file_unique_id": "mtproto:42"}

        async def download_message_media(self, chat, mid, destination, **kwargs):
            account = current_account_context.get().account_id
            seen.append((account, chat, mid))
            if account == a:
                entered.set()
                try:
                    await asyncio.Event().wait()
                finally:
                    cleaning.set()
                    await release.wait()
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(b"book")
            return 4

    monkeypatch.setattr(accounts, "_user_client", lambda *args, **kwargs: User())
    service = download_service(db, accounts, client, tmp_path)
    accounts._process_queue_operations = service.process_operations
    result = await service.enqueue_telegram_user_download(candidate.candidate_id, candidate.messages[0].attachments[0])
    worker = asyncio.create_task(service._process_one())
    stopping = None
    try:
        await asyncio.wait_for(entered.wait(), 3)
        row = await accounts.registry.get(a)
        params = {"fields": {"api_hash": "changed-test-hash"}} if action == "edit" else {}
        receipt = await accounts.stop_account(a, action, expected_revision=row["revision"], idempotency_key="account-stop", **params)
        stopping = asyncio.create_task(accounts.process_operations())
        await asyncio.wait_for(cleaning.wait(), 3)
        assert not stopping.done() and not worker.done()
        assert (await service.get_task(result.task_id))["status"] == "STOPPING"
        assert seen == [(a, 111, 42)]
    finally:
        release.set()
        await asyncio.wait_for(asyncio.gather(worker, *([stopping] if stopping else []), return_exceptions=True), 5)
    assert stopping.result() == 1
    assert (await accounts.lifecycle.get_operation(receipt["operation_id"]))["status"] == "COMPLETED"
    assert (await service.get_task(result.task_id))["status"] == "PENDING"
    assert await service._process_one()
    task = await service.get_task(result.task_id)
    assert task["status"] == "COMPLETED"
    assert [attempt["status"] for attempt in task["attempts"]] == ["CANCELLED", "COMPLETED"]
    assert seen == [(a, 111, 42), (b, 222, 42)]


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["retry", "restart"])
async def test_explicit_retry_resets_pool_and_prefers_recovered_origin(environment, tmp_path, monkeypatch, action):
    db, accounts, create, _, client = environment
    a, b = await create("TG_USER", "a"), await create("TG_USER", "b")
    await source(db, a, file_unique_id="mtproto:42", chat_id=111, peer_type="private")
    candidate = await source(db, b, file_unique_id="mtproto:42", chat_id=222, peer_type="private")
    recovered = False
    seen = []

    class User:
        async def inspect_message_media(self, *args):
            return {"file_name": "book.zip", "size_bytes": 4, "file_unique_id": "mtproto:42"}

        async def download_message_media(self, chat, mid, destination, **kwargs):
            account = current_account_context.get().account_id
            seen.append((account, chat))
            assert kwargs == {"expected_file_unique_id": "mtproto:42"}
            if not recovered:
                raise ProviderConnectionError("TELEGRAM_USER_UNREACHABLE", "controlled failure")
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(b"book")
            return 4

    monkeypatch.setattr(accounts, "_user_client", lambda *args, **kwargs: User())
    service = download_service(db, accounts, client, tmp_path)
    result = await service.enqueue_telegram_user_download(candidate.candidate_id, candidate.messages[0].attachments[0])
    for _ in range(2):
        await service._process_one()
        await service.process_operations()
    old = await service.get_task(result.task_id)
    assert old["status"] == "FAILED" and seen == [(a, 111), (b, 222)]
    recovered = True
    receipt = await service.command(result.job_id, action, task_id=result.task_id,
        expected_revision=old["revision"], idempotency_key="fresh-retry")
    assert receipt["task_id"] != result.task_id
    after = await service.get_task(result.task_id)
    # A retry supersedes the old downstream intent; execution history stays fixed.
    expected = {**old, "pack_intent": {**old["pack_intent"], "status": "CANCELLED"}}
    assert after == expected
    assert json.loads((await service.get_task(result.task_id))["details_json"])["account_id"] == b
    with db.connection() as connection:
        details = json.loads(connection.execute("SELECT details_json FROM download_jobs WHERE id = ?", (result.job_id,)).fetchone()[0])
        assert details["account_id"] == a
        assert "account_pool" not in details and "attempted_accounts" not in details
        assert not any(key.startswith("download_") for key in details)
    assert await service._process_one()
    assert seen == [(a, 111), (b, 222), (a, 111)]
    assert (await service.get_task(receipt["task_id"]))["status"] == "COMPLETED"
    assert await service.get_task(result.task_id) == expected
    assert [attempt["status"] for attempt in (await service.get_task(result.task_id))["attempts"]] == ["FAILED", "FAILED"]


@pytest.mark.asyncio
async def test_retry_of_bot_origin_user_route_keeps_verified_user_coordinates(environment, tmp_path, monkeypatch):
    db, accounts, create, _, client = environment
    bot, user = await create("TG_BOT", "bot"), await create("TG_USER", "user")
    await source(db, bot, file_unique_id="AgADKgAH", chat_id=111, peer_type="private")
    candidate = await source(db, user, file_unique_id="mtproto:42", chat_id=222, peer_type="private")
    recovered = False
    seen = []

    class User:
        async def inspect_message_media(self, chat, mid):
            assert (chat, mid) == (222, 42)
            return {"file_name": "book.zip", "size_bytes": 4, "file_unique_id": "mtproto:42"}

        async def download_message_media(self, chat, mid, destination, **kwargs):
            seen.append((current_account_context.get().account_id, chat, mid, kwargs))
            if not recovered:
                raise ProviderConnectionError("TELEGRAM_USER_UNREACHABLE", "controlled failure")
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(b"book")
            return 4

    monkeypatch.setattr(accounts, "_user_client", lambda *args, **kwargs: User())
    service = download_service(db, accounts, client, tmp_path)
    result = await service.enqueue_telegram_user_download(candidate.candidate_id, candidate.messages[0].attachments[0])
    await service._process_one()
    await service.process_operations()
    assert (await service.get_task(result.task_id))["status"] == "FAILED"
    recovered = True
    assert await service.retry_job(result.job_id) == "PENDING"
    await service._process_one()
    assert seen == [(user, 222, 42, {"expected_file_unique_id": "mtproto:42"})] * 2
    with db.connection() as connection:
        assert connection.execute("SELECT state FROM download_jobs WHERE id = ?", (result.job_id,)).fetchone() == ("COMPLETED",)


@pytest.mark.asyncio
async def test_stopping_previous_account_does_not_revoke_pending_successor(environment, tmp_path, monkeypatch):
    db, accounts, create, _, client = environment
    a, b = await create("TG_USER", "a"), await create("TG_USER", "b")
    await source(db, a, file_unique_id="mtproto:42", chat_id=111, peer_type="private")
    candidate = await source(db, b, file_unique_id="mtproto:42", chat_id=222, peer_type="private")
    seen = []

    class User:
        async def inspect_message_media(self, *args):
            return {"file_name": "book.zip", "size_bytes": 4, "file_unique_id": "mtproto:42"}

        async def download_message_media(self, chat, mid, destination, **kwargs):
            account = current_account_context.get().account_id
            seen.append(account)
            if account == a:
                raise ProviderConnectionError("TELEGRAM_USER_UNREACHABLE", "controlled failure")
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(b"book")
            return 4

    monkeypatch.setattr(accounts, "_user_client", lambda *args, **kwargs: User())
    service = download_service(db, accounts, client, tmp_path)
    accounts._process_queue_operations = service.process_operations
    result = await service.enqueue_telegram_user_download(candidate.candidate_id, candidate.messages[0].attachments[0])
    await service._process_one()
    await service.process_operations()
    before = await service.get_task(result.task_id)
    assert before["status"] == "PENDING"
    account = await accounts.registry.get(a)
    await accounts.stop_account(a, "disable", expected_revision=account["revision"], idempotency_key="old-account-stop")
    assert await accounts.process_operations() == 1
    after = await service.get_task(result.task_id)
    assert after["status"] == "PENDING" and after["revision"] == before["revision"]
    assert await service._process_one()
    assert (await service.get_task(result.task_id))["status"] == "COMPLETED" and seen == [a, b]


@pytest.mark.asyncio
async def test_changed_origin_prevents_replacement_preflight(environment, tmp_path, monkeypatch):
    db, accounts, create, _, client = environment
    a, b = await create("TG_USER", "a"), await create("TG_USER", "b")
    await source(db, a, file_unique_id="mtproto:42", chat_id=111, peer_type="private")
    candidate = await source(db, b, file_unique_id="mtproto:42", chat_id=222, peer_type="private")
    probes = []

    class User:
        async def inspect_message_media(self, *args):
            probes.append(current_account_context.get().account_id)
            return {"file_name": "book.zip", "size_bytes": 4, "file_unique_id": "mtproto:42"}

        async def download_message_media(self, *args, **kwargs):
            raise ProviderConnectionError("TELEGRAM_USER_UNREACHABLE", "controlled failure")

    monkeypatch.setattr(accounts, "_user_client", lambda *args, **kwargs: User())
    service = download_service(db, accounts, client, tmp_path)
    result = await service.enqueue_telegram_user_download(candidate.candidate_id, candidate.messages[0].attachments[0])
    await service._process_one()
    with db.connection() as connection:
        connection.execute("UPDATE source_messages SET observation_revision = observation_revision + 1 WHERE id = ?",
                           (candidate.messages[0].observation_id,))
    assert await service.process_operations() == 1
    assert (await service.get_task(result.task_id))["status"] == "FAILED"
    assert probes == []


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel_order", ["before-stop", "after-stop", "no-cancel"])
async def test_interrupted_stop_processor_preserves_cleanup_and_operator_cancel(environment, tmp_path, monkeypatch, cancel_order):
    db, accounts, create, _, client = environment
    a, b = await create("TG_USER", "a"), await create("TG_USER", "b")
    await source(db, a, file_unique_id="mtproto:42", chat_id=111, peer_type="private")
    candidate = await source(db, b, file_unique_id="mtproto:42", chat_id=222, peer_type="private")
    entered, cleaning, release, exited = (asyncio.Event() for _ in range(4))
    seen = []

    class User:
        async def inspect_message_media(self, *args):
            assert exited.is_set()
            seen.append("probe-b")
            return {"file_name": "book.zip", "size_bytes": 4, "file_unique_id": "mtproto:42"}

        async def download_message_media(self, *args, **kwargs):
            seen.append("download-a")
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                cleaning.set()
                await release.wait()
                exited.set()

    monkeypatch.setattr(accounts, "_user_client", lambda *args, **kwargs: User())
    service = download_service(db, accounts, client, tmp_path)
    accounts._process_queue_operations = service.process_operations
    result = await service.enqueue_telegram_user_download(candidate.candidate_id, candidate.messages[0].attachments[0])
    worker = asyncio.create_task(service._process_one())
    processing = resumed = None

    async def cancel():
        task = await service.get_task(result.task_id)
        await service.command(result.job_id, "cancel", task_id=result.task_id,
                              expected_revision=task["revision"], idempotency_key="operator-cancel")

    try:
        await asyncio.wait_for(entered.wait(), 3)
        if cancel_order == "before-stop":
            await cancel()
        account = await accounts.registry.get(a)
        receipt = await accounts.stop_account(a, "disable", expected_revision=account["revision"], idempotency_key="stop-a")
        if cancel_order == "after-stop":
            await cancel()
        # Exercise the queue coordinator directly, including its shield around
        # producer cleanup; account-runtime stopping alone does not prove it.
        processing = asyncio.create_task(service.process_operations())
        await asyncio.wait_for(cleaning.wait(), 3)
        processing.cancel()
        with pytest.raises(asyncio.CancelledError):
            await processing
        assert not exited.is_set() and not worker.done()
        waiting = asyncio.Event()
        original_stop = service.stop_attempt

        async def stop_again(task_id):
            waiting.set()
            await original_stop(task_id)

        monkeypatch.setattr(service, "stop_attempt", stop_again)
        resumed = asyncio.create_task(service.process_operations())
        await asyncio.wait_for(waiting.wait(), 3)
        assert not resumed.done() and not exited.is_set()
        assert (await service.get_task(result.task_id))["status"] == "STOPPING"
        assert seen == ["download-a"]
    finally:
        release.set()
        await asyncio.wait_for(asyncio.gather(worker, *[t for t in (processing, resumed) if t is not None],
                                              return_exceptions=True), 5)
    assert resumed.result() == 1 and exited.is_set()
    assert await accounts.process_operations() == 1
    assert (await accounts.lifecycle.get_operation(receipt["operation_id"]))["status"] == "COMPLETED"
    task = await service.get_task(result.task_id)
    assert task["status"] == ("PENDING" if cancel_order == "no-cancel" else "CANCELLED")
    assert task["attempts"][0]["status"] == "CANCELLED"
    assert seen == (["download-a", "probe-b"] if cancel_order == "no-cancel" else ["download-a"])


@pytest.mark.asyncio
async def test_bot_disable_drains_stream_before_using_other_bots_own_file_id(environment, tmp_path):
    db, accounts, create, _, _ = environment
    a, b = await create("TG_BOT", "a"), await create("TG_BOT", "b")
    await source(db, a, file_id="a-file", file_unique_id="AgADKgAH")
    candidate = await source(db, b, file_id="b-file", file_unique_id="AgADKgAH")
    entered, cleaning, release, exited = (asyncio.Event() for _ in range(4))
    seen = []

    class Stream(httpx.AsyncByteStream):
        async def __aiter__(self):
            entered.set()
            try:
                yield b"part"
                await asyncio.Event().wait()
            finally:
                cleaning.set()
                await release.wait()
                exited.set()

    async def handler(request):
        seen.append((request.url.path, request.url.params.get("file_id")))
        if request.url.path.endswith("getFile"):
            own_file = "a-file" if request.url.path == "/botfixture-a/getFile" else "b-file"
            assert request.url.params["file_id"] == own_file
            if own_file == "b-file":
                assert exited.is_set()
            return httpx.Response(200, json={"ok": True, "result": {
                "file_id": own_file, "file_unique_id": "AgADKgAH", "file_path": "book.zip", "file_size": 4}})
        if request.url.path == "/file/botfixture-a/book.zip":
            return httpx.Response(200, stream=Stream())
        assert request.url.path == "/file/botfixture-b/book.zip" and exited.is_set()
        return httpx.Response(200, content=b"book")

    async with httpx.AsyncClient(base_url="https://api.telegram.org", transport=httpx.MockTransport(handler)) as client:
        service = download_service(db, accounts, client, tmp_path)
        accounts._process_queue_operations = service.process_operations
        result = await service.enqueue_telegram_download(candidate.candidate_id, candidate.messages[0].attachments[0])
        worker = asyncio.create_task(service._process_one())
        stopping = None
        try:
            await asyncio.wait_for(entered.wait(), 3)
            account = await accounts.registry.get(a)
            await accounts.stop_account(a, "disable", expected_revision=account["revision"], idempotency_key="stop-bot-a")
            stopping = asyncio.create_task(accounts.process_operations())
            await asyncio.wait_for(cleaning.wait(), 3)
            assert not stopping.done() and not exited.is_set()
            assert (await service.get_task(result.task_id))["status"] == "STOPPING"
            assert len(seen) == 2
        finally:
            release.set()
            await asyncio.wait_for(asyncio.gather(worker, *([stopping] if stopping else []), return_exceptions=True), 5)
        assert stopping.result() == 1
        assert await service._process_one()
        task = await service.get_task(result.task_id)
        assert task["status"] == "COMPLETED"
        assert [attempt["status"] for attempt in task["attempts"]] == ["CANCELLED", "COMPLETED"]
    assert seen == [("/botfixture-a/getFile", "a-file"), ("/file/botfixture-a/book.zip", None),
                    ("/botfixture-b/getFile", "b-file"), ("/file/botfixture-b/book.zip", None)]


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["edit", "deactivate"])
async def test_changing_selected_fallback_observation_physically_stops_transfer(environment, tmp_path, monkeypatch, change):
    db, accounts, create, _, client = environment
    a, b = await create("TG_USER", "a"), await create("TG_USER", "b")
    await source(db, a, file_unique_id="mtproto:42", chat_id=111, peer_type="private")
    candidate = await source(db, b, file_unique_id="mtproto:42", chat_id=222, peer_type="private")
    fallback_id = candidate.messages[1].observation_id
    with db.connection() as connection:
        connection.execute("UPDATE external_accounts SET enabled = 0 WHERE account_id = ?", (a,))
        fallback_message = load_message(connection, fallback_id)
    entered, cleaning, release = (asyncio.Event() for _ in range(3))

    class User:
        async def inspect_message_media(self, *args):
            return {"file_name": "book.zip", "size_bytes": 4, "file_unique_id": "mtproto:42"}

        async def download_message_media(self, chat, mid, destination, **kwargs):
            assert current_account_context.get().account_id == b and (chat, mid) == (222, 42)
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                cleaning.set()
                await release.wait()

    monkeypatch.setattr(accounts, "_user_client", lambda *args, **kwargs: User())
    service = download_service(db, accounts, client, tmp_path)
    result = await service.enqueue_telegram_user_download(candidate.candidate_id, candidate.messages[0].attachments[0])
    worker = asyncio.create_task(service._process_one())
    stopping = None
    try:
        await asyncio.wait_for(entered.wait(), 3)
        if change == "edit":
            await db.save_candidate_message(None, replace(fallback_message, is_edit=True, message_text="changed fallback"))
        else:
            await db.deactivate_candidate_message(222, 42, account_id=b)
        assert (await service.get_task(result.task_id))["status"] == "STOPPING"
        stopping = asyncio.create_task(service.process_operations())
        await asyncio.wait_for(cleaning.wait(), 3)
        assert not worker.done() and not stopping.done()
        with db.connection() as connection:
            assert connection.execute("SELECT status FROM ingest_observation_changes").fetchone() == ("PENDING",)
    finally:
        release.set()
        if stopping is None:
            worker.cancel()
        await asyncio.wait_for(asyncio.gather(worker, *([stopping] if stopping else []), return_exceptions=True), 5)
    assert (await service.get_task(result.task_id))["status"] == "CANCELLED"
    with db.connection() as connection:
        assert connection.execute("SELECT count(*) FROM artifacts").fetchone() == (0,)
        assert connection.execute("SELECT status FROM ingest_observation_changes").fetchone() == ("COMPLETED",)
        assert connection.execute("SELECT observation_revision FROM source_messages WHERE id = ?",
                                  (candidate.messages[0].observation_id,)).fetchone() == (candidate.messages[0].observation_revision,)
