"""Thumbnail domain errors."""

from __future__ import annotations


class ThumbnailError(ValueError):
    """Raised for a thumbnail that cannot be served.

    These are caught and translated to either a placeholder or a 4xx/5xx at
    the API boundary. Never passes through to the default error handler.
    """

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.public_message = message
        super().__init__(f"[{code}] {message}")


__all__ = ["ThumbnailError"]