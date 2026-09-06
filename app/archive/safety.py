from __future__ import annotations

import logging
import re
from pathlib import PurePosixPath

from app.archive.errors import ArchiveSafetyError
from app.archive.models import ArchiveManifest, ArchiveMember, SafetyLimits


LOGGER = logging.getLogger(__name__)


IMAGE_EXTENSIONS: frozenset[str] = frozenset(
    {".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp", ".avif", ".jxl"}
)

# Extensions that must never be published inside a CBZ.
NESTED_ARCHIVE_EXTENSIONS: frozenset[str] = frozenset(
    {".zip", ".cbz", ".rar", ".cbr", ".7z", ".cb7", ".tar", ".gz", ".xz", ".bz2"}
)

ALLOWED_SIDECAR_NAMES: frozenset[str] = frozenset({"comicinfo.xml"})

_IMAGE_SIGNATURES: tuple[tuple[bytes, frozenset[str]], ...] = (
    (b"\xff\xd8\xff", frozenset({".jpg", ".jpeg"})),
    (b"\x89PNG\r\n\x1a\n", frozenset({".png"})),
    (b"GIF87a", frozenset({".gif"})),
    (b"GIF89a", frozenset({".gif"})),
    (b"BM", frozenset({".bmp"})),
)

#: The extension each signature *should* have carried. One canonical extension
#: per format rather than the set of names that format may go by: a page repaired
#: to `.jpeg` would be just as correct and needlessly unlike every other page in
#: the book.
_SIGNATURE_EXTENSIONS: tuple[tuple[bytes, str], ...] = (
    (b"\xff\xd8\xff", ".jpg"),
    (b"\x89PNG\r\n\x1a\n", ".png"),
    (b"GIF87a", ".gif"),
    (b"GIF89a", ".gif"),
    (b"BM", ".bmp"),
)

#: Every extension that correctly names a given detected format, so a page that
#: is already named acceptably is left alone. `.jpeg` and `.jpg` are the same
#: claim about the same bytes, and rewriting one into the other would be churn
#: dressed up as a repair.
_EXTENSION_ALIASES: dict[str, frozenset[str]] = {
    ".jpg": frozenset({".jpg", ".jpeg"}),
    ".png": frozenset({".png"}),
    ".gif": frozenset({".gif"}),
    ".bmp": frozenset({".bmp"}),
    ".webp": frozenset({".webp"}),
}

_DIGITS = re.compile(r"(\d+)")

#: First bytes of every image container this application is willing to open.
#: ``RIFF`` covers WebP and the ``\x00\x00\x00`` prefix covers the ISO-BMFF box
#: length that AVIF and HEIF start with. An SVG or an HTML error page fails
#: here, which is the point.
_CONTAINER_PREFIXES: tuple[bytes, ...] = (
    b"\xff\xd8\xff",
    b"\x89PNG\r\n\x1a\n",
    b"GIF87a",
    b"GIF89a",
    b"BM",
    b"RIFF",
    b"\x00\x00\x00",
)


def looks_like_image(data: bytes) -> bool:
    """Accept only payloads whose first bytes match a known image container.

    This is the "is this an image at all" gate, distinct from
    ``header_matches_extension``, which cross-checks a *claimed* extension and
    is deliberately permissive. Callers that pull bytes off the network — the
    Telegraph fetcher, the thumbnail proxy — use this one before handing the
    payload to a decoder.
    """
    if len(data) < 12:
        return False
    if data.lstrip()[:1] == b"<":
        return False
    return data.startswith(_CONTAINER_PREFIXES)


def natural_sort_key(name: str) -> tuple:
    """Sort `2.jpg` before `10.jpg` while keeping the order deterministic."""
    parts = _DIGITS.split(name.lower())
    return tuple(
        (1, int(part)) if part.isdigit() else (0, part)
        for part in parts
        if part != ""
    )


def normalize_member_name(name: str) -> str:
    """Return a safe POSIX-relative member name or raise on traversal."""
    cleaned = name.replace("\\", "/").strip()
    if not cleaned:
        raise ArchiveSafetyError(
            "ARCHIVE_MEMBER_INVALID", "\u538b\u7f29\u5305\u5305\u542b\u7a7a\u6210\u5458\u540d"
        )
    if cleaned.startswith("/") or re.match(r"^[A-Za-z]:", cleaned):
        raise ArchiveSafetyError(
            "ARCHIVE_MEMBER_ABSOLUTE",
            f"\u538b\u7f29\u5305\u6210\u5458\u4f7f\u7528\u7edd\u5bf9\u8def\u5f84: {name}",
        )
    pure = PurePosixPath(cleaned)
    if any(part == ".." for part in pure.parts):
        raise ArchiveSafetyError(
            "ARCHIVE_MEMBER_TRAVERSAL",
            f"\u538b\u7f29\u5305\u6210\u5458\u8bd5\u56fe\u8df3\u51fa\u76ee\u5f55: {name}",
        )
    return str(pure)


def is_image_member(name: str) -> bool:
    return PurePosixPath(name.lower()).suffix in IMAGE_EXTENSIONS


def header_matches_extension(name: str, header: bytes) -> bool:
    """Cross-check an image extension against its magic number.

    Formats without a short fixed signature (WebP, AVIF, JXL) and members with
    no captured header are accepted; the byte check only rejects clear
    mismatches such as an executable renamed to `.jpg`.
    """
    suffix = PurePosixPath(name.lower()).suffix
    if not header:
        return True
    for signature, extensions in _IMAGE_SIGNATURES:
        if suffix in extensions:
            return header.startswith(signature)
        if header.startswith(signature):
            # A known image body under a different image extension is fine,
            # but it must not masquerade as a non-image member.
            continue
    if suffix == ".webp":
        return header.startswith(b"RIFF") or len(header) < 4
    return True


def detected_image_extension(header: bytes) -> str | None:
    """The extension these bytes actually are, or None if they are not an image.

    Positive identification, not a guess: it answers only for containers with a
    fixed signature, and a payload it cannot name gets None rather than a
    plausible default. That is what makes it usable as the 「is this really an
    image?」 half of a relaxation -- an executable renamed to `.jpg` is still
    refused, because nothing here identifies it.

    WebP needs both halves of its header: `RIFF` alone is any RIFF container,
    including audio, so the `WEBP` form at offset 8 is what distinguishes it.
    AVIF, HEIF and JXL are deliberately absent -- their `ftyp` box needs real
    parsing to tell the brands apart, and mislabelling one as another would be a
    worse answer than declining to name it.
    """
    if not header:
        return None
    for signature, extension in _SIGNATURE_EXTENSIONS:
        if header.startswith(signature):
            return extension
    if header.startswith(b"RIFF") and header[8:12] == b"WEBP":
        return ".webp"
    return None


def effective_page_extension(name: str, header: bytes) -> str:
    """The extension a page should be published under.

    The claimed one, unless the bytes say otherwise and can say what they are.
    This is the whole of the 「header does not match the extension」 repair, and it
    is a rename rather than a transcode on purpose: CBZ page names are generated
    by `page_file_names`, never taken from the archive, so the original member
    name is not preserved either way -- and re-encoding PNG bytes into a `.jpg`
    to honour a name nobody will ever see would throw away image quality to
    satisfy a filename the uploader got wrong.
    """
    suffix = PurePosixPath(name.lower()).suffix
    detected = detected_image_extension(header)
    if detected is None:
        # No positive identification means no grounds to rename: an AVIF or JXL
        # page, which nothing here can name, keeps the extension it came with.
        return suffix
    if suffix in _EXTENSION_ALIASES[detected]:
        return suffix
    # Deliberately not routed through `header_matches_extension`, which is a
    # laxer question and answers True in two cases that still need repairing: a
    # member with no extension at all (`001` -- no claim to contradict, but
    # publishing `0001` with no suffix gives readers nothing to dispatch on), and
    # JPEG bytes named `.avif` (an image extension with no fixed signature, so
    # the mismatch is invisible to that check).
    return detected


def member_depth(name: str) -> int:
    return len(PurePosixPath(name).parts)


def validate_manifest(
    manifest: ArchiveManifest, limits: SafetyLimits
) -> tuple[ArchiveMember, ...]:
    """Validate an archive listing before anything is written to disk.

    Returns the publishable image members in natural page order.
    """
    files = manifest.files
    if not files:
        raise ArchiveSafetyError(
            "ARCHIVE_EMPTY", "\u538b\u7f29\u5305\u4e0d\u5305\u542b\u4efb\u4f55\u6587\u4ef6"
        )
    if len(files) > limits.max_members:
        raise ArchiveSafetyError(
            "ARCHIVE_TOO_MANY_MEMBERS",
            f"\u538b\u7f29\u5305\u6210\u5458\u6570 {len(files)} \u8d85\u8fc7\u4e0a\u9650 {limits.max_members}",
        )
    total_size = 0
    pages: list[ArchiveMember] = []
    for member in files:
        name = normalize_member_name(member.name)
        if member.is_symlink:
            raise ArchiveSafetyError(
                "ARCHIVE_MEMBER_SYMLINK",
                f"\u538b\u7f29\u5305\u5305\u542b\u7b26\u53f7\u94fe\u63a5: {member.name}",
            )
        if member_depth(name) > limits.max_depth:
            raise ArchiveSafetyError(
                "ARCHIVE_MEMBER_TOO_DEEP",
                f"\u538b\u7f29\u5305\u76ee\u5f55\u5c42\u7ea7\u8d85\u8fc7\u4e0a\u9650: {member.name}",
            )
        suffix = PurePosixPath(name.lower()).suffix
        if suffix in NESTED_ARCHIVE_EXTENSIONS:
            raise ArchiveSafetyError(
                "ARCHIVE_NESTED_ARCHIVE",
                f"\u538b\u7f29\u5305\u5305\u542b\u5d4c\u5957\u538b\u7f29\u5305: {member.name}",
            )
        if member.size > limits.max_member_bytes:
            raise ArchiveSafetyError(
                "ARCHIVE_MEMBER_TOO_LARGE",
                f"\u6210\u5458 {member.name} \u89e3\u5f00\u540e\u5927\u5c0f\u8d85\u8fc7\u4e0a\u9650",
            )
        total_size += member.size
        if total_size > limits.max_total_bytes:
            raise ArchiveSafetyError(
                "ARCHIVE_TOTAL_TOO_LARGE",
                "\u538b\u7f29\u5305\u89e3\u5f00\u540e\u603b\u5927\u5c0f\u8d85\u8fc7\u4e0a\u9650",
            )
        if (
            member.compressed_size > 0
            and member.size / member.compressed_size > limits.max_compression_ratio
        ):
            raise ArchiveSafetyError(
                "ARCHIVE_COMPRESSION_RATIO",
                f"\u6210\u5458 {member.name} \u538b\u7f29\u7387\u5f02\u5e38\uff0c\u53ef\u80fd\u662f\u538b\u7f29\u70b8\u5f39",
            )
        detected = detected_image_extension(member.header)
        # A member with no extension at all is judged by its bytes. Uploaders do
        # ship books whose pages are named `001` with no suffix, and refusing
        # those produced `ARCHIVE_NO_IMAGES` for an archive that was entirely
        # images -- a whole book lost to a naming habit.
        if is_image_member(name) or (not suffix and detected is not None):
            mislabelled = detected is not None and suffix not in _EXTENSION_ALIASES[
                detected
            ]
            if mislabelled or not header_matches_extension(name, member.header):
                if detected is None:
                    # Still refused: the bytes are not any image this
                    # application can identify, so `page.jpg` holding an
                    # executable fails exactly as it did before. The magic-number
                    # gate is not what was relaxed.
                    raise ArchiveSafetyError(
                        "ARCHIVE_MEMBER_FAKE_IMAGE",
                        f"\u6210\u5458 {member.name} \u7684\u6587\u4ef6\u5934\u4e0e\u6269\u5c55\u540d\u4e0d\u7b26"
                        "\uff0c\u4e5f\u4e0d\u662f\u53ef\u8bc6\u522b\u7684\u56fe\u7247\u683c\u5f0f",
                    )
                # A real image under the wrong name. This used to fail the entire
                # archive: one page called `.png` while holding JPEG bytes -- a
                # mistake an uploader makes with a batch converter and never
                # notices -- meant none of the other two hundred pages were
                # published either. The page is kept and `page_file_names` gives
                # it the extension its bytes actually are.
                LOGGER.info(
                    "archive_member_extension_repaired member=%s detected=%s",
                    member.name,
                    detected,
                )
            pages.append(member)
    if not pages:
        raise ArchiveSafetyError(
            "ARCHIVE_NO_IMAGES",
            "\u538b\u7f29\u5305\u4e0d\u5305\u542b\u53ef\u53d1\u5e03\u7684\u56fe\u7247\u9875",
        )
    pages.sort(key=lambda member: natural_sort_key(normalize_member_name(member.name)))
    return tuple(pages)


def page_file_names(members: tuple[ArchiveMember, ...]) -> tuple[str, ...]:
    """Return stable, collision-free CBZ page names in the given order.

    The extension follows the bytes wherever they disagree with the member's own
    name, so a published page is never labelled as a format it is not. Readers
    dispatch on the extension, so a JPEG published as `0007.png` is a page that
    silently fails to display in some of them.
    """
    used: set[str] = set()
    names: list[str] = []
    for index, member in enumerate(members, start=1):
        suffix = effective_page_extension(
            normalize_member_name(member.name), member.header
        )
        name = f"{index:04d}{suffix}"
        attempt = 1
        while name.lower() in used:
            attempt += 1
            name = f"{index:04d}-{attempt}{suffix}"
        used.add(name.lower())
        names.append(name)
    return tuple(names)


__all__ = [
    "ALLOWED_SIDECAR_NAMES",
    "IMAGE_EXTENSIONS",
    "NESTED_ARCHIVE_EXTENSIONS",
    "detected_image_extension",
    "effective_page_extension",
    "header_matches_extension",
    "is_image_member",
    "looks_like_image",
    "member_depth",
    "natural_sort_key",
    "normalize_member_name",
    "page_file_names",
    "validate_manifest",
]