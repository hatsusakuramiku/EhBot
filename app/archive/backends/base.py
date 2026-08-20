from __future__ import annotations

from pathlib import Path
from typing import Protocol

from app.archive.models import ArchiveManifest, ArchiveMember, ToolProfile


class ArchiveBackend(Protocol):
    """Format-specific archive operations selected through a tool profile."""

    name: str
    #: A streaming backend can copy members straight into the CBZ without
    #: writing the whole archive to the working directory first.
    streaming: bool

    def inspect(
        self, volumes: tuple[Path, ...], password: str | None
    ) -> ArchiveManifest: ...

    def test_password(
        self, volumes: tuple[Path, ...], password: str | None
    ) -> None: ...

    def extract(
        self,
        volumes: tuple[Path, ...],
        destination: Path,
        password: str | None,
        members: tuple[ArchiveMember, ...],
    ) -> dict[str, Path]: ...

    def pack_cbz(
        self,
        pages: tuple[tuple[str, Path], ...],
        destination: Path,
        comicinfo: bytes,
    ) -> int: ...


def profile_supports(profile: ToolProfile, source_format: str) -> bool:
    return profile.enabled and profile.supports(source_format)


__all__ = ["ArchiveBackend", "profile_supports"]