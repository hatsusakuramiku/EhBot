import httpx
import pytest

from app.connections.telegram import TelegramBotApi
from app.connections.models import ProviderConnectionError


@pytest.mark.asyncio
async def test_telegram_bot_api_verifies_token_and_returns_identity() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/bot123:secret/getMe"
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

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url="https://api.telegram.org",
    ) as client:
        identity = await TelegramBotApi("123:secret", client).verify()

    assert identity.bot_id == 42
    assert identity.username == "ehbot_intake_bot"
    assert identity.display_name == "EhBot Intake"


@pytest.mark.asyncio
async def test_telegram_bot_api_reports_invalid_token_without_request_url() -> None:
    async def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"ok": False, "error_code": 401, "description": "Unauthorized"},
        )

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url="https://api.telegram.org",
    ) as client:
        with pytest.raises(ProviderConnectionError) as captured:
            await TelegramBotApi("123:must-not-leak", client).verify()

    assert captured.value.code == "TELEGRAM_UNAUTHORIZED"
    assert captured.value.public_message == "Bot Token 无效或已被撤销"
    assert "must-not-leak" not in str(captured.value)


@pytest.mark.asyncio
async def test_telegram_bot_api_requests_updates_after_persisted_offset() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/bot123:secret/getUpdates"
        assert request.url.params["offset"] == "101"
        assert request.url.params["timeout"] == "30"
        return httpx.Response(
            200,
            json={"ok": True, "result": [{"update_id": 101, "message": {}}]},
        )

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url="https://api.telegram.org",
    ) as client:
        updates = await TelegramBotApi("123:secret", client).get_updates(101)

    assert updates == [{"update_id": 101, "message": {}}]
