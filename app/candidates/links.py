"""URL extraction for Telegram messages.

Telegram carries a hyperlink's target in a ``text_link`` entity, not in the
message text, so a channel that posts 「预览」 as a link leaves nothing for a
text regex to find. Both the ingestor and the candidate recomputation in
``Database`` read links through this module so the two agree on what counts.
"""

from __future__ import annotations

import re
from urllib.parse import urlsplit


GALLERY_URL_PATTERN = re.compile(
    r"https?://(?:exhentai\.org|e-hentai\.org)/g/(\d+)/([A-Za-z0-9]+)/?",
    flags=re.IGNORECASE,
)

# Closing punctuation is excluded so a URL wrapped in brackets, or sitting at
# the end of a sentence, does not absorb the trailing character. The two CJK
# blocks are 　-〿 (、。「」〈〉) and ＀-￯ (the fullwidth forms
# （）！？), which channels routinely wrap links in; no codepoint in either can
# appear in a URL.
_BARE_URL_PATTERN = re.compile(
    "https?://[^\\s<>\"'()\\[\\]　-〿＀-￯]+",
    re.IGNORECASE,
)

# Telegraph publishes on both domains; graph.org is the shorter alias.
PREVIEW_HOSTS = frozenset({"telegra.ph", "graph.org"})


def entity_urls(message: dict) -> tuple[str, ...]:
    """Collect the targets of every ``text_link`` entity in a message.

    ``entities`` covers text messages and ``caption_entities`` covers media
    captions; a message never has both, but reading both costs nothing.
    """
    urls: list[str] = []
    for key in ("entities", "caption_entities"):
        entities = message.get(key)
        if not isinstance(entities, list):
            continue
        for entity in entities:
            if not isinstance(entity, dict):
                continue
            if entity.get("type") != "text_link":
                continue
            url = str(entity.get("url") or "").strip()
            if url and url not in urls:
                urls.append(url)
    return tuple(urls)


def message_urls(message: dict, text: str) -> tuple[str, ...]:
    """Every URL a message offers: entity targets first, then bare text.

    Entity targets come first so a hyperlinked link wins over a bare one
    further down the caption.
    """
    urls = list(entity_urls(message))
    for match in _BARE_URL_PATTERN.finditer(text):
        url = match.group(0).rstrip(".,;:!?")
        if url and url not in urls:
            urls.append(url)
    return tuple(urls)


def normalize_preview_url(url: str) -> str | None:
    """Return the canonical form of a Telegraph page URL, or None.

    Rejects anything that is not an http(s) URL on a known Telegraph host, or
    that carries no page path. The fragment and query are dropped because a
    Telegraph page is identified by its path alone, which keeps the same page
    from being stored twice.
    """
    try:
        parts = urlsplit(url.strip())
    except ValueError:
        return None
    if parts.scheme.lower() not in {"http", "https"}:
        return None
    host = (parts.hostname or "").lower().removeprefix("www.")
    if host not in PREVIEW_HOSTS:
        return None
    path = parts.path.rstrip("/")
    if not path or path == "/":
        return None
    return f"https://{host}{path}"


def preview_urls(urls: tuple[str, ...]) -> tuple[str, ...]:
    """Filter and canonicalize the Telegraph page URLs out of a URL list."""
    found: list[str] = []
    for url in urls:
        normalized = normalize_preview_url(url)
        if normalized is not None and normalized not in found:
            found.append(normalized)
    return tuple(found)


def find_gallery_ref(
    urls: tuple[str, ...], text: str
) -> tuple[int, str] | None:
    """Find the first ExHentai gallery reference in the text or any URL."""
    match = GALLERY_URL_PATTERN.search(text)
    if match is None:
        for url in urls:
            match = GALLERY_URL_PATTERN.search(url)
            if match is not None:
                break
    if match is None:
        return None
    return int(match.group(1)), match.group(2)


__all__ = [
    "GALLERY_URL_PATTERN",
    "PREVIEW_HOSTS",
    "entity_urls",
    "find_gallery_ref",
    "message_urls",
    "normalize_preview_url",
    "preview_urls",
]
