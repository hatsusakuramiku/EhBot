"""The download and packing queues, and the actions on one job.

`/activity/jobs/{job_id}/{action}` is declared last for the reason its docstring
gives: it is a catch-all, and `switch-source` has to be matched before it.
"""

from __future__ import annotations

from urllib.parse import quote_plus

from fastapi import APIRouter, Form, Request
from fastapi.responses import RedirectResponse

from app.api.actions import BATCH_JOB_ACTIONS, JOB_ACTIONS, apply_job_batch
from app.api.activity import queue_snapshot
from app.api.contracts import ApiError
from app.api.events import EVENT_DOWNLOAD
from app.api.serializers import job_summary
from app.downloads.models import (
    DEFAULT_JOB_PRIORITY,
    MAX_JOB_PRIORITY,
    MIN_JOB_PRIORITY,
)
from app.downloads.service import DownloadError
from app.web import deps

router = APIRouter()


#: The three tabs of the activity domain, with the label each one wears as a
#: heading and as a tab. Defined once so the tab strip, the `<title>` and the
#: page heading cannot disagree, and so adding a tab is one entry.
ACTIVITY_TABS: tuple[tuple[str, str, str, str], ...] = (
    (
        "queue",
        "/activity",
        "队列",
        "Telegram 附件、EH 种子、Archive Download 与预览页四条来源共用一个下载队列",
    ),
    (
        "packing",
        "/activity/packing",
        "打包",
        "下载完成后的解压与 CBZ 打包任务。它们不占用下载并发，所以单独一个队列",
    ),
    (
        "history",
        "/activity/history",
        "历史",
        "所有已结束的任务：完成、失败与取消。终态记录不会被清理",
    ),
)


async def _render_activity(
    request: Request,
    tab: str,
    error: str | None = None,
):
    """Render one activity tab.

    All three tabs read the same snapshot, even 历史: the tab counts and the
    needs-attention banner are shown on every tab, because a packaging job
    stuck on a password is not something the operator should have to change
    tab to discover.
    """
    snapshot = await queue_snapshot(deps.download_service(request))
    counts = {
        "queue": snapshot["counts"]["downloads"],
        "packing": snapshot["counts"]["packing"],
        "history": None,
    }
    current = next(entry for entry in ACTIVITY_TABS if entry[0] == tab)
    context = {
        "csrf_token": request.session["csrf_token"],
        "tab": tab,
        "tab_title": current[2],
        "tab_description": current[3],
        "tabs": [
            {
                "key": key,
                "href": href,
                "label": label,
                "count": counts[key],
            }
            for key, href, label, _ in ACTIVITY_TABS
        ],
        "snapshot": snapshot,
        "error": error,
        "queue_columns": [
            {"key": "select", "label": "选择"},
            {"key": "job", "label": "任务"},
            {"key": "candidate", "label": "候选"},
            {"key": "provider", "label": "来源"},
            {"key": "priority", "label": "优先级", "numeric": True},
            {"key": "attempt", "label": "尝试", "numeric": True},
            {"key": "artifact", "label": "产出"},
            {"key": "actions", "label": "操作"},
        ],
        # History has neither a checkbox nor an action column: every row is
        # terminal, so there is nothing to select it for.
        "history_columns": [
            {"key": "job", "label": "任务"},
            {"key": "candidate", "label": "候选"},
            {"key": "provider", "label": "来源"},
            {"key": "priority", "label": "优先级", "numeric": True},
            {"key": "attempt", "label": "尝试", "numeric": True},
            {"key": "artifact", "label": "产出"},
        ],
        "default_priority": DEFAULT_JOB_PRIORITY,
        "min_priority": MIN_JOB_PRIORITY,
        "max_priority": MAX_JOB_PRIORITY,
        "empty_title": (
            "暂无打包任务" if tab == "packing" else "暂无进行中的下载任务"
        ),
        "empty_hint": (
            "下载完成的任务会自动进入打包队列"
            if tab == "packing"
            else "在已审核候选详情页触发下载后会出现在这里"
        ),
    }
    if tab == "history":
        context["history_jobs"] = [
            job_summary(job)
            for job in await deps.download_service(request).list_history_jobs()
        ]
    return deps.templates(request).TemplateResponse(
        request=request, name="activity.html", context=context
    )


@router.get("/activity")
async def activity_queue(request: Request, error: str | None = None):
    redirect = deps.require_authenticated(request)
    if redirect:
        return redirect
    return await _render_activity(request, "queue", error)


@router.get("/activity/packing")
async def activity_packing(request: Request, error: str | None = None):
    redirect = deps.require_authenticated(request)
    if redirect:
        return redirect
    return await _render_activity(request, "packing", error)


@router.get("/activity/history")
async def activity_history(request: Request):
    redirect = deps.require_authenticated(request)
    if redirect:
        return redirect
    return await _render_activity(request, "history")

#: The pre-R4 paths. Kept as redirects rather than deleted: an operator's
#: bookmark and any link in an old Telegram notification both point here, and
#: a 404 on a page that used to work is worse than one extra hop. 307 rather
#: than 301 so a browser does not cache the redirect forever, which would
#: make the paths impossible to reclaim.
@router.get("/downloads")
async def downloads_dashboard(request: Request):
    return RedirectResponse("/activity", status_code=307)


@router.get("/downloads/history")
async def downloads_history(request: Request):
    return RedirectResponse("/activity/history", status_code=307)


async def _activity_redirect(
    request: Request, error: str | None = None
) -> RedirectResponse:
    """Back to the tab the operator submitted from, error and all.

    The referer decides, so acting on a packaging job from 打包 does not drop
    the operator onto 队列. It is only ever used to pick between two known
    paths -- never followed -- so a forged header buys nothing.
    """
    referer = request.headers.get("referer") or ""
    target = (
        "/activity/packing" if "/activity/packing" in referer else "/activity"
    )
    if error:
        target = f"{target}?error={quote_plus(error)}"
    return RedirectResponse(target, status_code=303)


@router.post("/activity/jobs/batch")
async def activity_batch(
    request: Request,
    csrf_token: str = Form(),
    action: str = Form(),
    job_ids: list[int] = Form(default=[]),
    priority: int | None = Form(default=None),
    provider: str | None = Form(default=None),
):
    """The bulk toolbar, without JavaScript.

    Runs through `apply_job_batch`, the same coroutine
    `POST /api/v1/jobs/batch` uses, so the form and the API cannot disagree
    about what a batch does or about which jobs it skips. Skips are folded
    into the redirect's error text: a form post has nowhere else to report
    that three of eight jobs were already cancelled.
    """
    redirect = deps.require_authenticated(request)
    if redirect:
        return redirect
    deps.validate_csrf(request, csrf_token)
    if action not in BATCH_JOB_ACTIONS:
        return await _activity_redirect(request, f"未知的任务动作：{action}")
    if not job_ids:
        return await _activity_redirect(request, "请至少选择一个任务")
    try:
        result = await apply_job_batch(
            deps.download_service(request),
            action,
            list(dict.fromkeys(job_ids)),
            provider=provider,
            priority=priority,
            announce=lambda job_id: request.app.state.event_bus.publish(
                EVENT_DOWNLOAD, job_id=job_id
            ),
        )
    except ApiError as exc:
        return await _activity_redirect(request, exc.message)
    skipped = result["skipped"]
    if not skipped:
        return await _activity_redirect(request)
    return await _activity_redirect(
        request,
        f"{len(result['applied'])} 个任务已执行，{len(skipped)} 个跳过："
        f"{skipped[0]['message']}",
    )


async def _job_action(
    request: Request,
    csrf_token: str,
    action,
    job_id: int,
    return_to: str | None = None,
):
    """Run one queue action and return where the form came from, either way.

    `return_to` is how the work detail page keeps the operator on itself
    without a second set of job routes; it is validated by
    `local_return_to`, and anything else falls back to the activity page.
    """
    redirect = deps.require_authenticated(request)
    if redirect:
        return redirect
    deps.validate_csrf(request, csrf_token)
    target = deps.local_return_to(return_to)
    try:
        await action(job_id)
    except DownloadError as exc:
        if target:
            return RedirectResponse(
                f"{target}?error={quote_plus(exc.public_message)}",
                status_code=303,
            )
        return await _activity_redirect(request, exc.public_message)
    request.app.state.event_bus.publish(EVENT_DOWNLOAD, job_id=job_id)
    if target:
        return RedirectResponse(target, status_code=303)
    return await _activity_redirect(request)


@router.post("/activity/jobs/{job_id}/switch-source")
async def switch_download_source(
    request: Request,
    job_id: int,
    csrf_token: str = Form(),
    provider: str = Form(),
    return_to: str | None = Form(None),
):
    """Move a stalled torrent to another source at the operator's request.

    A stall is never resolved automatically: dropping to preview grade or
    spending GP are both choices the service refuses to make for someone.

    Declared above the catch-all below, for the same reason the API's copy is:
    Starlette matches routes in declaration order, so `/jobs/5/switch-source`
    would otherwise be answered by `activity_job_action` and refused as an
    unknown action.
    """
    return await _job_action(
        request,
        csrf_token,
        lambda target: deps.download_service(request).switch_source(target, provider),
        job_id,
        return_to,
    )


@router.post("/activity/jobs/{job_id}/{action}")
async def activity_job_action(
    request: Request,
    job_id: int,
    action: str,
    csrf_token: str = Form(),
    return_to: str | None = Form(None),
):
    """One row's action button, without JavaScript.

    The action table is `app.api.actions.JOB_ACTIONS`, shared with the JSON
    API, so the form fallback can never offer a verb the API does not have.
    """
    method_name = JOB_ACTIONS.get(action)
    if method_name is None:
        local = deps.local_return_to(return_to)
        if local:
            return RedirectResponse(
                f"{local}?error={quote_plus(f'未知的任务动作：{action}')}",
                status_code=303,
            )
        return await _activity_redirect(request, f"未知的任务动作：{action}")
    return await _job_action(
        request,
        csrf_token,
        getattr(deps.download_service(request), method_name),
        job_id,
        return_to,
    )
