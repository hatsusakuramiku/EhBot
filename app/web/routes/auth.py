"""Web adapters for shared administrator authentication and configuration."""

from __future__ import annotations

import asyncio
import secrets

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import RedirectResponse

from app.admin_auth import AuthError, authenticate, modify_administrator
from app.api.auth import api_auth_error
from app.api.status import SETTINGS_PASSWORDS
from app.bootstrap import remove_bootstrap_password
from app.web import deps
from app.web.login_throttle import (
    LOCKOUT_SECONDS, MAX_FAILED_ATTEMPTS, MAX_TRACKED_CLIENTS, _prune_expired,
)
from app.web.settings_view import render_settings, settings_redirect

router = APIRouter()


def _login_response(request, *, username="admin", error=None, status_code=200):
    return deps.templates(request).TemplateResponse(
        request=request, name="login.html", status_code=status_code,
        context={"csrf_token": request.session.setdefault("csrf_token", secrets.token_urlsafe(32)),
                 "username": username, "error": error},
    )


@router.get("/login")
async def login_page(request: Request):
    return _login_response(request)


@router.post("/login")
async def login(
    request: Request, username: str = Form(""), password: str = Form(""),
    csrf_token: str = Form(),
):
    deps.validate_csrf(request, csrf_token)
    database = deps.database(request)
    try:
        admin = await authenticate(
            database, request.app.state.password_hasher, username, password,
            request.app.state.login_attempts,
            request.client.host if request.client else "unknown",
        )
        session_id = await database.create_browser_session(admin)
    except AuthError as exc:
        status = api_auth_error(exc).status_code
        if exc.code == "AUTH_LOCKED_OUT":
            raise HTTPException(status_code=429, detail="Too many login attempts") from exc
        return _login_response(request, username=username, error=exc.message, status_code=status)
    # Replacing a login in the same browser does not leave its previous session usable.
    await database.revoke_browser_session(request.session.get("session_id", ""))
    request.session.clear()
    request.session.update(
        authenticated=True, username=admin.username, admin_id=admin.id,
        auth_epoch=admin.auth_epoch, session_id=session_id,
        csrf_token=secrets.token_urlsafe(32), must_change_password=not admin.password_changed,
    )
    destination = (
        request.url_for("settings_section", section=SETTINGS_PASSWORDS).path
        if not admin.password_changed else request.url_for("dashboard").path
    )
    return RedirectResponse(destination, status_code=303)


@router.get("/change-password")
async def change_password_page(request: Request):
    return RedirectResponse(
        request.url_for("settings_section", section=SETTINGS_PASSWORDS).path,
        status_code=307,
    )


@router.post("/change-password")
async def change_password(
    request: Request, current_password: str = Form(), new_password: str = Form(),
    confirmation: str = Form(), csrf_token: str = Form(), expected_revision: int = Form(),
):
    if not request.session.get("authenticated"):
        return RedirectResponse(request.url_for("login_page").path, status_code=303)
    deps.validate_csrf(request, csrf_token)
    session_id = secrets.token_urlsafe(32)
    try:
        admin = await modify_administrator(
            deps.database(request), request.app.state.password_hasher,
            current_password=current_password, expected_revision=expected_revision,
            new_password=new_password, confirmation=confirmation,
            actor_session_id=request.session["session_id"], renew_session_id=session_id,
        )
        request.session.update(
            session_id=session_id,
            auth_epoch=admin.auth_epoch, must_change_password=False,
        )
    except AuthError as exc:
        return await render_settings(
            request, SETTINGS_PASSWORDS, error=exc.message,
            status_code=409 if exc.code == "REVISION_CONFLICT" else 400,
        )
    await asyncio.to_thread(remove_bootstrap_password, deps.settings(request).data_path)
    return RedirectResponse(request.url_for("dashboard").path, status_code=303)


@router.post("/administrator")
async def rename_administrator(
    request: Request, username: str = Form(), current_password: str = Form(),
    expected_revision: int = Form(), csrf_token: str = Form(),
):
    if not request.session.get("authenticated"):
        return RedirectResponse(request.url_for("login_page").path, status_code=303)
    deps.validate_csrf(request, csrf_token)
    try:
        admin = await modify_administrator(
            deps.database(request), request.app.state.password_hasher,
            current_password=current_password, expected_revision=expected_revision, username=username,
            actor_session_id=request.session["session_id"],
        )
    except AuthError as exc:
        return await render_settings(
            request, SETTINGS_PASSWORDS, error=exc.message, administrator_draft=username,
            status_code=409 if exc.code == "REVISION_CONFLICT" else 400,
        )
    request.session["username"] = admin.username
    return settings_redirect(request, SETTINGS_PASSWORDS)


@router.post("/logout")
async def logout(request: Request, csrf_token: str = Form()):
    deps.validate_csrf(request, csrf_token)
    await deps.database(request).revoke_browser_session(request.session.get("session_id", ""))
    request.session.clear()
    return RedirectResponse(request.url_for("login_page").path, status_code=303)
