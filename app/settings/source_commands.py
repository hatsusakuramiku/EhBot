"""One transactional source configuration writer for HTTP, forms and WS."""
from __future__ import annotations

import asyncio
from hashlib import sha256
import json
from uuid import uuid4

from app.db.database import Database
from app.settings.sources import SourceFieldError, parse_source, parse_source_rules, selected_source_ids


class SourceCommandError(SourceFieldError):
    def __init__(self, code, message, *, status_code=400, details=None):
        super().__init__(code, message)
        self.status_code = status_code
        self.details = details or {}


def _revision(value):
    if type(value) is int and value >= 0:
        return value
    if isinstance(value, str) and value.isascii() and value.isdecimal():
        return int(value)
    raise SourceCommandError("PRECONDITION_REQUIRED", "请提交已读取的来源版本；新建使用 0", status_code=428)


def _parsed(reader, values):
    result, error = reader(values)
    if error is not None:
        raise SourceCommandError(error.code, error.public_message)
    return result


def _prepare(action, values):
    if action == "save":
        return {"source": _parsed(parse_source, values),
                "expected_revision": _revision(values.get("expected_revision"))}
    if action == "add":
        entries = values.get("entries")
        if not isinstance(entries, list) or not entries:
            raise SourceCommandError("SOURCE_IDS_REQUIRED", "请至少选择一个会话")
        parsed = []
        seen = set()
        for entry in entries:
            if not isinstance(entry, dict):
                raise SourceCommandError("SOURCE_INVALID", "会话格式无效")
            source = _parsed(parse_source, entry)
            if source["chat_id"] in seen:
                raise SourceCommandError("SOURCE_INVALID", "会话选择重复")
            seen.add(source["chat_id"])
            parsed.append({key: source[key] for key in ("source_type", "chat_id", "display_name")}
                          | {"expected_revision": _revision(entry.get("expected_revision"))})
        return {"account_id": str(values.get("account_id") or "").strip() or None,
                "entries": parsed}
    if action not in {"enable", "disable", "delete", "apply"}:
        raise SourceCommandError("ACTION_UNKNOWN", "未知的来源动作")
    source_ids = selected_source_ids(values)
    if not source_ids or any(value < 1 for value in source_ids):
        raise SourceCommandError("SOURCE_IDS_REQUIRED", "请至少选择一个来源")
    revisions = values.get("expected_revisions")
    if isinstance(revisions, str):
        try:
            revisions = json.loads(revisions)
        except ValueError:
            revisions = None
    if not isinstance(revisions, dict):
        raise SourceCommandError("PRECONDITION_REQUIRED", "请提交每条来源已读取的版本", status_code=428)
    return {"source_ids": source_ids,
            "expected_revisions": {str(value): _revision(revisions.get(str(value))) for value in source_ids},
            **({"rules": _parsed(parse_source_rules, values)} if action == "apply" else {})}


class SourceCommands:
    """Serialize only the short database mutation; no network work under lock."""

    def __init__(self, database: Database):
        self.database = database

    async def execute(self, action, values):
        key = values.get("idempotency_key")
        if not isinstance(key, str) or not key.strip() or len(key) > 128:
            raise SourceCommandError("IDEMPOTENCY_KEY_REQUIRED", "请提交 1–128 字符的幂等键", status_code=428)
        payload = _prepare(action, values)
        digest = sha256(json.dumps({"action": action, **payload}, sort_keys=True,
                                   separators=(",", ":")).encode()).hexdigest()
        return await asyncio.to_thread(self._execute, action, payload, key, digest)

    @staticmethod
    def _receipt(connection, key, digest=None):
        row = connection.execute(
            "SELECT payload_hash, response_json FROM source_command_receipts "
            "WHERE scope = 'administrator' AND idempotency_key = ?", (key,),
        ).fetchone()
        if row is None:
            return None
        if digest is not None and row[0] != digest:
            raise SourceCommandError("IDEMPOTENCY_CONFLICT", "此幂等键已用于不同的来源命令", status_code=409)
        return json.loads(row[1])

    async def receipt(self, key):
        return await asyncio.to_thread(self._read_receipt, key)

    def _read_receipt(self, key):
        with self.database.connection() as connection:
            result = self._receipt(connection, key)
        if result is None:
            raise SourceCommandError("COMMAND_NOT_FOUND", "来源命令回执不存在", status_code=404)
        return result

    async def versions(self):
        return await asyncio.to_thread(self._versions)

    def _versions(self):
        # Includes tombstones so explicit re-addition can acknowledge their
        # version. Old creation requests must never silently resurrect them.
        with self.database.connection() as connection:
            return [{"source_id": row[0], "account_id": row[1], "chat_id": row[2],
                     "revision": row[3], "dismissed": bool(row[4])}
                    for row in connection.execute(
                        "SELECT ts.id, ta.external_account_id, ts.chat_id, ts.revision, ts.dismissed "
                        "FROM telegram_sources ts JOIN telegram_accounts ta ON ta.id = ts.account_id "
                        "WHERE ts.legacy_retired = 0 ORDER BY ts.id")]

    @staticmethod
    def _row(connection, where, parameters):
        return connection.execute(
            "SELECT id, revision, dismissed, source_type, chat_id, display_name, enabled, rules_json, legacy_retired "
            "FROM telegram_sources WHERE " + where, parameters,
        ).fetchone()

    @staticmethod
    def _check(row, expected):
        if (row is None and expected == 0) or (row is not None and row[1] == expected):
            return
        current = None if row is None else {
            "source_id": row[0], "revision": row[1], "dismissed": bool(row[2]),
            "source_type": row[3], "chat_id": row[4], "display_name": row[5],
            "enabled": bool(row[6]), "rules_json": row[7],
        }
        raise SourceCommandError("REVISION_CONFLICT", "来源已变化，请核对当前配置与草稿后再提交", status_code=409,
                                 details={"current": current})

    def _account(self, connection, account_id):
        if account_id is None and connection.execute("SELECT 1 FROM legacy_telegram_source_imports LIMIT 1").fetchone():
            raise SourceCommandError("ACCOUNT_SELECTION_REQUIRED", "旧共享来源已迁移，请明确选择监听账号")
        try:
            return self.database._ensure_telegram_account(connection, account_id)
        except ValueError:
            raise SourceCommandError("ACCOUNT_INVALID", "所选 Telegram 账号不存在或已删除") from None

    def _save(self, connection, source, expected):
        account_id = self._account(connection, source["account_id"])
        row = self._row(connection, "account_id = ? AND chat_id = ?", (account_id, source["chat_id"]))
        self._check(row, expected)
        rules = {key: value for key, value in source.items()
                 if key not in {"account_id", "source_type", "chat_id", "display_name", "enabled"}}
        params = (source["source_type"], source["display_name"], int(source["enabled"]),
                  json.dumps(rules, separators=(",", ":")))
        if row is None:
            source_id = connection.execute(
                "INSERT INTO telegram_sources(source_type, display_name, enabled, rules_json, account_id, chat_id) "
                "VALUES (?, ?, ?, ?, ?, ?)", (*params, account_id, source["chat_id"]),
            ).lastrowid
        else:
            source_id = row[0]
            connection.execute(
                "UPDATE telegram_sources SET source_type = ?, display_name = ?, enabled = ?, rules_json = ?, "
                "dismissed = 0 WHERE id = ?", (*params, source_id),
            )
        return self._result(connection, source_id, created=row is None)

    @staticmethod
    def _result(connection, source_id, *, status="COMPLETED", created=False):
        row = connection.execute("SELECT revision, chat_id FROM telegram_sources WHERE id = ?", (source_id,)).fetchone()
        return {"source_id": source_id, "chat_id": row[1], "revision": row[0], "status": status, "created": created}

    def _batch_item(self, connection, action, source_id, expected, rules=None):
        row = self._row(connection, "id = ?", (source_id,))
        if row is None:
            raise SourceCommandError("SOURCE_NOT_FOUND", "来源不存在", status_code=404)
        self._check(row, expected)
        if row[8]:
            raise SourceCommandError("SOURCE_MIGRATED", "旧共享来源已迁移，请重新读取账号专属来源", status_code=409)
        if row[2]:
            raise SourceCommandError("SOURCE_DISMISSED", "来源已删除，请明确重新添加", status_code=409)
        if action == "delete":
            connection.execute("UPDATE telegram_sources SET dismissed = 1, enabled = 0, rules_json = '{}' WHERE id = ?", (source_id,))
        elif action == "apply":
            connection.execute("UPDATE telegram_sources SET rules_json = ? WHERE id = ?",
                               (json.dumps(rules, separators=(",", ":")), source_id))
        else:
            connection.execute("UPDATE telegram_sources SET enabled = ? WHERE id = ?", (int(action == "enable"), source_id))
        return self._result(connection, source_id)

    def _add_item(self, connection, account_id, entry):
        row = self._row(connection, "account_id = ? AND chat_id = ?", (account_id, entry["chat_id"]))
        self._check(row, entry["expected_revision"])
        if row is None:
            source_id = connection.execute(
                "INSERT INTO telegram_sources(account_id, source_type, chat_id, display_name, enabled) VALUES(?, ?, ?, ?, 0)",
                (account_id, entry["source_type"], entry["chat_id"], entry["display_name"]),
            ).lastrowid
            return self._result(connection, source_id, created=True)
        if row[2]:
            connection.execute("UPDATE telegram_sources SET dismissed = 0, enabled = 0 WHERE id = ?", (row[0],))
            return self._result(connection, row[0])
        return self._result(connection, row[0], status="SKIPPED")

    def _execute(self, action, payload, key, digest):
        with self.database.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            replay = self._receipt(connection, key, digest)
            if replay is not None:
                return replay
            if action == "save":
                results = [self._save(connection, payload["source"], payload["expected_revision"])]
            else:
                account_id = self._account(connection, payload["account_id"]) if action == "add" else None
                items = payload["entries"] if action == "add" else payload["source_ids"]
                results = []
                for item in items:
                    try:
                        result = (self._add_item(connection, account_id, item) if action == "add" else
                                  self._batch_item(connection, action, item, payload["expected_revisions"][str(item)], payload.get("rules")))
                    except SourceCommandError as exc:
                        result = {**({"chat_id": item["chat_id"]} if action == "add" else {"source_id": item}),
                                  "status": "FAILED", "error": {"code": exc.code, "message": exc.public_message,
                                                                      "details": exc.details}}
                    results.append(result)
            failed = sum(item["status"] == "FAILED" for item in results)
            response = {"command_id": str(uuid4()), "idempotency_key": key, "action": action,
                        "status": "FAILED" if failed == len(results) else "PARTIAL" if failed else "COMPLETED", "items": results}
            connection.execute(
                "INSERT INTO source_command_receipts(idempotency_key, payload_hash, response_json) VALUES(?, ?, ?)",
                (key, digest, json.dumps(response, separators=(",", ":"))),
            )
            return response
