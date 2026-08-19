from __future__ import annotations

import re
from dataclasses import dataclass
from html.parser import HTMLParser
from typing import Iterable


@dataclass(frozen=True, slots=True)
class GalleryMetadata:
    title: str
    title_japanese: str | None
    category: str | None
    uploader: str | None
    rating: float | None
    language: str | None
    artists: tuple[str, ...]
    groups: tuple[str, ...]
    tags: tuple[str, ...]
    page_count: int | None
    description: str | None


class _GalleryTextExtractor(HTMLParser):
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


def _extract_with_html(html: str, pattern: str) -> str | None:
    match = re.search(pattern, html, flags=re.IGNORECASE | re.DOTALL)
    return match.group(1).strip() if match else None


def _extract_all_with_html(html: str, pattern: str) -> tuple[str, ...]:
    matches = re.findall(pattern, html, flags=re.IGNORECASE | re.DOTALL)
    seen: list[str] = []
    for value in matches:
        cleaned = re.sub(r"\s+", " ", value).strip()
        if cleaned and cleaned not in seen:
            seen.append(cleaned)
    return tuple(seen)


def _parse_rating(value: str | None) -> float | None:
    if not value:
        return None
    match = re.search(r"([0-9]+\.[0-9]+)", value)
    return float(match.group(1)) if match else None


def _parse_pages(value: str | None) -> int | None:
    if not value:
        return None
    digits = re.search(r"([0-9]+)\s*pages?", value, flags=re.IGNORECASE)
    return int(digits.group(1)) if digits else None


def parse_gallery_html(html: str) -> GalleryMetadata | None:
    extractor = _GalleryTextExtractor()
    extractor.feed(html)
    text = extractor.text

    title = (
        _extract_with_html(html, r'<h1[^>]*id="gn"[^>]*>(.*?)</h1>')
        or _extract_with_html(html, r'<h1[^>]*id="gj"[^>]*>(.*?)</h1>')
    )
    if title is None:
        match = re.search(r"<title>([^<]+)</title>", html, flags=re.IGNORECASE)
        if match:
            title = match.group(1).split("|", 1)[0].strip()
    if not title:
        return None

    title_japanese = _extract_with_html(
        html, r'<h1[^>]*id="gj"[^>]*>(.*?)</h1>'
    )

    category_match = re.search(
        r'category[\'"]?\s*[:=]\s*[\'"]?([a-z0-9_\-]+)',
        html,
        flags=re.IGNORECASE,
    )
    category = (
        category_match.group(1)
        if category_match
        else _extract_with_html(
            html, r'class="ic[^"]*"[^>]*>\s*<[^>]*>([^<]+)</a>'
        )
    )

    uploader = _extract_with_html(
        html, r'class="gder"[^>]*>\s*<[^>]*>([^<]+)</[^>]*>'
    ) or _extract_with_html(
        html, r"Posted by[^<]*<a[^>]*>([^<]+)</a>"
    )

    rating = _parse_rating(
        _extract_with_html(html, r'class="rating"[^>]*>([^<]+)')
    )

    language_match = re.search(
        r"<tr>[^<]*<td[^>]*>\s*Language\s*</td>[^<]*<td[^>]*>(.*?)</td>",
        html,
        flags=re.IGNORECASE | re.DOTALL,
    )
    language = None
    if language_match:
        lang_text = re.sub(r"<[^>]+>", " ", language_match.group(1)).strip()
        lang_attr = re.search(r'title="([^"]+)"', language_match.group(1))
        language = lang_attr.group(1) if lang_attr else (lang_text or None)

    artist_value = re.search(
        r"<tr>[^<]*<td[^>]*>\s*Artist\s*</td>[^<]*<td[^>]*>(.*?)</td>",
        html,
        flags=re.IGNORECASE | re.DOTALL,
    )
    artists: tuple[str, ...] = ()
    if artist_value:
        artists = _extract_all_with_html(
            artist_value.group(1), r'<a[^>]*>([^<]+)</a>'
        )

    group_value = re.search(
        r"<tr>[^<]*<td[^>]*>\s*Group\s*</td>[^<]*<td[^>]*>(.*?)</td>",
        html,
        flags=re.IGNORECASE | re.DOTALL,
    )
    groups: tuple[str, ...] = ()
    if group_value:
        groups = _extract_all_with_html(
            group_value.group(1), r'<a[^>]*>([^<]+)</a>'
        )

    tag_rows = re.findall(
        r"<tr>[^<]*<td[^>]*>\s*([a-z\s]+?)\s*</td>[^<]*<td[^>]*>(.*?)</td>",
        html,
        flags=re.IGNORECASE | re.DOTALL,
    )
    tags: list[str] = []
    for label, content in tag_rows:
        if "tag" not in label.lower():
            continue
        for tag in re.findall(r'<a[^>]*>([^<]+)</a>', content):
            tag = tag.strip()
            if tag and tag not in tags:
                tags.append(tag)
    tags_tuple: tuple[str, ...] = tuple(tags)

    page_count = _parse_pages(_extract_with_html(html, r"(\d+\s*pages)"))

    description_match = re.search(
        r'<div[^>]*class="[^"]*gmid[^"]*"[^>]*>(.*?)</div>',
        html,
        flags=re.IGNORECASE | re.DOTALL,
    )
    description = None
    if description_match:
        description = re.sub(
            r"<[^>]+>", " ", description_match.group(1)
        ).strip()
        description = re.sub(r"\s+", " ", description)

    return GalleryMetadata(
        title=title,
        title_japanese=title_japanese,
        category=category,
        uploader=uploader,
        rating=rating,
        language=language,
        artists=artists,
        groups=groups,
        tags=tags_tuple,
        page_count=page_count,
        description=description,
    )


def merge_metadata(
    scraped: GalleryMetadata, manual_entries: Iterable[dict] = ()
) -> dict[str, str | None]:
    merged: dict[str, str | None] = {
        "Title": scraped.title,
        "JapaneseTitle": scraped.title_japanese,
        "Artist": ", ".join(scraped.artists) if scraped.artists else None,
        "Group": ", ".join(scraped.groups) if scraped.groups else None,
        "Language": scraped.language,
        "Category": scraped.category,
        "Tags": ", ".join(scraped.tags) if scraped.tags else None,
        "Rating": f"{scraped.rating:.2f}" if scraped.rating is not None else None,
        "Pages": str(scraped.page_count) if scraped.page_count else None,
        "Uploader": scraped.uploader,
        "Description": scraped.description,
    }
    for entry in manual_entries:
        field_name = entry.get("field_name")
        if not field_name or not entry.get("is_manual"):
            continue
        merged[field_name] = entry["field_value"]
    return merged


__all__ = ["GalleryMetadata", "parse_gallery_html", "merge_metadata"]