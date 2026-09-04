from __future__ import annotations

from dataclasses import dataclass


REVIEW_APPROVE = "APPROVE"
REVIEW_REJECT = "REJECT"
REVIEW_NEEDS_REVISION = "NEEDS_REVISION"
REVIEW_REQUEUE = "REQUEUE"
REVIEW_EDIT_METADATA = "EDIT_METADATA"
REVIEW_LOCK_METADATA = "LOCK_METADATA"

REVIEW_ACTIONS: tuple[str, ...] = (
    REVIEW_APPROVE,
    REVIEW_REJECT,
    REVIEW_NEEDS_REVISION,
    REVIEW_REQUEUE,
    REVIEW_EDIT_METADATA,
    REVIEW_LOCK_METADATA,
)

#: Audit-trail entries nobody typed. `AUTO_APPROVE` is written by the automatic
#: approval rules, `METADATA_RULE` by a source's own filters re-evaluating a
#: candidate. They are not in `REVIEW_ACTIONS` because no operator can perform
#: them, but the timeline has to read them, so they are named here rather than
#: as bare strings at their two write sites.
REVIEW_AUTO_APPROVE = "AUTO_APPROVE"
REVIEW_METADATA_RULE = "METADATA_RULE"

#: The two reserved `operator_name` values. Everything else in that column is a
#: real login. They live here, next to the actions they sign, so the timeline can
#: tell an operator's decision from a rule's without a second copy of either
#: string: `AUTO_OPERATOR` is what `ReviewOrchestrator` records for a rule-driven
#: approval, `SYSTEM_OPERATOR` what the metadata-rule re-evaluation records.
AUTO_OPERATOR = "自动审批"
SYSTEM_OPERATOR = "system"

STATUS_PENDING_REVIEW = "PENDING_REVIEW"
STATUS_NEEDS_INFO = "NEEDS_INFO"
STATUS_APPROVED = "APPROVED"
STATUS_REJECTED = "REJECTED"
STATUS_NEEDS_REVISION = "NEEDS_REVISION"
STATUS_PROCESSING = "PROCESSING"
STATUS_FAILED = "FAILED"

REVIEWABLE_STATUSES: frozenset[str] = frozenset(
    {
        STATUS_PENDING_REVIEW,
        STATUS_NEEDS_INFO,
        STATUS_NEEDS_REVISION,
        STATUS_REJECTED,
    }
)

#: Statuses 重新排队 may act on. A superset of the reviewable ones by exactly
#: `FAILED`, and that difference is the whole reason this set exists: a failed
#: download is not something to approve or reject -- the decision was already
#: taken -- but it must be possible to put it back in front of the operator.
#: Before this existed a `FAILED` candidate was in no reviewable state *and* had
#: no requeue, so a work whose retry was refused (a permanent error, or a source
#: that no longer has the file) was a dead end with no button on the page that
#: could move it anywhere.
REQUEUEABLE_STATUSES: frozenset[str] = frozenset(
    {
        STATUS_REJECTED,
        STATUS_NEEDS_REVISION,
        STATUS_FAILED,
    }
)


def statuses_allowing(action: str) -> frozenset[str]:
    """Which current statuses `action` may be applied to.

    The guard is per-action rather than one set for all of them because
    `REQUEUE` and the three review verbs answer different questions: a `FAILED`
    candidate can be sent back to the queue but must not be approvable, since
    approving it would skip the look the requeue exists to force. Every caller
    -- the page's action list, the JSON layer and the database guard -- reads
    this one function, so a state can never be offered by the page and refused
    by the write.
    """
    return REQUEUEABLE_STATUSES if action == REVIEW_REQUEUE else REVIEWABLE_STATUSES

METADATA_FIELDS: tuple[str, ...] = (
    "Title",
    "JapaneseTitle",
    "Artist",
    "Group",
    "Parody",
    "Character",
    "Language",
    "Category",
    "Tags",
    "Rating",
    "Pages",
    "Uploader",
    "Description",
)

# Untranslated upstream values, stored alongside the Chinese fields so
# operators can still search by the original E-Hentai tag.
RAW_METADATA_FIELDS: tuple[str, ...] = (
    "TagsRaw",
    "ArtistRaw",
    "GroupRaw",
    "ParodyRaw",
    "CharacterRaw",
    "LanguageRaw",
    "CategoryRaw",
)

# Display labels for the review UI, which is otherwise entirely Chinese.
FIELD_LABELS: dict[str, str] = {
    "Title": "标题",
    "JapaneseTitle": "日文标题",
    "Artist": "作者",
    "Group": "社团",
    "Parody": "原作",
    "Character": "角色",
    "Language": "语言",
    "Category": "分类",
    "Tags": "中文标签",
    "Rating": "评分",
    "Pages": "页数",
    "Uploader": "上传者",
    "Description": "简介",
    "FileSize": "文件大小",
    "Web": "来源网址",
    "ScanInformation": "图源等级",
    "TagsRaw": "原始标签",
    "ArtistRaw": "原始作者",
    "GroupRaw": "原始社团",
    "ParodyRaw": "原始原作",
    "CharacterRaw": "原始角色",
    "LanguageRaw": "原始语言",
    "CategoryRaw": "原始分类",
    # Not a metadata field: the automatic-approval DSL's collection pseudo-field,
    # which matches against the tag set rather than one stored value. It is
    # labelled here because the rule editor offers it in the same dropdown as the
    # real fields, and a lone `TAG` among Chinese labels reads as a bug.
    "TAG": "标签集合",
}


def field_label(field_name: str) -> str:
    """Return the Chinese label for a metadata field, or the raw name."""
    return FIELD_LABELS.get(field_name, field_name)


def split_metadata_entries(entries):
    """Split metadata into translated fields and untranslated originals.

    The review UI shows the Chinese values first and keeps the upstream
    English values in a secondary list, so a gallery with both does not
    render two interleaved copies of every field.
    """
    tag_entries = tuple(
        entry
        for field_name in ("TagsRaw", "Tags")
        for entry in entries
        if entry.field_name == field_name
    )
    primary = tuple(
        entry
        for entry in entries
        if entry.field_name not in RAW_METADATA_FIELDS
        and entry.field_name != "Tags"
    ) + tag_entries
    raw = tuple(
        entry
        for entry in entries
        if entry.field_name in RAW_METADATA_FIELDS
        and entry.field_name != "TagsRaw"
    )
    return primary, raw


@dataclass(frozen=True, slots=True)
class MetadataEntry:
    field_name: str
    field_value: str
    value_source: str
    confidence: float | None
    is_manual: bool
    created_at: str
    #: Pinned by the operator against re-scraping. Distinct from `is_manual`:
    #: a locked value can still be one ExHentai supplied, which is exactly the
    #: case `is_manual` cannot express.
    is_locked: bool = False


@dataclass(frozen=True, slots=True)
class ReviewActionEntry:
    action: str
    operator_name: str
    details: dict
    created_at: str


@dataclass(frozen=True, slots=True)
class CandidateReviewSummary:
    candidate_id: int
    title: str | None
    status: str
    metadata: tuple[MetadataEntry, ...]
    review_history: tuple[ReviewActionEntry, ...]
