"""Single administrator identity and shared credential validation."""

from __future__ import annotations

import asyncio
import logging
import time
import unicodedata
from dataclasses import dataclass, field

from pwdlib.exceptions import PwdlibError

from app.web import login_throttle


@dataclass(frozen=True, slots=True)
class AdminAuth:
    id: int
    username: str
    password_hash: str = field(repr=False)
    password_changed: bool
    revision: int
    auth_epoch: int

    def profile(self) -> dict:
        return {
            "username": self.username,
            "revision": self.revision,
            "must_change_password": not self.password_changed,
        }


class AuthError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def normalize_username(value: str) -> str:
    """Trim the login name; preserve case and reject spaces/control characters."""
    name = value.strip()
    if not 1 <= len(name) <= 64 or any(
        char.isspace() or unicodedata.category(char).startswith("C")
        for char in name
    ):
        raise AuthError("USERNAME_INVALID", "账户名须为 1–64 个字符，不能包含空白或控制字符")
    return name


async def verify_password(hasher, password: str, admin: AdminAuth) -> bool:
    try:
        return await asyncio.to_thread(hasher.verify, password, admin.password_hash)
    except PwdlibError as exc:
        raise AuthError("AUTH_NOT_CONFIGURED", "认证尚未配置") from exc


async def authenticate(database, hasher, username, password, attempts, client_key):
    """Verify both fields with the same address throttle for every login entry.

    Passwords are not trimmed. Re-read the administrator revision when issuing
    credentials: a successful hash check alone does not authorize a stale login.
    """
    now = time.monotonic()
    if login_throttle.is_locked(attempts, client_key, now):
        raise AuthError("AUTH_LOCKED_OUT", "尝试次数过多，请稍后再试")
    login_throttle.failed_count(attempts, client_key, now)
    admin = await database.get_administrator()
    if admin is None:
        raise AuthError("AUTH_NOT_CONFIGURED", "认证尚未配置")
    try:
        name = normalize_username(username)
    except AuthError:
        name = ""
    matches = bool(password) and await verify_password(hasher, password, admin)
    if not matches or name != admin.username:
        # Read after the asynchronous hash check so simultaneous failures do
        # not overwrite each other's counter from an earlier snapshot.
        count = login_throttle.failed_count(attempts, client_key, now)
        if login_throttle.record_failure(attempts, client_key, now, count):
            logging.getLogger(__name__).warning(
                "login_locked_out client=%s attempts=%d", client_key, count + 1,
                extra={"error_code": "LOGIN_LOCKED_OUT"},
            )
        raise AuthError("AUTH_INVALID_CREDENTIALS", "账户名或密码不正确")
    if login_throttle.is_locked(attempts, client_key, time.monotonic()):
        raise AuthError("AUTH_LOCKED_OUT", "尝试次数过多，请稍后再试")
    login_throttle.clear(attempts, client_key)
    return admin


async def validate_session(database, session: dict) -> AdminAuth | None:
    """Validate the fixed administrator and this browser's stored login session."""
    if not session.get("authenticated"):
        return None
    admin = await database.browser_session_admin(session.get("session_id", ""))
    if admin is None or session.get("admin_id") != admin.id:
        return None
    if session.get("auth_epoch") != admin.auth_epoch:
        return None
    session["username"] = admin.username
    session["must_change_password"] = not admin.password_changed
    return admin


async def modify_administrator(
    database, hasher, *, current_password: str, expected_revision: int,
    username: str | None = None, new_password: str | None = None,
    confirmation: str | None = None,
    actor_session_id: str | None = None, actor_credential_id: int | None = None,
    renew_session_id: str | None = None,
) -> AdminAuth:
    """Verify a configuration snapshot, then conditionally commit its changes."""
    if type(expected_revision) is not int or expected_revision < 0:
        raise AuthError("REVISION_REQUIRED", "请刷新后提交管理员配置版本")
    admin = await database.get_administrator()
    if admin is None:
        raise AuthError("AUTH_NOT_CONFIGURED", "认证尚未配置")
    if expected_revision != admin.revision:
        raise AuthError("REVISION_CONFLICT", "管理员配置已更新，请核对后重新提交")
    if not await verify_password(hasher, current_password, admin):
        raise AuthError("AUTH_INVALID_CREDENTIALS", "当前密码不正确")
    name = normalize_username(username) if username is not None else None
    password_hash = None
    if new_password is not None:
        if len(new_password) < 12:
            raise AuthError("PASSWORD_INVALID", "新密码至少需要 12 个字符")
        if new_password != confirmation:
            raise AuthError("PASSWORD_INVALID", "两次输入的新密码不一致")
        password_hash = await asyncio.to_thread(hasher.hash, new_password)
    return await database.update_administrator(
        admin, username=name, password_hash=password_hash,
        actor_session_id=actor_session_id, actor_credential_id=actor_credential_id,
        renew_session_id=renew_session_id,
    )
