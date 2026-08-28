"""Login, logout and the administrator password change.

The failed-attempt counter is per-process state on `app.state` rather than a
module global: two applications in one test session must not share a lockout.
"""

from __future__ import annotations

import asyncio
import secrets
import time

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import RedirectResponse
from pwdlib.exceptions import PwdlibError

from app.api.status import SETTINGS_PASSWORDS
from app.bootstrap import remove_bootstrap_password
from app.web import deps
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
    client_key = request.client.host if request.client else "unknown"
    failed_count, locked_until = request.app.state.login_attempts.get(client_key, (0, 0.0))
    now = time.monotonic()
    if locked_until > now:
        raise HTTPException(status_code=429, detail="Too many login attempts")
    if locked_until:
        failed_count = 0
        request.app.state.login_attempts.pop(client_key, None)
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
        failed_count += 1
        request.app.state.login_attempts[client_key] = (
            failed_count,
            now + 60 if failed_count >= 5 else 0.0,
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
    request.app.state.login_attempts.pop(client_key, None)
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
    await asyncio.to_thread(remove_bootstrap_password, deps.settings(request).data_path)
    request.session["must_change_password"] = False
    return RedirectResponse(request.url_for("dashboard").path, status_code=303)


@router.post("/logout")
async def logout(request: Request, csrf_token: str = Form()):
    deps.validate_csrf(request, csrf_token)
    request.session.clear()
    return RedirectResponse(request.url_for("login_page").path, status_code=303)
