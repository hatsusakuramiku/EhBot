from __future__ import annotations

import asyncio
from dataclasses import replace
import logging
import sqlite3

import httpx

from app.candidates import mtproto
from app.candidates.ingestor import CandidateIngestor
from app.candidates.rules import evaluate_source_rules
from app.connections.exhentai import ExHentaiApi, ExHentaiCredentials
from app.connections.models import (
    ConnectionSnapshot,
    ProviderConnectionError,
    ProviderStatus,
    TelegramUserAccount,
    refusal_detail,
)
from app.connections.telegram import TelegramBotApi
from app.connections.telegram_user import (
    EntityIndex,
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


#: How often the user account checks its channels for new messages, and how
#: many messages one chat may hand over per pass. Ten seconds keeps an arrival
#: feeling immediate without turning `messages.getHistory` into a busy loop; the
#: batch bounds one poll so a channel that was quiet for a week cannot stall the
#: loop behind a single enormous read.
_USER_INGEST_INTERVAL_SECONDS = 10.0
_USER_INGEST_BATCH = 100

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
        # The MTProto ingester, when a user account is logged in. Separate from
        # `_telegram_task` because the two are independent: either can run with
        # the other absent, and both may run at once.
        self._user_ingest_task: asyncio.Task[None] | None = None
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
        # Shared by every client this manager builds -- the ingest loop and the
        # download worker alike -- because a fresh client starts with Telethon's
        # entity cache empty and cannot resolve a channel id until it has read
        # the account's dialog list once.
        self._user_entities = EntityIndex()
        #: Enabled chats the account could not resolve at the last capability
        #: check, and the enabled set that check ran against. A source the
        #: account is not in does not become readable between two polls ten
        #: seconds apart, so it is reported once and then skipped instead of
        #: being retried -- and re-logged -- on every pass.
        self._user_unreadable: dict[int, str] = {}
        self._user_checked_chats: tuple[int, ...] | None = None

    def telegram_available(self) -> bool:
        """Whether the Bot API route is worth queueing a job for.

        `not_configured` deliberately still counts as available: the only way an
        attachment ever carries a `file_id` is that the Bot API reported it, so
        an id is proof a bot existed when the message was ingested -- and
        treating 「no token right now」 as 「cannot fetch」 would strand every
        work already in the database. `error` is the state that means stop: a
        token is configured and the API is refusing it, which is exactly when
        the user account should take over.
        """
        return self._telegram_status.state != "error"

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

    def note_sources_changed(self) -> None:
        """Make the next poll re-check which sources this account can read.

        Called when the operator saves a source. The enabled set alone cannot
        see 「the same source, saved again after joining the channel」 -- no
        field of the row changed -- and that re-save is exactly how the
        operator asks a deployment to look afresh, so the save has to say so
        rather than rely on a diff that will not move.
        """
        self._user_checked_chats = None
        self._user_unreadable = {}

    def _forget_user_chats(self) -> None:
        """Drop everything learned about the previous account's chats.

        Called when the session changes: another account has different access
        hashes, so both the resolved entities and the capability verdict are
        about a session that no longer exists.
        """
        self._user_entities.clear()
        self._user_unreadable = {}
        self._user_checked_chats = None

    def _user_client(
        self, credentials: TelegramUserCredentials, session: str | None
    ) -> TelegramUserClient:
        return TelegramUserClient(
            credentials,
            session,
            client_factory=self._user_client_factory,
            entity_index=self._user_entities,
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
        self._ensure_user_ingest_task()

    def _ensure_user_ingest_task(self) -> None:
        """Start the MTProto ingester if it should be running and is not.

        Called at startup, after a successful login, and by nothing else: the
        task itself is long-lived and exits only on cancel, so 「is it already
        running」 is the whole guard. No candidate ingestor means this deployment
        has no review pipeline wired (a test), and there is nothing to feed.
        """
        if self._candidate_ingestor is None:
            return
        if self._telegram_user.state != "connected":
            return
        if (
            self._user_ingest_task is not None
            and not self._user_ingest_task.done()
        ):
            return
        self._user_ingest_task = asyncio.create_task(
            self._run_user_ingest(), name="telegram-user-ingest"
        )

    async def _run_user_ingest(self) -> None:
        """Poll the configured channels with the operator's own account.

        The bot is not the only way a work arrives. A deployment that runs no
        bot still has an account that can read the channels it belongs to, and
        without this loop such a deployment ingests nothing at all -- the
        account could download a book but never learn one existed.

        It runs *alongside* the bot when both are configured, which is safe
        because both paths write through `save_candidate_message`: the unique
        key on `(account_id, chat_id, message_id)` is what makes the second
        reading of a message a no-op instead of a second candidate.
        """
        while True:
            try:
                await self._ingest_with_user_account()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - the loop must outlive a bad poll
                logging.getLogger(__name__).exception(
                    "telegram_user_ingest_failed",
                    extra={"error_code": "TELEGRAM_USER_INGEST_FAILED"},
                )
            await asyncio.sleep(_USER_INGEST_INTERVAL_SECONDS)

    async def _ingest_with_user_account(self) -> int:
        """One pass over every enabled source. Returns how many works it added."""
        if self._candidate_ingestor is None:
            return 0
        if self._telegram_user.state != "connected":
            return 0
        credentials = await self._read_user_credentials()
        session = await asyncio.to_thread(
            self._secret_store.read, TELEGRAM_USER_SESSION_SECRET
        )
        if credentials is None or not session:
            return 0
        client = self._user_client(credentials, session)
        targets = await self._database.telegram_ingest_targets()
        if not targets:
            return 0
        # Which sources this account can read is a property of the session and
        # the membership list, not of this particular poll, so it is checked
        # only when something could have moved it -- a changed enabled set, or
        # a source save that said so through `note_sources_changed`. A channel
        # the account is not in then costs one line at check time instead of
        # one on every pass.
        enabled = tuple(sorted(int(target["chat_id"]) for target in targets))
        if enabled != self._user_checked_chats:
            if not await self._check_user_sources(client, targets, enabled):
                return 0
        created = 0
        for target in targets:
            if int(target["chat_id"]) in self._user_unreadable:
                continue
            try:
                created += await self._ingest_source(client, target)
            except ProviderConnectionError as exc:
                # A failure here is news even for a source the check passed: a
                # rate limit or an outage is transient, unlike「the account is
                # not in this channel」, which `_check_user_sources` reports once
                # and stops retrying. One unreachable chat must not stop the
                # others either.
                logging.getLogger(__name__).warning(
                    "telegram_user_ingest_source_failed",
                    extra={
                        "error_code": exc.code,
                        "error_message": exc.public_message,
                        "error_detail": refusal_detail(exc),
                        "chat_id": target["chat_id"],
                    },
                )
        if created:
            await self._notify_ingested()
        return created

    async def _check_user_sources(
        self,
        client: TelegramUserClient,
        targets: list[dict],
        enabled: tuple[int, ...],
    ) -> bool:
        """Decide once which of the enabled sources this account can read.

        One dialog pass answers it for every source at once, because the list
        of chats the account is in *is* the membership test. A source that is
        not in it is reported here, once, and then skipped until the deployment
        is asked to look again: a source saved, another account logged in, or a
        restart.

        Returns False when the check itself could not run, in which case this
        pass is abandoned: assuming every source is fine would only turn a
        connection problem into a burst of per-source failures.
        """
        logger = logging.getLogger(__name__)
        try:
            readable = await client.load_dialogs()
        except ProviderConnectionError as exc:
            logger.warning(
                "telegram_user_sources_check_failed",
                extra={
                    "error_code": exc.code,
                    "error_message": exc.public_message,
                    "error_detail": refusal_detail(exc),
                },
            )
            return False
        unreadable: dict[int, str] = {}
        for target in targets:
            chat_id = int(target["chat_id"])
            if chat_id in readable:
                continue
            unreadable[chat_id] = str(target.get("display_name") or "")
            logger.warning(
                "telegram_user_source_unreadable",
                extra={
                    "error_code": "TELEGRAM_USER_ENTITY_UNRESOLVED",
                    "error_message": (
                        "登录账户不在该来源中，轮询已跳过；把账户加入频道后"
                        "重新保存该来源或重启即可重新检测"
                    ),
                    "chat_id": chat_id,
                },
            )
        self._user_unreadable = unreadable
        self._user_checked_chats = enabled
        return True

    async def _ingest_source(self, client: TelegramUserClient, target: dict) -> int:
        chat_id = int(target["chat_id"])
        cursor = int(target["cursor"] or 0)
        if cursor <= 0:
            newest = await client.latest_message_id(chat_id)
            if newest is None:
                return 0
            await self._database.set_source_cursor(chat_id, newest)
            logging.getLogger(__name__).info(
                "telegram_user_ingest_seeded",
                extra={
                    "error_code": "TELEGRAM_USER_INGEST_SEEDED",
                    "chat_id": chat_id,
                    "message_id": newest,
                },
            )
            return 0
        raw_messages = await client.fetch_channel_messages(
            chat_id, after_id=cursor, limit=_USER_INGEST_BATCH
        )
        created = 0
        highest = cursor
        for raw in raw_messages:
            highest = max(highest, int(getattr(raw, "id", 0) or 0))
            message = mtproto.parse_user_message(raw)
            if message is None:
                continue
            source = await self._database.discover_telegram_source(message)
            decision = evaluate_source_rules(source, message)
            if decision.result == "IGNORE":
                continue
            message = replace(
                message,
                filter_result=decision.result,
                filter_reason=decision.reason,
            )
            # `None` rather than an update id: this message did not arrive
            # through `getUpdates`, so there is no row to mark -- the parameter
            # exists for the bot path's bookkeeping, which this path has no use
            # for.
            if await self._database.save_candidate_message(None, message):
                created += 1
        if highest > cursor:
            await self._database.set_source_cursor(chat_id, highest)
        return created

    async def _notify_ingested(self) -> None:
        """Let the automatic-approval rule act on what just arrived."""
        if self._on_candidates_ingested is None:
            return
        try:
            await self._on_candidates_ingested()
        except Exception:  # noqa: BLE001 - ingestion must not fail on review policy
            logging.getLogger(__name__).exception(
                "auto_approval_after_ingest_failed",
                extra={"error_code": "AUTO_APPROVAL_AFTER_INGEST_FAILED"},
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
            # A different account has different access hashes, so nothing
            # resolved for the previous session can be reused.
            self._forget_user_chats()
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
            self._forget_user_chats()
            await self._cancel_user_ingest_task()
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

    async def _cancel_user_ingest_task(self) -> None:
        if self._user_ingest_task is None:
            return
        self._user_ingest_task.cancel()
        await asyncio.gather(self._user_ingest_task, return_exceptions=True)
        self._user_ingest_task = None

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
        await self._cancel_user_ingest_task()
