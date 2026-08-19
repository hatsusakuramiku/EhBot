from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import shutil

import httpx

from app.connections.models import ProviderConnectionError, TelegramBotIdentity


@dataclass(frozen=True, slots=True)
class TelegramFile:
    file_id: str
    file_unique_id: str
    file_path: str
    file_size: int | None


class TelegramBotApi:
    def __init__(self, token: str, client: httpx.AsyncClient) -> None:
        self._token = token
        self._client = client

    async def _request(self, method: str, **params) -> dict:
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

    async def get_file(self, file_id: str) -> TelegramFile:
        payload = await self._request("getFile", file_id=file_id)
        result = payload["result"]
        return TelegramFile(
            file_id=str(result["file_id"]),
            file_unique_id=str(
                result.get("file_unique_id") or result["file_id"]
            ),
            file_path=str(result["file_path"]),
            file_size=(
                int(result["file_size"])
                if result.get("file_size") is not None
                else None
            ),
        )

    async def download_file(
        self, file_path: str, destination: Path
    ) -> int:
        url = f"/file/bot{self._token}/{file_path}"
        destination.parent.mkdir(parents=True, exist_ok=True)
        try:
            async with self._client.stream(
                "GET", url, follow_redirects=True
            ) as response:
                response.raise_for_status()
                with destination.open("wb") as target:
                    copied = 0
                    async for chunk in response.aiter_bytes(
                        chunk_size=64 * 1024
                    ):
                        if not chunk:
                            continue
                        target.write(chunk)
                        copied += len(chunk)
            return copied
        except (httpx.HTTPError, OSError):
            destination.unlink(missing_ok=True)
            raise ProviderConnectionError(
                "TELEGRAM_DOWNLOAD_FAILED", "Telegram 文件下载失败"
            ) from None


__all__ = ["TelegramBotApi", "TelegramFile"]