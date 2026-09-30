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
@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status_code", "payload", "expected_code"),
    [
        (
            409,
            {
                "ok": False,
                "error_code": 409,
                "description": "Conflict: terminated by other getUpdates request",
            },
            "TELEGRAM_CONFLICT",
        ),
        (
            403,
            {"ok": False, "error_code": 403, "description": "Forbidden"},
            "TELEGRAM_FORBIDDEN",
        ),
        (
            500,
            {"ok": False, "error_code": 500, "description": "Internal Server Error"},
            "TELEGRAM_SERVER_ERROR",
        ),
        (
            400,
            {"ok": False, "error_code": 400, "description": "Bad Request"},
            "TELEGRAM_REJECTED",
        ),
    ],
)
async def test_telegram_bot_api_maps_http_errors_to_specific_codes(
    status_code: int, payload: dict, expected_code: str
) -> None:
    async def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(status_code, json=payload)

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url="https://api.telegram.org",
    ) as client:
        with pytest.raises(ProviderConnectionError) as captured:
            await TelegramBotApi("123:secret", client).get_updates(None)

    assert captured.value.code == expected_code
    assert "123:secret" not in str(captured.value)


@pytest.mark.asyncio
async def test_telegram_bot_api_surfaces_rate_limit_retry_after() -> None:
    async def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            429,
            json={
                "ok": False,
                "error_code": 429,
                "description": "Too Many Requests",
                "parameters": {"retry_after": 17},
            },
        )

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url="https://api.telegram.org",
    ) as client:
        with pytest.raises(ProviderConnectionError) as captured:
            await TelegramBotApi("123:secret", client).get_updates(None)

    assert captured.value.code == "TELEGRAM_RATE_LIMITED"
    assert captured.value.retry_after == 17
    assert "17" in captured.value.public_message


@pytest.mark.asyncio
async def test_a_completed_download_replaces_the_file(tmp_path) -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/file/bot123:secret/documents/book.zip"
        return httpx.Response(200, content=b"new-archive")

    destination = tmp_path / "book.zip"
    destination.write_bytes(b"old-archive")
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url="https://api.telegram.org",
    ) as client:
        size = await TelegramBotApi("123:secret", client).download_file(
            "documents/book.zip", destination
        )

    assert size == len(b"new-archive")
    assert destination.read_bytes() == b"new-archive"
    assert not (tmp_path / "book.zip.part").exists()


@pytest.mark.asyncio
async def test_a_failed_download_keeps_the_previous_file(tmp_path) -> None:
    """A retry reuses the job row, so it writes the same path as before.

    Streaming straight into `destination` meant a failed re-download truncated
    -- and then deleted -- the archive the artifact row still referenced.
    """

    async def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="boom")

    destination = tmp_path / "book.zip"
    destination.write_bytes(b"previous-archive")
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url="https://api.telegram.org",
    ) as client:
        with pytest.raises(ProviderConnectionError) as caught:
            await TelegramBotApi("123:secret", client).download_file(
                "documents/book.zip", destination
            )

    assert caught.value.code == "TELEGRAM_DOWNLOAD_FAILED"
    assert destination.read_bytes() == b"previous-archive"
    assert not (tmp_path / "book.zip.part").exists()


@pytest.mark.asyncio
async def test_telegram_bot_api_reports_transport_failure_as_unreachable() -> None:
    async def handler(_: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("dns failure")

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url="https://api.telegram.org",
    ) as client:
        with pytest.raises(ProviderConnectionError) as captured:
            await TelegramBotApi("123:secret", client).verify()

    assert captured.value.code == "TELEGRAM_UNREACHABLE"
