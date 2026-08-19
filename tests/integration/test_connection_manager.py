import asyncio
from pathlib import Path
import sqlite3

import httpx
import pytest

from app.candidates.ingestor import CandidateIngestor
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
                    "result": [
                        {
                            "update_id": 100,
                            "channel_post": {
                                "message_id": 80,
                                "date": 1_700_000_300,
                                "chat": {
                                    "id": -100123,
                                    "title": "Polling Channel",
                                },
                                "caption": "Polling Candidate",
                                "photo": [
                                    {
                                        "file_id": "poll-photo",
                                        "file_unique_id": "poll-photo-unique",
                                        "width": 800,
                                        "height": 1200,
                                    }
                                ],
                            },
                        }
                    ],
                },
            )
        await asyncio.Event().wait()
        raise AssertionError("unreachable")

    database = Database(tmp_path / "ehbot.db")
    await database.initialize()
    await database.configure_telegram_source(
        source_type="CHANNEL",
        chat_id=-100123,
        display_name="Polling Channel",
        enabled=True,
        allowed_archive_formats=("zip", "rar", "7z", "cbz"),
        max_attachment_size_mb=0,
    )
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
            candidate_ingestor=CandidateIngestor(database),
        )
        await manager.configure_telegram("123:secret")
        for _ in range(100):
            if await database.list_candidates():
                break
            await asyncio.sleep(0.01)

        snapshot = manager.snapshot()
        await manager.stop()

    assert store.is_configured("telegram_bot_token") is True
    assert snapshot.telegram.state == "connected"
    assert snapshot.telegram.identity == "@ehbot_intake_bot"
    assert await database.latest_telegram_update_id() == 100
    assert (await database.list_candidates())[0].title == "Polling Candidate"


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


@pytest.mark.asyncio
async def test_startup_processes_saved_updates_without_telegram_connection(
    tmp_path: Path,
) -> None:
    database = Database(tmp_path / "ehbot.db")
    await database.initialize()
    await database.configure_telegram_source(
        source_type="CHANNEL",
        chat_id=-100123,
        display_name="Offline Channel",
        enabled=True,
        allowed_archive_formats=("zip", "rar", "7z", "cbz"),
        max_attachment_size_mb=0,
    )
    await database.save_telegram_updates(
        [
            {
                "update_id": 400,
                "channel_post": {
                    "message_id": 70,
                    "date": 1_700_000_200,
                    "chat": {"id": -100123, "title": "Offline Channel"},
                    "caption": "Offline Candidate",
                    "photo": [
                        {
                            "file_id": "offline-photo",
                            "file_unique_id": "offline-photo-unique",
                            "width": 800,
                            "height": 1200,
                        }
                    ],
                },
            }
        ]
    )
    store = SecretStore(tmp_path / "private")
    async with httpx.AsyncClient() as client:
        manager = ConnectionManager(
            store,
            database,
            telegram_client=client,
            candidate_ingestor=CandidateIngestor(database),
        )
        await manager.start()
        candidates = await database.list_candidates()
        await manager.stop()

    assert len(candidates) == 1
    assert candidates[0].title == "Offline Candidate"


@pytest.mark.asyncio
async def test_candidate_storage_failure_sets_visible_connection_error(
    tmp_path: Path,
) -> None:
    update_sent = False

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal update_sent
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
        if not update_sent:
            update_sent = True
            return httpx.Response(
                200,
                json={
                    "ok": True,
                    "result": [
                        {
                            "update_id": 401,
                            "message": {
                                "message_id": 71,
                                "date": 1_700_000_201,
                                "chat": {"id": 900, "username": "fixture"},
                                "from": {"id": 900},
                                "text": "https://exhentai.org/g/99887/errorToken/",
                            },
                        }
                    ],
                },
            )
        await asyncio.Event().wait()
        raise AssertionError("unreachable")

    database = Database(tmp_path / "ehbot.db")
    await database.initialize()
    await database.configure_telegram_source(
        source_type="PRIVATE_CHAT",
        chat_id=900,
        display_name="Fixture Sender",
        enabled=True,
        allowed_archive_formats=("zip", "rar", "7z", "cbz"),
        max_attachment_size_mb=0,
    )
    with sqlite3.connect(database.path) as connection:
        connection.execute("DROP TABLE candidates")
    store = SecretStore(tmp_path / "private")
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url="https://api.telegram.org",
    ) as client:
        manager = ConnectionManager(
            store,
            database,
            telegram_client=client,
            candidate_ingestor=CandidateIngestor(database),
        )
        await manager.configure_telegram("123:secret")
        for _ in range(100):
            if manager.snapshot().telegram.state == "error":
                break
            await asyncio.sleep(0.01)
        snapshot = manager.snapshot()
        await manager.stop()

    assert snapshot.telegram.state == "error"
    assert snapshot.telegram.error == "消息处理失败，将自动重试"
