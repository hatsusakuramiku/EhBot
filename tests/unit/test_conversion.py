from pathlib import Path
from types import SimpleNamespace
import zipfile

import pytest

from app.archive.errors import ArchiveError
from app.archive.formats import detect_source_format
from app.archive.models import SafetyLimits
from app.archive.processor import ArchiveProcessor
from app.conversion.comicinfo import build_comicinfo_xml
from app.conversion.naming import safe_library_name
from app.conversion.service import _metadata_tags

from tests.unit.archive_fixtures import (
    ZIP_ONLY_PROFILES,
    write_image_zip,
)


def test_detect_source_format_recognises_archives(tmp_path: Path) -> None:
    zip_path = tmp_path / "a.zip"
    write_image_zip(zip_path, ("01.jpg",))
    assert detect_source_format(zip_path) == "zip"
    assert detect_source_format(tmp_path / "a.cbz") == "zip"
    assert detect_source_format(tmp_path / "a.rar") == "rar"
    assert detect_source_format(tmp_path / "a.7z") == "7z"
    assert detect_source_format(tmp_path / "a.unknown") == "unknown"


def test_build_comicinfo_xml_includes_fields() -> None:
    xml = build_comicinfo_xml(
        title="Demo",
        artist="Some Artist",
        language="zh",
        category="Doujinshi",
        tags=("tag1", "tag2"),
        rating=4.5,
        description="Hello",
        page_count=24,
    ).decode("utf-8")
    assert "<Title>Demo</Title>" in xml
    assert "<LanguageISO>zh</LanguageISO>" in xml
    assert "<Genre>Doujinshi</Genre>" in xml
    assert "<Tags>tag1, tag2</Tags>" in xml
    assert "<Rating>4.50</Rating>" in xml
    assert "<PageCount>24</PageCount>" in xml
    assert xml.startswith("<?xml")


def test_comicinfo_includes_original_and_chinese_tags_without_duplicates() -> None:
    metadata = [
        SimpleNamespace(
            field_name="TagsRaw",
            field_value="female:big breasts, language:chinese",
        ),
        SimpleNamespace(field_name="Tags", field_value="\u5de8\u4e73, \u6c49\u8bed, \u5de8\u4e73"),
    ]
    xml = build_comicinfo_xml(
        title="Bilingual",
        tags=_metadata_tags(metadata),
    ).decode("utf-8")

    assert (
        "<Tags>female:big breasts, language:chinese, \u5de8\u4e73, \u6c49\u8bed</Tags>"
        in xml
    )


def _process(source: Path, destination: Path, tmp_path: Path):
    processor = ArchiveProcessor(
        profiles=ZIP_ONLY_PROFILES, limits=SafetyLimits()
    )
    return processor.process(
        source,
        destination=destination,
        work_directory=tmp_path / "work",
        comicinfo_builder=lambda page_count: build_comicinfo_xml(
            title="Streamed", page_count=page_count
        ),
    )


def test_zip_pipeline_writes_comicinfo_and_orders_pages(tmp_path: Path) -> None:
    source = tmp_path / "src.zip"
    destination = tmp_path / "out.cbz"
    write_image_zip(source, ("10.jpg", "2.jpg", "01.jpg", "ComicInfo.xml"))

    result = _process(source, destination, tmp_path)

    assert result.page_count == 3
    assert destination.exists()
    with zipfile.ZipFile(destination, "r") as archive:
        names = archive.namelist()
        comicinfo = archive.read("ComicInfo.xml").decode("utf-8")
    assert names == ["ComicInfo.xml", "0001.jpg", "0002.jpg", "0003.jpg"]
    assert "<Title>Streamed</Title>" in comicinfo
    assert "<PageCount>3</PageCount>" in comicinfo


def test_zip_pipeline_rejects_identical_source_and_destination(
    tmp_path: Path,
) -> None:
    source = tmp_path / "src.zip"
    write_image_zip(source, ("01.jpg",))
    with pytest.raises(ArchiveError):
        _process(source, source, tmp_path)


def test_zip_pipeline_fails_on_missing_source(tmp_path: Path) -> None:
    with pytest.raises(ArchiveError):
        _process(tmp_path / "missing.zip", tmp_path / "out.cbz", tmp_path)


def test_zip_pipeline_removes_partial_output_on_failure(tmp_path: Path) -> None:
    source = tmp_path / "src.zip"
    destination = tmp_path / "out.cbz"
    with zipfile.ZipFile(source, "w") as archive:
        archive.writestr("notes.txt", b"no images here")

    with pytest.raises(ArchiveError):
        _process(source, destination, tmp_path)

    assert not destination.exists()
    assert not destination.with_name(f"{destination.name}.part").exists()


def test_safe_library_name_sanitises_segments() -> None:
    assert safe_library_name("[Artist] Title", fallback="x") == "[Artist] Title"
    assert safe_library_name("a/b:c*d", fallback="x") == "a b c d"
    assert safe_library_name("   ", fallback="candidate-7") == "candidate-7"
    assert safe_library_name("con", fallback="x") == "con-archive"
    assert len(safe_library_name("z" * 400, fallback="x")) == 120