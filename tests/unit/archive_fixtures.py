"""Shared archive fixtures: real ZIP bytes and registered tool profiles."""

from __future__ import annotations

from pathlib import Path
import zipfile

from app.archive.models import (
    BACKEND_SEVEN_ZIP,
    BACKEND_ZIPFILE,
    PROFILE_KIND_BUILTIN,
    PROFILE_KIND_CLI,
    ToolProfile,
)


JPEG_HEADER = b"\xff\xd8\xff\xe0"
PNG_HEADER = b"\x89PNG\r\n\x1a\n"


ZIPFILE_PROFILE = ToolProfile(
    profile_id=1,
    name="zipfile-default",
    backend=BACKEND_ZIPFILE,
    kind=PROFILE_KIND_BUILTIN,
    executable_path=None,
    supported_formats=("zip",),
    timeout_seconds=600,
    capabilities=("stream", "zip_password"),
    enabled=True,
)

SEVEN_ZIP_PROFILE = ToolProfile(
    profile_id=2,
    name="7zz-default",
    backend=BACKEND_SEVEN_ZIP,
    kind=PROFILE_KIND_CLI,
    executable_path="7zz",
    supported_formats=("rar", "7z", "zip"),
    timeout_seconds=900,
    capabilities=("password", "volumes"),
    enabled=True,
)

ZIP_ONLY_PROFILES: tuple[ToolProfile, ...] = (ZIPFILE_PROFILE,)
ALL_PROFILES: tuple[ToolProfile, ...] = (ZIPFILE_PROFILE, SEVEN_ZIP_PROFILE)


def image_bytes(name: str, size: int = 64) -> bytes:
    header = PNG_HEADER if name.lower().endswith(".png") else JPEG_HEADER
    return header + b"\x00" * max(size - len(header), 0)


def write_image_zip(
    path: Path,
    names: tuple[str, ...],
    *,
    password: str | None = None,
    size: int = 64,
) -> None:
    """Write a ZIP whose image members carry real magic numbers."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        for name in names:
            if name.lower() == "comicinfo.xml":
                archive.writestr(name, b"<ComicInfo />")
                continue
            archive.writestr(name, image_bytes(name, size))
    if password:
        raise NotImplementedError(
            "zipfile cannot write encrypted archives; use a recorded fixture"
        )


__all__ = [
    "ALL_PROFILES",
    "JPEG_HEADER",
    "PNG_HEADER",
    "SEVEN_ZIP_PROFILE",
    "ZIPFILE_PROFILE",
    "ZIP_ONLY_PROFILES",
    "image_bytes",
    "write_image_zip",
]