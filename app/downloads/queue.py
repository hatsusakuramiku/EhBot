"""Durable command arbitration shared by workers and administrator clients.

The SQL adapter mirrors legacy provider writes into task/attempt history. This
module owns short command transactions; provider preflight runs outside them.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import sqlite3
from uuid import uuid4

from app.db.database import Database
from app.downloads.models import MAX_JOB_PRIORITY, MIN_JOB_PRIORITY, PERMANENT_DOWNLOAD_ERRORS


class DownloadError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.public_message = message


TERMINAL = frozenset({"COMPLETED", "FAILED", "CANCELLED", "CONVERSION_COMPLETED", "CONVERSION_FAILED"})
STALE_EXECUTION_ERRORS = frozenset({"QUEUE_STOP_NOT_CONFIRMED", "QUEUE_STALE_ARTIFACT", "QUEUE_STALE_COMPLETION", "QUEUE_STALE_EXECUTION"})


def task_snapshot(connection, job_id: int) -> dict:
    row = connection.execute(
        "SELECT t.task_id, t.revision, t.generation, t.current_attempt_id, t.status "
        "FROM download_jobs j JOIN queue_tasks t ON t.task_id = j.task_id WHERE j.id = ?",
        (job_id,),
    ).fetchone()
    if row is None:
        raise DownloadError("JOB_NOT_FOUND", "下载任务不存在")
    return dict(zip(("task_id", "revision", "generation", "attempt_id", "state"), row))


class QueueControl:
    def __init__(self, database: Database, *, switch_preflight=None) -> None:
        self._database = database
        self._switch_preflight = switch_preflight

    async def get_task(self, task_id: str) -> dict:
        return await asyncio.to_thread(self._get_task_sync, task_id)

    def pending_operations(self) -> list[dict]:
        with self._database.connection() as connection:
            connection.row_factory = sqlite3.Row
            return [dict(row) for row in connection.execute(
                "SELECT o.*, t.provider FROM queue_operations o "
                "JOIN queue_tasks t ON t.task_id = o.task_id "
                "WHERE o.status NOT IN ('COMPLETED', 'FAILED') ORDER BY o.created_at, o.operation_id"
            )]

    def operation_error(self, operation_id: str, code: str) -> None:
        """An unconfirmed stop remains retryable and keeps its work slot."""
        with self._database.connection() as connection:
            connection.execute(
                "UPDATE queue_operations SET error_code = ?, updated_at = CURRENT_TIMESTAMP "
                "WHERE operation_id = ? AND status NOT IN ('COMPLETED', 'FAILED')",
                (code, operation_id),
            )

    def finish_operation(self, operation: dict) -> None:
        """After physical stop, release the old slot and enqueue its successor atomically."""
        with self._database.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            saved = connection.execute(
                "SELECT status FROM queue_operations WHERE operation_id = ?", (operation["operation_id"],),
            ).fetchone()
            if saved is None or saved[0] in {"COMPLETED", "FAILED"}:
                return
            if operation["action"] == "remove":
                # Business rows are already gone. The independent reservation
                # remains until every sibling stop and file cleanup completes.
                if connection.execute("SELECT 1 FROM download_jobs WHERE task_id = ?",
                                      (operation["task_id"],)).fetchone() is not None:
                    raise DownloadError("REMOVAL_NOT_COMMITTED", "移除事务尚未完成")
                connection.execute(
                    "UPDATE queue_attempts SET status = 'CANCELLED', finished_at = CURRENT_TIMESTAMP "
                    "WHERE attempt_id = ? AND finished_at IS NULL", (operation["attempt_id"],),
                )
                connection.execute(
                    "UPDATE queue_operations SET status = 'COMPLETED', error_code = NULL, "
                    "updated_at = CURRENT_TIMESTAMP WHERE operation_id = ?", (operation["operation_id"],),
                )
                return
            current = task_snapshot(connection, operation["legacy_job_id"])
            if current["task_id"] != operation["task_id"]:
                raise DownloadError("TASK_SUPERSEDED", "停止操作对应的旧任务已被替换")
            action = operation["action"]
            if action == "stop-seeding":
                connection.execute(
                    "UPDATE download_jobs SET details_json = json_set(details_json, '$.seeding', json('false')), "
                    "updated_at = CURRENT_TIMESTAMP WHERE id = ?", (operation["legacy_job_id"],),
                )
            else:
                if current["state"] != "STOPPING":
                    raise DownloadError("STOP_STATE_CHANGED", "停止意图的状态已改变，不能启动新来源")
                connection.execute(
                    "UPDATE download_jobs SET state = 'CANCELLED', lease_owner = NULL, lease_expires_at = NULL, "
                    "updated_at = CURRENT_TIMESTAMP WHERE id = ?", (operation["legacy_job_id"],),
                )
                if action in {"retry", "restart"}:
                    connection.execute(
                        "UPDATE download_jobs SET state = 'PENDING', details_json = '{}', error_code = NULL, "
                        "error_message = NULL, retry_at = NULL, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                        (operation["legacy_job_id"],),
                    )
                elif action == "switch-source":
                    provider = json.loads(operation["params_json"])["provider"]
                    if connection.execute("SELECT 1 FROM candidate_approvals WHERE candidate_id = ?",
                                          (operation["candidate_id"],)).fetchone() is None:
                        raise DownloadError("CANDIDATE_NOT_DOWNLOADABLE", "没有有效审批事实，不能切源")
                    key = f"{provider.lower()}:{operation['candidate_id']}"
                    connection.execute(
                        "INSERT INTO download_jobs(candidate_id, idempotency_key, provider, state, details_json) "
                        "VALUES (?, ?, ?, 'PENDING', '{}') ON CONFLICT(idempotency_key) DO UPDATE SET "
                        "state = 'PENDING', details_json = '{}', error_code = NULL, error_message = NULL, "
                        "lease_owner = NULL, lease_expires_at = NULL, retry_at = NULL, updated_at = CURRENT_TIMESTAMP",
                        (operation["candidate_id"], key, provider),
                    )
                if action in {"retry", "restart", "switch-source"}:
                    connection.execute(
                        "UPDATE candidates SET status = 'APPROVED', filter_reason = '', updated_at = CURRENT_TIMESTAMP "
                        "WHERE id = ? AND status IN ('FAILED', 'PROCESSING', 'NEEDS_INFO')",
                        (operation["candidate_id"],),
                    )
            connection.execute(
                "UPDATE queue_operations SET status = 'COMPLETED', error_code = NULL, updated_at = CURRENT_TIMESTAMP "
                "WHERE operation_id = ?", (operation["operation_id"],),
            )

    def _get_task_sync(self, task_id: str) -> dict:
        with self._database.connection() as connection:
            connection.row_factory = sqlite3.Row
            task = connection.execute("SELECT * FROM queue_tasks WHERE task_id = ?", (task_id,)).fetchone()
            if task is None:
                raise DownloadError("TASK_NOT_FOUND", "任务不存在")
            result = dict(task)
            result['attempts'] = [dict(row) for row in connection.execute(
                "SELECT * FROM queue_attempts WHERE task_id = ? ORDER BY attempt_no", (task_id,),
            )]
            result['operations'] = [dict(row) for row in connection.execute(
                "SELECT operation_id, action, status, error_code, created_at, updated_at "
                "FROM queue_operations WHERE task_id = ? ORDER BY created_at, operation_id", (task_id,),
            )]
            result['removals'] = [dict(row) for row in connection.execute(
                "SELECT r.removal_id, r.status, r.delete_files, r.deleted_json, r.failed_json "
                "FROM queue_removals r, json_each(r.task_ids_json) ids WHERE ids.value = ?",
                (task_id,),
            )]
            return result

    async def command(self, job_id: int, action: str, *, task_id: str,
                      expected_revision: int, idempotency_key: str, **params) -> dict:
        try:
            prepared_source = None
            if action == "switch-source" and self._switch_preflight is not None:
                receipt, prepared_source = await asyncio.to_thread(
                    self._command_sync, job_id, action, task_id, expected_revision,
                    idempotency_key, params, prepare=True,
                )
                if receipt is not None:
                    return receipt
                # Network validation must not hold SQLite's writer lock. The
                # command transaction below checks identity/revision again.
                try:
                    await self._switch_preflight(prepared_source[0], params["provider"])
                except Exception:
                    # A concurrent copy may already have committed this command
                    # while our read-only probe was in flight.
                    receipt, _ = await asyncio.to_thread(
                        self._command_sync, job_id, action, task_id, expected_revision,
                        idempotency_key, params, prepare=True,
                    )
                    if receipt is not None:
                        return receipt
                    raise
            return await asyncio.to_thread(self._command_sync, job_id, action, task_id,
                                           expected_revision, idempotency_key, params,
                                           prepared_source=prepared_source)
        except sqlite3.IntegrityError as exc:
            if 'queue_tasks.candidate_id' in str(exc):
                raise DownloadError("WORK_SLOT_OCCUPIED", "另一下载任务已占用该作品槽") from exc
            raise

    def internal_command(self, job_id: int, action: str, **params) -> dict:
        """Internal callers have no client draft; read and apply under one lock.

        HTTP/Web/WS must call command instead and supply their observed identity
        and revision. Never use this to repair missing client preconditions.
        """
        if action == "switch-source":
            raise DownloadError("SOURCE_PREFLIGHT_REQUIRED", "切源必须通过异步来源预检入口")
        try:
            with self._database.connection() as connection:
                connection.execute("BEGIN IMMEDIATE")
                current = task_snapshot(connection, job_id)
                return self._apply(connection, job_id, action, current, params)
        except sqlite3.IntegrityError as exc:
            if 'queue_tasks.candidate_id' in str(exc):
                raise DownloadError("WORK_SLOT_OCCUPIED", "另一下载任务已占用该作品槽") from exc
            raise

    def _command_sync(self, job_id, action, task_id, expected_revision, key, params, *,
                      prepare=False, prepared_source=None):
        if not isinstance(task_id, str) or not task_id:
            raise DownloadError("TASK_ID_REQUIRED", "请提供已读取的任务身份")
        if type(expected_revision) is not int or expected_revision < 1:
            raise DownloadError("REVISION_REQUIRED", "请提供已读取的任务版本")
        if not isinstance(key, str) or not 1 <= len(key) <= 200:
            raise DownloadError("IDEMPOTENCY_KEY_REQUIRED", "请提供 1–200 字符的幂等键")
        normalized = json.dumps([job_id, action, task_id, expected_revision, params],
                                sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()
        with self._database.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            receipt = connection.execute(
                "SELECT request_digest, result_json FROM queue_command_receipts "
                "WHERE scope = 'administrator' AND idempotency_key = ?", (key,),
            ).fetchone()
            if receipt is not None:
                if receipt[0] != digest:
                    raise DownloadError("IDEMPOTENCY_CONFLICT", "此幂等键已用于不同的命令")
                result = json.loads(receipt[1])
                return (result, None) if prepare else result
            current = task_snapshot(connection, job_id)
            if current["task_id"] != task_id:
                raise DownloadError("TASK_SUPERSEDED", "该旧任务已经结束，请刷新后操作新任务")
            if current["revision"] != expected_revision:
                raise DownloadError("REVISION_CONFLICT", "任务已被另一端修改，请刷新后核对")
            if prepare:
                if current["state"] in {"COMPLETED", "CONVERSION_COMPLETED"}:
                    raise DownloadError("JOB_ALREADY_COMPLETED", "已完成的任务无法取消或切源")
                if current["state"] in {"CANCELLED", "STOPPING"}:
                    raise DownloadError("JOB_NOT_CANCELLABLE", "任务已取消或正在停止")
                if params.get("provider") not in {"TELEGRAPH", "EXHENTAI"}:
                    raise DownloadError("PROVIDER_UNSUPPORTED", "不支持切换到此下载来源")
                candidate_id = connection.execute(
                    "SELECT candidate_id FROM download_jobs WHERE id = ?", (job_id,),
                ).fetchone()[0]
                return None, (candidate_id, self._switch_reference(connection, candidate_id, params["provider"]))
            if prepared_source is not None:
                candidate_id, reference = prepared_source
                if self._switch_reference(connection, candidate_id, params["provider"]) != reference:
                    raise DownloadError("SOURCE_CHANGED", "目标来源信息在预检期间已改变，请刷新后重试")
            result = self._apply(connection, job_id, action, current, params)
            connection.execute(
                "INSERT INTO queue_command_receipts(scope, idempotency_key, request_digest, result_json) "
                "VALUES ('administrator', ?, ?, ?)",
                (key, digest, json.dumps(result, separators=(",", ":"))),
            )
            # Do not expire receipts for operations that are still unresolved.
            connection.execute(
                "DELETE FROM queue_command_receipts WHERE created_at < datetime('now', '-7 days') "
                "AND NOT EXISTS (SELECT 1 FROM queue_operations o "
                "WHERE o.operation_id = json_extract(result_json, '$.operation_id') "
                "AND o.status NOT IN ('COMPLETED', 'FAILED'))"
            )
            return result

    @staticmethod
    def _switch_reference(connection, candidate_id, provider):
        row = connection.execute(
            "SELECT ex_gid, ex_gallery_token, preview_url FROM candidates WHERE id = ?", (candidate_id,),
        ).fetchone()
        if row is None:
            raise DownloadError("CANDIDATE_NOT_FOUND", "候选不存在或已被删除")
        if provider == "EXHENTAI":
            return row[:2]
        pages = connection.execute(
            "SELECT field_value FROM metadata_values WHERE candidate_id = ? AND field_name = 'Pages' "
            "ORDER BY is_manual DESC LIMIT 1", (candidate_id,),
        ).fetchone()
        return row[2], pages[0] if pages else None

    def _apply(self, connection, job_id, action, current, params):
        row = connection.execute(
            "SELECT candidate_id, provider, state, error_code, details_json FROM download_jobs WHERE id = ?",
            (job_id,),
        ).fetchone()
        candidate_id, provider, state, error_code, details_json = row
        operation_id = None
        if action == "priority":
            priority = params.get("priority")
            if type(priority) is not int or not MIN_JOB_PRIORITY <= priority <= MAX_JOB_PRIORITY:
                raise DownloadError("PRIORITY_OUT_OF_RANGE", "任务优先级超出允许范围")
            if state in TERMINAL or state == "STOPPING":
                raise DownloadError("JOB_NOT_QUEUED", "已结束的任务没有队列位置；正在停止的任务须等待确认")
            connection.execute("UPDATE download_jobs SET priority = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                               (priority, job_id))
        elif action in {"pause", "resume", "retry", "restart"}:
            if action == "pause":
                if state not in {"PENDING", "CONVERSION_PENDING"}:
                    raise DownloadError("JOB_NOT_PAUSABLE", "只有排队中的任务可以暂停")
                target = "PAUSED"
            elif action == "resume":
                if state != "PAUSED":
                    raise DownloadError("JOB_NOT_PAUSED", "只有已暂停的任务可以继续")
                target = "CONVERSION_PENDING" if provider == "CONVERSION" else "PENDING"
            else:
                allowed = {"FAILED", "CANCELLED", "PAUSED", "CONVERSION_FAILED"}
                if action == "restart":
                    allowed = TERMINAL
                if state not in allowed:
                    raise DownloadError("JOB_NOT_RETRYABLE", "只有已失败、已暂停或已取消的任务可以重试")
                if error_code in PERMANENT_DOWNLOAD_ERRORS:
                    raise DownloadError("JOB_PERMANENTLY_FAILED", "该失败原因无法通过重试解决")
                if provider != "CONVERSION" and connection.execute(
                    "SELECT 1 FROM candidate_approvals WHERE candidate_id = ?", (candidate_id,),
                ).fetchone() is None:
                    raise DownloadError("CANDIDATE_NOT_DOWNLOADABLE", "没有有效审批事实，不能重试下载")
                target = "CONVERSION_PENDING" if provider == "CONVERSION" else "PENDING"
                # A saved torrent handle may still refer to an external transfer.
                # P3 reconciles and stops it before retry becomes claimable.
                if provider == "EH_TORRENT" and details_json not in (None, "", "{}"):
                    operation_id = self._operation(connection, job_id, action, current, params)
                    target = "STOPPING"
            connection.execute(
                "UPDATE download_jobs SET state = ?, error_code = NULL, error_message = NULL, "
                "lease_owner = NULL, lease_expires_at = NULL, retry_at = NULL, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                (target, job_id),
            )
            if action in {"retry", "restart"} and target != "STOPPING" and provider != "CONVERSION":
                connection.execute(
                    "UPDATE candidates SET status = 'APPROVED', filter_reason = '', updated_at = CURRENT_TIMESTAMP "
                    "WHERE id = ? AND status IN ('FAILED', 'PROCESSING', 'NEEDS_INFO')", (candidate_id,),
                )
        elif action in {"cancel", "switch-source", "stop-seeding"}:
            if action == "stop-seeding":
                if provider != "EH_TORRENT" or state != "COMPLETED":
                    raise DownloadError("JOB_NOT_SEEDING", "只有已完成的种子任务可以停止做种")
                operation_id = self._operation(connection, job_id, action, current, params)
                connection.execute("UPDATE queue_tasks SET revision = revision + 1 WHERE task_id = ?", (current["task_id"],))
            else:
                if state in {"COMPLETED", "CONVERSION_COMPLETED"}:
                    raise DownloadError("JOB_ALREADY_COMPLETED", "已完成的任务无法取消或切源")
                if state in {"CANCELLED", "STOPPING"}:
                    raise DownloadError("JOB_NOT_CANCELLABLE", "任务已取消或正在停止")
                if action == "switch-source" and params.get("provider") not in {"TELEGRAPH", "EXHENTAI"}:
                    raise DownloadError("PROVIDER_UNSUPPORTED", "不支持切换到此下载来源")
                # Queued work has no running execution to stop. All other work
                # keeps the slot until the operation processor confirms cleanup.
                immediate = action == "cancel" and state in {"PENDING", "PAUSED", "CONVERSION_PENDING"}
                if not immediate:
                    operation_id = self._operation(connection, job_id, action, current, params)
                connection.execute(
                    "UPDATE download_jobs SET state = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                    ("CANCELLED" if immediate else "STOPPING", job_id),
                )
        else:
            raise DownloadError("ACTION_UNKNOWN", "未知任务命令")
        result = {"job_id": job_id, "action": action, **task_snapshot(connection, job_id)}
        if operation_id is not None:
            result["operation_id"] = operation_id
        if action == "priority":
            result["priority"] = params["priority"]
        return result

    @staticmethod
    def _operation(connection, job_id, action, current, params):
        if connection.execute(
            "SELECT 1 FROM queue_operations WHERE task_id = ? AND status NOT IN ('COMPLETED', 'FAILED')",
            (current['task_id'],),
        ).fetchone() is not None:
            raise DownloadError("OPERATION_IN_PROGRESS", "该任务已有未完成的收尾操作")
        operation_id = uuid4().hex
        connection.execute(
            "INSERT INTO queue_operations(operation_id, task_id, attempt_id, generation, "
            "legacy_job_id, candidate_id, action, params_json, resource_json) "
            "SELECT ?, task_id, ?, ?, id, candidate_id, ?, ?, details_json FROM download_jobs WHERE id = ?",
            (operation_id, current["attempt_id"], current["generation"], action,
             json.dumps(params, separators=(",", ":")), job_id),
        )
        return operation_id
