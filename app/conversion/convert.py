from __future__ import annotations


class ConversionError(ValueError):
    """A conversion task failed with a stable, operator-facing error code."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.public_message = message


__all__ = ["ConversionError"]