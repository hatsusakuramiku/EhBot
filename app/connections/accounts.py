"""Independent account configuration and immutable private credential versions."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
import json
import sqlite3
from uuid import uuid4

from app.db.database import Database
from app.connections.models import ProviderConnectionError
from app.secrets import SecretStore


ACCOUNT_FIELDS = {
    "TG_BOT": frozenset({"bot_token"}),
    "TG_USER": frozenset({"api_id", "api_hash", "phone"}),
    "EH": frozenset({"ipb_member_id", "ipb_pass_hash", "igneous"}),
}
MASK = "********"


class AccountError(ProviderConnectionError):
    def __init__(self, code: str, public_message: str) -> None:
        super().__init__(code, public_message)


@dataclass(frozen=True, slots=True)
class AccountContext:
    """A private snapshot; never serialize it into a public list or WS event."""
    account_id: str
    kind: str
    revision: int
    credential_revision: int
    values: dict[str, str] = field(repr=False)


class AccountRegistry:
    """Own storage and CAS; runtime activation is performed by the coordinator.

    New configurations are disabled and may be incomplete or unverified. An
    active configuration must be stopped by the coordinator before its secrets
    can change. Renaming does not alter the credential version. Older private
    versions remain available to their exact execution contexts until cleanup.
    """

    def __init__(self, database: Database, secrets: SecretStore) -> None:
        self._database = database
        self._secrets = secrets

    @staticmethod
    def _fields(kind: str, fields: dict, clear_fields=()) -> dict[str, str]:
        allowed = ACCOUNT_FIELDS.get(kind) if isinstance(kind, str) else None
        if allowed is None:
            raise AccountError("ACCOUNT_KIND_INVALID", "不支持的账号类型")
        if (not isinstance(fields, dict) or not isinstance(clear_fields, (list, tuple))
                or any(not isinstance(key, str) or key not in allowed for key in fields)
                or any(not isinstance(key, str) or key not in allowed for key in clear_fields)
                or set(fields) & set(clear_fields)):
            raise AccountError("ACCOUNT_FIELDS_INVALID", "账号字段或清除动作无效")
        if any(not isinstance(value, str) or not value.strip() or value == MASK for value in fields.values()):
            raise AccountError("ACCOUNT_VALUE_INVALID", "请填写真实值；未修改字段不要提交，清空请使用明确的清除动作")
        return {key: value.strip() for key, value in fields.items()}

    @staticmethod
    def _label(label: str) -> str:
        if not isinstance(label, str) or not label.strip() or len(label.strip()) > 120:
            raise AccountError("ACCOUNT_LABEL_INVALID", "账号别名需为 1–120 个字符")
        return label.strip()

    @staticmethod
    def _expected(expected_revision: int) -> None:
        if type(expected_revision) is not int or expected_revision < 1:
            raise AccountError("PRECONDITION_REQUIRED", "请提交已读取的账号版本")

    @staticmethod
    def _public(row) -> dict:
        names = json.loads(row["configured_fields_json"])
        return {
            "account_id": row["account_id"], "kind": row["kind"], "label": row["label"],
            "remote_identity": row["remote_identity"], "platform_name": row["platform_name"],
            "enabled": bool(row["enabled"]), "revision": row["revision"],
            "credential_revision": row["credential_revision"],
            "verified": row["verified_credential_revision"] == row["credential_revision"],
            "stop_operation_id": row["stop_operation_id"],
            "fields": {key: MASK if key in names else "" for key in sorted(ACCOUNT_FIELDS[row["kind"]])},
        }

    @staticmethod
    def _row(connection, account_id: str):
        connection.row_factory = sqlite3.Row
        row = connection.execute(
            "SELECT * FROM external_accounts WHERE account_id = ? AND deleted_at IS NULL", (account_id,),
        ).fetchone()
        if row is None:
            raise AccountError("ACCOUNT_NOT_FOUND", "账号不存在或已删除")
        return row

    def _write_version(self, account_id: str, revision: int, values: dict) -> str:
        # Each competing editor needs its own file, even at the same revision.
        # Writing account_id + revision alone would overwrite the CAS winner.
        reference = f"account_{account_id.replace('-', '')}_r{revision}_{uuid4().hex}"
        self._secrets.write(reference, json.dumps(values, separators=(",", ":")))
        return reference

    def _discard_unreferenced(self, reference: str) -> None:
        try:
            with self._database.connection() as connection:
                referenced = connection.execute(
                    "SELECT 1 FROM external_account_credentials WHERE secret_ref = ? "
                    "UNION ALL SELECT 1 FROM account_operations WHERE next_credentials_ref = ? AND status = 'PENDING'",
                    (reference, reference),
                ).fetchone()
            if referenced is None:
                self._secrets.delete(reference)
        except (OSError, sqlite3.Error):
            # Uncertain cleanup leaves an orphan for later reconciliation; it
            # must not erase a possibly committed reference or mask the error.
            pass

    @staticmethod
    def _receipt(connection, command):
        if command is None:
            return None
        key, digest, action = command
        row = connection.execute(
            "SELECT payload_hash, response_json FROM account_command_receipts WHERE scope = 'administrator' AND idempotency_key = ?", (key,),
        ).fetchone()
        if row is None:
            return None
        if row[0] != digest:
            raise AccountError("IDEMPOTENCY_CONFLICT", "此幂等键已用于不同的账号命令")
        return json.loads(row[1])

    @staticmethod
    def _complete_command(connection, command, result):
        if command is None:
            return result
        key, digest, action = command
        response = {"command_id": uuid4().hex, "account_id": result["account_id"], "action": action,
                    "status": "COMPLETED", "result": result}
        connection.execute(
            "INSERT INTO account_command_receipts(idempotency_key, payload_hash, response_json) VALUES(?, ?, ?)",
            (key, digest, json.dumps(response, separators=(",", ":"))),
        )
        return response

    def _read_command_sync(self, command):
        with self._database.connection() as connection:
            return self._receipt(connection, command)

    async def list_accounts(self) -> list[dict]:
        return await asyncio.to_thread(self._list_sync)

    def _list_sync(self):
        with self._database.connection() as connection:
            connection.row_factory = sqlite3.Row
            return [self._public(row) for row in connection.execute(
                "SELECT * FROM external_accounts WHERE deleted_at IS NULL ORDER BY created_at, account_id",
            )]

    async def get(self, account_id: str) -> dict:
        return await asyncio.to_thread(self._get_sync, account_id)

    def _get_sync(self, account_id):
        with self._database.connection() as connection:
            return self._public(self._row(connection, account_id))

    async def context(self, account_id: str, *, credential_revision: int | None = None) -> AccountContext:
        """Read current configuration or the positive version bound to an attempt."""
        if credential_revision is not None:
            self._expected(credential_revision)
        return await asyncio.to_thread(self._context_sync, account_id, credential_revision)

    def _context_sync(self, account_id, credential_revision=None):
        with self._database.connection() as connection:
            row = self._row(connection, account_id)
            selected = row["credential_revision"] if credential_revision is None else credential_revision
            version = connection.execute(
                "SELECT secret_ref FROM external_account_credentials WHERE account_id = ? AND credential_revision = ?",
                (account_id, selected),
            ).fetchone()
        if version is None:
            raise AccountError("ACCOUNT_CREDENTIAL_VERSION_NOT_FOUND", "请求的账号凭据版本不存在")
        payload = self._secrets.read(version[0])
        if payload is None:
            raise AccountError("ACCOUNT_CREDENTIALS_MISSING", "账号私有配置文件缺失")
        try:
            values = json.loads(payload)
            if not isinstance(values, dict) or any(not isinstance(value, str) for value in values.values()):
                raise ValueError
        except ValueError as exc:
            raise AccountError("ACCOUNT_CREDENTIALS_INVALID", "账号私有配置文件无效") from exc
        return AccountContext(account_id, row["kind"], row["revision"], selected, values)

    async def reveal(self, account_id: str) -> dict:
        """Explicit configuration view; the HTTP layer must prohibit caching."""
        context = await self.context(account_id)
        return {"account_id": account_id, "revision": context.revision,
                "credential_revision": context.credential_revision,
                "fields": {key: value for key, value in context.values.items() if key in ACCOUNT_FIELDS[context.kind]}}

    async def create(self, kind: str, label: str, fields: dict) -> dict:
        values = self._fields(kind, fields)
        label = self._label(label)
        return await asyncio.to_thread(self._create_sync, kind, label, values)

    def _create_sync(self, kind, label, values, legacy_slot=None, command=None):
        account_id = str(uuid4())
        reference = self._write_version(account_id, 1, values)
        try:
            with self._database.connection() as connection:
                connection.execute("BEGIN IMMEDIATE")
                receipt = self._receipt(connection, command)
                if receipt is not None:
                    self._secrets.delete(reference)
                    return receipt
                connection.execute(
                    "INSERT INTO external_accounts(account_id, kind, label, credentials_ref, configured_fields_json, legacy_slot) "
                    "VALUES(?, ?, ?, ?, ?, ?)",
                    (account_id, kind, label, reference, json.dumps(sorted(values)), legacy_slot),
                )
                if legacy_slot is not None:
                    connection.execute("UPDATE external_accounts SET activation_requested = 1 WHERE account_id = ?", (account_id,))
                connection.execute(
                    "INSERT INTO external_account_credentials(account_id, credential_revision, secret_ref) VALUES(?, 1, ?)",
                    (account_id, reference),
                )
                result = self._public(self._row(connection, account_id))
                result = self._complete_command(connection, command, result)
            return result
        except BaseException:
            self._discard_unreferenced(reference)
            raise

    async def import_legacy(self) -> dict[str, str]:
        """Copy old single-account private configuration once; retain originals.

        The coordinator validates and activates the imported accounts later.
        A retained import marker also prevents deleted accounts from silently
        reappearing on restart. TG sessions stay private and are never revealed.
        """
        return await asyncio.to_thread(self._import_legacy_sync)

    def _import_legacy_sync(self):
        imported = {}
        for kind in ACCOUNT_FIELDS:
            with self._database.connection() as connection:
                row = connection.execute(
                    "SELECT account_id, deleted_at FROM external_accounts WHERE legacy_slot = ?", (kind,),
                ).fetchone()
            if row is not None:
                if row[1] is None:
                    imported[kind] = row[0]
                continue
            values = {}
            if kind == "TG_BOT":
                token = self._secrets.read("telegram_bot_token")
                if token is not None:
                    values["bot_token"] = token
            elif kind == "EH":
                cookies = self._secrets.read("exhentai_cookies")
                if cookies is not None:
                    try:
                        decoded = json.loads(cookies)
                        if not isinstance(decoded, dict):
                            raise ValueError
                        values = {key: decoded[key] for key in ACCOUNT_FIELDS[kind] if key in decoded}
                        if any(not isinstance(value, str) for value in values.values()):
                            raise ValueError
                    except ValueError as exc:
                        raise AccountError("ACCOUNT_CREDENTIALS_INVALID", "旧 EH 配置无效，请核对私有配置") from exc
            else:
                pair = self._secrets.read("telegram_user_api")
                session = self._secrets.read("telegram_user_session")
                if pair is not None:
                    api_id, separator, api_hash = pair.partition(":")
                    if not separator:
                        raise AccountError("ACCOUNT_CREDENTIALS_INVALID", "旧 TG API 配置无效，请核对私有配置")
                    values.update(api_id=api_id, api_hash=api_hash)
                if session is not None:
                    values["session"] = session
            if not values:
                continue
            try:
                account = self._create_sync(kind, f"Legacy {kind}", values, kind)
                imported[kind] = account["account_id"]
            except sqlite3.IntegrityError:
                # Another registry can finish the same import after our read.
                with self._database.connection() as connection:
                    row = connection.execute(
                        "SELECT account_id, deleted_at FROM external_accounts WHERE legacy_slot = ?", (kind,),
                    ).fetchone()
                if row is None:
                    raise
                if row[1] is None:
                    imported[kind] = row[0]
        self._import_legacy_sources_sync()
        return imported

    def _import_legacy_sources_sync(self):
        """Copy shared rules once per proven legacy slot, never observations.

        Retired originals retain their configuration for a legacy slot whose
        credentials are imported later. The group marker prevents a restart
        from undoing subsequent edits or resurrecting dismissed subscriptions.
        """
        with self._database.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            accounts = connection.execute(
                "SELECT account_id, kind FROM external_accounts "
                "WHERE legacy_slot IN ('TG_BOT', 'TG_USER') AND deleted_at IS NULL "
                "AND account_id NOT IN (SELECT account_id FROM legacy_telegram_source_imports) ORDER BY kind, account_id"
            ).fetchall()
            for external_id, kind in accounts:
                account_id = self._database._ensure_telegram_account(connection, external_id)
                connection.execute(
                    "INSERT INTO telegram_sources(account_id, source_type, chat_id, display_name, enabled, "
                    "rules_json, dismissed, last_message_id) "
                    "SELECT ?, ts.source_type, ts.chat_id, ts.display_name, ts.enabled, ts.rules_json, ts.dismissed, "
                    "CASE WHEN ? = 'TG_USER' THEN ts.last_message_id ELSE 0 END "
                    "FROM telegram_sources ts JOIN telegram_accounts ta ON ta.id = ts.account_id "
                    "WHERE ta.session_path = 'bot-api://configured' AND ta.external_account_id IS NULL "
                    "ON CONFLICT(account_id, chat_id) DO NOTHING",
                    (account_id, kind),
                )
                connection.execute("INSERT INTO legacy_telegram_source_imports(account_id) VALUES(?)", (external_id,))
            if accounts:
                connection.execute(
                    "UPDATE telegram_sources SET legacy_retired = 1 WHERE legacy_retired = 0 "
                    "AND account_id IN (SELECT id FROM telegram_accounts "
                    "WHERE session_path = 'bot-api://configured' AND external_account_id IS NULL)"
                )

    async def edit(self, account_id: str, *, expected_revision: int,
                   label: str | None = None, fields: dict | None = None, clear_fields=()) -> dict:
        self._expected(expected_revision)
        if label is not None:
            label = self._label(label)
        return await asyncio.to_thread(self._edit_sync, account_id, expected_revision, label,
                                       {} if fields is None else fields, clear_fields)

    def _edit_sync(self, account_id, expected_revision, label, fields, clear_fields, command=None):
        if command is not None:
            with self._database.connection() as connection:
                receipt = self._receipt(connection, command)
            if receipt is not None:
                return receipt
        context = self._context_sync(account_id)
        if context.revision != expected_revision:
            raise AccountError("REVISION_CONFLICT", "账号已被另一端修改，请刷新后核对")
        updates = self._fields(context.kind, fields, clear_fields)
        values = dict(context.values)
        values.update(updates)
        for name in clear_fields:
            values.pop(name, None)
        if context.kind == "TG_USER" and any(values.get(name) != context.values.get(name) for name in ("api_id", "api_hash", "phone")):
            values.pop("session", None)
        changed = values != context.values
        reference = self._write_version(account_id, context.credential_revision + 1, values) if changed else None
        try:
            with self._database.connection() as connection:
                connection.execute("BEGIN IMMEDIATE")
                receipt = self._receipt(connection, command)
                if receipt is not None:
                    if reference is not None:
                        self._secrets.delete(reference)
                    return receipt
                row = self._row(connection, account_id)
                if row["revision"] != expected_revision:
                    raise AccountError("REVISION_CONFLICT", "账号已被另一端修改，请刷新后核对")
                if row["stop_operation_id"] is not None:
                    raise AccountError("ACCOUNT_STOPPING", "账号旧执行尚未确认停止，请等待当前操作完成")
                if changed and row["enabled"]:
                    raise AccountError("ACCOUNT_ACTIVE", "请先由连接管理器确认旧版本执行停止，再修改凭据")
                if not changed and (label is None or label == row["label"]):
                    return self._complete_command(connection, command, self._public(row))
                next_revision = context.credential_revision + int(changed)
                connection.execute(
                    "UPDATE external_accounts SET label = ?, revision = revision + 1, credential_revision = ?, "
                    "credentials_ref = ?, configured_fields_json = ?, updated_at = CURRENT_TIMESTAMP WHERE account_id = ?",
                    (label if label is not None else row["label"], next_revision,
                     reference if changed else row["credentials_ref"], json.dumps(sorted(values)), account_id),
                )
                if changed:
                    connection.execute(
                        "INSERT INTO external_account_credentials(account_id, credential_revision, secret_ref) VALUES(?, ?, ?)",
                        (account_id, next_revision, reference),
                    )
                result = self._public(self._row(connection, account_id))
                result = self._complete_command(connection, command, result)
            return result
        except BaseException:
            if reference is not None:
                self._discard_unreferenced(reference)
            raise

    async def record_identity(self, account_id: str, credential_revision: int,
                              remote_identity: str, platform_name: str, *, expected_revision: int | None = None) -> dict:
        """Bind only the adapter's verified identity for the tested version."""
        self._expected(credential_revision)
        if not isinstance(remote_identity, str) or not remote_identity.strip():
            raise AccountError("ACCOUNT_IDENTITY_INVALID", "平台身份无效")
        return await asyncio.to_thread(self._record_identity_sync, account_id, credential_revision,
                                       remote_identity.strip(), platform_name, expected_revision)

    async def save_session(self, account_id: str, *, expected_revision: int,
                           session: str, remote_identity: str, platform_name: str) -> dict:
        """Store a verified login session privately; never accept it via public fields.

        Called after the login request lease exits. CAS and stop intent are
        checked again under the write lock, so a concurrent stop/edit wins
        without saving a late session. Enabled accounts must be stopped first.
        """
        self._expected(expected_revision)
        if not isinstance(session, str) or not session or not remote_identity:
            raise AccountError("ACCOUNT_SESSION_INVALID", "登录会话无效")
        return await asyncio.to_thread(self._save_session_sync, account_id, expected_revision,
                                       session, remote_identity, platform_name)

    def _save_session_sync(self, account_id, expected_revision, session, remote_identity, platform_name):
        context = self._context_sync(account_id)
        if context.kind != "TG_USER":
            raise AccountError("ACCOUNT_KIND_INVALID", "只有 TG 用户账号支持登录会话")
        values = {**context.values, "session": session}
        reference = self._write_version(account_id, context.credential_revision + 1, values)
        try:
            with self._database.connection() as connection:
                connection.execute("BEGIN IMMEDIATE")
                row = self._row(connection, account_id)
                if row["revision"] != expected_revision:
                    raise AccountError("REVISION_CONFLICT", "登录期间账号已改变，请重新登录")
                if row["enabled"] or row["stop_operation_id"] is not None:
                    raise AccountError("ACCOUNT_ACTIVE", "请确认旧账号执行停止后再保存登录会话")
                if row["remote_identity"] not in (None, remote_identity):
                    raise AccountError("ACCOUNT_IDENTITY_CHANGED", "登录属于另一个平台账号，请新建账号")
                next_revision = context.credential_revision + 1
                connection.execute(
                    "INSERT INTO external_account_credentials(account_id, credential_revision, secret_ref) VALUES(?, ?, ?)",
                    (account_id, next_revision, reference),
                )
                connection.execute(
                    "UPDATE external_accounts SET credentials_ref = ?, credential_revision = ?, verified_credential_revision = ?, "
                    "remote_identity = ?, platform_name = ?, configured_fields_json = ?, revision = revision + 1, "
                    "updated_at = CURRENT_TIMESTAMP WHERE account_id = ?",
                    (reference, next_revision, next_revision, remote_identity, platform_name, json.dumps(sorted(values)), account_id),
                )
                result = self._public(self._row(connection, account_id))
            return result
        except BaseException:
            self._discard_unreferenced(reference)
            raise

    def _record_identity_sync(self, account_id, credential_revision, remote_identity, platform_name, expected_revision=None):
        with self._database.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = self._row(connection, account_id)
            if expected_revision is not None and row["revision"] != expected_revision:
                raise AccountError("REVISION_CONFLICT", "验证期间账号已改变，请刷新后核对")
            if row["credential_revision"] != credential_revision:
                raise AccountError("REVISION_CONFLICT", "验证期间账号凭据已改变，请重新测试")
            if row["remote_identity"] not in (None, remote_identity):
                raise AccountError("ACCOUNT_IDENTITY_CHANGED", "凭据属于另一个平台账号，请新建账号以保留原来源身份")
            if row["stop_operation_id"] is not None:
                raise AccountError("ACCOUNT_STOPPING", "账号正在停止，不能提交验证结果")
            if row["kind"] == "EH":
                connection.execute(
                    "DELETE FROM eh_account_health WHERE account_id = ? AND credential_revision = ? AND capability = 'AUTH'",
                    (account_id, credential_revision),
                )
            if (row["remote_identity"] == remote_identity and row["platform_name"] == platform_name
                    and row["verified_credential_revision"] == credential_revision):
                return self._public(row)
            connection.execute(
                "UPDATE external_accounts SET remote_identity = ?, platform_name = ?, verified_credential_revision = ?, "
                "revision = revision + 1, updated_at = CURRENT_TIMESTAMP WHERE account_id = ?",
                (remote_identity, platform_name, credential_revision, account_id),
            )
            return self._public(self._row(connection, account_id))

    async def enable(self, account_id: str, *, expected_revision: int) -> dict:
        """Activate only the exact verified configuration, after old stops end."""
        self._expected(expected_revision)
        return await asyncio.to_thread(self._enable_sync, account_id, expected_revision)

    def _enable_sync(self, account_id, expected_revision, command=None, exclusive_kind=False):
        try:
            with self._database.connection() as connection:
                connection.execute("BEGIN IMMEDIATE")
                receipt = self._receipt(connection, command)
                if receipt is not None:
                    return receipt
                row = self._row(connection, account_id)
                if row["revision"] != expected_revision:
                    raise AccountError("REVISION_CONFLICT", "账号已被另一端修改，请刷新后核对")
                if row["stop_operation_id"] is not None:
                    raise AccountError("ACCOUNT_STOPPING", "账号旧执行尚未确认停止")
                if row["verified_credential_revision"] != row["credential_revision"]:
                    raise AccountError("ACCOUNT_NOT_VERIFIED", "请先验证当前凭据版本")
                if exclusive_kind and connection.execute(
                    "SELECT 1 FROM external_accounts WHERE kind = ? AND enabled = 1 AND deleted_at IS NULL AND account_id <> ?",
                    (row["kind"], account_id),
                ).fetchone() is not None:
                    raise AccountError("ACCOUNT_RUNTIME_LIMIT", "当前阶段同类账号只能启用一个，请先停用原账号")
                if not row["enabled"]:
                    connection.execute(
                        "UPDATE external_accounts SET enabled = 1, activation_requested = 1, revision = revision + 1, "
                        "updated_at = CURRENT_TIMESTAMP WHERE account_id = ?", (account_id,),
                    )
                return self._complete_command(connection, command, self._public(self._row(connection, account_id)))
        except sqlite3.IntegrityError as exc:
            if "external_accounts.kind, external_accounts.remote_identity" in str(exc):
                raise AccountError("ACCOUNT_IDENTITY_IN_USE", "此平台账号已有启用配置") from exc
            raise
