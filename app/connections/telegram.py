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
        except httpx.HTTPError:
            raise ProviderConnectionError(
                "TELEGRAM_UNREACHABLE", "暂时无法连接 Telegram"
            ) from None
        try:
            payload = response.json()
        except ValueError:
            payload = {}
        if not isinstance(payload, dict):
            payload = {}
        if response.status_code != 200 or not payload.get("ok"):
            raise self._error_for(response.status_code, payload)
        return payload

    @staticmethod
    def _error_for(
        status_code: int, payload: dict
    ) -> ProviderConnectionError:
        """Map a Telegram error response to a specific connection error."""
        error_code = payload.get("error_code")
        if isinstance(error_code, int):
            status_code = error_code
        parameters = payload.get("parameters")
        retry_after = None
        if isinstance(parameters, dict):
            raw_retry = parameters.get("retry_after")
            if isinstance(raw_retry, int):
                retry_after = raw_retry
        if status_code == 401:
            return ProviderConnectionError(
                "TELEGRAM_UNAUTHORIZED", "Bot Token 无效或已被撤销"
            )
        if status_code == 409:
            return ProviderConnectionError(
                "TELEGRAM_CONFLICT",
                "该 Bot Token 正被其他程序轮询或已设置 Webhook。"
                "请关闭另一个 EhBot 实例，或删除 Webhook 后重试",
            )
        if status_code == 429:
            wait_hint = (
                f"，请等待约 {retry_after} 秒" if retry_after else ""
            )
            return ProviderConnectionError(
                "TELEGRAM_RATE_LIMITED",
                f"Telegram 触发限流{wait_hint}",
                retry_after=retry_after,
            )
        if status_code == 403:
            return ProviderConnectionError(
                "TELEGRAM_FORBIDDEN",
                "Telegram 拒绝访问，请确认 Bot 仍在该频道且具备权限",
            )
        if status_code >= 500:
            return ProviderConnectionError(
                "TELEGRAM_SERVER_ERROR", "Telegram 服务端暂时不可用"
            )
        description = payload.get("description")
        description = description if isinstance(description, str) else ""
        if "file is too big" in description.lower():
            # The Bot API cannot download files larger than 20 MB. This is a
            # hard protocol ceiling, not a transient fault, so retrying can
            # never succeed and the job must say so.
            return ProviderConnectionError(
                "TELEGRAM_FILE_TOO_BIG",
                "文件超过 Telegram Bot API 的 20 MB 下载上限，Bot 无法取回该文件；"
                "可在「外部连接」登录 Telegram 用户账户后改用大文件来源，或改用 EH 种子 / ExHentai 源",
            )
        if description:
            # Preserve Telegram's own wording. The bare fallback used to hide
            # the real reason and made every 400 look like a network problem.
            return ProviderConnectionError(
                "TELEGRAM_REJECTED",
                f"Telegram 拒绝了请求：{description}",
            )
        return ProviderConnectionError(
            "TELEGRAM_REJECTED", "Telegram 拒绝了连接请求"
        )

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