"""The Telegram and ExHentai connection forms, on the 连接 settings tab.

Credentials arrive here and go straight into the secret store: nothing on this
path writes one to a log, and no response echoes one back to the page.
"""

from __future__ import annotations

from dataclasses import asdict

from fastapi import APIRouter, Form, Request
from fastapi.responses import RedirectResponse

from app.api.status import SETTINGS_CONNECTIONS
from app.connections.exhentai import ExHentaiCredentials
from app.connections.models import ProviderConnectionError
from app.web import deps
from app.web.settings_view import render_settings, settings_redirect

router = APIRouter()


@router.get("/connections")
async def connections_page(request: Request):
    """Retired: 外部连接 is a tab of `/settings`."""
    return RedirectResponse(
        request.url_for(
            "settings_section", section=SETTINGS_CONNECTIONS
        ).path,
        status_code=307,
    )


@router.post("/connections/telegram")
async def configure_telegram(
    request: Request,
    bot_token: str = Form(),
    csrf_token: str = Form(),
):
    redirect = deps.require_authenticated(request)
    if redirect:
        return redirect
    deps.validate_csrf(request, csrf_token)
    try:
        await deps.connection_manager(request).configure_telegram(bot_token)
    except ProviderConnectionError as exc:
        # The refusal is named rather than left to the snapshot: the
        # provider records its own error, but a credential that was
        # rejected before it could be stored leaves nothing there.
        return await render_settings(
            request,
            SETTINGS_CONNECTIONS,
            error=exc.public_message,
            status_code=400,
        )
    return settings_redirect(request, SETTINGS_CONNECTIONS)


@router.post("/connections/telegram/disconnect")
async def disconnect_telegram(request: Request, csrf_token: str = Form()):
    redirect = deps.require_authenticated(request)
    if redirect:
        return redirect
    deps.validate_csrf(request, csrf_token)
    await deps.connection_manager(request).disconnect_telegram()
    return settings_redirect(request, SETTINGS_CONNECTIONS)


@router.post("/connections/telegram-user/login")
async def start_telegram_user_login(
    request: Request,
    api_id: str = Form(),
    api_hash: str = Form(),
    phone: str = Form(),
    csrf_token: str = Form(),
):
    """Step one: store the api pair and ask Telegram to send a code."""
    redirect = deps.require_authenticated(request)
    if redirect:
        return redirect
    deps.validate_csrf(request, csrf_token)
    try:
        await deps.connection_manager(request).start_telegram_user_login(
            api_id, api_hash, phone
        )
    except ProviderConnectionError as exc:
        return await render_settings(
            request,
            SETTINGS_CONNECTIONS,
            error=exc.public_message,
            status_code=400,
        )
    return settings_redirect(request, SETTINGS_CONNECTIONS)


@router.post("/connections/telegram-user/verify")
async def verify_telegram_user_login(
    request: Request,
    csrf_token: str = Form(),
    code: str = Form(default=""),
    password: str = Form(default=""),
):
    """Step two: the code, or the 2FA password when Telegram asked for one.

    Both fields are optional at the HTTP level and the pair is validated here,
    because which one is required depends on how far the login got -- a form that
    marked either `required` would block the other step.
    """
    redirect = deps.require_authenticated(request)
    if redirect:
        return redirect
    deps.validate_csrf(request, csrf_token)
    if not code.strip() and not password:
        return await render_settings(
            request,
            SETTINGS_CONNECTIONS,
            error="请填写验证码，或两步验证密码",
            status_code=400,
        )
    try:
        await deps.connection_manager(request).complete_telegram_user_login(
            code=code or None, password=password or None
        )
    except ProviderConnectionError as exc:
        return await render_settings(
            request,
            SETTINGS_CONNECTIONS,
            error=exc.public_message,
            status_code=400,
        )
    return settings_redirect(request, SETTINGS_CONNECTIONS)


@router.post("/connections/telegram-user/disconnect")
async def disconnect_telegram_user(request: Request, csrf_token: str = Form()):
    redirect = deps.require_authenticated(request)
    if redirect:
        return redirect
    deps.validate_csrf(request, csrf_token)
    await deps.connection_manager(request).disconnect_telegram_user()
    return settings_redirect(request, SETTINGS_CONNECTIONS)


@router.post("/connections/exhentai")
async def configure_exhentai(
    request: Request,
    ipb_member_id: str = Form(),
    ipb_pass_hash: str = Form(),
    igneous: str = Form(),
    csrf_token: str = Form(),
):
    redirect = deps.require_authenticated(request)
    if redirect:
        return redirect
    deps.validate_csrf(request, csrf_token)
    try:
        await deps.connection_manager(request).configure_exhentai(
            ExHentaiCredentials(ipb_member_id, ipb_pass_hash, igneous)
        )
    except ProviderConnectionError as exc:
        # The refusal is named rather than left to the snapshot: the
        # provider records its own error, but a credential that was
        # rejected before it could be stored leaves nothing there.
        return await render_settings(
            request,
            SETTINGS_CONNECTIONS,
            error=exc.public_message,
            status_code=400,
        )
    return settings_redirect(request, SETTINGS_CONNECTIONS)


@router.post("/connections/exhentai/disconnect")
async def disconnect_exhentai(request: Request, csrf_token: str = Form()):
    redirect = deps.require_authenticated(request)
    if redirect:
        return redirect
    deps.validate_csrf(request, csrf_token)
    await deps.connection_manager(request).disconnect_exhentai()
    return settings_redirect(request, SETTINGS_CONNECTIONS)


@router.get("/api/connections/status")
async def connection_status(request: Request):
    redirect = deps.require_authenticated(request)
    if redirect:
        return redirect
    return asdict(deps.connection_manager(request).snapshot())
