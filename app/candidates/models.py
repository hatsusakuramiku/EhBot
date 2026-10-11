from __future__ import annotations

from dataclasses import dataclass


def display_title(
    title: str | None, ex_gid: int | None, candidate_id: int
) -> str:
    """What to show where a work is named, when it has no title yet (R62).

    Since R62 a work's name comes from its gallery, so a candidate in 待补充 has
    none -- and the list used to fall back to the candidate id, which is what
    made every unread row look alike. A gallery id is at least the work's own
    identity: the same number the gallery is known by upstream. When there is
    not even that, the candidate id is the only handle left.

    This is a display fallback only. `title` stays None so the API, the
    metadata drawer and the search still see 「no title yet」.
    """
    if title:
        return title
    if ex_gid is not None:
        return f"#{ex_gid}"
    return f"候选 #{candidate_id}"


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
    #: No title is read out of the message any more (R62): the gallery's own
    #: metadata is the only automatic source of a work's name, and a candidate
    #: that has none waits in 待补充 for the operator or the next scrape.
    filter_result: str
    filter_reason: str
    ex_gid: int | None = None
    ex_gallery_token: str | None = None
    preview_urls: tuple[str, ...] = ()
    #: True when `ex_gid` was read from a preview page's slug rather than an
    #: explicit EH link (R60). Kept so the filter reason can say which, without
    #: the qualification rules having to re-parse the message.
    gallery_from_preview: bool = False


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
    #: Upstream cover URL from gdata. Kept on the candidate rather than in
    #: `metadata_values` because it is routing state for the thumbnail proxy,
    #: not a metadata field an operator reviews or edits.
    thumb_url: str | None = None
    #: Why the candidate was flagged, shown on the row when it explains
    #: something (R60). Read for every row, rendered for few.
    filter_reason: str | None = None

    @property
    def display_title(self) -> str:
        return display_title(self.title, self.ex_gid, self.candidate_id)


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
    magnet_url: str | None = None

    @property
    def display_title(self) -> str:
        return display_title(self.title, self.ex_gid, self.candidate_id)


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
