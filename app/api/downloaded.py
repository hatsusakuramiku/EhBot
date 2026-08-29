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
from app.api.status import DOWNLOADED_TAB_STATUS, downloaded_tab_view
from app.conversion.naming import LibraryPathError
from app.db.database import DOWNLOADED_PACK_FILTERS


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
        offset=offset,
        limit=limit,
    )
    items = [serialize_work(work) for work in works]
    counts = await database.downloaded_work_counts()
    return {
        "tab": downloaded_tab_view(tab).to_payload(),
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
        status = 404 if str(code).endswith("_NOT_FOUND") else 400
        return ApiError(str(code), str(message), status_code=status)
    return exc


async def _refile_for_repack(
    archived_service,
    conversion_service,
    candidate_id: int,
    *,
    operator_name: str,
) -> None:
    """Point one work at the path the current layout template gives it.

    Why a batch repack recomputes at all. The layout template is a setting an
    operator changes, and the books already in the library were filed under
    whatever it said at the time. 「批量打包时优先使用最新路径配置为每个
    作品生成最新的归档路径与文件名」 is the instruction; without it a repack
    reads the pin the *last* pack wrote and the new template never reaches the
    books that predate it.

    Three things it deliberately does not do:

    * **It does not touch a path the operator typed.** `is_manual` on the pin is
      the same guard `is_manual` is over metadata: a template is a default, and a
      default must never overwrite a decision. An operator who named one book by
      hand and then batch-packs fifty keeps that name.
    * **It does not refuse the batch when one path is unusable.** The work is
      left exactly as it was -- old pin, old file, nothing moved -- and the
      packing job is parked in `CONVERSION_WAITING_PATH` carrying the reason, so
      it lands in 需干预 with 「为什么」 attached rather than vanishing from a
      flash message. That is what makes the outcome answerable tomorrow.
    * **It does not move the file itself.** The pack is what publishes to the new
      path, and it lands there because it reads the pin. Moving first would leave
      a book at a path no artifact row names if the pack then failed.
    """
    if await archived_service.has_manual_path_pin(candidate_id):
        return
    metadata = await conversion_service.metadata_for(candidate_id)
    title = conversion_service.title_of(metadata, candidate_id)
    try:
        relative = await conversion_service.planned_library_path(
            candidate_id, title, metadata
        )
    except LibraryPathError as exc:
        # Park the packing task with the reason instead of moving anything. The
        # job has to exist for the reason to be visible, so it is enqueued first
        # and then parked -- `enqueue_for_candidate` is idempotent, and the
        # caller's own enqueue afterwards finds the row already parked.
        await archived_service.park_for_invalid_path(
            candidate_id, exc.code, exc.public_message
        )
        raise
    await archived_service.pin_computed_path(
        candidate_id, relative.as_posix(), operator_name=operator_name
    )


async def apply_downloaded_batch(
    archived_service,
    conversion_service,
    action: str,
    candidate_ids: list[int],
    *,
    delete_files: bool = False,
    repack: bool = False,
    operator_name: str = "admin",
    announce: Callable[[int], None] | None = None,
) -> dict[str, Any]:
    """Run one action against a selection of downloaded works.

    One work at a time, for the reason `apply_review_batch` acts per candidate:
    all-or-nothing over a selection means one already-removed book refuses the
    other forty-nine, and a replay must be able to finish what a half-failed
    first attempt started. Each refusal is reported under `skipped` with the
    reason the service gave.

    A genuine fault -- anything without a code/message pair -- is re-raised
    rather than folded into `skipped`: a broken filesystem must not read as
    「49 已执行，1 跳过」.
    """
    if action not in DOWNLOADED_BATCH_ACTIONS:
        raise ApiError(
            "ACTION_UNKNOWN",
            f"未知的作品动作：{action}",
            details={"allowed": sorted(DOWNLOADED_BATCH_ACTIONS)},
        )

    if action == "repack":
        # Packaging is the conversion service's own entry point, and it is the
        # same one the work detail page's 重新打包 posts to -- there is no second
        # path that could disagree about what re-packing means.
        #
        # Before enqueueing, each work's archive path is recomputed from the
        # *current* layout template and pinned, so a batch started after a
        # template change re-files every selected book instead of leaving the
        # ones packed under the old template where they were. A work whose
        # recomputed path is unusable is left completely alone -- see
        # `_refile_for_repack`.
        async def run(candidate_id: int):
            await _refile_for_repack(
                archived_service,
                conversion_service,
                candidate_id,
                operator_name=operator_name,
            )
            return await conversion_service.enqueue_for_candidate(candidate_id)

    elif action == "remove":

        async def run(candidate_id: int):
            return await archived_service.remove_work(
                candidate_id,
                delete_files=delete_files,
                operator_name=operator_name,
            )

    else:

        async def run(candidate_id: int):
            return await archived_service.redownload_work(
                candidate_id, repack=repack
            )

    applied: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    for candidate_id in candidate_ids:
        try:
            result = await run(candidate_id)
        except Exception as exc:  # noqa: BLE001 - re-raised when unexpected
            translated = _translate(exc)
            if not isinstance(translated, ApiError):
                raise translated from exc
            skipped.append(
                {
                    "candidate_id": candidate_id,
                    "code": translated.code,
                    "message": translated.message,
                }
            )
            continue
        applied.append({"candidate_id": candidate_id, "result": result})
        if announce is not None:
            announce(candidate_id)

    return {
        "action": action,
        "requested": len(candidate_ids),
        "applied": applied,
        "skipped": skipped,
        # Echoed back so a client can tell a records-only removal from one that
        # took the files with it without re-reading its own request.
        "delete_files": bool(delete_files) if action == "remove" else False,
    }


@router.get("/downloaded")
async def downloaded_list(request: Request) -> dict[str, Any]:
    """The 已下载内容 list, same snapshot the page renders."""
    deps.require_session(request)
    params = request.query_params
    tab = params.get("tab") or "all"
    if tab not in DOWNLOADED_PACK_FILTERS:
        raise ApiError(
            "TAB_UNKNOWN",
            f"未知的分区：{tab}",
            details={"allowed": sorted(DOWNLOADED_TAB_STATUS)},
        )
    page = PageParams.clamp(
        _int(params.get("page")), _int(params.get("page_size"))
    )
    return await downloaded_snapshot(
        deps.database(request),
        tab=tab,
        search=(params.get("search") or "").strip(),
        sort=params.get("sort") or "newest",
        offset=page.offset,
        limit=page.limit,
    )


@router.post("/downloaded/batch")
async def downloaded_batch(request: Request) -> dict[str, Any]:
    """Run one action against a selection of downloaded works."""
    deps.require_session(request)
    deps.require_csrf(request)
    payload = await request.json()
    if not isinstance(payload, dict):
        raise ApiError("BODY_INVALID", "请求体必须是 JSON 对象")
    return await apply_downloaded_batch(
        deps.archived_work_service(request),
        deps.conversion_service(request),
        str(payload.get("action") or ""),
        _work_ids(payload.get("candidate_ids")),
        delete_files=bool(payload.get("delete_files")),
        repack=bool(payload.get("repack")),
        operator_name=str(request.session.get("username") or "admin"),
        announce=lambda candidate_id: request.app.state.event_bus.publish(
            EVENT_DOWNLOAD, candidate_id=candidate_id
        ),
    )


__all__ = [
    "DOWNLOADED_BATCH_ACTIONS",
    "DOWNLOADED_SORT_OPTIONS",
    "MAX_DOWNLOADED_BATCH",
    "apply_downloaded_batch",
    "downloaded_snapshot",
    "router",
]