"""Thumbnail data-transfer objects.

These are plain dataclasses, not ORM models. The database layer returns
``ThumbnailRow``, and the service resolves it to ``ThumbnailResult`` for the
endpoint.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ThumbnailRow:
    """One row from the ``thumbnails`` table, as the service needs it."""

    hash: str
    kind: str
    variant: str
    source_url: str | None
    source_path: str | None
    state: str
    content_type: str | None
    byte_size: int | None
    width: int | None
    height: int | None
    error_code: str | None
    attempt_count: int


@dataclass(frozen=True, slots=True)
class ThumbnailResult:
    """What the endpoint needs to send back for one thumbnail."""

    file_path: str | None
    content_type: str
    byte_size: int | None
    width: int | None
    height: int | None
    state: str
    error_code: str | None


__all__ = ["ThumbnailResult", "ThumbnailRow"]