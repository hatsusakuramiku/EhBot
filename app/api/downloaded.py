"""The 已下载内容 domain: one snapshot, and the batch actions on a selection.

`downloaded_snapshot` is the single assembler, in the shape `/activity` and
`/candidates` established: the page render and `GET /api/v1/downloaded` call the
same function, so a card and a JSON client can never disagree about what a book
is or what it can still do. A test asserts the page context is a superset of the
JSON body.

The three actions are deliberately *not* folded into `apply_job_batch`. That
coroutine dispatches on a job id through `JOB_ACTIONS`, and every entry in it
is a queue lifecycle call on one row. These act on a *work* -- a candidate whose
download and packaging rows are two halves of one book -- and two of them touch
the filesystem. Sharing the loop would have meant `JOB_ACTIONS` mapping a name
onto something that is not a `DownloadService` method, which is exactly the kind
of table that stops being readable. What *is* shared is the shape of the result:
`applied` / `skipped` with a reason per skip, so a replayed batch is safe here
too.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from fastapi import APIRouter, Request

from app.api import deps
from app.api.contracts import ApiError, PageParams
from app.api.events import EVENT_DOWNLOAD
from app.api.serializers import downloaded_work as serialize_work
from app.api.status import (
    DOWNLOADED_TAB_STATUS,
    downloaded_tab_view,
)
from app.db.database import (
    DOWNLOADED_PACK_FILTERS,
    DOWNLOADED_SORT_ORDERS,
    sort_direction,
)
from app.downloads.dedup import apply_dedup, build_dedup_plan
from app.search.query import PAGE_DOWNLOADED, parse_query
from app.review.batches import ReviewBatches
from app.review.commands import WorkCommandError
from app.review.application import rename_work


router = APIRouter(tags=["downloaded"])


#: Batch ceiling, same value and same reasoning as `MAX_BATCH` in
#: `app.api.actions`: a larger selection is refused rather than truncated,
#: because acting on the first hundred of what an operator selected is worse
#: than telling them to narrow it.
MAX_DOWNLOADED_BATCH = 100

#: Sorts the page offers. The keys are what `list_downloaded_works` whitelists;
#: the words are here because they are interface vocabulary.
DOWNLOADED_SORT_OPTIONS: tuple[tuple[str, str], ...] = (
    ("newest", "最近下载"),
    ("oldest", "最早下载"),
    ("title", "标题"),
    ("largest", "文件最大"),
)

#: The batch actions, mapped to nothing: each is dispatched explicitly below,
#: because the three take different arguments (a delete-files flag, a repack
#: flag, neither) and a table pretending they are uniform would be a lie that
#: costs an argument check.
DOWNLOADED_BATCH_ACTIONS: frozenset[str] = frozenset(
    {"repack", "remove", "redownload"}
)


async def downloaded_snapshot(
    database: Any,
    *,
    tab: str = "all",
    search: str = "",
    sort: str = "newest",
    direction: str | None = None,
    offset: int = 0,
    limit: int = 50,
) -> dict[str, Any]:
    """Everything the 已下载内容 page and its endpoint need, in one read.

    `live` is the server's answer to「还有东西在动吗」, computed from the packing
    states on this page. The client polls only while it is true, which is what
    keeps a library of finished books from waking the process every two seconds.
    """
    works, total = await database.list_downloaded_works(
        search=search,
        pack_filter=tab,
        sort=sort,
        direction=direction,
        offset=offset,
        limit=limit,
    )
    items = [serialize_work(work) for work in works]
    counts = await database.downloaded_work_counts()
    return {
        # The *key* travels as a string, and the words travel beside it. They
        # have to be separate fields: this payload is spread straight into the
        # page context, and anything reading `tab` -- the poller's `data-tab`,
        # the hidden batch field, the module the strip is drawn from -- means
        # the identifier. Sending the resolved view under the same name silently
        # put a dict where every one of those expected a string.
        "tab": tab,
        "tab_view": downloaded_tab_view(tab).to_payload(),
        "search": search,
        # What the search box was read as (R61): the conditions it produced,
        # the free words, and anything that degraded, so a JSON client can draw
        # the same chip bar the page does.
        "search_filters": parse_query(
            search, page=PAGE_DOWNLOADED
        ).to_payload(),
        # The direction a request actually got, default included, so the page's
        # arrow and a JSON client agree about which way the list is sorted.
        "direction": sort_direction(DOWNLOADED_SORT_ORDERS, sort, direction),
        "works": items,
        "total": total,
        "counts": counts,
        # Read off the resolved payload rather than through `is_live`: the pack
        # codes are *derived* vocabulary and deliberately absent from
        # `_REGISTRIES`, so `is_live("packing")` is False and the page would
        # never start polling. The badge the operator sees and the decision to
        # poll therefore come from one object.
        "live": any(item["pack"]["live"] for item in items),
    }


def _int(raw: str | None) -> int | None:
    """Parse a paging parameter, treating nonsense as absent.

    Absent rather than an error because `PageParams.clamp` already defines what
    a missing value means; refusing `?page=abc` would turn a mistyped URL into a
    400 where the operator wants page one.
    """
    if raw is None or not str(raw).strip():
        return None
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


def _work_ids(raw: Any) -> list[int]:
    """Validate a selection of candidate ids.

    Deduplicated while preserving order, for the reason the job batch does it:
    a double-submitted checkbox must not make an action run twice, and the order
    the operator sees is the order the results should read in.
    """
    if not isinstance(raw, list) or not raw:
        raise ApiError("WORK_IDS_REQUIRED", "请至少选择一件作品")
    ids: list[int] = []
    for value in raw:
        try:
            number = int(value)
        except (TypeError, ValueError) as exc:
            raise ApiError(
                "WORK_ID_INVALID", f"无效的作品编号：{value}"
            ) from exc
        if number not in ids:
            ids.append(number)
    if len(ids) > MAX_DOWNLOADED_BATCH:
        raise ApiError(
            "BATCH_TOO_LARGE",
            f"一次最多处理 {MAX_DOWNLOADED_BATCH} 件作品，请缩小选择范围",
            details={"limit": MAX_DOWNLOADED_BATCH},
        )
    return ids


def _translate(exc: Exception) -> Exception:
    """Map a domain refusal onto the JSON envelope.

    Identical in spirit to `app.api.actions._translate`, and kept separate for
    the same reason the two dependency modules are: anything without a
    code/message pair is a real fault and must surface as a 500 rather than be
    disguised as a tidy 400.
    """
    code = getattr(exc, "code", None)
    message = getattr(exc, "public_message", None)
    if code and message:
        if isinstance(exc, WorkCommandError):
            return ApiError(str(code), str(message), status_code=exc.status_code, details=exc.details)
        status = 404 if str(code).endswith("_NOT_FOUND") else 400
        return ApiError(str(code), str(message), status_code=status)
    return exc


async def apply_downloaded_batch(
    archived_service,
    conversion_service,
    action: str,
    candidate_ids: list[int],
    *,
    delete_files: bool = False,
    repack: bool = False,
    operator_name: str = "admin",
    versions: dict | None = None,
    idempotency_key: str | None = None,
    announce: Callable[[int], None] | None = None,
) -> dict[str, Any]:
    """Thin adapter to fixed work outcomes; replay never repeats a finished task."""
    if action not in DOWNLOADED_BATCH_ACTIONS:
        raise ApiError(
            "ACTION_UNKNOWN",
            f"未知的作品动作：{action}",
            details={"allowed": sorted(DOWNLOADED_BATCH_ACTIONS)},
        )

    command = 'remove-files' if action == 'remove' and delete_files else action
    try:
        result = await ReviewBatches(archived_service._database, archived=archived_service,
            conversion=conversion_service).run(command, candidate_ids, versions, idempotency_key,
                                                operator_name, repack=repack)
    except WorkCommandError as exc:
        raise _translate(exc) from exc
    for item in result['applied']:
        if announce is not None:
            announce(item['candidate_id'])
    return {**result, 'action': action,
            'applied': [{'candidate_id': item['candidate_id'], 'result': item} for item in result['applied']],
            'delete_files': bool(delete_files) if action == 'remove' else False}


@router.get("/removals/{removal_id}")
async def api_removal(request: Request, removal_id: str) -> dict[str, Any]:
    """Read durable stop and file cleanup even after candidate/job deletion."""
    await deps.require_session(request)
    try:
        return await deps.archived_work_service(request).get_removal(removal_id)
    except Exception as exc:
        raise _translate(exc) from exc


@router.get("/downloaded")
async def downloaded_list(request: Request) -> dict[str, Any]:
    """The 已下载内容 list, same snapshot the page renders."""
    await deps.require_session(request)
    params = request.query_params
    tab = params.get("tab") or "all"
    if tab not in DOWNLOADED_PACK_FILTERS:
        raise ApiError(
            "TAB_UNKNOWN",
            f"未知的分区：{tab}",
            # The list that was checked, not the tab strip's vocabulary: the two
            # are equal today, and a message that quotes a set the guard does
            # not use is how they stop being equal without anybody noticing.
            details={"allowed": sorted(DOWNLOADED_PACK_FILTERS)},
        )
    page = PageParams.clamp(
        _int(params.get("page")), _int(params.get("page_size"))
    )
    return await downloaded_snapshot(
        deps.database(request),
        tab=tab,
        search=(params.get("search") or "").strip(),
        sort=params.get("sort") or "newest",
        direction=params.get("dir"),
        offset=page.offset,
        limit=page.limit,
    )


@router.post("/downloaded/batch")
async def downloaded_batch(request: Request) -> dict[str, Any]:
    """Run one action against a selection of downloaded works."""
    await deps.require_session(request)
    deps.require_csrf(request)
    payload = await request.json()
    if not isinstance(payload, dict):
        raise ApiError("BODY_INVALID", "请求体必须是 JSON 对象")
    payload = deps.command_payload(request, payload)
    return await apply_downloaded_batch(
        deps.archived_work_service(request),
        deps.conversion_service(request),
        str(payload.get("action") or ""),
        _work_ids(payload.get("candidate_ids")),
        delete_files=bool(payload.get("delete_files")),
        repack=bool(payload.get("repack")),
        operator_name=await deps.operator_name(request),
        versions=payload.get('versions'),
        idempotency_key=payload.get('idempotency_key'),
        announce=lambda candidate_id: request.app.state.event_bus.publish(
            EVENT_DOWNLOAD, candidate_id=candidate_id
        ),
    )


@router.get("/downloaded/dedup")
async def downloaded_dedup_plan(request: Request) -> dict[str, Any]:
    """The duplicate groups and the row each one would keep. No writes.

    The GET is the preview the operator confirms; the POST below is the same
    plan executed. Two verbs rather than a `dry_run` flag for the reason the
    delete endpoints split the same way: a read that cannot write is one that
    prefetchers and reloads cannot turn into a removal.
    """
    await deps.require_session(request)
    return await build_dedup_plan(deps.database(request))


@router.post("/downloaded/dedup")
async def downloaded_dedup(request: Request) -> dict[str, Any]:
    """Run the whole dedup pass: one winner per gallery, the rest removed."""
    await deps.require_session(request)
    deps.require_csrf(request)
    payload = await request.json()
    if not isinstance(payload, dict):
        raise ApiError('BODY_INVALID', '请求体必须是 JSON 对象')
    payload = deps.command_payload(request, payload)
    try:
        return await apply_dedup(
            deps.database(request), deps.archived_work_service(request), deps.download_service(request),
            plan_id=payload.get('plan_id'), idempotency_key=payload.get('idempotency_key'),
            operator_name=await deps.operator_name(request),
            announce=lambda candidate_id: request.app.state.event_bus.publish(EVENT_DOWNLOAD, candidate_id=candidate_id),
        )
    except WorkCommandError as exc:
        raise _translate(exc) from exc


@router.post("/downloaded/{candidate_id}/rename")
async def rename_downloaded(request: Request, candidate_id: int) -> dict[str, Any]:
    """Rename or relocate one work's published CBZ.

    Runs `ArchivedWorkService.rename_work`, the coroutine `/downloaded/{id}/rename`
    posts through, so the JSON layer cannot move a book the page would have
    refused: whether the work has a CBZ, and whether a pack currently holds the
    row, are decided by the service once for both.

    Per-work rather than batch, on the page's reason: a filename is specific to
    one book, and a batch rename would either need a template -- which is what
    the archive layout setting already is -- or give fifty books one name. A
    body without either field is a valid request only in the sense that the
    service then re-derives nothing; in practice the client sends one of the two.

    `repack` chains a packaging job afterwards, as the page's checkbox does, so
    the file on disk is rebuilt at the name that was just chosen rather than
    keeping the old one until the next pack.
    """
    return await _change_downloaded_path(request, candidate_id, strict=False)


@router.post('/works/{candidate_id}/archive-path')
async def set_work_archive_path(request: Request, candidate_id: int) -> dict[str, Any]:
    """Set an exact path before or after packaging, with one atomic command."""
    return await _change_downloaded_path(request, candidate_id, strict=True)


async def _change_downloaded_path(request, candidate_id, *, strict):
    await deps.require_session(request)
    deps.require_csrf(request)
    try:
        payload = await request.json()
    except ValueError:
        # Both parts are optional, so an absent body is an empty request rather
        # than a malformed one.
        payload = {}
    if not isinstance(payload, dict):
        raise ApiError("BODY_INVALID", "请求体必须是 JSON 对象")
    payload = deps.command_payload(request, payload)
    operator = await deps.operator_name(request)
    service = deps.archived_work_service(request)
    try:
        result = await rename_work(deps.database(request), service, deps.conversion_service(request),
                                   candidate_id, payload, operator, strict=strict)
    except Exception as exc:  # noqa: BLE001 - domain refusals carry a message
        # Same rule as the rest of this file's actions: a refusal that knows how
        # to describe itself is reported verbatim; anything else is a genuine
        # fault and surfaces as a 500 instead of being disguised as a tidy 400.
        message = getattr(exc, "public_message", None)
        if message is None:
            raise
        raise _translate(exc) from exc
    request.app.state.event_bus.publish(EVENT_DOWNLOAD, candidate_id=candidate_id)
    return result


__all__ = [
    "DOWNLOADED_BATCH_ACTIONS",
    "DOWNLOADED_SORT_OPTIONS",
    "MAX_DOWNLOADED_BATCH",
    "apply_downloaded_batch",
    "downloaded_snapshot",
    "router",
]
