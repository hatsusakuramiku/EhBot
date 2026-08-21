from __future__ import annotations

from dataclasses import dataclass, field
from html.parser import HTMLParser
import re


SECTION_LABELS = (
    "\u7c7b\u578b",
    "\u8bed\u8a00",
    "\u539f\u4f5c",
    "\u56e2\u961f",
    "\u827a\u672f\u5bb6",
    "\u89d2\u8272",
    "\u7537\u6027",
    "\u5973\u6027",
    "\u6df7\u5408",
    "\u5176\u4ed6",
    "\u8bc4\u5206",
    "\u9875\u6570",
    "\u6536\u85cf\u6570",
    "\u6742\u9879",
    "\u91cd\u65b0\u5206\u7c7b",
    "\u9884\u89c8",
    "\u539f\u59cb\u5730\u5740",
)


CATEGORY_LABELS: dict[str, str] = {
    "\u540c\u4eba\u5fd7": "doujinshi",
    "\u5355\u884c\u672c": "manga",
    "\u753b\u96c6": "artistcg",
    "\u6742\u5fd7": "artistcg",
    "\u56fe\u96c6": "imageset",
    "cosplay": "cosplay",
    "asianporn": "asianporn",
    "western": "western",
    "gamecg": "gamecg",
}


LANGUAGE_LABELS: dict[str, str] = {
    "\u6c49\u8bed": "chinese",
    "\u65e5\u8bed": "japanese",
    "\u82f1\u8bed": "english",
    "\u97e9\u8bed": "korean",
    "\u7ffb\u8bd1": "translated",
    "\u91cd\u8bd1": "rewrite",
}


GALLERY_URL_RE = re.compile(
    r"https?://(?:exhentai\.org|e-hentai\.org)/g/(\d+)/([A-Za-z0-9]+)/?",
    flags=re.IGNORECASE,
)

_HASHTAG_TOKEN_RE = re.compile(r"#([^\s,#\[\]<>()]+)")
_TRAILING_MARKDOWN_LINK_RE = re.compile(r"\s*\[[^\]]+\]\(https?://[^\s)]+\)\s*$")
_SECTION_LINE_RE = re.compile(
    r"^("
    + "|".join(re.escape(label) for label in SECTION_LABELS)
    + r")\s*:?(.*)$",
    flags=re.MULTILINE,
)


@dataclass(frozen=True, slots=True)
class ParsedReferenceMessage:
    is_reference: bool
    title: str | None = None
    ex_gid: int | None = None
    ex_gallery_token: str | None = None
    metadata: dict[str, str] = field(default_factory=dict)


class _TelegramTextExtractor(HTMLParser):
    '''Strip Telegram Desktop chat export HTML to readable plain text.'''

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._chunks: list[str] = []
        self._skip_depth = 0

    def handle_starttag(self, tag: str, attrs) -> None:
        if tag in {"script", "style"}:
            self._skip_depth += 1

    def handle_endtag(self, tag: str) -> None:
        if tag in {"script", "style"} and self._skip_depth > 0:
            self._skip_depth -= 1

    def handle_data(self, data: str) -> None:
        if self._skip_depth:
            return
        text = data.strip()
        if text:
            self._chunks.append(text)

    @property
    def text(self) -> str:
        return "\n".join(self._chunks)


def html_to_plain_text(html: str) -> str:
    extractor = _TelegramTextExtractor()
    extractor.feed(html)
    return extractor.text


def _normalize_token(token: str) -> str:
    return token.strip().strip("#").strip().lower()


def _match_label(raw: str, mapping: dict[str, str]) -> str | None:
    normalized = raw.strip().strip(":").strip().lower()
    if not normalized:
        return None
    return mapping.get(normalized)


def _extract_tokens(value: str) -> list[str]:
    tokens: list[str] = []
    for item in _HASHTAG_TOKEN_RE.findall(value):
        token = _normalize_token(item)
        if token:
            tokens.append(token)
    return tokens


def _clean_preview(remainder: str) -> str | None:
    cleaned = _TRAILING_MARKDOWN_LINK_RE.sub("", remainder).strip()
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned or None


def parse_reference_message(text: str) -> ParsedReferenceMessage:
    '''Detect and parse a Telegram channel reference message.

    Returns a ParsedReferenceMessage with ``is_reference=False`` when the
    supplied text does not look like a structured reference message.
    '''
    if not text:
        return ParsedReferenceMessage(is_reference=False)

    if not _SECTION_LINE_RE.search(text):
        return ParsedReferenceMessage(is_reference=False)

    gallery_match = GALLERY_URL_RE.search(text)
    if gallery_match is None:
        return ParsedReferenceMessage(is_reference=False)

    metadata: dict[str, str] = {}
    tag_buckets: list[str] = []
    title: str | None = None

    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        label_match = _SECTION_LINE_RE.match(line)
        if not label_match:
            continue
        label = label_match.group(1)
        remainder = (label_match.group(2) or "").strip()

        if label == "\u9884\u89c8":
            title = _clean_preview(remainder)
            continue
        if label == "\u539f\u59cb\u5730\u5740":
            continue

        tokens = _extract_tokens(remainder)
        if not tokens:
            continue

        if label == "\u7c7b\u578b":
            for token in tokens:
                mapped = _match_label(token, CATEGORY_LABELS)
                if mapped:
                    metadata.setdefault("Category", mapped)
                    break
        elif label == "\u8bed\u8a00":
            languages = [
                value
                for token in tokens
                for value in [_match_label(token, LANGUAGE_LABELS)]
                if value
            ]
            primary_language = next(
                (value for value in languages if value != "translated"),
                None,
            )
            if primary_language is not None:
                metadata.setdefault("Language", primary_language)
            tag_buckets.extend(
                token for token in tokens
                if not _match_label(token, LANGUAGE_LABELS)
            )
        elif label in ("\u539f\u4f5c", "\u56e2\u961f"):
            for token in tokens:
                metadata.setdefault("Group", token)
                break
        elif label == "\u827a\u672f\u5bb6":
            for token in tokens:
                metadata.setdefault("Artist", token)
                break
        elif label == "\u89d2\u8272":
            tag_buckets.extend(f"character:{token}" for token in tokens)
        elif label == "\u7537\u6027":
            tag_buckets.extend(f"male:{token}" for token in tokens)
        elif label == "\u5973\u6027":
            tag_buckets.extend(f"female:{token}" for token in tokens)
        elif label == "\u6df7\u5408":
            tag_buckets.extend(f"mixed:{token}" for token in tokens)
        elif label in ("\u5176\u4ed6", "\u6742\u9879"):
            tag_buckets.extend(tokens)

    if not metadata and not tag_buckets:
        return ParsedReferenceMessage(is_reference=False)

    if tag_buckets:
        seen: set[str] = set()
        deduped: list[str] = []
        for token in tag_buckets:
            if token in seen:
                continue
            seen.add(token)
            deduped.append(token)
        metadata.setdefault("Tags", ", ".join(deduped))

    return ParsedReferenceMessage(
        is_reference=True,
        title=title,
        ex_gid=int(gallery_match.group(1)),
        ex_gallery_token=gallery_match.group(2),
        metadata=metadata,
    )


__all__ = [
    "CATEGORY_LABELS",
    "LANGUAGE_LABELS",
    "SECTION_LABELS",
    "ParsedReferenceMessage",
    "GALLERY_URL_RE",
    "html_to_plain_text",
    "parse_reference_message",
]
