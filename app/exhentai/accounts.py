"""Finite authenticated metadata reads using the shared account selector."""
from __future__ import annotations

import asyncio
from contextlib import AsyncExitStack, closing
import json
from uuid import uuid4

from app.connections.accounts import AccountError
from app.downloads.execution import run_sync
from app.exhentai.downloader import ExHentaiDownloadError
from app.exhentai.health import ExHentaiHealth


class MetadataAccountPool:
    """Own one finite read operation; never retry a paid archive request.

    A successful account scope moves to the caller's stack so its commit is
    still fenced by that credential version. Failed scopes exit completely
    before another account is selected. Process-crash reconciliation of these
    records belongs to the worker-session recovery phase.
    """

    def __init__(self, accounts):
        self._accounts = accounts
        self._database = accounts.registry._database
        self._health = ExHentaiHealth(self._database)

    def _begin(self, operation_id, gid, connected):
        with self._database.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            rows = connection.execute(
                "SELECT ea.account_id, ea.label, ea.credential_revision, ea.enabled, "
                "ea.stop_operation_id, ea.verified_credential_revision, "
                "ar.cooldown_until > CURRENT_TIMESTAMP, ar.cooldown_until "
                "FROM external_accounts ea LEFT JOIN account_rotation ar ON ar.account_id = ea.account_id "
                "WHERE ea.kind = 'EH' AND ea.deleted_at IS NULL "
                "ORDER BY ar.last_sequence IS NOT NULL, ar.last_sequence, ea.created_at, ea.account_id",
            ).fetchall()
            candidates = []
            for account_id, label, revision, enabled, stopping, verified, cooling, retry_at in rows:
                reason = ("ACCOUNT_DISABLED" if not enabled else "ACCOUNT_STOPPING" if stopping
                          else "ACCOUNT_UNVERIFIED" if verified != revision else "ACCOUNT_COOLDOWN" if cooling
                          else "ACCOUNT_UNAVAILABLE" if account_id not in connected else None)
                health = self._health.account_failure(connection, account_id, revision, "METADATA")
                if reason is None and health is not None:
                    reason = health[0]
                    retry_at = health[1]
                candidates.append({"account_id": account_id, "label": label,
                                   "credential_revision": revision, "unavailable_reason": reason, "retry_at": retry_at})
            connection.execute(
                "INSERT INTO eh_metadata_operations(operation_id, gid, candidates_json, status) VALUES(?, ?, ?, 'RUNNING')",
                (operation_id, gid, json.dumps(candidates, separators=(",", ":"))),
            )
        return candidates

    def _start_attempt(self, operation_id, account, ordinal):
        with self._database.connection() as connection:
            connection.execute(
                "INSERT INTO eh_metadata_attempts(operation_id, account_id, credential_revision, ordinal, status) "
                "VALUES(?, ?, ?, ?, 'STARTED')",
                (operation_id, account["account_id"], account["credential_revision"], ordinal),
            )

    def _finish_attempt(self, operation_id, account_id, status, code):
        with self._database.connection() as connection:
            connection.execute(
                "UPDATE eh_metadata_attempts SET status = ?, error_code = ?, finished_at = CURRENT_TIMESTAMP "
                "WHERE operation_id = ? AND account_id = ? AND status = 'STARTED'",
                (status, code, operation_id, account_id),
            )

    def _finish(self, operation_id, status, code, results):
        # Only diagnostics are written here. Cancellation/stop may have revoked
        # the caller's queue/account fence, so do not use that production commit
        # context to lose the cancellation record. No metadata or task is changed.
        with closing(self._database._connect()) as connection:
            with connection:
                connection.execute("BEGIN IMMEDIATE")
                connection.execute(
                    "UPDATE eh_metadata_attempts SET status = ?, error_code = ?, finished_at = CURRENT_TIMESTAMP "
                    "WHERE operation_id = ? AND status = 'STARTED'",
                    ("CANCELLED" if status == "CANCELLED" else "UNKNOWN", code, operation_id),
                )
                connection.execute(
                    "UPDATE eh_metadata_operations SET status = ?, error_code = ?, results_json = ?, "
                    "finished_at = CURRENT_TIMESTAMP WHERE operation_id = ? AND status = 'RUNNING'",
                    (status, code, json.dumps(results, separators=(",", ":")), operation_id),
                )

    async def fetch(self, gid, fetch_metadata, retained_scope: AsyncExitStack):
        """Try each captured account/version once and retain the successful lease."""
        await self._health.check_shared()
        connected = {account_id for account_id, state in self._accounts._states.items() if state.state == "connected"}
        operation_id = uuid4().hex
        results = []
        try:
            candidates = await run_sync(self._begin, operation_id, gid, connected)
            versions = {account["account_id"]: account["credential_revision"] for account in candidates
                        if account["unavailable_reason"] is None}
            labels = {account["account_id"]: account["label"] for account in candidates}
            results.extend({"account_id": account["account_id"], "label": account["label"], "stage": "METADATA",
                            "status": "UNAVAILABLE", "error_code": account["unavailable_reason"], "retry_at": account["retry_at"]}
                           for account in candidates if account["unavailable_reason"] is not None)
            attempted = set()
            first = await self._accounts.select_account("EH", candidate_versions=versions, capability="METADATA")
            # Other operations may advance the shared cursor between failures;
            # this operation keeps its captured remainder in a fixed order.
            order = ([first["account_id"]] + [account_id for account_id in versions
                     if account_id != first["account_id"]]) if first is not None else []
            for account_id in order:
                account = first if account_id == first["account_id"] else await self._accounts.select_account(
                    "EH", candidate_versions={account_id: versions[account_id]}, capability="METADATA")
                if account is None:
                    continue
                attempted.add(account_id)
                await run_sync(self._start_attempt, operation_id, account, len(attempted))
                try:
                    async with AsyncExitStack() as scope:
                        context = await scope.enter_async_context(self._accounts.use_account(account_id, kind="EH"))
                        if context.credential_revision != versions[account_id]:
                            raise AccountError("ACCOUNT_UNAVAILABLE", "账号凭据已改变，本次不再借用")
                        metadata = await fetch_metadata(context)
                        await run_sync(self._finish_attempt, operation_id, account_id, "SUCCEEDED", None)
                        results.append({"account_id": account_id, "label": labels[account_id], "stage": "METADATA",
                                        "status": "SUCCEEDED", "error_code": None})
                        await run_sync(self._finish, operation_id, "SUCCEEDED", None, results)
                        retained_scope.push_async_exit(scope.pop_all())
                        return metadata
                except (ExHentaiDownloadError, AccountError) as exc:
                    await self._health.record_failure(exc, account_id=account_id,
                                                      credential_revision=versions[account_id])
                    self._accounts._notify()
                    status = "UNAVAILABLE" if isinstance(exc, AccountError) else "FAILED"
                    await run_sync(self._finish_attempt, operation_id, account_id, status, exc.code)
                    results.append({"account_id": account_id, "label": labels[account_id], "stage": "METADATA",
                                    "status": status, "error_code": exc.code})
                    if getattr(exc, "failure_scope", None) in {"site", "gdata"}:
                        results[-1]["retry_after"] = exc.retry_after
                        await run_sync(self._finish, operation_id, "FAILED", exc.code, results)
                        raise
            results.extend({"account_id": account_id, "label": labels[account_id], "stage": "METADATA",
                            "status": "UNAVAILABLE", "error_code": "ACCOUNT_NO_LONGER_AVAILABLE"}
                           for account_id in versions if account_id not in attempted)
            code = "EH_ALL_ACCOUNTS_FAILED" if any(result["status"] == "FAILED" for result in results) else "EH_NO_ACCOUNT_AVAILABLE"
            await run_sync(self._finish, operation_id, "FAILED", code, results)
        except BaseException as exc:
            cancelled = isinstance(exc, asyncio.CancelledError)
            await run_sync(self._finish, operation_id, "CANCELLED" if cancelled else "UNKNOWN",
                           "EH_METADATA_CANCELLED" if cancelled else "EH_METADATA_INTERRUPTED", results)
            raise
        message = "所有可用 EH 账号获取元数据失败" if code == "EH_ALL_ACCOUNTS_FAILED" else "没有可用于元数据的 EH 账号"
        details = "；".join(f"{result['label']}: {result['error_code']}" for result in results)
        error = ExHentaiDownloadError(code, message + ("；" + details if details else ""))
        error.account_results = results
        error.operation_id = operation_id
        raise error


class DownloadAccountPool:
    """Select immutable EH accounts for the existing queue's separate attempts.

    This only chooses credentials. QueueControl owns stopping, cancellation and
    resuming the task. An unresolved charge always pins its original account.
    """

    def __init__(self, accounts):
        self._accounts = accounts
        self._database = accounts.registry._database
        self._health = ExHentaiHealth(self._database)

    def _input(self, candidate_id, task_id, details, capability):
        with self._database.connection() as connection:
            candidate = connection.execute("SELECT ex_gid, magnet_url FROM candidates WHERE id = ?", (candidate_id,)).fetchone()
            if candidate is None:
                raise ExHentaiDownloadError("CANDIDATE_NOT_FOUND", "作品不存在或已移除")
            if capability == "TORRENT" and candidate[1]:
                return None, None
            pinned = None
            if capability == "ARCHIVE":
                pinned = connection.execute(
                    "SELECT intent_id, account_id, credential_revision, status FROM eh_archive_intents "
                    "WHERE gid = ? AND (status IN ('REQUESTED', 'UNKNOWN') OR "
                    "(status = 'READY' AND (task_id = ? OR intent_id = ?))) "
                    "ORDER BY status <> 'UNKNOWN', status <> 'REQUESTED', created_at DESC LIMIT 1",
                    (candidate[0], task_id, details.get("eh_archive_intent_id")),
                ).fetchone()
            versions = details.get("account_versions")
            if versions is None:
                versions = dict(connection.execute(
                    "SELECT account_id, credential_revision FROM external_accounts WHERE kind = 'EH' "
                    "AND enabled = 1 AND deleted_at IS NULL AND stop_operation_id IS NULL "
                    "AND verified_credential_revision = credential_revision ORDER BY created_at, account_id",
                ).fetchall())
        return versions, pinned

    async def select(self, candidate_id, task_id, details, *, capability="ARCHIVE"):
        """Return a finite pool and exact version, or a safe terminal error."""
        versions, pinned = await run_sync(self._input, candidate_id, task_id, details, capability)
        if versions is None:
            return {}
        await self._health.check_shared()
        if pinned is not None:
            intent_id, account_id, revision, _ = pinned
            try:
                account = await self._accounts.registry.get(account_id)
                with self._database.connection() as connection:
                    failure = self._health.account_failure(connection, account_id, revision)
                if (account["credential_revision"] != revision or not account["enabled"]
                        or account["stop_operation_id"] is not None
                        or failure is not None
                        or self._accounts.account_status(account_id)["state"] != "connected"):
                    raise AccountError("ACCOUNT_UNAVAILABLE", "原申请账号不可用")
            except AccountError:
                raise ExHentaiDownloadError("EH_ARCHIVE_RESULT_UNKNOWN", "原 EH 申请需要核实；原账号版本不可用，不能换账号重新申请") from None
            return {**details, "account_id": account_id, "credential_revision": revision,
                    "eh_archive_intent_id": intent_id, "account_versions": versions,
                    "account_pool": list(versions), "attempted_accounts": details.get("attempted_accounts", [])}
        attempted = details.get("attempted_accounts", [])
        account = await self._accounts.select_account("EH", excluded_account_ids=attempted,
            candidate_versions=versions, capability=capability)
        if account is None:
            code = "EH_ALL_ACCOUNTS_FAILED" if attempted else "EH_NO_ACCOUNT_AVAILABLE"
            raise ExHentaiDownloadError(code, "本轮 EH 账号均失败或不可用，请查看各次尝试")
        return {**details, "account_id": account["account_id"], "credential_revision": account["credential_revision"],
                "account_versions": versions, "account_pool": list(versions), "attempted_accounts": attempted}
