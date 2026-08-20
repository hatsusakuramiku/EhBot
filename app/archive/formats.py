from __future__ import annotations

import re
from pathlib import Path

from app.archive.models import (
    FORMAT_RAR,
    FORMAT_SEVEN_ZIP,
    FORMAT_UNKNOWN,
    FORMAT_ZIP,
)


ZIP_SIGNATURES: tuple[bytes, ...] = (b"PK\x03\x04", b"PK\x05\x06", b"PK\x07\x08")
RAR_SIGNATURES: tuple[bytes, ...] = (
    b"Rar!\x1a\x07\x00",
    b"Rar!\x1a\x07\x01\x00",
)
SEVEN_ZIP_SIGNATURE = b"7z\xbc\xaf\x27\x1c"

_EXTENSION_FORMATS: dict[str, str] = {
    ".zip": FORMAT_ZIP,
    ".cbz": FORMAT_ZIP,
    ".rar": FORMAT_RAR,
    ".cbr": FORMAT_RAR,
    ".7z": FORMAT_SEVEN_ZIP,
    ".cb7": FORMAT_SEVEN_ZIP,
}

# `name.part1.rar`, `name.part01.rar`
_RAR_PART_PATTERN = re.compile(r"^(?P<stem>.+)\.part(?P<index>\d+)\.rar$", re.IGNORECASE)
# `name.r00` ... `name.r99` companions of a leading `name.rar`
_RAR_LEGACY_PATTERN = re.compile(r"^(?P<stem>.+)\.r(?P<index>\d{2,3})$", re.IGNORECASE)
# `name.7z.001`, `name.zip.001`
_NUMBERED_PATTERN = re.compile(
    r"^(?P<stem>.+)\.(?P<container>zip|7z|rar)\.(?P<index>\d{3})$", re.IGNORECASE
)


def format_from_extension(path: Path) -> str:
    numbered = _NUMBERED_PATTERN.match(path.name)
    if numbered:
        return _EXTENSION_FORMATS.get(
            f".{numbered.group('container').lower()}", FORMAT_UNKNOWN
        )
    if _RAR_PART_PATTERN.match(path.name) or _RAR_LEGACY_PATTERN.match(path.name):
        return FORMAT_RAR
    return _EXTENSION_FORMATS.get(path.suffix.lower(), FORMAT_UNKNOWN)


def format_from_signature(path: Path) -> str:
    try:
        with path.open("rb") as handle:
            head = handle.read(8)
    except OSError:
        return FORMAT_UNKNOWN
    if head.startswith(SEVEN_ZIP_SIGNATURE):
        return FORMAT_SEVEN_ZIP
    if any(head.startswith(signature) for signature in RAR_SIGNATURES):
        return FORMAT_RAR
    if any(head.startswith(signature) for signature in ZIP_SIGNATURES):
        return FORMAT_ZIP
    return FORMAT_UNKNOWN


def detect_source_format(path: Path) -> str:
    """Detect the archive format from the magic number, then the extension.

    Later volumes of a split archive carry no signature of their own, so the
    file name decides in that case.
    """
    signature_format = format_from_signature(path)
    if signature_format != FORMAT_UNKNOWN:
        return signature_format
    return format_from_extension(path)


def _volume_index(path: Path) -> int | None:
    numbered = _NUMBERED_PATTERN.match(path.name)
    if numbered:
        return int(numbered.group("index"))
    rar_part = _RAR_PART_PATTERN.match(path.name)
    if rar_part:
        return int(rar_part.group("index"))
    legacy = _RAR_LEGACY_PATTERN.match(path.name)
    if legacy:
        # `.r00` immediately follows the leading `.rar` volume.
        return int(legacy.group("index")) + 2
    if path.suffix.lower() in {".rar", ".cbr"}:
        return 1
    return None


def volume_group(path: Path) -> str | None:
    """Return a stable group key when the path looks like a split volume."""
    numbered = _NUMBERED_PATTERN.match(path.name)
    if numbered:
        return f"{numbered.group('stem')}.{numbered.group('container').lower()}"
    rar_part = _RAR_PART_PATTERN.match(path.name)
    if rar_part:
        return f"{rar_part.group('stem')}.rar"
    legacy = _RAR_LEGACY_PATTERN.match(path.name)
    if legacy:
        return f"{legacy.group('stem')}.rar"
    return None


def _sibling_volumes(path: Path, group: str) -> tuple[Path, ...]:
    candidates: list[tuple[int, Path]] = []
    for sibling in path.parent.iterdir():
        if not sibling.is_file():
            continue
        sibling_group = volume_group(sibling)
        if sibling_group is None:
            if sibling.name == group:
                # The leading `name.rar` of a `.r00` series.
                candidates.append((1, sibling))
            continue
        if sibling_group != group:
            continue
        index = _volume_index(sibling)
        if index is None:
            continue
        candidates.append((index, sibling))
    candidates.sort(key=lambda item: (item[0], item[1].name))
    return tuple(item[1] for item in candidates)


def resolve_volumes(path: Path) -> tuple[tuple[Path, ...], tuple[str, ...]]:
    """Return the ordered volume list and the names of any missing volumes.

    A single-file archive returns just itself and no gaps. Split archives are
    always driven from their first volume, and the caller must treat a
    non-empty gap list as an unrecoverable-for-now state.
    """
    group = volume_group(path)
    if group is None:
        return (path,), ()
    volumes = _sibling_volumes(path, group)
    if not volumes:
        return (path,), ()
    indexes = [index for index in (_volume_index(item) for item in volumes) if index]
    missing: list[str] = []
    if indexes:
        expected = set(range(1, max(indexes) + 1))
        for index in sorted(expected - set(indexes)):
            missing.append(_expected_volume_name(group, index))
    return volumes, tuple(missing)


def _expected_volume_name(group: str, index: int) -> str:
    stem, _, container = group.rpartition(".")
    if container == "rar":
        if index == 1:
            return group
        return f"{stem}.part{index}.rar"
    return f"{group}.{index:03d}"


__all__ = [
    "RAR_SIGNATURES",
    "SEVEN_ZIP_SIGNATURE",
    "ZIP_SIGNATURES",
    "detect_source_format",
    "format_from_extension",
    "format_from_signature",
    "resolve_volumes",
    "volume_group",
]