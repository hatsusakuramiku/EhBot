import asyncio
from pathlib import Path

import httpx
import pytest

from app.connections.manager import ConnectionManager
from app.connections.exhentai import ExHentaiCredentials
from app.db.database import Database
from app.secrets import SecretStore


@pytest.mark.asyncio
async def test_configuring_telegram_starts_durable_update_polling(
    tmp_path: Path,
) -> None:
    first_poll_completed = False

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal first_poll_completed
        if request.url.path.endswith("/getMe"):
            return httpx.Response(
                200,
                json={
                    "ok": True,
                    "result": {
                        "id": 42,
                        "is_bot": True,
                        "first_name": "EhBot Intake",
                        "username": "ehbot_intake_bot",
                    },
                },
            )
        if not first_poll_completed:
            first_poll_completed = True
            return httpx.Response(
                200,
                json={
                    "ok": True,
                    "result": [{"update_id": 100, "message": {"text": "hello"}}],
                },
            )
        await asyncio.Event().wait()
        raise AssertionError("unreachable")

    database = Database(tmp_path / "ehbot.db")
    await database.initialize()
    store = SecretStore(tmp_path / "private")
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url="https://api.telegram.org",
        timeout=40,
    ) as client:
        manager = ConnectionManager(store, database, telegram_client=client)
        await manager.configure_telegram("123:secret")
        for _ in range(100):
            if await database.latest_telegram_update_id() == 100:
                break
            await asyncio.sleep(0.01)

        snapshot = manager.snapshot()
        await manager.stop()

    assert store.is_configured("telegram_bot_token") is True
    assert snapshot.telegram.state == "connected"
    assert snapshot.telegram.identity == "@ehbot_intake_bot"
    assert await database.latest_telegram_update_id() == 100


@pytest.mark.asyncio
async def test_configuring_exhentai_persists_verified_cookie_session(
    tmp_path: Path,
) -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.url == "https://exhentai.org/"
        return httpx.Response(200, text="<html><title>ExHentai.org</title></html>")

    database = Database(tmp_path / "ehbot.db")
    await database.initialize()
    store = SecretStore(tmp_path / "private")
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        timeout=15,
    ) as client:
        manager = ConnectionManager(
            store,
            database,
            telegram_client=client,
            exhentai_client=client,
        )
        await manager.configure_exhentai(
            ExHentaiCredentials("10001", "pass-secret", "igneous-secret")
        )
        snapshot = manager.snapshot()
        await manager.stop()

    assert store.is_configured("exhentai_cookies") is True
    assert snapshot.exhentai.state == "connected"
    assert snapshot.exhentai.identity == "Member 10001"


@pytest.mark.asyncio
async def test_saved_telegram_token_reconnects_on_startup(tmp_path: Path) -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/getMe"):
            return httpx.Response(
                200,
                json={
                    "ok": True,
                    "result": {
                        "id": 42,
                        "is_bot": True,
                        "first_name": "EhBot Intake",
                        "username": "ehbot_intake_bot",
                    },
                },
            )
        await asyncio.Event().wait()
        raise AssertionError("unreachable")

    database = Database(tmp_path / "ehbot.db")
    await database.initialize()
    store = SecretStore(tmp_path / "private")
    store.write("telegram_bot_token", "123:saved-secret")
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url="https://api.telegram.org",
        timeout=40,
    ) as client:
        manager = ConnectionManager(store, database, telegram_client=client)
        await manager.start()
        snapshot = manager.snapshot()
        await manager.stop()

    assert snapshot.telegram.state == "connected"
    assert snapshot.telegram.identity == "@ehbot_intake_bot"


@pytest.mark.asyncio
async def test_saved_exhentai_session_reconnects_without_telegram(
    tmp_path: Path,
) -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.url == "https://exhentai.org/"
        return httpx.Response(200, text="<title>ExHentai.org</title>")

    database = Database(tmp_path / "ehbot.db")
    await database.initialize()
    store = SecretStore(tmp_path / "private")
    store.write(
        "exhentai_cookies",
        ExHentaiCredentials("10001", "pass-secret", "igneous-secret").to_json(),
    )
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), timeout=15
    ) as client:
        manager = ConnectionManager(
            store,
            database,
            telegram_client=client,
            exhentai_client=client,
        )
        await manager.start()
        snapshot = manager.snapshot()
        await manager.stop()

    assert snapshot.telegram.state == "not_configured"
    assert snapshot.exhentai.state == "connected"
    assert snapshot.exhentai.identity == "Member 10001"


@pytest.mark.asyncio
async def test_disconnect_removes_saved_provider_credentials(tmp_path: Path) -> None:
    poll_started = asyncio.Event()

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/getMe"):
            return httpx.Response(
                200,
                json={
                    "ok": True,
                    "result": {
                        "id": 42,
                        "is_bot": True,
                        "first_name": "EhBot Intake",
                        "username": "ehbot_intake_bot",
                    },
                },
            )
        if request.url.path.endswith("/getUpdates"):
            poll_started.set()
            await asyncio.Event().wait()
        return httpx.Response(200, text="<title>ExHentai.org</title>")

    database = Database(tmp_path / "ehbot.db")
    await database.initialize()
    store = SecretStore(tmp_path / "private")
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url="https://api.telegram.org",
        timeout=40,
    ) as client:
        manager = ConnectionManager(
            store,
            database,
            telegram_client=client,
            exhentai_client=client,
        )
        await manager.configure_telegram("123:secret")
        await manager.configure_exhentai(
            ExHentaiCredentials("10001", "pass-secret", "igneous-secret")
        )
        await asyncio.wait_for(poll_started.wait(), timeout=1)

        await manager.disconnect_telegram()
        await manager.disconnect_exhentai()
        snapshot = manager.snapshot()

    assert store.is_configured("telegram_bot_token") is False
    assert store.is_configured("exhentai_cookies") is False
    assert snapshot.telegram.state == "not_configured"
    assert snapshot.exhentai.state == "not_configured"
