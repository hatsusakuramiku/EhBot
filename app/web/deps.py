"""Shared accessors for the HTML page layer.

The companion to `app/api/deps.py`, and it exists for the same reason: until R9
every one of these was a closure inside `create_app`, so a page route could not
live anywhere but that one function. `main.py` had grown to ~2960 lines, and the
size was not the real cost — the cost was that adding a route meant editing the
module that wires the application together, and that nothing about a route's
dependencies was written down anywhere.

Everything here reads `app.state` through the `Request`. That is the whole trick:
a router module needs no context object, no constructor arguments and no import
from `app.main`, which is what breaks the cycle (`main` imports the routers, the
routers import this).

Why not just use `app/api/deps.py`
---------------------------------
The two layers fail differently, and that difference is deliberate. A JSON
caller gets an `ApiError` with a stable code; a page gets an `HTTPException` or a
303 to the login form. An API must never redirect an unauthenticated caller,
because `fetch` follows the redirect and hands back a login page with status 200
— which is why `require_session` and `require_authenticated` are two functions
rather than one with a flag.
"""

from __future__ import annotations

import hmac
from typing import Any

from fastapi import HTTPException, Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates

from app.api.status import SETTINGS_PASSWORDS
from app.review.service import ReviewService


def templates(request: Request) -> Jinja2Templates:
    """The configured Jinja environment.

    Built once in `app/web/rendering.py` and published on `app.state`, because
    the filters and globals it carries (`status_label`, `connection_view`, …) are
    what stop a template from spelling a state's Chinese by hand. A router that
    built its own environment would silently lose them.
    """
    return request.app.state.templates


def validate_csrf(request: Request, supplied_token: str) -> None:
    """Verify the CSRF token a form submitted.

    The form-field variant of `app.api.deps.require_csrf`: a page posts a hidden
    field, an API sends a header. Compared with `compare_digest` rather than `==`
    so a wrong token cannot be discovered a character at a time.
    """
    expected_token = request.session.get("csrf_token", "")
    if not expected_token or not hmac.compare_digest(
        supplied_token, expected_token
    ):
        raise HTTPException(status_code=403, detail="Invalid CSRF token")


def require_authenticated(
    request: Request, *, allow_password_change: bool = False
) -> RedirectResponse | None:
    """Bounce a caller who may not see this page.

    An operator still holding the bootstrap password is sent to the 密码库 tab,
    which is where the form that clears the flag lives. `allow_password_change`
    is for that tab itself: bouncing a page off itself is a loop with no exit, so
    the one destination of the bounce opts out of it.

    Returns the redirect instead of raising it, so a handler reads as
    `if redirect: return redirect` — the caller decides, and a route that needs
    to do something before bouncing still can.
    """
    if not request.session.get("authenticated"):
        return RedirectResponse(
            request.url_for("login_page").path, status_code=303
        )
    if not allow_password_change and request.session.get(
        "must_change_password"
    ):
        return RedirectResponse(
            request.url_for(
                "settings_section", section=SETTINGS_PASSWORDS
            ).path,
            status_code=303,
        )
    return None


def _service(request: Request, name: str, detail: str) -> Any:
    """One optional service, or 503.

    A page must not answer 500 because a source is switched off: `telegraph` and
    `torrent` are optional by configuration, and a deployment without them is a
    supported deployment, not a broken one.
    """
    service = getattr(request.app.state, name, None)
    if service is None:
        raise HTTPException(status_code=503, detail=detail)
    return service


def database(request: Request) -> Any:
    # Never None: constructed in `create_app` before the app object exists.
    return request.app.state.database


def settings(request: Request) -> Any:
    """The immutable environment configuration.

    Distinct from `system_settings_service`, which is the operator-editable
    store: this is what the process was started with and cannot change without a
    restart.
    """
    return request.app.state.settings


def download_service(request: Request) -> Any:
    return _service(request, "download_service", "Downloads are unavailable")


def conversion_service(request: Request) -> Any:
    return _service(request, "conversion_service", "Conversion is unavailable")


def archive_settings_service(request: Request) -> Any:
    return _service(
        request, "archive_settings_service", "Archive settings are unavailable"
    )


def system_settings_service(request: Request) -> Any:
    # No None check: constructed eagerly in `create_app`, because it holds
    # nothing but the database handle and both `/api/v1/meta` and the settings
    # page read it on requests that can arrive before the workers have started.
    return request.app.state.system_settings_service


def exhentai_service(request: Request) -> Any:
    return _service(request, "exhentai_service", "ExHentai is unavailable")


def telegraph_service(request: Request) -> Any:
    return _service(
        request, "telegraph_service", "Telegraph source is unavailable"
    )


def torrent_service(request: Request) -> Any:
    return _service(request, "torrent_service", "Torrent source is unavailable")


def connection_manager(request: Request) -> Any:
    return _service(request, "connection_manager", "Connections are unavailable")


def review_service(request: Request) -> ReviewService:
    """A per-request review service.

    Cheap by construction — it holds the database handle and nothing else — so
    it is built per call rather than stored on `app.state`, which keeps it out of
    the startup ordering entirely.
    """
    return ReviewService(request.app.state.database)


def review_orchestrator(request: Request) -> Any:
    """The shared approve/reject/route coordinator.

    The same instance `app/api` uses. Two copies of the approve-then-enqueue
    sequence is how the page layer and the JSON layer would end up disagreeing
    about which candidates are downloadable.
    """
    return _service(request, "review_orchestrator", "Review is unavailable")


async def refresh_display_timezone(request: Request) -> str:
    """Re-cache the zone the shell renders timestamps in.

    `shell_context` runs for every rendered page and is synchronous, so it cannot
    read this from the database itself. Refreshed on startup and after the 系统
    form saves — which together are every moment it can change.
    """
    zone = await system_settings_service(request).timezone()
    request.app.state.display_timezone = zone
    return zone


def int_param(raw: str | None) -> int | None:
    """A query-string integer, or None when it is absent or junk.

    A hand-edited `?page=abc` should show page one, not a 422: the page is a
    place an operator lands from a bookmark, and nothing about it is worth
    refusing a render for.
    """
    try:
        return int(raw) if raw not in (None, "") else None
    except ValueError:
        return None


def query_href(request: Request, **params: object) -> str:
    """This URL with some query parameters replaced.

    A view toggle and a page link each differ from the current page by one
    parameter. Rebuilding the query string in the template would mean re-listing
    every filter at four call sites, and the first one to forget `search` would
    silently drop it; Starlette does the merge instead.

    Returned as a path so the rendered link is relative, which keeps the page
    correct behind a reverse proxy that terminates a different scheme.
    """
    url = request.url.include_query_params(**params)
    return f"{url.path}?{url.query}" if url.query else url.path


def local_return_to(raw: str | None) -> str | None:
    """Accept a same-site path to come back to, or nothing.

    Job actions live at `/activity/jobs/{id}/...` because the queue page owned
    them first, but R6 lets the work detail page post the same forms, and an
    operator who paused a download from `/works/12` must not land on the queue.
    The page says where it wants to return in a hidden field, which makes this an
    open-redirect surface: an absolute URL here would let a crafted form send
    someone off-site with the app's own redirect. So only a rooted path is
    honoured -- no scheme, no `//host` (protocol-relative, which browsers do
    treat as another origin), no backslash (which some parsers normalise into
    one), and no control characters.
    """
    target = (raw or "").strip()
    if not target.startswith("/") or target.startswith("//"):
        return None
    if "\\" in target or any(char < " " or char == "\x7f" for char in target):
        return None
    return target


__all__ = [
    "archive_settings_service",
    "connection_manager",
    "conversion_service",
    "database",
    "download_service",
    "exhentai_service",
    "int_param",
    "local_return_to",
    "query_href",
    "refresh_display_timezone",
    "require_authenticated",
    "review_orchestrator",
    "review_service",
    "settings",
    "system_settings_service",
    "telegraph_service",
    "templates",
    "torrent_service",
    "validate_csrf",
]
