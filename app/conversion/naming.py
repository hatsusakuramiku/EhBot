from __future__ import annotations

import re
import unicodedata


# Reserved on Windows and unsafe in path segments everywhere else.
_UNSAFE_CHARACTERS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_WINDOWS_RESERVED: frozenset[str] = frozenset(
    {
        "con",
        "prn",
        "aux",
        "nul",
        *(f"com{index}" for index in range(1, 10)),
        *(f"lpt{index}" for index in range(1, 10)),
    }
)

MAX_SEGMENT_LENGTH = 120


def safe_library_name(value: str, *, fallback: str) -> str:
    """Normalize a metadata value into one safe library path segment."""
    normalized = unicodedata.normalize("NFC", value or "").strip()
    cleaned = _UNSAFE_CHARACTERS.sub(" ", normalized)
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" .")
    if cleaned.lower() in _WINDOWS_RESERVED:
        cleaned = f"{cleaned}-archive"
    if len(cleaned) > MAX_SEGMENT_LENGTH:
        cleaned = cleaned[:MAX_SEGMENT_LENGTH].rstrip(" .")
    return cleaned or fallback


__all__ = ["MAX_SEGMENT_LENGTH", "safe_library_name"]