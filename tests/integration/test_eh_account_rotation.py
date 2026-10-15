"""EH account selection is persistent, fair and bounded by an attempted set."""
import asyncio
import pytest
import pytest_asyncio

from app.connections.models import ProviderStatus
from app.connections.service import AccountService
from app.db.database import Database
from app.connections.accounts import AccountRegistry
from app.secrets import SecretStore


@pytest_asyncio.fixture
async def accounts(tmp_path):
    db = Database(tmp_path / "rotation.db")
    await db.initialize()
    registry = AccountRegistry(db, SecretStore(tmp_path / "private"))
    service = AccountService(registry, telegram_client=None, exhentai_client=None)
    created = []
    for label in ("a", "b", "c"):
        row = await registry.create("EH", label, {"ipb_member_id": label, "ipb_pass_hash": label, "igneous": label})
        row = await registry.record_identity(row["account_id"], expected_revision=row["revision"],
            credential_revision=row["credential_revision"], remote_identity=label, platform_name=label)
        row = await registry.enable(row["account_id"], expected_revision=row["revision"])
        service._states[row["account_id"]] = ProviderStatus("connected", True, identity=label)
        created.append(row["account_id"])
    yield db, service, created


@pytest.mark.asyncio
async def test_eh_selection_rotates_and_persists_across_service_restart(accounts):
    db, service, ids = accounts
    ordered = [row["account_id"] for row in await service.registry.list_accounts()]
    chosen = [((await service.select_account("EH"))["account_id"]) for _ in range(4)]
    assert chosen == [ordered[0], ordered[1], ordered[2], ordered[0]]
    restarted = AccountService(service.registry, telegram_client=None, exhentai_client=None)
    restarted._states.update(service._states)
    assert (await restarted.select_account("EH"))["account_id"] == ordered[1]


@pytest.mark.asyncio
async def test_eh_selection_skips_cooldown_and_finite_exclusions(accounts):
    db, service, ids = accounts
    with db.connection() as connection:
        connection.execute("INSERT INTO account_rotation(account_id, cooldown_until) VALUES(?, datetime('now', '+1 hour'))", (ids[0],))
    selected = await service.select_account("EH", excluded_account_ids=[ids[1]])
    assert selected["account_id"] == ids[2]
    assert await service.select_account("EH", excluded_account_ids=ids) is None


@pytest.mark.asyncio
async def test_eh_availability_reads_do_not_advance_rotation(accounts):
    db, service, _ = accounts
    first = await service.selected_account("EH")
    assert (await service.selected_account("EH"))["account_id"] == first["account_id"]
    with db.connection() as connection:
        assert connection.execute("SELECT count(*) FROM account_rotation").fetchone() == (0,)
        assert connection.execute("SELECT count(*) FROM account_rotation_state").fetchone() == (0,)
    assert (await service.select_account("EH"))["account_id"] == first["account_id"]


@pytest.mark.asyncio
async def test_concurrent_eh_selections_have_distinct_fair_turns(accounts):
    db, service, ids = accounts
    chosen = await asyncio.gather(*(service.select_account("EH") for _ in ids))
    assert {account["account_id"] for account in chosen} == set(ids)
    with db.connection() as connection:
        assert connection.execute("SELECT last_sequence FROM account_rotation ORDER BY last_sequence").fetchall() == [(1,), (2,), (3,)]


@pytest.mark.asyncio
async def test_nested_eh_request_rotates_once_and_explicit_account_does_not_rotate(accounts):
    db, service, ids = accounts
    async with service.use_kind("EH") as first:
        async with service.use_kind("EH") as nested:
            assert nested is first
        async with service.use_kind("EH", account_id=first.account_id) as explicit:
            assert explicit is first
    async with service.use_kind("EH", account_id=ids[0]):
        pass
    async with service.use_kind("EH") as second:
        assert second.account_id != first.account_id
    with db.connection() as connection:
        assert connection.execute("SELECT next_sequence FROM account_rotation_state WHERE kind = 'EH'").fetchone() == (3,)


@pytest.mark.asyncio
async def test_eh_operation_serializes_same_account_until_physical_cleanup(accounts):
    _, service, ids = accounts
    entered, cleanup, release, second_entered = (asyncio.Event() for _ in range(4))

    async def first():
        async with service.use_kind("EH", account_id=ids[0]):
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                cleanup.set()
                await release.wait()

    async def second():
        async with service.use_kind("EH", account_id=ids[0]):
            second_entered.set()

    first_task = asyncio.create_task(first())
    second_task = None
    try:
        await asyncio.wait_for(entered.wait(), 3)
        second_task = asyncio.create_task(second())
        first_task.cancel()
        await asyncio.wait_for(cleanup.wait(), 3)
        # An unrelated account can still run while the first is cleaning up.
        async with service.use_kind("EH", account_id=ids[1]):
            assert not second_entered.is_set()
    finally:
        release.set()
        if not first_task.cancelling():
            first_task.cancel()
        await asyncio.gather(first_task, return_exceptions=True)
        if second_task is not None:
            await asyncio.wait_for(second_task, 3)
    assert second_entered.is_set()


@pytest.mark.asyncio
async def test_eh_selection_respects_captured_credential_versions(accounts):
    _, service, ids = accounts
    assert await service.select_account("EH", candidate_versions={}) is None
    assert await service.select_account("EH", candidate_versions={ids[0]: 999}) is None
    assert (await service.select_account("EH", candidate_versions={ids[1]: 1}))["account_id"] == ids[1]
