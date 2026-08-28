"""Shared accessors for JSON routes.

`create_app` defines its service getters as closures, so nothing outside that
function can reach them. These helpers read the same `app.state` slots through
the `Request`, which lets a router live in its own module without being handed
a dozen constructor arguments, and keeps a missing service reported as 503
rather than surfacing as `AttributeError`.
"""

from __future__ import annotations

import hmac
from typing import Any

from fastapi import Request

from app.api.contracts import ApiError


#: Header the browser sends for a state-changing JSON call. HTMX is configured
#: in `base.html` to attach it to every request, so the API can require it
#: without each caller remembering to add a form field.
CSRF_HEADER = "X-CSRF-Token"


def require_session(request: Request) -> None:
    """Reject an unauthenticated or password-change-pending JSON caller.

    The page layer redirects in this situation; an API must not, because a
    fetch would silently follow the redirect and hand the caller a login page
    with status 200. A 401 with a stable code lets the interface decide to
    navigate.
    """
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


def require_csrf(request: Request) -> None:
    """Verify the CSRF token on a state-changing JSON call.

    Accepts the token from a header only. A cookie-plus-header pair cannot be
    forged cross-origin without the attacker being able to read the session,
    which is the property the form-field version also relies on.
    """
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


def archive_settings_service(request: Request) -> Any:
    return _service(request, "archive_settings_service", "归档设置")


def system_settings_service(request: Request) -> Any:
    return _service(request, "system_settings_service", "系统设置")


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
    "archive_settings_service",
    "connection_manager",
    "conversion_service",
    "database",
    "download_service",
    "exhentai_service",
    "optional_service",
    "require_csrf",
    "require_session",
    "review_orchestrator",
    "system_settings_service",
    "telegraph_service",
    "thumbnail_service",
    "torrent_service",
]