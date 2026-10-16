from __future__ import annotations

import asyncio
import json
import sqlite3
import secrets
import time
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager, nullcontext
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from app.ai.models import (
    CHAIN_SCOPE_DEFAULT,
    AiModelChainEntry,
    AiPathSuggestion,
    AiProvider,
    AiProviderKey,
    AiProviderModel,
    AiRequestParams,
    parse_request_params,
)
from app.archive.models import ArchivePasswordEntry, ArchivePathRule, ToolProfile
from app.auto_approval.models import AutoApprovalRule, RULE_ACTION_APPROVE
from app.candidates.models import (
    CandidateDetail,
    CandidateListItem,
    CandidateMessage,
    ParsedSourceMessage,
    TelegramSourceConfig,
)
from app.candidates.rules import MISSING_TITLE_REASON, evaluate_metadata_rules
from app.connections.execution import current_account_executions
from app.search.query import (
    PAGE_CANDIDATES,
    PAGE_DOWNLOADED,
    candidate_search_sql,
    downloaded_search_sql,
    parse_query,
)
from app.credentials import ApiCredential
from app.admin_auth import AdminAuth, AuthError
from app.downloads.models import (
    CONVERSION_STATE_FAILED,
    CONVERSION_STATE_WAITING_PASSWORD,
    CONVERSION_STATE_WAITING_PATH,
    CONVERSION_STATE_WAITING_VOLUMES,
    DOWNLOAD_STATE_COMPLETED,
    PROVIDER_CONVERSION,
    DownloadedWork,
    ReArchiveCandidate,
)
from app.downloads.execution import current_execution, run_sync

from app.review.models import (
    MetadataEntry,
    ReviewActionEntry,
    statuses_allowing,
)


def _api_credential_from_row(row) -> ApiCredential:
    """One `api_credentials` row, in the column order every reader selects.

    Written once because four readers (lookup, active key, family revoke and the
    tests) must agree on it; a second hand-written index map is how a column
    gets swapped without anything failing loudly.
    """
    return ApiCredential(
        id=int(row[0]),
        kind=str(row[1]),
        label=str(row[2]),
        public_id=str(row[3]),
        secret_hash=str(row[4]),
        family_id=str(row[5] or ""),
        created_at=row[6],
        last_used_at=row[7],
        expires_at=int(row[8]) if row[8] is not None else None,
        revoked_at=row[9],
        rotated_from=int(row[10]) if row[10] is not None else None,
        auth_epoch=int(row[11]),
    )


#: Status -> counter key. This is the single place a candidate state is mapped
#: to the name the interface uses for its tab badges, so a new state shows up
#: in the dashboard by editing one dict instead of three call sites.
CANDIDATE_COUNT_KEYS: dict[str, str] = {
    "DISCOVERED": "discovered",
    "PENDING_REVIEW": "pending_review",
    "NEEDS_INFO": "needs_info",
    "APPROVED": "approved",
    "PROCESSING": "processing",
    "DOWNLOADED": "downloaded",
    "REJECTED": "rejected",
    "FAILED": "failed",
}

#: The two directions a `dir` query parameter may name.
SORT_DIRECTIONS: frozenset[str] = frozenset({"asc", "desc"})

class DuplicateCandidateError(ValueError):
    """A manual add named a work this deployment already holds.

    Raised in place of letting the gallery unique index surface as a 500: a
    duplicate is the *expected* outcome of pasting the same link twice, so it
    travels as a value the route can render rather than as an integrity
    failure. The existing row rides along so the page can offer to open it.
    """

    #: The noun for what was duplicated, used verbatim in the message.
    label = "候选"

    def __init__(self, existing_candidate_id: int, existing_work_id: str | None = None) -> None:
        self.existing_candidate_id = int(existing_candidate_id)
        self.existing_work_id = existing_work_id
        super().__init__(f"该{self.label}已有候选 #{self.existing_candidate_id}")


class DuplicateGalleryCandidate(DuplicateCandidateError):
    label = "画廊"


class DuplicateTorrentCandidate(DuplicateCandidateError):
    label = "磁力链接"


@dataclass(frozen=True, slots=True)
class SortSpec:
    """One sortable order, both ways round.

    Two whole ORDER BY bodies rather than an expression plus a direction: a
    stable tiebreak carries its own direction and must not flip with the primary
    column (`title_value IS NULL` always sorts missing titles last, whichever way
    the title itself runs). `default` is what a request that names no direction
    gets -- 「最新发现」 already means descending, and the URL does not have to
    spell that out.
    """

    asc: str
    desc: str
    default: str = "desc"


def order_by(
    sorts: Mapping[str, SortSpec],
    key: str,
    direction: str | None,
    fallback: str = "newest",
) -> str:
    """The ORDER BY body for one request. Whitelisted: an unknown key falls back
    rather than reaching the statement text."""
    spec = sorts.get(key) or sorts[fallback]
    chosen = direction if direction in SORT_DIRECTIONS else spec.default
    return spec.desc if chosen == "desc" else spec.asc


def sort_direction(
    sorts: Mapping[str, SortSpec],
    key: str,
    direction: str | None,
    fallback: str = "newest",
) -> str:
    """The direction a request actually gets, for links and hidden fields."""
    spec = sorts.get(key) or sorts[fallback]
    return direction if direction in SORT_DIRECTIONS else spec.default


#: Allowed sort keys mapped to their ORDER BY bodies. Values are interpolated into
#: SQL, so this table is the boundary that keeps a query-string value out of the
#: statement text -- nothing outside it may reach the ORDER BY clause.
CANDIDATE_SORT_ORDERS: dict[str, SortSpec] = {
    "newest": SortSpec("c.id ASC", "c.id DESC"),
    "oldest": SortSpec("c.id ASC", "c.id DESC", default="asc"),
    "updated": SortSpec(
        "c.updated_at ASC, c.id ASC", "c.updated_at DESC, c.id DESC"
    ),
    "title": SortSpec(
        "title_value IS NULL, title_value COLLATE NOCASE ASC, c.id ASC",
        "title_value IS NULL, title_value COLLATE NOCASE DESC, c.id DESC",
        default="asc",
    ),
}

#: The operator's 「标题来源」 preference lives in `archive_settings` under this
#: key, written by `ArchiveSettingsService.save_title_source`. The literal is
#: spelled here rather than imported from `app.archive.service`, which imports
#: `app.conversion`, which imports this module -- a real cycle. A test pins the
#: two together so the copy cannot rot.
TITLE_SOURCE_SETTING_KEY = "library_title_source"

#: `system_settings` row that records the one-time R62 title repair.
TITLE_MAINTENANCE_KEY = "title_maintenance_v1"

#: `1` when the preference is Japanese (the default, and what an unset row
#: means), `0` when it is English. Read inside the query rather than passed in,
#: so every list -- candidates, downloaded, dedup, the detail page -- follows
#: the setting without ten call sites having to remember to carry it.
_TITLE_LANGUAGE_SQL = (
    "CASE (SELECT value FROM archive_settings "
    f"WHERE key = '{TITLE_SOURCE_SETTING_KEY}') "
    "WHEN 'english' THEN 0 ELSE 1 END"
)

#: Trust order of `metadata_values.value_source`, as a SQL fragment (R62).
#:
#: The gallery page is the work's own record and outranks everything but the
#: operator's own edit -- and `is_manual`, which is ordered separately and ahead
#: of this wherever it is used. The codes are the same literals
#: `app/api/status.py` labels and the writers insert; the rank has to live where
#: the comparison happens, which is why this fragment is shared with
#: `app/conversion` instead of being written out twice. `AI` (R83) sits just
#: below the operator's own manual-add, above a torrent's contents: a model's
#: guess is better than a filename but worse than anything the gallery said.
METADATA_SOURCE_RANK_SQL = (
    "CASE mv.value_source WHEN 'EXHENTAI' THEN 7 WHEN 'OPERATOR' THEN 6 "
    "WHEN 'MANUAL_ADD' THEN 5 WHEN 'AI' THEN 4 "
    "WHEN 'EH_TORRENT' THEN 3 "
    "WHEN 'TELEGRAPH' THEN 3 WHEN 'FILENAME' THEN 2 "
    "WHEN 'TELEGRAM' THEN 1 WHEN 'INFERRED' THEN 0 ELSE 0 END"
)

#: How a work's name is chosen out of everything its candidate collected (R62).
#:
#: Three keys, in this order:
#:
#: 1. `is_manual` -- a value the operator typed wins outright, whichever
#:    language they put it in.
#: 2. the language, following 「标题来源」. `JapaneseTitle` is only ever written
#:    by a gallery scrape, so this layer is the operator's 「优先日文名」 rule
#:    written down, not a guess.
#: 3. the source rank above. Since R62 the gallery is the *only* automatic
#:    source of a title -- the caption is no longer parsed for one -- and the
#:    lower ranks are kept for rows written before that change until the
#:    one-time maintenance (see `maintain_titles`) clears them.
#:
#: Built rather than pasted because the same resolution is read by six queries,
#: and six copies is exactly how they came to disagree.
_TITLE_SUBQUERY = (
    "(SELECT mv.field_value FROM metadata_values mv "
    " WHERE mv.candidate_id = {candidate} "
    " AND mv.field_name IN ('Title', 'JapaneseTitle') "
    " ORDER BY mv.is_manual DESC, "
    " CASE mv.field_name WHEN 'JapaneseTitle' THEN {language} "
    " ELSE 1 - {language} END DESC, "
    " " + METADATA_SOURCE_RANK_SQL + " DESC, "
    " mv.confidence DESC, mv.created_at DESC LIMIT 1)"
)


def title_subquery(candidate_ref: str) -> str:
    """The shared title resolution, for one candidate reference.

    `candidate_ref` is the SQL expression naming the candidate -- `c.id`,
    `cm.candidate_id` or a bound `?` -- so a query without an alias for
    `candidates` can still use the same resolution.
    """
    return _TITLE_SUBQUERY.format(
        candidate=candidate_ref, language=_TITLE_LANGUAGE_SQL
    )


#: Projection shared by the paged and legacy candidate queries. Both read the
#: same columns in the same order, which is what lets a single row mapper serve
#: them and stops the two lists from drifting into different shapes.
_CANDIDATE_LIST_SELECT = (
    "SELECT c.id, c.status, c.filter_result, "
    + title_subquery("c.id")
    + " AS title_value, "
    "(SELECT mv.field_value FROM metadata_values mv "
    " WHERE mv.candidate_id = c.id AND mv.field_name = 'Artist' "
    " ORDER BY mv.is_manual DESC, mv.confidence DESC LIMIT 1), "
    "(SELECT mv.field_value FROM metadata_values mv "
    " WHERE mv.candidate_id = c.id AND mv.field_name = 'Tags' "
    " ORDER BY mv.is_manual DESC, mv.confidence DESC LIMIT 1), "
    "(SELECT mv.field_value FROM metadata_values mv "
    " WHERE mv.candidate_id = c.id AND mv.field_name = 'TagsRaw' "
    " ORDER BY mv.is_manual DESC, mv.confidence DESC LIMIT 1), "
    "(SELECT mv.field_value FROM metadata_values mv "
    " WHERE mv.candidate_id = c.id AND mv.field_name = 'Category' "
    " ORDER BY mv.is_manual DESC, mv.confidence DESC LIMIT 1), "
    "(SELECT mv.field_value FROM metadata_values mv "
    " WHERE mv.candidate_id = c.id AND mv.field_name = 'Language' "
    " ORDER BY mv.is_manual DESC, mv.confidence DESC LIMIT 1), "
    "COUNT(cm.source_message_id), c.updated_at, c.ex_gid, "
    "c.ex_gallery_token, c.thumb_url, c.filter_reason, wv.work_id, wv.revision "
    "FROM candidates c JOIN work_versions wv ON wv.candidate_id = c.id "
    "LEFT JOIN candidate_messages cm "
    "ON cm.candidate_id = c.id"
)


#: Which packaging outcome the 已下载内容 page is showing. Also the whitelist:
#: `list_downloaded_works` refuses a name that is not here rather than letting a
#: query-string value decide a WHERE clause.
DOWNLOADED_PACK_FILTERS: tuple[str, ...] = (
    "all",
    "unpacked",
    "packed",
    "attention",
    "failed",
)

#: Packing states that are waiting on the operator rather than failing. They get
#: their own filter because the remedy differs -- supply a password or the
#: missing volume, then requeue the same task.
_DOWNLOADED_ATTENTION_STATES: tuple[str, ...] = (
    CONVERSION_STATE_WAITING_PASSWORD,
    CONVERSION_STATE_WAITING_VOLUMES,
    CONVERSION_STATE_WAITING_PATH,
)

#: Whitelisted sorts, for the reason `CANDIDATE_SORT_ORDERS` is: nothing outside
#: this table may reach the ORDER BY text.
DOWNLOADED_SORT_ORDERS: dict[str, SortSpec] = {
    "newest": SortSpec("job_id ASC", "job_id DESC"),
    "oldest": SortSpec("job_id ASC", "job_id DESC", default="asc"),
    "title": SortSpec(
        "title_value IS NULL, title_value COLLATE NOCASE ASC, job_id ASC",
        "title_value IS NULL, title_value COLLATE NOCASE DESC, job_id DESC",
        default="asc",
    ),
    "largest": SortSpec(
        "archive_size IS NULL, archive_size ASC, job_id ASC",
        "archive_size IS NULL, archive_size DESC, job_id DESC",
    ),
}

#: The download-plus-packaging join every downloaded-works query reads.
#:
#: The packaging task is found by `idempotency_key = 'convert:{candidate_id}'`
#: rather than by provider-and-candidate, because that key is what
#: `ConversionService` guarantees is unique per work -- matching on the provider
#: would need an aggregate to pick one of several rows and could pair a book with
#: a different attempt's error.
#:
#: `LEFT JOIN` on both artifacts: a work with no CBZ yet is precisely what the
#: 未打包 filter is for, so an inner join would hide the rows the page exists to
#: show.
_DOWNLOADED_SELECT = (
    "SELECT c.id, dj.id AS job_id, dj.provider, dj.state, "
    + title_subquery("c.id")
    + " AS title_value, "
    "arch.path, arch.size_bytes AS archive_size, "
    "pack.id AS pack_job_id, pack.state AS pack_state, "
    "pack.error_code, pack.error_message, "
    "cbz.path AS cbz_path, cbz.size_bytes, cbz.page_count, "
    "cbz.library_relative_path, "
    "(SELECT mv.field_value FROM metadata_values mv "
    " WHERE mv.candidate_id = c.id AND mv.field_name = 'Artist' "
    " ORDER BY mv.is_manual DESC, mv.confidence DESC LIMIT 1), "
    "(SELECT mv.field_value FROM metadata_values mv "
    " WHERE mv.candidate_id = c.id AND mv.field_name = 'Category' "
    " ORDER BY mv.is_manual DESC, mv.confidence DESC LIMIT 1), "
    "(SELECT mv.field_value FROM metadata_values mv "
    " WHERE mv.candidate_id = c.id AND mv.field_name = 'Language' "
    " ORDER BY mv.is_manual DESC, mv.confidence DESC LIMIT 1), "
    "c.thumb_url, dj.updated_at, "
    "pin.relative_path AS pinned_path, pin.is_manual AS pinned_is_manual, "
    # The AI answer, joined so the badge is one column rather than a query per
    # row. Joined on the candidate (its primary key) so it cannot multiply rows;
    # a candidate with no suggestion simply gets NULL.
    "sug.relative_path AS ai_relative_path, "
    # The Chinese tag list and the upstream one, read here so the 已下载 card
    # can show every Chinese tag without a query per row -- the same shape
    # the candidate list selects for its own cards.
    "(SELECT mv.field_value FROM metadata_values mv "
    " WHERE mv.candidate_id = c.id AND mv.field_name = 'Tags' "
    " ORDER BY mv.is_manual DESC, mv.confidence DESC LIMIT 1), "
    "(SELECT mv.field_value FROM metadata_values mv "
    " WHERE mv.candidate_id = c.id AND mv.field_name = 'TagsRaw' "
    " ORDER BY mv.is_manual DESC, mv.confidence DESC LIMIT 1), "
    # The filter verdict travels with the work so a flagged row can say why
    # without a second read (R60); the serializer decides whether it is shown.
    "c.filter_result, c.filter_reason, v.work_id, v.revision "
    "FROM download_jobs dj "
    "JOIN candidates c ON c.id = dj.candidate_id "
    "JOIN work_versions v ON v.candidate_id = c.id "
    "LEFT JOIN artifacts arch "
    "  ON arch.job_id = dj.id AND arch.artifact_type = 'ARCHIVE' "
    "LEFT JOIN download_jobs pack "
    "  ON pack.idempotency_key = 'convert:' || c.id "
    "LEFT JOIN artifacts cbz "
    "  ON cbz.job_id = pack.id AND cbz.artifact_type = 'CBZ' "
    "LEFT JOIN work_archive_paths pin ON pin.candidate_id = c.id "
    "LEFT JOIN ai_path_suggestions sug ON sug.candidate_id = c.id"
)


def _downloaded_work(row: Sequence[object]) -> DownloadedWork:
    """Map a `_DOWNLOADED_SELECT` row onto the list DTO."""

    def text(index: int) -> str | None:
        value = row[index]
        return str(value) if value is not None else None

    def number(index: int) -> int | None:
        value = row[index]
        return int(value) if value is not None else None

    return DownloadedWork(
        candidate_id=int(row[0]),
        job_id=int(row[1]),
        provider=str(row[2]),
        state=str(row[3]),
        title=text(4),
        archive_path=text(5),
        archive_size=number(6),
        pack_job_id=number(7),
        pack_state=text(8),
        pack_error_code=text(9),
        pack_error_message=text(10),
        cbz_path=text(11),
        cbz_size=number(12),
        page_count=number(13),
        library_relative_path=text(14),
        artist=text(15),
        category=text(16),
        language=text(17),
        thumb_url=text(18),
        updated_at=str(row[19]) if row[19] is not None else "",
        pinned_path=text(20),
        pinned_is_manual=bool(row[21]) if row[21] is not None else False,
        ai_relative_path=text(22),
        tags=text(23),
        raw_tags=text(24),
        filter_result=text(25),
        filter_reason=text(26),
        work_id=text(27),
        revision=number(28),
    )


#: The column list every `ai_providers` read shares. One constant because a
#: mapper and its SELECT drifting apart is a bug that only shows up as a wrong
#: field on a page, and there are four places that read this table.
_AI_PROVIDER_COLUMNS = (
    "id, name, code, base_url, timeout_seconds, max_retries, enabled, "
    "custom_headers, default_params, provider_uuid, revision, keys_revision, models_revision"
)

#: The chain projection keeps the same provider/model field order as these DTOs.
_AI_MODEL_COLUMNS = (
    "id, provider_id, name, enabled, last_verified_at, last_verify_ok, "
    "last_verify_error, params, model_uuid, revision"
)


def _ai_provider_from_row(row: Sequence[object]) -> AiProvider:
    """Map one `ai_providers` row onto its DTO."""

    return AiProvider(
        provider_id=int(row[0]),
        name=str(row[1]),
        code=str(row[2]),
        base_url=str(row[3]),
        timeout_seconds=int(row[4]),
        max_retries=int(row[5]),
        enabled=bool(row[6]),
        custom_headers=_json_object(row[7]),
        default_params=_params(row[8]),
        provider_uuid=str(row[9]),
        revision=int(row[10]),
        keys_revision=int(row[11]),
        models_revision=int(row[12]),
    )


def _ai_key_from_row(row: Sequence[object]) -> AiProviderKey:
    """Map one `ai_provider_keys` row onto its DTO (never the cipher)."""

    return AiProviderKey(
        key_id=int(row[0]),
        provider_id=int(row[1]),
        label=str(row[2] or ""),
        enabled=bool(row[3]),
        failures=int(row[4] or 0),
        cooldown_until=(
            str(row[5]) if row[5] is not None else None
        ),
        last_used_at=str(row[6]) if row[6] is not None else None,
        created_at=str(row[7]),
        key_uuid=str(row[8]),
        revision=int(row[9]),
        health_revision=int(row[10]),
    )


#: The chain query joins both tables and has to say so with aliases; it is the
#: one place that cannot reuse the plain column constants, so it gets its own.
_AI_CHAIN_SELECT = (
    "SELECT ch.position, "
    "p.id, p.name, p.code, p.base_url, p.timeout_seconds, p.max_retries, "
    "p.enabled, p.custom_headers, p.default_params, p.provider_uuid, p.revision, p.keys_revision, p.models_revision, "
    "m.id, m.provider_id, m.name, m.enabled, m.last_verified_at, "
    "m.last_verify_ok, m.last_verify_error, m.params, m.model_uuid, m.revision "
    "FROM ai_model_chain ch "
    "JOIN ai_provider_models m ON m.id = ch.provider_model_id "
    "JOIN ai_providers p ON p.id = m.provider_id "
)


def _ai_model_from_row(row: Sequence[object]) -> AiProviderModel:
    """Map one `ai_provider_models` row onto its DTO.

    `last_verify_ok` stays tri-state: None (never tested) is not False (tested
    and failed), and the settings page says the two differently.
    """

    return AiProviderModel(
        model_id=int(row[0]),
        provider_id=int(row[1]),
        name=str(row[2]),
        enabled=bool(row[3]),
        last_verified_at=(
            str(row[4]) if row[4] is not None else None
        ),
        last_verify_ok=(
            None if row[5] is None else bool(row[5])
        ),
        last_verify_error=(
            str(row[6]) if row[6] is not None else None
        ),
        params=_params(row[7]),
        model_uuid=str(row[8]),
        revision=int(row[9]),
    )


def _params_document(params: AiRequestParams | None) -> dict[str, object]:
    """`AiRequestParams` as the JSON object the column stores.

    Only the set fields are written, so 「没填」 round-trips as 「没填」 rather than
    as an explicit null that a later merge would have to special-case.
    """
    document: dict[str, object] = {}
    if params is None:
        return document
    if params.temperature is not None:
        document["temperature"] = params.temperature
    if params.max_tokens is not None:
        document["max_tokens"] = params.max_tokens
    document.update(params.extra_body)
    return document


def _json_object(value: object) -> dict[str, object]:
    """A JSON column read as a dict, tolerating anything a human typed.

    The page validates before saving, so a non-object here means a row written
    by an older build or edited by hand; treating it as 「空」 keeps one bad cell
    from taking a page down.
    """
    if isinstance(value, dict):
        return dict(value)
    if not isinstance(value, str) or not value.strip():
        return {}
    try:
        decoded = json.loads(value)
    except ValueError:
        return {}
    return decoded if isinstance(decoded, dict) else {}


def _params(value: object) -> AiRequestParams:
    """A stored params column as `AiRequestParams`; unreadable means empty.

    Same reasoning as `_json_object`: the write path refuses bad JSON, so the
    read path only has to survive a value that never went through it.
    """
    try:
        return parse_request_params(value)
    except ValueError:
        return AiRequestParams()


def _ai_path_suggestion(row: Sequence[object]) -> AiPathSuggestion:
    """Map one `ai_path_suggestions` row onto its DTO."""

    return AiPathSuggestion(
        candidate_id=int(row[0]),
        fingerprint=str(row[1]),
        prompt_hash=str(row[2]),
        relative_path=str(row[3]),
        directory=str(row[4]),
        filename=str(row[5]),
        provider_id=int(row[6]) if row[6] is not None else None,
        model_name=str(row[7]),
        attempts=int(row[8]),
        created_at=str(row[9]),
        updated_at=str(row[10]),
    )


def _rearchive_candidate(row: Sequence[object]) -> ReArchiveCandidate:
    """Map a `_list_rearchive_candidates_sync` row onto its DTO."""

    def text(index: int) -> str | None:
        value = row[index]
        return str(value) if value is not None else None

    return ReArchiveCandidate(
        candidate_id=int(row[0]),
        title=text(1),
        pack_state=text(2),
        cbz_path=text(3),
        library_relative_path=text(4),
        pinned_path=text(5),
        pinned_is_manual=bool(row[6]) if row[6] is not None else False,
    )


@dataclass(frozen=True, slots=True)
class CandidateFacet:
    """One filterable metadata dimension in the candidate sidebar.

    `multi_valued` decides both how a row is matched and how several selected
    values combine, and the two follow from each other. `Tags` holds a
    comma-joined list, so a value is matched inside it and picking two tags
    means「同时带这两个标签」-- each selection narrows. `Artist`, `Language` and
    `Category` hold one value per candidate, so they are matched exactly and
    several selections mean「任选其一」: requiring all of them would always
    return nothing, which reads as a broken filter rather than as a narrow one.
    """

    #: Metadata field names carrying this dimension, most-preferred first. Tags
    #: are searched in both the translated and the raw field so a filter still
    #: works on a candidate that was never enriched.
    fields: tuple[str, ...]
    multi_valued: bool = False


#: Facet name -> definition. The names are the query-string keys, so this table
#: is also the whitelist: `_list_candidates_page_sync` refuses a name that is
#: not here rather than interpolating it into SQL.
CANDIDATE_FACETS: dict[str, CandidateFacet] = {
    "tags": CandidateFacet(fields=("Tags", "TagsRaw"), multi_valued=True),
    "artist": CandidateFacet(fields=("Artist",)),
    "language": CandidateFacet(fields=("Language",)),
    "category": CandidateFacet(fields=("Category",)),
}

#: How many values one facet group offers. A sidebar listing every tag in the
#: library is not a filter, it is a second scroll region -- the operator reaches
#: an unlisted value through the search box instead.
CANDIDATE_FACET_LIMIT = 24

#: How many metadata rows one facet group reads before it stops counting. The
#: sidebar is a convenience, not a report: an install large enough to hit this
#: still gets the common values, and the cap is what keeps a page render from
#: walking the whole metadata table.
_FACET_SCAN_LIMIT = 20000

#: Match one value inside a comma-joined field. The value is wrapped in commas
#: on both sides so 「loli」 does not match 「lolicon」; the two `replace` calls
#: normalise the separator first, because the writers join with ", " and the
#: message parser can leave newlines behind.
_FACET_CONTAINS_SQL = (
    "',' || replace(replace(mv.field_value, char(10), ','), ', ', ',') || ',' "
    "LIKE ? ESCAPE '\\'"
)


def _escape_like(value: str) -> str:
    """Neutralise LIKE wildcards in operator-supplied search text.

    Without this a title containing ``%`` would match every row, which reads as
    a broken filter rather than as the literal search the operator asked for.
    """
    return (
        value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    )


def _split_facet_values(raw: object) -> list[str]:
    """Split a stored field into its display values, case preserved.

    `Database._parse_tag_list` lowercases for rule matching; a sidebar has to
    show the operator the tag as it is written, so this keeps the original text
    and only folds case when deciding whether two entries are the same one.
    """
    if not isinstance(raw, str):
        return []
    seen: set[str] = set()
    values: list[str] = []
    for item in raw.replace("\n", ",").split(","):
        token = item.strip()
        key = token.casefold()
        if token and key not in seen:
            seen.add(key)
            values.append(token)
    return values


def _candidate_list_item(row: Sequence[object]) -> CandidateListItem:
    """Map a `_CANDIDATE_LIST_SELECT` row onto the list DTO."""
    return CandidateListItem(
        candidate_id=int(row[0]),
        status=str(row[1]),
        filter_result=str(row[2]),
        title=str(row[3]) if row[3] is not None else None,
        message_count=int(row[9]),
        updated_at=str(row[10]),
        ex_gid=int(row[11]) if row[11] is not None else None,
        ex_gallery_token=str(row[12]) if row[12] is not None else None,
        artist=str(row[4]) if row[4] is not None else None,
        tags=str(row[5]) if row[5] is not None else None,
        raw_tags=str(row[6]) if row[6] is not None else None,
        category=str(row[7]) if row[7] is not None else None,
        language=str(row[8]) if row[8] is not None else None,
        thumb_url=str(row[13]) if row[13] is not None else None,
        filter_reason=str(row[14]) if row[14] is not None else None,
        work_id=str(row[15]),
        revision=int(row[16]),
    )


class Database:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.initialized = False
        from app.candidates.execution import IngestProcessors
        self.ingest_processors = IngestProcessors()

    async def initialize(self) -> None:
        await asyncio.to_thread(self._initialize_sync)
        self.initialized = True

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=5)
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 5000")
        return connection

    @contextmanager
    def connection(self) -> Iterator[sqlite3.Connection]:
        """One connection, committed or rolled back, and always closed.

        `with sqlite3.connect(...) as connection` does **not** close anything --
        it only ends the transaction -- so every one of the call sites that used
        the bare connection was leaving the handle to the garbage collector.
        On CPython that mostly works and on a WAL database it mostly does not
        show, but "mostly" is doing real work in that sentence: an open handle
        holds its read snapshot, which is what keeps `-wal` growing and what
        turns a checkpoint into a no-op.

        This is also the public entry point the services use. They reached into
        `Database._connect` before -- 27 call sites, each carrying its own
        `noqa: SLF001` -- which is a private method serving as an API. Naming it
        does not fix the layering, but it does stop the linter suppression from
        being the thing that documents it.
        """
        connection = self._connect()
        try:
            with connection:
                yield connection
                execution = current_execution.get()
                if (connection.total_changes and execution is not None
                        and execution.database_path == self.path):
                    execution.check(connection)
                if connection.total_changes:
                    for account_execution in current_account_executions.get():
                        if account_execution.database_path == self.path:
                            account_execution.check(connection)
        finally:
            connection.close()

    @staticmethod
    def _ensure_bot_account(connection: sqlite3.Connection) -> int:
        connection.execute(
            "INSERT INTO telegram_accounts "
            "(account_type, display_name, session_path, status) "
            "VALUES ('BOT', 'Telegram Bot API', 'bot-api://configured', "
            "'DISCONNECTED') ON CONFLICT(session_path) DO NOTHING"
        )
        return int(
            connection.execute(
                "SELECT id FROM telegram_accounts "
                "WHERE session_path = 'bot-api://configured'"
            ).fetchone()[0]
        )

    @staticmethod
    def _ensure_telegram_account(connection: sqlite3.Connection, external_account_id: str | None) -> int:
        """Return the legacy Bot row or the proven external Telegram row."""
        if external_account_id is None:
            return Database._ensure_bot_account(connection)
        row = connection.execute(
            "SELECT kind, label FROM external_accounts WHERE account_id = ? AND deleted_at IS NULL",
            (external_account_id,),
        ).fetchone()
        if row is None or row[0] not in {"TG_BOT", "TG_USER"}:
            raise ValueError("unknown Telegram account")
        connection.execute(
            "INSERT OR IGNORE INTO telegram_accounts(account_type, display_name, session_path, external_account_id) "
            "VALUES (?, ?, ?, ?)",
            ("BOT" if row[0] == "TG_BOT" else "USER", row[1],
             f"account://{external_account_id}", external_account_id),
        )
        return int(connection.execute(
            "SELECT id FROM telegram_accounts WHERE external_account_id = ?",
            (external_account_id,),
        ).fetchone()[0])

    @staticmethod
    def _parse_tag_list(value) -> tuple[str, ...]:
        if value is None:
            return ()
        if isinstance(value, str):
            items = [
                item
                for item in value.replace("\n", ",").split(",")
            ]
        elif isinstance(value, list):
            items = [str(item) for item in value]
        else:
            raise ValueError("tag list must be string or list")
        cleaned: list[str] = []
        for item in items:
            token = item.strip().lower()
            if token:
                cleaned.append(token)
        return tuple(cleaned)

    @staticmethod
    def _source_from_row(row: sqlite3.Row | tuple) -> TelegramSourceConfig:
        enabled = bool(row[4])
        try:
            rules = json.loads(row[5])
            if not isinstance(rules, dict):
                raise ValueError("source rules must be an object")
            raw_formats = rules.get("allowed_archive_formats", [])
            if not isinstance(raw_formats, list) or any(
                value not in {"zip", "rar", "7z", "cbz"}
                for value in raw_formats
            ):
                raise ValueError("invalid archive formats")
            max_attachment_size_mb = int(
                rules.get("max_attachment_size_mb", 0)
            )
            if max_attachment_size_mb < 0:
                raise ValueError("invalid attachment size")
            required_tags = Database._parse_tag_list(
                rules.get("required_tags")
            )
            forbidden_tags = Database._parse_tag_list(
                rules.get("forbidden_tags")
            )
            allowed_languages = Database._parse_tag_list(
                rules.get("allowed_languages")
            )
            allowed_categories = Database._parse_tag_list(
                rules.get("allowed_categories")
            )
            min_rating_raw = rules.get("min_rating")
            if min_rating_raw in (None, ""):
                min_rating: float | None = None
            else:
                try:
                    min_rating = float(min_rating_raw)
                except (TypeError, ValueError):
                    raise ValueError(
                        "invalid min_rating"
                    ) from None
        except (TypeError, ValueError, json.JSONDecodeError):
            enabled = False
            raw_formats = []
            max_attachment_size_mb = 0
            required_tags = ()
            forbidden_tags = ()
            allowed_languages = ()
            allowed_categories = ()
            min_rating = None
        return TelegramSourceConfig(
            source_id=int(row[0]),
            source_type=str(row[1]),
            chat_id=int(row[2]),
            display_name=str(row[3]),
            enabled=enabled,
            allowed_archive_formats=tuple(
                str(value).lower() for value in raw_formats
            ),
            max_attachment_size_mb=max_attachment_size_mb,
            required_tags=required_tags,
            forbidden_tags=forbidden_tags,
            allowed_languages=allowed_languages,
            allowed_categories=allowed_categories,
            min_rating=min_rating,
            account_id=row[6] if len(row) > 6 else None,
            account_label=row[7] if len(row) > 7 else None,
            account_kind=row[8] if len(row) > 8 else None,
            revision=int(row[9]) if len(row) > 9 else 1,
        )

    @staticmethod
    def _candidate_title_missing(
        connection: sqlite3.Connection, candidate_id: int
    ) -> bool:
        """Whether this candidate still has no name (R62).

        Both title fields count: a gallery that only has a Japanese name is
        named, and so is one an operator typed a title for by hand. The answer
        is "no title" only when neither written source exists -- the caption is
        not one any more -- which is the single condition that keeps a candidate
        in 待补充.
        """
        row = connection.execute(
            "SELECT 1 FROM metadata_values WHERE candidate_id = ? "
            "AND field_name IN ('Title', 'JapaneseTitle') LIMIT 1",
            (candidate_id,),
        ).fetchone()
        return row is None

    @staticmethod
    def _update_candidate_filter_state(
        connection: sqlite3.Connection,
        candidate_id: int,
        fallback_reason: str,
    ) -> None:
        has_title = not Database._candidate_title_missing(
            connection, candidate_id
        )
        needs_rows = connection.execute(
            "SELECT sm.filter_reason FROM candidate_messages cm "
            "JOIN source_messages sm ON sm.id = cm.source_message_id "
            "WHERE cm.candidate_id = ? AND sm.filter_result = 'NEEDS_INFO' "
            "ORDER BY sm.id",
            (candidate_id,),
        ).fetchall()
        needs_reason = next(
            (
                str(row[0])
                for row in needs_rows
                if row[0] != MISSING_TITLE_REASON or not has_title
            ),
            None,
        )
        connection.execute(
            "UPDATE candidates SET status = ?, filter_result = ?, "
            "filter_reason = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
            (
                "NEEDS_INFO" if needs_reason is not None else "PENDING_REVIEW",
                "NEEDS_INFO" if needs_reason is not None else "ACCEPT",
                needs_reason or fallback_reason,
                candidate_id,
            ),
        )

    async def configure_telegram_source(
        self,
        *,
        source_type: str,
        chat_id: int,
        display_name: str,
        enabled: bool,
        allowed_archive_formats: tuple[str, ...],
        max_attachment_size_mb: int,
        required_tags: tuple[str, ...] = (),
        forbidden_tags: tuple[str, ...] = (),
        allowed_languages: tuple[str, ...] = (),
        allowed_categories: tuple[str, ...] = (),
        min_rating: float | None = None,
        account_id: str | None = None,
    ) -> None:
        await asyncio.to_thread(
            self._configure_telegram_source_sync,
            source_type,
            chat_id,
            display_name,
            enabled,
            allowed_archive_formats,
            max_attachment_size_mb,
            required_tags,
            forbidden_tags,
            allowed_languages,
            allowed_categories,
            min_rating,
            account_id,
        )

    def _configure_telegram_source_sync(
        self,
        source_type: str,
        chat_id: int,
        display_name: str,
        enabled: bool,
        allowed_archive_formats: tuple[str, ...],
        max_attachment_size_mb: int,
        required_tags: tuple[str, ...] = (),
        forbidden_tags: tuple[str, ...] = (),
        allowed_languages: tuple[str, ...] = (),
        allowed_categories: tuple[str, ...] = (),
        min_rating: float | None = None,
        account_id: str | None = None,
    ) -> None:
        rules_payload = {
            "allowed_archive_formats": list(allowed_archive_formats),
            "max_attachment_size_mb": max_attachment_size_mb,
            "required_tags": list(required_tags),
            "forbidden_tags": list(forbidden_tags),
            "allowed_languages": list(allowed_languages),
            "allowed_categories": list(allowed_categories),
            "min_rating": min_rating,
        }
        rules_json = json.dumps(rules_payload, separators=(",", ":"))
        with self.connection() as connection:
            telegram_account_id = self._ensure_telegram_account(connection, account_id)
            connection.execute(
                "INSERT INTO telegram_sources "
                "(account_id, source_type, chat_id, display_name, enabled, "
                " rules_json, dismissed) "
                "VALUES (?, ?, ?, ?, ?, ?, 0) ON CONFLICT(account_id, chat_id) "
                "DO UPDATE SET source_type = excluded.source_type, "
                "display_name = excluded.display_name, enabled = excluded.enabled, "
                "rules_json = excluded.rules_json, dismissed = 0",
                (
                    telegram_account_id,
                    source_type,
                    chat_id,
                    display_name,
                    int(enabled),
                    rules_json,
                ),
            )

    async def add_telegram_sources_bulk(
        self, entries: list[dict], external_account_id: str | None = None
    ) -> int:
        """Create many sources at once, disabled, without disturbing existing ones.

        The batch half of 添加来源. `ON CONFLICT DO NOTHING` rather than an
        upsert: the picker hands back chats the operator may already have
        configured, and re-adding one must not silently replace rules it already
        carries. Existing rows keep theirs; only genuinely new chats are created.
        A chat that was 删除 earlier is the one exception -- re-selecting it in
        the picker is the same intent as saving it alone, so its tombstone is
        lifted while its rules stay untouched.
        """
        return await asyncio.to_thread(self._add_telegram_sources_bulk_sync, entries, external_account_id)

    def _add_telegram_sources_bulk_sync(self, entries: list[dict], external_account_id: str | None = None) -> int:
        if not entries:
            return 0
        with self.connection() as connection:
            account_id = self._ensure_telegram_account(connection, external_account_id)
            before = connection.total_changes
            connection.executemany(
                "INSERT INTO telegram_sources "
                "(account_id, source_type, chat_id, display_name, enabled, "
                " rules_json, dismissed) "
                "VALUES (?, ?, ?, ?, 0, '{}', 0) "
                "ON CONFLICT(account_id, chat_id) DO NOTHING",
                [
                    (
                        account_id,
                        str(entry["source_type"]),
                        int(entry["chat_id"]),
                        str(entry["display_name"]),
                    )
                    for entry in entries
                ],
            )
            created = connection.total_changes - before
            # Counted before the revive so a re-added tombstone is not reported
            # as a new source; only rows the operator cannot already see move.
            connection.executemany(
                "UPDATE telegram_sources SET dismissed = 0 "
                "WHERE account_id = ? AND chat_id = ? AND dismissed = 1",
                [(account_id, int(entry["chat_id"])) for entry in entries],
            )
            return created

    async def update_telegram_sources_bulk(
        self,
        source_ids: list[int],
        *,
        enabled: bool | None = None,
        rules: dict | None = None,
    ) -> int:
        """Apply one change to a selection of sources.

        `enabled` and `rules` are independent; a caller passing neither is a
        programming error, so the caller's own 400 guard stays in the route.
        """
        return await asyncio.to_thread(
            self._update_telegram_sources_bulk_sync,
            [int(value) for value in source_ids],
            enabled,
            rules,
        )

    def _update_telegram_sources_bulk_sync(
        self,
        source_ids: list[int],
        enabled: bool | None,
        rules: dict | None,
    ) -> int:
        if not source_ids:
            return 0
        assignments: list[str] = []
        params: list[object] = []
        if enabled is not None:
            assignments.append("enabled = ?")
            params.append(1 if enabled else 0)
        if rules is not None:
            assignments.append("rules_json = ?")
            params.append(json.dumps(rules, separators=(",", ":")))
        if not assignments:
            return 0
        assignments.append("dismissed = 0")
        placeholders = ", ".join("?" for _ in source_ids)
        with self.connection() as connection:
            cursor = connection.execute(
                f"UPDATE telegram_sources SET {', '.join(assignments)} "
                f"WHERE id IN ({placeholders})",
                (*params, *source_ids),
            )
            return int(cursor.rowcount or 0)

    async def dismiss_telegram_sources(self, source_ids: list[int]) -> int:
        """Tombstone a selection of sources so discovery cannot revive them.

        Not a DELETE: `discover_telegram_source` inserts a row for every chat a
        message arrives from, so a deleted row would come back on the next
        message. A tombstone clears the whitelist state (disabled, no rules) and
        is hidden from the settings list, while giving the operator a real way
        back: saving the chat again un-dismisses it.
        """
        return await asyncio.to_thread(
            self._dismiss_telegram_sources_sync,
            [int(value) for value in source_ids],
        )

    def _dismiss_telegram_sources_sync(self, source_ids: list[int]) -> int:
        if not source_ids:
            return 0
        placeholders = ", ".join("?" for _ in source_ids)
        with self.connection() as connection:
            cursor = connection.execute(
                f"UPDATE telegram_sources SET dismissed = 1, enabled = 0, "
                f"rules_json = '{{}}' WHERE id IN ({placeholders})",
                tuple(source_ids),
            )
            return int(cursor.rowcount or 0)

    async def list_telegram_sources(self) -> list[TelegramSourceConfig]:
        return await asyncio.to_thread(self._list_telegram_sources_sync)

    def _list_telegram_sources_sync(self) -> list[TelegramSourceConfig]:
        with self.connection() as connection:
            rows = connection.execute(
                "SELECT ts.id, ts.source_type, ts.chat_id, ts.display_name, "
                "ts.enabled, ts.rules_json, ta.external_account_id, ea.label, ea.kind, ts.revision "
                "FROM telegram_sources ts "
                "JOIN telegram_accounts ta ON ta.id = ts.account_id "
                "LEFT JOIN external_accounts ea ON ea.account_id = ta.external_account_id "
                "WHERE ts.dismissed = 0 AND ts.legacy_retired = 0 "
                "ORDER BY ts.enabled DESC, ts.id DESC"
            ).fetchall()
        return [self._source_from_row(row) for row in rows]

    async def telegram_ingest_targets(self, external_account_id: str | None = None) -> list[dict]:
        """The enabled sources the MTProto ingester should read, with cursors.

        One row per source, each carrying the id to resume after: the greater of
        the stored cursor and the newest message already in `source_messages`.
        The second half matters when both ingest paths run -- the bot may have
        ingested messages the user account never listed, and a cursor that
        ignored them would make the account re-read, and then discard, every one
        of them on each poll.

        `0` means 「nothing has ever been ingested from this chat」, which the
        caller resolves by seeding the cursor at the newest message rather than
        walking the archive.
        """
        return await asyncio.to_thread(self._telegram_ingest_targets_sync, external_account_id)

    def _telegram_ingest_targets_sync(self, external_account_id: str | None = None) -> list[dict]:
        with self.connection() as connection:
            rows = connection.execute(
                "SELECT ts.chat_id, ts.display_name, ta.external_account_id, "
                "MAX(COALESCE(ts.last_message_id, 0), "
                "    COALESCE((SELECT MAX(sm.message_id) FROM source_messages sm "
                "              WHERE sm.account_id = ts.account_id "
                "              AND sm.chat_id = ts.chat_id), 0)) "
                "FROM telegram_sources ts "
                "JOIN telegram_accounts ta ON ta.id = ts.account_id "
                "WHERE ((? IS NULL AND ta.session_path = 'bot-api://configured') OR ta.external_account_id = ?) "
                "AND ts.enabled = 1 AND ts.legacy_retired = 0 "
                "ORDER BY ts.id",
                (external_account_id, external_account_id),
            ).fetchall()
        return [
            {
                "chat_id": int(row[0]),
                "display_name": str(row[1]),
                "account_id": row[2],
                "cursor": int(row[3] or 0),
            }
            for row in rows
        ]

    async def set_source_cursor(self, chat_id: int, message_id: int, external_account_id: str | None = None) -> None:
        """Remember how far the MTProto ingester has read one chat.

        Used to seed a new subscription at its latest message. Received batches
        advance the cursor atomically with TelegramUserInbox.save_batch.
        """
        await run_sync(
            self._set_source_cursor_sync, chat_id, message_id, external_account_id
        )

    def _set_source_cursor_sync(self, chat_id: int, message_id: int, external_account_id: str | None = None) -> None:
        with self.connection() as connection:
            connection.execute(
                "UPDATE telegram_sources "
                "SET last_message_id = MAX(COALESCE(last_message_id, 0), ?) "
                "WHERE chat_id = ? AND COALESCE(last_message_id, 0) < ? AND account_id = ?",
                (int(message_id), int(chat_id), int(message_id),
                 self._ensure_telegram_account(connection, external_account_id)),
            )

    async def discover_telegram_source(
        self, message: ParsedSourceMessage
    ) -> TelegramSourceConfig:
        return await asyncio.to_thread(self._discover_telegram_source_sync, message)

    def _discover_telegram_source_sync(
        self, message: ParsedSourceMessage
    ) -> TelegramSourceConfig:
        with self.connection() as connection:
            account_id = self._ensure_telegram_account(connection, message.account_id)
            connection.execute(
                "INSERT INTO telegram_sources "
                "(account_id, source_type, chat_id, display_name, enabled) "
                "VALUES (?, ?, ?, ?, 0) ON CONFLICT(account_id, chat_id) "
                "DO NOTHING",
                (
                    account_id,
                    "CHANNEL" if message.chat_id < 0 else "PRIVATE_CHAT",
                    message.chat_id,
                    message.chat_title,
                ),
            )
            row = connection.execute(
                "SELECT id, source_type, chat_id, display_name, enabled, rules_json "
                "FROM telegram_sources WHERE account_id = ? AND chat_id = ?",
                (account_id, message.chat_id),
            ).fetchone()
        return self._source_from_row(row)

    def _initialize_sync(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        migrations_path = Path(__file__).with_name("migrations")
        with self.connection() as connection:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.execute("PRAGMA synchronous = NORMAL")
            connection.execute(
                "CREATE TABLE IF NOT EXISTS schema_migrations "
                "(version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)"
            )
            applied = {
                row[0]
                for row in connection.execute("SELECT version FROM schema_migrations")
            }
            for migration in sorted(migrations_path.glob("*.sql")):
                version = int(migration.stem.split("_", 1)[0])
                if version in applied:
                    continue
                migration_sql = migration.read_text(encoding="utf-8")
                # executescript commits a pending transaction before it starts.
                # Run the Python identity backfill after DDL, without a second
                # executescript, so failures roll back the entire migration.
                connection.executescript("BEGIN IMMEDIATE;\n" + migration_sql)
                if version == 31:
                    from app.candidates.migration import backfill_ingestion
                    backfill_ingestion(connection)
                connection.execute(
                    "INSERT INTO schema_migrations (version, applied_at) VALUES (?, CURRENT_TIMESTAMP)",
                    (version,),
                )
                connection.commit()

    async def check_writable(self) -> bool:
        if not self.initialized:
            return False
        return await asyncio.to_thread(self._check_writable_sync)

    def _check_writable_sync(self) -> bool:
        try:
            with self.connection() as connection:
                connection.execute("BEGIN IMMEDIATE")
                connection.rollback()
            return True
        except sqlite3.Error:
            return False

    @staticmethod
    def _administrator(connection) -> AdminAuth | None:
        row = connection.execute(
            "SELECT id, username, password_hash, password_changed, revision, "
            "auth_epoch FROM admin_users"
        ).fetchone()
        return AdminAuth(row[0], row[1], row[2], bool(row[3]), row[4], row[5]) if row else None

    async def get_administrator(self) -> AdminAuth | None:
        return await asyncio.to_thread(self._get_administrator_sync)

    def _get_administrator_sync(self):
        with self.connection() as connection:
            return self._administrator(connection)

    async def update_administrator(
        self, admin: AdminAuth, *, username: str | None = None,
        password_hash: str | None = None,
        actor_session_id: str | None = None, actor_credential_id: int | None = None,
        renew_session_id: str | None = None,
    ) -> AdminAuth:
        return await asyncio.to_thread(
            self._update_administrator_sync, admin, username, password_hash,
            actor_session_id, actor_credential_id, renew_session_id,
        )

    def _update_administrator_sync(
        self, admin, username, password_hash, actor_session_id, actor_credential_id, renew_session_id,
    ):
        with self.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            current = self._administrator(connection)
            if current is None or current.id != admin.id or current.revision != admin.revision:
                raise AuthError("REVISION_CONFLICT", "管理员配置已更新，请核对后重新提交")
            now = int(time.time())
            if actor_session_id is not None:
                active = connection.execute(
                    "SELECT 1 FROM browser_sessions WHERE session_id = ? AND admin_id = ? "
                    "AND auth_epoch = ? AND revoked_at IS NULL AND expires_at > ?",
                    (actor_session_id, current.id, current.auth_epoch, now),
                ).fetchone()
                if active is None:
                    raise AuthError("AUTH_TOKEN_REVOKED", "会话已结束，请重新登录")
            if actor_credential_id is not None:
                active = connection.execute(
                    "SELECT 1 FROM api_credentials WHERE id = ? AND revoked_at IS NULL "
                    "AND (expires_at IS NULL OR expires_at > ?) "
                    "AND (kind = 'api_key' OR (kind = 'access' AND auth_epoch = ?))",
                    (actor_credential_id, now, current.auth_epoch),
                ).fetchone()
                if active is None:
                    raise AuthError("AUTH_TOKEN_REVOKED", "凭据已被撤销，请重新登录")
            name = username if username is not None else current.username
            if name == current.username and password_hash is None:
                return current
            connection.execute(
                "UPDATE admin_users SET username = ?, password_hash = ?, "
                "password_changed = ?, revision = revision + 1, "
                "auth_epoch = auth_epoch + ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                (name, password_hash or current.password_hash,
                 int(current.password_changed or password_hash is not None),
                 int(password_hash is not None), current.id),
            )
            if password_hash is not None:
                connection.execute(
                    "UPDATE api_credentials SET revoked_at = CURRENT_TIMESTAMP "
                    "WHERE kind IN ('access', 'refresh') AND revoked_at IS NULL"
                )
                connection.execute(
                    "UPDATE browser_sessions SET revoked_at = CURRENT_TIMESTAMP "
                    "WHERE revoked_at IS NULL"
                )
                if renew_session_id is not None:
                    connection.execute(
                        "INSERT INTO browser_sessions (session_id, admin_id, auth_epoch, expires_at) "
                        "VALUES (?, ?, ?, ?)",
                        (renew_session_id, current.id, current.auth_epoch + 1, now + 14 * 86400),
                    )
            return self._administrator(connection)

    async def create_browser_session(self, admin: AdminAuth) -> str:
        return await asyncio.to_thread(self._create_browser_session_sync, admin)

    def _create_browser_session_sync(self, admin):
        with self.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            current = self._administrator(connection)
            if current is None or current.id != admin.id or current.revision != admin.revision:
                raise AuthError("REVISION_CONFLICT", "认证已更新，请重新登录")
            now = int(time.time())
            connection.execute("DELETE FROM browser_sessions WHERE expires_at <= ?", (now,))
            session_id = secrets.token_urlsafe(32)
            connection.execute(
                "INSERT INTO browser_sessions (session_id, admin_id, auth_epoch, expires_at) "
                "VALUES (?, ?, ?, ?)",
                (session_id, admin.id, admin.auth_epoch, now + 14 * 86400),
            )
            return session_id

    async def browser_session_admin(self, session_id: str) -> AdminAuth | None:
        return await asyncio.to_thread(self._browser_session_admin_sync, session_id)

    def _browser_session_admin_sync(self, session_id):
        with self.connection() as connection:
            admin = self._administrator(connection)
            if admin is None:
                return None
            row = connection.execute(
                "SELECT 1 FROM browser_sessions WHERE session_id = ? AND admin_id = ? "
                "AND auth_epoch = ? AND expires_at > ? AND revoked_at IS NULL",
                (session_id, admin.id, admin.auth_epoch, int(time.time())),
            ).fetchone()
            return admin if row else None

    async def revoke_browser_session(self, session_id: str) -> None:
        await asyncio.to_thread(self._revoke_browser_session_sync, session_id)

    def _revoke_browser_session_sync(self, session_id):
        with self.connection() as connection:
            connection.execute(
                "UPDATE browser_sessions SET revoked_at = CURRENT_TIMESTAMP WHERE session_id = ?",
                (session_id,),
            )

    async def issue_credential_pair(
        self, admin: AdminAuth, *, refresh: tuple, access: tuple, label: str,
        family_id: str, access_ttl: int, refresh_ttl: int,
        previous: ApiCredential | None = None,
    ) -> None:
        """Commit both hashes and optional refresh consumption as one transaction.

        Login checks the verified administrator revision. Rotation checks the
        credential epoch and consumes one still-valid refresh exactly once.
        """
        await asyncio.to_thread(
            self._issue_credential_pair_sync, admin, refresh, access, label,
            family_id, access_ttl, refresh_ttl, previous,
        )

    def _issue_credential_pair_sync(
        self, admin, refresh, access, label, family_id, access_ttl, refresh_ttl, previous,
    ):
        with self.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            current = self._administrator(connection)
            if current is None or current.id != admin.id or current.auth_epoch != admin.auth_epoch:
                raise AuthError("AUTH_TOKEN_REVOKED", "认证已更新，请重新登录")
            if previous is None and current.revision != admin.revision:
                raise AuthError("REVISION_CONFLICT", "认证已更新，请重新登录")
            if not current.password_changed:
                raise AuthError("PASSWORD_CHANGE_REQUIRED", "请先修改初始密码")
            now = int(time.time())
            if previous is not None:
                cursor = connection.execute(
                    "UPDATE api_credentials SET revoked_at = CURRENT_TIMESTAMP "
                    "WHERE id = ? AND kind = 'refresh' AND secret_hash = ? "
                    "AND family_id = ? AND auth_epoch = ? AND revoked_at IS NULL "
                    "AND expires_at > ?",
                    (previous.id, previous.secret_hash, family_id, current.auth_epoch, now),
                )
                if cursor.rowcount != 1:
                    raise AuthError("AUTH_TOKEN_REVOKED", "刷新凭据已失效，请重新登录")
            cursor = connection.execute(
                "INSERT INTO api_credentials (kind, label, public_id, secret_hash, "
                "family_id, expires_at, rotated_from, auth_epoch) "
                "VALUES ('refresh', ?, ?, ?, ?, ?, ?, ?)",
                (label, *refresh, family_id, now + refresh_ttl,
                 previous.id if previous else None, current.auth_epoch),
            )
            connection.execute(
                "INSERT INTO api_credentials (kind, label, public_id, secret_hash, "
                "family_id, expires_at, rotated_from, auth_epoch) "
                "VALUES ('access', ?, ?, ?, ?, ?, ?, ?)",
                (label, *access, family_id, now + access_ttl, cursor.lastrowid, current.auth_epoch),
            )

    async def get_admin_auth(self, username: str) -> tuple[str, bool] | None:
        return await asyncio.to_thread(self._get_admin_auth_sync, username)

    def _get_admin_auth_sync(self, username: str) -> tuple[str, bool] | None:
        with self.connection() as connection:
            row = connection.execute(
                "SELECT password_hash, password_changed "
                "FROM admin_users WHERE username = ?",
                (username,),
            ).fetchone()
        if row is None:
            return None
        return row[0], bool(row[1])

    async def set_bootstrap_admin(self, username: str, password_hash: str) -> None:
        await asyncio.to_thread(
            self._set_bootstrap_admin_sync, username, password_hash
        )

    def _set_bootstrap_admin_sync(self, username: str, password_hash: str) -> None:
        with self.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            admin = self._administrator(connection)
            if admin is None:
                connection.execute(
                    "INSERT INTO admin_users (username, password_hash) VALUES (?, ?)",
                    (username, password_hash),
                )
            elif not admin.password_changed:
                connection.execute(
                    "UPDATE admin_users SET password_hash = ?, revision = revision + 1, "
                    "auth_epoch = auth_epoch + 1, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                    (password_hash, admin.id),
                )

    async def change_admin_password(
        self, username: str, password_hash: str
    ) -> None:
        admin = await self.get_administrator()
        if admin is None or admin.username != username:
            raise LookupError("Administrator account does not exist")
        await self.update_administrator(admin, password_hash=password_hash)

    # --- Mobile / API credentials (migration 023) -------------------------
    #
    # One table serves both families; see `app/credentials.py` for the wire
    # format and the reasoning. Only hashes are stored -- these methods never
    # see a plaintext secret on the way out.

    async def create_api_credential(
        self,
        *,
        kind: str,
        label: str,
        public_id: str,
        secret_hash: str,
        family_id: str = "",
        expires_at: int | None = None,
        rotated_from: int | None = None,
    ) -> int:
        """Insert one credential and return its row id."""
        return await asyncio.to_thread(
            self._create_api_credential_sync,
            kind,
            label,
            public_id,
            secret_hash,
            family_id,
            expires_at,
            rotated_from,
        )

    def _create_api_credential_sync(
        self,
        kind: str,
        label: str,
        public_id: str,
        secret_hash: str,
        family_id: str,
        expires_at: int | None,
        rotated_from: int | None,
    ) -> int:
        with self.connection() as connection:
            admin = self._administrator(connection)
            cursor = connection.execute(
                "INSERT INTO api_credentials "
                "(kind, label, public_id, secret_hash, family_id, expires_at, "
                "rotated_from, auth_epoch) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    kind,
                    label,
                    public_id,
                    secret_hash,
                    family_id,
                    expires_at,
                    rotated_from,
                    admin.auth_epoch if kind != "api_key" and admin is not None else 0,
                ),
            )
            return int(cursor.lastrowid)

    async def get_api_credential(self, public_id: str) -> ApiCredential | None:
        return await asyncio.to_thread(self._get_api_credential_sync, public_id)

    def _get_api_credential_sync(self, public_id: str) -> ApiCredential | None:
        with self.connection() as connection:
            row = connection.execute(
                "SELECT id, kind, label, public_id, secret_hash, family_id, "
                "created_at, last_used_at, expires_at, revoked_at, rotated_from, auth_epoch "
                "FROM api_credentials WHERE public_id = ?",
                (public_id,),
            ).fetchone()
        return _api_credential_from_row(row) if row is not None else None

    async def active_api_key(self) -> ApiCredential | None:
        """The one valid API key, or None when the operator has not minted one."""
        return await asyncio.to_thread(self._active_api_key_sync)

    def _active_api_key_sync(self) -> ApiCredential | None:
        with self.connection() as connection:
            row = connection.execute(
                "SELECT id, kind, label, public_id, secret_hash, family_id, "
                "created_at, last_used_at, expires_at, revoked_at, rotated_from, auth_epoch "
                "FROM api_credentials "
                "WHERE kind = 'api_key' AND revoked_at IS NULL "
                "ORDER BY id DESC LIMIT 1"
            ).fetchone()
        return _api_credential_from_row(row) if row is not None else None

    async def touch_api_credential(self, credential_id: int) -> None:
        """Record use. Callers throttle this; it is a write on every call."""
        await asyncio.to_thread(self._touch_api_credential_sync, credential_id)

    def _touch_api_credential_sync(self, credential_id: int) -> None:
        with self.connection() as connection:
            connection.execute(
                "UPDATE api_credentials SET last_used_at = CURRENT_TIMESTAMP "
                "WHERE id = ?",
                (credential_id,),
            )

    async def revoke_api_credential(self, credential_id: int) -> None:
        await asyncio.to_thread(self._revoke_api_credential_sync, credential_id)

    def _revoke_api_credential_sync(self, credential_id: int) -> None:
        with self.connection() as connection:
            connection.execute(
                "UPDATE api_credentials SET revoked_at = CURRENT_TIMESTAMP "
                "WHERE id = ? AND revoked_at IS NULL",
                (credential_id,),
            )

    async def revoke_credential_family(self, family_id: str) -> int:
        """Revoke every access/refresh row sharing a login family."""
        return await asyncio.to_thread(
            self._revoke_credential_family_sync, family_id
        )

    def _revoke_credential_family_sync(self, family_id: str) -> int:
        if not family_id:
            return 0
        with self.connection() as connection:
            cursor = connection.execute(
                "UPDATE api_credentials SET revoked_at = CURRENT_TIMESTAMP "
                "WHERE family_id = ? AND revoked_at IS NULL",
                (family_id,),
            )
            return int(cursor.rowcount)

    async def revoke_password_credentials(self) -> int:
        """Revoke every password-derived token (used on a password change).

        The API key is deliberately excluded: it is an independent long-lived
        credential, and the operator asked that changing the password not
        invalidate it.
        """
        return await asyncio.to_thread(self._revoke_password_credentials_sync)

    def _revoke_password_credentials_sync(self) -> int:
        with self.connection() as connection:
            cursor = connection.execute(
                "UPDATE api_credentials SET revoked_at = CURRENT_TIMESTAMP "
                "WHERE kind IN ('access', 'refresh') AND revoked_at IS NULL"
            )
            return int(cursor.rowcount)

    async def replace_api_key(
        self, *, label: str, public_id: str, secret_hash: str
    ) -> None:
        """Revoke the current key and insert its replacement, atomically.

        One transaction rather than revoke-then-insert: the partial unique index
        permits a single valid key, so a double-submitted form that interleaved
        the two calls would make the second insert fail the constraint. Doing
        both here means concurrent mints serialise on the write lock instead.
        """
        await asyncio.to_thread(
            self._replace_api_key_sync, label, public_id, secret_hash
        )

    def _replace_api_key_sync(
        self, label: str, public_id: str, secret_hash: str, *, connection=None
    ) -> None:
        with self.connection() if connection is None else nullcontext(connection) as connection:
            connection.execute(
                "UPDATE api_credentials SET revoked_at = CURRENT_TIMESTAMP "
                "WHERE kind = 'api_key' AND revoked_at IS NULL"
            )
            connection.execute(
                "INSERT INTO api_credentials "
                "(kind, label, public_id, secret_hash) "
                "VALUES ('api_key', ?, ?, ?)",
                (label, public_id, secret_hash),
            )

    async def revoke_active_api_key(self) -> int:
        """Revoke the current key; returns how many rows changed (0 or 1)."""
        return await asyncio.to_thread(self._revoke_active_api_key_sync)

    def _revoke_active_api_key_sync(self, *, connection=None) -> int:
        with self.connection() if connection is None else nullcontext(connection) as connection:
            cursor = connection.execute(
                "UPDATE api_credentials SET revoked_at = CURRENT_TIMESTAMP "
                "WHERE kind = 'api_key' AND revoked_at IS NULL"
            )
            return int(cursor.rowcount)

    async def save_telegram_updates(self, updates: list[dict]) -> int:
        return await run_sync(self._save_telegram_updates_sync, updates)

    @staticmethod
    def _telegram_update_scope() -> tuple[str, str | None]:
        from app.connections.runtime import current_account_context
        context = current_account_context.get()
        if context is not None and context.kind == "TG_BOT":
            return context.account_id, context.account_id
        return "legacy", None

    def _save_telegram_updates_sync(self, updates: list[dict]) -> int:
        scope, external_account_id = self._telegram_update_scope()
        with self.connection() as connection:
            changes_before = connection.total_changes
            connection.executemany(
                "INSERT OR IGNORE INTO telegram_bot_updates "
                "(account_scope, external_account_id, update_id, payload_json) VALUES (?, ?, ?, ?)",
                [
                    (
                        scope, external_account_id,
                        int(update["update_id"]),
                        json.dumps(update, ensure_ascii=False, separators=(",", ":")),
                    )
                    for update in updates
                ],
            )
            return connection.total_changes - changes_before

    async def latest_telegram_update_id(self) -> int | None:
        return await asyncio.to_thread(self._latest_telegram_update_id_sync)

    def _latest_telegram_update_id_sync(self) -> int | None:
        with self.connection() as connection:
            value = connection.execute(
                "SELECT MAX(update_id) FROM telegram_bot_updates WHERE account_scope = ?",
                (self._telegram_update_scope()[0],),
            ).fetchone()[0]
        return int(value) if value is not None else None

    async def pending_telegram_updates(
        self, *, limit: int
    ) -> list[tuple[int, dict]]:
        return await asyncio.to_thread(self._pending_telegram_updates_sync, limit)

    def _pending_telegram_updates_sync(self, limit: int) -> list[tuple[int, dict]]:
        with self.connection() as connection:
            rows = connection.execute(
                "SELECT update_id, payload_json FROM telegram_bot_updates "
                "WHERE processed_at IS NULL AND account_scope = ? "
                "ORDER BY ROW_NUMBER() OVER (PARTITION BY COALESCE("
                "json_extract(payload_json, '$.channel_post.chat.id'), json_extract(payload_json, '$.message.chat.id'), "
                "json_extract(payload_json, '$.edited_channel_post.chat.id'), json_extract(payload_json, '$.edited_message.chat.id')) "
                "ORDER BY update_id), update_id LIMIT ?",
                (self._telegram_update_scope()[0], limit),
            ).fetchall()
        return [(int(row[0]), json.loads(row[1])) for row in rows]

    async def mark_telegram_update_result(
        self, update_id: int, result: str, reason: str
    ) -> None:
        await asyncio.to_thread(
            self._mark_telegram_update_result_sync,
            update_id,
            result,
            reason,
        )

    def _mark_telegram_update_result_sync(
        self, update_id: int, result: str, reason: str
    ) -> None:
        with self.connection() as connection:
            connection.execute(
                "UPDATE telegram_bot_updates SET processed_at = CURRENT_TIMESTAMP, "
                "processing_result = ?, processing_reason = ? "
                "WHERE update_id = ? AND account_scope = ?",
                (result, reason, update_id, self._telegram_update_scope()[0]),
            )

    async def deactivate_candidate_message(self, chat_id: int, message_id: int, *, account_id: str | None = None) -> None:
        await asyncio.to_thread(
            self._deactivate_candidate_message_sync, chat_id, message_id, account_id
        )

    def _deactivate_candidate_message_sync(
        self, chat_id: int, message_id: int, account_id: str | None = None
    ) -> None:
        from app.candidates.persistence import deactivate_observation
        from app.connections.runtime import current_account_context
        context = current_account_context.get()
        if account_id is None and context is not None and context.kind in {"TG_BOT", "TG_USER"}:
            account_id = context.account_id
        with self.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            from app.candidates.changes import defer_deactivation
            if defer_deactivation(connection, self, chat_id, message_id, account_id):
                return
            deactivate_observation(connection, chat_id, message_id, account_id)

    async def save_candidate_message(
        self, update_id: int | None, message: ParsedSourceMessage
    ) -> bool:
        return await asyncio.to_thread(
            self._save_candidate_message_sync, update_id, message
        )

    def _save_candidate_message_sync(
        self, update_id: int | None, message: ParsedSourceMessage
    ) -> bool:
        from app.candidates.persistence import save_decided_message
        return save_decided_message(self, update_id, message)

    async def list_candidates(
        self, *, status: str = "PENDING_REVIEW", limit: int = 100
    ) -> list[CandidateListItem]:
        return await asyncio.to_thread(
            self._list_candidates_sync, status, limit
        )

    async def list_candidates_page(
        self,
        *,
        statuses: Sequence[str] | None = None,
        search: str | None = None,
        facets: Mapping[str, Sequence[str]] | None = None,
        sort: str = "newest",
        direction: str | None = None,
        offset: int = 0,
        limit: int = 50,
    ) -> tuple[list[CandidateListItem], int]:
        """One page of candidates plus the unpaged total.

        The total is returned alongside the rows because the interface needs
        both to render a pager, and computing it in a second round trip from
        the caller would let the two disagree when an ingest lands between the
        queries.

        `facets` selects on metadata dimensions named in `CANDIDATE_FACETS`; an
        unknown name raises rather than being ignored, because a silently
        dropped filter shows the operator more rows than they asked for and
        looks like the filter working.
        """
        return await asyncio.to_thread(
            self._list_candidates_page_sync,
            tuple(statuses) if statuses else (),
            search or "",
            {
                name: tuple(values)
                for name, values in (facets or {}).items()
                if values
            },
            sort,
            direction,
            offset,
            limit,
        )

    def _list_candidates_page_sync(
        self,
        statuses: tuple[str, ...],
        search: str,
        facets: Mapping[str, tuple[str, ...]],
        sort: str,
        direction: str | None,
        offset: int,
        limit: int,
    ) -> tuple[list[CandidateListItem], int]:
        where: list[str] = []
        params: list[object] = []
        if statuses:
            placeholders = ", ".join("?" for _ in statuses)
            where.append(f"c.status IN ({placeholders})")
            params.extend(statuses)
        if search.strip():
            # The `$…$` grammar (R61) compiles to EXISTS fragments over the
            # same (candidate_id, field_name) index the old substring search
            # used. Parsing is total, so a malformed query degrades to free
            # text instead of raising here.
            compiled = candidate_search_sql(
                parse_query(search, page=PAGE_CANDIDATES)
            )
            if compiled.clause != "1=1":
                where.append(compiled.clause)
                params.extend(compiled.params)
        for name, values in facets.items():
            facet = CANDIDATE_FACETS.get(name)
            if facet is None:
                raise ValueError(f"unknown candidate facet: {name}")
            fields = ", ".join("?" for _ in facet.fields)
            if facet.multi_valued:
                # One EXISTS per value, so the selections AND together.
                for value in values:
                    where.append(
                        "EXISTS (SELECT 1 FROM metadata_values mv "
                        " WHERE mv.candidate_id = c.id "
                        f"   AND mv.field_name IN ({fields}) "
                        f"   AND {_FACET_CONTAINS_SQL})"
                    )
                    params.extend(facet.fields)
                    params.append(f"%,{_escape_like(value)},%")
            else:
                chosen = ", ".join("?" for _ in values)
                where.append(
                    "EXISTS (SELECT 1 FROM metadata_values mv "
                    " WHERE mv.candidate_id = c.id "
                    f"   AND mv.field_name IN ({fields}) "
                    f"   AND mv.field_value IN ({chosen}))"
                )
                params.extend(facet.fields)
                params.extend(values)
        clause = f"WHERE {' AND '.join(where)}" if where else ""

        # Whitelisted so a hand-typed query string can never reach the SQL
        # text; an unknown key falls back to the default rather than erroring,
        # because a bookmarked link with a stale sort should still render.
        order = order_by(CANDIDATE_SORT_ORDERS, sort, direction)

        with self.connection() as connection:
            total = int(
                connection.execute(
                    f"SELECT COUNT(*) FROM candidates c {clause}",
                    tuple(params),
                ).fetchone()[0]
            )
            rows = connection.execute(
                f"{_CANDIDATE_LIST_SELECT} {clause} "
                f"GROUP BY c.id ORDER BY {order} LIMIT ? OFFSET ?",
                (*params, limit, offset),
            ).fetchall()
        return [_candidate_list_item(row) for row in rows], total

    def _list_candidates_sync(
        self, status: str, limit: int
    ) -> list[CandidateListItem]:
        with self.connection() as connection:
            rows = connection.execute(
                f"{_CANDIDATE_LIST_SELECT} WHERE c.status = ? "
                "GROUP BY c.id ORDER BY c.id DESC LIMIT ?",
                (status, limit),
            ).fetchall()
        return [_candidate_list_item(row) for row in rows]

    async def maintain_titles(self) -> dict[str, object]:
        """One-time repair of the titles written before R62.

        Run once at startup (`app/wiring.build_lifespan`). The display query
        already ignores these rows -- the gallery outranks the caption -- so this
        pass is not what makes a card correct; it is what stops the *stored*
        rows from lying. A caption's first line is not a title, and leaving it
        in the metadata table means the drawer offers it as one, and a candidate
        whose gallery answered without a name would look named.

        Only rows no operator wrote are touched (`is_manual` / `is_locked` are
        skipped), and only the three sources the message parser used to write.
        A candidate left without a name goes back to 待补充 -- from the statuses
        where that is a real statement, never out of APPROVED or REJECTED.

        Idempotent through a marker in `system_settings`.
        """
        return await asyncio.to_thread(self._maintain_titles_sync)

    def _maintain_titles_sync(self) -> dict[str, object]:
        with self.connection() as connection:
            done = connection.execute(
                "SELECT value FROM system_settings WHERE key = ?",
                (TITLE_MAINTENANCE_KEY,),
            ).fetchone()
            if done is not None:
                return {
                    "status": "already_done",
                    "scanned": 0,
                    "deleted": 0,
                    "needs_info": 0,
                }
            candidate_ids = [
                int(row[0])
                for row in connection.execute(
                    "SELECT DISTINCT candidate_id FROM metadata_values "
                    "WHERE field_name = 'Title' "
                    "AND value_source IN ('TELEGRAM', 'INFERRED', 'FILENAME') "
                    "AND is_manual = 0 AND is_locked = 0"
                ).fetchall()
            ]
            deleted = connection.execute(
                "DELETE FROM metadata_values WHERE field_name = 'Title' "
                "AND value_source IN ('TELEGRAM', 'INFERRED', 'FILENAME') "
                "AND is_manual = 0 AND is_locked = 0"
            ).rowcount
            needs_info = 0
            for candidate_id in candidate_ids:
                if not self._candidate_title_missing(connection, candidate_id):
                    continue
                # A candidate the operator approved, rejected or that is
                # already downloading keeps its status: this pass repairs what
                # a title was read off, and none of those states is a statement
                # about the title.
                changed = connection.execute(
                    "UPDATE candidates SET status = 'NEEDS_INFO', "
                    "filter_result = 'NEEDS_INFO', filter_reason = ?, "
                    "updated_at = CURRENT_TIMESTAMP "
                    "WHERE id = ? AND status IN ('PENDING_REVIEW', "
                    "'NEEDS_INFO')",
                    (MISSING_TITLE_REASON, candidate_id),
                ).rowcount
                needs_info += int(changed)
            connection.execute(
                "INSERT INTO system_settings (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value, "
                "updated_at = CURRENT_TIMESTAMP",
                (
                    TITLE_MAINTENANCE_KEY,
                    json.dumps(
                        {
                            "completed_at": datetime.now(UTC).isoformat(
                                timespec="seconds"
                            ),
                            "scanned": len(candidate_ids),
                            "deleted": deleted,
                            "needs_info": needs_info,
                        },
                        separators=(",", ":"),
                    ),
                ),
            )
        return {
            "status": "completed",
            "scanned": len(candidate_ids),
            "deleted": deleted,
            "needs_info": needs_info,
        }

    async def candidate_counts(self) -> dict[str, int]:
        return await asyncio.to_thread(self._candidate_counts_sync)

    def _candidate_counts_sync(self) -> dict[str, int]:
        # Every status gets a key, always, so a tab badge renders銆�0銆峳ather
        # than disappearing when nothing is in that state. `total` is counted
        # from the same rows rather than summed by the caller, which keeps it
        # correct if a future status is added without updating the map.
        counts = {key: 0 for key in CANDIDATE_COUNT_KEYS.values()}
        counts["total"] = 0
        with self.connection() as connection:
            rows = connection.execute(
                "SELECT status, COUNT(*) FROM candidates GROUP BY status"
            ).fetchall()
        for status, count in rows:
            counts["total"] += int(count)
            key = CANDIDATE_COUNT_KEYS.get(str(status))
            if key is not None:
                counts[key] = int(count)
        return counts

    async def list_downloaded_works(
        self,
        *,
        search: str | None = None,
        pack_filter: str = "all",
        sort: str = "newest",
        direction: str | None = None,
        offset: int = 0,
        limit: int = 50,
    ) -> tuple[list[DownloadedWork], int]:
        """One page of downloaded works plus the unpaged total.

        A work here is a candidate with a COMPLETED download that produced an
        ARCHIVE artifact -- that is what「已下载」means, and it is also exactly
        what packaging can act on. The packaging task is joined in rather than
        fetched per row, so a page of fifty is two queries instead of fifty-one.

        `pack_filter` groups by packaging outcome because that is the axis this
        page exists for: an operator comes here to find what still needs packing
        or what failed, not to browse. An unknown value raises for the same
        reason an unknown facet does -- a silently ignored filter shows more rows
        than were asked for and looks like the filter working.
        """
        if pack_filter not in DOWNLOADED_PACK_FILTERS:
            raise ValueError(f"unknown pack filter: {pack_filter}")
        return await asyncio.to_thread(
            self._list_downloaded_works_sync,
            (search or "").strip(),
            pack_filter,
            sort,
            direction,
            offset,
            limit,
        )

    def _list_downloaded_works_sync(
        self,
        search: str,
        pack_filter: str,
        sort: str,
        direction: str | None,
        offset: int,
        limit: int,
    ) -> tuple[list[DownloadedWork], int]:
        where = [
            "dj.state = ?",
            "dj.provider <> ?",
            "arch.path IS NOT NULL",
        ]
        params: list[object] = [
            DOWNLOAD_STATE_COMPLETED,
            PROVIDER_CONVERSION,
        ]
        if search.strip():
            # Same `$…$` grammar as the candidate list, with `source:` bound to
            # the job's provider. A free word still matches a provider by its
            # code or Chinese label, so the old 「EH 归档」 search keeps working.
            compiled = downloaded_search_sql(
                parse_query(search, page=PAGE_DOWNLOADED)
            )
            if compiled.clause != "1=1":
                where.append(compiled.clause)
                params.extend(compiled.params)
        # Each filter is expressed against the CBZ artifact or the packing job's
        # state, never against the candidate's status: packaging leaves the
        # status alone, so the status cannot answer「打好包了吗」.
        if pack_filter == "packed":
            where.append("cbz.path IS NOT NULL")
        elif pack_filter == "unpacked":
            where.append("cbz.path IS NULL AND pack.state IS NULL")
        elif pack_filter == "failed":
            where.append("pack.state = ?")
            params.append(CONVERSION_STATE_FAILED)
        elif pack_filter == "attention":
            placeholders = ", ".join("?" for _ in _DOWNLOADED_ATTENTION_STATES)
            where.append(f"pack.state IN ({placeholders})")
            params.extend(_DOWNLOADED_ATTENTION_STATES)

        clause = " AND ".join(where)
        order = order_by(DOWNLOADED_SORT_ORDERS, sort, direction)
        with self.connection() as connection:
            total = int(
                connection.execute(
                    f"SELECT COUNT(*) FROM ({_DOWNLOADED_SELECT} WHERE {clause})",
                    tuple(params),
                ).fetchone()[0]
            )
            rows = connection.execute(
                f"{_DOWNLOADED_SELECT} WHERE {clause} "
                f"ORDER BY {order} LIMIT ? OFFSET ?",
                (*params, limit, offset),
            ).fetchall()
        return [_downloaded_work(row) for row in rows], total

    async def archive_path_pin(self, candidate_id: int) -> dict | None:
        """The explicit archive path set for one work, if there is one."""
        return await asyncio.to_thread(self._archive_path_pin_sync, candidate_id)

    def _archive_path_pin_sync(self, candidate_id: int) -> dict | None:
        with self.connection() as connection:
            row = connection.execute(
                "SELECT relative_path, is_manual, operator_name, updated_at "
                "FROM work_archive_paths WHERE candidate_id = ?",
                (candidate_id,),
            ).fetchone()
        if row is None:
            return None
        return {
            "relative_path": str(row[0]),
            "is_manual": bool(row[1]),
            "operator_name": str(row[2]),
            "updated_at": str(row[3]) if row[3] is not None else "",
        }

    async def candidate_at_archive_path(self, relative_path: str) -> int | None:
        """Which work is pinned to this path, or None if it is free.

        The read behind「名称已存在」. It answers about the *pin* only; the caller
        also has to look at the filesystem, because a book packed before anyone
        pinned anything occupies a name without a row here.
        """
        return await asyncio.to_thread(
            self._candidate_at_archive_path_sync, relative_path
        )

    def _candidate_at_archive_path_sync(self, relative_path: str) -> int | None:
        with self.connection() as connection:
            row = connection.execute(
                "SELECT candidate_id FROM work_archive_paths "
                "WHERE relative_path = ?",
                (relative_path,),
            ).fetchone()
        return int(row[0]) if row is not None else None

    async def set_archive_path_pin(
        self,
        candidate_id: int,
        relative_path: str,
        *,
        is_manual: bool = True,
        operator_name: str = "admin",
    ) -> None:
        """Record where this work's CBZ must land from now on.

        A computed write against a work whose path the operator typed is a no-op
        -- not a partial update that keeps the flag and replaces the path. That is
        the whole content of the guard: a batch re-file recomputes paths from the
        layout template, and a template is a default, so it must not overwrite a
        decision. An operator who named one book by hand and then batch-packs
        fifty keeps that name.
        """
        await asyncio.to_thread(
            self._set_archive_path_pin_sync,
            candidate_id,
            relative_path,
            is_manual,
            operator_name,
        )

    def _set_archive_path_pin_sync(
        self,
        candidate_id: int,
        relative_path: str,
        is_manual: bool,
        operator_name: str,
    ) -> None:
        with self.connection() as connection:
            self.write_archive_path_pin(connection, candidate_id, relative_path, is_manual, operator_name)

    @staticmethod
    def write_archive_path_pin(
        connection: sqlite3.Connection, candidate_id: int, relative_path: str,
        is_manual: bool, operator_name: str, *, override_manual: bool = False,
    ) -> None:
        """Write a pin in the caller's transaction; only forced refile overrides manual paths."""
        connection.execute(
            "INSERT INTO work_archive_paths "
            "(candidate_id, relative_path, is_manual, operator_name) "
            "VALUES (?, ?, ?, ?) "
            "ON CONFLICT(candidate_id) DO UPDATE SET "
            "  relative_path = excluded.relative_path, "
            "  is_manual = excluded.is_manual, "
            "  operator_name = excluded.operator_name, "
            "  updated_at = CURRENT_TIMESTAMP "
            # Guard the whole UPDATE: retaining only is_manual while changing
            # relative_path would still overwrite the operator's decision.
            "WHERE ? OR NOT (work_archive_paths.is_manual = 1 "
            "           AND excluded.is_manual = 0)",
            (candidate_id, relative_path, 1 if is_manual else 0, operator_name, override_manual),
        )

    async def override_archive_path_pin(
        self,
        candidate_id: int,
        relative_path: str,
        *,
        operator_name: str = "admin",
    ) -> None:
        """Record a computed path over one the operator typed.

        The one caller is 强制重新归档, whose instruction is 「所有作品按当前
        规则重算」 -- so the guard in `set_archive_path_pin`, which exists so a
        *default* never overwrites a decision, is exactly what must not apply.
        A named method rather than a `force=` flag on that one, because the
        bypass has to be greppable: nothing else in this codebase should be able
        to demote a manual pin, and a boolean argument is how that would happen by
        accident.

        `is_manual` is written as 0 on purpose: after the move the pin is a
        computed path again, so the next template change may re-file the book.
        """
        await asyncio.to_thread(
            self._override_archive_path_pin_sync,
            candidate_id,
            relative_path,
            operator_name,
        )

    def _override_archive_path_pin_sync(
        self, candidate_id: int, relative_path: str, operator_name: str
    ) -> None:
        with self.connection() as connection:
            self.write_archive_path_pin(
                connection, candidate_id, relative_path, False, operator_name, override_manual=True,
            )

    async def clear_archive_path_pin(self, candidate_id: int) -> None:
        """Drop the pin, so the layout template decides again."""
        await asyncio.to_thread(self._clear_archive_path_pin_sync, candidate_id)

    def _clear_archive_path_pin_sync(self, candidate_id: int) -> None:
        with self.connection() as connection:
            connection.execute(
                "DELETE FROM work_archive_paths WHERE candidate_id = ?",
                (candidate_id,),
            )

    async def candidate_jobs(self, candidate_id: int) -> list[tuple[int, str]]:
        """``(job_id, provider)`` for every job this candidate owns.

        A work can hold one download row per source it was tried with
        (`telegram:` / `telegram-user:` / `exhentai:` / `telegraph:` / `torrent:`
        idempotency keys) plus a packaging row, so "the work's jobs" is a list,
        not the newest row. Removal reads this; before it existed, removing a
        work left every superseded source's row and files behind.
        """
        return await asyncio.to_thread(self._candidate_jobs_sync, int(candidate_id))

    def _candidate_jobs_sync(self, candidate_id: int) -> list[tuple[int, str]]:
        with self.connection() as connection:
            rows = connection.execute(
                "SELECT id, provider FROM download_jobs "
                "WHERE candidate_id = ? ORDER BY id",
                (candidate_id,),
            ).fetchall()
        return [(int(row[0]), str(row[1])) for row in rows]

    async def candidate_artifacts(self, candidate_id: int) -> list[tuple[str, str]]:
        """``(artifact_type, path)`` for every file this candidate produced."""
        return await asyncio.to_thread(
            self._candidate_artifacts_sync, int(candidate_id)
        )

    def _candidate_artifacts_sync(self, candidate_id: int) -> list[tuple[str, str]]:
        with self.connection() as connection:
            rows = connection.execute(
                "SELECT artifacts.artifact_type, artifacts.path "
                "FROM artifacts JOIN download_jobs "
                "  ON download_jobs.id = artifacts.job_id "
                "WHERE download_jobs.candidate_id = ? ORDER BY artifacts.id",
                (int(candidate_id),),
            ).fetchall()
        return [(str(row[0]), str(row[1])) for row in rows]

    async def purge_candidate(
        self,
        candidate_id: int,
        *,
        deleted_files: bool,
        operator_name: str,
        job_ids: list[int] | None = None,
        jobs: list[tuple[int, str]] | None = None,
    ) -> dict:
        """Delete a candidate and everything it produced, keeping an audit row.

        The candidate is the work's identity: its metadata, message links, review
        history, path pin, AI suggestions and every job row hang off it.
        `remove_work` deliberately keeps all of that -- it removes a *download*
        -- and this is the other decision: 彻底删除，把候选本身也去掉。

        `review_actions` and `download_jobs` do not cascade from `candidates`, so
        they are deleted explicitly; `artifacts` has to go first because it
        points at a job row with foreign keys ON. Everything else
        (`candidate_messages`, `metadata_values`, `work_archive_paths`,
        `ai_path_suggestions`) cascades with the candidate.

        `removed_works` gets one row per deleted job, written before the candidate
        goes so the record outlives it. A candidate that never downloaded has no
        job to record.

        The job list may be passed in by the caller, which has already resolved
        the paths it is about to delete; a second read is harmless but pointless.
        """
        return await asyncio.to_thread(
            self._purge_candidate_sync,
            int(candidate_id),
            bool(deleted_files),
            operator_name,
            job_ids,
            jobs,
        )

    def _purge_candidate_sync(
        self,
        candidate_id: int,
        deleted_files: bool,
        operator_name: str,
        job_ids: list[int] | None,
        jobs: list[tuple[int, str]] | None,
    ) -> dict:
        with self.connection() as connection:
            if jobs is None:
                jobs = [
                    (int(row[0]), str(row[1]))
                    for row in connection.execute(
                        "SELECT id, provider FROM download_jobs "
                        "WHERE candidate_id = ? ORDER BY id",
                        (candidate_id,),
                    ).fetchall()
                ]
            if job_ids is None:
                job_ids = [job_id for job_id, _ in jobs]
            title_row = connection.execute(
                "SELECT " + title_subquery("?") + "",
                (candidate_id,),
            ).fetchone()
            title = str(title_row[0]) if title_row is not None else None
            archive_row = connection.execute(
                "SELECT artifacts.path FROM artifacts "
                "WHERE artifacts.artifact_type = 'ARCHIVE' "
                "AND artifacts.job_id IN (SELECT id FROM download_jobs "
                "  WHERE candidate_id = ?) ORDER BY artifacts.id DESC LIMIT 1",
                (candidate_id,),
            ).fetchone()
            cbz_row = connection.execute(
                "SELECT artifacts.path FROM artifacts "
                "WHERE artifacts.artifact_type = 'CBZ' "
                "AND artifacts.job_id IN (SELECT id FROM download_jobs "
                "  WHERE candidate_id = ?) ORDER BY artifacts.id DESC LIMIT 1",
                (candidate_id,),
            ).fetchone()
            archive_path = str(archive_row[0]) if archive_row is not None else None
            cbz_path = str(cbz_row[0]) if cbz_row is not None else None
            if job_ids:
                placeholders = ", ".join("?" for _ in job_ids)
                connection.execute(
                    f"DELETE FROM artifacts WHERE job_id IN ({placeholders})",
                    tuple(job_ids),
                )
                connection.execute(
                    f"DELETE FROM download_jobs WHERE id IN ({placeholders})",
                    tuple(job_ids),
                )
            connection.execute(
                "DELETE FROM review_actions WHERE candidate_id = ?",
                (candidate_id,),
            )
            cursor = connection.execute(
                "DELETE FROM candidates WHERE id = ?", (candidate_id,)
            )
            removed_candidate = int(cursor.rowcount or 0)
            # One audit row per *candidate*, and only when it actually had a
            # job: a candidate that never downloaded has no work to record, and
            # a row for it would claim something was removed that never existed.
            if jobs:
                primary_job_id, primary_provider = jobs[-1]
                connection.execute(
                    "INSERT INTO removed_works "
                    "(candidate_id, job_id, provider, title, archive_path, "
                    " cbz_path, deleted_files, operator_name) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        candidate_id,
                        int(primary_job_id),
                        str(primary_provider),
                        title,
                        archive_path,
                        cbz_path,
                        1 if deleted_files else 0,
                        operator_name,
                    ),
                )
        return {
            "candidate_id": candidate_id,
            "removed_candidate": removed_candidate,
            "removed_jobs": len(job_ids),
        }

    async def downloaded_work_counts(self) -> dict[str, int]:
        """One count per pack filter, for the tab strip.

        Every filter is counted in one pass over the same join the list uses, so
        a badge cannot claim a number the list will not produce.
        """
        return await asyncio.to_thread(self._downloaded_work_counts_sync)

    def _downloaded_work_counts_sync(self) -> dict[str, int]:
        attention = ", ".join("?" for _ in _DOWNLOADED_ATTENTION_STATES)
        with self.connection() as connection:
            row = connection.execute(
                "SELECT COUNT(*), "
                "SUM(CASE WHEN cbz_path IS NOT NULL THEN 1 ELSE 0 END), "
                "SUM(CASE WHEN cbz_path IS NULL AND pack_state IS NULL "
                "         THEN 1 ELSE 0 END), "
                "SUM(CASE WHEN pack_state = ? THEN 1 ELSE 0 END), "
                f"SUM(CASE WHEN pack_state IN ({attention}) THEN 1 ELSE 0 END) "
                f"FROM ({_DOWNLOADED_SELECT} WHERE dj.state = ? "
                "       AND dj.provider <> ? AND arch.path IS NOT NULL)",
                (
                    CONVERSION_STATE_FAILED,
                    *_DOWNLOADED_ATTENTION_STATES,
                    DOWNLOAD_STATE_COMPLETED,
                    PROVIDER_CONVERSION,
                ),
            ).fetchone()
        return {
            "all": int(row[0] or 0),
            "packed": int(row[1] or 0),
            "unpacked": int(row[2] or 0),
            "failed": int(row[3] or 0),
            "attention": int(row[4] or 0),
        }

    async def downloaded_work(self, candidate_id: int) -> DownloadedWork | None:
        """One work that has produced an archive, or None.

        Used by the action paths, which need the current paths and the packing
        state before they delete, rename or requeue anything.

        Deliberately *not* filtered on `state = COMPLETED`, unlike the list: a
        work being re-downloaded has an archive from its previous run and a job
        row that is PENDING again, and it must still be found. Filtering here
        would make it invisible, and the caller's「已有任务在进行」guard would be
        unreachable -- the operator would be told the work has no archive, which
        is both wrong and unactionable. The state travels on the DTO so the
        caller decides what to do about it.
        """
        return await asyncio.to_thread(
            self._downloaded_work_sync, int(candidate_id)
        )

    def _downloaded_work_sync(self, candidate_id: int, connection=None) -> DownloadedWork | None:
        with nullcontext(connection) if connection is not None else self.connection() as connection:
            row = connection.execute(
                f"{_DOWNLOADED_SELECT} WHERE dj.provider <> ? "
                "  AND arch.path IS NOT NULL "
                "  AND c.id = ? ORDER BY dj.id DESC LIMIT 1",
                (PROVIDER_CONVERSION, candidate_id),
            ).fetchone()
        return _downloaded_work(row) if row is not None else None

    async def list_rearchive_candidates(self) -> list[ReArchiveCandidate]:
        """Every work the re-archive sweep may act on: one row per candidate.

        The universe is 「已下载内容」 -- a completed download that produced an
        ARCHIVE artifact -- because that is exactly what packaging can publish.
        Written as `EXISTS` over the candidate rather than over the job list so a
        candidate whose several sources all completed appears once: the sweep acts
        on *books*, and two rows for one book would be two moves of one file.

        The packing task is joined by `idempotency_key`, the same key
        `_DOWNLOADED_SELECT` uses and for the same reason -- it is what
        `ConversionService` guarantees is unique per work. The pin is joined on
        the candidate because that is where the decision lives now.

        Read in one query rather than by paging `list_downloaded_works`: this is a
        whole-library pass, and the paged query would either need a second
        page-size concept or a `.limit` nobody would maintain.
        """
        return await asyncio.to_thread(self._list_rearchive_candidates_sync)

    def _list_rearchive_candidates_sync(self) -> list[ReArchiveCandidate]:
        with self.connection() as connection:
            rows = connection.execute(
                "SELECT c.id, "
                + title_subquery("c.id")
                + ", pack.state, cbz.path, cbz.library_relative_path, "
                "pin.relative_path, pin.is_manual "
                "FROM candidates c "
                "LEFT JOIN download_jobs pack "
                "  ON pack.idempotency_key = 'convert:' || c.id "
                "LEFT JOIN artifacts cbz "
                "  ON cbz.job_id = pack.id AND cbz.artifact_type = 'CBZ' "
                "LEFT JOIN work_archive_paths pin ON pin.candidate_id = c.id "
                "WHERE EXISTS (SELECT 1 FROM download_jobs dj "
                "  JOIN artifacts arch ON arch.job_id = dj.id "
                "    AND arch.artifact_type = 'ARCHIVE' "
                "  WHERE dj.candidate_id = c.id AND dj.state = ? "
                "    AND dj.provider <> ?) "
                "ORDER BY c.id",
                (DOWNLOAD_STATE_COMPLETED, PROVIDER_CONVERSION),
            ).fetchall()
        return [_rearchive_candidate(row) for row in rows]

    async def candidate_facets(
        self, *, statuses: Sequence[str] | None = None
    ) -> dict[str, tuple[tuple[str, int], ...]]:
        """Values worth offering in the filter sidebar, with their row counts.

        Scoped to the same statuses as the list it filters, so the sidebar never
        offers a tag that would return nothing in the tab the operator is
        looking at. Not scoped to the *other* selected facets: a facet group
        that removes its own alternatives as soon as one is picked cannot be
        widened again without clearing it, which is the classic dead-end filter.
        """
        return await asyncio.to_thread(
            self._candidate_facets_sync,
            tuple(statuses) if statuses else (),
        )

    def _candidate_facets_sync(
        self, statuses: tuple[str, ...]
    ) -> dict[str, tuple[tuple[str, int], ...]]:
        status_clause = ""
        status_params: tuple[str, ...] = ()
        if statuses:
            placeholders = ", ".join("?" for _ in statuses)
            status_clause = f" AND c.status IN ({placeholders})"
            status_params = statuses
        options: dict[str, tuple[tuple[str, int], ...]] = {}
        with self.connection() as connection:
            for name, facet in CANDIDATE_FACETS.items():
                fields = ", ".join("?" for _ in facet.fields)
                rows = connection.execute(
                    "SELECT mv.candidate_id, mv.field_value "
                    "FROM candidates c JOIN metadata_values mv "
                    "ON mv.candidate_id = c.id "
                    f"WHERE mv.field_name IN ({fields}) "
                    "  AND mv.field_value IS NOT NULL "
                    "  AND mv.field_value <> ''"
                    f"{status_clause} LIMIT ?",
                    (*facet.fields, *status_params, _FACET_SCAN_LIMIT),
                ).fetchall()
                # Counted over distinct candidates in Python rather than with
                # COUNT(DISTINCT) in SQL, because one candidate can carry the
                # same tag in both `Tags` and `TagsRaw` and two GROUP BY rows
                # would count it twice -- a badge that overstates its own list.
                seen: dict[str, set[int]] = {}
                display: dict[str, str] = {}
                for candidate_id, value in rows:
                    if facet.multi_valued:
                        tokens = _split_facet_values(value)
                    else:
                        # Kept verbatim: the filter matches this string exactly,
                        # so a trimmed display value would select nothing.
                        tokens = [str(value)]
                    for token in tokens:
                        key = token.casefold()
                        display.setdefault(key, token)
                        seen.setdefault(key, set()).add(int(candidate_id))
                ranked = sorted(
                    seen.items(),
                    key=lambda item: (-len(item[1]), display[item[0]]),
                )
                options[name] = tuple(
                    (display[key], len(ids))
                    for key, ids in ranked[:CANDIDATE_FACET_LIMIT]
                )
        return options

    async def get_candidate(self, candidate_id: int) -> CandidateDetail | None:
        return await asyncio.to_thread(self._get_candidate_sync, candidate_id)

    async def adjacent_candidate_ids(
        self, candidate_id: int
    ) -> tuple[int | None, int | None]:
        """The neighbouring candidate ids, `(prev, next)` by id order.

        The detail page offers 上一个作品 / 下一个作品 as a quick jump; the
        natural order is the candidate id, which is also the order the 候选
        list is newest-first in. `prev` is the id immediately below, `next`
        immediately above; either may be ``None`` at the ends of the set.
        """
        return await asyncio.to_thread(
            self._adjacent_candidate_ids_sync, candidate_id
        )

    def _adjacent_candidate_ids_sync(
        self, candidate_id: int
    ) -> tuple[int | None, int | None]:
        with self.connection() as connection:
            prev = connection.execute(
                "SELECT id FROM candidates WHERE id < ? "
                "ORDER BY id DESC LIMIT 1",
                (candidate_id,),
            ).fetchone()
            next = connection.execute(
                "SELECT id FROM candidates WHERE id > ? "
                "ORDER BY id ASC LIMIT 1",
                (candidate_id,),
            ).fetchone()
        return (
            int(prev[0]) if prev is not None else None,
            int(next[0]) if next is not None else None,
        )

    async def set_candidate_eh_ref(
        self, candidate_id: int, ex_gid: int, ex_gallery_token: str | None
    ) -> dict:
        """Correct the unified identity after old executions are confirmed stopped."""
        return await asyncio.to_thread(
            self._set_candidate_eh_ref_sync,
            candidate_id,
            ex_gid,
            ex_gallery_token,
        )

    def _set_candidate_eh_ref_sync(
        self, candidate_id: int, ex_gid: int, ex_gallery_token: str | None
    ) -> dict:
        from app.candidates.references import request_reference
        return request_reference(self, candidate_id, ex_gid, ex_gallery_token)

    async def set_candidate_preview_url(
        self, candidate_id: int, preview_url: str | None
    ) -> None:
        """Manually record the Telegraph page that previews a work.

        An operator who has a preview page the ingestor missed can pin it here
        rather than re-posting the whole message: the detail page reads this
        single value, so writing it is all a correction needs.
        """
        await asyncio.to_thread(
            self._set_candidate_preview_url_sync, candidate_id, preview_url
        )

    def _set_candidate_preview_url_sync(
        self, candidate_id: int, preview_url: str | None, *, connection=None
    ) -> None:
        with nullcontext(connection) if connection is not None else self.connection() as connection:
            connection.execute(
                "UPDATE candidates SET preview_url = ?, "
                "updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                (preview_url, candidate_id),
            )

    async def set_candidate_manual_torrent(
        self,
        candidate_id: int,
        magnet_url: str | None,
        torrent_hash: str | None,
    ) -> None:
        """Manually pin a magnet as the work's torrent source.

        The operator's correction to a torrent the ingestor could not pull. The
        EH_TORRENT provider reads `magnet_url` sized to `torrent_hash` (the btih)
        ahead of a `.torrent` fetch, so pinning both is what routes the download
        through the magnet rather than the gallery. The gallery's own torrent
        count is dropped because a manual pin overrides it.
        """
        await asyncio.to_thread(
            self._set_candidate_manual_torrent_sync,
            candidate_id,
            magnet_url,
            torrent_hash,
        )

    def _set_candidate_manual_torrent_sync(
        self,
        candidate_id: int,
        magnet_url: str | None,
        torrent_hash: str | None,
        *, connection=None,
    ) -> None:
        with nullcontext(connection) if connection is not None else self.connection() as connection:
            connection.execute(
                "UPDATE candidates SET magnet_url = ?, torrent_hash = ?, "
                "torrent_count = NULL, "
                "updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                (magnet_url, torrent_hash, candidate_id),
            )

    def _get_candidate_sync(self, candidate_id: int) -> CandidateDetail | None:
        with self.connection() as connection:
            row = connection.execute(
                "SELECT c.id, c.status, c.filter_result, c.filter_reason, "
                + title_subquery("c.id")
                + ", c.ex_gid, c.ex_gallery_token, c.preview_url, "
                "c.torrent_count, "
                "c.torrent_hash, c.magnet_url, w.work_id FROM candidates c "
                "JOIN work_versions w ON w.candidate_id = c.id WHERE c.id = ?",

                (candidate_id,),
            ).fetchone()
            if row is None:
                return None
            message_rows = connection.execute(
                "SELECT COALESCE(ts.display_name, CAST(sm.chat_id AS TEXT)), "
                "sm.message_id, sm.message_text, sm.attachment_json, "
                "sm.message_date, sm.id, sm.observation_revision, ta.external_account_id, ea.kind, "
                "sm.chat_id, sm.peer_type FROM candidate_messages cm "
                "JOIN source_messages sm ON sm.id = cm.source_message_id "
                "JOIN telegram_accounts ta ON ta.id = sm.account_id "
                "LEFT JOIN external_accounts ea ON ea.account_id = ta.external_account_id "
                "LEFT JOIN telegram_sources ts ON ts.id = sm.rule_source_id "
                "WHERE cm.candidate_id = ? AND sm.message_state = 'ACTIVE' "
                "ORDER BY sm.id",
                (candidate_id,),
            ).fetchall()
        messages = tuple(
            CandidateMessage(
                chat_title=str(message_row[0]),
                message_id=int(message_row[1]),
                message_text=str(message_row[2] or ""),
                attachments=tuple({**attachment, "observation_id": int(message_row[5]),
                    "observation_revision": int(message_row[6]), "account_id": message_row[7],
                    "account_kind": message_row[8], "chat_id": int(message_row[9]),
                    "message_id": int(message_row[1]), "peer_type": message_row[10]}
                    for attachment in json.loads(message_row[3])),
                message_date=str(message_row[4]),
                observation_id=int(message_row[5]), observation_revision=int(message_row[6]),
                account_id=message_row[7], account_kind=message_row[8], chat_id=int(message_row[9]),
            )
            for message_row in message_rows
        )
        return CandidateDetail(
            candidate_id=int(row[0]),
            status=str(row[1]),
            filter_result=str(row[2]),
            filter_reason=str(row[3]),
            title=str(row[4]) if row[4] is not None else None,
            ex_gid=int(row[5]) if row[5] is not None else None,
            ex_gallery_token=str(row[6]) if row[6] is not None else None,
            messages=messages,
            preview_url=str(row[7]) if row[7] is not None else None,
            torrent_count=int(row[8]) if row[8] is not None else None,
            torrent_hash=str(row[9]) if row[9] is not None else None,
            magnet_url=str(row[10]) if row[10] is not None else None,
            work_id=str(row[11]),
        )

    async def locate_candidate_message(
        self, candidate_id: int, file_unique_id: str | None
    ) -> tuple[int, int] | None:
        """Where a candidate's attachment was posted: `(chat_id, message_id)`.

        The MTProto download route needs this and a bot `file_id` cannot supply
        it. New attachments carry both numbers inline, but rows ingested before
        that field existed do not -- and those are exactly the oversized books an
        operator wants to re-fetch with a user account. Reading them back off
        `source_messages` means the feature works on an existing database instead
        of only on messages received after the upgrade.

        Matched on `file_unique_id` when there is one, because a candidate can
        hold several messages and only one of them holds the archive; the newest
        message is the fallback when the id is unknown.
        """
        return await asyncio.to_thread(
            self._locate_candidate_message_sync, candidate_id, file_unique_id
        )

    def _locate_candidate_message_sync(
        self, candidate_id: int, file_unique_id: str | None
    ) -> tuple[int, int] | None:
        with self.connection() as connection:
            rows = connection.execute(
                "SELECT sm.chat_id, sm.message_id, sm.file_unique_id "
                "FROM candidate_messages cm "
                "JOIN source_messages sm ON sm.id = cm.source_message_id "
                "WHERE cm.candidate_id = ? "
                "ORDER BY sm.message_date DESC, sm.message_id DESC",
                (candidate_id,),
            ).fetchall()
        if not rows:
            return None
        if file_unique_id:
            for row in rows:
                if str(row[2] or "") == file_unique_id:
                    return int(row[0]), int(row[1])
        return int(rows[0][0]), int(rows[0][1])

    async def create_manual_candidate(
        self,
        *,
        filter_reason: str,
        ex_gid: int | None = None,
        ex_gallery_token: str | None = None,
        magnet_url: str | None = None,
        torrent_hash: str | None = None,
        ingest_claim=None,
    ) -> int:
        """Create an already-approved candidate from an operator's manual link.

        Manual adds skip the Telegram review funnel: the row lands straight in
        APPROVED so a download can be enqueued immediately. It carries no
        title: a manually added work used to get an `ExHentai #<gid>` row here
        so the page had something to show, but R62 made the gallery the only
        automatic source of a name and R63 has the caller fetch the gallery
        *before* inserting -- so a candidate that exists has a real name or the
        display fallback, never a fabricated one.
        """
        return await asyncio.to_thread(
            self._create_manual_candidate_sync,
            filter_reason,
            ex_gid,
            ex_gallery_token,
            magnet_url,
            torrent_hash,
            ingest_claim,
        )

    @staticmethod
    def _manual_duplicate_sync(
        connection: sqlite3.Connection,
        ex_gid: int | None,
        torrent_hash: str | None,
    ) -> DuplicateCandidateError | None:
        """The existing candidate a manual add would duplicate, if any.

        Gallery first because it is the indexed, authoritative identity; the
        magnet check compares lowercased because a btih is hex and case is a
        spelling, not a different torrent. There is no unique index on
        `torrent_hash` -- adding one would fail on a database that already
        holds duplicates -- so this in-transaction read is the whole gate on
        the magnet side.
        """
        if ex_gid is not None:
            row = connection.execute(
                'SELECT c.id, w.work_id FROM candidates c JOIN work_versions w ON w.candidate_id = c.id '
                'WHERE c.ex_gid = ?', (ex_gid,),
            ).fetchone()
            if row is not None:
                return DuplicateGalleryCandidate(int(row[0]), str(row[1]))
        if torrent_hash:
            row = connection.execute(
                "SELECT c.id, w.work_id FROM candidates c JOIN work_versions w ON w.candidate_id = c.id "
                "WHERE LOWER(c.torrent_hash) = ? ORDER BY c.id LIMIT 1",
                (str(torrent_hash).lower(),),
            ).fetchone()
            if row is not None:
                return DuplicateTorrentCandidate(int(row[0]), str(row[1]))
        return None

    @staticmethod
    def _gallery_candidate_id_sync(
        connection: sqlite3.Connection, ex_gid: int
    ) -> int | None:
        """Read the gallery's existing candidate id on an open connection.

        The body of `candidate_id_for_gallery`, shared so the in-transaction
        dup check and the gate read the same row the same way.
        """
        row = connection.execute(
            "SELECT id FROM candidates WHERE ex_gid = ? ORDER BY id LIMIT 1",
            (int(ex_gid),),
        ).fetchone()
        return int(row[0]) if row is not None else None

    def _create_manual_candidate_sync(
        self,
        filter_reason: str,
        ex_gid: int | None,
        ex_gallery_token: str | None,
        magnet_url: str | None,
        torrent_hash: str | None,
        ingest_claim=None,
    ) -> int:
        with self.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            return self.insert_manual_candidate(
                connection, filter_reason=filter_reason, ex_gid=ex_gid, ex_gallery_token=ex_gallery_token,
                magnet_url=magnet_url, torrent_hash=torrent_hash, ingest_claim=ingest_claim,
            )

    def insert_manual_candidate(self, connection, *, filter_reason: str, ex_gid: int | None,
                                ex_gallery_token: str | None, magnet_url=None, torrent_hash=None,
                                ingest_claim=None) -> int:
        """Insert and finish ownership inside the caller's BEGIN IMMEDIATE transaction.

        The validated manual publisher also writes metadata before committing.
        No candidate or LINKED entry is visible if any part of that write fails.
        """
        if not connection.in_transaction:
            raise ValueError("Manual publication requires a write transaction")
        if ex_gid is not None:
            from app.candidates.entries import check_claim, claim_manual_gallery
            if ingest_claim is None:
                ingest_claim = claim_manual_gallery(connection, ex_gid, owner=secrets.token_hex(16))
            else:
                entry = check_claim(connection, ingest_claim)
                from app.candidates.identity import ingest_key
                if entry["unique_key"] != ingest_key(gallery_id=ex_gid):
                    raise ValueError("Manual claim does not match gallery")
        duplicate = self._manual_duplicate_sync(connection, ex_gid, torrent_hash)
        if duplicate is not None:
            raise duplicate
        try:
            cursor = connection.execute(
                "INSERT INTO candidates "
                "(status, ex_gid, ex_gallery_token, filter_result, filter_reason, magnet_url, torrent_hash) "
                "VALUES ('APPROVED', ?, ?, 'ACCEPT', ?, ?, ?)",
                (ex_gid, ex_gallery_token, filter_reason, magnet_url, torrent_hash),
            )
        except sqlite3.IntegrityError:
            duplicate = self._manual_duplicate_sync(connection, ex_gid, None)
            if duplicate is not None:
                raise duplicate from None
            raise
        candidate_id = int(cursor.lastrowid)
        if ingest_claim is not None:
            from app.candidates.entries import finish_claim
            finish_claim(connection, ingest_claim, candidate_id=candidate_id)
        return candidate_id

    async def set_candidate_metadata_value(
        self,
        candidate_id: int,
        field_name: str,
        field_value: str,
        source: str,
        confidence: float | None = None,
    ) -> None:
        await asyncio.to_thread(
            self._set_candidate_metadata_value_sync,
            candidate_id,
            field_name,
            field_value,
            source,
            confidence,
        )

    def _set_candidate_metadata_value_sync(
        self,
        candidate_id: int,
        field_name: str,
        field_value: str,
        source: str,
        confidence: float | None,
    ) -> None:
        with self.connection() as connection:
            connection.execute(
                "INSERT INTO metadata_values "
                "(candidate_id, field_name, field_value, value_source, "
                "confidence) VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT(candidate_id, field_name, value_source) "
                "DO UPDATE SET field_value = excluded.field_value, "
                "confidence = excluded.confidence",
                (candidate_id, field_name, field_value, source, confidence),
            )


    async def transition_candidate_status(
        self,
        candidate_id: int,
        operator_name: str,
        action: str,
        new_status: str,
        note: str | None,
    ) -> None:
        await asyncio.to_thread(
            self._transition_candidate_status_sync,
            candidate_id,
            operator_name,
            action,
            new_status,
            note,
        )


    def _transition_candidate_status_sync(
        self,
        candidate_id: int,
        operator_name: str,
        action: str,
        new_status: str,
        note: str | None,
        connection=None,
    ) -> None:
        details = {"status": new_status}
        if note:
            details["note"] = note
        details_json = json.dumps(details, separators=(",", ":"))
        with nullcontext(connection) if connection is not None else self.connection() as connection:
            cursor = connection.execute(
                "SELECT status FROM candidates WHERE id = ?",
                (candidate_id,),
            )
            row = cursor.fetchone()
            if row is None:
                raise LookupError(f"Candidate {candidate_id} does not exist")
            current_status = str(row[0])
            # `statuses_allowing` rather than a set of literals kept here: the
            # copy that used to live in this class drifted from the one the page
            # reads, and a state the page offers but the write refuses is a
            # button that can only produce an error.
            if current_status not in statuses_allowing(action):
                raise PermissionError(
                    f"Candidate in state {current_status} cannot be reviewed"
                )
            connection.execute(
                "UPDATE candidates SET status = ?, filter_reason = ?, "
                "updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                (new_status, note or "", candidate_id),
            )
            connection.execute(
                "INSERT INTO review_actions "
                "(candidate_id, action, operator_name, details_json) "
                "VALUES (?, ?, ?, ?)",
                (candidate_id, action, operator_name, details_json),
            )


    async def set_manual_metadata(
        self,
        candidate_id: int,
        operator_name: str,
        field_name: str,
        field_value: str,
    ) -> None:
        await asyncio.to_thread(
            self._set_manual_metadata_sync,
            candidate_id,
            operator_name,
            field_name,
            field_value,
        )


    def _set_manual_metadata_sync(
        self,
        candidate_id: int,
        operator_name: str,
        field_name: str,
        field_value: str,
        connection=None,
    ) -> None:
        with nullcontext(connection) if connection is not None else self.connection() as connection:
            candidate_row = connection.execute(
                "SELECT 1 FROM candidates WHERE id = ?",
                (candidate_id,),
            ).fetchone()
            if candidate_row is None:
                raise LookupError(f"Candidate {candidate_id} does not exist")
            connection.execute(
                "INSERT INTO metadata_values "
                "(candidate_id, field_name, field_value, value_source, "
                "confidence, is_manual) VALUES (?, ?, ?, 'OPERATOR', 1.0, 1) "
                "ON CONFLICT(candidate_id, field_name, value_source) "
                "DO UPDATE SET field_value = excluded.field_value, "
                "confidence = 1.0, is_manual = 1, "
                "created_at = CURRENT_TIMESTAMP",
                (candidate_id, field_name, field_value),
            )
            connection.execute(
                "INSERT INTO review_actions "
                "(candidate_id, action, operator_name, details_json) "
                "VALUES (?, 'EDIT_METADATA', ?, ?)",
                (
                    candidate_id,
                    operator_name,
                    json.dumps(
                        {"field": field_name, "value": field_value},
                        separators=(",", ":"),
                    ),
                ),
            )

    async def set_metadata_lock(
        self,
        candidate_id: int,
        operator_name: str,
        field_name: str,
        locked: bool,
    ) -> None:
        await asyncio.to_thread(
            self._set_metadata_lock_sync,
            candidate_id,
            operator_name,
            field_name,
            locked,
        )

    def _set_metadata_lock_sync(
        self,
        candidate_id: int,
        operator_name: str,
        field_name: str,
        locked: bool,
        connection=None,
    ) -> None:
        """Pin or release every stored value for one field.

        The lock is set on all rows for the field, not just the winning one,
        because the operator is expressing a decision about the field rather
        than about the row that happens to be selected today: a later scrape
        adding a higher-confidence value must not slip past the lock by
        landing on a row that was never locked.
        """
        with nullcontext(connection) if connection is not None else self.connection() as connection:
            candidate_row = connection.execute(
                "SELECT 1 FROM candidates WHERE id = ?",
                (candidate_id,),
            ).fetchone()
            if candidate_row is None:
                raise LookupError(f"Candidate {candidate_id} does not exist")
            cursor = connection.execute(
                "UPDATE metadata_values SET is_locked = ? "
                "WHERE candidate_id = ? AND field_name = ?",
                (1 if locked else 0, candidate_id, field_name),
            )
            if cursor.rowcount == 0:
                raise LookupError(
                    f"Candidate {candidate_id} has no {field_name} value"
                )
            connection.execute(
                "INSERT INTO review_actions "
                "(candidate_id, action, operator_name, details_json) "
                "VALUES (?, 'LOCK_METADATA', ?, ?)",
                (
                    candidate_id,
                    operator_name,
                    json.dumps(
                        {"field": field_name, "locked": bool(locked)},
                        separators=(",", ":"),
                    ),
                ),
            )

    async def effective_metadata(self, candidate_id: int) -> dict[str, str]:
        return await asyncio.to_thread(
            self._effective_metadata_sync, candidate_id
        )

    def _effective_metadata_sync(self, candidate_id: int, connection=None) -> dict[str, str]:
        with nullcontext(connection) if connection is not None else self.connection() as connection:
            rows = connection.execute(
                # The same trust order the list queries use (R62): the
                # gallery's own record first, a caption line last. This is what
                # the archive path rules and the auto-approval rules read, so a
                # rule matching `{Title}` sees the work's name too.
                "SELECT mv.field_name, mv.field_value FROM metadata_values mv "
                "WHERE mv.candidate_id = ? ORDER BY mv.is_manual DESC, "
                + METADATA_SOURCE_RANK_SQL
                + " DESC, mv.confidence DESC, mv.created_at DESC",
                (candidate_id,),
            ).fetchall()
        metadata: dict[str, str] = {}
        for field_name, field_value in rows:
            metadata.setdefault(str(field_name), str(field_value))
        return metadata

    async def candidate_id_for_gallery(self, ex_gid: int) -> int | None:
        """The candidate that already owns this gallery id, if any.

        Keyed on the id alone, not the `(id, token)` pair the table is unique
        on: the token is part of the URL rather than part of the work's
        identity, and a re-post that spells it differently still names the same
        gallery. Used by the ingestion gate (R59) and by the dedup pass.
        """
        return await asyncio.to_thread(self._candidate_id_for_gallery_sync, ex_gid)

    def _candidate_id_for_gallery_sync(self, ex_gid: int) -> int | None:
        with self.connection() as connection:
            return self._gallery_candidate_id_sync(connection, ex_gid)

    async def duplicate_gallery_groups(self) -> list[dict]:
        """Every candidate that shares a gallery id with at least one other.

        One row per candidate, ordered by gallery then by id, so the caller can
        group in one pass and the oldest member of each group is the first it
        sees. Read for the 一键去重 pass (R59); the ranking (packed, pages,
        oldest) is decided in `app.downloads.dedup`, not in SQL, because the
        three keys are a policy the operator stated in words.
        """
        return await asyncio.to_thread(self._duplicate_gallery_groups_sync)

    def _duplicate_gallery_groups_sync(self, connection=None) -> list[dict]:
        if connection is None:
            with self.connection() as connection:
                return self._duplicate_gallery_groups_sync(connection)
        rows = connection.execute(
                "SELECT c.id, c.ex_gid, c.status, "
                + title_subquery("c.id")
                + ", cbz.id IS NOT NULL, cbz.page_count, v.work_id, v.revision "
                "FROM candidates c "
                "JOIN work_versions v ON v.candidate_id = c.id "
                "LEFT JOIN download_jobs pack "
                "  ON pack.idempotency_key = 'convert:' || c.id "
                "LEFT JOIN artifacts cbz "
                "  ON cbz.job_id = pack.id AND cbz.artifact_type = 'CBZ' "
                "WHERE c.ex_gid IS NOT NULL "
                "AND EXISTS (SELECT 1 FROM candidates other "
                "  WHERE other.ex_gid = c.ex_gid AND other.id != c.id) "
                "ORDER BY c.ex_gid, c.id"
            ).fetchall()
        return [
            {
                "candidate_id": int(row[0]),
                "ex_gid": int(row[1]),
                "status": str(row[2]),
                "title": str(row[3]) if row[3] is not None else None,
                "packaged": bool(row[4]),
                "page_count": int(row[5]) if row[5] is not None else None,
                "work_id": row[6],
                "expected_revision": row[7],
            }
            for row in rows
        ]

    async def pending_candidate_ids(
        self,
        limit: int = 100,
        *,
        oldest_first: bool = False,
        require_gallery: bool = False,
        after_id: int | None = None,
    ) -> tuple[int, ...]:
        """Candidates awaiting review, newest first unless asked otherwise.

        `oldest_first` exists for `AutoApprovalSweeper` and is not a preference.
        Newest-first plus a `LIMIT` is a window, not a cursor: with more pending
        candidates than the batch size, every sweep re-read the same newest page
        and a candidate that fell out of it was never evaluated again -- a rule
        would simply never see the oldest book in a backlog. A trial run wants
        the opposite order, because what an operator is checking is the rule
        against what just arrived, so the caller says which it needs.

        `after_id` is a bounded round-robin cursor for the sweeper. When it is
        supplied, rows after that id are returned first and the query wraps to
        the oldest eligible id. Trial runs leave it unset and therefore keep
        their newest-first behaviour.

        `require_gallery` is the same kind of necessity for the sweeper (R59):
        automatic rules never decide a candidate without a gallery id, so an
        unfiltered batch would let a backlog of them occupy the whole window
        and starve every candidate behind it.
        """
        return await asyncio.to_thread(
            self._pending_candidate_ids_sync, limit, oldest_first, require_gallery,
            after_id,
        )

    def _pending_candidate_ids_sync(
        self,
        limit: int,
        oldest_first: bool = False,
        require_gallery: bool = False,
        after_id: int | None = None,
    ) -> tuple[int, ...]:
        with self.connection() as connection:
            if after_id is not None and oldest_first:
                order = "ORDER BY (id <= ?) ASC, id ASC"
                params = (after_id, limit)
            else:
                order = "ORDER BY id " + ("ASC" if oldest_first else "DESC")
                params = (limit,)
            rows = connection.execute(
                "SELECT id FROM candidates WHERE status = 'PENDING_REVIEW' "
                + ("AND ex_gid IS NOT NULL " if require_gallery else "")
                + order
                + " LIMIT ?",
                params,
            ).fetchall()
        return tuple(int(row[0]) for row in rows)

    @staticmethod
    def _auto_approval_rule_from_row(row: sqlite3.Row | tuple) -> AutoApprovalRule:
        condition = json.loads(str(row[5]))
        if not isinstance(condition, dict):
            raise ValueError("automatic approval rule condition must be an object")
        return AutoApprovalRule(
            rule_id=int(row[0]),
            name=str(row[1]),
            enabled=bool(row[2]),
            priority=int(row[3]),
            version=int(row[4]),
            condition=condition,
            dsl_snapshot=str(row[6]),
            created_at=str(row[7]),
            updated_at=str(row[8]),
            case_sensitive=bool(row[9]),
            action=str(row[10]),
            rule_uuid=str(row[11]),
        )

    async def list_auto_approval_rules(
        self, *, enabled_only: bool = False
    ) -> tuple[AutoApprovalRule, ...]:
        return await asyncio.to_thread(
            self._list_auto_approval_rules_sync, enabled_only
        )

    def _list_auto_approval_rules_sync(
        self, enabled_only: bool, connection=None
    ) -> tuple[AutoApprovalRule, ...]:
        where_sql = "WHERE enabled = 1 " if enabled_only else ""
        with nullcontext(connection) if connection is not None else self.connection() as connection:
            rows = connection.execute(
                "SELECT id, name, enabled, priority, version, condition_json, "
                "dsl_snapshot, created_at, updated_at, case_sensitive, action, rule_uuid "
                "FROM auto_approval_rules "
                + where_sql
                + "ORDER BY priority, id"
            ).fetchall()
        return tuple(self._auto_approval_rule_from_row(row) for row in rows)

    async def get_auto_approval_rule(
        self, rule_id: int
    ) -> AutoApprovalRule | None:
        return await asyncio.to_thread(self._get_auto_approval_rule_sync, rule_id)

    def _get_auto_approval_rule_sync(
        self, rule_id: int, connection=None
    ) -> AutoApprovalRule | None:
        with nullcontext(connection) if connection is not None else self.connection() as connection:
            row = connection.execute(
                "SELECT id, name, enabled, priority, version, condition_json, "
                "dsl_snapshot, created_at, updated_at, case_sensitive, action, rule_uuid "
                "FROM auto_approval_rules "
                "WHERE id = ?",
                (rule_id,),
            ).fetchone()
        return self._auto_approval_rule_from_row(row) if row is not None else None

    async def save_auto_approval_rule(
        self,
        *,
        rule_id: int | None,
        name: str,
        enabled: bool,
        priority: int,
        condition: dict,
        dsl_snapshot: str,
        case_sensitive: bool = False,
        action: str = RULE_ACTION_APPROVE,
    ) -> AutoApprovalRule:
        return await asyncio.to_thread(
            self._save_auto_approval_rule_sync,
            rule_id,
            name,
            enabled,
            priority,
            condition,
            dsl_snapshot,
            case_sensitive,
            action,
        )

    def _save_auto_approval_rule_sync(
        self,
        rule_id: int | None,
        name: str,
        enabled: bool,
        priority: int,
        condition: dict,
        dsl_snapshot: str,
        case_sensitive: bool = False,
        action: str = RULE_ACTION_APPROVE,
        connection=None,
    ) -> AutoApprovalRule:
        with nullcontext(connection) if connection is not None else self.connection() as connection:
            if rule_id is None:
                cursor = connection.execute(
                    "INSERT INTO auto_approval_rules "
                    "(name, enabled, priority, condition_json, dsl_snapshot, "
                    "case_sensitive, action) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (
                        name,
                        int(enabled),
                        priority,
                        json.dumps(condition, ensure_ascii=False, separators=(",", ":")),
                        dsl_snapshot,
                        int(case_sensitive),
                        action,
                    ),
                )
                rule_id = int(cursor.lastrowid)
            else:
                cursor = connection.execute(
                    "UPDATE auto_approval_rules SET name = ?, enabled = ?, "
                    "priority = ?, condition_json = ?, dsl_snapshot = ?, "
                    "case_sensitive = ?, action = ?, "
                    "version = version + 1, updated_at = CURRENT_TIMESTAMP "
                    "WHERE id = ?",
                    (
                        name,
                        int(enabled),
                        priority,
                        json.dumps(condition, ensure_ascii=False, separators=(",", ":")),
                        dsl_snapshot,
                        int(case_sensitive),
                        action,
                        rule_id,
                    ),
                )
                if cursor.rowcount != 1:
                    raise LookupError(f"Automatic approval rule {rule_id} does not exist")
            result = self._get_auto_approval_rule_sync(rule_id, connection=connection)
        if result is None:
            raise LookupError(f"Automatic approval rule {rule_id} does not exist")
        return result

    async def set_auto_approval_rule_enabled(
        self, rule_id: int, enabled: bool
    ) -> None:
        await asyncio.to_thread(
            self._set_auto_approval_rule_enabled_sync, rule_id, enabled
        )

    def _set_auto_approval_rule_enabled_sync(
        self, rule_id: int, enabled: bool
    ) -> None:
        with self.connection() as connection:
            cursor = connection.execute(
                "UPDATE auto_approval_rules SET enabled = ?, "
                "updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                (int(enabled), rule_id),
            )
            if cursor.rowcount != 1:
                raise LookupError(f"Automatic approval rule {rule_id} does not exist")

    async def delete_auto_approval_rule(self, rule_id: int) -> None:
        await asyncio.to_thread(self._delete_auto_approval_rule_sync, rule_id)

    def _delete_auto_approval_rule_sync(self, rule_id: int) -> None:
        """Remove a rule outright.

        A hard delete rather than a disabled flag: `enabled` already expresses
        「暂时不要跑这条」, so a second, permanent kind of
        disabled would leave the list showing rules an operator believes they
        removed. What the rule *did* survives regardless -- `record_review_action`
        stores the whole rule snapshot on every automatic approval, so a
        deleted rule's past decisions remain auditable without the row.

        `LookupError` rather than a silent no-op, for the reason
        `_save_auto_approval_rule_sync` raises it: a delete that hits nothing is
        a stale page acting on a rule someone else already removed, and the
        caller has to be able to say so.
        """
        with self.connection() as connection:
            cursor = connection.execute(
                "DELETE FROM auto_approval_rules WHERE id = ?", (rule_id,)
            )
            if cursor.rowcount != 1:
                raise LookupError(
                    f"Automatic approval rule {rule_id} does not exist"
                )

    @staticmethod
    def _archive_path_rule_from_row(
        row: sqlite3.Row | tuple,
    ) -> ArchivePathRule:
        condition = json.loads(str(row[5]))
        if not isinstance(condition, dict):
            raise ValueError("archive path rule condition must be an object")
        return ArchivePathRule(
            rule_id=int(row[0]),
            name=str(row[1]),
            enabled=bool(row[2]),
            priority=int(row[3]),
            version=int(row[4]),
            condition=condition,
            dsl_snapshot=str(row[6]),
            path_template=str(row[7]),
            created_at=str(row[8]),
            updated_at=str(row[9]),
            case_sensitive=bool(row[10]),
            rule_uuid=str(row[11]),
        )

    async def list_archive_path_rules(
        self, *, enabled_only: bool = False
    ) -> tuple[ArchivePathRule, ...]:
        return await asyncio.to_thread(
            self._list_archive_path_rules_sync, enabled_only
        )

    def _list_archive_path_rules_sync(
        self, enabled_only: bool
    ) -> tuple[ArchivePathRule, ...]:
        where_sql = "WHERE enabled = 1 " if enabled_only else ""
        with self.connection() as connection:
            rows = connection.execute(
                "SELECT id, name, enabled, priority, version, condition_json, "
                "dsl_snapshot, path_template, created_at, updated_at, "
                "case_sensitive, rule_uuid FROM archive_path_rules "
                + where_sql
                + "ORDER BY priority, id"
            ).fetchall()
        return tuple(
            self._archive_path_rule_from_row(row) for row in rows
        )

    async def get_archive_path_rule(
        self, rule_id: int
    ) -> ArchivePathRule | None:
        return await asyncio.to_thread(
            self._get_archive_path_rule_sync, rule_id
        )

    def _get_archive_path_rule_sync(
        self, rule_id: int, connection=None
    ) -> ArchivePathRule | None:
        with nullcontext(connection) if connection is not None else self.connection() as connection:
            row = connection.execute(
                "SELECT id, name, enabled, priority, version, condition_json, "
                "dsl_snapshot, path_template, created_at, updated_at, "
                "case_sensitive, rule_uuid FROM archive_path_rules WHERE id = ?",
                (rule_id,),
            ).fetchone()
        return (
            self._archive_path_rule_from_row(row)
            if row is not None
            else None
        )

    async def save_archive_path_rule(
        self,
        *,
        rule_id: int | None,
        name: str,
        enabled: bool,
        priority: int,
        condition: dict,
        dsl_snapshot: str,
        path_template: str,
        case_sensitive: bool = False,
    ) -> ArchivePathRule:
        return await asyncio.to_thread(
            self._save_archive_path_rule_sync,
            rule_id,
            name,
            enabled,
            priority,
            condition,
            dsl_snapshot,
            path_template,
            case_sensitive,
        )

    def _save_archive_path_rule_sync(
        self,
        rule_id: int | None,
        name: str,
        enabled: bool,
        priority: int,
        condition: dict,
        dsl_snapshot: str,
        path_template: str,
        case_sensitive: bool = False,
        connection=None,
    ) -> ArchivePathRule:
        with nullcontext(connection) if connection is not None else self.connection() as connection:
            if rule_id is None:
                cursor = connection.execute(
                    "INSERT INTO archive_path_rules "
                    "(name, enabled, priority, condition_json, dsl_snapshot, "
                    "path_template, case_sensitive) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (
                        name,
                        int(enabled),
                        priority,
                        json.dumps(
                            condition, ensure_ascii=False, separators=(",", ":")
                        ),
                        dsl_snapshot,
                        path_template,
                        int(case_sensitive),
                    ),
                )
                rule_id = int(cursor.lastrowid)
            else:
                cursor = connection.execute(
                    "UPDATE archive_path_rules SET name = ?, enabled = ?, "
                    "priority = ?, condition_json = ?, dsl_snapshot = ?, "
                    "path_template = ?, case_sensitive = ?, "
                    "version = version + 1, updated_at = CURRENT_TIMESTAMP "
                    "WHERE id = ?",
                    (
                        name,
                        int(enabled),
                        priority,
                        json.dumps(
                            condition, ensure_ascii=False, separators=(",", ":")
                        ),
                        dsl_snapshot,
                        path_template,
                        int(case_sensitive),
                        rule_id,
                    ),
                )
                if cursor.rowcount != 1:
                    raise LookupError(
                        f"Archive path rule {rule_id} does not exist"
                    )
            result = self._get_archive_path_rule_sync(rule_id, connection=connection)
        if result is None:
            raise LookupError(f"Archive path rule {rule_id} does not exist")
        return result

    async def set_archive_path_rule_enabled(
        self, rule_id: int, enabled: bool
    ) -> None:
        await asyncio.to_thread(
            self._set_archive_path_rule_enabled_sync, rule_id, enabled
        )

    def _set_archive_path_rule_enabled_sync(
        self, rule_id: int, enabled: bool
    ) -> None:
        with self.connection() as connection:
            cursor = connection.execute(
                "UPDATE archive_path_rules SET enabled = ?, "
                "updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                (int(enabled), rule_id),
            )
            if cursor.rowcount != 1:
                raise LookupError(
                    f"Archive path rule {rule_id} does not exist"
                )

    async def delete_archive_path_rule(self, rule_id: int) -> None:
        await asyncio.to_thread(self._delete_archive_path_rule_sync, rule_id)

    def _delete_archive_path_rule_sync(self, rule_id: int) -> None:
        """Remove a routing rule outright.

        A hard delete rather than a disabled flag, for the same reason the
        auto-approval one is: `enabled` already means 「暂时不要跑这条」, and a
        second, permanent kind of disabled would leave the list showing rules
        an operator believes they removed. A rule produces no audit trail of
        its own -- the pack just computes a path each time -- so deleting it
        cannot rewrite anything.

        `LookupError` rather than a silent no-op: a delete that hits nothing is
        a stale page acting on a rule someone else already removed.
        """
        with self.connection() as connection:
            cursor = connection.execute(
                "DELETE FROM archive_path_rules WHERE id = ?", (rule_id,)
            )
            if cursor.rowcount != 1:
                raise LookupError(
                    f"Archive path rule {rule_id} does not exist"
                )

    async def record_review_action(
        self, candidate_id: int, action: str, operator_name: str, details: dict
    ) -> None:
        await asyncio.to_thread(
            self._record_review_action_sync,
            candidate_id,
            action,
            operator_name,
            details,
        )

    def _record_review_action_sync(
        self, candidate_id: int, action: str, operator_name: str, details: dict
    ) -> None:
        with self.connection() as connection:
            connection.execute(
                "INSERT INTO review_actions "
                "(candidate_id, action, operator_name, details_json) "
                "VALUES (?, ?, ?, ?)",
                (
                    candidate_id,
                    action,
                    operator_name,
                    json.dumps(details, ensure_ascii=False, separators=(",", ":")),
                ),
            )
    async def list_metadata(
        self, candidate_id: int
    ) -> tuple[MetadataEntry, ...]:
        return await asyncio.to_thread(self._list_metadata_sync, candidate_id)


    def list_metadata_sync(
        self, candidate_id: int
    ) -> tuple[MetadataEntry, ...]:
        return self._list_metadata_sync(candidate_id)

    async def re_evaluate_candidate_metadata_rules(
        self, candidate_id: int
    ) -> None:
        await asyncio.to_thread(
            self._re_evaluate_candidate_metadata_rules_sync,
            candidate_id,
        )

    def _re_evaluate_candidate_metadata_rules_sync(
        self, candidate_id: int, connection=None
    ) -> None:
        with nullcontext(connection) if connection is not None else self.connection() as connection:
            source_row = connection.execute(
                "SELECT ts.id, ts.source_type, ts.chat_id, ts.display_name, "
                "ts.enabled, ts.rules_json FROM telegram_sources ts "
                "JOIN source_messages sm ON sm.rule_source_id = ts.id "
                "JOIN candidate_messages cm ON cm.source_message_id = sm.id "
                "WHERE cm.candidate_id = ? "
                "AND sm.message_state = 'ACTIVE' "
                "ORDER BY sm.id LIMIT 1",
                (candidate_id,),
            ).fetchone()
            if source_row is None:
                return
            source = self._source_from_row(source_row)
            current_status_row = connection.execute(
                "SELECT status FROM candidates WHERE id = ?",
                (candidate_id,),
            ).fetchone()
            if current_status_row is None:
                return
            current_status = str(current_status_row[0])
            if current_status in {"APPROVED", "PROCESSING", "FAILED"}:
                return
            metadata_rows = connection.execute(
                "SELECT field_name, field_value FROM metadata_values "
                "WHERE candidate_id = ? "
                "ORDER BY is_manual DESC, confidence DESC, created_at",
                (candidate_id,),
            ).fetchall()
            metadata: dict[str, str] = {}
            for field_name, field_value in metadata_rows:
                field_key = str(field_name)
                if field_key in metadata:
                    continue
                metadata[field_key] = str(field_value)
            decision = evaluate_metadata_rules(source, metadata)
            if decision.result == "IGNORE":
                new_status = "REJECTED"
                new_filter_result = "IGNORE"
                new_filter_reason = decision.reason
            elif Database._candidate_title_missing(connection, candidate_id):
                # The other half of 「拿不到标题就待补充」 (R62): a candidate
                # whose gallery answered without a name -- or has not been read
                # yet -- waits here even though every metadata rule passed.
                # Reading the gallery is what moves it on, which is what makes
                # a failed scrape visible instead of silently reviewable.
                new_status = "NEEDS_INFO"
                new_filter_result = "NEEDS_INFO"
                new_filter_reason = MISSING_TITLE_REASON
            elif decision.result == "ACCEPT":
                if current_status in {"REJECTED", "NEEDS_INFO"}:
                    new_status = "PENDING_REVIEW"
                    new_filter_result = "ACCEPT"
                    new_filter_reason = decision.reason
                else:
                    return
            else:
                new_status = "NEEDS_INFO"
                new_filter_result = "NEEDS_INFO"
                new_filter_reason = decision.reason
            connection.execute(
                "UPDATE candidates SET status = ?, filter_result = ?, "
                "filter_reason = ?, updated_at = CURRENT_TIMESTAMP "
                "WHERE id = ?",
                (
                    new_status,
                    new_filter_result,
                    new_filter_reason,
                    candidate_id,
                ),
            )
            connection.execute(
                "INSERT INTO review_actions "
                "(candidate_id, action, operator_name, details_json) "
                "VALUES (?, 'METADATA_RULE', 'system', ?)",
                (
                    candidate_id,
                    json.dumps(
                        {
                            "result": decision.result,
                            "reason": decision.reason,
                            "source_id": source.source_id,
                        },
                        separators=(",", ":"),
                    ),
                ),
            )


    def _list_metadata_sync(self, candidate_id: int) -> tuple[MetadataEntry, ...]:
        with self.connection() as connection:
            rows = connection.execute(
                "SELECT field_name, field_value, value_source, confidence, "
                "is_manual, created_at, is_locked FROM metadata_values "
                "WHERE candidate_id = ? ORDER BY is_manual DESC, field_name",
                (candidate_id,),
            ).fetchall()
        return tuple(
            MetadataEntry(
                field_name=str(row[0]),
                field_value=str(row[1]),
                value_source=str(row[2]),
                confidence=float(row[3]) if row[3] is not None else None,
                is_manual=bool(row[4]),
                created_at=str(row[5]),
                is_locked=bool(row[6]),
            )
            for row in rows
        )


    async def list_review_actions(
        self, candidate_id: int
    ) -> tuple[ReviewActionEntry, ...]:
        return await asyncio.to_thread(
            self._list_review_actions_sync, candidate_id
        )


    def list_review_actions_sync(
        self, candidate_id: int
    ) -> tuple[ReviewActionEntry, ...]:
        return self._list_review_actions_sync(candidate_id)


    def _list_review_actions_sync(
        self, candidate_id: int
    ) -> tuple[ReviewActionEntry, ...]:
        with self.connection() as connection:
            rows = connection.execute(
                "SELECT action, operator_name, details_json, created_at "
                "FROM review_actions WHERE candidate_id = ? "
                "ORDER BY id DESC",
                (candidate_id,),
            ).fetchall()
        return tuple(
            ReviewActionEntry(
                action=str(row[0]),
                operator_name=str(row[1]),
                details=self._safe_json(row[2]),
                created_at=str(row[3]),
            )
            for row in rows
        )


    async def list_archive_tool_profiles(
        self, *, enabled_only: bool = False
    ) -> tuple[ToolProfile, ...]:
        return await asyncio.to_thread(
            self._list_archive_tool_profiles_sync, enabled_only
        )


    def _list_archive_tool_profiles_sync(
        self, enabled_only: bool
    ) -> tuple[ToolProfile, ...]:
        where_sql = "WHERE enabled = 1 " if enabled_only else ""
        with self.connection() as connection:
            rows = connection.execute(
                "SELECT id, name, backend, kind, executable_path, "
                "supported_formats, timeout_seconds, capabilities, enabled, profile_uuid, revision "
                "FROM archive_tool_profiles " + where_sql + "ORDER BY id"
            ).fetchall()
        return tuple(self._tool_profile_from_row(row) for row in rows)


    @staticmethod
    def _tool_profile_from_row(row) -> ToolProfile:
        return ToolProfile(
            profile_id=int(row[0]),
            name=str(row[1]),
            backend=str(row[2]),
            kind=str(row[3]),
            executable_path=str(row[4]) if row[4] is not None else None,
            supported_formats=tuple(json.loads(str(row[5]))),
            timeout_seconds=int(row[6]),
            capabilities=tuple(json.loads(str(row[7]))),
            enabled=bool(row[8]),
            profile_uuid=str(row[9]),
            revision=int(row[10]),
        )


    async def get_archive_tool_profile(self, name: str) -> ToolProfile | None:
        return await asyncio.to_thread(self._get_archive_tool_profile_sync, name)


    def _get_archive_tool_profile_sync(self, name: str) -> ToolProfile | None:
        with self.connection() as connection:
            row = connection.execute(
                "SELECT id, name, backend, kind, executable_path, "
                "supported_formats, timeout_seconds, capabilities, enabled, profile_uuid, revision "
                "FROM archive_tool_profiles WHERE name = ?",
                (name,),
            ).fetchone()
        return self._tool_profile_from_row(row) if row is not None else None


    async def set_archive_tool_profile_state(
        self,
        name: str,
        *,
        enabled: bool | None = None,
        executable_path: str | None = None,
        timeout_seconds: int | None = None,
    ) -> None:
        await asyncio.to_thread(
            self._set_archive_tool_profile_state_sync,
            name,
            enabled,
            executable_path,
            timeout_seconds,
        )


    def _set_archive_tool_profile_state_sync(
        self,
        name: str,
        enabled: bool | None,
        executable_path: str | None,
        timeout_seconds: int | None,
        *,
        connection=None,
    ) -> None:
        assignments: list[str] = []
        params: list[object] = []
        if enabled is not None:
            assignments.append("enabled = ?")
            params.append(int(enabled))
        if executable_path is not None:
            assignments.append("executable_path = ?")
            params.append(executable_path)
        if timeout_seconds is not None:
            if timeout_seconds <= 0:
                raise ValueError("timeout_seconds must be positive")
            assignments.append("timeout_seconds = ?")
            params.append(int(timeout_seconds))
        if not assignments:
            return
        assignments.append("updated_at = CURRENT_TIMESTAMP")
        params.append(name)
        with self.connection() if connection is None else nullcontext(connection) as connection:
            cursor = connection.execute(
                "UPDATE archive_tool_profiles SET "
                + ", ".join(assignments)
                + " WHERE name = ?",
                tuple(params),
            )
            if cursor.rowcount != 1:
                raise LookupError(f"Archive tool profile {name!r} does not exist")


    async def list_archive_passwords(
        self, *, enabled_only: bool = False
    ) -> tuple[ArchivePasswordEntry, ...]:
        return await asyncio.to_thread(
            self._list_archive_passwords_sync, enabled_only
        )


    def _list_archive_passwords_sync(
        self, enabled_only: bool
    ) -> tuple[ArchivePasswordEntry, ...]:
        where_sql = "WHERE enabled = 1 " if enabled_only else ""
        with self.connection() as connection:
            rows = connection.execute(
                "SELECT id, name, priority, enabled, last_success_at, created_at, password_uuid, revision "
                "FROM archive_passwords "
                + where_sql
                + "ORDER BY last_success_at IS NULL, last_success_at DESC, "
                "priority, id"
            ).fetchall()
        return tuple(
            ArchivePasswordEntry(
                password_id=int(row[0]),
                name=str(row[1]),
                priority=int(row[2]),
                enabled=bool(row[3]),
                last_success_at=str(row[4]) if row[4] is not None else None,
                created_at=str(row[5]),
                password_uuid=str(row[6]),
                revision=int(row[7]),
            )
            for row in rows
        )


    async def list_archive_password_secrets(self) -> tuple[tuple[int, str, str, int], ...]:
        """Read ciphertext and its configuration identity in the same snapshot."""
        return await asyncio.to_thread(self._list_archive_password_secrets_sync)


    def _list_archive_password_secrets_sync(self) -> tuple[tuple[int, str, str, int], ...]:
        with self.connection() as connection:
            rows = connection.execute(
                "SELECT id, secret_json, password_uuid, revision FROM archive_passwords "
                "WHERE enabled = 1 "
                "ORDER BY last_success_at IS NULL, last_success_at DESC, "
                "priority, id"
            ).fetchall()
        return tuple((int(row[0]), str(row[1]), str(row[2]), int(row[3])) for row in rows)


    async def save_archive_password(
        self, *, name: str, secret_json: str, priority: int, enabled: bool
    ) -> int:
        return await asyncio.to_thread(
            self._save_archive_password_sync, name, secret_json, priority, enabled
        )


    def _save_archive_password_sync(
        self, name: str, secret_json: str, priority: int, enabled: bool, *, connection=None
    ) -> int:
        with self.connection() if connection is None else nullcontext(connection) as connection:
            connection.execute(
                "INSERT INTO archive_passwords "
                "(name, secret_json, priority, enabled) VALUES (?, ?, ?, ?) "
                "ON CONFLICT(name) DO UPDATE SET secret_json = excluded.secret_json, "
                "priority = excluded.priority, enabled = excluded.enabled, "
                "updated_at = CURRENT_TIMESTAMP",
                (name, secret_json, int(priority), int(enabled)),
            )
            row = connection.execute(
                "SELECT id FROM archive_passwords WHERE name = ?", (name,),
            ).fetchone()
            if row:
                return int(row[0])
        raise LookupError(f"Archive password {name!r} could not be saved")


    async def delete_archive_password(self, password_id: int) -> None:
        await asyncio.to_thread(self._delete_archive_password_sync, password_id)


    def _delete_archive_password_sync(self, password_id: int, *, connection=None) -> None:
        with self.connection() if connection is None else nullcontext(connection) as connection:
            connection.execute(
                "DELETE FROM archive_passwords WHERE id = ?", (password_id,)
            )


    async def mark_archive_password_success(self, password_id: int, *, password_uuid: str, expected_revision: int) -> None:
        await asyncio.to_thread(
            self._mark_archive_password_success_sync, password_id, password_uuid, expected_revision
        )


    def _mark_archive_password_success_sync(self, password_id: int, password_uuid: str, expected_revision: int) -> None:
        with self.connection() as connection:
            connection.execute(
                "UPDATE archive_passwords SET last_success_at = CURRENT_TIMESTAMP, "
                "updated_at = CURRENT_TIMESTAMP WHERE id = ? AND password_uuid = ? AND revision = ? AND enabled = 1",
                (password_id, password_uuid, expected_revision),
            )


    async def archive_settings(self) -> dict[str, str]:
        return await asyncio.to_thread(self._archive_settings_sync)


    def _archive_settings_sync(self) -> dict[str, str]:
        with self.connection() as connection:
            rows = connection.execute(
                "SELECT key, value FROM archive_settings"
            ).fetchall()
        return {str(row[0]): str(row[1]) for row in rows}


    async def save_archive_settings(self, values: dict[str, str]) -> None:
        await asyncio.to_thread(self._save_archive_settings_sync, values)


    def _save_archive_settings_sync(self, values: dict[str, str]) -> None:
        with self.connection() as connection:
            for key, value in values.items():
                connection.execute(
                    "INSERT INTO archive_settings (key, value) VALUES (?, ?) "
                    "ON CONFLICT(key) DO UPDATE SET value = excluded.value, "
                    "updated_at = CURRENT_TIMESTAMP",
                    (str(key), str(value)),
                )


    async def system_settings(self) -> dict[str, str]:
        return await asyncio.to_thread(self._system_settings_sync)


    def _system_settings_sync(self) -> dict[str, str]:
        with self.connection() as connection:
            rows = connection.execute(
                "SELECT key, value FROM system_settings"
            ).fetchall()
        return {str(row[0]): str(row[1]) for row in rows}


    async def save_system_settings(self, values: dict[str, str]) -> None:
        await asyncio.to_thread(self._save_system_settings_sync, values)


    def _save_system_settings_sync(self, values: dict[str, str]) -> None:
        with self.connection() as connection:
            for key, value in values.items():
                connection.execute(
                    "INSERT INTO system_settings (key, value) VALUES (?, ?) "
                    "ON CONFLICT(key) DO UPDATE SET value = excluded.value, "
                    "updated_at = CURRENT_TIMESTAMP",
                    (str(key), str(value)),
                )


    # -------------------------------------------------------------- AI providers

    async def list_ai_providers(
        self, *, enabled_only: bool = False
    ) -> tuple[AiProvider, ...]:
        return await asyncio.to_thread(self._list_ai_providers_sync, enabled_only)

    def _list_ai_providers_sync(
        self, enabled_only: bool
    ) -> tuple[AiProvider, ...]:
        where_sql = "WHERE enabled = 1 " if enabled_only else ""
        with self.connection() as connection:
            rows = connection.execute(
                f"SELECT {_AI_PROVIDER_COLUMNS} FROM ai_providers "
                + where_sql
                + "ORDER BY name, id"
            ).fetchall()
        return tuple(_ai_provider_from_row(row) for row in rows)

    async def get_ai_provider(self, provider_id: int) -> AiProvider | None:
        return await asyncio.to_thread(self._get_ai_provider_sync, provider_id)

    def _get_ai_provider_sync(self, provider_id: int) -> AiProvider | None:
        with self.connection() as connection:
            row = connection.execute(
                f"SELECT {_AI_PROVIDER_COLUMNS} FROM ai_providers WHERE id = ?",
                (provider_id,),
            ).fetchone()
        return _ai_provider_from_row(row) if row is not None else None

    async def save_ai_provider(
        self,
        *,
        provider_id: int | None,
        name: str,
        code: str,
        base_url: str,
        timeout_seconds: int,
        max_retries: int,
        enabled: bool,
        custom_headers: dict[str, object] | None = None,
        default_params: AiRequestParams | None = None,
    ) -> AiProvider:
        return await asyncio.to_thread(
            self._save_ai_provider_sync,
            provider_id,
            name,
            code,
            base_url,
            timeout_seconds,
            max_retries,
            enabled,
            json.dumps(custom_headers or {}, ensure_ascii=False),
            json.dumps(_params_document(default_params), ensure_ascii=False),
        )

    def _save_ai_provider_sync(
        self,
        provider_id: int | None,
        name: str,
        code: str,
        base_url: str,
        timeout_seconds: int,
        max_retries: int,
        enabled: bool,
        custom_headers: str,
        default_params: str,
        *, connection: sqlite3.Connection | None = None,
    ) -> AiProvider:
        """Insert or update one provider, keyed by id when there is one.

        A duplicate *name* is a `LookupError` rather than a second row: two
        providers an operator cannot tell apart in a dropdown is a
        configuration bug, not a feature.
        """
        with (nullcontext(connection) if connection is not None else self.connection()) as connection:
            try:
                if provider_id is None:
                    cursor = connection.execute(
                        "INSERT INTO ai_providers (name, code, base_url, "
                        "timeout_seconds, max_retries, enabled, "
                        "custom_headers, default_params) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                        (
                            name,
                            code,
                            base_url,
                            timeout_seconds,
                            max_retries,
                            1 if enabled else 0,
                            custom_headers,
                            default_params,
                        ),
                    )
                    provider_id = int(cursor.lastrowid)
                else:
                    cursor = connection.execute(
                        "UPDATE ai_providers SET name = ?, code = ?, "
                        "base_url = ?, timeout_seconds = ?, max_retries = ?, "
                        "enabled = ?, custom_headers = ?, default_params = ?, "
                        "updated_at = CURRENT_TIMESTAMP "
                        "WHERE id = ?",
                        (
                            name,
                            code,
                            base_url,
                            timeout_seconds,
                            max_retries,
                            1 if enabled else 0,
                            custom_headers,
                            default_params,
                            provider_id,
                        ),
                    )
                    if cursor.rowcount != 1:
                        raise LookupError(
                            f"AI provider {provider_id} does not exist"
                        )
            except sqlite3.IntegrityError as exc:
                raise LookupError(f"AI provider name {name!r} is taken") from exc
            row = connection.execute(
                f"SELECT {_AI_PROVIDER_COLUMNS} FROM ai_providers WHERE id = ?",
                (provider_id,),
            ).fetchone()
        return _ai_provider_from_row(row)

    async def delete_ai_provider(self, provider_id: int) -> None:
        await asyncio.to_thread(self._delete_ai_provider_sync, provider_id)

    def _delete_ai_provider_sync(
        self, provider_id: int, *, connection: sqlite3.Connection | None = None,
    ) -> None:
        """Remove a provider, its keys and its models (and their chain slots).

        The children go first, in dependency order. `PRAGMA foreign_keys` is on
        so the cascades would do it, but deleting explicitly keeps the intent
        readable and does not silently change meaning if that pragma is ever
        dropped for a bulk import.
        """
        with (nullcontext(connection) if connection is not None else self.connection()) as connection:
            connection.execute(
                "DELETE FROM ai_model_chain WHERE provider_model_id IN "
                "(SELECT id FROM ai_provider_models WHERE provider_id = ?)",
                (provider_id,),
            )
            connection.execute(
                "DELETE FROM ai_provider_models WHERE provider_id = ?",
                (provider_id,),
            )
            connection.execute(
                "DELETE FROM ai_provider_keys WHERE provider_id = ?",
                (provider_id,),
            )
            cursor = connection.execute(
                "DELETE FROM ai_providers WHERE id = ?", (provider_id,)
            )
            if cursor.rowcount != 1:
                raise LookupError(f"AI provider {provider_id} does not exist")

    async def list_ai_provider_keys(
        self, provider_id: int, *, usable_only: bool = False
    ) -> tuple[AiProviderKey, ...]:
        return await asyncio.to_thread(
            self._list_ai_provider_keys_sync, provider_id, usable_only
        )

    def _list_ai_provider_keys_sync(
        self, provider_id: int, usable_only: bool
    ) -> tuple[AiProviderKey, ...]:
        where_sql = (
            "AND enabled = 1 "
            "AND (cooldown_until IS NULL "
            "     OR cooldown_until <= CURRENT_TIMESTAMP) "
            if usable_only
            else ""
        )
        with self.connection() as connection:
            rows = connection.execute(
                "SELECT id, provider_id, label, enabled, failures, "
                "cooldown_until, last_used_at, created_at, key_uuid, revision, health_revision "
                "FROM ai_provider_keys WHERE provider_id = ? "
                + where_sql
                + "ORDER BY id",
                (provider_id,),
            ).fetchall()
        return tuple(_ai_key_from_row(row) for row in rows)

    async def ai_provider_key_cipher(
        self, key_id: int, *, expected: AiProviderKey | None = None,
        provider: AiProvider | None = None,
    ) -> str | None:
        """The encrypted envelope for one key, or None once it is gone.

        The only read that returns a secret, and it returns ciphertext: the
        caller holds the master key and decrypts for one request. Nothing here
        logs the value.
        """
        return await asyncio.to_thread(self._ai_provider_key_cipher_sync, key_id, expected, provider)

    def _ai_provider_key_cipher_sync(
        self, key_id: int, expected: AiProviderKey | None = None,
        provider: AiProvider | None = None,
    ) -> str | None:
        guard, args = self._ai_key_guard(expected, provider)
        with self.connection() as connection:
            row = connection.execute(
                "SELECT cipher FROM ai_provider_keys WHERE id = ?" + guard,
                (key_id, *args),
            ).fetchone()
        return str(row[0]) if row is not None else None

    @staticmethod
    def _ai_key_guard(
        expected: AiProviderKey | None, provider: AiProvider | None,
    ) -> tuple[str, tuple[object, ...]]:
        """Bind a delayed key read/health write to its original configuration."""
        sql, args = '', ()
        if expected is not None:
            sql += ' AND key_uuid = ? AND revision = ?'
            args += (expected.key_uuid, expected.revision)
        if provider is not None:
            sql += (' AND provider_id IN (SELECT id FROM ai_providers '
                    'WHERE provider_uuid = ? AND revision = ?)')
            args += (provider.provider_uuid, provider.revision)
        return sql, args

    async def add_ai_provider_key(
        self, provider_id: int, *, label: str, cipher: str
    ) -> AiProviderKey:
        rows = await self.add_ai_provider_keys(provider_id, [(label, cipher)])
        return rows[0]

    async def add_ai_provider_keys(
        self, provider_id: int, entries: Sequence[tuple[str, str]]
    ) -> tuple[AiProviderKey, ...]:
        """Insert several keys in one transaction.

        One transaction rather than a loop of single inserts: the page takes a
        whole textarea at once, and half of a paste landing in the database
        because the fifth line failed is a state nobody can reason about.
        """
        return await asyncio.to_thread(
            self._add_ai_provider_keys_sync, provider_id, tuple(entries)
        )

    def _add_ai_provider_keys_sync(
        self, provider_id: int, entries: tuple[tuple[str, str], ...],
        *, connection: sqlite3.Connection | None = None,
    ) -> tuple[AiProviderKey, ...]:
        stored: list[AiProviderKey] = []
        with (nullcontext(connection) if connection is not None else self.connection()) as connection:
            for label, cipher in entries:
                cursor = connection.execute(
                    "INSERT INTO ai_provider_keys (provider_id, label, cipher) "
                    "VALUES (?, ?, ?)",
                    (provider_id, label, cipher),
                )
                row = connection.execute(
                    "SELECT id, provider_id, label, enabled, failures, "
                    "cooldown_until, last_used_at, created_at, key_uuid, revision, health_revision "
                    "FROM ai_provider_keys WHERE id = ?",
                    (int(cursor.lastrowid),),
                ).fetchone()
                stored.append(_ai_key_from_row(row))
        return tuple(stored)

    async def set_ai_provider_key_enabled(
        self, key_id: int, enabled: bool
    ) -> None:
        await asyncio.to_thread(
            self._set_ai_provider_key_enabled_sync, key_id, enabled
        )

    def _set_ai_provider_key_enabled_sync(
        self, key_id: int, enabled: bool, *, connection: sqlite3.Connection | None = None,
    ) -> None:
        with (nullcontext(connection) if connection is not None else self.connection()) as connection:
            cursor = connection.execute(
                "UPDATE ai_provider_keys SET enabled = ? WHERE id = ?",
                (1 if enabled else 0, key_id),
            )
            if cursor.rowcount != 1:
                raise LookupError(f"AI provider key {key_id} does not exist")

    async def delete_ai_provider_key(self, key_id: int) -> None:
        await asyncio.to_thread(self._delete_ai_provider_key_sync, key_id)

    def _delete_ai_provider_key_sync(
        self, key_id: int, *, connection: sqlite3.Connection | None = None,
    ) -> None:
        with (nullcontext(connection) if connection is not None else self.connection()) as connection:
            cursor = connection.execute(
                "DELETE FROM ai_provider_keys WHERE id = ?", (key_id,)
            )
            if cursor.rowcount != 1:
                raise LookupError(f"AI provider key {key_id} does not exist")

    async def record_ai_key_failure(
        self, key_id: int, *, cooldown_minutes: int,
        expected: AiProviderKey | None = None, provider: AiProvider | None = None,
    ) -> None:
        await asyncio.to_thread(
            self._record_ai_key_failure_sync, key_id, cooldown_minutes, expected, provider
        )

    def _record_ai_key_failure_sync(
        self, key_id: int, cooldown_minutes: int,
        expected: AiProviderKey | None = None, provider: AiProvider | None = None,
    ) -> None:
        """Count a refusal and park this key for a while.

        The cooldown is computed by SQLite so every writer shares one clock --
        the process may run in a container whose clock differs from the
        operator's, and the comparison in `usable_only` is against the
        database's own `CURRENT_TIMESTAMP`.
        """
        with self.connection() as connection:
            guard, args = self._ai_key_guard(expected, provider)
            connection.execute(
                "UPDATE ai_provider_keys SET failures = failures + 1, "
                "cooldown_until = datetime('now', ?), "
                "last_used_at = CURRENT_TIMESTAMP WHERE id = ?" + guard,
                (f"+{max(0, int(cooldown_minutes))} minutes", key_id, *args),
            )

    async def reset_ai_key_cooldown(self, key_id: int) -> None:
        await asyncio.to_thread(self._reset_ai_key_cooldown_sync, key_id)

    def _reset_ai_key_cooldown_sync(
        self, key_id: int, *, connection: sqlite3.Connection | None = None,
    ) -> None:
        with (nullcontext(connection) if connection is not None else self.connection()) as connection:
            cursor = connection.execute(
                "UPDATE ai_provider_keys SET cooldown_until = NULL WHERE id = ?",
                (key_id,),
            )
            if cursor.rowcount != 1:
                raise LookupError(f"AI provider key {key_id} does not exist")

    async def mark_ai_key_used(
        self, key_id: int, *, expected: AiProviderKey | None = None,
        provider: AiProvider | None = None,
    ) -> None:
        await asyncio.to_thread(self._mark_ai_key_used_sync, key_id, expected, provider)

    def _mark_ai_key_used_sync(
        self, key_id: int, expected: AiProviderKey | None = None,
        provider: AiProvider | None = None,
    ) -> None:
        with self.connection() as connection:
            guard, args = self._ai_key_guard(expected, provider)
            connection.execute(
                "UPDATE ai_provider_keys SET last_used_at = CURRENT_TIMESTAMP, "
                "cooldown_until = NULL WHERE id = ?" + guard,
                (key_id, *args),
            )

    async def list_ai_provider_models(
        self, provider_id: int
    ) -> tuple[AiProviderModel, ...]:
        return await asyncio.to_thread(
            self._list_ai_provider_models_sync, provider_id
        )

    def _list_ai_provider_models_sync(
        self, provider_id: int
    ) -> tuple[AiProviderModel, ...]:
        with self.connection() as connection:
            rows = connection.execute(
                f"SELECT {_AI_MODEL_COLUMNS} FROM ai_provider_models "
                "WHERE provider_id = ? ORDER BY name, id",
                (provider_id,),
            ).fetchall()
        return tuple(_ai_model_from_row(row) for row in rows)

    async def get_ai_provider_model(
        self, model_id: int
    ) -> AiProviderModel | None:
        return await asyncio.to_thread(
            self._get_ai_provider_model_sync, model_id
        )

    def _get_ai_provider_model_sync(
        self, model_id: int
    ) -> AiProviderModel | None:
        with self.connection() as connection:
            row = connection.execute(
                f"SELECT {_AI_MODEL_COLUMNS} FROM ai_provider_models "
                "WHERE id = ?",
                (model_id,),
            ).fetchone()
        return _ai_model_from_row(row) if row is not None else None

    async def add_ai_provider_model(
        self, provider_id: int, name: str
    ) -> AiProviderModel:
        return await asyncio.to_thread(
            self._add_ai_provider_model_sync, provider_id, name
        )

    def _add_ai_provider_model_sync(
        self, provider_id: int, name: str
    ) -> AiProviderModel:
        """Re-adding a known model name returns the existing row.

        An operator who types a model twice is not making a second model; and
        the verification already on that row must survive, so this is an
        upsert-on-identity rather than a duplicate error.
        """
        with self.connection() as connection:
            connection.execute(
                "INSERT INTO ai_provider_models (provider_id, name) "
                "VALUES (?, ?) ON CONFLICT(provider_id, name) DO NOTHING",
                (provider_id, name),
            )
            row = connection.execute(
                f"SELECT {_AI_MODEL_COLUMNS} FROM ai_provider_models "
                "WHERE provider_id = ? AND name = ?",
                (provider_id, name),
            ).fetchone()
        return _ai_model_from_row(row)

    async def add_ai_provider_models(
        self, provider_id: int, names: Sequence[str]
    ) -> tuple[AiProviderModel, ...]:
        """Register several names at once, keeping any row that already exists.

        The 「拉取模型」 result is a checklist, not a form: ten ticks must be one
        submit, and a name that is already configured must come back as the same
        row (with its verification state) rather than as an error.
        """
        return await asyncio.to_thread(
            self._add_ai_provider_models_sync, provider_id, tuple(names)
        )

    def _add_ai_provider_models_sync(
        self, provider_id: int, names: tuple[str, ...], *, connection: sqlite3.Connection | None = None,
    ) -> tuple[AiProviderModel, ...]:
        stored: list[AiProviderModel] = []
        with (nullcontext(connection) if connection is not None else self.connection()) as connection:
            for name in names:
                connection.execute(
                    "INSERT INTO ai_provider_models (provider_id, name) "
                    "VALUES (?, ?) ON CONFLICT(provider_id, name) DO NOTHING",
                    (provider_id, name),
                )
                row = connection.execute(
                    f"SELECT {_AI_MODEL_COLUMNS} FROM ai_provider_models "
                    "WHERE provider_id = ? AND name = ?",
                    (provider_id, name),
                ).fetchone()
                stored.append(_ai_model_from_row(row))
        return tuple(stored)

    async def set_ai_provider_model_enabled(
        self, model_id: int, enabled: bool
    ) -> None:
        await asyncio.to_thread(
            self._set_ai_provider_model_enabled_sync, model_id, enabled
        )

    async def set_ai_provider_models_enabled(
        self, model_ids: Sequence[int], enabled: bool
    ) -> int:
        """Flip several models in one statement; returns how many changed."""
        return await asyncio.to_thread(
            self._set_ai_provider_models_enabled_sync, tuple(model_ids), enabled
        )

    def _set_ai_provider_models_enabled_sync(
        self, model_ids: tuple[int, ...], enabled: bool
    ) -> int:
        if not model_ids:
            return 0
        placeholders = ", ".join("?" for _ in model_ids)
        with self.connection() as connection:
            cursor = connection.execute(
                f"UPDATE ai_provider_models SET enabled = ? "
                f"WHERE id IN ({placeholders})",
                (1 if enabled else 0, *model_ids),
            )
        return int(cursor.rowcount)

    async def set_ai_provider_model_params(
        self, model_id: int, params: AiRequestParams
    ) -> None:
        await asyncio.to_thread(
            self._set_ai_provider_model_params_sync, model_id, params
        )

    def _set_ai_provider_model_params_sync(
        self, model_id: int, params: AiRequestParams, *, connection: sqlite3.Connection | None = None,
    ) -> None:
        document = json.dumps(_params_document(params), ensure_ascii=False)
        with (nullcontext(connection) if connection is not None else self.connection()) as connection:
            cursor = connection.execute(
                "UPDATE ai_provider_models SET params = ? WHERE id = ?",
                (document, model_id),
            )
            if cursor.rowcount != 1:
                raise LookupError(f"AI provider model {model_id} does not exist")

    def _set_ai_provider_model_enabled_sync(
        self, model_id: int, enabled: bool, *, connection: sqlite3.Connection | None = None,
    ) -> None:
        with (nullcontext(connection) if connection is not None else self.connection()) as connection:
            cursor = connection.execute(
                "UPDATE ai_provider_models SET enabled = ? WHERE id = ?",
                (1 if enabled else 0, model_id),
            )
            if cursor.rowcount != 1:
                raise LookupError(f"AI provider model {model_id} does not exist")

    async def delete_ai_provider_model(self, model_id: int) -> None:
        await asyncio.to_thread(self._delete_ai_provider_model_sync, model_id)

    def _delete_ai_provider_model_sync(
        self, model_id: int, *, connection: sqlite3.Connection | None = None,
    ) -> None:
        """Delete a model and whatever chain slot it occupies.

        Leaving the slot behind would shift nothing (positions are explicit) but
        would leave a hole the next save would have to reason about; deleting
        it makes the chain shorter, which is the honest result of removing a
        model an operator no longer wants tried.
        """
        with (nullcontext(connection) if connection is not None else self.connection()) as connection:
            connection.execute(
                "DELETE FROM ai_model_chain WHERE provider_model_id = ?",
                (model_id,),
            )
            cursor = connection.execute(
                "DELETE FROM ai_provider_models WHERE id = ?", (model_id,)
            )
            if cursor.rowcount != 1:
                raise LookupError(
                    f"AI provider model {model_id} does not exist"
                )

    async def mark_ai_model_verified(
        self, model_id: int, *, ok: bool, error: str | None,
        expected: AiProviderModel | None = None, provider: AiProvider | None = None,
        operation_id: str | None = None,
    ) -> bool:
        return await asyncio.to_thread(
            self._mark_ai_model_verified_sync, model_id, ok, error, expected, provider, operation_id
        )

    def _mark_ai_model_verified_sync(
        self, model_id: int, ok: bool, error: str | None,
        expected: AiProviderModel | None = None, provider: AiProvider | None = None,
        operation_id: str | None = None,
    ) -> bool:
        guard, args = '', ()
        if expected is not None:
            guard += ' AND model_uuid = ? AND revision = ?'
            args += (expected.model_uuid, expected.revision)
        if provider is not None:
            guard += (' AND provider_id IN (SELECT id FROM ai_providers WHERE '
                      'provider_uuid = ? AND revision = ? AND keys_revision = ?)')
            args += (provider.provider_uuid, provider.revision, provider.keys_revision)
        if operation_id is not None:
            guard += (" AND EXISTS (SELECT 1 FROM ai_network_operations o WHERE o.operation_id = ? "
                      "AND o.status = 'RUNNING' AND (o.session_id IS NULL OR EXISTS "
                      "(SELECT 1 FROM worker_sessions s WHERE s.session_id = o.session_id AND s.status = 'LIVE')))")
            args += (operation_id,)
        with self.connection() as connection:
            cursor = connection.execute(
                "UPDATE ai_provider_models SET last_verified_at = "
                "CURRENT_TIMESTAMP, last_verify_ok = ?, last_verify_error = ? "
                "WHERE id = ?" + guard,
                (1 if ok else 0, error, model_id, *args),
            )
            if cursor.rowcount != 1 and expected is None and provider is None:
                raise LookupError(
                    f"AI provider model {model_id} does not exist"
                )
            return cursor.rowcount == 1

    async def list_ai_model_chain(
        self, scope: str = CHAIN_SCOPE_DEFAULT
    ) -> tuple[AiModelChainEntry, ...]:
        return await asyncio.to_thread(self._list_ai_model_chain_sync, scope)

    def _list_ai_model_chain_sync(
        self, scope: str
    ) -> tuple[AiModelChainEntry, ...]:
        with self.connection() as connection:
            rows = connection.execute(
                _AI_CHAIN_SELECT + "WHERE ch.scope = ? ORDER BY ch.position",
                (scope,),
            ).fetchall()
        return tuple(
            AiModelChainEntry(
                position=int(row[0]),
                provider=_ai_provider_from_row(row[1:14]),
                model=_ai_model_from_row(row[14:24]),
                scope=scope,
            )
            for row in rows
        )

    async def save_ai_model_chain(
        self, model_ids: Sequence[int], scope: str = CHAIN_SCOPE_DEFAULT
    ) -> tuple[AiModelChainEntry, ...]:
        return await asyncio.to_thread(
            self._save_ai_model_chain_sync, tuple(model_ids), scope
        )

    def _save_ai_model_chain_sync(
        self, model_ids: tuple[int, ...], scope: str, *, connection: sqlite3.Connection | None = None,
    ) -> tuple[AiModelChainEntry, ...]:
        """Replace the whole chain with `model_ids`, in that order.

        Whole-chain replacement rather than add/remove/reorder verbs: the
        order *is* the configuration (position 0 is the primary), and three
        verbs would each have to renumber the rest anyway. One transaction
        keeps a failed save from leaving the primary missing.
        """
        with (nullcontext(connection) if connection is not None else self.connection()) as connection:
            if model_ids:
                placeholders = ", ".join("?" for _ in model_ids)
                found = {
                    int(row[0])
                    for row in connection.execute(
                        f"SELECT id FROM ai_provider_models "
                        f"WHERE id IN ({placeholders})",
                        model_ids,
                    )
                }
                missing = [mid for mid in model_ids if mid not in found]
                if missing:
                    raise LookupError(
                        f"AI provider model {missing[0]} does not exist"
                    )
            connection.execute(
                "DELETE FROM ai_model_chain WHERE scope = ?", (scope,)
            )
            for position, model_id in enumerate(model_ids):
                connection.execute(
                    "INSERT INTO ai_model_chain "
                    "(scope, position, provider_model_id) VALUES (?, ?, ?)",
                    (scope, position, model_id),
                )
            rows = connection.execute(
                _AI_CHAIN_SELECT + "WHERE ch.scope = ? ORDER BY ch.position",
                (scope,),
            ).fetchall()
        return tuple(
            AiModelChainEntry(
                position=int(row[0]),
                provider=_ai_provider_from_row(row[1:14]),
                model=_ai_model_from_row(row[14:24]),
                scope=scope,
            )
            for row in rows
        )

    @staticmethod
    def _safe_json(value: str | None) -> dict:
        if not value:
            return {}
        try:
            decoded = json.loads(value)
        except (TypeError, ValueError):
            return {}
        return decoded if isinstance(decoded, dict) else {}

    # ------------------------------------------------------------------
    #  AI path suggestions (the answer cache)
    # ------------------------------------------------------------------
    #: One row per candidate; the row is *semantics*, not caching. The model is
    #: not deterministic, so the packer, the detail page and a later re-archive
    #: sweep have to read the same recorded answer -- and 「路径有变动」 is only
    #: a meaningful test against something that was recorded earlier.
    _AI_SUGGESTION_COLUMNS = (
        "candidate_id, fingerprint, prompt_hash, relative_path, directory, "
        "filename, provider_id, model_name, attempts, created_at, updated_at"
    )

    async def get_ai_path_suggestion(
        self, candidate_id: int
    ) -> AiPathSuggestion | None:
        return await asyncio.to_thread(
            self._get_ai_path_suggestion_sync, candidate_id
        )

    def _get_ai_path_suggestion_sync(
        self, candidate_id: int
    ) -> AiPathSuggestion | None:
        with self.connection() as connection:
            row = connection.execute(
                f"SELECT {self._AI_SUGGESTION_COLUMNS} "
                "FROM ai_path_suggestions WHERE candidate_id = ?",
                (candidate_id,),
            ).fetchone()
        return _ai_path_suggestion(row) if row is not None else None

    async def save_ai_path_suggestion(
        self, suggestion: AiPathSuggestion
    ) -> None:
        """Record one answer, replacing whatever this book had before.

        Replaced rather than appended: the table answers 「这本书现在是什么
        答案」 and a history of every prompt revision would make the 需干预 view
        and the predicate both ask 「哪一行」 before they could be answered.
        """
        await asyncio.to_thread(
            self._save_ai_path_suggestion_sync, suggestion
        )

    def _save_ai_path_suggestion_sync(
        self, suggestion: AiPathSuggestion
    ) -> None:
        with self.connection() as connection:
            connection.execute(
                "INSERT INTO ai_path_suggestions "
                "(candidate_id, fingerprint, prompt_hash, relative_path, "
                " directory, filename, provider_id, model_name, attempts) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(candidate_id) DO UPDATE SET "
                "  fingerprint = excluded.fingerprint, "
                "  prompt_hash = excluded.prompt_hash, "
                "  relative_path = excluded.relative_path, "
                "  directory = excluded.directory, "
                "  filename = excluded.filename, "
                "  provider_id = excluded.provider_id, "
                "  model_name = excluded.model_name, "
                "  attempts = excluded.attempts, "
                # `created_at` is left alone on purpose: it marks when this book
                # first got an answer, which is what a 「这本书什么时候被 AI 定
                # 过位」 question reads.
                "  updated_at = CURRENT_TIMESTAMP",
                (
                    suggestion.candidate_id,
                    suggestion.fingerprint,
                    suggestion.prompt_hash,
                    suggestion.relative_path,
                    suggestion.directory,
                    suggestion.filename,
                    suggestion.provider_id,
                    suggestion.model_name,
                    suggestion.attempts,
                ),
            )

    async def clear_ai_path_suggestions(self) -> int:
        """Drop every cached answer and report how many there were.

        The whole-library form, because a fingerprint change invalidates the
        library as a whole: 「改 prompt = 全库需要新答案」 means the only
        coherent cache-clearing granularity is all of it. Returns the count so
        the page can say what it did rather than 「已完成」.
        """
        return await asyncio.to_thread(self._clear_ai_path_suggestions_sync)

    def _clear_ai_path_suggestions_sync(self, connection=None) -> int:
        with self.connection() if connection is None else nullcontext(connection) as connection:
            return connection.execute("DELETE FROM ai_path_suggestions").rowcount

    async def ai_path_suggestion_count(self) -> int:
        return await asyncio.to_thread(self._ai_path_suggestion_count_sync)

    def _ai_path_suggestion_count_sync(self) -> int:
        with self.connection() as connection:
            row = connection.execute(
                "SELECT COUNT(*) FROM ai_path_suggestions"
            ).fetchone()
        return int(row[0]) if row is not None else 0
