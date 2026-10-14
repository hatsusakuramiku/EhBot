"""Account-scoped adapters for the existing Telegram ingestion loops."""
import json

from app.connections.manager import ConnectionManager
from app.connections.telegram import TelegramBotApi
from app.connections.runtime import AccountRuntime, current_account_context


class _BorrowedSecrets:
    """Read-only legacy names, resolved exclusively from a borrowed snapshot."""

    def read(self, name):
        context = current_account_context.get()
        if context is None:
            raise RuntimeError("Account credential read requires a runtime lease")
        values = context.values
        if name == "telegram_user_session":
            return values.get("session")
        if name == "telegram_user_api":
            if "api_id" not in values or "api_hash" not in values:
                return None
            return f"{values['api_id']}:{values['api_hash']}"
        if name == "telegram_bot_token":
            return values.get("bot_token")
        if name == "exhentai_cookies":
            return json.dumps({name: values[name] for name in ("ipb_member_id", "ipb_pass_hash", "igneous") if name in values})
        raise ValueError("Unsupported account credential name")

    def is_configured(self, name):
        return self.read(name) is not None


class AccountListener(ConnectionManager):
    """Reuse ingestion only; account service owns verification/configuration.

    The store is read-only and contains no legacy files. Each account has its
    own task, entity cache and capability state; simultaneous activation is
    gated until P6's ingestion identity/deduplication is ready.
    """

    def __init__(self, account_id: str, runtime: AccountRuntime, database, **kwargs):
        super().__init__(_BorrowedSecrets(), database, **kwargs)
        self.account_id = account_id
        self._runtime = runtime

    async def _poll_telegram(self):
        async with self._runtime.use(self.account_id) as context:
            api = TelegramBotApi(context.values["bot_token"], self._telegram_client)
            await super()._poll_telegram(api)

    async def _ingest_with_user_account(self):
        async with self._runtime.use(self.account_id):
            return await super()._ingest_with_user_account()

    async def list_telegram_user_dialogs(self):
        async with self._runtime.use(self.account_id):
            return await super().list_telegram_user_dialogs()
