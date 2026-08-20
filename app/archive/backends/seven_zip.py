from __future__ import annotations

import logging
import os
import shutil
import subprocess
from pathlib import Path

from app.archive.errors import (
    ArchiveError,
    ArchivePasswordRequired,
    ArchiveToolUnavailable,
)
from app.archive.formats import detect_source_format
from app.archive.models import (
    BACKEND_SEVEN_ZIP,
    ArchiveManifest,
    ArchiveMember,
    ToolProfile,
)
from app.archive.safety import normalize_member_name
from app.archive.toolchain import installed_executable


HEADER_SIZE = 16

# Observed against 7-Zip 26.00: a wrong member password reports
# "Data Error in encrypted file. Wrong password?", and a header-encrypted
# archive reports "Cannot open encrypted archive. Wrong password?".
_PASSWORD_MARKERS: tuple[str, ...] = (
    "wrong password",
    "cannot open encrypted archive",
    "password is incorrect",
)

# Upstream ships `7zzs`/`7zz` on Linux and macOS. `7z`/`7za` only appear
# on Windows development hosts, where the operator installs 7-Zip.
_CANDIDATE_EXECUTABLES: tuple[str, ...] = ("7zzs", "7zz", "7z", "7za")

# Default Windows install locations, probed only on Windows development
# hosts when the configured name is not resolvable through PATH.
_WINDOWS_FALLBACK_DIRECTORIES: tuple[str, ...] = (
    r"C:\Program Files\7-Zip",
    r"C:\Program Files (x86)\7-Zip",
)

_ATTRIBUTE_DIRECTORY = "D"
_ATTRIBUTE_SYMLINK = "l"


class SevenZipBackend:
    """Controlled `7zz` subprocess backend for RAR, 7Z, and split archives.

    The main process never loads a third-party DLL: it only spawns the
    registered executable with a fixed argument list and parses the
    structured `-slt` listing.
    """

    name = BACKEND_SEVEN_ZIP
    streaming = False

    def __init__(
        self,
        profile: ToolProfile,
        runner=None,
        tools_path: Path | None = None,
    ) -> None:
        self._profile = profile
        self._runner = runner or self._run_subprocess
        self._tools_path = tools_path

    @property
    def profile(self) -> ToolProfile:
        return self._profile

    def _executable(self) -> str:
        configured = (self._profile.executable_path or "").strip()
        if not configured:
            raise ArchiveToolUnavailable(
                f"\u5de5\u5177 profile {self._profile.name} \u672a\u914d\u7f6e\u53ef\u6267\u884c\u6587\u4ef6"
            )
        if Path(configured).is_absolute():
            if not Path(configured).is_file():
                raise ArchiveToolUnavailable(
                    f"\u5de5\u5177 profile {self._profile.name} \u7684\u53ef\u6267\u884c\u6587\u4ef6\u4e0d\u5b58\u5728"
                )
            return configured
        resolved = resolve_seven_zip_executable(configured, self._tools_path)
        if resolved is None:
            raise ArchiveToolUnavailable(
                f"\u672a\u627e\u5230\u53ef\u7528\u7684 7-Zip \u53ef\u6267\u884c\u6587\u4ef6\uff08\u5df2\u5c1d\u8bd5 {configured}\uff09"
            )
        return resolved

    def _run_subprocess(
        self, arguments: tuple[str, ...], working_directory: Path | None = None
    ) -> tuple[int, str]:
        executable = self._executable()
        try:
            completed = subprocess.run(  # noqa: S603 - fixed argv, no shell
                [executable, *arguments],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=self._profile.timeout_seconds,
                check=False,
                shell=False,
                cwd=str(working_directory) if working_directory else None,
            )
        except subprocess.TimeoutExpired as exc:
            raise ArchiveError(
                "ARCHIVE_TOOL_TIMEOUT",
                f"\u5f52\u6863\u5de5\u5177\u8d85\u65f6\uff08{self._profile.timeout_seconds}s\uff09",
            ) from exc
        except OSError as exc:
            raise ArchiveToolUnavailable(
                f"\u65e0\u6cd5\u542f\u52a8\u5f52\u6863\u5de5\u5177: {exc}"
            ) from exc
        return completed.returncode, f"{completed.stdout}\n{completed.stderr}"

    def _invoke(
        self, arguments: tuple[str, ...], working_directory: Path | None = None
    ) -> str:
        code, output = (
            self._runner(arguments, working_directory)
            if working_directory is not None
            else self._runner(arguments)
        )
        if code == 0:
            return output
        lowered = output.lower()
        if any(marker in lowered for marker in _PASSWORD_MARKERS):
            raise ArchivePasswordRequired()
        logging.getLogger(__name__).warning(
            "archive_tool_failed",
            extra={"error_code": "ARCHIVE_TOOL_FAILED"},
        )
        raise ArchiveError(
            "ARCHIVE_TOOL_FAILED",
            f"\u5f52\u6863\u5de5\u5177\u9000\u51fa\u7801 {code}",
        )

    @staticmethod
    def _password_argument(password: str | None) -> str:
        # `-p` with an empty value stops 7zz from prompting interactively.
        return f"-p{password}" if password else "-p"

    def inspect(
        self, volumes: tuple[Path, ...], password: str | None
    ) -> ArchiveManifest:
        output = self._invoke(
            (
                "l",
                "-slt",
                "-ba",
                "-sccUTF-8",
                self._password_argument(password),
                str(volumes[0]),
            )
        )
        members = tuple(parse_slt_listing(output))
        return ArchiveManifest(
            source_format=self._detected_format(volumes[0]),
            members=members,
            volumes=volumes,
            encrypted=any(member.encrypted for member in members),
        )

    @staticmethod
    def _detected_format(source: Path) -> str:
        return detect_source_format(source)

    def test_password(
        self, volumes: tuple[Path, ...], password: str | None
    ) -> None:
        self._invoke(
            (
                "t",
                "-ba",
                "-sccUTF-8",
                self._password_argument(password),
                str(volumes[0]),
            )
        )

    def extract(
        self,
        volumes: tuple[Path, ...],
        destination: Path,
        password: str | None,
        members: tuple[ArchiveMember, ...],
    ) -> dict[str, Path]:
        destination.mkdir(parents=True, exist_ok=True)
        self._invoke(
            (
                "x",
                "-ba",
                "-y",
                "-sccUTF-8",
                self._password_argument(password),
                f"-o{destination}",
                str(volumes[0]),
            )
        )
        resolved_root = destination.resolve()
        extracted: dict[str, Path] = {}
        for member in members:
            safe_name = normalize_member_name(member.name)
            target = (destination / safe_name).resolve()
            if not target.is_relative_to(resolved_root):
                raise ArchiveError(
                    "ARCHIVE_MEMBER_TRAVERSAL",
                    f"\u6210\u5458 {member.name} \u8df3\u51fa\u4e86\u5de5\u4f5c\u76ee\u5f55",
                )
            if target.is_symlink():
                raise ArchiveError(
                    "ARCHIVE_MEMBER_SYMLINK",
                    f"\u6210\u5458 {member.name} \u89e3\u538b\u540e\u662f\u7b26\u53f7\u94fe\u63a5",
                )
            if not target.is_file():
                raise ArchiveError(
                    "ARCHIVE_MEMBER_MISSING",
                    f"\u89e3\u538b\u540e\u7f3a\u5c11\u6210\u5458 {member.name}",
                )
            extracted[member.name] = target
        return extracted

    def pack_cbz(
        self,
        pages: tuple[tuple[str, Path], ...],
        destination: Path,
        comicinfo: bytes,
    ) -> int:
        """Pack the CBZ with the same tool that extracted the archive."""
        if not pages:
            raise ArchiveError(
                "ARCHIVE_NO_IMAGES",
                "\u6ca1\u6709\u53ef\u6253\u5305\u7684\u56fe\u7247\u9875",
            )
        staging = destination.parent / f"{destination.stem}.pages"
        staging.mkdir(parents=True, exist_ok=True)
        comicinfo_path = staging / "ComicInfo.xml"
        comicinfo_path.write_bytes(comicinfo)
        for page_name, path in pages:
            target = staging / page_name
            if target.exists():
                target.unlink()
            if _can_hardlink(path, target):
                target.hardlink_to(path)
            else:
                shutil.copy2(path, target)
        try:
            # `-spf2` would keep the staging prefix, so the archive is built
            # with the staging directory as the working directory instead and
            # every page is stored under its flat CBZ name.
            self._invoke(
                (
                    "a",
                    "-tzip",
                    "-mx0",
                    "-ba",
                    "-y",
                    "-sccUTF-8",
                    str(destination.resolve()),
                    "*",
                ),
                working_directory=staging,
            )
        finally:
            shutil.rmtree(staging, ignore_errors=True)
        return len(pages)


def resolve_seven_zip_executable(
    configured: str, tools_path: Path | None = None
) -> str | None:
    """Resolve a 7-Zip executable name to an absolute path.

    Resolution order is the managed install under `tools_path` first, then the
    configured name on `PATH`, then the other known 7-Zip command names, then
    the default Windows install directories. Only fixed, known names are
    probed; no value from an archive or a remote source is ever used.
    """
    if tools_path is not None:
        managed = installed_executable(tools_path)
        if managed is not None:
            return str(managed)
    names: list[str] = [configured]
    names.extend(name for name in _CANDIDATE_EXECUTABLES if name != configured)
    for name in names:
        resolved = shutil.which(name)
        if resolved:
            return resolved
    if os.name == "nt":
        for directory in _WINDOWS_FALLBACK_DIRECTORIES:
            for name in names:
                candidate = Path(directory) / f"{Path(name).stem}.exe"
                if candidate.is_file():
                    return str(candidate)
    return None


def _can_hardlink(source: Path, target: Path) -> bool:
    try:
        return source.stat().st_dev == target.parent.stat().st_dev
    except OSError:
        return False


def parse_slt_listing(output: str) -> list[ArchiveMember]:
    """Parse `7zz l -slt` output into archive members."""
    members: list[ArchiveMember] = []
    current: dict[str, str] = {}

    def flush() -> None:
        name = current.get("Path")
        if not name:
            return
        attributes = current.get("Attributes", "")
        members.append(
            ArchiveMember(
                name=name,
                size=_as_int(current.get("Size")),
                compressed_size=_as_int(current.get("Packed Size")),
                is_dir=_ATTRIBUTE_DIRECTORY in attributes.split(" ")[0],
                is_symlink=_ATTRIBUTE_SYMLINK in attributes,
                encrypted=current.get("Encrypted", "").strip() == "+",
            )
        )

    for raw_line in output.splitlines():
        line = raw_line.rstrip()
        if not line.strip():
            flush()
            current = {}
            continue
        key, separator, value = line.partition(" = ")
        if not separator:
            continue
        current[key.strip()] = value.strip()
    flush()
    return members


def _as_int(value: str | None) -> int:
    if not value:
        return 0
    try:
        return int(value)
    except ValueError:
        return 0


__all__ = [
    "SevenZipBackend",
    "parse_slt_listing",
    "resolve_seven_zip_executable",
]