from __future__ import annotations

import httpx

from app.connections.models import ProviderConnectionError, TelegramBotIdentity


class TelegramBotApi:
    def __init__(self, token: str, client: httpx.AsyncClient) -> None:
        self._token = token
        self._client = client

    async def _request(self, method: str, **params: int) -> dict:
        try:
            response = await self._client.get(
                f"/bot{self._token}/{method}", params=params
            )
            response.raise_for_status()
            payload = response.json()
        except (httpx.HTTPError, ValueError):
            raise ProviderConnectionError(
                "TELEGRAM_UNREACHABLE", "暂时无法连接 Telegram"
            ) from None
        if not payload.get("ok"):
            code = (
                "TELEGRAM_UNAUTHORIZED"
                if payload.get("error_code") == 401
                else "TELEGRAM_REJECTED"
            )
            message = (
                "Bot Token 无效或已被撤销"
                if code == "TELEGRAM_UNAUTHORIZED"
                else "Telegram 拒绝了连接请求"
            )
            raise ProviderConnectionError(code, message)
        return payload

    async def verify(self) -> TelegramBotIdentity:
        payload = await self._request("getMe")
        result = payload["result"]
        return TelegramBotIdentity(
            bot_id=int(result["id"]),
            username=str(result["username"]),
            display_name=str(result["first_name"]),
        )

    async def get_updates(
        self, offset: int | None, *, timeout: int = 30
    ) -> list[dict]:
        params = {"timeout": timeout}
        if offset is not None:
            params["offset"] = offset
        payload = await self._request("getUpdates", **params)
        return list(payload["result"])
