from __future__ import annotations

import asyncio
from dataclasses import replace
import logging
import sqlite3

import httpx

from app.candidates.ingestor import CandidateIngestor
from app.connections.exhentai import ExHentaiApi, ExHentaiCredentials
from app.connections.models import (
    ConnectionSnapshot,
    ProviderConnectionError,
    ProviderStatus,
    TelegramUserAccount,
)
from app.connections.telegram import TelegramBotApi
from app.connections.telegram_user import (
    LoginChallenge,
    TelegramUserClient,
    TelegramUserCredentials,
    TelegramUserError,
)
from app.db.database import Database
from app.secrets import SecretStore


# A 409 means another poller holds the token, so back off longer than a
# transient network error to avoid fighting over getUpdates.
#: Credential-store names for the MTProto account. The application identity and
#: the session are separate secrets because they have different lifetimes: a
#: session can be revoked from Telegram's own device list while the api pair
#: stays valid, and making the operator re-enter the api pair to recover from
#: that would be busy work.
TELEGRAM_USER_API_SECRET = "telegram_user_api"
TELEGRAM_USER_SESSION_SECRET = "telegram_user_session"


_POLL_BACKOFF_SECONDS: dict[str, int] = {
    "TELEGRAM_CONFLICT": 30,
    "TELEGRAM_FORBIDDEN": 30,
    "TELEGRAM_UNAUTHORIZED": 60,
    "TELEGRAM_SERVER_ERROR": 15,
}


class ConnectionManager:
    def __init__(
        self,
        secret_store: SecretStore,
        database: Database,
        *,
        telegram_client: httpx.AsyncClient,
        exhentai_client: httpx.AsyncClient | None = None,
        candidate_ingestor: CandidateIngestor | None = None,
        user_client_factory=None,
        on_candidates_ingested=None,
    ) -> None:
        self._secret_store = secret_store
        self._database = database
        self._telegram_client = telegram_client
        self._exhentai_client = exhentai_client
        self._candidate_ingestor = candidate_ingestor
        # Called after an ingest that created candidates, so an automatic
        # approval rule fires as the book arrives rather than at the next sweep.
        # A callable rather than the sweeper itself because this class polls
        # Telegram and knows nothing about review policy -- and because the
        # sweeper is built during the lifespan, after this is constructed.
        self._on_candidates_ingested = on_candidates_ingested
        self._telegram_task: asyncio.Task[None] | None = None
        self._telegram_status = ProviderStatus(
            state="not_configured", configured=False
        )
        self._exhentai_status = ProviderStatus(
            state="not_configured", configured=False
        )
        self._telegram_lock = asyncio.Lock()
        # Injected so tests can drive a login without Telethon or a network:
        # None means「build a real client」, which is what production passes.
        self._user_client_factory = user_client_factory
        self._telegram_user = TelegramUserAccount(
            state="not_configured", configured=False
        )
        # The pending login, held only in memory. A code expires in minutes, so
        # a challenge that survived a restart would be a challenge that cannot
        # be completed -- persisting it would only make the interface offer a
        # dead form.
        self._user_challenge: LoginChallenge | None = None
        self._user_lock = asyncio.Lock()

    def snapshot(self) -> ConnectionSnapshot:
        return ConnectionSnapshot(
            telegram=self._telegram_status,
            exhentai=self._exhentai_status,
            telegram_user=self._telegram_user,
        )

    def user_download_available(self) -> bool:
        """Whether an oversized attachment can be fetched right now.

        Read by the review orchestrator per routing decision rather than
        captured once: an operator can log the account in while candidates are
        already waiting, and the next approval has to see it.
        """
        return self._telegram_user.state == "connected"

    async def _ingest_pending(self) -> None:
        """Drain the update backlog, then let a rule act on what it produced.

        The two callers -- startup and the poll loop -- both need the pair, and
        having it in one place is what stops a new candidate from being swept on
        one path and not the other.

        The callback's failure is contained here: ingestion has already been
        committed by this point, so an approval that raises must not roll the
        poll loop back or stop it polling. The candidate simply stays pending
        until the timed sweep reaches it, which is the same outcome as having no
        rule.
        """
        if self._candidate_ingestor is None:
            return
        summary = await self._candidate_ingestor.process_pending_updates()
        if (
            not summary.created_candidates
            or self._on_candidates_ingested is None
        ):
            return
        try:
            await self._on_candidates_ingested()
        except Exception:  # noqa: BLE001 - ingestion must not fail on review policy
            logging.getLogger(__name__).exception(
                "auto_approval_after_ingest_failed",
                extra={"error_code": "AUTO_APPROVAL_AFTER_INGEST_FAILED"},
            )

    async def start(self) -> None:
        await self._ingest_pending()
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
        await self._restore_telegram_user()
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

    async def _read_user_credentials(self) -> TelegramUserCredentials | None:
        """The stored api_id/api_hash pair, or None when absent or unparsable.

        None for both cases on purpose: a fresh install and a blob this version
        can no longer read mean the same thing to every caller -- there is no
        user account, fall back to the bot.
        """
        raw = await asyncio.to_thread(
            self._secret_store.read, TELEGRAM_USER_API_SECRET
        )
        if not raw:
            return None
        api_id, _, api_hash = raw.partition(":")
        try:
            return TelegramUserCredentials.parse(api_id, api_hash)
        except TelegramUserError:
            return None

    def _user_client(
        self, credentials: TelegramUserCredentials, session: str | None
    ) -> TelegramUserClient:
        return TelegramUserClient(
            credentials, session, client_factory=self._user_client_factory
        )

    async def _restore_telegram_user(self) -> None:
        """Re-verify a stored session at startup, without blocking the boot.

        A revoked session must show up as「连接异常」on the connections tab rather
        than as a job that fails hours later, and a Telegram outage at boot must
        not stop the rest of the service from starting -- so the failure is
        recorded in the snapshot and nothing is raised.
        """
        credentials = await self._read_user_credentials()
        session = await asyncio.to_thread(
            self._secret_store.read, TELEGRAM_USER_SESSION_SECRET
        )
        if credentials is None or not session:
            self._telegram_user = TelegramUserAccount(
                state="not_configured",
                configured=credentials is not None,
            )
            return
        try:
            identity = await self._user_client(credentials, session).verify()
        except ProviderConnectionError as exc:
            self._telegram_user = TelegramUserAccount(
                state="error", configured=True, error=exc.public_message
            )
            return
        self._telegram_user = TelegramUserAccount(
            state="connected", configured=True, identity=identity.label
        )

    async def start_telegram_user_login(
        self, api_id: str, api_hash: str, phone: str
    ) -> None:
        """Store the application identity and request a login code.

        The api pair is written before the code is requested because Telegram
        validates it as part of sending the code: a pair that gets that far is
        known good, and keeping it means a mistyped *code* does not cost the
        operator the api fields as well.
        """
        async with self._user_lock:
            credentials = TelegramUserCredentials.parse(api_id, api_hash)
            self._telegram_user = TelegramUserAccount(
                state="not_configured",
                configured=self._secret_store.is_configured(
                    TELEGRAM_USER_SESSION_SECRET
                ),
            )
            client = self._user_client(credentials, None)
            try:
                challenge = await client.send_code(phone)
            except ProviderConnectionError as exc:
                self._telegram_user = TelegramUserAccount(
                    state="error",
                    configured=self._secret_store.is_configured(
                        TELEGRAM_USER_SESSION_SECRET
                    ),
                    error=exc.public_message,
                )
                raise
            await asyncio.to_thread(
                self._secret_store.write,
                TELEGRAM_USER_API_SECRET,
                f"{credentials.api_id}:{credentials.api_hash}",
            )
            self._user_challenge = challenge
            self._telegram_user = TelegramUserAccount(
                state="awaiting_code",
                configured=True,
                phone=challenge.phone,
            )

    async def complete_telegram_user_login(
        self, code: str | None = None, password: str | None = None
    ) -> None:
        """Finish the pending login with a code, or with a 2FA password.

        A `SessionPasswordNeededError` is not an error the operator caused, so it
        parks the login in `awaiting_password` and keeps the challenge: the next
        submission carries only the password, and the code does not have to be
        requested again.
        """
        async with self._user_lock:
            challenge = self._user_challenge
            if challenge is None:
                raise TelegramUserError(
                    "TELEGRAM_USER_NO_CHALLENGE",
                    "登录流程已失效，请重新获取验证码",
                )
            credentials = await self._read_user_credentials()
            if credentials is None:
                raise TelegramUserError(
                    "TELEGRAM_USER_NOT_CONFIGURED",
                    "尚未保存 API ID 与 API Hash，请重新开始登录",
                )
            client = self._user_client(credentials, None)
            try:
                session, identity = await client.sign_in(
                    challenge, code=code, password=password
                )
            except ProviderConnectionError as exc:
                if exc.code == "TELEGRAM_USER_PASSWORD_NEEDED":
                    self._user_challenge = replace(
                        challenge, requires_password=True
                    )
                    self._telegram_user = TelegramUserAccount(
                        state="awaiting_password",
                        configured=True,
                        phone=challenge.phone,
                    )
                    raise
                self._telegram_user = TelegramUserAccount(
                    state=(
                        "awaiting_password"
                        if challenge.requires_password
                        else "awaiting_code"
                    ),
                    configured=True,
                    phone=challenge.phone,
                    error=exc.public_message,
                )
                raise
            await asyncio.to_thread(
                self._secret_store.write,
                TELEGRAM_USER_SESSION_SECRET,
                session,
            )
            self._user_challenge = None
            self._telegram_user = TelegramUserAccount(
                state="connected", configured=True, identity=identity.label
            )

    async def disconnect_telegram_user(self) -> None:
        """Forget the session and the api pair, and drop any pending login.

        Both secrets go, not just the session: 「断开」 on this panel means the
        deployment no longer holds a credential for that account, and leaving the
        api pair behind would show a half-configured state nobody asked for.
        """
        async with self._user_lock:
            self._user_challenge = None
            await asyncio.to_thread(
                self._secret_store.delete, TELEGRAM_USER_SESSION_SECRET
            )
            await asyncio.to_thread(
                self._secret_store.delete, TELEGRAM_USER_API_SECRET
            )
            self._telegram_user = TelegramUserAccount(
                state="not_configured", configured=False
            )

    async def telegram_user_context(self):
        """The credentials and session the download path needs, or None.

        Read per job rather than captured: an operator can log in, or the session
        can be revoked, between one delivery and the next.
        """
        credentials = await self._read_user_credentials()
        session = await asyncio.to_thread(
            self._secret_store.read, TELEGRAM_USER_SESSION_SECRET
        )
        if credentials is None or not session:
            return None
        return self._user_client(credentials, session)

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
                    await self._ingest_pending()
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
                await asyncio.sleep(
                    exc.retry_after
                    if exc.retry_after is not None
                    else _POLL_BACKOFF_SECONDS.get(exc.code, 5)
                )

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
