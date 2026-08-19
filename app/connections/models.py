from dataclasses import dataclass
from typing import Literal


class ProviderConnectionError(Exception):
    def __init__(self, code: str, public_message: str) -> None:
        super().__init__(public_message)
        self.code = code
        self.public_message = public_message


@dataclass(frozen=True, slots=True)
class TelegramBotIdentity:
    bot_id: int
    username: str
    display_name: str


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
