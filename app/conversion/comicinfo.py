from __future__ import annotations

from datetime import datetime, UTC
from xml.etree import ElementTree as ET
from xml.etree.ElementTree import Element, SubElement

from app.archive.quality import quality_note


# ComicInfo scalar elements. Generated facts are deliberately not editable.
DERIVED_ELEMENTS = frozenset({"PageCount", "Manga", "Added"})
EDITABLE_ELEMENTS = frozenset({
    "Title", "JapaneseTitle", "Series", "LocalizedSeries", "Publisher",
    "Writer", "Penciller", "Inker", "Colorist", "Letterer", "CoverArtist",
    "Characters", "LanguageISO", "Genre", "Tags", "Rating", "Summary", "Web",
    "ScanInformation", "Translator", "Editor", "Year", "Month", "Day",
    "Volume", "Number", "Count", "AlternateSeries", "AlternateNumber",
    "AlternateCount", "SeriesGroup", "StoryArc", "StoryArcNumber", "Notes",
    "AgeRating", "Format", "Imprint", "Teams", "Locations", "MainCharacterOrTeam",
    "CommunityRating", "BlackAndWhite", "Review", "GTIN",
})


def field_writes_archive(field_name: str) -> bool:
    """Whether an operator field contributes to ComicInfo.xml."""
    return field_name in EDITABLE_ELEMENTS or field_name in {
        "Artist", "Language", "Category", "Description", "Group", "Parody",
        "Character",
    }


def metadata_tags(metadata) -> tuple[str, ...]:
    """Combine upstream and translated tags without duplicating a value."""
    values = {}
    for entry in metadata:
        values.setdefault(entry.field_name, entry.field_value)
    tags = []
    for name in ("TagsRaw", "Tags"):
        for item in (values.get(name) or "").replace("\n", ",").split(","):
            tag = item.strip()
            if tag and tag not in tags:
                tags.append(tag)
    return tuple(tags)


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
    series: str | None = None,
    character: str | None = None,
    web: str | None = None,
    scan_information: str | None = None,
) -> bytes:
    root = Element("ComicInfo")
    SubElement(root, "Title").text = title
    # `<Series>` is the series a library groups volumes under. The stored `Series`
    # metadata wins, then an AI-resolved name (R83); with neither, the historical
    # `parody or title` fallback is kept so an upgrade changes no existing book.
    resolved_series = series or parody or title
    SubElement(root, "Series").text = resolved_series
    # A resolved series goes to the localized copy too; without one the previous
    # `japanese_title or title` fallback stands.
    SubElement(root, "LocalizedSeries").text = series or japanese_title or title
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


def build_from_metadata(
    metadata,
    title: str,
    page_count: int,
    image_quality: str | None = None,
    series: str | None = None,
    *,
    existing: bytes | None = None,
) -> bytes:
    """Build packing and edit output from the same field mapping.

    Rows arrive best first. Explicit ComicInfo overrides win over mapped values.
    During an edit preserve the file's generated facts, quality record and
    unmapped elements, so a metadata correction does not rewrite its history.
    """
    values = {}
    for entry in metadata:
        values.setdefault(entry.field_name, entry.field_value)
    rating_value = values.get("Rating")
    try:
        rating = float(rating_value) if rating_value else None
    except ValueError:
        rating = None
    source = values.get("ScanInformation")
    note = quality_note(image_quality)
    scan_information = f"{source} {note}" if source and note else source or note
    root = ET.fromstring(build_comicinfo_xml(
        title=values.get("Title") or title,
        artist=values.get("Artist"), language=values.get("Language"),
        category=values.get("Category"), tags=metadata_tags(metadata), rating=rating,
        description=values.get("Description"), page_count=page_count,
        japanese_title=values.get("JapaneseTitle"), group=values.get("Group"),
        parody=values.get("Parody"), series=series or values.get("Series"),
        character=values.get("Character"), web=values.get("Web"),
        scan_information=scan_information,
    ))
    if existing is not None:
        previous = ET.fromstring(existing)
        for child in previous:
            if child.tag in DERIVED_ELEMENTS or child.tag == "ScanInformation":
                found = root.find(child.tag)
                if found is not None:
                    root.remove(found)
                root.append(child)
            elif root.find(child.tag) is None:
                root.append(child)
    for name in EDITABLE_ELEMENTS:
        # Shared names (Title, Series, Tags, Rating, Web) already participate
        # in the mapping. Only an operator override should replace formatting.
        direct = next((entry for entry in metadata
                       if entry.field_name == name), None)
        if direct is None:
            continue
        mapped = name in {"Title", "Series", "Tags", "Rating", "Web", "ScanInformation"}
        if mapped and not getattr(direct, "is_manual", False):
            continue
        found = root.find(name)
        if found is None:
            found = SubElement(root, name)
        found.text = f"{rating:.2f}" if name == "Rating" and rating is not None else direct.field_value
    return ET.tostring(root, encoding="utf-8", xml_declaration=True)


__all__ = ["LANGUAGE_ISO_CODES", "build_comicinfo_xml", "build_from_metadata"]
