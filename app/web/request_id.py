"""Correlation id for one request, in one place.

A single operator action can touch a page route, the review orchestrator and a
worker, and until now nothing tied those log lines together: reading the log
after a failed approval meant guessing which records belonged to it.

The id is generated per request, published on `logging.request_id_var` so every
existing `logging.getLogger(__name__)` call inside the request picks it up
without being edited, and echoed in the `X-Request-ID` response header so an
operator can quote it from a browser's network tab.

An inbound `X-Request-ID` is honoured **only** when proxy headers are trusted,
for the same reason `X-Forwarded-For` is: a value a client can set is a value a
client can use to forge or collide with someone else's identifier. Untrusted
input is replaced rather than rejected, because a request is still worth serving
when its optional header is unusable. Even trusted values are length-clamped and
stripped to a safe alphabet before reaching a log line.
"""

from __future__ import annotations

import re

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

from app.logging import new_request_id, request_id_var

HEADER_NAME = "X-Request-ID"

#: Length beyond which a supplied id is discarded rather than truncated: a
#: truncated id is a *different* id that looks legitimate, which is worse than
#: generating a fresh one.
MAX_SUPPLIED_LENGTH = 64

_SAFE_ID = re.compile(r"\A[A-Za-z0-9._:-]+\Z")


def _clean_supplied_id(raw: str | None) -> str | None:
    if raw is None:
        return None
    candidate = raw.strip()
    if not candidate or len(candidate) > MAX_SUPPLIED_LENGTH:
        return None
    if not _SAFE_ID.match(candidate):
        return None
    return candidate


class RequestIdMiddleware(BaseHTTPMiddleware):
    def __init__(self, app, *, trust_inbound: bool = False) -> None:
        super().__init__(app)
        self._trust_inbound = trust_inbound

    async def dispatch(self, request: Request, call_next) -> Response:
        supplied = (
            _clean_supplied_id(request.headers.get(HEADER_NAME))
            if self._trust_inbound
            else None
        )
        request_id = supplied or new_request_id()
        token = request_id_var.set(request_id)
        try:
            response = await call_next(request)
        finally:
            # Reset on the way out so a task that outlives the request does not
            # keep writing this id, and so the worker loops never inherit one.
            request_id_var.reset(token)
        response.headers[HEADER_NAME] = request_id
        return response