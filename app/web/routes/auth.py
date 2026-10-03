"""Login, logout and the administrator password change.

The throttle itself lives in `app.web.login_throttle`, because the JSON login at
`POST /api/v1/auth/login` takes the same administrator password and must share
one counter. The names are re-exported here so existing callers (and tests) keep
importing them from this module.

**A lockout is logged.** It used to be silent, which made「我被锁在外面了」
unanswerable from the log: the 429 appeared in the access log as a status code
with no reason attached. It is now a warning with a stable `error_code`, and it
is the one place the throttle's decision is visible.
"""

from __future__ import annotations

import asyncio
import logging
import secrets
import time

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import RedirectResponse
from pwdlib.exceptions import PwdlibError

from app.api.status import SETTINGS_PASSWORDS
from app.bootstrap import remove_bootstrap_password
from app.web import deps, login_throttle
from app.web.login_throttle import (
    LOCKOUT_SECONDS,
    MAX_FAILED_ATTEMPTS,
    MAX_TRACKED_CLIENTS,
    _prune_expired,
)
from app.web.settings_view import render_settings

router = APIRouter()


@router.get("/login")
async def login_page(request: Request):
    csrf_token = request.session.setdefault("csrf_token", secrets.token_urlsafe(32))
    return deps.templates(request).TemplateResponse(
        request=request,
        name="login.html",
        context={"csrf_token": csrf_token},
    )


@router.post("/login")
async def login(
    request: Request,
    password: str = Form(),
    csrf_token: str = Form(),
):
    deps.validate_csrf(request, csrf_token)
    attempts = request.app.state.login_attempts
    client_key = request.client.host if request.client else "unknown"
    now = time.monotonic()
    if login_throttle.is_locked(attempts, client_key, now):
        raise HTTPException(status_code=429, detail="Too many login attempts")
    failed_count = login_throttle.failed_count(attempts, client_key, now)
    admin_auth = await deps.database(request).get_admin_auth("admin")
    if admin_auth is None:
        raise HTTPException(status_code=503, detail="Authentication is not configured")
    try:
        password_matches = await asyncio.to_thread(
            request.app.state.password_hasher.verify, password, admin_auth[0]
        )
    except PwdlibError as exc:
        raise HTTPException(
            status_code=503, detail="Authentication is not configured"
        ) from exc
    if not password_matches:
        if login_throttle.record_failure(attempts, client_key, now, failed_count):
            # The one record of the throttle firing. Without it a locked-out
            # operator sees a 429 in the access log and no reason for it.
            logging.getLogger(__name__).warning(
                "login_locked_out client=%s attempts=%d",
                client_key,
                failed_count + 1,
                extra={"error_code": "LOGIN_LOCKED_OUT"},
            )
        return deps.templates(request).TemplateResponse(
            request=request,
            name="login.html",
            context={
                "csrf_token": request.session["csrf_token"],
                "error": "密码不正确",
            },
            status_code=401,
        )
    login_throttle.clear(attempts, client_key)
    request.session.clear()
    request.session["authenticated"] = True
    request.session["username"] = "admin"
    request.session["csrf_token"] = secrets.token_urlsafe(32)
    request.session["must_change_password"] = not admin_auth[1]
    destination = (
        request.url_for(
            "settings_section", section=SETTINGS_PASSWORDS
        ).path
        if not admin_auth[1]
        else request.url_for("dashboard").path
    )
    return RedirectResponse(destination, status_code=303)


@router.get("/change-password")
async def change_password_page(request: Request):
    """Retired: the administrator password is changed on the 密码库 tab.

    A 307 rather than a deletion because this is the path a first login used
    to land on, and an operator who bookmarked it during setup should reach
    the form rather than a 404. The destination tab is the one section
    `require_authenticated` lets through while the bootstrap password is
    still in place, so the redirect works during exactly the situation it
    was written for.
    """
    return RedirectResponse(
        request.url_for(
            "settings_section", section=SETTINGS_PASSWORDS
        ).path,
        status_code=307,
    )


@router.post("/change-password")
async def change_password(
    request: Request,
    current_password: str = Form(),
    new_password: str = Form(),
    confirmation: str = Form(),
    csrf_token: str = Form(),
):
    if not request.session.get("authenticated"):
        return RedirectResponse(
            request.url_for("login_page").path, status_code=303
        )
    deps.validate_csrf(request, csrf_token)
    error: str | None = None
    if len(new_password) < 12:
        error = "新密码至少需要 12 个字符"
    elif new_password != confirmation:
        error = "两次输入的新密码不一致"
    admin_auth = await deps.database(request).get_admin_auth("admin")
    if admin_auth is None:
        raise HTTPException(status_code=503, detail="Authentication is not configured")
    try:
        current_password_matches = await asyncio.to_thread(
            request.app.state.password_hasher.verify, current_password, admin_auth[0]
        )
    except PwdlibError as exc:
        raise HTTPException(
            status_code=503, detail="Authentication is not configured"
        ) from exc
    if not current_password_matches:
        error = "当前密码不正确"
    if error:
        return await render_settings(
            request,
            SETTINGS_PASSWORDS,
            error=error,
            status_code=400,
        )
    new_password_hash = await asyncio.to_thread(
        request.app.state.password_hasher.hash, new_password
    )
    await deps.database(request).change_admin_password("admin", new_password_hash)
    # Changing the password revokes every password-derived mobile token, so a
    # device that was signed in with the old password has to sign in again. The
    # API key is deliberately untouched: it is an independent credential.
    revoked = await deps.database(request).revoke_password_credentials()
    if revoked:
        logging.getLogger(__name__).info(
            "password_change_revoked_tokens revoked=%d", revoked
        )
    await asyncio.to_thread(remove_bootstrap_password, deps.settings(request).data_path)
    request.session["must_change_password"] = False
    return RedirectResponse(request.url_for("dashboard").path, status_code=303)


@router.post("/logout")
async def logout(request: Request, csrf_token: str = Form()):
    deps.validate_csrf(request, csrf_token)
    request.session.clear()
    return RedirectResponse(request.url_for("login_page").path, status_code=303)
