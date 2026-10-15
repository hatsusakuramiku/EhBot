"""Durable account stop arbitration; physical stop uses the existing queue core."""
from __future__ import annotations

import asyncio
import hashlib
import json
import sqlite3
from uuid import uuid4

from app.connections.accounts import AccountContext, AccountError, AccountRegistry
from app.downloads.execution import Execution
from app.downloads.queue import QueueControl, TERMINAL, task_snapshot


class AccountLifecycle:
    """Reserve a stop before changing credentials, with repeatable command receipts.

    A runtime adapter must stop this account's listeners, login challenge and
    non-queue requests. Queue dependencies are confirmed by P3, including
    removals whose business rows no longer exist. No timeout implies success.
    """

    def __init__(self, registry: AccountRegistry) -> None:
        self._registry = registry
        self._database = registry._database
        self._queue = QueueControl(self._database)
        self._process_lock = asyncio.Lock()

    async def bind_execution(self, context: AccountContext, execution: Execution) -> None:
        """Bind the selected immutable version under the same lock as stop admission."""
        await asyncio.to_thread(self._bind_sync, context, execution)

    def _bind_sync(self, context, execution):
        if execution.database_path != self._database.path:
            raise AccountError("ACCOUNT_EXECUTION_INVALID", "账号与执行不属于同一数据库")
        with self._database.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            execution.check(connection)
            row = self._registry._row(connection, context.account_id)
            if (not row["enabled"] or row["stop_operation_id"] is not None
                    or row["credential_revision"] != context.credential_revision
                    or row["verified_credential_revision"] != context.credential_revision
                    or row["kind"] != context.kind):
                raise AccountError("ACCOUNT_UNAVAILABLE", "所选账号版本已停用、改变或尚未验证")
            bound = connection.execute(
                "SELECT credential_revision FROM account_execution_bindings WHERE attempt_id = ? AND account_id = ?",
                (execution.attempt_id, context.account_id),
            ).fetchone()
            if bound is not None and bound[0] != context.credential_revision:
                raise AccountError("ACCOUNT_EXECUTION_INVALID", "同一执行不能替换已绑定的账号凭据版本")
            connection.execute(
                "INSERT OR IGNORE INTO account_execution_bindings(attempt_id, account_id, credential_revision) VALUES(?, ?, ?)",
                (execution.attempt_id, context.account_id, context.credential_revision),
            )

    @staticmethod
    def _digest(account_id, action, revision, label, fields, clear_fields):
        payload = json.dumps([account_id, action, revision, label, fields, sorted(clear_fields)],
                             sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    @staticmethod
    def _receipt(connection, key, digest):
        row = connection.execute(
            "SELECT payload_hash, response_json FROM account_command_receipts "
            "WHERE scope = 'administrator' AND idempotency_key = ?", (key,),
        ).fetchone()
        if row is None:
            return None
        if row[0] != digest:
            raise AccountError("IDEMPOTENCY_CONFLICT", "此幂等键已用于不同的账号命令")
        return json.loads(row[1])

    async def request_stop(self, account_id: str, action: str, *, expected_revision: int,
                           idempotency_key: str, label: str | None = None,
                           fields: dict | None = None, clear_fields=()) -> dict:
        """Accept disable/edit/delete; the response means admitted, not stopped."""
        self._registry._expected(expected_revision)
        if action not in {"disable", "edit", "delete"}:
            raise AccountError("ACCOUNT_ACTION_INVALID", "不支持的账号停止命令")
        if not isinstance(idempotency_key, str) or not 1 <= len(idempotency_key) <= 200:
            raise AccountError("IDEMPOTENCY_KEY_REQUIRED", "请提供 1–200 字符的幂等键")
        if fields is not None and not isinstance(fields, dict):
            raise AccountError("ACCOUNT_FIELDS_INVALID", "账号字段无效")
        fields = {} if fields is None else dict(fields)
        if any(not isinstance(name, str) or not isinstance(value, str) for name, value in fields.items()):
            raise AccountError("ACCOUNT_FIELDS_INVALID", "账号字段无效")
        if not isinstance(clear_fields, (tuple, list)) or any(not isinstance(key, str) for key in clear_fields):
            raise AccountError("ACCOUNT_FIELDS_INVALID", "账号清除动作无效")
        if action != "edit" and (label is not None or fields or clear_fields):
            raise AccountError("ACCOUNT_FIELDS_INVALID", "停用或删除命令不能同时修改配置")
        if label is not None:
            label = self._registry._label(label)
        return await asyncio.to_thread(self._request_sync, account_id, action, expected_revision,
                                       idempotency_key, label, fields, tuple(clear_fields))

    def _request_sync(self, account_id, action, revision, key, label, fields, clear_fields):
        digest = self._digest(account_id, action, revision, label, fields, clear_fields)
        with self._database.connection() as connection:
            receipt = self._receipt(connection, key, digest)
            if receipt is not None:
                return receipt
        context = self._registry._context_sync(account_id)
        if context.revision != revision:
            raise AccountError("REVISION_CONFLICT", "账号已被另一端修改，请刷新后核对")
        values = dict(context.values)
        if action == "edit":
            values.update(self._registry._fields(context.kind, fields, clear_fields))
            for name in clear_fields:
                values.pop(name, None)
            if context.kind == "TG_USER" and any(values.get(name) != context.values.get(name) for name in ("api_id", "api_hash", "phone")):
                values.pop("session", None)
        changed = values != context.values
        reference = self._registry._write_version(account_id, context.credential_revision + 1, values) if changed else None
        try:
            with self._database.connection() as connection:
                connection.execute("BEGIN IMMEDIATE")
                receipt = self._receipt(connection, key, digest)
                if receipt is not None:
                    if reference is not None:
                        self._registry._secrets.delete(reference)
                    return receipt
                row = self._registry._row(connection, account_id)
                if row["revision"] != revision:
                    raise AccountError("REVISION_CONFLICT", "账号已被另一端修改，请刷新后核对")
                if row["stop_operation_id"] is not None:
                    raise AccountError("ACCOUNT_STOPPING", "账号已有未完成停止操作")
                if action == "edit" and not changed:
                    if label is not None and label != row["label"]:
                        connection.execute(
                            "UPDATE external_accounts SET label = ?, revision = revision + 1, updated_at = CURRENT_TIMESTAMP WHERE account_id = ?",
                            (label, account_id),
                        )
                    updated = self._registry._public(self._registry._row(connection, account_id))
                    result = {"account_id": account_id, "action": action, "status": "COMPLETED", "result": updated}
                    connection.execute(
                        "INSERT INTO account_command_receipts(idempotency_key, payload_hash, response_json) VALUES(?, ?, ?)",
                        (key, digest, json.dumps(result, separators=(",", ":"))),
                    )
                    return result
                operation_id = uuid4().hex
                connection.execute(
                    "INSERT INTO account_operations(operation_id, account_id, action, credential_revision, "
                    "next_credentials_ref, next_fields_json, next_label) VALUES(?, ?, ?, ?, ?, ?, ?)",
                    (operation_id, account_id, action, context.credential_revision, reference,
                     json.dumps(sorted(values)) if changed else None, label),
                )
                connection.execute(
                    "UPDATE external_accounts SET enabled = 0, activation_requested = 0, stop_operation_id = ?, revision = revision + 1, "
                    "updated_at = CURRENT_TIMESTAMP WHERE account_id = ?", (operation_id, account_id),
                )
                self._stop_bound_executions(connection, account_id, operation_id)
                result = {"account_id": account_id, "operation_id": operation_id,
                          "action": action, "status": "PENDING", "revision": revision + 1}
                connection.execute(
                    "INSERT INTO account_command_receipts(idempotency_key, payload_hash, response_json) VALUES(?, ?, ?)",
                    (key, digest, json.dumps(result, separators=(",", ":"))),
                )
            return result
        except BaseException:
            if reference is not None:
                self._registry._discard_unreferenced(reference)
            raise

    def _stop_bound_executions(self, connection, account_id, operation_id):
        rows = connection.execute(
            "SELECT DISTINCT t.task_id, j.id, t.status, t.provider, j.details_json, a.finished_at FROM account_execution_bindings b "
            "JOIN queue_attempts a ON a.attempt_id = b.attempt_id "
            "JOIN queue_tasks t ON t.task_id = a.task_id "
            "LEFT JOIN download_jobs j ON j.task_id = t.task_id "
            "WHERE b.account_id = ? AND t.current_attempt_id = b.attempt_id", (account_id,),
        ).fetchall()
        for task_id, job_id, status, provider, details_json, finished_at in rows:
            details = json.loads(details_json or "{}")
            # The previous attempt remains current until its pending successor
            # is claimed. Its historical binding must not revoke that successor.
            if (provider in {"TELEGRAM", "TELEGRAM_USER", "EXHENTAI", "EH_TORRENT"} and finished_at is not None
                    and details.get("account_id") and details["account_id"] != account_id):
                continue
            existing = connection.execute(
                "SELECT operation_id FROM queue_operations WHERE task_id = ? AND status NOT IN ('COMPLETED', 'FAILED')",
                (task_id,),
            ).fetchall()
            if not existing and job_id is not None and status not in TERMINAL:
                tg_replacement = provider in {"TELEGRAM", "TELEGRAM_USER"} and details.get("observation_id") and details.get("origin_account_id")
                eh_replacement = (provider in {"EXHENTAI", "EH_TORRENT"} and details.get("account_pool") is not None
                    and connection.execute("SELECT 1 FROM queue_remote_resources WHERE task_id = ? AND status <> 'STOP_CONFIRMED'",
                                           (task_id,)).fetchone() is None)
                if tg_replacement or eh_replacement:
                    connection.execute("UPDATE download_jobs SET error_code = 'ACCOUNT_UNAVAILABLE', "
                                       "error_message = '下载账号已停用或正在修改配置' WHERE id = ?", (job_id,))
                    replacement_id = self._queue.request_account_replacement(
                        connection, job_id, task_snapshot(connection, job_id), stop_requested=True)
                    existing = [(replacement_id,)]
                else:
                    result = self._queue._apply(connection, job_id, "cancel", task_snapshot(connection, job_id), {})
                    if "operation_id" in result:
                        existing = [(result["operation_id"],)]
            elif (not existing and job_id is not None and status == "COMPLETED" and provider == "EH_TORRENT"
                    and json.loads(details_json or "{}").get("seeding")):
                result = self._queue._apply(connection, job_id, "stop-seeding", task_snapshot(connection, job_id), {})
                existing = [(result["operation_id"],)]
            for dependency in existing:
                connection.execute(
                    "INSERT INTO account_stop_dependencies(operation_id, queue_operation_id) VALUES(?, ?)",
                    (operation_id, dependency[0]),
                )

    async def get_operation(self, operation_id: str) -> dict:
        return await asyncio.to_thread(self._get_operation_sync, operation_id)

    def _get_operation_sync(self, operation_id):
        with self._database.connection() as connection:
            row = connection.execute(
                "SELECT operation_id, account_id, action, status, credential_revision, error_code, result_json FROM account_operations WHERE operation_id = ?",
                (operation_id,),
            ).fetchone()
            if row is None:
                raise AccountError("ACCOUNT_OPERATION_NOT_FOUND", "账号操作不存在")
            result = dict(zip(("operation_id", "account_id", "action", "status", "credential_revision", "error_code"), row[:6]))
            result["result"] = json.loads(row[6]) if row[6] else None
            return result

    async def process_operations(self, *, stop_runtime, process_queue_operations) -> int:
        """Retry pending intentions; adapters return only after physical stop.

        stop_runtime(account_id, credential_revision) returns True only after
        listeners, login and non-queue requests are stopped. False or an error
        leaves the durable intention pending. Queue failures never free it.
        """
        async with self._process_lock:
            operations = await asyncio.to_thread(self._pending_sync)
            completed = 0
            for operation in operations:
                try:
                    stopped = await stop_runtime(operation["account_id"], operation["credential_revision"])
                    if stopped is not True:
                        continue
                    await process_queue_operations()
                    completed += int(await asyncio.to_thread(self._finish_sync, operation["operation_id"]))
                except asyncio.CancelledError:
                    raise
                except Exception:
                    # Private adapter exceptions can contain provider secrets.
                    # Keep only a fixed public code and retry this intention.
                    await asyncio.to_thread(self._record_error_sync, operation["operation_id"])
                    continue
            return completed

    def _record_error_sync(self, operation_id):
        with self._database.connection() as connection:
            connection.execute(
                "UPDATE account_operations SET error_code = 'ACCOUNT_STOP_NOT_CONFIRMED', updated_at = CURRENT_TIMESTAMP "
                "WHERE operation_id = ? AND status = 'PENDING'", (operation_id,),
            )

    def _pending_sync(self):
        with self._database.connection() as connection:
            ids = [row[0] for row in connection.execute(
                "SELECT operation_id FROM account_operations WHERE status = 'PENDING' ORDER BY created_at, operation_id",
            )]
        return [self._get_operation_sync(operation_id) for operation_id in ids]

    def _finish_sync(self, operation_id):
        with self._database.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.row_factory = sqlite3.Row
            operation = connection.execute("SELECT * FROM account_operations WHERE operation_id = ?", (operation_id,)).fetchone()
            if operation is None or operation["status"] != "PENDING":
                return False
            if connection.execute(
                "SELECT 1 FROM account_stop_dependencies d JOIN queue_operations q ON q.operation_id = d.queue_operation_id "
                "WHERE d.operation_id = ? AND q.status <> 'COMPLETED'", (operation_id,),
            ).fetchone() is not None:
                return False
            account = self._registry._row(connection, operation["account_id"])
            if account["stop_operation_id"] != operation_id:
                raise AccountError("ACCOUNT_STOP_CHANGED", "账号停止意图已改变")
            next_revision = operation["credential_revision"] + int(operation["next_credentials_ref"] is not None)
            reference = operation["next_credentials_ref"] or account["credentials_ref"]
            if operation["next_credentials_ref"] is not None:
                connection.execute(
                    "INSERT INTO external_account_credentials(account_id, credential_revision, secret_ref) VALUES(?, ?, ?)",
                    (operation["account_id"], next_revision, reference),
                )
            connection.execute(
                "UPDATE external_accounts SET stop_operation_id = NULL, revision = revision + 1, credential_revision = ?, "
                "credentials_ref = ?, configured_fields_json = ?, label = ?, deleted_at = ?, updated_at = CURRENT_TIMESTAMP "
                "WHERE account_id = ?",
                (next_revision, reference, operation["next_fields_json"] or account["configured_fields_json"],
                 operation["next_label"] if operation["next_label"] is not None else account["label"],
                 connection.execute("SELECT CURRENT_TIMESTAMP").fetchone()[0] if operation["action"] == "delete" else None,
                 operation["account_id"]),
            )
            result = ({"account_id": operation["account_id"], "deleted": True} if operation["action"] == "delete"
                      else self._registry._public(self._registry._row(connection, operation["account_id"])))
            connection.execute(
                "UPDATE account_operations SET status = 'COMPLETED', result_json = ?, error_code = NULL, "
                "updated_at = CURRENT_TIMESTAMP WHERE operation_id = ?",
                (json.dumps(result, separators=(",", ":")), operation_id),
            )
            return True
