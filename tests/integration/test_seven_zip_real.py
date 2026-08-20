"""End-to-end coverage against a real 7-Zip executable.

These tests are skipped when no 7-Zip command is installed, so the suite
still runs on hosts without the tool. They intentionally exercise the paths
that injected-runner fixtures cannot prove: real `-slt` output, real
extraction, real split-volume handling, and real password failures.
"""

from __future__ import annotations

import os
import subprocess
import zipfile
from pathlib import Path

import pytest

from app.archive.backends.seven_zip import (
    SevenZipBackend,
    resolve_seven_zip_executable,
)
from app.archive.errors import ArchivePasswordRequired, ArchiveVolumesMissing
from app.archive.models import SafetyLimits
from app.archive.processor import ArchiveProcessor

from tests.unit.archive_fixtures import ALL_PROFILES, JPEG_HEADER, image_bytes


SEVEN_ZIP = resolve_seven_zip_executable("7zz")

pytestmark = pytest.mark.skipif(
    SEVEN_ZIP is None, reason="no 7-Zip executable is installed on this host"
)


def _run(*arguments: str) -> None:
    subprocess.run(
        [str(SEVEN_ZIP), *arguments],
        check=True,
        capture_output=True,
    )


def _pages(directory: Path, count: int = 2, *, size: int = 512) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    for index in range(1, count + 1):
        (directory / f"{index:02d}.jpg").write_bytes(
            image_bytes(f"{index:02d}.jpg", size=size)
        )
    return directory


def _incompressible_pages(directory: Path, count: int = 3) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    for index in range(1, count + 1):
        (directory / f"{index:02d}.jpg").write_bytes(
            JPEG_HEADER + os.urandom(120_000)
        )
    return directory


def _process(
    source: Path,
    tmp_path: Path,
    *,
    name: str = "out",
    passwords: tuple[tuple[int, str], ...] = (),
):
    processor = ArchiveProcessor(
        profiles=ALL_PROFILES, limits=SafetyLimits(), passwords=passwords
    )
    return processor.process(
        source,
        destination=tmp_path / "library" / f"{name}.cbz",
        work_directory=tmp_path / "work",
        comicinfo_builder=lambda count: (
            f"<ComicInfo><PageCount>{count}</PageCount></ComicInfo>".encode()
        ),
        library_path=tmp_path / "library",
    )


def test_resolved_executable_reports_its_version() -> None:
    completed = subprocess.run(
        [str(SEVEN_ZIP)], capture_output=True, text=True, check=False
    )
    assert "7-Zip" in completed.stdout


def test_real_seven_zip_archive_is_published_as_cbz(tmp_path: Path) -> None:
    source = _pages(tmp_path / "src")
    archive = tmp_path / "book.7z"
    _run("a", "-t7z", "-bso0", "-bsp0", str(archive), str(source / "*"))

    result = _process(archive, tmp_path)

    assert result.snapshot.backend == "seven_zip"
    assert result.snapshot.tool_profile == "7zz-default"
    assert result.snapshot.source_format == "7z"
    assert result.page_count == 2
    with zipfile.ZipFile(result.cbz_path) as cbz:
        assert cbz.namelist() == ["0001.jpg", "0002.jpg", "ComicInfo.xml"]
        assert "<PageCount>2</PageCount>" in cbz.read("ComicInfo.xml").decode(
            "utf-8"
        )


def test_real_seven_zip_flattens_nested_directories(tmp_path: Path) -> None:
    source = tmp_path / "src"
    (source / "chapter one").mkdir(parents=True)
    (source / "chapter one" / "02.jpg").write_bytes(image_bytes("02.jpg"))
    (source / "01.jpg").write_bytes(image_bytes("01.jpg"))
    archive = tmp_path / "nested.7z"
    _run("a", "-t7z", "-bso0", "-bsp0", str(archive), str(source / "*"))

    result = _process(archive, tmp_path)

    assert result.page_count == 2
    with zipfile.ZipFile(result.cbz_path) as cbz:
        assert cbz.namelist() == ["0001.jpg", "0002.jpg", "ComicInfo.xml"]


def test_real_seven_zip_cbz_pages_are_stored_uncompressed(tmp_path: Path) -> None:
    source = _pages(tmp_path / "src", 1)
    archive = tmp_path / "book.7z"
    _run("a", "-t7z", "-bso0", "-bsp0", str(archive), str(source / "*"))

    result = _process(archive, tmp_path)

    with zipfile.ZipFile(result.cbz_path) as cbz:
        assert all(
            info.compress_type == zipfile.ZIP_STORED for info in cbz.infolist()
        )


def test_real_encrypted_archive_without_vault_is_recoverable(
    tmp_path: Path,
) -> None:
    source = _pages(tmp_path / "src", 1)
    archive = tmp_path / "enc.7z"
    _run(
        "a", "-t7z", "-pS3cret", "-bso0", "-bsp0", str(archive), str(source / "*")
    )

    with pytest.raises(ArchivePasswordRequired):
        _process(archive, tmp_path)

    assert not (tmp_path / "library" / "out.cbz").exists()
    assert list((tmp_path / "library").glob("*.part")) == []


def test_real_encrypted_archive_opens_with_vault_password(tmp_path: Path) -> None:
    source = _pages(tmp_path / "src")
    archive = tmp_path / "enc.7z"
    _run(
        "a", "-t7z", "-pS3cret", "-bso0", "-bsp0", str(archive), str(source / "*")
    )

    result = _process(
        archive, tmp_path, passwords=((4, "wrong-one"), (7, "S3cret"))
    )

    assert result.password_id == 7
    assert result.page_count == 2


def test_real_header_encrypted_archive_needs_password_to_inspect(
    tmp_path: Path,
) -> None:
    """A `-mhe=on` archive cannot even be listed without the password."""
    source = _pages(tmp_path / "src", 1)
    archive = tmp_path / "henc.7z"
    _run(
        "a",
        "-t7z",
        "-pS3cret",
        "-mhe=on",
        "-bso0",
        "-bsp0",
        str(archive),
        str(source / "*"),
    )

    with pytest.raises(ArchivePasswordRequired):
        _process(archive, tmp_path, name="header-none")

    result = _process(
        archive, tmp_path, name="header-ok", passwords=((7, "S3cret"),)
    )
    assert result.password_id == 7
    assert result.page_count == 1


def test_real_split_archive_is_processed_from_first_volume(
    tmp_path: Path,
) -> None:
    source = _incompressible_pages(tmp_path / "src")
    split_directory = tmp_path / "split"
    split_directory.mkdir()
    _run(
        "a",
        "-t7z",
        "-v100k",
        "-bso0",
        "-bsp0",
        str(split_directory / "book.7z"),
        str(source / "*"),
    )
    volumes = sorted(path.name for path in split_directory.iterdir())
    assert len(volumes) > 1, "expected 7-Zip to produce multiple volumes"

    result = _process(split_directory / "book.7z.001", tmp_path)

    assert result.volume_count == len(volumes)
    assert result.page_count == 3


def test_real_split_archive_with_gap_reports_missing_volume(
    tmp_path: Path,
) -> None:
    source = _incompressible_pages(tmp_path / "src")
    split_directory = tmp_path / "split"
    split_directory.mkdir()
    _run(
        "a",
        "-t7z",
        "-v100k",
        "-bso0",
        "-bsp0",
        str(split_directory / "book.7z"),
        str(source / "*"),
    )
    (split_directory / "book.7z.002").rename(tmp_path / "held.002")

    with pytest.raises(ArchiveVolumesMissing) as error:
        _process(split_directory / "book.7z.001", tmp_path)

    assert error.value.missing == ("book.7z.002",)


def test_real_seven_zip_cleans_up_after_success(tmp_path: Path) -> None:
    source = _pages(tmp_path / "src")
    archive = tmp_path / "book.7z"
    _run("a", "-t7z", "-bso0", "-bsp0", str(archive), str(source / "*"))

    _process(archive, tmp_path)

    assert list((tmp_path / "work").rglob("*")) == []
    assert list((tmp_path / "library").glob("*.part")) == []
    assert list((tmp_path / "library").glob("*.pages")) == []


def test_real_seven_zip_rejects_corrupted_archive(tmp_path: Path) -> None:
    source = _pages(tmp_path / "src")
    archive = tmp_path / "book.7z"
    _run("a", "-t7z", "-bso0", "-bsp0", str(archive), str(source / "*"))
    data = bytearray(archive.read_bytes())
    data[-16:] = b"\x00" * 16
    archive.write_bytes(bytes(data))

    with pytest.raises(Exception) as error:
        _process(archive, tmp_path)

    assert getattr(error.value, "code", "").startswith("ARCHIVE_")


def test_backend_inspect_reports_real_member_sizes(tmp_path: Path) -> None:
    source = _pages(tmp_path / "src", 2, size=2048)
    archive = tmp_path / "book.7z"
    _run("a", "-t7z", "-bso0", "-bsp0", str(archive), str(source / "*"))

    profile = ALL_PROFILES[1]
    manifest = SevenZipBackend(profile).inspect((archive,), None)

    assert manifest.source_format == "7z"
    assert manifest.member_count == 2
    assert manifest.encrypted is False
    assert manifest.total_size == 4096