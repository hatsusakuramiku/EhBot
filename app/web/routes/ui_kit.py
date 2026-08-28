"""Fixture data for the component gallery at `/ui-kit`.

Kept out of `app/main.py` and out of the template. Out of `main.py` because a
page that renders nothing but fixtures does not belong in the module that wires
the application together; out of the template because the states shown here are
the real enum codes, and a template holding a hardcoded list of them would drift
from `app/api/status.py` silently -- the one thing R3 exists to stop.

Nothing here touches the database. The gallery must render on a fresh install,
which is exactly when someone opens it to check the design system.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request

from app.web import deps

#: Every candidate state, in lifecycle order rather than dict order, so the
#: gallery reads as a timeline and a missing tone is obvious.
CANDIDATE_STATES: tuple[str, ...] = (
    "DISCOVERED",
    "PENDING_REVIEW",
    "NEEDS_INFO",
    "NEEDS_REVISION",
    "APPROVED",
    "PROCESSING",
    "DOWNLOADED",
    "REJECTED",
    "FAILED",
)

#: Download and conversion states share a column in the database and a badge in
#: the interface, so they are shown as one row here too.
JOB_STATES: tuple[str, ...] = (
    "PENDING",
    "DOWNLOADING",
    "WAITING_TORRENT",
    "PAUSED",
    "COMPLETED",
    "CANCELLED",
    "CONVERSION_PENDING",
    "CONVERSION_RUNNING",
    "CONVERSION_COMPLETED",
    "CONVERSION_WAITING_VOLUMES",
    "CONVERSION_WAITING_PASSWORD",
    "CONVERSION_FAILED",
)

CONNECTION_STATES: tuple[str, ...] = (
    "connected",
    "connecting",
    "error",
    "not_configured",
    # The MTProto login is a multi-step exchange, so the user account has two
    # states a token-based connection cannot be in. They are in the gallery for
    # the same reason as the rest: a test asserts every label in `status.py`
    # renders here, which is what keeps a template from writing its own.
    "awaiting_code",
    "awaiting_password",
)

PROVIDERS: tuple[str, ...] = (
    "TELEGRAM",
    "TELEGRAM_USER",
    "EH_TORRENT",
    "EXHENTAI",
    "TELEGRAPH",
    "CONVERSION",
)

TABLE_COLUMNS: tuple[dict[str, Any], ...] = (
    {"key": "select", "label": "选择"},
    {"key": "title", "label": "标题", "sortable": True},
    {"key": "status", "label": "状态", "sortable": True},
    {"key": "provider", "label": "来源"},
    {"key": "pages", "label": "页数", "numeric": True, "sortable": True},
    {"key": "updated", "label": "更新时间", "sortable": True},
)


def _rows() -> tuple[dict[str, Any], ...]:
    return (
        {
            "title": "[作者名] 示例本 01",
            "status": "PENDING_REVIEW",
            "provider": "TELEGRAM",
            "pages": 186,
            "updated": "2026-08-26 10:12",
            "selected": True,
        },
        {
            "title": "[作者名] 示例本 02 (汉化)",
            "status": "PROCESSING",
            "provider": "EH_TORRENT",
            "pages": 42,
            "updated": "2026-08-26 09:57",
            "selected": True,
        },
        {
            "title": "[Circle] 示例合集 (C99)",
            "status": "CONVERSION_WAITING_PASSWORD",
            "provider": "TELEGRAM",
            "pages": 320,
            "updated": "2026-08-25 22:04",
            "selected": False,
        },
        {
            "title": "[作者名] 示例本 04",
            "status": "DOWNLOADED",
            "provider": "TELEGRAPH",
            "pages": 28,
            "updated": "2026-08-25 18:30",
            "selected": False,
        },
        {
            "title": "[作者名] 示例本 05",
            "status": "FAILED",
            "provider": "EXHENTAI",
            "pages": 0,
            "updated": "2026-08-25 14:11",
            "selected": False,
        },
        {
            "title": "[作者名] 示例本 06",
            "status": "NEEDS_INFO",
            "provider": "TELEGRAM",
            "pages": 96,
            "updated": "2026-08-24 20:48",
            "selected": False,
        },
    )


def _cards() -> tuple[dict[str, Any], ...]:
    # The first card deliberately has no cover: the placeholder branch is the
    # one a fresh install sees most, so it is shown first rather than as an
    # afterthought. The rest point at the thumbnail proxy with obviously fake
    # hashes -- the images 404, which is itself the honest demonstration that
    # `<img>` failure does not break the card layout.
    return (
        {
            "title": "尚未取得封面的候选",
            "cover": None,
            "meta": "Telegram · 186 页",
            "tags": ("汉化", "单行本"),
            "status": "PENDING_REVIEW",
            "selected": False,
        },
        {
            "title": "[作者名] 示例本 02 (汉化)",
            "cover": {"url": "/api/v1/thumbnails/" + "0" * 64, "hash": "0" * 64},
            "meta": "EH 种子 · 42 页",
            "tags": ("汉化",),
            "status": "PROCESSING",
            "selected": True,
        },
        {
            "title": "[Circle] 示例合集 (C99)",
            "cover": {"url": "/api/v1/thumbnails/" + "1" * 64, "hash": "1" * 64},
            "meta": "Telegram · 320 页",
            "tags": ("合集", "待补密码"),
            "status": "CONVERSION_WAITING_PASSWORD",
            "selected": False,
        },
        {
            "title": "[作者名] 示例本 04",
            "cover": {"url": "/api/v1/thumbnails/" + "2" * 64, "hash": "2" * 64},
            "meta": "预览页图源 · 28 页",
            "tags": ("短篇",),
            "status": "DOWNLOADED",
            "selected": False,
        },
    )


def ui_kit_context() -> dict[str, Any]:
    """Return the `demo` object the gallery template renders from."""
    return {
        "tabs": [
            {"key": "all", "label": "全部", "href": "#", "count": 137},
            {"key": "review", "label": "待审核", "href": "#", "count": 12},
            {"key": "needs-info", "label": "待补充", "href": "#", "count": 3},
            {"key": "processing", "label": "处理中", "href": "#", "count": 5},
            {"key": "failed", "label": "失败", "href": "#", "count": 1},
        ],
        "candidate_states": CANDIDATE_STATES,
        "job_states": JOB_STATES,
        "connection_states": CONNECTION_STATES,
        "providers": PROVIDERS,
        "columns": TABLE_COLUMNS,
        "rows": _rows(),
        "cards": _cards(),
        "filters": {
            "status": [
                {"value": "PENDING_REVIEW", "label": "待审核", "count": 12, "checked": True},
                {"value": "NEEDS_INFO", "label": "待补充", "count": 3},
                {"value": "PROCESSING", "label": "处理中", "count": 5},
                {"value": "FAILED", "label": "失败", "count": 1},
            ],
            "provider": [
                {"value": "TELEGRAM", "label": "Telegram 原档", "count": 88},
                {"value": "EH_TORRENT", "label": "EH 种子", "count": 31},
                {"value": "TELEGRAPH", "label": "预览页图源", "count": 18},
            ],
        },
    }


__all__ = ["ui_kit_context"]

router = APIRouter()


@router.get("/ui-kit")
async def ui_kit_page(request: Request):
    # Behind the session like every other page: it is a developer tool, not
    # public documentation, and an unauthenticated route here would be one
    # more surface to keep honest for no benefit.
    redirect = deps.require_authenticated(request)
    if redirect:
        return redirect
    return deps.templates(request).TemplateResponse(
        request=request,
        name="ui_kit.html",
        context={"demo": ui_kit_context()},
    )
