import asyncio
from pathlib import Path
from types import SimpleNamespace
import zipfile

import pytest

from app.conversion.comicinfo import build_comicinfo_xml
from app.conversion.convert import (
    ConversionError,
    detect_format,
    is_supported,
    stream_zip_to_cbz,
)
from app.conversion.service import _metadata_tags


def _write_fake_zip(path: Path, names: tuple[str, ...]) -> None:
    with zipfile.ZipFile(path, "w") as zout:
        for name in names:
            zout.writestr(name, b"x" * 32)


def test_detect_format_recognises_archives(tmp_path: Path) -> None:
    assert detect_format(tmp_path / "a.zip") == "zip"
    assert detect_format(tmp_path / "a.cbz") == "zip"
    assert detect_format(tmp_path / "a.rar") == "rar"
    assert detect_format(tmp_path / "a.7z") == "7z"
    assert detect_format(tmp_path / "a.unknown") == "unknown"


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
        SimpleNamespace(field_name="Tags", field_value="巨乳, 汉语, 巨乳"),
    ]
    xml = build_comicinfo_xml(
        title="Bilingual",
        tags=_metadata_tags(metadata),
    ).decode("utf-8")

    assert (
        "<Tags>female:big breasts, language:chinese, 巨乳, 汉语</Tags>"
        in xml
    )


def test_stream_zip_to_cbz_writes_comicinfo(tmp_path: Path) -> None:
    source = tmp_path / "src.zip"
    destination = tmp_path / "out.cbz"
    _write_fake_zip(
        source,
        ("01.jpg", "02.jpg", "03.jpg", "ComicInfo.xml"),
    )

    page_count = stream_zip_to_cbz(
        source,
        destination,
        title="Streamed",
    )
    assert page_count == 3  # ComicInfo.xml skipped
    assert destination.exists()

    with zipfile.ZipFile(destination, "r") as zin:
        names = zin.namelist()
        assert "ComicInfo.xml" in names
        assert "01.jpg" in names
        assert "02.jpg" in names
        assert "03.jpg" in names
        comicinfo = zin.read("ComicInfo.xml").decode("utf-8")
    assert "<Title>Streamed</Title>" in comicinfo


def test_stream_zip_to_cbz_requires_different_paths(tmp_path: Path) -> None:
    source = tmp_path / "src.zip"
    _write_fake_zip(source, ("01.jpg",))
    with pytest.raises(ConversionError):
        stream_zip_to_cbz(source, source, title="conflict")


def test_stream_zip_to_cbz_fails_on_missing_source(tmp_path: Path) -> None:
    with pytest.raises(ConversionError):
        stream_zip_to_cbz(
            tmp_path / "missing.zip",
            tmp_path / "out.cbz",
            title="x",
        )


def test_is_supported_zip_only() -> None:
    assert is_supported(Path("a.zip"))
    assert is_supported(Path("a.cbz"))
    assert not is_supported(Path("a.rar"))
    assert not is_supported(Path("a.7z"))
