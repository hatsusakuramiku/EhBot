"""Bearer auth for the mobile client: password login, refresh, logout, whoami.

The browser keeps using the session cookie; this module is the second door into
the same `/api/v1` endpoints. Two credential families exist, both verified in
`app/api/deps.py`:

* the API key, minted in the web UI, is pasted into the client and used directly
  as a bearer token -- it is never issued by an endpoint;
* `POST /auth/login` verifies the administrator password (the same hash and the
  same throttle the page login uses) and mints an access/refresh pair.

Refresh rotates: the presented refresh token is revoked before a new pair is
issued, so a leaked refresh token is usable at most once. Every credential of
one login shares a `family_id`; `logout` revokes that family. A password change
(pages layer) revokes every password-derived token but deliberately leaves the
API key alone.
"""

from __future__ import annotations

import asyncio
import logging
import secrets
import time

from fastapi import APIRouter, Request
from pwdlib.exceptions import PwdlibError

from app.api import deps
from app.api.contracts import ApiError
from app.credentials import (
    KIND_ACCESS,
    KIND_API_KEY,
    KIND_REFRESH,
    hash_secret,
    new_token,
    parse_token,
    secret_matches,
)
from app.web import login_throttle


router = APIRouter(tags=["auth"])

#: Stored on a login family when the client did not name the device.
DEFAULT_DEVICE_LABEL = "移动端"

_MAX_LABEL_LENGTH = 80


async def _json_body(request: Request) -> dict:
    try:
        payload = await request.json()
    except Exception as exc:
        raise ApiError("BODY_INVALID", "请求体必须是 JSON 对象") from exc
    if not isinstance(payload, dict):
        raise ApiError("BODY_INVALID", "请求体必须是 JSON 对象")
    return payload


def _client_key(request: Request) -> str:
    return request.client.host if request.client else "unknown"


async def _issue_pair(
    request: Request,
    *,
    label: str,
    family_id: str | None = None,
    rotated_from: int | None = None,
) -> dict[str, object]:
    """Mint an access/refresh pair and return the wire payload.

    The refresh row is written first so the access row can point back at it
    (`rotated_from`); both share the family id that `logout` revokes.
    """
    service = deps.system_settings_service(request)
    access_ttl = await service.mobile_access_ttl_seconds()
    refresh_ttl = await service.mobile_refresh_ttl_seconds()
    database = deps.database(request)
    family = family_id or secrets.token_hex(16)
    now = int(time.time())

    refresh_token, refresh_public, refresh_secret = new_token(KIND_REFRESH)
    refresh_id = await database.create_api_credential(
        kind=KIND_REFRESH,
        label=label,
        public_id=refresh_public,
        secret_hash=hash_secret(refresh_secret),
        family_id=family,
        expires_at=now + refresh_ttl,
    )

    access_token, access_public, access_secret = new_token(KIND_ACCESS)
    await database.create_api_credential(
        kind=KIND_ACCESS,
        label=label,
        public_id=access_public,
        secret_hash=hash_secret(access_secret),
        family_id=family,
        expires_at=now + access_ttl,
        rotated_from=refresh_id,
    )

    return {
        "token_type": "Bearer",
        "access_token": access_token,
        "refresh_token": refresh_token,
        "expires_in": access_ttl,
        "refresh_expires_in": refresh_ttl,
    }


@router.post("/auth/login")
async def login(request: Request) -> dict[str, object]:
    """Exchange the administrator password for an access/refresh pair.

    Shares `app.state.login_attempts` with the page login, so switching
    endpoints does not buy a fresh attempt budget.
    """
    payload = await _json_body(request)
    password = str(payload.get("password") or "")
    label = str(payload.get("label") or "").strip()[:_MAX_LABEL_LENGTH]
    if not password:
        raise ApiError(
            "AUTH_INVALID_CREDENTIALS", "请输入密码", status_code=401
        )

    attempts = request.app.state.login_attempts
    client_key = _client_key(request)
    now = time.monotonic()
    if login_throttle.is_locked(attempts, client_key, now):
        raise ApiError(
            "AUTH_LOCKED_OUT", "尝试次数过多，请稍后再试", status_code=429
        )
    failed_count = login_throttle.failed_count(attempts, client_key, now)

    admin_auth = await deps.database(request).get_admin_auth("admin")
    if admin_auth is None:
        raise ApiError(
            "SERVICE_UNAVAILABLE", "认证尚未配置", status_code=503
        )
    try:
        matches = await asyncio.to_thread(
            request.app.state.password_hasher.verify, password, admin_auth[0]
        )
    except PwdlibError as exc:
        raise ApiError(
            "SERVICE_UNAVAILABLE", "认证尚未配置", status_code=503
        ) from exc
    if not matches:
        if login_throttle.record_failure(attempts, client_key, now, failed_count):
            logging.getLogger(__name__).warning(
                "api_login_locked_out client=%s attempts=%d",
                client_key,
                failed_count + 1,
                extra={"error_code": "LOGIN_LOCKED_OUT"},
            )
        raise ApiError("AUTH_INVALID_CREDENTIALS", "密码不正确", status_code=401)
    if not admin_auth[1]:
        raise ApiError(
            "PASSWORD_CHANGE_REQUIRED",
            "请先在网页端修改初始密码",
            status_code=403,
        )
    login_throttle.clear(attempts, client_key)
    return await _issue_pair(request, label=label or DEFAULT_DEVICE_LABEL)


@router.post("/auth/refresh")
async def refresh(request: Request) -> dict[str, object]:
    """Rotate a refresh token into a fresh access/refresh pair."""
    payload = await _json_body(request)
    parsed = parse_token(str(payload.get("refresh_token") or ""))
    if parsed is None:
        raise ApiError(
            "AUTH_INVALID_CREDENTIALS", "刷新凭据无效", status_code=401
        )
    public_id, secret = parsed
    database = deps.database(request)
    record = await database.get_api_credential(public_id)
    if (
        record is None
        or record.kind != KIND_REFRESH
        or not secret_matches(secret, record.secret_hash)
    ):
        raise ApiError(
            "AUTH_INVALID_CREDENTIALS", "刷新凭据无效", status_code=401
        )
    if record.is_revoked:
        raise ApiError("AUTH_TOKEN_REVOKED", "刷新凭据已被撤销", status_code=401)
    if record.is_expired(int(time.time())):
        raise ApiError("AUTH_TOKEN_EXPIRED", "刷新凭据已过期", status_code=401)
    await database.revoke_api_credential(record.id)
    return await _issue_pair(
        request,
        label=record.label or DEFAULT_DEVICE_LABEL,
        family_id=record.family_id,
        rotated_from=record.id,
    )


@router.post("/auth/logout")
async def logout(request: Request) -> dict[str, object]:
    """Revoke the caller's login family.

    An API key is not a login family and is not revoked here -- keys are managed
    from the web UI -- so logging out with one is a no-op that still answers ok.
    """
    credential = await deps.require_bearer(request)
    if credential.kind != KIND_API_KEY and credential.family_id:
        await deps.database(request).revoke_credential_family(
            credential.family_id
        )
    return {"ok": True}


@router.get("/auth/whoami")
async def whoami(request: Request) -> dict[str, object]:
    """Who the caller is, for the client's 「测试连接」 and session display."""
    await deps.require_session(request)
    credential = getattr(request.state, "api_credential", None)
    if credential is None:
        return {
            "username": str(request.session.get("username") or "admin"),
            "kind": "session",
            "label": "",
            "expires_at": None,
            "must_change_password": bool(
                request.session.get("must_change_password")
            ),
        }
    return {
        "username": "admin",
        "kind": credential.kind,
        "label": credential.label,
        "expires_at": credential.expires_at,
        "must_change_password": False,
    }


__all__ = ["router"]
