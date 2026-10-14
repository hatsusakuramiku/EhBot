"""Physical account request stops, including metadata work outside the queue."""
import asyncio
import sqlite3
from threading import Event

import httpx
import pytest

from app.connections.accounts import AccountError
from app.connections.lifecycle import AccountLifecycle
from app.connections.runtime import AccountRuntime
from app.downloads.execution import run_sync
from app.downloads.service import DownloadService
from tests.integration.test_account_registry import registry
from tests.integration.test_account_lifecycle import active_account, no_queue_operations, pending


@pytest.mark.asyncio
async def test_nonqueue_http_cleanup_finishes_before_credentials_change(registry):
    account, _ = await active_account(registry)
    lifecycle = AccountLifecycle(registry)
    runtime = AccountRuntime(registry, lifecycle)
    entered, cancelling, release, exited = (asyncio.Event() for _ in range(4))
    async def transport(request):
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelling.set()
            await release.wait()
            exited.set()
    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
        async def metadata_request():
            async with runtime.use(account["account_id"]) as context:
                await client.get("https://example.invalid/metadata", headers={"X-Test-Cookie": context.values["igneous"]})
        request = asyncio.create_task(metadata_request())
        await asyncio.wait_for(entered.wait(), 3)
        receipt = await lifecycle.request_stop(account["account_id"], "edit", expected_revision=account["revision"],
                                               idempotency_key="metadata-edit", fields={"igneous": "next-test-cookie"})
        stopping = asyncio.create_task(lifecycle.process_operations(stop_runtime=runtime.stop, process_queue_operations=no_queue_operations))
        await asyncio.wait_for(cancelling.wait(), 3)
        try:
            assert not stopping.done() and not exited.is_set()
            assert (await registry.context(account["account_id"])).credential_revision == 1
            with pytest.raises(AccountError) as unavailable:
                async with runtime.use(account["account_id"], for_verification=True):
                    pytest.fail("a stop intention must block new verification requests too")
            assert unavailable.value.code == "ACCOUNT_UNAVAILABLE"
        finally:
            release.set()
            await asyncio.gather(request, stopping, return_exceptions=True)
        assert request.cancelled()
        assert stopping.result() == 1 and exited.is_set()
        assert (await lifecycle.get_operation(receipt["operation_id"]))["status"] == "COMPLETED"


@pytest.mark.asyncio
async def test_stop_waits_for_real_thread_and_does_not_stop_other_account(registry):
    first, _ = await active_account(registry, identity="1234")
    second, _ = await active_account(registry, identity="5678")
    lifecycle = AccountLifecycle(registry)
    runtime = AccountRuntime(registry, lifecycle)
    entered, release, exited = Event(), Event(), Event()
    def thread_request():
        entered.set()
        assert release.wait(5)
        exited.set()
    async def first_request():
        async with runtime.use(first["account_id"]):
            await run_sync(thread_request)
    first_task = asyncio.create_task(first_request())
    while not entered.is_set():
        await asyncio.sleep(0.001)
    second_entered = asyncio.Event()
    async def second_request():
        async with runtime.use(second["account_id"]):
            second_entered.set()
            await asyncio.Event().wait()
    second_task = asyncio.create_task(second_request())
    await asyncio.wait_for(second_entered.wait(), 3)
    await lifecycle.request_stop(first["account_id"], "disable", expected_revision=first["revision"], idempotency_key="thread-disable")
    stopping = asyncio.create_task(lifecycle.process_operations(stop_runtime=runtime.stop, process_queue_operations=no_queue_operations))
    try:
        while not first_task.cancelling():
            await asyncio.sleep(0.001)
        assert not stopping.done() and not exited.is_set()
        assert not second_task.cancelling() and not second_task.done()
    finally:
        release.set()
        second_task.cancel()
        await asyncio.gather(first_task, second_task, stopping, return_exceptions=True)
    assert exited.is_set() and stopping.result() == 1


@pytest.mark.asyncio
async def test_nested_leases_and_cancelled_stop_processor_preserve_cleanup_barrier(registry):
    account, _ = await active_account(registry)
    lifecycle = AccountLifecycle(registry)
    runtime = AccountRuntime(registry, lifecycle)
    entered, cancelling, release = (asyncio.Event() for _ in range(3))
    async def request():
        async with runtime.use(account["account_id"]):
            async with runtime.use(account["account_id"]):
                pass
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelling.set()
                await release.wait()
    borrower = asyncio.create_task(request())
    await asyncio.wait_for(entered.wait(), 3)
    receipt = await lifecycle.request_stop(account["account_id"], "disable", expected_revision=account["revision"], idempotency_key="nested-disable")
    stopping = asyncio.create_task(lifecycle.process_operations(stop_runtime=runtime.stop, process_queue_operations=no_queue_operations))
    await asyncio.wait_for(cancelling.wait(), 3)
    stopping.cancel()
    with pytest.raises(asyncio.CancelledError):
        await stopping
    assert (await lifecycle.get_operation(receipt["operation_id"]))["status"] == "PENDING"
    retry = asyncio.create_task(lifecycle.process_operations(stop_runtime=runtime.stop, process_queue_operations=no_queue_operations))
    await asyncio.sleep(0)
    assert not retry.done() and borrower.cancelling() == 1
    release.set()
    await asyncio.gather(borrower, return_exceptions=True)
    assert await asyncio.wait_for(retry, 3) == 1


@pytest.mark.asyncio
async def test_disabled_account_can_be_tested_but_not_used_for_download(registry):
    account = await registry.create("TG_BOT", "New", {"bot_token": "new-test-token"})
    lifecycle = AccountLifecycle(registry)
    runtime = AccountRuntime(registry, lifecycle)
    with pytest.raises(AccountError):
        async with runtime.use(account["account_id"]):
            pytest.fail("disabled account cannot download")
    async with runtime.use(account["account_id"], for_verification=True) as context:
        assert context.values["bot_token"] == "new-test-token"
    assert runtime._leases == {}


@pytest.mark.asyncio
async def test_provider_lease_automatically_binds_real_queue_attempt(registry, tmp_path):
    account, context = await active_account(registry)
    lifecycle = AccountLifecycle(registry)
    runtime = AccountRuntime(registry, lifecycle)
    entered = asyncio.Event()
    async def provider(candidate_id):
        async with runtime.use(account["account_id"]):
            entered.set()
            await asyncio.Event().wait()
    downloads = DownloadService(registry._database, tmp_path, exhentai_download=provider)
    pending(registry._database, "EXHENTAI")
    worker = asyncio.create_task(downloads._process_one())
    await asyncio.wait_for(entered.wait(), 3)
    with registry._database.connection() as c:
        binding = c.execute("SELECT account_id, credential_revision FROM account_execution_bindings").fetchone()
    assert binding == (account["account_id"], context.credential_revision)
    receipt = await lifecycle.request_stop(account["account_id"], "disable", expected_revision=account["revision"], idempotency_key="provider-disable")
    assert await lifecycle.process_operations(stop_runtime=runtime.stop, process_queue_operations=downloads.process_operations) == 1
    assert await worker is True
    assert (await lifecycle.get_operation(receipt["operation_id"]))["status"] == "COMPLETED"


@pytest.mark.asyncio
async def test_nonqueue_late_thread_metadata_transaction_rolls_back_after_stop(registry):
    account, _ = await active_account(registry)
    lifecycle = AccountLifecycle(registry)
    runtime = AccountRuntime(registry, lifecycle)
    with registry._database.connection() as c:
        c.execute("INSERT INTO candidates(id, status) VALUES(1, 'NEEDS_INFO')")
    entered, release = Event(), Event()
    errors = []
    def metadata_write():
        entered.set()
        assert release.wait(5)
        try:
            with registry._database.connection() as c:
                c.execute("UPDATE candidates SET status = 'PENDING_REVIEW' WHERE id = 1")
        except sqlite3.IntegrityError as exc:
            errors.append(str(exc))
    async def request():
        async with runtime.use(account["account_id"]):
            await run_sync(metadata_write)
    task = asyncio.create_task(request())
    while not entered.is_set():
        await asyncio.sleep(0.001)
    await lifecycle.request_stop(account["account_id"], "disable", expected_revision=account["revision"], idempotency_key="late-metadata")
    processing = asyncio.create_task(lifecycle.process_operations(stop_runtime=runtime.stop, process_queue_operations=no_queue_operations))
    release.set()
    await asyncio.gather(task, processing, return_exceptions=True)
    assert processing.result() == 1 and errors == ["ACCOUNT_STALE_EXECUTION"]
    with registry._database.connection() as c:
        assert c.execute("SELECT status FROM candidates WHERE id = 1").fetchone()[0] == "NEEDS_INFO"
