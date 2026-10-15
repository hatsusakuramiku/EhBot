from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from app.candidates import links
from app.candidates.entries import IngestError, fail_claim, load_message, renew_claim, resolve_message_identity
from app.candidates.persistence import prepare_observation, publish_candidate
from app.candidates.models import (
    IngestSummary,
    ParsedSourceMessage,
    RuleDecision,
)
from app.candidates.parse_rules import (
    PARSE_RULES_KEY,
    message_qualifies,
    parse_rules_view,
)
from app.candidates.rules import evaluate_source_rules
from app.db.database import Database
from app.downloads.execution import run_sync


class CandidateIngestor:
    """Turn incoming messages into candidates, or decide not to.

    Both Telegram transports use process_message: local source checks, durable
    identity registration/claim, optional AI, then guarded candidate publication.
    Duplicate receivers supplement one record before expensive work starts.
    """

    def __init__(
        self,
        database: Database,
        *,
        settings_service: object | None = None,
        admission: object | None = None,
    ) -> None:
        self._database = database
        self._settings = settings_service
        self._admission = admission
        self._processor_runtime = database.ingest_processors
        self._processors = self._processor_runtime.tasks

    def set_admission(self, admission: object | None) -> None:
        """Wire the AI gate after startup.

        The AI service is built after the connection manager (which owns this
        ingestor) because it needs the archive settings the manager does not, so
        the gate is attached rather than passed to the constructor. `None` turns
        it off, which is what a deployment with no AI configuration ever sees.
        """
        self._admission = admission

    # ------------------------------------------------------------- admission

    async def parse_rules(self) -> dict:
        """The current scheme, from the settings service or straight from k/v."""
        if self._settings is not None:
            return await self._settings.parse_rules()  # type: ignore[attr-defined]
        stored = await self._database.system_settings()
        return parse_rules_view(stored.get(PARSE_RULES_KEY))

    async def admit_message(
        self, message: ParsedSourceMessage, *, rules: dict | None = None
    ) -> RuleDecision:
        """Gate one parsed message, then apply the source's own filters.

        Returns the decision the caller stores on the candidate: `IGNORE` means
        nothing is written, `NEEDS_INFO` means the candidate exists but is
        flagged, `ACCEPT` means it is a normal pending candidate.
        """
        if rules is None:
            rules = await self.parse_rules()
        source = await self._database.discover_telegram_source(message)
        decision = evaluate_source_rules(source, message)
        if decision.result == "IGNORE":
            return decision
        gate = await self._gate(message, rules)
        if gate.result == "IGNORE":
            return gate
        return decision

    async def _gate(
        self, message: ParsedSourceMessage, rules: dict
    ) -> RuleDecision:
        """AI admission and structural rules, after durable ownership was acquired."""
        if self._admission is not None:
            admission = await self._admission.decide(message)  # type: ignore[attr-defined]
            if admission.verdict == "reject":
                return RuleDecision("IGNORE", admission.reason)
            if admission.verdict == "accept" and admission.override:
                return RuleDecision("ACCEPT", admission.reason)
        qualified, reason = message_qualifies(rules, message)
        if not qualified:
            return RuleDecision("IGNORE", reason)
        return RuleDecision("ACCEPT", reason)

    async def process_message(self, message: ParsedSourceMessage, *, rules: dict | None = None,
                              update_id: int | None = None) -> tuple[bool, RuleDecision]:
        """Persist one observing account's input; only the identity owner runs AI."""
        from app.connections.runtime import current_account_context
        context = current_account_context.get()
        if context is not None and context.kind in {"TG_BOT", "TG_USER"}:
            if message.account_id is not None and message.account_id != context.account_id:
                raise IngestError("INGEST_ACCOUNT_MISMATCH")
            message = replace(message, account_id=context.account_id)
        if message.account_id is not None:
            message = replace(message, attachments=tuple(
                {**attachment, "account_id": message.account_id} for attachment in message.attachments
            ))
        message = await run_sync(self._resolve_message, message)
        rules = await self.parse_rules() if rules is None else rules
        if message.is_edit:
            await self._stop_previous_processor(message.chat_id, message.message_id, message.account_id)
        source = await self._database.discover_telegram_source(message)
        local = evaluate_source_rules(source, message)
        qualified, reason = message_qualifies(rules, message)
        if local.result == "IGNORE" or (self._admission is None and not qualified):
            decision = local if local.result == "IGNORE" else RuleDecision("IGNORE", reason)
            if message.is_edit:
                await self._database.deactivate_candidate_message(message.chat_id, message.message_id, account_id=message.account_id)
            if update_id is not None:
                await self._database.mark_telegram_update_result(update_id, decision.result, decision.reason)
            return False, decision
        message = replace(message, filter_result=local.result, filter_reason=local.reason, rule_source_id=source.source_id)
        try:
            async with self._processor_runtime.lock:
                entry, claim, fixed_input = await run_sync(self._prepare_message, message, qualified)
                if claim is not None:
                    task = asyncio.create_task(self._execute_claim(claim, fixed_input, rules, update_id))
                    self._processors[claim.entry_id] = task
        except IngestError as exc:
            if exc.code != "INGEST_SOURCE_UNQUALIFIED":
                raise
            decision = RuleDecision("IGNORE", reason)
            if update_id is not None:
                await self._database.mark_telegram_update_result(update_id, decision.result, decision.reason)
            return False, decision
        if claim is None:
            decision = RuleDecision(
                "IGNORE" if entry["state"] in {"IGNORED", "REMOVED", "MERGED", "BLOCKED"} else local.result,
                "已保存备用来源" if entry["state"] == "LINKED" else "已登记，复用现有处理记录",
            )
            if update_id is not None:
                await self._database.mark_telegram_update_result(update_id, decision.result, decision.reason)
            return False, decision
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            if task.cancelled() and not asyncio.current_task().cancelling():
                decision = RuleDecision("IGNORE", "输入已更新，旧处理已撤销")
                if update_id is not None:
                    await self._database.mark_telegram_update_result(update_id, decision.result, decision.reason)
                return False, decision
            await self._drain_processor(task)
            raise
        finally:
            if self._processors.get(claim.entry_id) is task:
                del self._processors[claim.entry_id]

    async def _execute_claim(self, claim, fixed_input, rules, update_id):
        finished = asyncio.Event()
        try:
            async with asyncio.TaskGroup() as group:
                group.create_task(self._heartbeat(claim, finished))
                try:
                    decision = await self.admit_message(fixed_input, rules=rules)
                finally:
                    finished.set()
            decided = replace(fixed_input, filter_result=decision.result, filter_reason=decision.reason)
            created = await run_sync(self._publish_message, claim, decided, update_id)
            return created, decision
        except BaseException:
            await run_sync(self._processing_failed, claim)
            raise

    async def _stop_previous_processor(self, chat_id, message_id, account_id):
        entry_id = await run_sync(self._message_entry_id, chat_id, message_id, account_id)
        task = self._processors.get(entry_id)
        if task is not None and not task.done():
            await self._drain_processor(task)
            if asyncio.current_task().cancelling():
                raise asyncio.CancelledError

    async def _drain_processor(self, task):
        """Cancel once and await actual cleanup even if the waiting listener is cancelled again."""
        await self._processor_runtime.drain(task)

    def _message_entry_id(self, chat_id, message_id, account_id):
        with self._database.connection() as connection:
            row = connection.execute(
                "SELECT sm.ingest_entry_id FROM source_messages sm JOIN telegram_accounts ta ON ta.id = sm.account_id "
                "WHERE sm.chat_id = ? AND sm.message_id = ? AND "
                "((? IS NULL AND ta.session_path = 'bot-api://configured') OR ta.external_account_id = ?)",
                (chat_id, message_id, account_id, account_id),
            ).fetchone()
            return row[0] if row is not None else None

    async def process_pending_entries(self) -> int:
        """Recover persisted due inputs even after their transport event was acknowledged."""
        async with self._processor_runtime.lock:
            messages = await run_sync(self._due_inputs)
        created = 0
        rules = await self.parse_rules()
        for message in messages:
            was_created, _ = await self.process_message(message, rules=rules)
            created += int(was_created)
        return created

    def _due_inputs(self):
        from app.connections.runtime import current_account_context
        context = current_account_context.get()
        account = context.account_id if context is not None else None
        with self._database.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            rows = connection.execute(
                "WITH due AS (SELECT e.id, ROW_NUMBER() OVER "
                "(PARTITION BY sm.account_id, sm.chat_id ORDER BY e.id) AS source_position "
                "FROM ingest_entries e JOIN source_messages sm ON sm.id = e.input_source_message_id "
                "JOIN telegram_accounts ta ON ta.id = sm.account_id WHERE "
                "((e.processing_state = 'PENDING' AND (e.retry_at IS NULL OR e.retry_at <= unixepoch())) "
                "OR (e.processing_state = 'PROCESSING' AND e.lease_until <= unixepoch())) "
                "AND (((? IS NULL AND ta.external_account_id IS NULL) OR ta.external_account_id = ?) "
                "OR EXISTS(SELECT 1 FROM source_messages other "
                "JOIN telegram_accounts observer ON observer.id = other.account_id "
                "WHERE other.ingest_entry_id = e.id AND other.ingest_generation = e.generation "
                "AND other.message_state = 'ACTIVE' AND "
                "((? IS NULL AND observer.external_account_id IS NULL) OR observer.external_account_id = ?))) "
                "), ordered AS (SELECT id, ROW_NUMBER() OVER (ORDER BY source_position, id) AS scan_position FROM due) "
                "SELECT id FROM ordered ORDER BY (scan_position <= COALESCE( "
                "(SELECT scan_position FROM ordered WHERE id = ?), 0)), scan_position LIMIT 100",
                (account, account, account, account, self._processor_runtime.recovery_cursors.get(account)),
            ).fetchall()
            if rows:
                self._processor_runtime.recovery_cursors[account] = rows[-1][0]
            from app.candidates.persistence import reselect_pending_input
            messages = []
            for (entry_id,) in rows:
                if entry_id in self._processor_runtime.active_ids():
                    continue
                reselect_pending_input(connection, entry_id)
                source_id = connection.execute("SELECT input_source_message_id FROM ingest_entries WHERE id = ?", (entry_id,)).fetchone()[0]
                message = load_message(connection, source_id)
                if message.account_id == account:
                    messages.append(message)
            return messages

    def _prepare_message(self, message, qualified):
        with self._database.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            if message.is_edit:
                from app.candidates.changes import defer_observation_change
                if defer_observation_change(connection, self._database, message):
                    return {'state': 'STOPPING'}, None, None
            entry, claim = prepare_observation(connection, message, owner=uuid4().hex, qualified=qualified,
                                               active_entry_ids=self._processor_runtime.active_ids())
            fixed_input = load_message(connection, claim.source_message_id) if claim is not None else None
            return entry, claim, fixed_input

    def _resolve_message(self, message):
        with self._database.connection() as connection:
            return resolve_message_identity(connection, message)

    def _publish_message(self, claim, message, update_id):
        with self._database.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            from app.candidates.persistence import source_still_eligible, reselect_pending_input
            if not source_still_eligible(connection, claim.source_message_id):
                # Subscription changes during AI invalidate that input, not all
                # other observing accounts. Keep recovery durable without paying
                # for another attempt on the now-disabled source.
                fail_claim(connection, claim, error_code="INGEST_SOURCE_CHANGED", retry_seconds=0)
                reselect_pending_input(connection, claim.entry_id)
                return False
            _, created = publish_candidate(connection, self._database, claim, message)
            connection.execute(
                "UPDATE telegram_bot_updates SET processed_at = CURRENT_TIMESTAMP, processing_result = ?, "
                "processing_reason = ? WHERE update_id = ? AND account_scope = ?",
                (message.filter_result, message.filter_reason, update_id, self._database._telegram_update_scope()[0]),
            )
            return created

    async def _heartbeat(self, claim, finished):
        while not finished.is_set():
            try:
                await asyncio.wait_for(finished.wait(), timeout=30)
            except TimeoutError:
                await run_sync(self._renew, claim)

    def _renew(self, claim):
        with self._database.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            renew_claim(connection, claim)

    def _processing_failed(self, claim):
        # Cleanup revokes only this processor. It must still work after its
        # credential lease was stopped; candidate writes remain guarded.
        from app.connections.execution import current_account_executions
        token = current_account_executions.set(())
        try:
            with self._database.connection() as connection:
                connection.execute("BEGIN IMMEDIATE")
                try:
                    fail_claim(connection, claim, error_code="INGEST_PROCESSING_FAILED")
                except IngestError as exc:
                    if exc.code != "INGEST_STALE_PROCESSOR":
                        raise
        finally:
            current_account_executions.reset(token)

    # ----------------------------------------------------------------- batch

    async def process_user_inbox(self) -> IngestSummary:
        """Consume one bounded delivery pass through the shared identity owner."""
        from app.candidates.inbox import TelegramUserInbox
        inbox = TelegramUserInbox(self._database)
        created = await self.process_pending_entries()
        processed = ignored = 0
        rules = await self.parse_rules()
        for inbox_id, message in await run_sync(inbox.pending, limit=100):
            was_created, decision = await self.process_message(message, rules=rules)
            await run_sync(inbox.acknowledge, inbox_id)
            processed += 1
            created += int(was_created)
            ignored += int(decision.result == "IGNORE")
        return IngestSummary(processed, created, ignored, 0)

    async def process_pending_updates(self) -> IngestSummary:
        processed = 0
        created = await self.process_pending_entries()
        ignored = 0
        failed = 0
        # Read once per batch rather than per message: the scheme is one settings
        # row and a poll may carry a hundred updates.
        rules = await self.parse_rules()
        updates = await self._database.pending_telegram_updates(limit=100)
        for update_id, update in updates:
            try:
                message = self._parse_message(update)
            except (KeyError, OverflowError, TypeError, ValueError):
                await self._database.mark_telegram_update_result(
                    update_id, "ERROR", "Telegram Update 字段无效"
                )
                processed += 1
                failed += 1
                continue
            if message is None:
                edited_identity = self._edited_message_identity(update)
                if edited_identity is not None:
                    from app.connections.runtime import current_account_context
                    context = current_account_context.get()
                    await self._stop_previous_processor(*edited_identity, context.account_id if context is not None else None)
                    await self._database.deactivate_candidate_message(
                        *edited_identity
                    )
                await self._database.mark_telegram_update_result(
                    update_id,
                    "IGNORE",
                    (
                        "编辑后不再包含候选内容"
                        if edited_identity is not None
                        else "未包含图片预览、ExHentai 链接、预览页链接或压缩包附件"
                    ),
                )
                processed += 1
                ignored += 1
                continue
            was_created, decision = await self.process_message(message, rules=rules, update_id=update_id)
            processed += 1
            created += int(was_created)
            ignored += int(decision.result == "IGNORE")
        return IngestSummary(processed, created, ignored, failed)

    @staticmethod
    def _edited_message_identity(update: dict) -> tuple[int, int] | None:
        message = update.get("edited_channel_post") or update.get("edited_message")
        if not isinstance(message, dict) or not isinstance(message.get("chat"), dict):
            return None
        try:
            return int(message["chat"]["id"]), int(message["message_id"])
        except (KeyError, TypeError, ValueError):
            return None

    @staticmethod
    def _parse_message(update: dict) -> ParsedSourceMessage | None:
        message = (
            update.get("channel_post")
            or update.get("message")
            or update.get("edited_channel_post")
            or update.get("edited_message")
        )
        is_edit = "edited_channel_post" in update or "edited_message" in update
        if not isinstance(message, dict):
            return None
        text = str(message.get("caption") or message.get("text") or "").strip()
        urls = links.message_urls(message, text)
        page_urls = links.preview_urls(urls)
        gallery_ref = links.find_gallery_ref(urls, text)
        # Only when the message links no gallery of its own: a Telegraph slug
        # like `ehentai-<gid>-<token>` names the same work, so a preview-only
        # post stops being an anonymous candidate (R60).
        gallery_ref_from_preview = False
        if gallery_ref is None:
            gallery_ref = links.find_preview_gallery_ref(page_urls)
            gallery_ref_from_preview = gallery_ref is not None
        photo = None
        if message.get("photo"):
            photo = max(
                message["photo"],
                key=lambda item: int(item.get("width", 0))
                * int(item.get("height", 0)),
            )
        document = message.get("document")
        archive = None
        if isinstance(document, dict):
            file_name = str(document.get("file_name") or "")
            if Path(file_name).suffix.lower() in {".zip", ".rar", ".7z", ".cbz"}:
                archive = document
        if photo is None and gallery_ref is None and archive is None and not page_urls:
            return None
        ex_gid = gallery_ref[0] if gallery_ref is not None else None
        chat = message.get("chat") or {}
        peer_type = chat.get("type")
        if peer_type not in {"channel", "supergroup", "group", "private"}:
            # Old fixtures/events may omit type; only a channel-post envelope
            # with a real marked channel id proves a shared message box.
            peer_type = (
                "channel" if ("channel_post" in update or "edited_channel_post" in update)
                and int(chat["id"]) < -1000000000000 else None
            )
        origin = message.get("forward_origin")
        if not isinstance(origin, dict) or origin.get("type") != "channel":
            origin = {}
        origin_chat = origin.get("chat")
        origin_channel_id = (
            int(origin_chat["id"]) if isinstance(origin_chat, dict) and origin_chat.get("type") == "channel"
            and origin.get("message_id") is not None else None
        )
        origin_message_id = int(origin["message_id"]) if origin_channel_id is not None else None
        attachments = ()
        file_unique_id = None
        if photo is not None:
            file_unique_id = str(photo["file_unique_id"])
            attachments = (
                {
                    "type": "photo",
                    "file_id": str(photo["file_id"]),
                    "file_unique_id": file_unique_id,
                    "width": int(photo.get("width", 0)),
                    "height": int(photo.get("height", 0)),
                    "size_bytes": int(photo.get("file_size", 0)),
                },
            )
        elif archive is not None:
            file_unique_id = str(archive["file_unique_id"])
            attachments = (
                {
                    "type": "archive",
                    "file_id": str(archive["file_id"]),
                    "file_unique_id": file_unique_id,
                    "file_name": str(archive["file_name"]),
                    "mime_type": str(archive.get("mime_type") or ""),
                    "size_bytes": int(archive.get("file_size", 0)),
                    # Where the attachment lives, not just what it is. A bot
                    # `file_id` is useless to the MTProto route -- file
                    # references are per-account -- so the user-account download
                    # re-reads the message by these two numbers. They are stored
                    # on the attachment rather than looked up later because a
                    # download job carries the attachment and nothing else.
                    "chat_id": int(chat["id"]),
                    "message_id": int(message["message_id"]),
                },
            )
        return ParsedSourceMessage(
            is_edit=is_edit,
            chat_id=int(chat["id"]),
            chat_title=str(chat.get("title") or chat.get("username") or chat["id"]),
            message_id=int(message["message_id"]),
            sender_id=(
                int(message["from"]["id"])
                if isinstance(message.get("from"), dict)
                else None
            ),
            reply_to_message_id=(
                int(message["reply_to_message"]["message_id"])
                if isinstance(message.get("reply_to_message"), dict)
                else None
            ),
            media_group_id=(
                str(message["media_group_id"])
                if message.get("media_group_id") is not None
                else None
            ),
            message_text=text,
            attachments=attachments,
            file_unique_id=file_unique_id,
            message_date=datetime.fromtimestamp(
                int(message["date"]), tz=UTC
            ).isoformat(),
            filter_result="ACCEPT",
            filter_reason=(
                "包含图片预览"
                if photo is not None
                else "包含压缩包附件"
                if archive is not None
                else "由预览页链接识别 ExHentai 画廊"
                if gallery_ref_from_preview
                else "包含 ExHentai 画廊链接"
                if gallery_ref is not None
                else "包含预览页链接"
            ),
            ex_gid=ex_gid,
            ex_gallery_token=(
                gallery_ref[1] if gallery_ref is not None else None
            ),
            preview_urls=page_urls,
            gallery_from_preview=gallery_ref_from_preview,
            is_forwarded=bool(message.get("forward_origin") or message.get("forward_date") or message.get("forward_from_chat")),
            peer_type=peer_type,
            origin_channel_id=origin_channel_id,
            origin_message_id=origin_message_id,
        )
