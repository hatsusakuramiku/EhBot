from dataclasses import dataclass, field
from typing import Literal


class ProviderConnectionError(Exception):
    def __init__(
        self,
        code: str,
        public_message: str,
        retry_after: int | None = None,
    ) -> None:
        super().__init__(public_message)
        self.code = code
        self.public_message = public_message
        self.retry_after = retry_after


@dataclass(frozen=True, slots=True)
class TelegramBotIdentity:
    bot_id: int
    username: str
    display_name: str


@dataclass(frozen=True, slots=True)
class TelegramUserAccount:
    """The configured MTProto account, as the interface needs to see it.

    Separate from `ProviderStatus` because a user session answers one more
    question than a connection does: whether a login is half-finished. An
    operator who requested a code and closed the tab has to be able to tell that
    from 「尚未配置」, or the only way forward looks like starting over.
    """

    state: Literal[
        "not_configured", "awaiting_code", "awaiting_password", "connected", "error"
    ]
    configured: bool
    identity: str | None = None
    error: str | None = None
    #: The number the pending code was sent to, so the form can name it. Only
    #: ever the value the operator typed -- never read back out of a session.
    phone: str | None = None


@dataclass(frozen=True, slots=True)
class ProviderStatus:
    state: Literal["not_configured", "connecting", "connected", "error"]
    configured: bool
    identity: str | None = None
    error: str | None = None


@dataclass(frozen=True, slots=True)
class ConnectionSnapshot:
    telegram: ProviderStatus
    exhentai: ProviderStatus
    #: The MTProto user account. Defaulted so that every existing constructor
    #: call -- and every stored expectation of a two-provider snapshot -- keeps
    #: working: a deployment that never configures one is the normal case.
    telegram_user: TelegramUserAccount = field(
        default_factory=lambda: TelegramUserAccount(
            state="not_configured", configured=False
        )
    )
