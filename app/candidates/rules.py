from __future__ import annotations

from pathlib import Path

from app.candidates.models import (
    ParsedSourceMessage,
    RuleDecision,
    TelegramSourceConfig,
)


def evaluate_source_rules(
    source: TelegramSourceConfig, message: ParsedSourceMessage
) -> RuleDecision:
    if not source.enabled:
        return RuleDecision("IGNORE", "来源未加入白名单")
    if source.source_type == "PRIVATE_CHAT" and message.sender_id != source.chat_id:
        return RuleDecision("IGNORE", "私聊发送者与白名单不匹配")

    max_size_bytes = source.max_attachment_size_mb * 1024 * 1024
    size_unknown = False
    for attachment in message.attachments:
        if attachment.get("type") == "archive":
            extension = Path(str(attachment.get("file_name") or "")).suffix
            archive_format = extension.lower().lstrip(".")
            if archive_format not in source.allowed_archive_formats:
                return RuleDecision("IGNORE", f"压缩格式 {archive_format.upper()} 未允许")
        size_bytes = int(attachment.get("size_bytes") or 0)
        if max_size_bytes and size_bytes > max_size_bytes:
            return RuleDecision("IGNORE", "附件超过来源配置的大小上限")
        if max_size_bytes and size_bytes <= 0:
            size_unknown = True

    if size_unknown:
        return RuleDecision("NEEDS_INFO", "附件大小未知，无法应用大小上限")
    if message.title is None:
        return RuleDecision("NEEDS_INFO", "缺少可识别标题")
    return RuleDecision("ACCEPT", message.filter_reason)
