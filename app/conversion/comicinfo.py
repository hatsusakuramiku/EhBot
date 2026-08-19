from __future__ import annotations

from datetime import datetime, UTC
from xml.etree import ElementTree as ET
from xml.etree.ElementTree import Element, SubElement


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
) -> bytes:
    root = Element("ComicInfo")
    SubElement(root, "Title").text = title
    SubElement(root, "Series").text = title
    SubElement(root, "LocalizedSeries").text = title
    SubElement(root, "Publisher").text = "EhBot"
    if artist:
        SubElement(root, "Writer").text = artist
        SubElement(root, "CoverArtist").text = artist
    if language:
        SubElement(root, "LanguageISO").text = language
    if category:
        SubElement(root, "Category").text = category
    if tags:
        SubElement(root, "Tags").text = ", ".join(tags)
    if rating is not None:
        SubElement(root, "Rating").text = f"{rating:.2f}"
    if description:
        SubElement(root, "Summary").text = description
    if page_count is not None and page_count > 0:
        SubElement(root, "PageCount").text = str(page_count)
    SubElement(root, "Manga").text = "Yes"
    SubElement(root, "Added").text = datetime.now(UTC).strftime(
        "%Y-%m-%d %H:%M:%S"
    )
    return ET.tostring(root, encoding="utf-8", xml_declaration=True)


__all__ = ["build_comicinfo_xml"]
