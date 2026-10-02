from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

from app.candidates import links
from app.candidates.models import (
    IngestSummary,
    ParsedSourceMessage,
    RuleDecision,
)
from app.candidates.parse_rules import (
    PARSE_RULES_KEY,
    message_qualifies,
    parse_rules_view,
    title_forces_needs_info,
)
from app.candidates.rules import evaluate_source_rules
from app.db.database import Database


class CandidateIngestor:
    """Turn incoming messages into candidates, or decide not to.

    One pipeline for both ingest paths -- the Bot API updates and the MTProto
    user-account read. `admit_message` is the shared half: the AI gate (when the
    operator turned it on), the parse rules, the source's own filters and the
    title requirement, in that order. A second copy of any of these is how the
    two paths would start disagreeing about what a candidate is.
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
        gate = await self._gate(message, rules)
        if gate.result == "IGNORE":
            return gate
        source = await self._database.discover_telegram_source(message)
        decision = evaluate_source_rules(source, message)
        if decision.result != "ACCEPT":
            return decision
        if title_forces_needs_info(rules, message):
            return RuleDecision("NEEDS_INFO", "解析规则要求必须提供标题")
        return RuleDecision("ACCEPT", gate.reason or decision.reason)

    async def _gate(
        self, message: ParsedSourceMessage, rules: dict
    ) -> RuleDecision:
        """The two pre-source gates: AI admission, then the parse scheme.

        The AI gate runs first (an operator decision recorded in the proposal):
        it is the expensive, semantic judgement, and when the operator lets it
        override the parse scheme a message can be admitted on the model's word
        alone. With the override off, the two are an AND -- the model's `accept`
        still has to survive the structural rules.
        """
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

    # ----------------------------------------------------------------- batch

    async def process_pending_updates(self) -> IngestSummary:
        processed = 0
        created = 0
        ignored = 0
        failed = 0
        # Read once per batch rather than per message: the scheme is one settings
        # row and a poll may carry a hundred updates.
        rules = await self.parse_rules()
        while updates := await self._database.pending_telegram_updates(limit=100):
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
                decision = await self.admit_message(message, rules=rules)
                if decision.result == "IGNORE":
                    if message.is_edit:
                        await self._database.deactivate_candidate_message(
                            message.chat_id, message.message_id
                        )
                    await self._database.mark_telegram_update_result(
                        update_id, decision.result, decision.reason
                    )
                    processed += 1
                    ignored += 1
                    continue
                message = replace(
                    message,
                    filter_result=decision.result,
                    filter_reason=decision.reason,
                )
                was_created = await self._database.save_candidate_message(
                    update_id, message
                )
                processed += 1
                created += int(was_created)
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
        gallery_ref = links.find_gallery_ref(urls, text)
        page_urls = links.preview_urls(urls)
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
        explicit_title = next(
            (
                line.strip()
                for line in text.splitlines()
                if line.strip() and not line.strip().lower().startswith("http")
            ),
            None,
        )
        ex_gid = gallery_ref[0] if gallery_ref is not None else None
        title = explicit_title
        title_source = "TELEGRAM" if explicit_title is not None else None
        title_confidence = 0.9 if explicit_title is not None else None
        if title is None and ex_gid is not None:
            title = f"ExHentai #{ex_gid}"
            title_source = "INFERRED"
            title_confidence = 0.2
        if title is None and archive is not None:
            title = Path(str(archive["file_name"])).stem
            title_source = "FILENAME"
            title_confidence = 0.5
        chat = message.get("chat") or {}
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
            title=title,
            title_source=title_source,
            title_confidence=title_confidence,
            filter_result="ACCEPT",
            filter_reason=(
                "包含图片预览"
                if photo is not None
                else "包含压缩包附件"
                if archive is not None
                else "包含 ExHentai 画廊链接"
                if gallery_ref is not None
                else "包含预览页链接"
            ),
            ex_gid=ex_gid,
            ex_gallery_token=(
                gallery_ref[1] if gallery_ref is not None else None
            ),
            preview_urls=page_urls,
        )
