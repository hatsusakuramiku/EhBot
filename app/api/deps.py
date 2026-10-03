"""Shared accessors for JSON routes.

`create_app` defines its service getters as closures, so nothing outside that
function can reach them. These helpers read the same `app.state` slots through
the `Request`, which lets a router live in its own module without being handed
a dozen constructor arguments, and keeps a missing service reported as 503
rather than surfacing as `AttributeError`.

`require_session` accepts either a browser session cookie or an
`Authorization: Bearer` credential, so the same endpoints serve the web
interface and the mobile client. `require_csrf` is only meaningful for the
cookie path -- see its docstring.
"""

from __future__ import annotations

import hmac
import time
from datetime import datetime, timezone
from typing import Any

from fastapi import Request

from app.api.contracts import ApiError
from app.credentials import (
    BEARER_KINDS,
    ApiCredential,
    parse_token,
    secret_matches,
)


#: Header the browser sends for a state-changing JSON call. HTMX is configured
#: in `base.html` to attach it to every request, so the API can require it
#: without each caller remembering to add a form field.
CSRF_HEADER = "X-CSRF-Token"

#: `last_used_at` is rewritten at most this often, so a busy mobile client does
#: not turn every read into a database write just to keep one timestamp warm.
TOUCH_INTERVAL_SECONDS = 60


def _last_used_is_stale(value: str | None) -> bool:
    """Whether the stored timestamp is old enough to be worth rewriting.

    The value comes from SQLite's `CURRENT_TIMESTAMP` (UTC, no zone suffix). A
    value in any other shape is treated as stale rather than fatal: a bad clock
    must not turn a working credential into a locked-out operator.
    """
    if not value:
        return True
    try:
        seen = datetime.strptime(value, "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return True
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    return (now - seen).total_seconds() >= TOUCH_INTERVAL_SECONDS


async def _bearer_credential(request: Request) -> ApiCredential | None:
    """Resolve an `Authorization: Bearer` credential, or None when absent.

    A malformed or rejected header raises instead of falling back to the
    session: a client that sent a token must hear why it was refused, and
    silently using a cookie would hide a broken credential. Browser requests
    never carry this header, so the page path is untouched.
    """
    # A real `Request` always carries headers; the directly-driven stream tests
    # use a minimal double, for which "no header" is the safe reading.
    headers = getattr(request, "headers", None)
    header = headers.get("authorization") if headers is not None else None
    if not header:
        return None
    scheme, _, value = header.partition(" ")
    parsed = parse_token(value) if scheme.strip().lower() == "bearer" else None
    if parsed is None:
        raise ApiError(
            "AUTH_INVALID_CREDENTIALS", "凭据格式不正确", status_code=401
        )
    public_id, secret = parsed
    database = _service(request, "database", "数据库")
    record = await database.get_api_credential(public_id)
    if record is None or not secret_matches(secret, record.secret_hash):
        raise ApiError(
            "AUTH_INVALID_CREDENTIALS", "凭据无效或已失效", status_code=401
        )
    if record.kind not in BEARER_KINDS:
        # A refresh token is only accepted at `POST /api/v1/auth/refresh`.
        raise ApiError(
            "AUTH_INVALID_CREDENTIALS", "凭据不能用于此处", status_code=401
        )
    if record.is_revoked:
        raise ApiError("AUTH_TOKEN_REVOKED", "凭据已被撤销", status_code=401)
    if record.is_expired(int(time.time())):
        raise ApiError("AUTH_TOKEN_EXPIRED", "凭据已过期", status_code=401)
    request.state.auth_source = "bearer"
    request.state.api_credential = record
    if _last_used_is_stale(record.last_used_at):
        await database.touch_api_credential(record.id)
    return record


async def require_session(request: Request) -> None:
    """Reject an unauthenticated or password-change-pending JSON caller.

    Accepts either a password-derived session cookie or a Bearer credential.
    The page layer redirects in this situation; an API must not, because a
    fetch would silently follow the redirect and hand the caller a login page
    with status 200. A 401 with a stable code lets the interface decide to
    navigate.

    A bearer credential skips the `must_change_password` check by construction:
    the login endpoint refuses to mint one while the bootstrap password is
    still in place.
    """
    if await _bearer_credential(request) is not None:
        return
    if not request.session.get("authenticated"):
        raise ApiError(
            "NOT_AUTHENTICATED", "请先登录", status_code=401
        )
    if request.session.get("must_change_password"):
        raise ApiError(
            "PASSWORD_CHANGE_REQUIRED",
            "请先修改初始密码",
            status_code=403,
        )


async def require_bearer(request: Request) -> ApiCredential:
    """Require a bearer credential specifically (no session fallback).

    Used by the endpoints that act on the credential itself -- logout has
    nothing to revoke when the caller is a browser cookie.
    """
    credential = await _bearer_credential(request)
    if credential is None:
        raise ApiError("NOT_AUTHENTICATED", "请先登录", status_code=401)
    return credential


def require_csrf(request: Request) -> None:
    """Verify the CSRF token on a state-changing JSON call.

    Accepts the token from a header only. A cookie-plus-header pair cannot be
    forged cross-origin without the attacker being able to read the session,
    which is the property the form-field version also relies on.

    A bearer credential is exempt: a browser never sends it automatically, so a
    cross-site request cannot carry it, and a native client has no cookie
    session to protect.
    """
    state = getattr(request, "state", None)
    if state is not None and getattr(state, "auth_source", None) == "bearer":
        return
    expected = request.session.get("csrf_token", "")
    supplied = request.headers.get(CSRF_HEADER, "")
    if not expected or not supplied or not hmac.compare_digest(
        supplied, expected
    ):
        raise ApiError(
            "CSRF_INVALID", "请求校验失败，请刷新页面重试", status_code=403
        )
def _service(request: Request, name: str, label: str) -> Any:
    service = getattr(request.app.state, name, None)
    if service is None:
        raise ApiError(
            "SERVICE_UNAVAILABLE",
            f"{label}当前不可用",
            status_code=503,
            details={"service": name},
        )
    return service


def database(request: Request) -> Any:
    return _service(request, "database", "数据库")


def download_service(request: Request) -> Any:
    return _service(request, "download_service", "下载服务")


def conversion_service(request: Request) -> Any:
    return _service(request, "conversion_service", "打包服务")


def archived_work_service(request: Request) -> Any:
    """The 已下载内容 service: remove, rename and re-download.

    Separate from `download_service` because it is a separate object:
    the queue service owns tasks in flight, this one owns works that
    have finished, and only this one deletes files.
    """
    return _service(request, "archived_work_service", "已下载内容服务")


def archive_settings_service(request: Request) -> Any:
    return _service(request, "archive_settings_service", "归档设置")


def system_settings_service(request: Request) -> Any:
    return _service(request, "system_settings_service", "系统设置")


def ai_service(request: Request) -> Any:
    return _service(request, "ai_service", "AI 供应商")


def exhentai_service(request: Request) -> Any:
    return _service(request, "exhentai_service", "ExHentai 服务")


def telegraph_service(request: Request) -> Any:
    return _service(request, "telegraph_service", "预览页图源")


def torrent_service(request: Request) -> Any:
    return _service(request, "torrent_service", "种子服务")


def connection_manager(request: Request) -> Any:
    return _service(request, "connection_manager", "外部连接")


def thumbnail_service(request: Request) -> Any:
    return _service(request, "thumbnail_service", "缩略图服务")


def review_orchestrator(request: Request) -> Any:
    """The shared approve/reject/route coordinator.

    Exposed so the JSON layer runs the identical code path as the HTML routes.
    Reimplementing the approve-then-enqueue sequence per layer is how the two
    would end up disagreeing about which candidates are downloadable.
    """
    return _service(request, "review_orchestrator", "审核编排")


def optional_service(request: Request, name: str) -> Any | None:
    """Read a service slot without failing when it is switched off.

    Telegraph and torrent are optional by configuration, so a summary endpoint
    has to be able to say「未启用」instead of returning 503 for the whole page.
    """
    return getattr(request.app.state, name, None)


__all__ = [
    "CSRF_HEADER",
    "ai_service",
    "archive_settings_service",
    "connection_manager",
    "conversion_service",
    "database",
    "download_service",
    "exhentai_service",
    "optional_service",
    "require_bearer",
    "require_csrf",
    "require_session",
    "review_orchestrator",
    "system_settings_service",
    "telegraph_service",
    "thumbnail_service",
    "torrent_service",
]