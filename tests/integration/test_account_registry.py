"""Account storage contracts, using only temporary databases and private files."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
import json
import sqlite3
from threading import Barrier

import pytest

from app.connections.accounts import AccountError, AccountRegistry, MASK
from app.db.database import Database
from app.secrets import SecretStore
from tests.integration.test_queue_control import old_database


@pytest.fixture
def registry(tmp_path):
    database = Database(tmp_path / "accounts.db")
    asyncio.run(database.initialize())
    return AccountRegistry(database, SecretStore(tmp_path / "private"))


@pytest.mark.asyncio
async def test_multiple_accounts_keep_independent_masked_configurations(registry):
    first = await registry.create("TG_BOT", "Bot A", {"bot_token": "test-token-a"})
    second = await registry.create("TG_BOT", "Bot B", {"bot_token": "test-token-b"})
    eh = await registry.create("EH", "EH A", {"ipb_member_id": "123", "ipb_pass_hash": "test-cookie"})
    user = await registry.create("TG_USER", "User A", {"api_id": "1234", "api_hash": "test-api-hash", "phone": "+123456"})
    assert len({row["account_id"] for row in (first, second, eh, user)}) == 4
    assert not any(row["enabled"] for row in await registry.list_accounts())
    serialized = json.dumps(await registry.list_accounts())
    for secret in ("test-token-a", "test-token-b", "test-cookie", "test-api-hash", "+123456"):
        assert secret not in serialized
    assert first["fields"] == {"bot_token": MASK}
    assert eh["fields"]["igneous"] == ""
    assert (await registry.reveal(second["account_id"]))["fields"]["bot_token"] == "test-token-b"
    context = await registry.context(first["account_id"])
    assert context.values["bot_token"] == "test-token-a" and "test-token-a" not in repr(context)
    with registry._database.connection() as c:
        database_values = repr(c.execute("SELECT * FROM external_accounts").fetchall())
        assert "test-token-a" not in database_values and "test-cookie" not in database_values


@pytest.mark.asyncio
async def test_partial_edits_keep_untouched_fields_and_old_execution_version(registry):
    original = await registry.create("EH", "EH", {"ipb_member_id": "123", "ipb_pass_hash": "old-test-hash", "igneous": "old-test-igneous"})
    account_id = original["account_id"]
    before = await registry.context(account_id)
    updated = await registry.edit(account_id, expected_revision=1, fields={"igneous": "new-test-igneous"})
    assert updated["revision"] == 2 and updated["credential_revision"] == 2
    revealed = await registry.reveal(account_id)
    assert revealed["fields"] == {"ipb_member_id": "123", "ipb_pass_hash": "old-test-hash", "igneous": "new-test-igneous"}
    assert before.values["igneous"] == "old-test-igneous"
    exact_old = await registry.context(account_id, credential_revision=1)
    assert exact_old.values == before.values and exact_old.credential_revision == 1
    with pytest.raises(AccountError) as missing_version:
        await registry.context(account_id, credential_revision=99)
    assert missing_version.value.code == "ACCOUNT_CREDENTIAL_VERSION_NOT_FOUND"
    with registry._database.connection() as c:
        versions = c.execute("SELECT credential_revision, secret_ref FROM external_account_credentials ORDER BY credential_revision").fetchall()
    assert len(versions) == 2
    assert json.loads(registry._secrets.read(versions[0][1])) == before.values
    renamed = await registry.edit(account_id, expected_revision=2, label="EH renamed")
    assert renamed["revision"] == 3 and renamed["credential_revision"] == 2
    assert await registry.edit(account_id, expected_revision=3) == renamed
    cleared = await registry.edit(account_id, expected_revision=3, clear_fields=["igneous"])
    assert cleared["fields"]["igneous"] == ""
    assert (await registry.reveal(account_id))["fields"] == {"ipb_member_id": "123", "ipb_pass_hash": "old-test-hash"}


@pytest.mark.asyncio
@pytest.mark.parametrize("fields,clear_fields", [({"bot_token": MASK}, []), ({"bot_token": ""}, []),
                                                ({"unknown": "x"}, []), ({"bot_token": 1}, []),
                                                ({"bot_token": "x"}, ["bot_token"]), ({}, ["unknown"])])
async def test_mask_empty_unknown_or_ambiguous_changes_cannot_destroy_credentials(registry, fields, clear_fields):
    account = await registry.create("TG_BOT", "Bot", {"bot_token": "retained-test-token"})
    with pytest.raises(AccountError):
        await registry.edit(account["account_id"], expected_revision=1, fields=fields, clear_fields=clear_fields)
    assert await registry.get(account["account_id"]) == account
    assert (await registry.reveal(account["account_id"]))["fields"] == {"bot_token": "retained-test-token"}


def test_competing_credential_editors_cannot_overwrite_winners_private_file(registry, monkeypatch):
    account = asyncio.run(registry.create("TG_BOT", "Bot", {"bot_token": "old-test-token"}))
    barrier = Barrier(2)
    write = registry._write_version
    def competing_write(*args):
        reference = write(*args)
        barrier.wait(timeout=5)
        return reference
    monkeypatch.setattr(registry, "_write_version", competing_write)
    def edit(value):
        try:
            return value, registry._edit_sync(account["account_id"], 1, None, {"bot_token": value}, [])
        except AccountError as exc:
            return value, exc.code
    with ThreadPoolExecutor(2) as pool:
        results = list(pool.map(edit, ["new-test-a", "new-test-b"]))
    winners = [(value, result) for value, result in results if isinstance(result, dict)]
    assert len(winners) == 1
    assert [result for _, result in results if isinstance(result, str)] == ["REVISION_CONFLICT"]
    winner, _ = winners[0]
    assert asyncio.run(registry.reveal(account["account_id"]))["fields"]["bot_token"] == winner
    with registry._database.connection() as c:
        references = {row[0] for row in c.execute("SELECT secret_ref FROM external_account_credentials")}
    assert {path.name for path in registry._secrets.root.iterdir()} == references


@pytest.mark.asyncio
async def test_failed_private_write_cannot_bind_missing_configuration(registry, monkeypatch):
    account = await registry.create("TG_BOT", "Bot", {"bot_token": "old-test-token"})
    def fail(*args):
        raise OSError("controlled private storage failure")
    monkeypatch.setattr(registry._secrets, "write", fail)
    with pytest.raises(OSError):
        await registry.edit(account["account_id"], expected_revision=1, fields={"bot_token": "new-test-token"})
    assert await registry.get(account["account_id"]) == account
    assert (await registry.reveal(account["account_id"]))["fields"]["bot_token"] == "old-test-token"


@pytest.mark.asyncio
async def test_database_refusal_rolls_back_version_and_cleans_unbound_file(registry):
    account = await registry.create("TG_BOT", "Bot", {"bot_token": "old-test-token"})
    files = {path.name for path in registry._secrets.root.iterdir()}
    with registry._database.connection() as c:
        c.execute("CREATE TRIGGER reject_account_edit BEFORE UPDATE ON external_accounts "
                  "BEGIN SELECT RAISE(ABORT, 'CONTROLLED_REFUSAL'); END")
    with pytest.raises(sqlite3.IntegrityError, match="CONTROLLED_REFUSAL"):
        await registry.edit(account["account_id"], expected_revision=1, fields={"bot_token": "new-test-token"})
    assert await registry.get(account["account_id"]) == account
    assert {path.name for path in registry._secrets.root.iterdir()} == files


@pytest.mark.asyncio
async def test_active_configuration_cannot_be_overwritten_before_runtime_stop(registry):
    account = await registry.create("TG_BOT", "Bot", {"bot_token": "old-test-token"})
    with registry._database.connection() as c:
        c.execute("UPDATE external_accounts SET enabled = 1, remote_identity = '1234' WHERE account_id = ?", (account["account_id"],))
    files = {path.name for path in registry._secrets.root.iterdir()}
    with pytest.raises(AccountError, match="停止") as error:
        await registry.edit(account["account_id"], expected_revision=1, fields={"bot_token": "new-test-token"})
    assert error.value.code == "ACCOUNT_ACTIVE"
    renamed = await registry.edit(account["account_id"], expected_revision=1, label="New alias")
    assert renamed["enabled"] and renamed["credential_revision"] == 1
    assert {path.name for path in registry._secrets.root.iterdir()} == files


@pytest.mark.asyncio
async def test_late_verification_or_changed_platform_identity_cannot_rebind_sources(registry):
    account = await registry.create("TG_BOT", "Bot", {"bot_token": "old-test-token"})
    account = await registry.record_identity(account["account_id"], 1, "1234", "Public Bot")
    assert await registry.record_identity(account["account_id"], 1, "1234", "Public Bot") == account
    account = await registry.edit(account["account_id"], expected_revision=account["revision"], fields={"bot_token": "new-test-token"})
    with pytest.raises(AccountError) as late:
        await registry.record_identity(account["account_id"], 1, "5678", "Other Bot")
    assert late.value.code == "REVISION_CONFLICT"
    with pytest.raises(AccountError) as changed:
        await registry.record_identity(account["account_id"], 2, "5678", "Other Bot")
    assert changed.value.code == "ACCOUNT_IDENTITY_CHANGED"
    assert (await registry.get(account["account_id"]))["remote_identity"] == "1234"


@pytest.mark.asyncio
async def test_duplicate_enabled_platform_identity_is_rejected_by_database(registry):
    accounts = [await registry.create("TG_BOT", label, {}) for label in ("A", "B")]
    for account in accounts:
        await registry.record_identity(account["account_id"], 1, "1234", "Public Bot")
    with registry._database.connection() as c:
        c.execute("UPDATE external_accounts SET enabled = 1 WHERE account_id = ?", (accounts[0]["account_id"],))
    with pytest.raises(sqlite3.IntegrityError):
        with registry._database.connection() as c:
            c.execute("UPDATE external_accounts SET enabled = 1 WHERE account_id = ?", (accounts[1]["account_id"],))
    await registry._database.initialize()
    assert len(await registry.list_accounts()) == 2


def test_upgrade_030_preserves_legacy_tasks_and_is_repeatable(tmp_path):
    database = old_database(tmp_path, [(1, "TELEGRAM", "PENDING", "legacy")], last_version=29)
    with database.connection() as c:
        tasks = c.execute("SELECT * FROM queue_tasks").fetchall()
        jobs = c.execute("SELECT * FROM download_jobs").fetchall()
    asyncio.run(database.initialize())
    asyncio.run(database.initialize())
    with database.connection() as c:
        assert c.execute("SELECT * FROM queue_tasks").fetchall() == tasks
        assert c.execute("SELECT * FROM download_jobs").fetchall() == jobs
        assert c.execute("SELECT COUNT(*) FROM external_accounts").fetchone()[0] == 0
        assert c.execute("SELECT COUNT(*) FROM schema_migrations WHERE version = 30").fetchone()[0] == 1


def test_upgrade_030_conflict_rolls_back_schema_and_allows_retry(tmp_path):
    database = old_database(tmp_path, [(1, "TELEGRAM", "PENDING", "legacy")], last_version=29)
    with database.connection() as c:
        c.execute("CREATE TABLE external_account_credentials(marker TEXT)")
    with pytest.raises(sqlite3.OperationalError, match="already exists"):
        asyncio.run(database.initialize())
    with database.connection() as c:
        assert c.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0] == 29
        assert c.execute("SELECT COUNT(*) FROM sqlite_master WHERE name IN "
                         "('external_accounts', 'external_accounts_enabled_identity')").fetchone()[0] == 0
        assert c.execute("PRAGMA table_info(external_account_credentials)").fetchone()[1] == "marker"
        c.execute("DROP TABLE external_account_credentials")
    asyncio.run(database.initialize())
    assert asyncio.run(AccountRegistry(database, SecretStore(tmp_path / "private")).list_accounts()) == []


@pytest.mark.asyncio
async def test_legacy_import_is_repeatable_keeps_originals_and_never_reveals_session(registry):
    originals = {
        "telegram_bot_token": "legacy-test-bot-token",
        "exhentai_cookies": json.dumps({"ipb_member_id": "123", "ipb_pass_hash": "legacy-test-cookie", "igneous": "legacy-test-igneous"}),
        "telegram_user_api": "1234:legacy-test-api-hash",
        "telegram_user_session": "legacy-test-session",
    }
    for name, value in originals.items():
        registry._secrets.write(name, value)
    imported = await registry.import_legacy()
    assert await registry.import_legacy() == imported
    assert set(imported) == {"TG_BOT", "TG_USER", "EH"}
    assert len(await registry.list_accounts()) == 3
    assert all(registry._secrets.read(name) == value for name, value in originals.items())
    user = imported["TG_USER"]
    assert (await registry.context(user)).values["session"] == "legacy-test-session"
    revealed = await registry.reveal(user)
    assert "legacy-test-session" not in json.dumps(revealed) and "session" not in revealed["fields"]
    assert revealed["fields"] == {"api_id": "1234", "api_hash": "legacy-test-api-hash"}
    with registry._database.connection() as c:
        c.execute("UPDATE external_accounts SET deleted_at = CURRENT_TIMESTAMP WHERE account_id = ?", (user,))
    assert "TG_USER" not in await registry.import_legacy()


def test_concurrent_legacy_import_registers_one_account_and_cleans_losing_file(registry, monkeypatch):
    registry._secrets.write("telegram_bot_token", "legacy-test-token")
    barrier = Barrier(2)
    write = registry._write_version
    def competing_write(*args):
        reference = write(*args)
        barrier.wait(timeout=5)
        return reference
    monkeypatch.setattr(registry, "_write_version", competing_write)
    with ThreadPoolExecutor(2) as pool:
        results = list(pool.map(lambda _: registry._import_legacy_sync(), range(2)))
    assert results[0] == results[1]
    assert len(asyncio.run(registry.list_accounts())) == 1
    with registry._database.connection() as c:
        references = {row[0] for row in c.execute("SELECT secret_ref FROM external_account_credentials")}
    assert {path.name for path in registry._secrets.root.iterdir()} == references | {"telegram_bot_token"}
