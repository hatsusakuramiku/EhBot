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
from app.archive.errors import (
    ArchivePasswordRequired,
    ArchiveSafetyError,
    ArchiveVolumesMissing,
)
from app.archive.models import SafetyLimits
from app.archive.processor import ArchiveProcessor

from tests.unit.archive_fixtures import ALL_PROFILES, JPEG_HEADER, image_bytes


TOOLS_PATH = Path(os.getenv("EHBOT_TEST_TOOLS_PATH", "data/tools"))
SEVEN_ZIP = resolve_seven_zip_executable("7zz", TOOLS_PATH)

pytestmark = pytest.mark.skipif(
    SEVEN_ZIP is None, reason="no managed 7-Zip executable is installed"
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
        profiles=ALL_PROFILES,
        limits=SafetyLimits(),
        passwords=passwords,
        tools_path=TOOLS_PATH,
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


def test_real_solid_archive_of_flat_pages_is_probed_and_published(
    tmp_path: Path,
) -> None:
    """A book of near-blank pages is not a bomb, and the block says otherwise.

    `image_bytes` is a JPEG signature followed by zeros: perfectly valid as far
    as the gate is concerned, and compressible enough that the whole solid
    block expands far past the ratio limit. The listing carries no member
    bytes, so the gate probes the block leader -- one bounded read, and the
    bytes say JPEG, so the block is exempt and the book is published. Before
    this, the leader was charged with the block's packed size and the archive
    was refused as a decompression bomb.
    """
    source = _pages(tmp_path / "src", 2, size=200_000)
    archive = tmp_path / "flat.7z"
    _run("a", "-t7z", "-bso0", "-bsp0", str(archive), str(source / "*"))

    profile = ALL_PROFILES[1]
    manifest = SevenZipBackend(profile, tools_path=TOOLS_PATH).inspect(
        (archive,), None
    )
    assert manifest.files[0].block == 0
    total = sum(member.size for member in manifest.files)
    packed = max(member.compressed_size for member in manifest.files)
    assert total / packed > 200, "the fixture must actually trip the gate"

    result = _process(archive, tmp_path)

    assert result.page_count == 2


def test_real_solid_archive_of_compressed_garbage_is_refused(
    tmp_path: Path,
) -> None:
    """The probe is what tells a flat book from a payload that is not a page.

    This archive trips the same gate, but its bytes are compressible *and* not
    an image -- the case the ratio gate exists for. The probe reads the block
    leader, finds no image signature, and the refusal stands.
    """
    source = tmp_path / "src"
    source.mkdir(parents=True, exist_ok=True)
    for index in (1, 2):
        (source / f"{index:02d}.jpg").write_bytes(
            b"<html><body>" + b" " * 200_000 + b"</body></html>"
        )
    archive = tmp_path / "garbage.7z"
    _run("a", "-t7z", "-bso0", "-bsp0", str(archive), str(source / "*"))

    with pytest.raises(ArchiveSafetyError) as error:
        _process(archive, tmp_path)

    assert error.value.code == "ARCHIVE_COMPRESSION_RATIO"


def test_backend_inspect_reports_real_member_sizes(tmp_path: Path) -> None:
    source = _pages(tmp_path / "src", 2, size=2048)
    archive = tmp_path / "book.7z"
    _run("a", "-t7z", "-bso0", "-bsp0", str(archive), str(source / "*"))

    profile = ALL_PROFILES[1]
    manifest = SevenZipBackend(profile, tools_path=TOOLS_PATH).inspect(
        (archive,), None
    )

    assert manifest.source_format == "7z"
    assert manifest.member_count == 2
    assert manifest.encrypted is False
    assert manifest.total_size == 4096


# --- built-in reader fallback (R51) ---------------------------------------


def test_aes256_zip_falls_back_to_seven_zip(tmp_path: Path) -> None:
    """The archive that used to stop at "waiting for password" now packs.

    `zipfile` cannot decode WinZip AES-256 (method 99), so admission hands the
    archive to 7-Zip instead of calling the correct password wrong.
    """
    source = _pages(tmp_path / "src", 3, size=2048)
    archive = tmp_path / "aes.zip"
    _run(
        "a", "-tzip", "-mem=AES256", "-pS3cret", "-bso0", "-bsp0",
        str(archive), str(source / "*"),
    )

    result = _process(archive, tmp_path, passwords=((1, "S3cret"),))

    assert result.snapshot.backend == "seven_zip"
    assert result.page_count == 3
    assert result.password_id == 1


def test_unencrypted_deflate64_zip_no_longer_asks_for_a_password(
    tmp_path: Path,
) -> None:
    # Compressible pages: 7-Zip stores incompressible data verbatim, and only a
    # real Deflate64 stream (method 9) exercises the fallback.
    source = _pages(tmp_path / "src", 3, size=4096)
    archive = tmp_path / "deflate64.zip"
    _run(
        "a", "-tzip", "-mm=Deflate64", "-bso0", "-bsp0",
        str(archive), str(source / "*"),
    )

    result = _process(archive, tmp_path)

    assert result.snapshot.backend == "seven_zip"
    assert result.page_count == 3
    assert result.password_id is None


def test_encrypted_deflate64_zip_uses_the_vault_after_falling_back(
    tmp_path: Path,
) -> None:
    source = _pages(tmp_path / "src", 3, size=4096)
    archive = tmp_path / "deflate64-secret.zip"
    _run(
        "a", "-tzip", "-mm=Deflate64", "-pS3cret", "-bso0", "-bsp0",
        str(archive), str(source / "*"),
    )

    result = _process(archive, tmp_path, passwords=((1, "S3cret"),))

    assert result.snapshot.backend == "seven_zip"
    assert result.password_id == 1


def test_a_plain_zip_keeps_the_builtin_streaming_backend(tmp_path: Path) -> None:
    source = _pages(tmp_path / "src", 2)
    archive = tmp_path / "plain.zip"
    _run("a", "-tzip", "-mx=9", "-bso0", "-bsp0", str(archive), str(source / "*"))

    result = _process(archive, tmp_path)

    assert result.snapshot.backend == "zipfile"


def test_an_aes_zip_with_an_empty_vault_still_reports_a_password(
    tmp_path: Path,
) -> None:
    """Falling back must not turn a real password problem into a method error."""
    source = _pages(tmp_path / "src", 1, size=2048)
    archive = tmp_path / "aes-empty.zip"
    _run(
        "a", "-tzip", "-mem=AES256", "-pS3cret", "-bso0", "-bsp0",
        str(archive), str(source / "*"),
    )

    with pytest.raises(ArchivePasswordRequired):
        _process(archive, tmp_path)


# --- real RAR archives (committed fixtures, no rar binary required) --------


RAR_FIXTURES = Path(__file__).resolve().parent.parent / "fixtures" / "rar"
RAR_PASSWORD = "S3cret"

pytestmark = [
    pytestmark,
    pytest.mark.skipif(
        not (RAR_FIXTURES / "rar5-plain.rar").is_file(),
        reason="RAR fixtures are not present",
    ),
]


def _rar(name: str) -> Path:
    return RAR_FIXTURES / name


def test_real_rar5_archive_is_published_as_cbz(tmp_path: Path) -> None:
    result = _process(_rar("rar5-plain.rar"), tmp_path)
    assert result.snapshot.backend == "seven_zip"
    assert result.snapshot.source_format == "rar"
    assert result.page_count == 3


def test_real_rar5_header_encrypted_archive_uses_the_vault(
    tmp_path: Path,
) -> None:
    """`-hp` hides the names too, so the vault is consulted before listing."""
    result = _process(
        _rar("rar5-hp.rar"), tmp_path, passwords=((4, RAR_PASSWORD),)
    )
    assert result.snapshot.backend == "seven_zip"
    assert result.page_count == 3
    assert result.password_id == 4


def test_real_rar3_header_encrypted_archive_uses_the_vault(
    tmp_path: Path,
) -> None:
    result = _process(
        _rar("rar3-hp.rar"), tmp_path, passwords=((4, RAR_PASSWORD),)
    )
    assert result.page_count == 3
    assert result.password_id == 4


def test_real_rar_with_data_only_encryption_uses_the_vault(
    tmp_path: Path,
) -> None:
    result = _process(
        _rar("rar5-password.rar"), tmp_path, passwords=((4, RAR_PASSWORD),)
    )
    assert result.page_count == 3
    assert result.password_id == 4


def test_real_rar_without_the_password_still_asks_for_one(tmp_path: Path) -> None:
    with pytest.raises(ArchivePasswordRequired):
        _process(_rar("rar5-hp.rar"), tmp_path, passwords=((1, "wrong"),))


def test_real_rar5_volumes_are_all_used(tmp_path: Path) -> None:
    result = _process(_rar("rar5-vol.part1.rar"), tmp_path)
    assert result.volume_count == 3
    assert result.page_count == 3


def test_real_legacy_rnn_volumes_are_found_from_the_leading_rar(
    tmp_path: Path,
) -> None:
    """The `.rar` end of a `.r00` series must find its companions (R51 fix)."""
    result = _process(_rar("rar3-old.rar"), tmp_path)
    assert result.volume_count == 3
    assert result.page_count == 3


def test_real_solid_rar_high_ratio_pages_pass_the_ratio_gate(
    tmp_path: Path,
) -> None:
    """A solid block of blank pages is identified as an image and allowed."""
    result = _process(_rar("rar5-solid.rar"), tmp_path)
    assert result.page_count == 3
