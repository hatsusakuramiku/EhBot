"""The candidate domain: the six tabs, the batch bar and the row actions.

The tab routes are declared above `/candidates/{candidate_id}` on purpose --
Starlette matches in declaration order and that route types its parameter as
`int`, so below it `/candidates/approved` would be answered by the detail
redirect and refused as an unparsable id. Keep that order when editing.
"""

from __future__ import annotations

from urllib.parse import quote_plus

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import RedirectResponse

from app.api.actions import REVIEW_BATCH_ACTIONS, apply_review_batch
from app.api.candidates import (
    CANDIDATE_SORTS,
    CANDIDATE_TABS,
    candidate_facet_selection,
    candidate_tab_counts,
)
from app.api.contracts import ApiError, PageParams
from app.api.events import EVENT_CANDIDATE, EVENT_DOWNLOAD
from app.api.serializers import candidate_summary
from app.api.status import candidate_tab_view
from app.conversion.service import ConversionError
from app.downloads.service import DownloadError
from app.exhentai.service import ExHentaiDownloadError
from app.review.models import REVIEWABLE_STATUSES
from app.review.service import ReviewError
from app.telegraph.models import TelegraphError
from app.torrent.models import TorrentError
from app.web import deps
from app.web.routes.works import render_review_error

router = APIRouter()


#: The six candidate tabs, in the order the strip shows them. The label is
#: *not* here: it comes from `candidate_tab_view`, so a tab name reads the
#: same in the strip, in a JSON payload and in a badge. What is here is page
#: copy -- the sentence under the heading and the two lines an empty tab
#: shows -- which belongs to the page and to nothing else.
#:
#: 「全部」 is listed first because it is the superset, but `/candidates`
#: renders 待审核: the domain's front door should be the queue an operator
#: opens it to work, the way `/activity` is 队列 rather than a combined view.
CANDIDATE_PAGE_TABS: tuple[dict[str, str], ...] = (
    {
        "key": "all",
        "href": "/candidates/all",
        "description": "全部候选，包含已结束的记录",
        "empty_title": "还没有任何候选",
        "empty_hint": "白名单来源的新消息与手动添加的链接都会落到这里",
    },
    {
        "key": "pending",
        "href": "/candidates",
        "description": "确认元数据后可批量加入下载队列",
        "empty_title": "暂无待审核候选",
        "empty_hint": "白名单来源的新候选会显示在这里",
    },
    {
        "key": "needs_info",
        "href": "/candidates/needs-info",
        "description": "缺标题、缺附件或需要修订的候选",
        "empty_title": "暂无待补充候选",
        "empty_hint": "信息不足的候选会显示在这里",
    },
    {
        "key": "approved",
        "href": "/candidates/approved",
        "description": "已通过审核、正在下载或已完成的候选",
        "empty_title": "暂无已通过候选",
        "empty_hint": "通过审核的候选会进入下载队列并显示在这里",
    },
    {
        "key": "rejected",
        "href": "/candidates/rejected",
        "description": "已驳回的候选，可在详情页重新入队",
        "empty_title": "暂无驳回记录",
        "empty_hint": "驳回不会删除候选，随时可以改主意",
    },
    {
        "key": "failed",
        "href": "/candidates/failed",
        "description": "下载或打包失败、需要检查后重试的候选",
        "empty_title": "暂无失败候选",
        "empty_hint": "失败的候选会显示在这里，附带失败原因",
    },
)

#: Sort keys offered in the toolbar, with the words for each. The keys are
#: `CANDIDATE_SORTS`; the database owns what each one means in SQL.
CANDIDATE_SORT_OPTIONS: tuple[tuple[str, str], ...] = (
    ("newest", "最新发现"),
    ("oldest", "最早发现"),
    ("updated", "最近更新"),
    ("title", "按标题"),
)

#: Facet name -> sidebar heading. The names are `CANDIDATE_FACETS`, which is
#: what decides how each one is matched in SQL.
CANDIDATE_FILTER_GROUPS: tuple[tuple[str, str], ...] = (
    ("tags", "标签"),
    ("artist", "作者"),
    ("language", "语言"),
    ("category", "分类"),
)


async def _render_candidates(
    request: Request,
    tab: str,
    *,
    error: str | None = None,
    status_code: int = 200,
):
    """Render one candidate tab.

    Replaces the four near-identical queue pages. Every tab reads the same
    query string -- search, sort, view, facets, page -- so a filter survives
    a tab switch instead of being a per-page feature, and the tab counts are
    shown on all six because「待审核还有多少」is not a question worth changing
    tab to answer.

    Arguments are read from `request.query_params` rather than declared on
    each route: six routes repeating eight parameters is six chances for one
    of them to drift, and everything here is optional by construction.
    """
    params = request.query_params
    search = (params.get("search") or "").strip()
    sort = params.get("sort") or "newest"
    if sort not in CANDIDATE_SORTS:
        # Forgiving on purpose, unlike the JSON endpoint: a bookmarked link
        # with a sort we have since renamed should still show the list.
        sort = "newest"
    view = params.get("view") if params.get("view") in {"grid", "list"} else "grid"
    try:
        facets = candidate_facet_selection(
            {name: params.getlist(name) for name, _ in CANDIDATE_FILTER_GROUPS}
        )
    except ApiError as exc:
        facets = {}
        error = error or exc.message
    page = PageParams.clamp(
        deps.int_param(params.get("page")), deps.int_param(params.get("page_size"))
    )
    statuses = CANDIDATE_TABS[tab]

    if tab == "pending":
        # Kept from the old queue page: opening 待审核 is what enriches new
        # candidates and lets an auto-approval rule fire. Bounded to the
        # page the operator is looking at, which the pre-R5 version was not.
        first, _ = await deps.database(request).list_candidates_page(
            statuses=statuses,
            search=search,
            facets=facets,
            sort=sort,
            offset=page.offset,
            limit=page.limit,
        )
        await deps.exhentai_service(request).enrich_candidates_for_review(first)
        for candidate in first:
            await deps.review_orchestrator(request).apply_automatic_approval(candidate.candidate_id)

    items, total = await deps.database(request).list_candidates_page(
        statuses=statuses,
        search=search,
        facets=facets,
        sort=sort,
        offset=page.offset,
        limit=page.limit,
    )
    counts = candidate_tab_counts(await deps.database(request).candidate_counts())
    options = await deps.database(request).candidate_facets(statuses=statuses)
    current = next(
        entry for entry in CANDIDATE_PAGE_TABS if entry["key"] == tab
    )
    # Batch review is offered wherever a candidate can still be reviewed.
    # `REVIEWABLE_STATUSES` decides that, not the tab name, and 「全部」 has
    # no status filter so it always offers it.
    batch_enabled = not statuses or bool(set(statuses) & REVIEWABLE_STATUSES)
    return deps.templates(request).TemplateResponse(
        request=request,
        name="candidates.html",
        context={
            "csrf_token": request.session["csrf_token"],
            "tab": tab,
            "tab_title": candidate_tab_view(tab).label,
            "tab_description": current["description"],
            # The tab's own path, with no query string: what 「清除」 goes to
            # and what the filter form posts back to.
            "tab_href": current["href"],
            "tabs": [
                {
                    "key": entry["key"],
                    "href": entry["href"],
                    "label": candidate_tab_view(entry["key"]).label,
                    "count": counts.get(entry["key"], 0),
                }
                for entry in CANDIDATE_PAGE_TABS
            ],
            # Serialised through the same function the JSON list uses, so a
            # card and an API client describe a candidate identically -- the
            # cover URL included, which is a proxy path and never upstream.
            "candidates": [candidate_summary(item) for item in items],
            "total": total,
            "page": page.page,
            "page_size": page.page_size,
            # Paging and the view switch are links rather than scripted
            # buttons, so both survive JavaScript being off and a shared URL
            # reopens exactly what the sender was looking at.
            "prev_href": deps.query_href(request, page=page.page - 1),
            "next_href": deps.query_href(request, page=page.page + 1),
            "grid_href": deps.query_href(request, view="grid"),
            "list_href": deps.query_href(request, view="list"),
            "sort": sort,
            "sorts": [
                {"key": key, "label": label}
                for key, label in CANDIDATE_SORT_OPTIONS
            ],
            "search": search,
            "view": view,
            # List-view headers. The select and action columns are dropped
            # where the tab cannot review anything, so a terminal tab does
            # not show an empty checkbox column.
            "columns": [
                *(
                    [{"key": "select", "label": "选择"}]
                    if batch_enabled
                    else []
                ),
                {"key": "candidate", "label": "候选"},
                {"key": "status", "label": "状态"},
                {"key": "tags", "label": "标签"},
                {"key": "messages", "label": "消息", "numeric": True},
                {"key": "updated", "label": "更新"},
                {"key": "actions", "label": "操作"},
            ],
            "filters": [
                {
                    "name": name,
                    "title": title,
                    "options": [
                        {
                            "value": value,
                            "label": value,
                            "count": count,
                            "checked": value in facets.get(name, ()),
                        }
                        for value, count in options.get(name, ())
                    ],
                }
                for name, title in CANDIDATE_FILTER_GROUPS
            ],
            "active_filters": sum(
                len(values) for values in facets.values()
            ),
            # Batch review is offered wherever a candidate can still be
            # reviewed -- see `batch_enabled` above.
            "batch_enabled": batch_enabled,
            "empty_title": current["empty_title"],
            "empty_hint": current["empty_hint"],
            "error": error,
        },
        status_code=status_code,
    )


#: The six tab routes. Declared above `/candidates/{candidate_id}` because
#: Starlette matches in declaration order and that route types its parameter
#: as `int`: below it, `/candidates/approved` would be answered by the detail
#: page and refused as an unparsable id.
@router.get("/candidates")
async def candidate_queue(request: Request, error: str | None = None):
    redirect = deps.require_authenticated(request)
    if redirect:
        return redirect
    return await _render_candidates(request, "pending", error=error)


@router.get("/candidates/all")
async def all_candidates(request: Request, error: str | None = None):
    redirect = deps.require_authenticated(request)
    if redirect:
        return redirect
    return await _render_candidates(request, "all", error=error)


@router.get("/candidates/needs-info")
async def needs_info_queue(request: Request, error: str | None = None):
    redirect = deps.require_authenticated(request)
    if redirect:
        return redirect
    return await _render_candidates(request, "needs_info", error=error)


@router.get("/candidates/approved")
async def approved_queue(request: Request, error: str | None = None):
    redirect = deps.require_authenticated(request)
    if redirect:
        return redirect
    return await _render_candidates(request, "approved", error=error)


@router.get("/candidates/rejected")
async def rejected_queue(request: Request, error: str | None = None):
    redirect = deps.require_authenticated(request)
    if redirect:
        return redirect
    return await _render_candidates(request, "rejected", error=error)


@router.get("/candidates/failed")
async def failed_queue(request: Request, error: str | None = None):
    redirect = deps.require_authenticated(request)
    if redirect:
        return redirect
    return await _render_candidates(request, "failed", error=error)

#: 处理中 was its own page before R5 and is now part of 已通过, which covers
#: APPROVED, PROCESSING and DOWNLOADED. Kept as a redirect rather than
#: deleted, for the reason `/downloads` was: a bookmark that used to work
#: should not 404. 307 so no browser caches it and makes the path
#: unreclaimable.
@router.get("/candidates/processing")
async def processing_queue(request: Request):
    return RedirectResponse("/candidates/approved", status_code=307)


async def _candidates_redirect(
    request: Request, error: str | None = None
) -> RedirectResponse:
    """Back to the tab the operator submitted from, error and all.

    The tab travels in a hidden form field rather than being read off the
    referer: a batch approved from 待补充 must not drop the operator onto
    待审核, and unlike the activity page the choice is one of six, which is
    more than a header sniff should be deciding.
    """
    target = str((await request.form()).get("tab") or "")
    entry = next(
        (item for item in CANDIDATE_PAGE_TABS if item["key"] == target),
        CANDIDATE_PAGE_TABS[1],
    )
    href = entry["href"]
    if error:
        href = f"{href}?error={quote_plus(error)}"
    return RedirectResponse(href, status_code=303)


@router.post("/candidates/batch-review")
async def batch_review(request: Request):
    """The bulk toolbar, without JavaScript.

    Runs through `apply_review_batch`, the same coroutine
    `POST /api/v1/candidates/batch` uses, so the form and the API cannot
    disagree about what a batch does or about which candidates it skips.
    Skips are folded into the redirect's message: a form post has nowhere
    else to report that three of eight were already approved.
    """
    redirect = deps.require_authenticated(request)
    if redirect:
        return redirect
    form = await request.form()
    deps.validate_csrf(request, str(form.get("csrf_token") or ""))
    action = str(form.get("action") or "")
    try:
        candidate_ids = list(
            dict.fromkeys(
                int(value) for value in form.getlist("candidate_ids")
            )
        )
    except ValueError:
        candidate_ids = []
    if action not in REVIEW_BATCH_ACTIONS:
        return await _candidates_redirect(
            request, f"未知的审核动作：{action}"
        )
    if not candidate_ids:
        return await _candidates_redirect(request, "请至少选择一条候选")
    operator = request.session.get("username", "admin")
    try:
        result = await apply_review_batch(
            deps.review_orchestrator(request),
            action,
            candidate_ids,
            operator,
            announce_candidate=lambda candidate_id: (
                request.app.state.event_bus.publish(
                    EVENT_CANDIDATE, candidate_id=candidate_id
                )
            ),
            announce_job=lambda job_id: request.app.state.event_bus.publish(
                EVENT_DOWNLOAD, job_id=job_id
            ),
        )
    except ApiError as exc:
        return await _candidates_redirect(request, exc.message)
    skipped = result["skipped"]
    if not skipped:
        return await _candidates_redirect(request)
    return await _candidates_redirect(
        request,
        f"{len(result['applied'])} 条已处理，{len(skipped)} 条跳过："
        f"{skipped[0]['message']}",
    )


@router.get("/candidates/{candidate_id}")
async def candidate_detail(request: Request, candidate_id: int):
    """The retired detail path, kept as a redirect to `/works/{id}`.

    Until R6 this rendered the page and `/works/{id}` bounced here; the two
    have swapped. 307 rather than 301 for the same reason as every other
    retirement in this refactor — a permanent redirect cached in a browser
    makes the path unreclaimable. The route keeps its name so that
    `url_for('candidate_detail', ...)` in anything still holding the old
    reference resolves instead of raising.
    """
    return RedirectResponse(
        request.url_for("work_detail", candidate_id=candidate_id).path,
        status_code=307,
    )


@router.post("/candidates/{candidate_id}/approve")
async def approve_candidate(
    request: Request,
    candidate_id: int,
    csrf_token: str = Form(),
    tab: str | None = Form(None),
):
    """Approve one candidate and enqueue its download.

    Both the detail page and the list's 行内快速通过 post here, so there is
    one approve path rather than a shortcut that could drift from it. `tab`
    is only sent by the list, and its presence is what says「回到列表」: a
    quick approve must not teleport the operator into a detail page they did
    not ask to open.
    """
    redirect = deps.require_authenticated(request)
    if redirect:
        return redirect
    deps.validate_csrf(request, csrf_token)
    operator = request.session.get("username", "admin")
    try:
        await deps.review_orchestrator(request).approve_and_enqueue([candidate_id], operator)
    except ReviewError as exc:
        if tab is not None:
            return await _candidates_redirect(request, exc.public_message)
        return await render_review_error(
            request,
            candidate_id,
            exc.public_message,
        )
    if tab is not None:
        return await _candidates_redirect(request)
    return RedirectResponse(
        request.url_for("work_detail", candidate_id=candidate_id).path,
        status_code=303,
    )


@router.post("/candidates/{candidate_id}/reject")
async def reject_candidate(
    request: Request,
    candidate_id: int,
    csrf_token: str = Form(),
):
    redirect = deps.require_authenticated(request)
    if redirect:
        return redirect
    deps.validate_csrf(request, csrf_token)
    operator = request.session.get("username", "admin")
    try:
        await deps.review_service(request).reject_candidate(candidate_id, operator)
    except ReviewError as exc:
        return await render_review_error(
            request, candidate_id, exc.public_message
        )
    return RedirectResponse(
        request.url_for("work_detail", candidate_id=candidate_id).path,
        status_code=303,
    )


@router.post("/candidates/{candidate_id}/needs-revision")
async def needs_revision_candidate(
    request: Request,
    candidate_id: int,
    csrf_token: str = Form(),
    reason: str = Form(""),
):
    redirect = deps.require_authenticated(request)
    if redirect:
        return redirect
    deps.validate_csrf(request, csrf_token)
    operator = request.session.get("username", "admin")
    try:
        await deps.review_service(request).request_revision(
            candidate_id, operator, reason
        )
    except ReviewError as exc:
        return await render_review_error(
            request, candidate_id, exc.public_message
        )
    return RedirectResponse(
        request.url_for("work_detail", candidate_id=candidate_id).path,
        status_code=303,
    )


@router.post("/candidates/{candidate_id}/requeue")
async def requeue_candidate(
    request: Request,
    candidate_id: int,
    csrf_token: str = Form(),
):
    redirect = deps.require_authenticated(request)
    if redirect:
        return redirect
    deps.validate_csrf(request, csrf_token)
    operator = request.session.get("username", "admin")
    form = await request.form()
    note = str(form.get("note") or "").strip() or None
    try:
        await deps.review_service(request).requeue_candidate(candidate_id, operator, note)
    except ReviewError as exc:
        return await render_review_error(
            request, candidate_id, exc.public_message
        )
    return RedirectResponse(
        request.url_for("work_detail", candidate_id=candidate_id).path,
        status_code=303,
    )


@router.post("/candidates/{candidate_id}/metadata")
async def edit_metadata(
    request: Request,
    candidate_id: int,
    csrf_token: str = Form(),
    field_name: str = Form(),
    field_value: str = Form(""),
):
    redirect = deps.require_authenticated(request)
    if redirect:
        return redirect
    deps.validate_csrf(request, csrf_token)
    operator = request.session.get("username", "admin")
    try:
        await deps.review_service(request).set_manual_metadata(
            candidate_id, operator, field_name, field_value
        )
    except ReviewError as exc:
        return await render_review_error(
            request, candidate_id, exc.public_message
        )
    return RedirectResponse(
        request.url_for("work_detail", candidate_id=candidate_id).path,
        status_code=303,
    )


@router.post("/candidates/{candidate_id}/download")
async def download_candidate(
    request: Request,
    candidate_id: int,
    csrf_token: str = Form(),
):
    redirect = deps.require_authenticated(request)
    if redirect:
        return redirect
    deps.validate_csrf(request, csrf_token)
    candidate = await deps.database(request).get_candidate(candidate_id)
    if candidate is None:
        raise HTTPException(status_code=404, detail="Candidate not found")
    archive_attachments = []
    for message in candidate.messages:
        for attachment in message.attachments:
            if attachment.get("type") == "archive":
                archive_attachments.append(attachment)
    if not archive_attachments:
        return await render_review_error(
            request,
            candidate_id,
            "该候选没有可下载的压缩附件",
        )
    try:
        await deps.download_service(request).enqueue_telegram_download(
            candidate_id, archive_attachments[0]
        )
    except DownloadError as exc:
        return await render_review_error(
            request, candidate_id, exc.public_message
        )
    return RedirectResponse(
        request.url_for("work_detail", candidate_id=candidate_id).path,
        status_code=303,
    )


@router.post("/candidates/{candidate_id}/telegram-user")
async def download_candidate_with_user(
    request: Request,
    candidate_id: int,
    csrf_token: str = Form(),
):
    """Fetch the uploader's archive with the MTProto user account.

    A separate route from `/download` rather than a mode on it: the two use
    different credentials and have different failure modes, and an operator whose
    Bot API attempt failed on the 20 MB ceiling is choosing the other protocol
    deliberately. Same shape as `/download` otherwise -- one attachment, one job,
    refusals re-render the work page.
    """
    redirect = deps.require_authenticated(request)
    if redirect:
        return redirect
    deps.validate_csrf(request, csrf_token)
    candidate = await deps.database(request).get_candidate(candidate_id)
    if candidate is None:
        raise HTTPException(status_code=404, detail="Candidate not found")
    archive_attachments = [
        attachment
        for message in candidate.messages
        for attachment in message.attachments
        if attachment.get("type") == "archive"
    ]
    if not archive_attachments:
        return await render_review_error(
            request,
            candidate_id,
            "该候选没有可下载的压缩附件",
        )
    try:
        await deps.download_service(request).enqueue_telegram_user_download(
            candidate_id, archive_attachments[0]
        )
    except DownloadError as exc:
        return await render_review_error(
            request, candidate_id, exc.public_message
        )
    return RedirectResponse(
        request.url_for("work_detail", candidate_id=candidate_id).path,
        status_code=303,
    )


@router.post("/candidates/{candidate_id}/exhentai-metadata")
async def fetch_exhentai_metadata(
    request: Request,
    candidate_id: int,
    csrf_token: str = Form(),
):
    redirect = deps.require_authenticated(request)
    if redirect:
        return redirect
    deps.validate_csrf(request, csrf_token)
    try:
        await deps.exhentai_service(request).fetch_metadata_for_candidate(candidate_id)
    except ExHentaiDownloadError as exc:
        return await render_review_error(
            request, candidate_id, exc.public_message
        )
    return RedirectResponse(
        request.url_for("work_detail", candidate_id=candidate_id).path,
        status_code=303,
    )


@router.post("/candidates/{candidate_id}/exhentai-archive")
async def download_exhentai_archive(
    request: Request,
    candidate_id: int,
    csrf_token: str = Form(),
):
    redirect = deps.require_authenticated(request)
    if redirect:
        return redirect
    deps.validate_csrf(request, csrf_token)
    try:
        await deps.exhentai_service(request).download_archive_for_candidate(candidate_id)
    except ExHentaiDownloadError as exc:
        return await render_review_error(
            request, candidate_id, exc.public_message
        )
    return RedirectResponse(
        request.url_for("work_detail", candidate_id=candidate_id).path,
        status_code=303,
    )


@router.post("/candidates/{candidate_id}/telegraph")
async def download_telegraph_preview(
    request: Request,
    candidate_id: int,
    csrf_token: str = Form(),
):
    """Fetch the preview page on demand.

    Queued rather than run inline: a 78-page book takes far longer than a
    request should, and the queue already reports progress and failures.
    """
    redirect = deps.require_authenticated(request)
    if redirect:
        return redirect
    deps.validate_csrf(request, csrf_token)
    try:
        deps.telegraph_service(request)
        await deps.download_service(request).enqueue_telegraph_download(candidate_id)
    except (DownloadError, TelegraphError) as exc:
        return await render_review_error(
            request, candidate_id, exc.public_message
        )
    return RedirectResponse(
        request.url_for("work_detail", candidate_id=candidate_id).path,
        status_code=303,
    )


@router.post("/candidates/{candidate_id}/torrent")
async def download_torrent(
    request: Request,
    candidate_id: int,
    csrf_token: str = Form(),
):
    """Queue the EH torrent route on demand.

    Queued rather than run inline for the same reason as every other
    source, plus one of its own: the transfer is the client's work and may
    take hours, so there is nothing useful to return synchronously.
    """
    redirect = deps.require_authenticated(request)
    if redirect:
        return redirect
    deps.validate_csrf(request, csrf_token)
    try:
        deps.torrent_service(request)
        await deps.download_service(request).enqueue_torrent_download(candidate_id)
    except (DownloadError, TorrentError) as exc:
        return await render_review_error(
            request, candidate_id, exc.public_message
        )
    return RedirectResponse(
        request.url_for("work_detail", candidate_id=candidate_id).path,
        status_code=303,
    )


@router.post("/candidates/{candidate_id}/convert")
async def convert_candidate(
    request: Request,
    candidate_id: int,
    csrf_token: str = Form(),
):
    redirect = deps.require_authenticated(request)
    if redirect:
        return redirect
    deps.validate_csrf(request, csrf_token)
    try:
        await deps.conversion_service(request).enqueue_for_candidate(candidate_id)
    except ConversionError as exc:
        return await render_review_error(
            request, candidate_id, exc.public_message
        )
    return RedirectResponse(
        request.url_for("work_detail", candidate_id=candidate_id).path,
        status_code=303,
    )
