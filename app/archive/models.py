from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


FORMAT_ZIP = "zip"
FORMAT_RAR = "rar"
FORMAT_SEVEN_ZIP = "7z"
FORMAT_UNKNOWN = "unknown"

SUPPORTED_FORMATS: tuple[str, ...] = (FORMAT_ZIP, FORMAT_RAR, FORMAT_SEVEN_ZIP)

BACKEND_ZIPFILE = "zipfile"
BACKEND_SEVEN_ZIP = "seven_zip"

PROFILE_KIND_BUILTIN = "BUILTIN"
PROFILE_KIND_CLI = "CLI"
PROFILE_KIND_BRIDGE = "BRIDGE"

PROFILE_KINDS: tuple[str, ...] = (
    PROFILE_KIND_BUILTIN,
    PROFILE_KIND_CLI,
    PROFILE_KIND_BRIDGE,
)


@dataclass(frozen=True, slots=True)
class ArchiveMember:
    """One entry of an archive listing, before any extraction happens."""

    name: str
    size: int = 0
    compressed_size: int = 0
    is_dir: bool = False
    is_symlink: bool = False
    encrypted: bool = False
    header: bytes = b""


@dataclass(frozen=True, slots=True)
class ArchiveManifest:
    source_format: str
    members: tuple[ArchiveMember, ...] = ()
    volumes: tuple[Path, ...] = ()
    encrypted: bool = False

    @property
    def files(self) -> tuple[ArchiveMember, ...]:
        return tuple(member for member in self.members if not member.is_dir)

    @property
    def member_count(self) -> int:
        return len(self.files)

    @property
    def total_size(self) -> int:
        return sum(member.size for member in self.files)

    @property
    def compressed_size(self) -> int:
        return sum(member.compressed_size for member in self.files)


@dataclass(frozen=True, slots=True)
class SafetyLimits:
    """Pre-extraction limits, enforced identically for every backend."""

    max_members: int = 5000
    max_total_bytes: int = 4 * 1024 * 1024 * 1024
    max_member_bytes: int = 512 * 1024 * 1024
    max_compression_ratio: float = 200.0
    max_depth: int = 8

    @classmethod
    def from_mapping(cls, values: dict[str, str]) -> SafetyLimits:
        defaults = cls()
        return cls(
            max_members=int(
                values.get("max_members", defaults.max_members)
            ),
            max_total_bytes=int(
                values.get("max_total_bytes", defaults.max_total_bytes)
            ),
            max_member_bytes=int(
                values.get("max_member_bytes", defaults.max_member_bytes)
            ),
            max_compression_ratio=float(
                values.get(
                    "max_compression_ratio", defaults.max_compression_ratio
                )
            ),
            max_depth=int(values.get("max_depth", defaults.max_depth)),
        )


@dataclass(frozen=True, slots=True)
class ToolProfile:
    """A registered tool configuration; operators never submit raw commands."""

    profile_id: int
    name: str
    backend: str
    kind: str
    executable_path: str | None
    supported_formats: tuple[str, ...]
    timeout_seconds: int
    capabilities: tuple[str, ...]
    enabled: bool

    def supports(self, source_format: str) -> bool:
        return source_format in self.supported_formats

    def has_capability(self, capability: str) -> bool:
        return capability in self.capabilities


@dataclass(frozen=True, slots=True)
class ArchivePasswordEntry:
    """Password vault metadata; the plaintext never leaves the vault."""

    password_id: int
    name: str
    priority: int
    enabled: bool
    last_success_at: str | None
    created_at: str


@dataclass(frozen=True, slots=True)
class ArchiveTaskSnapshot:
    """Deterministic retry snapshot persisted on the conversion task."""

    backend: str
    tool_profile: str
    source_format: str
    library_path: str
    work_path: str

    def as_dict(self) -> dict[str, str]:
        return {
            "backend": self.backend,
            "tool_profile": self.tool_profile,
            "source_format": self.source_format,
            "library_path": self.library_path,
            "work_path": self.work_path,
        }


@dataclass(frozen=True, slots=True)
class ArchiveProcessResult:
    cbz_path: Path
    page_count: int
    skipped_members: tuple[str, ...]
    snapshot: ArchiveTaskSnapshot
    password_id: int | None
    volume_count: int


__all__ = [
    "ArchiveManifest",
    "ArchiveMember",
    "ArchivePasswordEntry",
    "ArchiveProcessResult",
    "ArchiveTaskSnapshot",
    "BACKEND_SEVEN_ZIP",
    "BACKEND_ZIPFILE",
    "FORMAT_RAR",
    "FORMAT_SEVEN_ZIP",
    "FORMAT_UNKNOWN",
    "FORMAT_ZIP",
    "PROFILE_KINDS",
    "PROFILE_KIND_BRIDGE",
    "PROFILE_KIND_BUILTIN",
    "PROFILE_KIND_CLI",
    "SUPPORTED_FORMATS",
    "SafetyLimits",
    "ToolProfile",
]