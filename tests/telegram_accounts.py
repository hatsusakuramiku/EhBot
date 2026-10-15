"""Known synthetic Bot identity for tests that exercise production account wiring."""
from functools import wraps

import httpx

from app.connections.accounts import AccountRegistry
from app.connections.runtime import current_account_context
from app.secrets import SecretStore


def with_fixture_bot(function):
    """Seed messages as a named fake Bot, preserving unknown legacy test data."""
    @wraps(function)
    async def seed(database, *args, **kwargs):
        await database.initialize()
        registry = AccountRegistry(database, SecretStore(database.path.parent / "private"))
        bots = [row for row in await registry.list_accounts() if row["kind"] == "TG_BOT"]
        if bots:
            account = bots[0]
        else:
            account = await registry.create("TG_BOT", "Fixture Bot", {"bot_token": "fixture-bot"})
            account = await registry.record_identity(account["account_id"], account["credential_revision"], "1", "@fixture_bot")
            account = await registry.enable(account["account_id"], expected_revision=account["revision"])
        token = current_account_context.set(await registry.context(account["account_id"]))
        try:
            return await function(database, *args, **kwargs)
        finally:
            current_account_context.reset(token)
    return seed


def bot_transport():
    """Keep startup verification, polling and downloads off the real network."""
    async def respond(request):
        if request.url.path.endswith("getMe"):
            return httpx.Response(200, json={"ok": True, "result": {"id": 1, "username": "fixture_bot", "first_name": "Fixture"}})
        if request.url.path.endswith("getUpdates"):
            import asyncio
            await asyncio.sleep(0.05)
            return httpx.Response(200, json={"ok": True, "result": []})
        if request.url.path.endswith("getFile"):
            return httpx.Response(200, json={"ok": True, "result": {"file_id": "fixture", "file_unique_id": "fixture", "file_path": "fixture.zip"}})
        return httpx.Response(200, content=b"PK-fixture")
    return httpx.MockTransport(respond)
