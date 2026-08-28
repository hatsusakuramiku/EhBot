"""MTProto user-account access, for the files the Bot API cannot serve.

The Bot API refuses `getFile` above 20 MB. That ceiling is in the *protocol*,
not in the token, so no amount of retrying or proxying moves it -- but a regular
user account speaking MTProto has no such limit. This module is the second way
into Telegram: the bot keeps ingesting messages (it is the thing that receives
updates), and a configured user account is what actually fetches an oversized
archive.

Two properties shape the whole file.

**A user session is a full account credential, not a scoped token.** It is
stored through `SecretStore` like every other secret, and the only thing this
module ever reports outward is the account's own display identity -- never the
session string, never the phone number beyond what the operator typed.

**A bot `file_id` is worthless here.** File references are per-account in
MTProto, so the user client cannot resolve an id the bot handed out. It has to
re-read the message by `(chat_id, message_id)` and download the media it finds
there, which is why `download_message_media` takes those two numbers and why
the ingestor persists them on every attachment.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from app.connections.models import ProviderConnectionError


#: Telethon's own default. Named here because it is the number that makes this
#: module worth having: the Bot API stops at 20 MB, MTProto at 2 GB (4 GB with
#: Telegram Premium), and an operator reading an error needs the comparison.
MTPROTO_FILE_LIMIT = 2 * 1024 * 1024 * 1024

#: Download chunk size handed to Telethon. Matches the Bot API path's streaming
#: chunk so a progress callback reports at the same granularity on both routes.
_CHUNK_BYTES = 64 * 1024


@dataclass(frozen=True, slots=True)
class TelegramUserIdentity:
    """Who the stored session belongs to."""

    user_id: int
    username: str | None
    display_name: str

    @property
    def label(self) -> str:
        """What the connections tab shows beside the badge.

        A username when the account has one, the display name otherwise: an
        account with neither is possible and would otherwise render as an empty
        string where the operator expects to see who is logged in.
        """
        if self.username:
            return f"@{self.username}"
        return self.display_name or f"#{self.user_id}"


@dataclass(frozen=True, slots=True)
class LoginChallenge:
    """A login waiting on a code, and possibly a password after that.

    `phone_code_hash` is Telegram's handle for the code it just sent. It is
    carried in memory rather than persisted: a challenge that outlives the
    process is one an operator cannot complete anyway, because the code expires.
    """

    phone: str
    phone_code_hash: str
    requires_password: bool = False


class TelegramUserError(ProviderConnectionError):
    """A refusal from the MTProto path, in the shared connection vocabulary.

    Subclassing rather than raising `ProviderConnectionError` directly keeps the
    connection manager's existing `except` clauses working while letting the
    login routes tell a user-session failure from a bot-token one.
    """


def _client_factory_default(
    api_id: int, api_hash: str, session: str | None
) -> Any:
    """Build a real Telethon client. Imported lazily and injectable.

    The import is inside the function so that neither the module import graph
    nor a test that never logs in pays for Telethon's own imports, and so a
    deployment that leaves the user account unconfigured is unaffected by the
    dependency being present.
    """
    from telethon import TelegramClient
    from telethon.sessions import StringSession

    return TelegramClient(
        StringSession(session) if session else StringSession(),
        api_id,
        api_hash,
        # Two attempts, not the default five: a login route is answering an
        # operator who is watching the page, and a wrong api_id should say so
        # rather than hang for half a minute.
        connection_retries=2,
        request_retries=2,
    )


@dataclass(frozen=True, slots=True)
class TelegramUserCredentials:
    """The application identity a user session is created under.

    `api_id`/`api_hash` come from my.telegram.org and belong to the operator,
    not to this project: shipping a shared pair would put every deployment
    behind one rate limit and one revocation.
    """

    api_id: int
    api_hash: str

    @classmethod
    def parse(cls, api_id: str, api_hash: str) -> "TelegramUserCredentials":
        """Validate what the form submitted.

        Both refusals are shaped like the connection errors the page already
        renders, because a mistyped api_id is the most likely failure here and
        「无法连接」 would be a lie about it.
        """
        raw_id = (api_id or "").strip()
        raw_hash = (api_hash or "").strip()
        if not raw_id.isdigit():
            raise TelegramUserError(
                "TELEGRAM_USER_API_ID_INVALID",
                "API ID 必须是纯数字，可在 my.telegram.org 查看",
            )
        if len(raw_hash) < 32:
            raise TelegramUserError(
                "TELEGRAM_USER_API_HASH_INVALID",
                "API Hash 格式不正确，应为 32 位十六进制字符串",
            )
        return cls(api_id=int(raw_id), api_hash=raw_hash)


class TelegramUserClient:
    """A connected MTProto session, opened per operation.

    Telethon's client owns a socket and a background read loop, so it is opened
    and closed around each use rather than held for the process lifetime. A
    download takes minutes at most, and a session held open across an idle night
    is a session Telegram may drop out from under the next job -- reconnecting
    per job is both simpler and what makes a stored session survive a restart
    with no reconnection logic of its own.
    """

    def __init__(
        self,
        credentials: TelegramUserCredentials,
        session: str | None = None,
        *,
        client_factory: Callable[[int, str, str | None], Any] | None = None,
    ) -> None:
        self._credentials = credentials
        self._session = session
        self._client_factory = client_factory or _client_factory_default

    def _build(self) -> Any:
        return self._client_factory(
            self._credentials.api_id,
            self._credentials.api_hash,
            self._session,
        )

    async def _connect(self) -> Any:
        client = self._build()
        try:
            await client.connect()
        except Exception as exc:  # noqa: BLE001 - provider boundary
            raise _translate(exc) from exc
        return client

    @staticmethod
    async def _close(client: Any) -> None:
        """Disconnect, tolerating a client that is already gone.

        A failure here cannot be reported to anyone useful -- the operation it
        belongs to has already returned -- and letting it propagate would turn a
        successful download into a failed job.
        """
        try:
            result = client.disconnect()
            if asyncio.iscoroutine(result):
                await result
        except Exception:  # noqa: BLE001 - teardown is best-effort
            pass

    async def send_code(self, phone: str) -> LoginChallenge:
        """Ask Telegram to send a login code to `phone`."""
        cleaned = (phone or "").strip().replace(" ", "")
        if not cleaned.startswith("+") or not cleaned[1:].isdigit():
            raise TelegramUserError(
                "TELEGRAM_USER_PHONE_INVALID",
                "手机号需带国际区号，例如 +8613800138000",
            )
        client = await self._connect()
        try:
            sent = await client.send_code_request(cleaned)
        except Exception as exc:  # noqa: BLE001 - provider boundary
            raise _translate(exc) from exc
        finally:
            await self._close(client)
        return LoginChallenge(
            phone=cleaned, phone_code_hash=str(sent.phone_code_hash)
        )

    async def sign_in(
        self,
        challenge: LoginChallenge,
        *,
        code: str | None = None,
        password: str | None = None,
    ) -> tuple[str, TelegramUserIdentity]:
        """Complete a login, returning the session string and who it belongs to.

        The session string is returned rather than stored: this class has no
        access to the credential store on purpose, so the only place a session
        is written is the connection manager, next to every other secret.
        """
        client = await self._connect()
        try:
            if password:
                await client.sign_in(password=password)
            else:
                await client.sign_in(
                    phone=challenge.phone,
                    code=(code or "").strip(),
                    phone_code_hash=challenge.phone_code_hash,
                )
            identity = await _identity_of(client)
            session = client.session.save()
        except Exception as exc:  # noqa: BLE001 - provider boundary
            raise _translate(exc) from exc
        finally:
            await self._close(client)
        return str(session), identity

    async def verify(self) -> TelegramUserIdentity:
        """Confirm the stored session still authorises the account."""
        client = await self._connect()
        try:
            if not await client.is_user_authorized():
                raise TelegramUserError(
                    "TELEGRAM_USER_UNAUTHORIZED",
                    "用户会话已失效，请重新登录",
                )
            return await _identity_of(client)
        except TelegramUserError:
            raise
        except Exception as exc:  # noqa: BLE001 - provider boundary
            raise _translate(exc) from exc
        finally:
            await self._close(client)

    async def download_message_media(
        self,
        chat_id: int,
        message_id: int,
        destination: Path,
        *,
        progress: Callable[[int, int], None] | None = None,
    ) -> int:
        """Download the media on one message, by chat and message id.

        Not by `file_id`: MTProto file references are per-account, so an id the
        bot minted cannot be resolved here. Re-reading the message is also what
        keeps this honest about deletions -- a message the uploader removed
        fails as「消息已被删除」rather than as a mysterious download error.
        """
        client = await self._connect()
        try:
            if not await client.is_user_authorized():
                raise TelegramUserError(
                    "TELEGRAM_USER_UNAUTHORIZED",
                    "用户会话已失效，请重新登录",
                )
            message = await client.get_messages(chat_id, ids=message_id)
            if message is None or not getattr(message, "media", None):
                raise TelegramUserError(
                    "TELEGRAM_USER_MESSAGE_GONE",
                    "源消息已被删除或不再包含附件，无法用用户账户下载",
                )
            destination.parent.mkdir(parents=True, exist_ok=True)
            written = await client.download_media(
                message,
                file=str(destination),
                part_size_kb=_CHUNK_BYTES // 1024,
                progress_callback=progress,
            )
        except TelegramUserError:
            destination.unlink(missing_ok=True)
            raise
        except Exception as exc:  # noqa: BLE001 - provider boundary
            destination.unlink(missing_ok=True)
            raise _translate(exc) from exc
        finally:
            await self._close(client)
        if written is None or not destination.exists():
            destination.unlink(missing_ok=True)
            raise TelegramUserError(
                "TELEGRAM_USER_DOWNLOAD_FAILED",
                "用户账户下载未产生文件",
            )
        return destination.stat().st_size


async def _identity_of(client: Any) -> TelegramUserIdentity:
    me = await client.get_me()
    if me is None:
        raise TelegramUserError(
            "TELEGRAM_USER_UNAUTHORIZED", "用户会话已失效，请重新登录"
        )
    first = str(getattr(me, "first_name", "") or "")
    last = str(getattr(me, "last_name", "") or "")
    return TelegramUserIdentity(
        user_id=int(me.id),
        username=(str(me.username) if getattr(me, "username", None) else None),
        display_name=(f"{first} {last}".strip() or f"#{int(me.id)}"),
    )


#: Telethon exception class names mapped to an operator-facing refusal. Matched
#: by name rather than by `isinstance` so this table needs no imports from
#: Telethon: the module must stay importable, and testable, without it.
_ERROR_BY_NAME: dict[str, tuple[str, str]] = {
    "ApiIdInvalidError": (
        "TELEGRAM_USER_API_ID_INVALID",
        "API ID 与 API Hash 不匹配，请在 my.telegram.org 核对",
    ),
    "PhoneNumberInvalidError": (
        "TELEGRAM_USER_PHONE_INVALID",
        "Telegram 不认识这个手机号",
    ),
    "PhoneNumberBannedError": (
        "TELEGRAM_USER_PHONE_BANNED",
        "该手机号已被 Telegram 封禁",
    ),
    "PhoneCodeInvalidError": (
        "TELEGRAM_USER_CODE_INVALID",
        "验证码不正确，请重新输入",
    ),
    "PhoneCodeExpiredError": (
        "TELEGRAM_USER_CODE_EXPIRED",
        "验证码已过期，请重新获取",
    ),
    "SessionPasswordNeededError": (
        "TELEGRAM_USER_PASSWORD_NEEDED",
        "该账户开启了两步验证，请输入密码",
    ),
    "PasswordHashInvalidError": (
        "TELEGRAM_USER_PASSWORD_INVALID",
        "两步验证密码不正确",
    ),
    "AuthKeyUnregisteredError": (
        "TELEGRAM_USER_UNAUTHORIZED",
        "用户会话已在 Telegram 端被注销，请重新登录",
    ),
    "AuthKeyDuplicatedError": (
        "TELEGRAM_USER_UNAUTHORIZED",
        "该会话已在别处使用，请重新登录",
    ),
    "UserDeactivatedBanError": (
        "TELEGRAM_USER_UNAUTHORIZED",
        "该 Telegram 账户已被停用",
    ),
    "ChannelPrivateError": (
        "TELEGRAM_USER_NO_ACCESS",
        "登录的账户不在该频道中，无法读取原始消息",
    ),
    "ChatForbiddenError": (
        "TELEGRAM_USER_NO_ACCESS",
        "登录的账户无权读取该会话",
    ),
    "FileReferenceExpiredError": (
        "TELEGRAM_USER_FILE_REFERENCE_EXPIRED",
        "文件引用已过期，请重试该任务",
    ),
}


def _translate(exc: Exception) -> ProviderConnectionError:
    """Turn a Telethon exception into a named, operator-facing refusal.

    `FloodWaitError` carries the wait in seconds and becomes `retry_after`, so
    the download queue's existing backoff handling applies to it unchanged
    rather than hammering a rate-limited account.
    """
    if isinstance(exc, ProviderConnectionError):
        return exc
    name = type(exc).__name__
    if name == "FloodWaitError":
        seconds = int(getattr(exc, "seconds", 0) or 0)
        return TelegramUserError(
            "TELEGRAM_USER_RATE_LIMITED",
            f"Telegram 限流，请等待约 {seconds} 秒后重试",
            retry_after=seconds or None,
        )
        # `retry_after` is what the worker's backoff table already reads.
    known = _ERROR_BY_NAME.get(name)
    if known is not None:
        return TelegramUserError(*known)
    if isinstance(exc, (OSError, asyncio.TimeoutError)):
        return TelegramUserError(
            "TELEGRAM_USER_UNREACHABLE", "暂时无法连接 Telegram"
        )
    return TelegramUserError(
        "TELEGRAM_USER_FAILED", "用户账户操作失败，请稍后重试"
    )


__all__ = [
    "MTPROTO_FILE_LIMIT",
    "LoginChallenge",
    "TelegramUserClient",
    "TelegramUserCredentials",
    "TelegramUserError",
    "TelegramUserIdentity",
]
