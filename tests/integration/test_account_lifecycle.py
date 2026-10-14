"""Account revisions, durable stops and real queue cancellation boundaries."""
import asyncio
from dataclasses import replace
import json
import sqlite3
from uuid import uuid4

import pytest

from app.connections.accounts import AccountError, AccountRegistry
from app.connections.lifecycle import AccountLifecycle
from app.downloads.execution import Execution, current_execution
from app.downloads.service import DownloadService
from tests.integration.test_account_registry import registry
from tests.integration.test_running_removal import archived


def pending(database, provider="TELEGRAM", candidate_id=1):
    with database.connection() as c:
        c.execute("INSERT OR IGNORE INTO candidates(id, status) VALUES(?, 'APPROVED')", (candidate_id,))
        return c.execute(
            "INSERT INTO download_jobs(candidate_id, idempotency_key, provider, state) VALUES(?, ?, ?, 'PENDING')",
            (candidate_id, uuid4().hex, provider),
        ).lastrowid


async def active_account(registry, kind="EH", identity="1234"):
    fields = {"ipb_member_id": identity, "ipb_pass_hash": "old-test-cookie", "igneous": "old-test-igneous"} if kind == "EH" else {"bot_token": "old-test-token"}
    account = await registry.create(kind, "Account", fields)
    verified = await registry.record_identity(account["account_id"], 1, identity, "Public account")
    enabled = await registry.enable(account["account_id"], expected_revision=verified["revision"])
    return enabled, await registry.context(account["account_id"])


async def stop_runtime(*args):
    return True


async def no_queue_operations():
    return 0


@pytest.mark.asyncio
async def test_enable_requires_verification_of_current_version_and_unique_identity(registry):
    first = await registry.create("TG_BOT", "First", {"bot_token": "first-test-token"})
    with pytest.raises(AccountError) as unverified:
        await registry.enable(first["account_id"], expected_revision=first["revision"])
    assert unverified.value.code == "ACCOUNT_NOT_VERIFIED"
    verified = await registry.record_identity(first["account_id"], 1, "1234", "Bot")
    active = await registry.enable(first["account_id"], expected_revision=verified["revision"])
    assert await registry.enable(first["account_id"], expected_revision=active["revision"]) == active
    second = await registry.create("TG_BOT", "Second", {"bot_token": "second-test-token"})
    second = await registry.record_identity(second["account_id"], 1, "1234", "Same bot")
    with pytest.raises(AccountError) as duplicate:
        await registry.enable(second["account_id"], expected_revision=second["revision"])
    assert duplicate.value.code == "ACCOUNT_IDENTITY_IN_USE"
    assert await registry.get(second["account_id"]) == second
    changed = await registry.edit(second["account_id"], expected_revision=second["revision"], fields={"bot_token": "replacement-test-token"})
    assert not changed["verified"] and changed["remote_identity"] == "1234"
    with pytest.raises(AccountError) as stale_validation:
        await registry.enable(second["account_id"], expected_revision=changed["revision"])
    assert stale_validation.value.code == "ACCOUNT_NOT_VERIFIED"
    assert (await registry.record_identity(second["account_id"], 2, "1234", "Same bot"))["verified"]


@pytest.mark.asyncio
async def test_pending_edit_preserves_old_secrets_until_runtime_stops_and_blocks_bypasses(registry):
    account, context = await active_account(registry)
    lifecycle = AccountLifecycle(registry)
    receipt = await lifecycle.request_stop(account["account_id"], "edit", expected_revision=account["revision"],
                                           idempotency_key="edit-command", fields={"igneous": "next-test-igneous"})
    stopping = await registry.get(account["account_id"])
    assert not stopping["enabled"] and stopping["stop_operation_id"] == receipt["operation_id"]
    assert (await registry.context(account["account_id"])).values == context.values
    for action in (
        registry.edit(account["account_id"], expected_revision=stopping["revision"], label="Bypass"),
        registry.enable(account["account_id"], expected_revision=stopping["revision"]),
        registry.record_identity(account["account_id"], 1, "1234", "Public account"),
    ):
        with pytest.raises(AccountError) as error:
            await action
        assert error.value.code == "ACCOUNT_STOPPING"
    async def not_stopped(*args):
        return False
    assert await lifecycle.process_operations(stop_runtime=not_stopped, process_queue_operations=no_queue_operations) == 0
    assert (await lifecycle.get_operation(receipt["operation_id"]))["status"] == "PENDING"
    restarted = AccountLifecycle(AccountRegistry(registry._database, registry._secrets))
    assert await restarted.process_operations(stop_runtime=stop_runtime, process_queue_operations=no_queue_operations) == 1
    updated = await registry.get(account["account_id"])
    assert not updated["enabled"] and not updated["verified"] and updated["credential_revision"] == 2
    assert updated["revision"] == account["revision"] + 2 and updated["stop_operation_id"] is None
    assert (await registry.context(account["account_id"])).values == {**context.values, "igneous": "next-test-igneous"}
    assert (await registry.context(account["account_id"], credential_revision=1)).values == context.values
    with registry._database.connection() as c:
        public_storage = repr(c.execute("SELECT * FROM account_operations").fetchall())
        public_storage += repr(c.execute("SELECT * FROM account_command_receipts").fetchall())
    assert "next-test-igneous" not in public_storage


@pytest.mark.asyncio
async def test_disable_keeps_credentials_and_delete_keeps_replay_and_import_marker(registry):
    registry._secrets.write("telegram_bot_token", "legacy-test-token")
    account_id = (await registry.import_legacy())["TG_BOT"]
    account = await registry.get(account_id)
    lifecycle = AccountLifecycle(registry)
    command = dict(expected_revision=account["revision"], idempotency_key="disable-command")
    receipt = await lifecycle.request_stop(account_id, "disable", **command)
    assert await lifecycle.request_stop(account_id, "disable", **command) == receipt
    assert await lifecycle.process_operations(stop_runtime=stop_runtime, process_queue_operations=no_queue_operations) == 1
    disabled = await registry.get(account_id)
    assert disabled["credential_revision"] == 1 and (await registry.context(account_id)).values["bot_token"] == "legacy-test-token"
    verified = await registry.record_identity(account_id, 1, "1234", "Bot")
    enabled = await registry.enable(account_id, expected_revision=verified["revision"])
    command = dict(expected_revision=enabled["revision"], idempotency_key="delete-command")
    deleted_receipt = await lifecycle.request_stop(account_id, "delete", **command)
    assert await lifecycle.process_operations(stop_runtime=stop_runtime, process_queue_operations=no_queue_operations) == 1
    assert await registry.list_accounts() == [] and await registry.import_legacy() == {}
    assert await lifecycle.request_stop(account_id, "delete", **command) == deleted_receipt
    assert (await lifecycle.get_operation(deleted_receipt["operation_id"]))["status"] == "COMPLETED"
    with pytest.raises(AccountError) as reused:
        await lifecycle.request_stop(account_id, "disable", **command)
    assert reused.value.code == "IDEMPOTENCY_CONFLICT"


@pytest.mark.asyncio
async def test_competing_commands_reserve_one_intention_and_one_pending_file(registry):
    account, _ = await active_account(registry)
    lifecycle = AccountLifecycle(registry)
    results = await asyncio.gather(*[
        lifecycle.request_stop(account["account_id"], "edit", expected_revision=account["revision"],
                               idempotency_key=f"edit-{index}", fields={"igneous": f"next-test-{index}"})
        for index in range(2)
    ], return_exceptions=True)
    assert sum(isinstance(result, dict) for result in results) == 1
    assert [result.code for result in results if isinstance(result, AccountError)] == ["REVISION_CONFLICT"]
    with registry._database.connection() as c:
        references = {row[0] for row in c.execute("SELECT secret_ref FROM external_account_credentials")}
        references |= {row[0] for row in c.execute("SELECT next_credentials_ref FROM account_operations")}
        assert c.execute("SELECT COUNT(*) FROM account_command_receipts").fetchone()[0] == 1
    assert {path.name for path in registry._secrets.root.iterdir()} == references


@pytest.mark.asyncio
async def test_receipt_failure_rolls_back_stop_queue_dependencies_and_private_file(registry, tmp_path):
    account, context = await active_account(registry)
    lifecycle = AccountLifecycle(registry)
    job_id = pending(registry._database, "EXHENTAI")
    downloads = DownloadService(registry._database, tmp_path)
    job = downloads._claim_pending_job_sync()
    execution = Execution(registry._database.path, job["task_id"], job["attempt_id"], job["generation"])
    await lifecycle.bind_execution(context, execution)
    files = {path.name for path in registry._secrets.root.iterdir()}
    with registry._database.connection() as c:
        c.execute("CREATE TRIGGER refuse_account_receipt BEFORE INSERT ON account_command_receipts "
                  "BEGIN SELECT RAISE(ABORT, 'CONTROLLED_REFUSAL'); END")
    with pytest.raises(sqlite3.IntegrityError, match="CONTROLLED_REFUSAL"):
        await lifecycle.request_stop(account["account_id"], "edit", expected_revision=account["revision"],
                                     idempotency_key="rollback-command", fields={"igneous": "next-test-value"})
    assert await registry.get(account["account_id"]) == account
    assert {path.name for path in registry._secrets.root.iterdir()} == files
    with registry._database.connection() as c:
        assert c.execute("SELECT state FROM download_jobs WHERE id = ?", (job_id,)).fetchone()[0] == "DOWNLOADING"
        assert c.execute("SELECT COUNT(*) FROM queue_operations").fetchone()[0] == 0
        assert c.execute("SELECT COUNT(*) FROM account_operations").fetchone()[0] == 0


@pytest.mark.asyncio
async def test_edit_waits_for_real_bound_transfer_cleanup_and_revokes_late_write(registry, tmp_path):
    account, context = await active_account(registry)
    lifecycle = AccountLifecycle(registry)
    entered, cancelling, release, exited = (asyncio.Event() for _ in range(4))
    late_writes = []
    async def producer(candidate_id):
        await lifecycle.bind_execution(context, current_execution.get())
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelling.set()
            await release.wait()
            try:
                with registry._database.connection() as c:
                    c.execute("UPDATE download_jobs SET state = 'COMPLETED' WHERE id = ?", (job_id,))
            except sqlite3.IntegrityError as exc:
                late_writes.append(str(exc))
            exited.set()
    downloads = DownloadService(registry._database, tmp_path, exhentai_download=producer)
    job_id = pending(registry._database, "EXHENTAI")
    worker = asyncio.create_task(downloads._process_one())
    await asyncio.wait_for(entered.wait(), 3)
    receipt = await lifecycle.request_stop(account["account_id"], "edit", expected_revision=account["revision"],
                                           idempotency_key="bound-edit", fields={"igneous": "next-test-value"})
    processing = asyncio.create_task(lifecycle.process_operations(stop_runtime=stop_runtime, process_queue_operations=downloads.process_operations))
    await asyncio.wait_for(cancelling.wait(), 3)
    try:
        assert not processing.done() and not exited.is_set()
        assert (await registry.context(account["account_id"])).values == context.values
        with pytest.raises(AccountError) as revoked:
            # A different queued attempt cannot select the now-disabled account.
            pending(registry._database, "TELEGRAM", candidate_id=2)
            other = downloads._claim_pending_job_sync()
            execution = Execution(registry._database.path, other["task_id"], other["attempt_id"], other["generation"])
            await lifecycle.bind_execution(context, execution)
        assert revoked.value.code == "ACCOUNT_UNAVAILABLE"
    finally:
        release.set()
        await asyncio.gather(worker, processing)
    assert processing.result() == 1 and exited.is_set()
    assert late_writes == ["QUEUE_STOP_NOT_CONFIRMED"]
    assert (await lifecycle.get_operation(receipt["operation_id"]))["status"] == "COMPLETED"


@pytest.mark.asyncio
async def test_unconfirmed_remote_dependency_survives_restart_before_new_version(registry, tmp_path):
    account, context = await active_account(registry)
    lifecycle = AccountLifecycle(registry)
    reachable = False
    async def remote_stop(operation):
        if not reachable:
            raise OSError("controlled outage")
    downloads = DownloadService(registry._database, tmp_path, torrent_stop=remote_stop)
    job_id = pending(registry._database, "EH_TORRENT")
    job = downloads._claim_pending_job_sync()
    await lifecycle.bind_execution(context, Execution(registry._database.path, job["task_id"], job["attempt_id"], job["generation"]))
    downloads._park_job_sync(job_id, {"hash": "a" * 40})
    receipt = await lifecycle.request_stop(account["account_id"], "edit", expected_revision=account["revision"],
                                           idempotency_key="remote-edit", fields={"igneous": "next-test-value"})
    assert await lifecycle.process_operations(stop_runtime=stop_runtime, process_queue_operations=downloads.process_operations) == 0
    assert (await registry.context(account["account_id"])).credential_revision == 1
    reachable = True
    restarted = AccountLifecycle(registry)
    assert await restarted.process_operations(stop_runtime=stop_runtime, process_queue_operations=downloads.process_operations) == 1
    assert (await registry.context(account["account_id"])).credential_revision == 2
    assert (await restarted.get_operation(receipt["operation_id"]))["status"] == "COMPLETED"


@pytest.mark.asyncio
async def test_removed_business_rows_still_block_account_delete_until_remote_stop(registry, tmp_path):
    account, context = await active_account(registry)
    lifecycle = AccountLifecycle(registry)
    reachable = False
    async def remote_stop(operation):
        if not reachable:
            raise OSError("controlled outage")
    downloads = DownloadService(registry._database, tmp_path, torrent_stop=remote_stop)
    job_id = pending(registry._database, "EH_TORRENT")
    job = downloads._claim_pending_job_sync()
    await lifecycle.bind_execution(context, Execution(registry._database.path, job["task_id"], job["attempt_id"], job["generation"]))
    downloads._park_job_sync(job_id, {"hash": "b" * 40})
    removal = archived(registry._database, tmp_path)
    await removal.remove_work(1)
    with registry._database.connection() as c:
        assert c.execute("SELECT COUNT(*) FROM download_jobs").fetchone()[0] == 0
    receipt = await lifecycle.request_stop(account["account_id"], "delete", expected_revision=account["revision"], idempotency_key="removed-delete")
    assert await lifecycle.process_operations(stop_runtime=stop_runtime, process_queue_operations=downloads.process_operations) == 0
    assert (await registry.get(account["account_id"]))["stop_operation_id"] == receipt["operation_id"]
    reachable = True
    assert await lifecycle.process_operations(stop_runtime=stop_runtime, process_queue_operations=downloads.process_operations) == 1
    assert await registry.list_accounts() == []


@pytest.mark.asyncio
async def test_one_runtime_failure_does_not_starve_other_account_or_leak_secrets(registry):
    first, _ = await active_account(registry, identity="1234")
    second, _ = await active_account(registry, identity="5678")
    lifecycle = AccountLifecycle(registry)
    first_receipt = await lifecycle.request_stop(first["account_id"], "disable", expected_revision=first["revision"], idempotency_key="first-disable")
    await lifecycle.request_stop(second["account_id"], "disable", expected_revision=second["revision"], idempotency_key="second-disable")
    async def failing_runtime(account_id, revision):
        if account_id == first["account_id"]:
            raise RuntimeError("test-secret-must-never-appear")
        return True
    assert await lifecycle.process_operations(stop_runtime=failing_runtime, process_queue_operations=no_queue_operations) == 1
    operation = await lifecycle.get_operation(first_receipt["operation_id"])
    assert operation["status"] == "PENDING" and operation["error_code"] == "ACCOUNT_STOP_NOT_CONFIRMED"
    assert "test-secret" not in json.dumps(operation)
    assert (await registry.get(second["account_id"]))["stop_operation_id"] is None
    assert await lifecycle.process_operations(stop_runtime=stop_runtime, process_queue_operations=no_queue_operations) == 1
    assert (await lifecycle.get_operation(first_receipt["operation_id"]))["error_code"] is None


@pytest.mark.asyncio
async def test_exact_binding_rejects_stale_credentials_and_wrong_database(registry, tmp_path):
    account, context = await active_account(registry)
    lifecycle = AccountLifecycle(registry)
    pending(registry._database)
    job = DownloadService(registry._database, tmp_path)._claim_pending_job_sync()
    execution = Execution(registry._database.path, job["task_id"], job["attempt_id"], job["generation"])
    await lifecycle.bind_execution(context, execution)
    await lifecycle.bind_execution(context, execution)
    with pytest.raises(AccountError) as stale:
        await lifecycle.bind_execution(replace(context, credential_revision=2), execution)
    assert stale.value.code == "ACCOUNT_UNAVAILABLE"
    with pytest.raises(AccountError) as different:
        await lifecycle.bind_execution(context, replace(execution, database_path=tmp_path / "wrong.db"))
    assert different.value.code == "ACCOUNT_EXECUTION_INVALID"
    with registry._database.connection() as c:
        assert c.execute("SELECT COUNT(*) FROM account_execution_bindings").fetchone()[0] == 1


@pytest.mark.asyncio
async def test_finalize_failure_keeps_pending_private_version_and_retries_atomic_commit(registry):
    account, context = await active_account(registry)
    lifecycle = AccountLifecycle(registry)
    receipt = await lifecycle.request_stop(account["account_id"], "edit", expected_revision=account["revision"],
                                           idempotency_key="failed-finalize", fields={"igneous": "next-test-value"})
    with registry._database.connection() as c:
        reference = c.execute("SELECT next_credentials_ref FROM account_operations").fetchone()[0]
        c.execute("CREATE TRIGGER refuse_account_finalize BEFORE UPDATE OF stop_operation_id ON external_accounts "
                  "WHEN NEW.stop_operation_id IS NULL BEGIN SELECT RAISE(ABORT, 'CONTROLLED_REFUSAL'); END")
    registry._discard_unreferenced(reference)
    assert registry._secrets.read(reference) is not None
    assert await lifecycle.process_operations(stop_runtime=stop_runtime, process_queue_operations=no_queue_operations) == 0
    assert (await registry.context(account["account_id"])).values == context.values
    with registry._database.connection() as c:
        assert c.execute("SELECT COUNT(*) FROM external_account_credentials").fetchone()[0] == 1
        c.execute("DROP TRIGGER refuse_account_finalize")
    assert await AccountLifecycle(registry).process_operations(stop_runtime=stop_runtime, process_queue_operations=no_queue_operations) == 1
    assert (await lifecycle.get_operation(receipt["operation_id"]))["status"] == "COMPLETED"
    assert (await registry.context(account["account_id"])).values["igneous"] == "next-test-value"


@pytest.mark.asyncio
async def test_cancelling_operation_processor_never_confirms_a_runtime_stop(registry):
    account, _ = await active_account(registry)
    lifecycle = AccountLifecycle(registry)
    receipt = await lifecycle.request_stop(account["account_id"], "disable", expected_revision=account["revision"], idempotency_key="cancel-processor")
    entered = asyncio.Event()
    async def blocked_runtime(*args):
        entered.set()
        await asyncio.Event().wait()
    task = asyncio.create_task(lifecycle.process_operations(stop_runtime=blocked_runtime, process_queue_operations=no_queue_operations))
    await asyncio.wait_for(entered.wait(), 3)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert (await lifecycle.get_operation(receipt["operation_id"]))["status"] == "PENDING"
    assert (await registry.get(account["account_id"]))["stop_operation_id"] == receipt["operation_id"]


@pytest.mark.asyncio
async def test_completed_owned_seeding_is_a_stop_dependency_without_erasing_completion(registry, tmp_path):
    account, context = await active_account(registry)
    lifecycle = AccountLifecycle(registry)
    reachable = False
    async def remote_stop(operation):
        assert operation["action"] == "stop-seeding"
        if not reachable:
            raise OSError("controlled outage")
    downloads = DownloadService(registry._database, tmp_path, torrent_stop=remote_stop)
    job_id = pending(registry._database, "EH_TORRENT")
    job = downloads._claim_pending_job_sync()
    await lifecycle.bind_execution(context, Execution(registry._database.path, job["task_id"], job["attempt_id"], job["generation"]))
    downloads._park_job_sync(job_id, {"hash": "c" * 40, "seeding": True})
    downloads._mark_job_completed_sync(job_id)
    await lifecycle.request_stop(account["account_id"], "disable", expected_revision=account["revision"], idempotency_key="seeding-disable")
    assert await lifecycle.process_operations(stop_runtime=stop_runtime, process_queue_operations=downloads.process_operations) == 0
    reachable = True
    assert await lifecycle.process_operations(stop_runtime=stop_runtime, process_queue_operations=downloads.process_operations) == 1
    with registry._database.connection() as c:
        state, details = c.execute("SELECT state, details_json FROM download_jobs WHERE id = ?", (job_id,)).fetchone()
    assert state == "COMPLETED" and json.loads(details)["seeding"] is False


@pytest.mark.asyncio
async def test_pure_label_edit_does_not_disable_account_or_restart_credential_version(registry):
    account, _ = await active_account(registry)
    lifecycle = AccountLifecycle(registry)
    result = await lifecycle.request_stop(account["account_id"], "edit", expected_revision=account["revision"],
                                          idempotency_key="label-only", label="Renamed", fields={"igneous":"old-test-igneous"})
    assert result["status"] == "COMPLETED" and result["result"]["enabled"]
    assert result["result"]["credential_revision"] == account["credential_revision"]
    with registry._database.connection() as c:
        assert c.execute("SELECT COUNT(*) FROM account_operations").fetchone()[0] == 0
