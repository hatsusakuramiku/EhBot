"""The 已下载内容 page: what has been downloaded, and what to do with it.

Restores the domain cut on 2026-08-26 and reinstated on 2026-08-28 by operator
instruction, per §1.3.1 of the requirements document: a page listing downloaded
works in a grid or a list, with multi-select and batch 重新打包 / 移除 /
重新下载, plus per-work rename and relocate.

It follows `/candidates` rather than inventing a layout: the whole page state
lives in the query string and is read in one place, the grid and the list are
two renderings of one selection form, and every action is a real form so the
page works with JavaScript off.

Two decisions worth knowing before editing:

* **`/downloaded/{candidate_id}/...` never renders a page.** Every action
  redirects back to the tab it came from, carrying its own error. The detail
  page for a work is `/works/{id}` and there is exactly one of those.
* **The literal routes are declared above the typed one**, because Starlette
  matches in declaration order and `{candidate_id}` is typed `int`: below it,
  `/downloaded/batch` would be answered by the action route and refused as an
  unparsable id.
"""

from __future__ import annotations

from urllib.parse import quote_plus

from fastapi import APIRouter, Form, Request
from fastapi.responses import RedirectResponse

from app.api.contracts import ApiError, PageParams
from app.api.downloaded import (
    DOWNLOADED_BATCH_ACTIONS,
    DOWNLOADED_SORT_OPTIONS,
    apply_downloaded_batch,
    downloaded_snapshot,
)
from app.api.events import EVENT_DOWNLOAD
from app.api.status import DOWNLOADED_TAB_STATUS, downloaded_tab_view
from app.db.database import DOWNLOADED_PACK_FILTERS
from app.web import deps


router = APIRouter()


#: The five tabs, in the order they are shown. Each is a `pack_filter` the
#: database whitelists; the words come from `downloaded_tab_view`, so the tab
#: strip, the sidebar and the JSON payload cannot drift apart.
#:
#: The description and the two empty-state lines stay here rather than in the
#: status registry: they are page copy, not vocabulary a JSON client shares --
#: the same split `/candidates` and `/activity` make.
DOWNLOADED_TABS: tuple[dict[str, str], ...] = (
    {
        "key": "all",
        "href": "/downloaded",
        "description": "所有已下载完成的作品，以及它们的打包与归档状态",
        "empty_title": "暂无已下载内容",
        "empty_hint": "下载任务完成后，作品会出现在这里等待打包",
    },
    {
        "key": "unpacked",
        "href": "/downloaded/unpacked",
        "description": "已下载但还没有打包成 CBZ 的作品，可在此批量打包",
        "empty_title": "没有待打包的作品",
        "empty_hint": "已下载的作品都已打包完成",
    },
    {
        "key": "packed",
        "href": "/downloaded/packed",
        "description": "已打包并归档的作品，可重新打包、改名或移除",
        "empty_title": "还没有打包好的作品",
        "empty_hint": "在「待打包」中选择作品并打包后会出现在这里",
    },
    {
        "key": "attention",
        "href": "/downloaded/attention",
        "description": "打包任务停在缺卷或缺密码上，补齐后重新打包即可",
        "empty_title": "没有需要处理的作品",
        "empty_hint": "缺分卷或缺密码的打包任务会归到这里",
    },
    {
        "key": "failed",
        "href": "/downloaded/failed",
        "description": "打包失败的作品，可查看原因后重新打包或重新下载",
        "empty_title": "没有打包失败的作品",
        "empty_hint": "打包失败的任务会归到这里，附带失败原因",
    },
)


async def _render_downloaded(
    request: Request,
    tab: str,
    *,
    error: str | None = None,
    notice: str | None = None,
):
    """Render one tab of the 已下载内容 page.

    Reads search / sort / view / page off `request.query_params` rather than
    declaring them on each of five routes, for the reason `_render_candidates`
    does: five routes repeating four parameters is five chances for one to drift,
    and a filtered list should be a link the operator can send themselves.
    """
    params = request.query_params
    search = (params.get("search") or "").strip()
    sort = params.get("sort") or "newest"
    if sort not in {key for key, _ in DOWNLOADED_SORT_OPTIONS}:
        # Forgiving, like the candidate page: a bookmark carrying a sort we have
        # since renamed should still render the list.
        sort = "newest"
    view = params.get("view") if params.get("view") in {"grid", "list"} else "grid"
    page = PageParams.clamp(
        deps.int_param(params.get("page")), deps.int_param(params.get("page_size"))
    )
    snapshot = await downloaded_snapshot(
        deps.database(request),
        tab=tab,
        search=search,
        sort=sort,
        offset=page.offset,
        limit=page.limit,
    )
    current = next(entry for entry in DOWNLOADED_TABS if entry["key"] == tab)
    return deps.templates(request).TemplateResponse(
        request=request,
        name="downloaded.html",
        context={
            "csrf_token": request.session["csrf_token"],
            "tab": tab,
            "tab_title": downloaded_tab_view(tab).label,
            "tab_description": current["description"],
            "tab_href": current["href"],
            "tabs": [
                {
                    "key": entry["key"],
                    "href": entry["href"],
                    "label": downloaded_tab_view(entry["key"]).label,
                    "count": snapshot["counts"].get(entry["key"], 0),
                }
                for entry in DOWNLOADED_TABS
            ],
            # The snapshot is spread in rather than nested so the template reads
            # the same names the JSON body uses, and a test can assert the page
            # context is a superset of it.
            **snapshot,
            "total": snapshot["total"],
            "page": page.page,
            "page_size": page.page_size,
            "prev_href": deps.query_href(request, page=page.page - 1),
            "next_href": deps.query_href(request, page=page.page + 1),
            "grid_href": deps.query_href(request, view="grid"),
            "list_href": deps.query_href(request, view="list"),
            "search": search,
            "sort": sort,
            "sorts": [
                {"key": key, "label": label}
                for key, label in DOWNLOADED_SORT_OPTIONS
            ],
            "view": view,
            "columns": [
                {"key": "select", "label": "选择"},
                {"key": "work", "label": "作品"},
                {"key": "pack", "label": "打包"},
                {"key": "source", "label": "来源"},
                {"key": "pages", "label": "页数", "numeric": True},
                {"key": "size", "label": "大小", "numeric": True},
                {"key": "path", "label": "归档路径"},
                {"key": "actions", "label": "操作"},
            ],
            "empty_title": current["empty_title"],
            "empty_hint": current["empty_hint"],
            "error": error,
            "notice": notice,
        },
    )


#: The five tab routes, all declared above `/downloaded/{candidate_id}`.
@router.get("/downloaded")
async def downloaded_index(
    request: Request, error: str | None = None, notice: str | None = None
):
    redirect = deps.require_authenticated(request)
    if redirect:
        return redirect
    return await _render_downloaded(request, "all", error=error, notice=notice)


@router.get("/downloaded/unpacked")
async def downloaded_unpacked(
    request: Request, error: str | None = None, notice: str | None = None
):
    redirect = deps.require_authenticated(request)
    if redirect:
        return redirect
    return await _render_downloaded(
        request, "unpacked", error=error, notice=notice
    )


@router.get("/downloaded/packed")
async def downloaded_packed(
    request: Request, error: str | None = None, notice: str | None = None
):
    redirect = deps.require_authenticated(request)
    if redirect:
        return redirect
    return await _render_downloaded(
        request, "packed", error=error, notice=notice
    )


@router.get("/downloaded/attention")
async def downloaded_attention(
    request: Request, error: str | None = None, notice: str | None = None
):
    redirect = deps.require_authenticated(request)
    if redirect:
        return redirect
    return await _render_downloaded(
        request, "attention", error=error, notice=notice
    )


@router.get("/downloaded/failed")
async def downloaded_failed(
    request: Request, error: str | None = None, notice: str | None = None
):
    redirect = deps.require_authenticated(request)
    if redirect:
        return redirect
    return await _render_downloaded(
        request, "failed", error=error, notice=notice
    )


def _redirect(
    tab: str, *, error: str | None = None, notice: str | None = None
) -> RedirectResponse:
    """Back to the tab the operator acted from, message and all.

    The tab travels as a form field rather than being read from the referer:
    the operator's filters live in the query string, and a redirect that forgot
    them would silently widen the list they were working in.
    """
    target = next(
        (entry["href"] for entry in DOWNLOADED_TABS if entry["key"] == tab),
        "/downloaded",
    )
    if error:
        return RedirectResponse(
            f"{target}?error={quote_plus(error)}", status_code=303
        )
    if notice:
        return RedirectResponse(
            f"{target}?notice={quote_plus(notice)}", status_code=303
        )
    return RedirectResponse(target, status_code=303)


def _summarise(result: dict) -> tuple[str | None, str | None]:
    """Turn a batch result into one line of page copy.

    A form post has nowhere else to report that three of eight works were
    skipped, so the count and the first reason are folded into the redirect --
    the same compromise `/activity` makes. The first reason rather than all of
    them: they are usually the same one, and a redirect URL is not a log.
    """
    applied = len(result["applied"])
    skipped = result["skipped"]
    if not skipped:
        return (None, f"{applied} 件作品已执行")
    return (
        f"{applied} 件已执行，{len(skipped)} 件跳过：{skipped[0]['message']}",
        None,
    )


@router.post("/downloaded/batch")
async def downloaded_batch_action(
    request: Request,
    csrf_token: str = Form(),
    action: str = Form(),
    tab: str = Form(default="all"),
    candidate_ids: list[int] = Form(default=[]),
    delete_files: str | None = Form(default=None),
    repack: str | None = Form(default=None),
):
    """The bulk toolbar, without JavaScript.

    Runs through `apply_downloaded_batch`, the same coroutine
    `POST /api/v1/downloaded/batch` uses, so the form and the API cannot
    disagree about what a batch does or about which works it skips.

    `delete_files` and `repack` arrive as checkbox values, which is why they are
    typed `str | None`: an unchecked HTML checkbox sends nothing at all, so
    absence is the default and「不删文件」cannot be reached by accident.
    """
    redirect = deps.require_authenticated(request)
    if redirect:
        return redirect
    deps.validate_csrf(request, csrf_token)
    if tab not in DOWNLOADED_PACK_FILTERS:
        tab = "all"
    # 「移除并删除文件」 arrives as its own action name rather than as a second
    # form field, because `ui.confirm` can carry exactly one name/value pair
    # through to the form it is teleported out of. Encoding the choice in the
    # name keeps one endpoint and one shared coroutine -- the alternative was a
    # second remove route, which is the thing to avoid -- and it makes the
    # destructive variant impossible to reach by omission: a request that does
    # not name it deletes nothing.
    wants_files = action == "remove-files"
    if wants_files:
        action = "remove"
    if action not in DOWNLOADED_BATCH_ACTIONS:
        return _redirect(tab, error=f"未知的作品动作：{action}")
    if not candidate_ids:
        return _redirect(tab, error="请至少选择一件作品")
    try:
        result = await apply_downloaded_batch(
            deps.archived_work_service(request),
            deps.conversion_service(request),
            action,
            list(dict.fromkeys(candidate_ids)),
            delete_files=wants_files or delete_files is not None,
            repack=repack is not None,
            operator_name=str(request.session.get("username") or "admin"),
            announce=lambda candidate_id: request.app.state.event_bus.publish(
                EVENT_DOWNLOAD, candidate_id=candidate_id
            ),
        )
    except ApiError as exc:
        return _redirect(tab, error=exc.message)
    error, notice = _summarise(result)
    return _redirect(tab, error=error, notice=notice)


@router.post("/downloaded/{candidate_id}/repack")
async def repack_downloaded_work(
    request: Request,
    candidate_id: int,
    csrf_token: str = Form(),
    tab: str = Form(default="all"),
):
    """Pack or re-pack one work.

    A per-work endpoint so the row button can be a plain
    `<button formaction="...">` inside the batch form, the way `/activity`'s row
    actions are. The alternative -- having the row button submit the batch with
    a single id -- needs JavaScript to fix up the checkboxes first, which is
    exactly the kind of row action that shipped broken once.

    Posts through the same `enqueue_for_candidate` the work detail page's
    重新打包 uses. There is deliberately no second packing path.
    """
    redirect = deps.require_authenticated(request)
    if redirect:
        return redirect
    deps.validate_csrf(request, csrf_token)
    if tab not in DOWNLOADED_PACK_FILTERS:
        tab = "all"
    try:
        await deps.conversion_service(request).enqueue_for_candidate(candidate_id)
    except Exception as exc:  # noqa: BLE001 - domain refusals carry a message
        message = getattr(exc, "public_message", None)
        if message is None:
            raise
        return _redirect(tab, error=str(message))
    request.app.state.event_bus.publish(
        EVENT_DOWNLOAD, candidate_id=candidate_id
    )
    return _redirect(tab, notice="已加入打包队列")


@router.post("/downloaded/{candidate_id}/rename")
async def rename_downloaded_work(
    request: Request,
    candidate_id: int,
    csrf_token: str = Form(),
    tab: str = Form(default="all"),
    filename: str = Form(default=""),
    directory: str = Form(default=""),
    repack: str | None = Form(default=None),
):
    """Rename or relocate one work's published CBZ.

    Per-work rather than batch on purpose: a filename is specific to one book,
    and a batch rename would either need a template -- which is what the archive
    layout setting already is -- or give fifty books the same name.
    """
    redirect = deps.require_authenticated(request)
    if redirect:
        return redirect
    deps.validate_csrf(request, csrf_token)
    if tab not in DOWNLOADED_PACK_FILTERS:
        tab = "all"
    try:
        result = await deps.archived_work_service(request).rename_work(
            candidate_id,
            filename=filename or None,
            directory=directory or None,
        )
        if repack is not None:
            await deps.conversion_service(request).enqueue_for_candidate(
                candidate_id
            )
    except Exception as exc:  # noqa: BLE001 - domain refusals carry a message
        message = getattr(exc, "public_message", None)
        if message is None:
            raise
        return _redirect(tab, error=str(message))
    request.app.state.event_bus.publish(
        EVENT_DOWNLOAD, candidate_id=candidate_id
    )
    if not result["moved"]:
        return _redirect(tab, notice="文件名未变更")
    return _redirect(tab, notice=f"已移动到 {result['relative_path']}")


__all__ = ["DOWNLOADED_TABS", "router"]