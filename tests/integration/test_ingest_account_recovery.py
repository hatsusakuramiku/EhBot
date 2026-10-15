"""Saved observations recover through eligible account leases without borrowing another identity."""
import asyncio
import time

import pytest

from app.candidates.entries import IngestError, check_claim, claim_entry, register_observation
from app.candidates.ingestor import CandidateIngestor
from app.connections.accounts import AccountRegistry
from app.connections.lifecycle import AccountLifecycle
from app.connections.runtime import AccountRuntime
from app.secrets import SecretStore
from tests.integration.test_account_lifecycle import no_queue_operations
from tests.integration.test_ingest_entries import message
from tests.integration.test_ingest_pipeline import accepted, setup


async def active_pipeline(tmp_path):
    database, (a, b) = await setup(tmp_path)
    registry = AccountRegistry(database, SecretStore(tmp_path / "private"))
    lifecycle = AccountLifecycle(registry)
    return database, registry, lifecycle, AccountRuntime(registry, lifecycle), a, b


@pytest.mark.asyncio
@pytest.mark.parametrize("account_change", ["stopping", "disabled", "deleted", "unverified"])
async def test_saved_observer_recovers_when_original_account_cannot_be_borrowed(tmp_path, account_change):
    db, registry, lifecycle, runtime, a, b = await active_pipeline(tmp_path)
    seen = []

    class Admission:
        async def decide(self, parsed):
            seen.append(parsed.account_id)
            if len(seen) == 1:
                # Persist B while A owns the key; no new delivery is needed later.
                async with runtime.use(b):
                    assert not (await ingestor.process_message(message(b, chat_id=-1000000000888)))[0]
                raise RuntimeError("fixture original request failed")
            return accepted()

    ingestor = CandidateIngestor(db, admission=Admission())
    async with runtime.use(a):
        with pytest.raises(ExceptionGroup):
            await ingestor.process_message(message(a))
    first = await registry.get(a)
    action = {"deleted": "delete", "unverified": "edit"}.get(account_change, "disable")
    await lifecycle.request_stop(a, action, expected_revision=first["revision"],
                                 idempotency_key="fixture-account-change",
                                 fields={"bot_token": "fixture-replacement"} if action == "edit" else None)
    if account_change != "stopping":
        assert await lifecycle.process_operations(stop_runtime=runtime.stop,
                                                  process_queue_operations=no_queue_operations) == 1
    with db.connection() as connection:
        connection.execute("UPDATE ingest_entries SET retry_at = NULL")
        assert connection.execute("SELECT enabled FROM telegram_sources WHERE account_id = "
                                  "(SELECT id FROM telegram_accounts WHERE external_account_id = ?) LIMIT 1", (a,)).fetchone() == (1,)
    async with runtime.use(b):
        fresh = CandidateIngestor(db, admission=Admission())
        assert await fresh.process_pending_entries() == 1
        assert await fresh.process_pending_entries() == 0
    assert seen == [a, b]
    with db.connection() as connection:
        assert connection.execute("SELECT count(*) FROM candidates").fetchone() == (1,)
        assert connection.execute("SELECT count(*) FROM candidate_messages").fetchone() == (2,)


@pytest.mark.asyncio
async def test_other_receiving_account_does_not_process_still_eligible_original_input(tmp_path):
    db, _, _, runtime, a, b = await active_pipeline(tmp_path)
    seen = []

    class Admission:
        async def decide(self, parsed):
            seen.append(parsed.account_id)
            if len(seen) == 1:
                raise RuntimeError("fixture original request failed")
            return accepted()

    ingestor = CandidateIngestor(db, admission=Admission())
    async with runtime.use(a):
        with pytest.raises(ExceptionGroup):
            await ingestor.process_message(message(a))
    with db.connection() as connection:
        connection.execute("UPDATE ingest_entries SET retry_at = NULL")
    async with runtime.use(b):
        assert not (await ingestor.process_message(message(b, chat_id=-1000000000888)))[0]
        assert await ingestor.process_pending_entries() == 0
    assert seen == [a]
    async with runtime.use(a):
        assert await ingestor.process_pending_entries() == 1
    assert seen == [a, a]


@pytest.mark.asyncio
@pytest.mark.parametrize("delivery", ["saved", "new"])
async def test_expired_abandoned_input_yields_to_an_eligible_observer(tmp_path, delivery):
    db, registry, lifecycle, runtime, a, b = await active_pipeline(tmp_path)
    with db.connection() as connection:
        connection.execute("BEGIN IMMEDIATE")
        entry = register_observation(connection, message(a))
        old_claim = claim_entry(connection, entry["id"], owner="fixture-dead-process",
                                lease_seconds=1, now=int(time.time()) - 10)
        if delivery == "saved":
            register_observation(connection, message(b, chat_id=-1000000000888))
    first = await registry.get(a)
    await lifecycle.request_stop(a, "disable", expected_revision=first["revision"], idempotency_key="fixture-disable-dead-owner")
    assert await lifecycle.process_operations(stop_runtime=runtime.stop, process_queue_operations=no_queue_operations) == 1
    seen = []

    class Admission:
        async def decide(self, parsed):
            seen.append(parsed.account_id)
            return accepted()

    async with runtime.use(b):
        ingestor = CandidateIngestor(db, admission=Admission())
        if delivery == "saved":
            assert await ingestor.process_pending_entries() == 1
        else:
            assert (await ingestor.process_message(message(b, chat_id=-1000000000888)))[0]
        assert await ingestor.process_pending_entries() == 0
    assert seen == [b]
    with db.connection() as connection:
        with pytest.raises(IngestError, match="INGEST_STALE_PROCESSOR"):
            check_claim(connection, old_claim)
        assert connection.execute("SELECT count(*) FROM candidates").fetchone() == (1,)


@pytest.mark.asyncio
@pytest.mark.parametrize("new_consumer", [False, True])
async def test_expired_live_processor_keeps_exclusive_ownership_until_actual_cleanup(tmp_path, new_consumer):
    db, registry, lifecycle, runtime, a, b = await active_pipeline(tmp_path)
    entered, cleaning, release = (asyncio.Event() for _ in range(3))
    seen = []

    class Admission:
        async def decide(self, parsed):
            seen.append(parsed.account_id)
            if len(seen) == 1:
                entered.set()
                try:
                    await asyncio.Event().wait()
                finally:
                    cleaning.set()
                    await release.wait()
            return accepted()

    ingestor = CandidateIngestor(db, admission=Admission())

    async def original():
        async with runtime.use(a):
            return await ingestor.process_message(message(a))

    first = asyncio.create_task(original())
    stopping = None
    try:
        await asyncio.wait_for(entered.wait(), 3)
        async with runtime.use(b):
            assert not (await ingestor.process_message(message(b, chat_id=-1000000000888)))[0]
        account = await registry.get(a)
        await lifecycle.request_stop(a, "disable", expected_revision=account["revision"], idempotency_key="fixture-stop-live-owner")
        stopping = asyncio.create_task(lifecycle.process_operations(stop_runtime=runtime.stop,
                                                                    process_queue_operations=no_queue_operations))
        await asyncio.wait_for(cleaning.wait(), 3)
        with db.connection() as connection:
            connection.execute("UPDATE ingest_entries SET lease_until = unixepoch() - 1")
        async with runtime.use(b):
            consumer = CandidateIngestor(db, admission=Admission()) if new_consumer else ingestor
            assert await consumer.process_pending_entries() == 0
            assert not (await consumer.process_message(message(b, chat_id=-1000000000888)))[0]
        assert seen == [a] and not first.done() and not stopping.done()
    finally:
        release.set()
        if stopping is None:
            first.cancel()
        await asyncio.gather(first, *([stopping] if stopping is not None else []), return_exceptions=True)
    assert first.cancelled() and stopping.result() == 1
    async with runtime.use(b):
        assert await ingestor.process_pending_entries() == 1
    assert seen == [a, b]


@pytest.mark.asyncio
async def test_expired_original_that_is_still_eligible_recovers_only_under_its_own_account(tmp_path):
    db, _, _, runtime, a, b = await active_pipeline(tmp_path)
    with db.connection() as connection:
        connection.execute("BEGIN IMMEDIATE")
        entry = register_observation(connection, message(a))
        claim_entry(connection, entry["id"], owner="fixture-dead-process", lease_seconds=1, now=int(time.time()) - 10)
        register_observation(connection, message(b, chat_id=-1000000000888))
    seen = []

    class Admission:
        async def decide(self, parsed):
            seen.append(parsed.account_id)
            return accepted()

    ingestor = CandidateIngestor(db, admission=Admission())
    async with runtime.use(b):
        assert await ingestor.process_pending_entries() == 0
    assert not seen
    async with runtime.use(a):
        assert await ingestor.process_pending_entries() == 1
    assert seen == [a]


@pytest.mark.asyncio
async def test_foreign_fixed_inputs_cannot_starve_the_receiving_accounts_own_saved_work(tmp_path):
    db, _, _, runtime, a, b = await active_pipeline(tmp_path)
    for index in range(100):
        await db.configure_telegram_source(account_id=a, source_type="CHANNEL", chat_id=-1000000010000 - index,
                                           display_name="Fixture source", enabled=True,
                                           allowed_archive_formats=("zip",), max_attachment_size_mb=0)
    with db.connection() as connection:
        connection.execute("BEGIN IMMEDIATE")
        for index in range(100):
            register_observation(connection, message(a, gid=1000 + index, chat_id=-1000000010000 - index))
            register_observation(connection, message(b, gid=1000 + index, message_id=index + 1, chat_id=-1000000000888))
        register_observation(connection, message(b, gid=2000, message_id=101, chat_id=-1000000000888))
    seen = []

    class Admission:
        async def decide(self, parsed):
            seen.append((parsed.account_id, parsed.ex_gid))
            return accepted()

    async with runtime.use(b):
        # Restarting the consumer must not reset the account's scan on each pass.
        recovered = 0
        for _ in range(3):
            recovered += await CandidateIngestor(db, admission=Admission()).process_pending_entries()
        assert recovered == 1
    assert seen == [(b, 2000)]
    with db.connection() as connection:
        assert connection.execute("SELECT count(*) FROM ingest_entries WHERE processing_state = 'PENDING'").fetchone() == (100,)
