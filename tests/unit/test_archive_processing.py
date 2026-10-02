from dataclasses import replace
import io
from pathlib import Path
import platform
import zipfile

import pytest

from app.archive.backends.seven_zip import (
    SevenZipBackend,
    parse_slt_listing,
    resolve_seven_zip_executable,
)
from app.archive.backends.zip_backend import ZipfileBackend
from app.archive.errors import (
    ArchiveBackendUnsupported,
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
from app.archive.toolchain import (
    PREFERRED_BINARIES,
    WINDOWS_EXECUTABLE,
    WINDOWS_LIBRARY,
    install_root,
)
from app.archive.safety import (
    detected_image_extension,
    effective_page_extension,
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

from app.archive.quality import (
    QUALITY_HIGH,
    QUALITY_LOW,
    QUALITY_MEDIUM,
    QUALITY_ORIGINAL,
    normalize_quality,
    quality_note,
    quality_profile,
    reencode_page,
)

from tests.unit.archive_fixtures import (
    ALL_PROFILES,
    JPEG_HEADER,
    PNG_HEADER,
    SEVEN_ZIP_PROFILE,
    ZIP_ONLY_PROFILES,
    image_bytes,
    real_jpeg_bytes,
    write_image_zip,
    write_real_image_zip,
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
    # The leading volume of a legacy `.r00` series belongs to the same group as
    # its companions, so the set can also be found from the `.rar` end.
    assert volume_group(Path("a.rar")) == "a.rar"
    assert volume_group(Path("a.cbr")) == "a.rar"
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


def test_resolve_volumes_finds_a_legacy_rnn_series_from_either_end(
    tmp_path: Path,
) -> None:
    """`.rar` + `.r00`/`.r01` must group the same way from either direction.

    Before this, starting from the leading `.rar` returned a single volume, so
    the missing-volume gate never fired for a truncated legacy set.
    """
    for name in ("book.rar", "book.r00", "book.r01"):
        (tmp_path / name).write_bytes(b"Rar!\x1a\x07\x00")
    from_rar = resolve_volumes(tmp_path / "book.rar")
    from_r00 = resolve_volumes(tmp_path / "book.r00")
    assert from_rar == from_r00
    assert [path.name for path in from_rar[0]] == [
        "book.rar",
        "book.r00",
        "book.r01",
    ]
    assert from_rar[1] == ()


def test_resolve_volumes_reports_a_gap_in_a_legacy_rnn_series(
    tmp_path: Path,
) -> None:
    for name in ("book.rar", "book.r01"):
        (tmp_path / name).write_bytes(b"Rar!\x1a\x07\x00")
    volumes, missing = resolve_volumes(tmp_path / "book.rar")
    assert [path.name for path in volumes] == ["book.rar", "book.r01"]
    assert missing == ("book.r00",)


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

    # A payload that is not an image at all, so no amount of ratio is the
    # archive's way of saying "these are flat pages". Refused by the ratio
    # gate, which runs after the per-member gates: naming this member `.jpg`
    # instead would make it `ARCHIVE_MEMBER_FAKE_IMAGE` first, which is the
    # more precise refusal of the two.
    with pytest.raises(ArchiveSafetyError) as ratio_error:
        validate_manifest(
            _manifest(
                _member(
                    "payload.bin",
                    size=10_000_000,
                    compressed_size=10,
                    header=b"<html><body>not an image</body></html>",
                )
            ),
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


def test_the_ratio_gate_does_not_fire_on_a_real_image() -> None:
    """A flat page is the one thing a real JPEG compresses like a bomb.

    JPEG's entropy-coded output for a blank or solid-colour page deflates
    hundreds of times over -- a 4800x4800 solid-white page measured 428x in a
    zip and 548x in a 7z -- so a ratio gate that cannot tell "redundant image"
    from "not an image" rejects legitimate pages. Positive identification of
    the container is what separates them: a member whose header carries a real
    image signature passes regardless of its ratio.
    """
    pages = validate_manifest(
        _manifest(
            _member("01.jpg", size=10_000_000, compressed_size=10),
            _member("02.png", size=10_000_000, compressed_size=10, header=PNG_HEADER),
        ),
        SafetyLimits(),
    )

    assert [member.name for member in pages] == ["01.jpg", "02.png"]


def _solid(
    *members: ArchiveMember, block: int = 0
) -> tuple[ArchiveMember, ...]:
    """Members of one solid block, in the shape the 7zz listing produces.

    `Packed Size` lands on the block's first member and is absent (zero) for
    the rest, which is exactly the shape that made a per-member ratio wrong.
    """
    return tuple(
        replace(member, block=block)
        for member in members
    )


def test_a_solid_block_is_judged_by_the_block_not_its_leader() -> None:
    """The old rule charged the leader with the whole block and hid the bomb.

    A small compressible leader with a large payload behind it in the same
    solid block: read one member at a time, the leader's own ratio is 1.1 and
    the payload has no packed size at all, so nothing fired. The block's real
    expansion is what the number on the leader describes.
    """
    members = _solid(
        _member("01.jpg", size=1_000, compressed_size=900, header=b""),
        _member("02.jpg", size=10_000_000, compressed_size=0, header=b""),
    )

    with pytest.raises(ArchiveSafetyError) as caught:
        validate_manifest(_manifest(*members), SafetyLimits())

    assert caught.value.code == "ARCHIVE_COMPRESSION_RATIO"
    # The refusal names the block, because no single member's size explains it.
    assert "01.jpg" in caught.value.public_message


def test_a_flagged_block_is_probed_and_passes_when_it_is_an_image() -> None:
    """7z carries no member bytes, so the gate asks the backend for the head.

    The probe costs a decompression, which is why it happens only after the
    ratio has already flagged the block -- and only once per block, on the
    block's first member.
    """
    members = _solid(
        _member("01.jpg", size=1_000, compressed_size=900, header=b""),
        _member("02.jpg", size=10_000_000, compressed_size=0, header=b""),
    )
    probed: list[str] = []

    def read_header(member: ArchiveMember) -> bytes:
        probed.append(member.name)
        return JPEG_HEADER

    pages = validate_manifest(
        _manifest(*members), SafetyLimits(), read_header=read_header
    )

    assert probed == ["01.jpg"]
    assert [member.name for member in pages] == ["01.jpg", "02.jpg"]


def test_a_flagged_block_is_refused_when_the_probe_is_not_an_image() -> None:
    members = _solid(
        _member("01.jpg", size=1_000, compressed_size=900, header=b""),
        _member("02.jpg", size=10_000_000, compressed_size=0, header=b""),
    )

    with pytest.raises(ArchiveSafetyError) as caught:
        validate_manifest(
            _manifest(*members),
            SafetyLimits(),
            read_header=lambda member: b"\x00\x00\x00\x00",
        )

    assert caught.value.code == "ARCHIVE_COMPRESSION_RATIO"


def test_an_encrypted_archive_is_never_probed() -> None:
    """A member-level password is not something the safety gate handles.

    Encrypted bytes cannot be identified without it, so the refusal stands --
    and the probe must not be attempted, because that would put the password
    handling in the gate.
    """
    members = _solid(
        _member("01.jpg", size=1_000, compressed_size=900, header=b""),
        _member("02.jpg", size=10_000_000, compressed_size=0, header=b""),
    )
    manifest = ArchiveManifest(
        source_format="7z", members=members, encrypted=True
    )

    def read_header(member: ArchiveMember) -> bytes:  # pragma: no cover
        raise AssertionError("the gate must not probe an encrypted archive")

    with pytest.raises(ArchiveSafetyError) as caught:
        validate_manifest(manifest, SafetyLimits(), read_header=read_header)

    assert caught.value.code == "ARCHIVE_COMPRESSION_RATIO"


def test_a_block_under_the_limit_is_not_probed() -> None:
    members = _solid(
        _member("01.jpg", size=1_000, compressed_size=900, header=b""),
        _member("02.jpg", size=1_000, compressed_size=0, header=b""),
    )

    def read_header(member: ArchiveMember) -> bytes:  # pragma: no cover
        raise AssertionError("a block under the limit must not be probed")

    pages = validate_manifest(
        _manifest(*members), SafetyLimits(), read_header=read_header
    )

    assert [member.name for member in pages] == ["01.jpg", "02.jpg"]


def test_the_ratio_gate_still_fires_when_no_header_was_captured() -> None:
    """The 7zz listing carries no member bytes, and that must not be an exemption.

    `detected` is None both for bytes that are not an image and for bytes the
    backend never read. Reading it the other way round would switch the gate
    off for every 7z and rar archive, where it is the only content gate that
    fires at all.
    """
    with pytest.raises(ArchiveSafetyError) as caught:
        validate_manifest(
            _manifest(
                _member("01.jpg", size=10_000_000, compressed_size=10, header=b"")
            ),
            SafetyLimits(),
        )

    assert caught.value.code == "ARCHIVE_COMPRESSION_RATIO"


def test_validate_manifest_rejects_fake_image_extension() -> None:
    """Bytes that are not any image this application knows still fail the book.

    This is the half of the check that must not be relaxed: `01.png` holding an
    executable is refused exactly as it was before the mislabelled-page repair
    was added beside it.
    """
    with pytest.raises(ArchiveSafetyError) as error:
        validate_manifest(
            _manifest(_member("01.png", header=b"MZ\x90\x00 not an image")),
            SafetyLimits(),
        )
    assert error.value.code == "ARCHIVE_MEMBER_FAKE_IMAGE"
    # The message has to say which of the two things was wrong, because the
    # remedies differ: a mislabelled page is repaired silently, and this is not
    # that case.
    assert "不是可识别的图片格式" in error.value.public_message


# --- 放宽解压校验 (item 5) ---------------------------------------------------
#
# The reported problem: one page whose header disagreed with its extension --
# what a batch converter leaves behind and no uploader notices -- failed the
# whole archive with `ARCHIVE_MEMBER_FAKE_IMAGE`, so a complete two-hundred-page
# book could not be packed at all. A real image under a wrong name is now
# published under the extension its bytes actually are.


def test_a_mislabelled_page_is_repaired_instead_of_failing_the_book() -> None:
    pages = validate_manifest(
        _manifest(
            _member("01.png", header=JPEG_HEADER),
            _member("02.jpg", header=PNG_HEADER),
            _member("03.jpg", header=JPEG_HEADER),
        ),
        SafetyLimits(),
    )
    assert [member.name for member in pages] == ["01.png", "02.jpg", "03.jpg"]
    # Each page carries the extension of its own bytes: readers dispatch on the
    # extension, so publishing JPEG bytes as `.png` is a page that silently
    # fails to render in some of them.
    assert page_file_names(pages) == ("0001.jpg", "0002.png", "0003.jpg")


def test_an_extensionless_member_is_judged_by_its_bytes() -> None:
    """Books whose pages are named `001` used to fail as `ARCHIVE_NO_IMAGES`."""
    pages = validate_manifest(
        _manifest(_member("001", header=JPEG_HEADER), _member("002", header=PNG_HEADER)),
        SafetyLimits(),
    )
    assert len(pages) == 2
    assert page_file_names(pages) == ("0001.jpg", "0002.png")


def test_an_extensionless_non_image_is_still_not_a_page() -> None:
    """Relaxing the naming rule must not turn every stray file into a page."""
    with pytest.raises(ArchiveSafetyError) as error:
        validate_manifest(
            _manifest(_member("readme", header=b"just some text here")),
            SafetyLimits(),
        )
    assert error.value.code == "ARCHIVE_NO_IMAGES"


def test_detected_image_extension_names_only_what_it_can_prove() -> None:
    assert detected_image_extension(JPEG_HEADER) == ".jpg"
    assert detected_image_extension(PNG_HEADER) == ".png"
    assert detected_image_extension(b"GIF89a" + b"\x00" * 6) == ".gif"
    assert detected_image_extension(b"BM" + b"\x00" * 10) == ".bmp"
    assert detected_image_extension(b"RIFF\x00\x00\x00\x00WEBP") == ".webp"
    # `RIFF` alone is any RIFF container, including audio.
    assert detected_image_extension(b"RIFF\x00\x00\x00\x00WAVE") is None
    assert detected_image_extension(b"MZ\x90\x00") is None
    assert detected_image_extension(b"") is None


def test_a_page_named_for_a_signatureless_format_is_repaired_too() -> None:
    """`.avif` has no fixed signature, so the mismatch check cannot see it.

    `header_matches_extension` answers True for JPEG bytes called `01.avif` --
    there is no AVIF signature to compare against -- so the page would have been
    published as `.avif` and failed to open. Repair keys on positive
    identification instead, which sees it.
    """
    assert effective_page_extension("01.avif", JPEG_HEADER) == ".jpg"
    # And a page whose bytes really are unidentifiable keeps the name it came
    # with: an actual AVIF must not be renamed on a guess.
    assert effective_page_extension("01.avif", b"\x00\x00\x00 ftypavif") == ".avif"


def test_a_correctly_named_page_keeps_its_own_extension() -> None:
    """`.jpeg` stays `.jpeg`: agreement is not a reason to rewrite a name."""
    assert effective_page_extension("01.jpeg", JPEG_HEADER) == ".jpeg"
    assert effective_page_extension("01.png", PNG_HEADER) == ".png"
    # No header captured at all is no evidence, so the claim stands.
    assert effective_page_extension("01.webp", b"") == ".webp"


def test_page_file_names_are_stable_and_collision_free() -> None:
    # `_member` defaults to a JPEG header, so the PNG page has to be given its
    # own: page names now follow the bytes, and a `.png` name over JPEG bytes is
    # the mislabelled-page case covered below rather than this one.
    names = page_file_names(
        (
            _member("b/01.jpg"),
            _member("a/01.jpg"),
            _member("cover.png", header=PNG_HEADER),
        )
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


def _rewrite_zip_method(path: Path, method: int) -> None:
    """Rewrite the compression-method field of every ZIP record."""
    data = bytearray(path.read_bytes())
    payload = method.to_bytes(2, "little")
    # Local file header has the method at offset 8, central directory at 10.
    for signature, offset in ((b"PK\x03\x04", 8), (b"PK\x01\x02", 10)):
        start = data.find(signature)
        while start != -1:
            data[start + offset : start + offset + 2] = payload
            start = data.find(signature, start + 1)
    path.write_bytes(bytes(data))


def test_zipfile_backend_rejects_a_method_it_cannot_decode(
    tmp_path: Path,
) -> None:
    """AES-256 (99) and Deflate64 (9) are a backend mismatch, not a password.

    The listing carries the method, so admission can hand the archive to
    7-Zip instead of reporting "wrong password" for every vault entry.
    """
    source = tmp_path / "aes.zip"
    write_image_zip(source, ("01.jpg",))
    _rewrite_zip_method(source, 99)

    with pytest.raises(ArchiveBackendUnsupported) as error:
        ZipfileBackend().inspect((source,), None)
    assert error.value.code == "ARCHIVE_COMPRESSION_UNSUPPORTED"
    assert "99" in error.value.public_message

    with pytest.raises(ArchiveBackendUnsupported):
        ZipfileBackend().test_password((source,), "S3cret")


def test_zipfile_backend_keeps_supported_methods_on_the_builtin_path(
    tmp_path: Path,
) -> None:
    source = tmp_path / "plain.zip"
    write_image_zip(source, ("01.jpg", "02.jpg"))
    manifest = ZipfileBackend().inspect((source,), None)
    assert manifest.member_count == 2
    assert manifest.encrypted is False


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
Block = 0
Attributes = _ -----
Encrypted = -

Path = sub
Size = 0
Packed Size = 0
Attributes = D_ ----

Path = sub/02.jpg
Size = 4096
Packed Size = 1024
Block = 0
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
    # The block id is what tells the ratio gate that this `Packed Size` is a
    # property of the block rather than of this one member.
    assert members[0].block == 0
    assert members[2].block == 0
    # A directory has no block id, and neither does an archive that reports a
    # packed size per member.
    assert members[1].block is None


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


def test_seven_zip_pack_cbz_falls_back_to_copy_when_linking_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A cross-device hardlink must not fail the pack; a copy is the fallback.

    The operator's work and library directories are different mounts, and two
    bind mounts of one host filesystem can report an equal `st_dev` while
    still refusing the link, so the link itself has to be allowed to fail.
    """
    page = tmp_path / "src" / "0001.jpg"
    page.parent.mkdir(parents=True)
    page.write_bytes(image_bytes("0001.jpg"))
    observed: dict[str, object] = {}

    def runner(arguments, working_directory=None):
        staged = Path(working_directory) / "0001.jpg"
        observed["staged"] = staged.read_bytes()
        observed["linked"] = staged.stat().st_ino == page.stat().st_ino
        return 0, ""

    def refuse(self, target):
        raise OSError(18, "Invalid cross-device link")

    monkeypatch.setattr(Path, "hardlink_to", refuse)
    backend = SevenZipBackend(SEVEN_ZIP_PROFILE, runner=runner)
    written = backend.pack_cbz(
        (("0001.jpg", page),), tmp_path / "out.cbz.part", b"<ComicInfo />"
    )

    assert written == 1
    assert observed["staged"] == page.read_bytes()
    assert observed["linked"] is False


def test_seven_zip_pack_cbz_hardlinks_when_the_filesystem_allows_it(
    tmp_path: Path,
) -> None:
    """The copy is only a fallback: same-device staging still links."""
    page = tmp_path / "src" / "0001.jpg"
    page.parent.mkdir(parents=True)
    page.write_bytes(image_bytes("0001.jpg"))
    observed: dict[str, object] = {}

    def runner(arguments, working_directory=None):
        staged = Path(working_directory) / "0001.jpg"
        observed["linked"] = staged.stat().st_ino == page.stat().st_ino
        return 0, ""

    backend = SevenZipBackend(SEVEN_ZIP_PROFILE, runner=runner)
    written = backend.pack_cbz(
        (("0001.jpg", page),), tmp_path / "out.cbz.part", b"<ComicInfo />"
    )

    assert written == 1
    assert observed["linked"] is True


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
    """A managed install under the data directory is the only lookup.

    The managed layout is per platform -- `7z.exe` beside `7z.dll` on Windows,
    the standalone `7zz`/`7zzs` elsewhere -- and `installed_executable` also
    requires the executable bit, which a POSIX host takes from the mode and
    Windows from the `.exe` extension. Write the pair this host looks for, or
    the test only passes on Windows.
    """
    root = install_root(tmp_path / "tools")
    root.mkdir(parents=True, exist_ok=True)
    if platform.system().strip().lower() == "windows":
        managed = root / WINDOWS_EXECUTABLE
        managed.write_bytes(b"managed executable")
        (root / WINDOWS_LIBRARY).write_bytes(b"managed runtime")
    else:
        managed = root / PREFERRED_BINARIES[0]
        managed.write_bytes(b"managed executable")
        managed.chmod(0o755)

    resolved = resolve_seven_zip_executable("7zz", tmp_path / "tools")

    assert resolved == str(managed)


def test_resolve_seven_zip_executable_never_uses_the_host_environment(
    tmp_path: Path,
) -> None:
    resolved = resolve_seven_zip_executable("7zz", tmp_path / "tools")

    assert resolved is None


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


def test_a_book_with_mislabelled_pages_still_packs(tmp_path: Path) -> None:
    """The whole point of the relaxation, driven through the real processor.

    Before this, the archive below produced `ARCHIVE_MEMBER_FAKE_IMAGE` and no
    CBZ at all, because one page was named `.png` while holding JPEG bytes.
    """
    source = tmp_path / "src.zip"
    with zipfile.ZipFile(source, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("01.png", image_bytes("01.jpg"))
        archive.writestr("02.jpg", image_bytes("02.jpg"))
    destination = tmp_path / "library" / "out.cbz"

    result = _processor().process(
        source,
        destination=destination,
        work_directory=tmp_path / "work",
        comicinfo_builder=lambda count: b"<ComicInfo />",
        library_path=tmp_path / "library",
    )

    assert result.page_count == 2
    # The mislabelled page is published as what it is, and the bytes are
    # untouched -- the repair is a rename, not a transcode.
    pages = _cbz_pages(destination)
    assert sorted(pages) == ["0001.jpg", "0002.jpg"]
    assert pages["0001.jpg"] == image_bytes("01.jpg")


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


class _UnreadableBackend:
    """Stands in for a backend whose decoder does not know the method."""

    streaming = False

    def inspect(self, volumes, password):
        raise ArchiveBackendUnsupported("built-in reader cannot decode this")

    def test_password(self, volumes, password):
        raise ArchiveBackendUnsupported("built-in reader cannot decode this")

    def extract(self, volumes, destination, password, members):
        raise ArchiveBackendUnsupported("built-in reader cannot decode this")

    def pack_cbz(self, pages, destination, comicinfo):
        raise ArchiveBackendUnsupported("built-in reader cannot decode this")


class _ReadableBackend:
    streaming = False

    def inspect(self, volumes, password):
        return ArchiveManifest(
            source_format="zip",
            members=(_member("01.jpg"),),
            volumes=volumes,
            encrypted=False,
        )

    def test_password(self, volumes, password):
        return None

    def extract(self, volumes, destination, password, members):
        destination.mkdir(parents=True, exist_ok=True)
        target = destination / "01.jpg"
        target.write_bytes(image_bytes("01.jpg"))
        return {"01.jpg": target}

    def pack_cbz(self, pages, destination, comicinfo):
        return ZipfileBackend().pack_cbz(pages, destination, comicinfo)


def _fake_build_backend(profile):
    if profile.backend == "zipfile":
        return _UnreadableBackend()
    return _ReadableBackend()


def test_processor_falls_back_to_the_next_profile(tmp_path: Path) -> None:
    """A backend that cannot decode the method must not end the attempt."""
    source = tmp_path / "src.zip"
    write_image_zip(source, ("01.jpg",))
    processor = ArchiveProcessor(profiles=ALL_PROFILES)
    processor.build_backend = _fake_build_backend
    result = processor.process(
        source,
        destination=tmp_path / "out.cbz",
        work_directory=tmp_path / "work",
        comicinfo_builder=lambda count: b"<ComicInfo />",
    )
    assert result.snapshot.backend == "seven_zip"
    assert result.snapshot.tool_profile == "7zz-default"
    assert result.page_count == 1


def test_processor_does_not_fall_back_on_a_password_failure(
    tmp_path: Path,
) -> None:
    """Only "this backend can't read it" moves on; a wrong vault does not."""
    source = tmp_path / "src.zip"
    write_image_zip(source, ("01.jpg",))
    built: list[str] = []

    class PasswordFailingBackend(_ReadableBackend):
        def test_password(self, volumes, password):
            raise ArchivePasswordRequired()

    def build(profile):
        built.append(profile.backend)
        return PasswordFailingBackend()

    processor = ArchiveProcessor(profiles=ALL_PROFILES)
    processor.build_backend = build
    with pytest.raises(ArchivePasswordRequired):
        processor.process(
            source,
            destination=tmp_path / "out.cbz",
            work_directory=tmp_path / "work",
            comicinfo_builder=lambda count: b"<ComicInfo />",
        )
    assert built == ["zipfile"]


def test_processor_names_the_fallback_when_nothing_can_read(
    tmp_path: Path,
) -> None:
    source = tmp_path / "src.zip"
    write_image_zip(source, ("01.jpg",))
    processor = _processor()
    processor.build_backend = lambda profile: _UnreadableBackend()
    with pytest.raises(ArchiveError) as error:
        processor.process(
            source,
            destination=tmp_path / "out.cbz",
            work_directory=tmp_path / "work",
            comicinfo_builder=lambda count: b"<ComicInfo />",
        )
    assert error.value.code == "ARCHIVE_COMPRESSION_UNSUPPORTED"
    assert "7-Zip" in error.value.public_message


# --- image quality -------------------------------------------------------


def _jpeg_dimensions(data: bytes) -> tuple[int, int]:
    from PIL import Image

    with Image.open(io.BytesIO(data)) as image:
        return image.size


def _cbz_pages(path: Path) -> dict[str, bytes]:
    with zipfile.ZipFile(path) as archive:
        return {
            name: archive.read(name)
            for name in archive.namelist()
            if name != "ComicInfo.xml"
        }


def test_unknown_quality_level_falls_back_to_original() -> None:
    """A stored value that no longer maps to a preset must never re-encode."""
    assert normalize_quality(None) == QUALITY_ORIGINAL
    assert normalize_quality("") == QUALITY_ORIGINAL
    assert normalize_quality("ultra") == QUALITY_ORIGINAL
    assert normalize_quality(" HIGH ") == QUALITY_HIGH
    assert not quality_profile("ultra").rewrites


def test_quality_presets_match_the_documented_table() -> None:
    """The presets are a published contract, so they are pinned by a test."""
    assert quality_profile(QUALITY_HIGH).jpeg_quality == 85
    assert quality_profile(QUALITY_HIGH).max_edge is None
    assert quality_profile(QUALITY_MEDIUM).jpeg_quality == 60
    assert quality_profile(QUALITY_MEDIUM).max_edge is None
    assert quality_profile(QUALITY_LOW).jpeg_quality == 40
    assert quality_profile(QUALITY_LOW).max_edge == 3000


def test_quality_note_only_describes_a_real_re_encode() -> None:
    assert quality_note(QUALITY_ORIGINAL) == ""
    assert quality_note(None) == ""
    assert quality_note(QUALITY_MEDIUM) == "requality=medium q60"
    assert quality_note(QUALITY_LOW) == "requality=low q40 max3000px"


def test_default_quality_publishes_pages_byte_for_byte(tmp_path: Path) -> None:
    """`original` is the default and must not touch a single page."""
    source = tmp_path / "src.zip"
    write_real_image_zip(source, ("01.jpg", "02.jpg"))
    destination = tmp_path / "out.cbz"
    result = _processor().process(
        source,
        destination=destination,
        work_directory=tmp_path / "work",
        comicinfo_builder=lambda count: b"<ComicInfo />",
    )
    with zipfile.ZipFile(source) as original:
        expected = original.read("01.jpg")
    assert result.image_quality == QUALITY_ORIGINAL
    assert result.rewritten_pages == 0
    assert _cbz_pages(destination)["0001.jpg"] == expected


def test_each_quality_level_shrinks_pages_more_than_the_last(
    tmp_path: Path,
) -> None:
    """The three presets must be ordered by size, not just by JPEG number."""
    source = tmp_path / "src.zip"
    write_real_image_zip(source, ("01.jpg",))
    sizes: dict[str, int] = {}
    for level in (QUALITY_ORIGINAL, QUALITY_HIGH, QUALITY_MEDIUM, QUALITY_LOW):
        destination = tmp_path / f"{level}.cbz"
        result = _processor(image_quality=level).process(
            source,
            destination=destination,
            work_directory=tmp_path / f"work-{level}",
            comicinfo_builder=lambda count: b"<ComicInfo />",
        )
        assert result.image_quality == level
        assert result.rewritten_pages == (0 if level == QUALITY_ORIGINAL else 1)
        sizes[level] = len(_cbz_pages(destination)["0001.jpg"])
    assert (
        sizes[QUALITY_ORIGINAL]
        > sizes[QUALITY_HIGH]
        > sizes[QUALITY_MEDIUM]
        > sizes[QUALITY_LOW]
    )


def test_low_quality_downscales_only_oversized_pages(tmp_path: Path) -> None:
    """The 3000px cap must resize a huge page and leave a small one alone."""
    staging = tmp_path / "staging"
    profile = quality_profile(QUALITY_LOW)

    small = tmp_path / "small.jpg"
    small.write_bytes(real_jpeg_bytes(width=400, height=200))
    outcome = reencode_page("0001.jpg", small, profile, staging / "small")
    assert outcome.rewritten
    assert _jpeg_dimensions(outcome.path.read_bytes()) == (400, 200)

    large = tmp_path / "large.jpg"
    large.write_bytes(real_jpeg_bytes(width=3600, height=1800))
    outcome = reencode_page("0001.jpg", large, profile, staging / "large")
    assert outcome.rewritten
    assert _jpeg_dimensions(outcome.path.read_bytes()) == (3000, 1500)


def test_png_pages_are_never_transcoded(tmp_path: Path) -> None:
    """PNG line art loses alpha and often grows as JPEG, so it is passed through."""
    source = tmp_path / "src.zip"
    write_real_image_zip(source, ("01.png", "02.jpg"))
    destination = tmp_path / "out.cbz"
    result = _processor(image_quality=QUALITY_LOW).process(
        source,
        destination=destination,
        work_directory=tmp_path / "work",
        comicinfo_builder=lambda count: b"<ComicInfo />",
    )
    with zipfile.ZipFile(source) as original:
        expected_png = original.read("01.png")
    pages = _cbz_pages(destination)
    assert pages["0001.png"] == expected_png
    assert result.rewritten_pages == 1


def test_re_encode_keeps_the_original_when_it_would_grow(tmp_path: Path) -> None:
    """Spending CPU to publish a bigger, lossier page is strictly worse."""
    already_small = tmp_path / "0001.jpg"
    already_small.write_bytes(real_jpeg_bytes(width=64, height=64, quality=20))
    outcome = reencode_page(
        "0001.jpg", already_small, quality_profile(QUALITY_HIGH), tmp_path / "s"
    )
    assert not outcome.rewritten
    assert outcome.path == already_small
    assert outcome.final_bytes == outcome.original_bytes


def test_undecodable_page_is_shipped_as_is(tmp_path: Path) -> None:
    """A page Pillow cannot read must not fail an otherwise complete book."""
    broken = tmp_path / "0001.jpg"
    broken.write_bytes(JPEG_HEADER + b"\x00" * 128)
    outcome = reencode_page(
        "0001.jpg", broken, quality_profile(QUALITY_MEDIUM), tmp_path / "s"
    )
    assert not outcome.rewritten
    assert outcome.path == broken


def test_re_encode_preserves_page_order_and_names(tmp_path: Path) -> None:
    """A quality change must never reorder or rename the pages of a book."""
    source = tmp_path / "src.zip"
    write_real_image_zip(source, ("2.jpg", "10.jpg", "1.jpg"))
    destination = tmp_path / "out.cbz"
    _processor(image_quality=QUALITY_MEDIUM).process(
        source,
        destination=destination,
        work_directory=tmp_path / "work",
        comicinfo_builder=lambda count: b"<ComicInfo />",
    )
    with zipfile.ZipFile(destination) as archive:
        assert archive.namelist() == [
            "ComicInfo.xml",
            "0001.jpg",
            "0002.jpg",
            "0003.jpg",
        ]


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
