"""Response envelope and paging contracts shared by every JSON endpoint.

Two shapes are fixed here so the browser never has to special-case a route:

* a failure is always ``{"error": {"code", "message", "details"}}``
* a list is always ``{"items", "total", "page", "page_size", "pages"}``

The existing HTML routes keep their own redirect-and-rerender behaviour; these
contracts apply to `/api/v1` only.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import logging
import math
from typing import Any

from fastapi import Request
from fastapi.responses import JSONResponse


#: Upper bound for a single page. A caller asking for more gets this instead of
#: an error: the request is still answerable, and refusing it would only push
#: the caller into paging loops that hit the database harder.
MAX_PAGE_SIZE = 200

DEFAULT_PAGE_SIZE = 50


class ApiError(Exception):
    """A failure that maps to the JSON error envelope.

    Carries an HTTP status alongside a stable machine-readable ``code`` so the
    interface can branch on the code and show ``message`` verbatim. Existing
    domain errors (``DownloadError``, ``ReviewError``, ``ConversionError`` and
    friends) already expose ``code``/``public_message``, so they translate into
    this without inventing new vocabulary.
    """

    def __init__(
        self,
        code: str,
        message: str,
        *,
        status_code: int = 400,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code
        self.details = details or {}

    def to_payload(self) -> dict[str, Any]:
        return {
            "error": {
                "code": self.code,
                "message": self.message,
                "details": self.details,
            }
        }


async def api_error_handler(_: Request, exc: Exception) -> JSONResponse:
    """Render an :class:`ApiError` as the standard envelope.

    Registered for `ApiError` only. Anything else keeps FastAPI's own
    behaviour, because swallowing unknown exceptions here would hide real bugs
    behind a tidy 400.
    """
    assert isinstance(exc, ApiError)  # noqa: S101 - handler is registered for this type
    logging.getLogger(__name__).warning(
        "api_error", extra={"error_code": exc.code}
    )
    return JSONResponse(status_code=exc.status_code, content=exc.to_payload())


@dataclass(frozen=True, slots=True)
class PageParams:
    """Validated paging window.

    Built through :meth:`clamp` rather than the constructor so a hand-typed
    query string can never produce a negative offset or an unbounded scan.
    """

    page: int = 1
    page_size: int = DEFAULT_PAGE_SIZE

    @classmethod
    def clamp(
        cls,
        page: int | None,
        page_size: int | None,
        *,
        max_page_size: int = MAX_PAGE_SIZE,
    ) -> PageParams:
        resolved_page = 1 if page is None or page < 1 else int(page)
        if page_size is None or page_size < 1:
            resolved_size = DEFAULT_PAGE_SIZE
        else:
            resolved_size = min(int(page_size), max_page_size)
        return cls(page=resolved_page, page_size=resolved_size)

    @property
    def offset(self) -> int:
        return (self.page - 1) * self.page_size

    @property
    def limit(self) -> int:
        return self.page_size


@dataclass(frozen=True, slots=True)
class Page:
    """A single page of results plus the counters the interface needs.

    ``pages`` is derived rather than supplied so it can never disagree with
    ``total`` and ``page_size``.
    """

    items: list[Any] = field(default_factory=list)
    total: int = 0
    page: int = 1
    page_size: int = DEFAULT_PAGE_SIZE

    @classmethod
    def of(
        cls, items: list[Any], total: int, params: PageParams
    ) -> Page:
        return cls(
            items=list(items),
            total=int(total),
            page=params.page,
            page_size=params.page_size,
        )

    @property
    def pages(self) -> int:
        if self.page_size <= 0:
            return 0
        return int(math.ceil(self.total / self.page_size))

    def to_payload(self) -> dict[str, Any]:
        return {
            "items": self.items,
            "total": self.total,
            "page": self.page,
            "page_size": self.page_size,
            "pages": self.pages,
        }


__all__ = [
    "ApiError",
    "DEFAULT_PAGE_SIZE",
    "MAX_PAGE_SIZE",
    "Page",
    "PageParams",
    "api_error_handler",
]