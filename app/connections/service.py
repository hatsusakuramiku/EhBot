"""Account configurations, verification, login and runtime activation."""
from __future__ import annotations

import asyncio
import hashlib
import json
from contextlib import asynccontextmanager
from dataclasses import dataclass, replace
from uuid import uuid4

from app.connections.accounts import AccountError, AccountRegistry
from app.connections.exhentai import ExHentaiApi, ExHentaiCredentials
from app.connections.lifecycle import AccountLifecycle
from app.connections.listeners import AccountListener
from app.connections.models import ConnectionSnapshot, ProviderConnectionError, ProviderStatus, TelegramUserAccount
from app.connections.runtime import AccountRuntime, current_account_context
from app.connections.telegram import TelegramBotApi
from app.connections.telegram_user import LoginChallenge, TelegramUserClient, TelegramUserCredentials, TelegramUserError
from app.downloads.execution import run_sync


@dataclass(slots=True)
class _PendingLogin:
    challenge_id: str
    credential_revision: int
    challenge: LoginChallenge


class AccountService:
    """Single-admin account control, with per-account runtime state.

    P4 stores many configurations but activates at most one of each kind.
    P6/P7 extend selection/listening after ingestion identity is migrated.
    Disabling/editing/deleting uses durable operations and confirmed cleanup.
    """

    def __init__(self, registry: AccountRegistry, *, telegram_client, exhentai_client,
                 candidate_ingestor=None, user_client_factory=None, on_candidates_ingested=None,
                 on_state_changed=None, process_queue_operations=None):
        self.registry = registry
        self.lifecycle = AccountLifecycle(registry)
        self.runtime = AccountRuntime(registry, self.lifecycle)
        self._telegram_client = telegram_client
        self._exhentai_client = exhentai_client
        self._candidate_ingestor = candidate_ingestor
        self._user_client_factory = user_client_factory
        self._on_candidates_ingested = on_candidates_ingested
        self._on_state_changed = on_state_changed
        self._process_queue_operations = process_queue_operations
        self._listeners: dict[str, AccountListener] = {}
        self._states: dict[str, ProviderStatus | TelegramUserAccount] = {}
        self._kinds: dict[str, str] = {}
        self._logins: dict[str, _PendingLogin] = {}
        self._login_locks: dict[str, asyncio.Lock] = {}
        self._activation_lock = asyncio.Lock()
        self._operation_task = None

    def _notify(self):
        if self._on_state_changed is not None:
            self._on_state_changed()

    def _listener(self, account_id):
        if account_id not in self._listeners:
            self._listeners[account_id] = AccountListener(
                account_id, self.runtime, self.registry._database,
                telegram_client=self._telegram_client, exhentai_client=self._exhentai_client,
                candidate_ingestor=self._candidate_ingestor, user_client_factory=self._user_client_factory,
                on_candidates_ingested=self._on_candidates_ingested, on_state_changed=self._on_state_changed,
            )
        return self._listeners[account_id]

    @staticmethod
    def _user_credentials(values):
        return TelegramUserCredentials.parse(values.get("api_id", ""), values.get("api_hash", ""))

    def _user_client(self, account_id, values, session=None):
        return TelegramUserClient(self._user_credentials(values), values.get("session") if session is None else session,
                                  client_factory=self._user_client_factory,
                                  entity_index=self._listener(account_id)._user_entities)

    @staticmethod
    def _eh_credentials(values):
        if not all(values.get(name) for name in ("ipb_member_id", "ipb_pass_hash", "igneous")):
            raise AccountError("ACCOUNT_CONFIGURATION_INCOMPLETE", "请填写 EH 的完整 Cookie 配置")
        return ExHentaiCredentials(*(values[name] for name in ("ipb_member_id", "ipb_pass_hash", "igneous")))

    async def test_account(self, account_id: str, *, expected_revision: int) -> dict:
        """Verify saved credentials, retaining them and reporting a named error."""
        self.registry._expected(expected_revision)
        async with self.runtime.use(account_id, for_verification=True) as context:
            if context.revision != expected_revision:
                raise AccountError("REVISION_CONFLICT", "账号已被另一端修改，请刷新后核对")
            self._kinds[account_id] = context.kind
            try:
                if context.kind == "TG_BOT":
                    token = context.values.get("bot_token")
                    if not token:
                        raise AccountError("ACCOUNT_CONFIGURATION_INCOMPLETE", "请填写 Bot Token")
                    identity = await TelegramBotApi(token, self._telegram_client).verify()
                    remote_identity, label = str(identity.bot_id), f"@{identity.username}"
                elif context.kind == "EH":
                    credentials = self._eh_credentials(context.values)
                    label = await ExHentaiApi(credentials, self._exhentai_client).verify()
                    remote_identity = credentials.ipb_member_id
                else:
                    if not context.values.get("session"):
                        raise AccountError("ACCOUNT_LOGIN_REQUIRED", "请先完成 TG 用户账号登录")
                    identity = await self._user_client(account_id, context.values).verify()
                    remote_identity, label = str(identity.user_id), identity.label
                result = await self.registry.record_identity(account_id, context.credential_revision, remote_identity, label,
                                                             expected_revision=context.revision)
            except (ProviderConnectionError, AccountError) as exc:
                self._states[account_id] = ProviderStatus("error", True, error=exc.public_message)
                self._notify()
                raise
        self._states[account_id] = ProviderStatus("connected" if result["enabled"] else "not_configured", True, identity=label)
        self._notify()
        return result

    async def enable_account(self, account_id: str, *, expected_revision: int) -> dict:
        """Enable a verified saved account; no credential re-entry is needed."""
        async with self._activation_lock:
            account = await self.registry.get(account_id)
            if account["revision"] != expected_revision:
                raise AccountError("REVISION_CONFLICT", "账号已被另一端修改，请刷新后核对")
            for other in await self.registry.list_accounts():
                if other["kind"] == account["kind"] and other["enabled"] and other["account_id"] != account_id:
                    raise AccountError("ACCOUNT_RUNTIME_LIMIT", "当前阶段同类账号只能启用一个，请先停用原账号")
            result = await self.registry.enable(account_id, expected_revision=expected_revision)
            await self._start_listener(result)
            self._notify()
            return result

    async def _start_listener(self, account):
        account_id, kind = account["account_id"], account["kind"]
        self._kinds[account_id] = kind
        listener = self._listener(account_id)
        if kind == "TG_BOT":
            listener._telegram_status = ProviderStatus("connected", True, identity=account["platform_name"])
            if listener._telegram_task is None or listener._telegram_task.done():
                listener._telegram_task = asyncio.create_task(listener._poll_telegram(), name=f"bot-{account_id}")
        elif kind == "TG_USER":
            listener._telegram_user = TelegramUserAccount("connected", True, identity=account["platform_name"])
            listener._ensure_user_ingest_task()
        self._states[account_id] = ProviderStatus("connected", True, identity=account["platform_name"])

    async def stop_account(self, account_id: str, action: str, **params):
        """Admit a durable stop; its operation can remain pending remotely."""
        result = await self.lifecycle.request_stop(account_id, action, **params)
        self._notify()
        return result

    async def _stop_runtime(self, account_id, credential_revision):
        await self.runtime.stop(account_id, credential_revision)
        listener = self._listeners.get(account_id)
        if listener is not None:
            await listener.stop()
            listener._forget_user_chats()
        self._logins.pop(account_id, None)
        self._states[account_id] = ProviderStatus("not_configured", True)
        return True

    async def process_operations(self):
        if self._process_queue_operations is None:
            # Startup may import accounts before downloads have been built.
            # Unwired stop processing must never claim dependencies stopped.
            return 0
        completed = await self.lifecycle.process_operations(
            stop_runtime=self._stop_runtime, process_queue_operations=self._process_queue_operations,
        )
        if completed:
            live_ids = {account["account_id"] for account in await self.registry.list_accounts()}
            for account_id in tuple(self._states):
                if account_id not in live_ids:
                    self._states.pop(account_id, None)
                    self._kinds.pop(account_id, None)
                    self._listeners.pop(account_id, None)
            self._notify()
        return completed

    def _reserve_command_sync(self, account_id, action, revision, key, digest):
        with self.registry._database.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = self.lifecycle._receipt(connection, key, digest)
            if existing is not None:
                return existing, False
            if account_id is not None:
                account = self.registry._row(connection, account_id)
                if account["revision"] != revision:
                    raise AccountError("REVISION_CONFLICT", "账号已被另一端修改，请刷新后核对")
                if account["stop_operation_id"] is not None:
                    raise AccountError("ACCOUNT_STOPPING", "请等待旧账号执行停止")
            response = {"command_id": uuid4().hex, "account_id": account_id, "action": action, "status": "PENDING"}
            connection.execute(
                "INSERT INTO account_command_receipts(idempotency_key, payload_hash, response_json) VALUES(?, ?, ?)",
                (key, digest, json.dumps(response, separators=(",", ":"))),
            )
            return response, True

    def _finish_command_sync(self, key, response):
        with self.registry._database.connection() as connection:
            connection.execute(
                "UPDATE account_command_receipts SET response_json = ? WHERE scope = 'administrator' AND idempotency_key = ?",
                (json.dumps(response, separators=(",", ":")), key),
            )

    async def command(self, action: str, *, account_id: str | None = None,
                      expected_revision: int | None = None, idempotency_key: str, **params):
        """One shared mutation entry for browser/native clients.

        Network commands reserve their receipt before contacting the provider.
        Pending/unknown receipts are never re-executed, including after restart;
        expired login challenges require a new explicit command/key. Secrets
        only contribute to the request hash, never the persisted response.
        """
        actions = {"create", "rename", "edit", "disable", "delete", "test", "enable", "login-start", "login-complete", "login-cancel"}
        if action not in actions:
            raise AccountError("ACCOUNT_ACTION_INVALID", "不支持的账号命令")
        if not isinstance(idempotency_key, str) or not 1 <= len(idempotency_key) <= 200:
            raise AccountError("IDEMPOTENCY_KEY_REQUIRED", "请提供 1–200 字符的幂等键")
        if action != "create":
            if not isinstance(account_id, str) or not account_id:
                raise AccountError("ACCOUNT_ID_REQUIRED", "请指定账号")
            self.registry._expected(expected_revision)
        if action in {"edit", "disable", "delete"}:
            return await self.stop_account(account_id, action, expected_revision=expected_revision,
                                           idempotency_key=idempotency_key, **params)
        digest = hashlib.sha256(json.dumps([account_id, action, expected_revision, params],
                                          sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()
        db_command = (idempotency_key, digest, action)
        replay = await run_sync(self.registry._read_command_sync, db_command)
        if replay is not None:
            return replay
        if action == "create":
            values = self.registry._fields(params.get("kind"), params.get("fields", {}))
            label = self.registry._label(params.get("label"))
            response = await run_sync(self.registry._create_sync, params["kind"], label, values, command=db_command)
            self._kinds[response["result"]["account_id"]] = response["result"]["kind"]
            self._states.setdefault(response["result"]["account_id"], ProviderStatus("not_configured", True))
            self._notify()
            return response
        if action == "rename":
            label = self.registry._label(params.get("label"))
            response = await run_sync(self.registry._edit_sync, account_id, expected_revision, label, {}, (), command=db_command)
            self._notify()
            return response
        if action == "enable":
            async with self._activation_lock:
                # Check private storage before accepting activation; the CAS
                # transaction below still owns current-version arbitration.
                await self.registry.context(account_id)
                response = await run_sync(self.registry._enable_sync, account_id, expected_revision,
                                          command=db_command, exclusive_kind=True)
                current = await self.registry.get(account_id)
                if current["enabled"] and current["stop_operation_id"] is None:
                    await self._start_listener(current)
                self._notify()
                return response
        receipt, reserved = await run_sync(self._reserve_command_sync, account_id, action,
                                          expected_revision, idempotency_key, digest)
        if not reserved:
            return receipt
        try:
            if action == "test":
                result = await self.test_account(account_id, expected_revision=expected_revision)
            elif action == "login-start":
                result = await self.start_login(account_id, expected_revision=expected_revision)
            elif action == "login-complete":
                result = await self.complete_login(account_id, expected_revision=expected_revision, **params)
            else:
                await self.cancel_login(account_id, expected_revision=expected_revision, **params)
                result = {"account_id": account_id}
            response = {**receipt, "status": "COMPLETED", "result": result}
        except asyncio.CancelledError:
            await run_sync(self._finish_command_sync, idempotency_key,
                           {**receipt, "status": "UNKNOWN", "error": {"code": "ACCOUNT_COMMAND_INTERRUPTED", "message": "请求被中断，请先核对账号状态再发起新操作"}})
            raise
        except ProviderConnectionError as exc:
            response = {**receipt, "status": "FAILED", "error": {"code": exc.code, "message": exc.public_message}}
        except Exception:
            # Preserve unknown outcomes without storing provider exception text.
            response = {**receipt, "status": "UNKNOWN", "error": {"code": "ACCOUNT_COMMAND_UNCONFIRMED", "message": "操作结果未确认，请核对账号状态"}}
        await run_sync(self._finish_command_sync, idempotency_key, response)
        self._notify()
        return response

    async def command_receipt(self, key):
        def read():
            with self.registry._database.connection() as connection:
                row = connection.execute("SELECT response_json FROM account_command_receipts WHERE scope = 'administrator' AND idempotency_key = ?", (key,)).fetchone()
            if row is None:
                raise AccountError("ACCOUNT_COMMAND_NOT_FOUND", "账号命令不存在")
            return json.loads(row[0])
        return await run_sync(read)

    async def _run_operations(self):
        while True:
            try:
                await self.process_operations()
            except asyncio.CancelledError:
                raise
            except Exception:
                # Keep durable intentions retryable after a transient DB error.
                # Provider exception text may contain private configuration.
                self._notify()
            await asyncio.sleep(1)

    async def start(self):
        await self.registry.import_legacy()
        accounts = await self.registry.list_accounts()
        for account in accounts:
            self._kinds[account["account_id"]] = account["kind"]
            self._states[account["account_id"]] = ProviderStatus("not_configured", True)
        if self._candidate_ingestor is not None:
            await self._listener("startup-backlog")._ingest_pending()
            del self._listeners["startup-backlog"]
        for account in accounts:
            with self.registry._database.connection() as connection:
                requested = connection.execute("SELECT activation_requested FROM external_accounts WHERE account_id = ?", (account["account_id"],)).fetchone()[0]
            if not requested or account["stop_operation_id"] is not None:
                continue
            try:
                verified = await self.test_account(account["account_id"], expected_revision=account["revision"])
                await self.enable_account(account["account_id"], expected_revision=verified["revision"])
            except (ProviderConnectionError, AccountError):
                # Saved configuration survives transient failures; no secret
                # or upstream exception is written into a public error/log.
                continue
        self._operation_task = asyncio.create_task(self._run_operations(), name="account-operations")

    async def stop(self):
        if self._operation_task is not None:
            self._operation_task.cancel()
            await asyncio.gather(self._operation_task, return_exceptions=True)
            self._operation_task = None
        for listener in self._listeners.values():
            await listener.stop()
        # Shutdown cancels remaining requests but does not persist a disable.
        for account_id, revision in tuple(self.runtime._leases):
            await self.runtime.stop(account_id, revision)
        self._logins.clear()

    async def selected_account(self, kind: str):
        for account in await self.registry.list_accounts():
            if (account["kind"] == kind and account["enabled"] and account["stop_operation_id"] is None
                    and self._states.get(account["account_id"], ProviderStatus("not_configured", False)).state == "connected"):
                return account
        return None

    @asynccontextmanager
    async def use_kind(self, kind: str, *, required: bool = True):
        existing = current_account_context.get()
        if existing is not None and existing.kind == kind:
            yield existing
            return
        account = await self.selected_account(kind)
        if account is None:
            if required:
                raise AccountError("ACCOUNT_NOT_ENABLED", "没有已启用的来源账号")
            yield None
            return
        async with self.runtime.use(account["account_id"]) as context:
            yield context

    async def start_login(self, account_id: str, *, expected_revision: int) -> dict:
        async with self._login_locks.setdefault(account_id, asyncio.Lock()):
            async with self.runtime.use(account_id, for_verification=True) as context:
                account = await self.registry.get(account_id)
                if context.kind != "TG_USER":
                    raise AccountError("ACCOUNT_KIND_INVALID", "只有 TG 用户账号支持登录")
                if context.revision != expected_revision:
                    raise AccountError("REVISION_CONFLICT", "账号已被另一端修改")
                if account["enabled"]:
                    raise AccountError("ACCOUNT_ACTIVE", "请先停用账号，再重新登录")
                phone = context.values.get("phone")
                if not phone:
                    raise AccountError("ACCOUNT_CONFIGURATION_INCOMPLETE", "请填写手机号")
                client = self._user_client(account_id, context.values, session="")
                challenge = await client.send_code(phone)
                login = _PendingLogin(uuid4().hex, context.credential_revision, challenge)
                self._logins[account_id] = login
                self._kinds[account_id] = context.kind
                self._states[account_id] = TelegramUserAccount("awaiting_code", True)
            self._notify()
            return {"account_id": account_id, "challenge_id": login.challenge_id, "state": "awaiting_code", "revision": context.revision}

    async def complete_login(self, account_id: str, *, challenge_id: str, expected_revision: int,
                             code: str | None = None, password: str | None = None):
        async with self._login_locks.setdefault(account_id, asyncio.Lock()):
            login = self._logins.get(account_id)
            if login is None or login.challenge_id != challenge_id:
                raise AccountError("ACCOUNT_LOGIN_STALE", "登录挑战已失效，请重新获取验证码")
            async with self.runtime.use(account_id, for_verification=True) as context:
                if context.revision != expected_revision or context.credential_revision != login.credential_revision:
                    raise AccountError("REVISION_CONFLICT", "登录期间账号配置已改变")
                try:
                    session, identity = await self._user_client(account_id, context.values, session="").sign_in(
                        login.challenge, code=code, password=password,
                    )
                except TelegramUserError as exc:
                    if exc.code == "TELEGRAM_USER_PASSWORD_NEEDED":
                        login.challenge = replace(login.challenge, requires_password=True)
                        self._states[account_id] = TelegramUserAccount("awaiting_password", True)
                        self._notify()
                    raise
            result = await self.registry.save_session(account_id, expected_revision=expected_revision,
                                                      session=session, remote_identity=str(identity.user_id), platform_name=identity.label)
            self._logins.pop(account_id, None)
            self._listener(account_id)._forget_user_chats()
            self._states[account_id] = ProviderStatus("not_configured", True, identity=identity.label)
            self._notify()
            return result

    async def cancel_login(self, account_id, challenge_id, *, expected_revision):
        async with self._login_locks.setdefault(account_id, asyncio.Lock()):
            account = await self.registry.get(account_id)
            if account["revision"] != expected_revision:
                raise AccountError("REVISION_CONFLICT", "登录期间账号已改变，请刷新后核对")
            if account["stop_operation_id"] is not None:
                raise AccountError("ACCOUNT_STOPPING", "请等待旧账号执行停止")
            login = self._logins.get(account_id)
            if login is None or login.challenge_id != challenge_id:
                raise AccountError("ACCOUNT_LOGIN_STALE", "登录挑战已失效")
            self._logins.pop(account_id)
            self._states[account_id] = ProviderStatus("not_configured", True)
            self._notify()

    def attach_candidate_admission(self, admission):
        if self._candidate_ingestor is not None:
            self._candidate_ingestor.set_admission(admission)

    def note_sources_changed(self):
        for listener in self._listeners.values():
            listener.note_sources_changed()

    def snapshot(self):
        by_kind = {}
        for account_id, state in self._states.items():
            kind = self._kinds.get(account_id)
            if kind not in by_kind or state.state == "connected":
                if kind == "TG_BOT" and account_id in self._listeners and state.state == "connected":
                    state = self._listeners[account_id]._telegram_status
                elif kind == "TG_USER" and account_id in self._listeners and state.state == "connected":
                    state = self._listeners[account_id]._telegram_user
                if kind == "TG_USER" and not isinstance(state, TelegramUserAccount):
                    state = TelegramUserAccount(state.state, state.configured, identity=state.identity, error=state.error)
                by_kind[kind] = state
        return ConnectionSnapshot(by_kind.get("TG_BOT", ProviderStatus("not_configured", False)),
                                  by_kind.get("EH", ProviderStatus("not_configured", False)),
                                  by_kind.get("TG_USER", TelegramUserAccount("not_configured", False)))

    def account_status(self, account_id):
        """Expose runtime health separately from persistent activation intent."""
        state = self._states.get(account_id, ProviderStatus("not_configured", True))
        listener = self._listeners.get(account_id)
        if listener is not None and state.state == "connected":
            kind = self._kinds.get(account_id)
            if kind == "TG_BOT":
                state = listener._telegram_status
            elif kind == "TG_USER":
                state = listener._telegram_user
        return {"state": state.state, "error": state.error}

    def telegram_available(self):
        return self.snapshot().telegram.state == "connected"

    def user_download_available(self):
        return self.snapshot().telegram_user.state == "connected"

    async def list_telegram_user_dialogs(self):
        account = await self.selected_account("TG_USER")
        if account is None:
            raise TelegramUserError("TELEGRAM_USER_NOT_CONFIGURED", "请先启用 TG 用户账号")
        return await self._listener(account["account_id"]).list_telegram_user_dialogs()

    async def telegram_user_context(self):
        context = current_account_context.get()
        if context is None or context.kind != "TG_USER":
            raise AccountError("ACCOUNT_CONTEXT_REQUIRED", "TG 用户下载必须在账号请求上下文内执行")
        return self._user_client(context.account_id, context.values)

    async def configure_telegram(self, token):
        raise AccountError("ACCOUNT_API_REQUIRED", "请通过账号管理保存、测试并启用配置")

    async def configure_exhentai(self, credentials):
        raise AccountError("ACCOUNT_API_REQUIRED", "请通过账号管理保存、测试并启用配置")

    async def start_telegram_user_login(self, api_id, api_hash, phone):
        raise AccountError("ACCOUNT_API_REQUIRED", "请通过账号管理使用已保存 API 配置登录")

    async def complete_telegram_user_login(self, code=None, password=None):
        raise AccountError("ACCOUNT_CHALLENGE_REQUIRED", "请通过账号管理提交对应账号的登录挑战和版本")

    async def disconnect_telegram(self):
        raise AccountError("ACCOUNT_REVISION_REQUIRED", "停用账号需提交账号身份与版本，请使用账号管理")

    async def disconnect_exhentai(self):
        raise AccountError("ACCOUNT_REVISION_REQUIRED", "停用账号需提交账号身份与版本，请使用账号管理")

    async def disconnect_telegram_user(self):
        raise AccountError("ACCOUNT_REVISION_REQUIRED", "停用账号需提交账号身份与版本，请使用账号管理")
