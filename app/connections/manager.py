from __future__ import annotations

import asyncio
import logging
import sqlite3

import httpx

from app.candidates.ingestor import CandidateIngestor
from app.connections.exhentai import ExHentaiApi, ExHentaiCredentials
from app.connections.models import (
    ConnectionSnapshot,
    ProviderConnectionError,
    ProviderStatus,
)
from app.connections.telegram import TelegramBotApi
from app.db.database import Database
from app.secrets import SecretStore


class ConnectionManager:
    def __init__(
        self,
        secret_store: SecretStore,
        database: Database,
        *,
        telegram_client: httpx.AsyncClient,
        exhentai_client: httpx.AsyncClient | None = None,
        candidate_ingestor: CandidateIngestor | None = None,
    ) -> None:
        self._secret_store = secret_store
        self._database = database
        self._telegram_client = telegram_client
        self._exhentai_client = exhentai_client
        self._candidate_ingestor = candidate_ingestor
        self._telegram_task: asyncio.Task[None] | None = None
        self._telegram_status = ProviderStatus(
            state="not_configured", configured=False
        )
        self._exhentai_status = ProviderStatus(
            state="not_configured", configured=False
        )
        self._telegram_lock = asyncio.Lock()

    def snapshot(self) -> ConnectionSnapshot:
        return ConnectionSnapshot(
            telegram=self._telegram_status,
            exhentai=self._exhentai_status,
        )

    async def start(self) -> None:
        if self._candidate_ingestor is not None:
            await self._candidate_ingestor.process_pending_updates()
        token = await asyncio.to_thread(
            self._secret_store.read, "telegram_bot_token"
        )
        if token is not None:
            try:
                await self.configure_telegram(token)
            except ProviderConnectionError as exc:
                self._telegram_status = ProviderStatus(
                    state="error",
                    configured=True,
                    error=exc.public_message,
                )
        cookies = await asyncio.to_thread(
            self._secret_store.read, "exhentai_cookies"
        )
        if cookies is not None:
            try:
                await self.configure_exhentai(ExHentaiCredentials.from_json(cookies))
            except (ProviderConnectionError, ValueError, KeyError) as exc:
                message = (
                    exc.public_message
                    if isinstance(exc, ProviderConnectionError)
                    else "ExHentai 配置文件无效"
                )
                self._exhentai_status = ProviderStatus(
                    state="error",
                    configured=True,
                    error=message,
                )

    async def configure_telegram(self, token: str) -> None:
        async with self._telegram_lock:
            self._telegram_status = ProviderStatus(
                state="connecting",
                configured=self._secret_store.is_configured("telegram_bot_token"),
            )
            api = TelegramBotApi(token.strip(), self._telegram_client)
            try:
                identity = await api.verify()
            except ProviderConnectionError as exc:
                self._telegram_status = ProviderStatus(
                    state="error",
                    configured=self._secret_store.is_configured(
                        "telegram_bot_token"
                    ),
                    error=exc.public_message,
                )
                raise
            await asyncio.to_thread(
                self._secret_store.write, "telegram_bot_token", token.strip()
            )
            await self._cancel_telegram_task()
            self._telegram_status = ProviderStatus(
                state="connected",
                configured=True,
                identity=f"@{identity.username}",
            )
            self._telegram_task = asyncio.create_task(
                self._poll_telegram(api), name="telegram-bot-poll"
            )

    async def configure_exhentai(
        self, credentials: ExHentaiCredentials
    ) -> None:
        if self._exhentai_client is None:
            raise RuntimeError("ExHentai HTTP client is not configured")
        self._exhentai_status = ProviderStatus(
            state="connecting",
            configured=self._secret_store.is_configured("exhentai_cookies"),
        )
        try:
            identity = await ExHentaiApi(
                credentials, self._exhentai_client
            ).verify()
        except ProviderConnectionError as exc:
            self._exhentai_status = ProviderStatus(
                state="error",
                configured=self._secret_store.is_configured("exhentai_cookies"),
                error=exc.public_message,
            )
            raise
        await asyncio.to_thread(
            self._secret_store.write,
            "exhentai_cookies",
            credentials.to_json(),
        )
        self._exhentai_status = ProviderStatus(
            state="connected",
            configured=True,
            identity=identity,
        )

    async def _poll_telegram(self, api: TelegramBotApi) -> None:
        latest_update_id = await self._database.latest_telegram_update_id()
        offset = latest_update_id + 1 if latest_update_id is not None else None
        while True:
            try:
                updates = await api.get_updates(offset)
                if updates:
                    await self._database.save_telegram_updates(updates)
                    if self._candidate_ingestor is not None:
                        await self._candidate_ingestor.process_pending_updates()
                    offset = max(int(update["update_id"]) for update in updates) + 1
                else:
                    await asyncio.sleep(0.05)
                if self._telegram_status.state == "error":
                    self._telegram_status = ProviderStatus(
                        state="connected",
                        configured=True,
                        identity=self._telegram_status.identity,
                    )
            except asyncio.CancelledError:
                raise
            except sqlite3.Error:
                self._telegram_status = ProviderStatus(
                    state="error",
                    configured=True,
                    identity=self._telegram_status.identity,
                    error="消息处理失败，将自动重试",
                )
                logging.getLogger(__name__).error(
                    "telegram_ingest_failed",
                    extra={"error_code": "TELEGRAM_INGEST_FAILED"},
                )
                await asyncio.sleep(5)
            except ProviderConnectionError as exc:
                self._telegram_status = ProviderStatus(
                    state="error",
                    configured=True,
                    identity=self._telegram_status.identity,
                    error=exc.public_message,
                )
                logging.getLogger(__name__).warning(
                    "telegram_poll_failed", extra={"error_code": exc.code}
                )
                await asyncio.sleep(5)

    async def _cancel_telegram_task(self) -> None:
        if self._telegram_task is None:
            return
        self._telegram_task.cancel()
        await asyncio.gather(self._telegram_task, return_exceptions=True)
        self._telegram_task = None

    async def disconnect_telegram(self) -> None:
        async with self._telegram_lock:
            await self._cancel_telegram_task()
            await asyncio.to_thread(
                self._secret_store.delete, "telegram_bot_token"
            )
            self._telegram_status = ProviderStatus(
                state="not_configured", configured=False
            )

    async def disconnect_exhentai(self) -> None:
        await asyncio.to_thread(self._secret_store.delete, "exhentai_cookies")
        self._exhentai_status = ProviderStatus(
            state="not_configured", configured=False
        )

    async def stop(self) -> None:
        await self._cancel_telegram_task()
