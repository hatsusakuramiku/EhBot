"""Shared ComicInfo mapping used by packing and metadata synchronization."""

from types import SimpleNamespace
from xml.etree import ElementTree as ET

from app.conversion.comicinfo import build_from_metadata


def rows(values):
    return [SimpleNamespace(field_name=name, field_value=value, is_manual=True)
            for name, value in values.items()]


def test_metadata_mapping_custom_elements_and_database_only_fields():
    root = ET.fromstring(build_from_metadata(rows({
        "Title": "Book", "Artist": "Artist", "Group": "Circle", "Series": "Series",
        "Language": "chinese", "Character": "Character", "Category": "Manga",
        "Rating": "4.5", "Description": "Summary", "Web": "https://example.org/",
        "Translator": "Translator", "ShelfLabel": "Private", "Year": "2026",
    }), "fallback", 5))
    assert root.findtext("Title") == "Book"
    assert root.findtext("Series") == root.findtext("LocalizedSeries") == "Series"
    assert root.findtext("Writer") == root.findtext("CoverArtist") == "Artist"
    assert root.findtext("Publisher") == "Circle"
    assert root.findtext("LanguageISO") == "zh"
    assert root.findtext("Characters") == "Character"
    assert root.findtext("Genre") == "Manga"
    assert root.findtext("Rating") == "4.50"
    assert root.findtext("Summary") == "Summary"
    assert root.findtext("Web") == "https://example.org/"
    assert root.findtext("Translator") == "Translator"
    assert root.findtext("Year") == "2026"
    assert root.findtext("PageCount") == "5"
    assert root.find("ShelfLabel") is None


def test_explicit_comicinfo_overrides_win_over_mapped_metadata_on_repack():
    root = ET.fromstring(build_from_metadata(rows({
        "Artist": "Gallery writer", "Writer": "Operator writer",
        "Description": "Gallery summary", "Summary": "Operator summary",
        "Translator": "Saved translator",
    }), "Title", 2))
    assert root.findtext("Writer") == "Operator writer"
    assert root.findtext("Penciller") == "Gallery writer"
    assert root.findtext("Summary") == "Operator summary"
    assert root.findtext("Translator") == "Saved translator"
