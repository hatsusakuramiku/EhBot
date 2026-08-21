from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ParsedSourceMessage:
    is_edit: bool
    chat_id: int
    chat_title: str
    message_id: int
    sender_id: int | None
    reply_to_message_id: int | None
    media_group_id: str | None
    message_text: str
    attachments: tuple[dict, ...]
    file_unique_id: str | None
    message_date: str
    title: str | None
    title_source: str | None
    title_confidence: float | None
    filter_result: str
    filter_reason: str
    ex_gid: int | None = None
    ex_gallery_token: str | None = None
    preview_urls: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class CandidateListItem:
    candidate_id: int
    status: str
    filter_result: str
    title: str | None
    message_count: int
    updated_at: str
    ex_gid: int | None
    ex_gallery_token: str | None
    artist: str | None = None
    tags: str | None = None
    raw_tags: str | None = None
    category: str | None = None
    language: str | None = None


@dataclass(frozen=True, slots=True)
class CandidateMessage:
    chat_title: str
    message_id: int
    message_text: str
    attachments: tuple[dict, ...]
    message_date: str


@dataclass(frozen=True, slots=True)
class CandidateDetail:
    candidate_id: int
    status: str
    filter_result: str
    filter_reason: str
    title: str | None
    ex_gid: int | None
    ex_gallery_token: str | None
    messages: tuple[CandidateMessage, ...]
    preview_url: str | None = None
    # NULL until gdata has answered, so 「未查询」 stays distinguishable from
    # 「确认无种」; the router treats only an explicit 0 as「无种」.
    torrent_count: int | None = None
    torrent_hash: str | None = None


@dataclass(frozen=True, slots=True)
class IngestSummary:
    processed_updates: int = 0
    created_candidates: int = 0
    ignored_updates: int = 0
    failed_updates: int = 0


@dataclass(frozen=True, slots=True)
class TelegramSourceConfig:
    source_id: int
    source_type: str
    chat_id: int
    display_name: str
    enabled: bool
    allowed_archive_formats: tuple[str, ...]
    max_attachment_size_mb: int
    required_tags: tuple[str, ...] = ()
    forbidden_tags: tuple[str, ...] = ()
    allowed_languages: tuple[str, ...] = ()
    allowed_categories: tuple[str, ...] = ()
    min_rating: float | None = None


@dataclass(frozen=True, slots=True)
class RuleDecision:
    result: str
    reason: str
