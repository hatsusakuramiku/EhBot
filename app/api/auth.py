"""Shared administrator login, profile and atomic bearer token rotation."""

from __future__ import annotations

import asyncio
import secrets
import time

from fastapi import APIRouter, Request

from app.admin_auth import AuthError, authenticate, modify_administrator
from app.api import deps
from app.api.contracts import ApiError
from app.bootstrap import remove_bootstrap_password
from app.credentials import (
    KIND_API_KEY, KIND_ACCESS, KIND_REFRESH, hash_secret,
    new_token, parse_token, secret_matches,
)

router = APIRouter(tags=["auth"])
DEFAULT_DEVICE_LABEL = "移动端"
_MAX_LABEL_LENGTH = 80


def api_auth_error(exc: AuthError) -> ApiError:
    status = {
        "AUTH_INVALID_CREDENTIALS": 401, "AUTH_TOKEN_REVOKED": 401,
        "AUTH_LOCKED_OUT": 429, "AUTH_NOT_CONFIGURED": 503,
        "PASSWORD_CHANGE_REQUIRED": 403, "REVISION_CONFLICT": 409,
    }.get(exc.code, 400)
    return ApiError(exc.code, exc.message, status_code=status)


async def _json_body(request: Request) -> dict:
    try:
        payload = await request.json()
    except Exception as exc:
        raise ApiError("BODY_INVALID", "请求体必须是 JSON 对象") from exc
    if not isinstance(payload, dict):
        raise ApiError("BODY_INVALID", "请求体必须是 JSON 对象")
    return payload


def _text(payload, key, default=""):
    value = payload.get(key, default)
    if not isinstance(value, str):
        raise ApiError("BODY_INVALID", "请求字段类型不正确")
    return value


async def _issue_pair(request, *, admin, label, previous=None):
    service = deps.system_settings_service(request)
    access_ttl = await service.mobile_access_ttl_seconds()
    refresh_ttl = await service.mobile_refresh_ttl_seconds()
    refresh_token, refresh_public, refresh_secret = new_token(KIND_REFRESH)
    access_token, access_public, access_secret = new_token(KIND_ACCESS)
    try:
        await deps.database(request).issue_credential_pair(
            admin, refresh=(refresh_public, hash_secret(refresh_secret)),
            access=(access_public, hash_secret(access_secret)), label=label,
            family_id=previous.family_id if previous else secrets.token_hex(16),
            access_ttl=access_ttl, refresh_ttl=refresh_ttl, previous=previous,
        )
    except AuthError as exc:
        raise api_auth_error(exc) from exc
    return {
        "token_type": "Bearer", "access_token": access_token,
        "refresh_token": refresh_token, "expires_in": access_ttl,
        "refresh_expires_in": refresh_ttl,
    }


@router.post("/auth/login")
async def api_login(request: Request) -> dict:
    payload = await _json_body(request)
    try:
        admin = await authenticate(
            deps.database(request), request.app.state.password_hasher,
            _text(payload, "username"), _text(payload, "password"),
            request.app.state.login_attempts,
            request.client.host if request.client else "unknown",
        )
    except AuthError as exc:
        raise api_auth_error(exc) from exc
    if not admin.password_changed:
        raise ApiError("PASSWORD_CHANGE_REQUIRED", "请先在网页端修改初始密码", status_code=403)
    label = _text(payload, "label").strip()[:_MAX_LABEL_LENGTH]
    return await _issue_pair(request, admin=admin, label=label or DEFAULT_DEVICE_LABEL)


@router.post("/auth/refresh")
async def refresh(request: Request) -> dict:
    parsed = parse_token(_text(await _json_body(request), "refresh_token"))
    if parsed is None:
        raise ApiError("AUTH_INVALID_CREDENTIALS", "刷新凭据无效", status_code=401)
    public_id, secret = parsed
    database = deps.database(request)
    record = await database.get_api_credential(public_id)
    if record is None or record.kind != KIND_REFRESH or not secret_matches(secret, record.secret_hash):
        raise ApiError("AUTH_INVALID_CREDENTIALS", "刷新凭据无效", status_code=401)
    if record.is_revoked:
        raise ApiError("AUTH_TOKEN_REVOKED", "刷新凭据已被撤销", status_code=401)
    if record.is_expired(int(time.time())):
        raise ApiError("AUTH_TOKEN_EXPIRED", "刷新凭据已过期", status_code=401)
    admin = await database.get_administrator()
    if admin is None or record.auth_epoch != admin.auth_epoch:
        raise ApiError("AUTH_TOKEN_REVOKED", "认证已更新，请重新登录", status_code=401)
    return await _issue_pair(
        request, admin=admin, label=record.label or DEFAULT_DEVICE_LABEL, previous=record,
    )


@router.post("/auth/logout")
async def api_logout(request: Request) -> dict:
    credential = await deps.require_bearer(request)
    if credential.kind != KIND_API_KEY and credential.family_id:
        await deps.database(request).revoke_credential_family(credential.family_id)
    return {"ok": True}


@router.get("/auth/whoami")
async def whoami(request: Request) -> dict:
    await deps.require_session(request)
    admin = await deps.database(request).get_administrator()
    credential = getattr(request.state, "api_credential", None)
    return {
        "username": admin.username, "revision": admin.revision,
        "kind": credential.kind if credential else "session",
        "label": credential.label if credential else "",
        "expires_at": credential.expires_at if credential else None,
        "must_change_password": not admin.password_changed,
    }


@router.get("/auth/profile")
async def admin_profile(request: Request) -> dict:
    await deps.require_session(request, allow_password_change=True)
    return (await deps.database(request).get_administrator()).profile()


@router.patch("/auth/profile")
async def update_admin_profile(request: Request) -> dict:
    await deps.require_session(request, allow_password_change=True)
    deps.require_csrf(request)
    payload = await _json_body(request)
    try:
        admin = await modify_administrator(
            deps.database(request), request.app.state.password_hasher,
            current_password=_text(payload, "current_password"),
            expected_revision=payload.get("expected_revision"),
            username=_text(payload, "username"),
            actor_session_id=request.session.get("session_id") if getattr(request.state, "auth_source", None) != "bearer" else None,
            actor_credential_id=request.state.api_credential.id if getattr(request.state, "auth_source", None) == "bearer" else None,
        )
    except AuthError as exc:
        raise api_auth_error(exc) from exc
    return admin.profile()


@router.post("/auth/change-password")
async def api_change_password(request: Request) -> dict:
    await deps.require_session(request, allow_password_change=True)
    deps.require_csrf(request)
    payload = await _json_body(request)
    bearer = getattr(request.state, "auth_source", None) == "bearer"
    session_id = None if bearer else secrets.token_urlsafe(32)
    try:
        admin = await modify_administrator(
            deps.database(request), request.app.state.password_hasher,
            current_password=_text(payload, "current_password"),
            expected_revision=payload.get("expected_revision"),
            new_password=_text(payload, "new_password"),
            confirmation=_text(payload, "confirmation"),
            actor_session_id=request.session.get("session_id") if not bearer else None,
            actor_credential_id=request.state.api_credential.id if bearer else None,
            renew_session_id=session_id,
        )
        if getattr(request.state, "auth_source", None) != "bearer":
            request.session.update(
                session_id=session_id,
                auth_epoch=admin.auth_epoch, must_change_password=False,
            )
    except AuthError as exc:
        raise api_auth_error(exc) from exc
    await asyncio.to_thread(remove_bootstrap_password, request.app.state.settings.data_path)
    return {**admin.profile(), "reauthenticate": getattr(request.state, "auth_source", None) == "bearer"}


__all__ = ["router"]
