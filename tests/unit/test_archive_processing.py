from dataclasses import replace
from pathlib import Path
import zipfile

import pytest

from app.archive.backends.seven_zip import (
    SevenZipBackend,
    parse_slt_listing,
    resolve_seven_zip_executable,
)
from app.archive.backends.zip_backend import ZipfileBackend
from app.archive.errors import (
    ArchiveError,
    ArchivePasswordRequired,
    ArchiveSafetyError,
    ArchiveToolUnavailable,
    ArchiveVolumesMissing,
    UnsupportedArchiveFormat,
)
from app.archive.formats import (
    detect_source_format,
    format_from_extension,
    resolve_volumes,
    volume_group,
)
from app.archive.models import (
    ArchiveManifest,
    ArchiveMember,
    SafetyLimits,
)
from app.archive.processor import ArchiveProcessor
from app.archive.toolchain import install_root
from app.archive.safety import (
    natural_sort_key,
    normalize_member_name,
    page_file_names,
    validate_manifest,
)
from app.archive.vault import (
    VaultError,
    decrypt_password,
    encrypt_password,
    generate_master_key,
)

from tests.unit.archive_fixtures import (
    ALL_PROFILES,
    JPEG_HEADER,
    SEVEN_ZIP_PROFILE,
    ZIP_ONLY_PROFILES,
    image_bytes,
    write_image_zip,
)


def _member(name: str, **kwargs) -> ArchiveMember:
    defaults = {
        "size": 1024,
        "compressed_size": 512,
        "header": JPEG_HEADER,
    }
    defaults.update(kwargs)
    return ArchiveMember(name=name, **defaults)


def _manifest(*members: ArchiveMember) -> ArchiveManifest:
    return ArchiveManifest(source_format="zip", members=members)


# --- format and volume detection ------------------------------------------


def test_format_from_extension_covers_split_names() -> None:
    assert format_from_extension(Path("a.part1.rar")) == "rar"
    assert format_from_extension(Path("a.r00")) == "rar"
    assert format_from_extension(Path("a.7z.001")) == "7z"
    assert format_from_extension(Path("a.zip.002")) == "zip"
    assert format_from_extension(Path("a.cbr")) == "rar"
    assert format_from_extension(Path("a.txt")) == "unknown"


def test_detect_source_format_prefers_magic_number(tmp_path: Path) -> None:
    disguised = tmp_path / "actually-a-zip.rar"
    write_image_zip(disguised, ("01.jpg",))
    assert detect_source_format(disguised) == "zip"


def test_volume_group_only_matches_split_names() -> None:
    assert volume_group(Path("a.part2.rar")) == "a.rar"
    assert volume_group(Path("a.r01")) == "a.rar"
    assert volume_group(Path("a.7z.003")) == "a.7z"
    assert volume_group(Path("a.zip")) is None


def test_resolve_volumes_returns_single_file_for_plain_archive(
    tmp_path: Path,
) -> None:
    source = tmp_path / "plain.zip"
    write_image_zip(source, ("01.jpg",))
    volumes, missing = resolve_volumes(source)
    assert volumes == (source,)
    assert missing == ()


def test_resolve_volumes_orders_parts_and_reports_gaps(tmp_path: Path) -> None:
    for name in ("book.part1.rar", "book.part2.rar", "book.part4.rar"):
        (tmp_path / name).write_bytes(b"x")
    volumes, missing = resolve_volumes(tmp_path / "book.part1.rar")
    assert [path.name for path in volumes] == [
        "book.part1.rar",
        "book.part2.rar",
        "book.part4.rar",
    ]
    assert missing == ("book.part3.rar",)


def test_resolve_volumes_handles_numbered_series(tmp_path: Path) -> None:
    for name in ("book.7z.001", "book.7z.002", "book.7z.003"):
        (tmp_path / name).write_bytes(b"x")
    volumes, missing = resolve_volumes(tmp_path / "book.7z.001")
    assert len(volumes) == 3
    assert missing == ()


# --- safety ---------------------------------------------------------------


def test_normalize_member_name_rejects_traversal_and_absolute_paths() -> None:
    assert normalize_member_name("dir\\01.jpg") == "dir/01.jpg"
    with pytest.raises(ArchiveSafetyError):
        normalize_member_name("../escape.jpg")
    with pytest.raises(ArchiveSafetyError):
        normalize_member_name("/etc/passwd")
    with pytest.raises(ArchiveSafetyError):
        normalize_member_name("C:/windows/system32/x.jpg")


def test_natural_sort_key_orders_numbers_naturally() -> None:
    names = ["10.jpg", "2.jpg", "1.jpg"]
    assert sorted(names, key=natural_sort_key) == ["1.jpg", "2.jpg", "10.jpg"]


def test_validate_manifest_accepts_images_and_orders_pages() -> None:
    pages = validate_manifest(
        _manifest(
            _member("10.jpg"),
            _member("2.jpg"),
            _member("notes.txt", header=b"hello"),
            ArchiveMember(name="dir/", is_dir=True),
        ),
        SafetyLimits(),
    )
    assert [member.name for member in pages] == ["2.jpg", "10.jpg"]


def test_validate_manifest_rejects_symlinks_and_nested_archives() -> None:
    with pytest.raises(ArchiveSafetyError) as symlink_error:
        validate_manifest(
            _manifest(_member("01.jpg", is_symlink=True)), SafetyLimits()
        )
    assert symlink_error.value.code == "ARCHIVE_MEMBER_SYMLINK"

    with pytest.raises(ArchiveSafetyError) as nested_error:
        validate_manifest(_manifest(_member("inner.zip")), SafetyLimits())
    assert nested_error.value.code == "ARCHIVE_NESTED_ARCHIVE"


def test_validate_manifest_enforces_limits() -> None:
    with pytest.raises(ArchiveSafetyError) as count_error:
        validate_manifest(
            _manifest(*(_member(f"{index}.jpg") for index in range(3))),
            SafetyLimits(max_members=2),
        )
    assert count_error.value.code == "ARCHIVE_TOO_MANY_MEMBERS"

    with pytest.raises(ArchiveSafetyError) as ratio_error:
        validate_manifest(
            _manifest(_member("01.jpg", size=10_000_000, compressed_size=10)),
            SafetyLimits(),
        )
    assert ratio_error.value.code == "ARCHIVE_COMPRESSION_RATIO"

    with pytest.raises(ArchiveSafetyError) as depth_error:
        validate_manifest(
            _manifest(_member("a/b/c/01.jpg")), SafetyLimits(max_depth=2)
        )
    assert depth_error.value.code == "ARCHIVE_MEMBER_TOO_DEEP"

    with pytest.raises(ArchiveSafetyError) as total_error:
        validate_manifest(
            _manifest(_member("01.jpg", size=200, compressed_size=200)),
            SafetyLimits(max_total_bytes=100),
        )
    assert total_error.value.code == "ARCHIVE_TOTAL_TOO_LARGE"


def test_validate_manifest_rejects_fake_image_extension() -> None:
    with pytest.raises(ArchiveSafetyError) as error:
        validate_manifest(
            _manifest(_member("01.png", header=b"MZ\x90\x00 not an image")),
            SafetyLimits(),
        )
    assert error.value.code == "ARCHIVE_MEMBER_FAKE_IMAGE"


def test_page_file_names_are_stable_and_collision_free() -> None:
    names = page_file_names(
        (_member("b/01.jpg"), _member("a/01.jpg"), _member("cover.png"))
    )
    assert names == ("0001.jpg", "0002.jpg", "0003.png")
    assert len(set(names)) == 3


# --- zipfile backend ------------------------------------------------------


def test_zipfile_backend_inspect_reports_members(tmp_path: Path) -> None:
    source = tmp_path / "src.zip"
    write_image_zip(source, ("01.jpg", "sub/02.png"))
    manifest = ZipfileBackend().inspect((source,), None)
    assert manifest.source_format == "zip"
    assert manifest.member_count == 2
    assert manifest.encrypted is False
    assert all(member.header for member in manifest.files)


def test_zipfile_backend_extract_stays_inside_destination(tmp_path: Path) -> None:
    source = tmp_path / "src.zip"
    write_image_zip(source, ("01.jpg", "sub/02.jpg"))
    backend = ZipfileBackend()
    manifest = backend.inspect((source,), None)
    extracted = backend.extract(
        (source,), tmp_path / "extract", None, manifest.files
    )
    assert len(extracted) == 2
    for path in extracted.values():
        assert path.is_relative_to(tmp_path / "extract")
        assert path.is_file()


def test_zipfile_backend_reports_encrypted_members(tmp_path: Path) -> None:
    """A ZIP with the encryption flag must not be treated as readable."""
    source = tmp_path / "encrypted.zip"
    plain = tmp_path / "plain.zip"
    write_image_zip(plain, ("01.jpg",))
    # Flip the general-purpose encryption bit in the local and central headers.
    data = bytearray(plain.read_bytes())
    for signature in (b"PK\x03\x04", b"PK\x01\x02"):
        offset = data.find(signature)
        while offset != -1:
            flag_offset = offset + (6 if signature == b"PK\x03\x04" else 8)
            data[flag_offset] |= 0x01
            offset = data.find(signature, offset + 1)
    source.write_bytes(bytes(data))

    manifest = ZipfileBackend().inspect((source,), None)
    assert manifest.encrypted is True


def test_zipfile_backend_pack_cbz_uses_stored_compression(tmp_path: Path) -> None:
    page = tmp_path / "page.jpg"
    page.write_bytes(image_bytes("page.jpg"))
    destination = tmp_path / "out.cbz"
    written = ZipfileBackend().pack_cbz(
        (("0001.jpg", page),), destination, b"<ComicInfo />"
    )
    assert written == 1
    with zipfile.ZipFile(destination) as archive:
        assert archive.namelist() == ["ComicInfo.xml", "0001.jpg"]
        assert all(
            info.compress_type == zipfile.ZIP_STORED
            for info in archive.infolist()
        )


# --- seven zip backend ----------------------------------------------------


SLT_OUTPUT = """Path = 01.jpg
Size = 2048
Packed Size = 1024
Attributes = _ -----
Encrypted = -

Path = sub
Size = 0
Packed Size = 0
Attributes = D_ ----

Path = sub/02.jpg
Size = 4096
Packed Size = 2048
Attributes = _ -----
Encrypted = +
"""


def test_parse_slt_listing_extracts_members() -> None:
    members = parse_slt_listing(SLT_OUTPUT)
    assert [member.name for member in members] == ["01.jpg", "sub", "sub/02.jpg"]
    assert members[0].size == 2048
    assert members[0].compressed_size == 1024
    assert members[1].is_dir is True
    assert members[2].encrypted is True


def test_seven_zip_backend_inspect_uses_registered_profile(tmp_path: Path) -> None:
    source = tmp_path / "book.7z"
    source.write_bytes(b"7z\xbc\xaf\x27\x1c" + b"\x00" * 16)
    calls: list[tuple[str, ...]] = []

    def runner(arguments: tuple[str, ...]) -> tuple[int, str]:
        calls.append(arguments)
        return 0, SLT_OUTPUT

    manifest = SevenZipBackend(SEVEN_ZIP_PROFILE, runner=runner).inspect(
        (source,), None
    )
    assert manifest.source_format == "7z"
    assert manifest.member_count == 2
    assert manifest.encrypted is True
    assert calls[0][0] == "l"
    assert "-slt" in calls[0]
    assert calls[0][-1] == str(source)


def test_seven_zip_backend_maps_password_failure() -> None:
    def runner(arguments: tuple[str, ...]) -> tuple[int, str]:
        return 2, "ERROR: Wrong password : 01.jpg"

    backend = SevenZipBackend(SEVEN_ZIP_PROFILE, runner=runner)
    with pytest.raises(ArchivePasswordRequired):
        backend.test_password((Path("book.7z"),), "bad")


def test_seven_zip_backend_maps_generic_tool_failure() -> None:
    def runner(arguments: tuple[str, ...]) -> tuple[int, str]:
        return 2, "ERROR: Unexpected end of archive"

    backend = SevenZipBackend(SEVEN_ZIP_PROFILE, runner=runner)
    with pytest.raises(ArchiveError) as error:
        backend.test_password((Path("book.7z"),), None)
    assert error.value.code == "ARCHIVE_TOOL_FAILED"


def test_seven_zip_backend_rejects_unconfigured_executable() -> None:
    profile = replace(SEVEN_ZIP_PROFILE, executable_path=None)
    with pytest.raises(ArchiveToolUnavailable):
        SevenZipBackend(profile).inspect((Path("book.7z"),), None)


def test_seven_zip_backend_rejects_missing_absolute_executable(
    tmp_path: Path,
) -> None:
    """An absolute path is used verbatim and must exist."""
    profile = replace(
        SEVEN_ZIP_PROFILE, executable_path=str(tmp_path / "missing" / "7zz")
    )
    with pytest.raises(ArchiveToolUnavailable):
        SevenZipBackend(profile).inspect((Path("book.7z"),), None)


def test_resolve_seven_zip_executable_prefers_managed_install(
    tmp_path: Path,
) -> None:
    """A managed install under the data directory wins over host lookup."""
    managed = install_root(tmp_path / "tools") / "7zzs"
    managed.parent.mkdir(parents=True, exist_ok=True)
    managed.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    managed.chmod(0o755)

    resolved = resolve_seven_zip_executable("7zz", tmp_path / "tools")

    assert resolved == str(managed)


def test_resolve_seven_zip_executable_ignores_empty_managed_directory(
    tmp_path: Path,
) -> None:
    resolved = resolve_seven_zip_executable("7zz", tmp_path / "tools")

    # Falls back to whatever the host provides, which may be nothing.
    assert resolved is None or Path(resolved).is_file()


def test_seven_zip_backend_extract_requires_expected_members(
    tmp_path: Path,
) -> None:
    source = tmp_path / "book.7z"
    source.write_bytes(b"7z\xbc\xaf\x27\x1c")

    def runner(arguments: tuple[str, ...]) -> tuple[int, str]:
        return 0, ""

    backend = SevenZipBackend(SEVEN_ZIP_PROFILE, runner=runner)
    with pytest.raises(ArchiveError) as error:
        backend.extract(
            (source,), tmp_path / "out", None, (_member("01.jpg"),)
        )
    assert error.value.code == "ARCHIVE_MEMBER_MISSING"


# --- processor ------------------------------------------------------------


def _processor(**kwargs) -> ArchiveProcessor:
    kwargs.setdefault("profiles", ZIP_ONLY_PROFILES)
    return ArchiveProcessor(**kwargs)


def test_processor_selects_streaming_profile_for_zip() -> None:
    processor = ArchiveProcessor(profiles=ALL_PROFILES)
    assert processor.select_profile("zip").backend == "zipfile"
    assert processor.select_profile("rar").backend == "seven_zip"
    with pytest.raises(UnsupportedArchiveFormat):
        processor.select_profile("unknown")


def test_processor_rejects_format_without_enabled_profile() -> None:
    with pytest.raises(UnsupportedArchiveFormat):
        _processor().select_profile("rar")


def test_processor_reports_missing_volumes(tmp_path: Path) -> None:
    (tmp_path / "book.part1.rar").write_bytes(b"Rar!\x1a\x07\x00")
    (tmp_path / "book.part3.rar").write_bytes(b"Rar!\x1a\x07\x00")
    processor = ArchiveProcessor(profiles=ALL_PROFILES)
    with pytest.raises(ArchiveVolumesMissing) as error:
        processor.process(
            tmp_path / "book.part1.rar",
            destination=tmp_path / "out.cbz",
            work_directory=tmp_path / "work",
            comicinfo_builder=lambda count: b"<ComicInfo />",
        )
    assert error.value.missing == ("book.part2.rar",)


def test_processor_records_task_snapshot(tmp_path: Path) -> None:
    source = tmp_path / "src.zip"
    write_image_zip(source, ("01.jpg", "02.jpg"))
    result = _processor().process(
        source,
        destination=tmp_path / "library" / "out.cbz",
        work_directory=tmp_path / "work",
        comicinfo_builder=lambda count: b"<ComicInfo />",
        library_path=tmp_path / "library",
    )
    assert result.snapshot.backend == "zipfile"
    assert result.snapshot.tool_profile == "zipfile-default"
    assert result.snapshot.source_format == "zip"
    assert result.page_count == 2
    assert result.volume_count == 1
    assert result.password_id is None


def test_processor_cleans_up_work_directory(tmp_path: Path) -> None:
    source = tmp_path / "src.zip"
    write_image_zip(source, ("01.jpg",))
    work = tmp_path / "work"
    _processor().process(
        source,
        destination=tmp_path / "out.cbz",
        work_directory=work,
        comicinfo_builder=lambda count: b"<ComicInfo />",
    )
    assert not (work / "extract-out").exists()


def test_processor_tries_vault_passwords_in_order(tmp_path: Path) -> None:
    """The first working password wins and is reported for bookkeeping."""
    source = tmp_path / "encrypted.zip"
    write_image_zip(source, ("01.jpg",))
    attempted: list[str | None] = []

    class FakeBackend:
        streaming = False

        def inspect(self, volumes, password):
            return ArchiveManifest(
                source_format="zip",
                members=(_member("01.jpg"),),
                volumes=volumes,
                encrypted=True,
            )

        def test_password(self, volumes, password):
            attempted.append(password)
            if password != "good":
                raise ArchivePasswordRequired()

        def extract(self, volumes, destination, password, members):
            destination.mkdir(parents=True, exist_ok=True)
            target = destination / "01.jpg"
            target.write_bytes(image_bytes("01.jpg"))
            return {"01.jpg": target}

        def pack_cbz(self, pages, destination, comicinfo):
            return ZipfileBackend().pack_cbz(pages, destination, comicinfo)

    processor = _processor(passwords=((7, "bad"), (9, "good")))
    processor.build_backend = lambda profile: FakeBackend()
    result = processor.process(
        source,
        destination=tmp_path / "out.cbz",
        work_directory=tmp_path / "work",
        comicinfo_builder=lambda count: b"<ComicInfo />",
    )
    assert attempted == ["bad", "good"]
    assert result.password_id == 9


# --- password vault ------------------------------------------------------


def test_vault_round_trip_hides_plaintext() -> None:
    key = generate_master_key()
    envelope = encrypt_password(key, "s3cret-password")
    assert "s3cret-password" not in envelope
    assert decrypt_password(key, envelope) == "s3cret-password"


def test_vault_rejects_tampered_ciphertext() -> None:
    key = generate_master_key()
    envelope = encrypt_password(key, "value")
    tampered = envelope.replace('"tag":"', '"tag":"A')
    with pytest.raises(VaultError):
        decrypt_password(key, tampered)


def test_vault_rejects_other_master_key() -> None:
    envelope = encrypt_password(generate_master_key(), "value")
    with pytest.raises(VaultError):
        decrypt_password(generate_master_key(), envelope)


def test_vault_rejects_empty_password() -> None:
    with pytest.raises(VaultError):
        encrypt_password(generate_master_key(), "")