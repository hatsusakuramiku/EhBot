"""Validate signed browser sessions before any HTTP page or command runs."""

from app.admin_auth import validate_session


class AdminSessionMiddleware:
    def __init__(self, app, database):
        self.app = app
        self.database = database

    async def __call__(self, scope, receive, send):
        if scope["type"] in {"http", "websocket"}:
            session = scope.get("session", {})
            if session.get("authenticated"):
                # Legacy cookies have no revocable device identity: require a new login.
                if await validate_session(self.database, session) is None:
                    session.clear()
        await self.app(scope, receive, send)
