from __future__ import annotations

from datetime import datetime, UTC
from xml.etree import ElementTree as ET
from xml.etree.ElementTree import Element, SubElement


# ISO 639-1 codes for the languages E-Hentai reports most often.
LANGUAGE_ISO_CODES: dict[str, str] = {
    "chinese": "zh",
    "japanese": "ja",
    "english": "en",
    "korean": "ko",
    "russian": "ru",
    "spanish": "es",
    "french": "fr",
    "german": "de",
    "portuguese": "pt",
    "italian": "it",
    "thai": "th",
    "vietnamese": "vi",
    "indonesian": "id",
}


def _language_iso(language: str) -> str:
    return LANGUAGE_ISO_CODES.get(language.strip().lower(), language)


def build_comicinfo_xml(
    *,
    title: str,
    artist: str | None = None,
    language: str | None = None,
    category: str | None = None,
    tags: tuple[str, ...] = (),
    rating: float | None = None,
    description: str | None = None,
    page_count: int | None = None,
    japanese_title: str | None = None,
    group: str | None = None,
    parody: str | None = None,
    character: str | None = None,
    web: str | None = None,
    scan_information: str | None = None,
) -> bytes:
    root = Element("ComicInfo")
    SubElement(root, "Title").text = title
    SubElement(root, "Series").text = parody or title
    SubElement(root, "LocalizedSeries").text = japanese_title or title
    # Publisher carries the circle/group when known, per the plan's mapping.
    SubElement(root, "Publisher").text = group or "EhBot"
    if artist:
        SubElement(root, "Writer").text = artist
        SubElement(root, "Penciller").text = artist
        SubElement(root, "CoverArtist").text = artist
    if character:
        SubElement(root, "Characters").text = character
    if language:
        SubElement(root, "LanguageISO").text = _language_iso(language)
    if category:
        SubElement(root, "Genre").text = category
    if tags:
        SubElement(root, "Tags").text = ", ".join(tags)
    if rating is not None:
        SubElement(root, "Rating").text = f"{rating:.2f}"
    if description:
        SubElement(root, "Summary").text = description
    if page_count is not None and page_count > 0:
        SubElement(root, "PageCount").text = str(page_count)
    if web:
        SubElement(root, "Web").text = web
    # Source grade, so a reading-grade book can be found and replaced later.
    if scan_information:
        SubElement(root, "ScanInformation").text = scan_information
    SubElement(root, "Manga").text = "Yes"
    SubElement(root, "Added").text = datetime.now(UTC).strftime(
        "%Y-%m-%d %H:%M:%S"
    )
    return ET.tostring(root, encoding="utf-8", xml_declaration=True)


__all__ = ["LANGUAGE_ISO_CODES", "build_comicinfo_xml"]
