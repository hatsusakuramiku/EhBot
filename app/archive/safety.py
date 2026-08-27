from __future__ import annotations

import re
from pathlib import PurePosixPath

from app.archive.errors import ArchiveSafetyError
from app.archive.models import ArchiveManifest, ArchiveMember, SafetyLimits


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
        if is_image_member(name):
            if not header_matches_extension(name, member.header):
                raise ArchiveSafetyError(
                    "ARCHIVE_MEMBER_FAKE_IMAGE",
                    f"\u6210\u5458 {member.name} \u7684\u6587\u4ef6\u5934\u4e0e\u6269\u5c55\u540d\u4e0d\u7b26",
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
    """Return stable, collision-free CBZ page names in the given order."""
    used: set[str] = set()
    names: list[str] = []
    for index, member in enumerate(members, start=1):
        suffix = PurePosixPath(normalize_member_name(member.name).lower()).suffix
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
    "header_matches_extension",
    "is_image_member",
    "looks_like_image",
    "member_depth",
    "natural_sort_key",
    "normalize_member_name",
    "page_file_names",
    "validate_manifest",
]