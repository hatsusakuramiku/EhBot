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

def evaluate_metadata_rules(
    source: "TelegramSourceConfig",
    metadata: "dict[str, str]",
) -> "RuleDecision":
    tags_raw = ",".join(
        (metadata.get("TagsRaw", ""), metadata.get("Tags", ""))
    )
    candidate_tags = {
        tag.strip().lower()
        for tag in tags_raw.replace("\n", ",").split(",")
        if tag.strip()
    }
    for required in source.required_tags:
        if required and required not in candidate_tags:
            return RuleDecision(
                "IGNORE",
                f"缺少必需 Tag {required}",
            )
    for forbidden in source.forbidden_tags:
        if forbidden and forbidden in candidate_tags:
            return RuleDecision(
                "IGNORE",
                f"包含禁用 Tag {forbidden}",
            )
    language = metadata.get("Language", "").strip().lower()
    if source.allowed_languages and language:
        if language not in source.allowed_languages:
            return RuleDecision(
                "IGNORE",
                f"语言 {language} 不在允许列表",
            )
    elif source.allowed_languages and not language:
        return RuleDecision(
            "NEEDS_INFO",
            "语言未知，无法应用语言允许规则",
        )
    category = metadata.get("Category", "").strip().lower()
    if source.allowed_categories and category:
        if category not in source.allowed_categories:
            return RuleDecision(
                "IGNORE",
                f"类别 {category} 不在允许列表",
            )
    elif source.allowed_categories and not category:
        return RuleDecision(
            "NEEDS_INFO",
            "类别未知，无法应用类别允许定义",
        )
    rating_raw = metadata.get("Rating", "").strip()
    if source.min_rating is not None and rating_raw:
        try:
            rating_value = float(rating_raw)
        except ValueError:
            return RuleDecision(
                "NEEDS_INFO",
                "评分格式无效",
            )
        if rating_value < source.min_rating:
            return RuleDecision(
                "IGNORE",
                f"评分 {rating_value} 低于阈值 {source.min_rating}",
            )
    elif source.min_rating is not None and not rating_raw:
        return RuleDecision(
            "NEEDS_INFO",
            "评分未知，无法应用最低评分限制",
        )
    return RuleDecision(
        "ACCEPT",
        "元数据规则检查完成",
    )

