"""Fetch a portable official 7-Zip toolchain from the upstream release.

Linux and macOS publish `.tar.xz` archives that Python can unpack directly.
Windows publishes an installer containing the portable `7z.exe` and `7z.dll`;
the official single-file `7zr.exe` extracts it into the managed tools directory
without running the installer or touching the host installation.

Every asset is pinned by version and SHA-256. A downloaded file that does not
match its digest is discarded, so a compromised mirror or a truncated transfer
cannot install an executable.
"""

from __future__ import annotations

import hashlib
import io
import os
import platform
import shutil
import stat
import subprocess
import tarfile
from dataclasses import dataclass
from pathlib import Path


SEVEN_ZIP_VERSION = "26.02"

RELEASE_BASE_URL = "https://github.com/ip7z/7zip/releases/download"

#: Statically linked binary first: it runs on slim images that ship no
#: `libstdc++`. The dynamically linked `7zz` is the fallback.
PREFERRED_BINARIES: tuple[str, ...] = ("7zzs", "7zz")
WINDOWS_EXECUTABLE = "7z.exe"
WINDOWS_LIBRARY = "7z.dll"

DOWNLOAD_TIMEOUT_SECONDS = 120


class ToolchainError(RuntimeError):
    """The 7-Zip toolchain could not be installed."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.public_message = message


@dataclass(frozen=True, slots=True)
class ReleaseAsset:
    file_name: str
    sha256: str

    @property
    def url(self) -> str:
        return f"{RELEASE_BASE_URL}/{SEVEN_ZIP_VERSION}/{self.file_name}"


def _asset(suffix: str, sha256: str) -> ReleaseAsset:
    compact_version = SEVEN_ZIP_VERSION.replace(".", "")
    return ReleaseAsset(
        file_name=f"7z{compact_version}-{suffix}.tar.xz", sha256=sha256
    )


def _windows_asset(suffix: str, sha256: str) -> ReleaseAsset:
    compact_version = SEVEN_ZIP_VERSION.replace(".", "")
    separator = "-" if suffix else ""
    return ReleaseAsset(
        file_name=f"7z{compact_version}{separator}{suffix}.exe", sha256=sha256
    )


WINDOWS_BOOTSTRAP_ASSET = ReleaseAsset(
    file_name="7zr.exe",
    sha256="56b8cc9f4971cef253644fafe54063ed7fdca551d4dee0f8c6baa81b855acd72",
)


#: Pinned upstream assets for 7-Zip 26.02, keyed by `(system, machine)`.
RELEASE_ASSETS: dict[tuple[str, str], ReleaseAsset] = {
    ("linux", "x64"): _asset(
        "linux-x64",
        "41aaba7b1235304ab5aa0624530c67ae829496cd29e875925271efdccc28c03e",
    ),
    ("linux", "arm64"): _asset(
        "linux-arm64",
        "70ea6cc737ae1495ea2d7eb20ef3120fe579bd3f1a83a9d2362b62ec5bde2bba",
    ),
    ("linux", "arm"): _asset(
        "linux-arm",
        "81b7f04b3528852fac10f5becf9f15870a5da4cb94fbcb8a138197eb937468bf",
    ),
    ("linux", "x86"): _asset(
        "linux-x86",
        "ae0148515c4b708440b57960931234eb02b11a856479668044a6126adf4b1181",
    ),
    ("darwin", "x64"): _asset(
        "mac",
        "1cf6760579502f87e591ff5c73a005ec50b3e4d6f507e8b038382d563c3175b9",
    ),
    ("darwin", "arm64"): _asset(
        "mac",
        "1cf6760579502f87e591ff5c73a005ec50b3e4d6f507e8b038382d563c3175b9",
    ),
    ("windows", "x64"): _windows_asset(
        "x64",
        "6745fa76dc2ea031596d8678f6f6b99c3c1b435b4164a63485adbbc7b8d82ef0",
    ),
    ("windows", "x86"): _windows_asset(
        "",
        "17d894c17b04984b6ffcc1b31926b39c42d315cd861c3adbf7f34bd941d529ac",
    ),
    ("windows", "arm64"): _windows_asset(
        "arm64",
        "7c6fde79ed5e11b81c7bb6573b7962d3b6322aa5fce69c33ed19f672b55173ab",
    ),
}

_MACHINE_ALIASES: dict[str, str] = {
    "x86_64": "x64",
    "amd64": "x64",
    "x64": "x64",
    "aarch64": "arm64",
    "arm64": "arm64",
    "armv8l": "arm64",
    "armv7l": "arm",
    "armv6l": "arm",
    "arm": "arm",
    "i386": "x86",
    "i486": "x86",
    "i586": "x86",
    "i686": "x86",
    "x86": "x86",
}


def normalize_machine(machine: str) -> str:
    return _MACHINE_ALIASES.get(machine.strip().lower(), machine.strip().lower())


def asset_for_platform(
    system: str | None = None, machine: str | None = None
) -> ReleaseAsset:
    """Return the pinned upstream asset for the running platform."""
    resolved_system = (system or platform.system()).strip().lower()
    resolved_machine = normalize_machine(machine or platform.machine())
    asset = RELEASE_ASSETS.get((resolved_system, resolved_machine))
    if asset is None:
        raise ToolchainError(
            "TOOLCHAIN_PLATFORM_UNSUPPORTED",
            f"\u5b98\u65b9 7-Zip \u672a\u53d1\u5e03 {resolved_system}/{resolved_machine} "
            "\u7684\u53ef\u7528\u4e8c\u8fdb\u5236",
        )
    return asset


def install_root(tools_path: Path) -> Path:
    """Version the install directory so an upgrade never overwrites in place."""
    return Path(tools_path) / "7zip" / SEVEN_ZIP_VERSION


def installed_executable(
    tools_path: Path, *, system: str | None = None
) -> Path | None:
    root = install_root(tools_path)
    resolved_system = (system or platform.system()).strip().lower()
    names = (
        (WINDOWS_EXECUTABLE,)
        if resolved_system == "windows"
        else PREFERRED_BINARIES
    )
    for name in names:
        candidate = root / name
        has_runtime = (
            resolved_system != "windows" or (root / WINDOWS_LIBRARY).is_file()
        )
        if candidate.is_file() and has_runtime and os.access(candidate, os.X_OK):
            return candidate
    return None


def verify_digest(payload: bytes, expected_sha256: str) -> str:
    digest = hashlib.sha256(payload).hexdigest()
    if digest != expected_sha256:
        raise ToolchainError(
            "TOOLCHAIN_DIGEST_MISMATCH",
            "\u4e0b\u8f7d\u7684 7-Zip \u5b58\u6863\u6821\u9a8c\u5931\u8d25\uff0c\u5df2\u4e22\u5f03",
        )
    return digest


def _download(url: str) -> bytes:
    import httpx

    try:
        response = httpx.get(
            url, timeout=DOWNLOAD_TIMEOUT_SECONDS, follow_redirects=True
        )
        response.raise_for_status()
    except httpx.HTTPError as exc:
        raise ToolchainError(
            "TOOLCHAIN_DOWNLOAD_FAILED",
            f"\u65e0\u6cd5\u4e0b\u8f7d\u5b98\u65b9 7-Zip \u4e8c\u8fdb\u5236: {exc}",
        ) from exc
    return response.content


def extract_binaries(payload: bytes, destination: Path) -> tuple[Path, ...]:
    """Extract only the known 7-Zip executables from the release archive.

    Member names are matched against a fixed allowlist rather than being
    joined onto the destination, so a crafted archive cannot write outside it.
    """
    destination.mkdir(parents=True, exist_ok=True)
    extracted: list[Path] = []
    try:
        with tarfile.open(fileobj=io.BytesIO(payload), mode="r:xz") as archive:
            for name in PREFERRED_BINARIES:
                try:
                    member = archive.getmember(name)
                except KeyError:
                    continue
                if not member.isfile():
                    continue
                reader = archive.extractfile(member)
                if reader is None:
                    continue
                target = destination / name
                with reader, target.open("wb") as writer:
                    shutil.copyfileobj(reader, writer, length=1024 * 1024)
                target.chmod(
                    target.stat().st_mode
                    | stat.S_IXUSR
                    | stat.S_IXGRP
                    | stat.S_IXOTH
                )
                extracted.append(target)
    except (tarfile.TarError, OSError) as exc:
        raise ToolchainError(
            "TOOLCHAIN_EXTRACT_FAILED",
            f"\u65e0\u6cd5\u89e3\u5f00\u5b98\u65b9 7-Zip \u5b58\u6863: {exc}",
        ) from exc
    if not extracted:
        raise ToolchainError(
            "TOOLCHAIN_BINARY_MISSING",
            "\u5b98\u65b9 7-Zip \u5b58\u6863\u4e2d\u6ca1\u6709\u627e\u5230\u53ef\u7528\u7684\u53ef\u6267\u884c\u6587\u4ef6",
        )
    return tuple(extracted)


def extract_windows_binaries(
    payload: bytes,
    bootstrap_payload: bytes,
    destination: Path,
    *,
    runner=None,
) -> tuple[Path, Path]:
    """Extract the portable Windows executable and DLL without installation."""
    work = destination / ".extract"
    unpacked = work / "unpacked"
    bootstrap = work / WINDOWS_BOOTSTRAP_ASSET.file_name
    package = work / "package.exe"
    try:
        unpacked.mkdir(parents=True, exist_ok=True)
        bootstrap.write_bytes(bootstrap_payload)
        package.write_bytes(payload)
        command = [
            str(bootstrap),
            "x",
            str(package),
            f"-o{unpacked}",
            "-y",
            "-bso0",
            "-bsp0",
        ]
        completed = (runner or subprocess.run)(
            command,
            capture_output=True,
            timeout=DOWNLOAD_TIMEOUT_SECONDS,
        )
        if completed.returncode != 0:
            raise ToolchainError(
                "TOOLCHAIN_EXTRACT_FAILED",
                "\u65e0\u6cd5\u89e3\u5f00\u5b98\u65b9 7-Zip Windows \u5b58\u6863",
            )
        executable_source = unpacked / WINDOWS_EXECUTABLE
        library_source = unpacked / WINDOWS_LIBRARY
        if not executable_source.is_file() or not library_source.is_file():
            raise ToolchainError(
                "TOOLCHAIN_BINARY_MISSING",
                "\u5b98\u65b9 7-Zip Windows \u5b58\u6863\u7f3a\u5c11 7z.exe \u6216 7z.dll",
            )
        executable = destination / WINDOWS_EXECUTABLE
        library = destination / WINDOWS_LIBRARY
        shutil.copy2(executable_source, executable)
        shutil.copy2(library_source, library)
        return executable, library
    except ToolchainError:
        raise
    except (OSError, subprocess.SubprocessError) as exc:
        raise ToolchainError(
            "TOOLCHAIN_EXTRACT_FAILED",
            f"\u65e0\u6cd5\u89e3\u5f00\u5b98\u65b9 7-Zip Windows \u5b58\u6863: {exc}",
        ) from exc
    finally:
        shutil.rmtree(work, ignore_errors=True)


def install(
    tools_path: Path,
    *,
    system: str | None = None,
    machine: str | None = None,
    force: bool = False,
    download=None,
    runner=None,
) -> Path:
    """Install the pinned official 7-Zip build and return its executable.

    The install is idempotent: an existing verified binary for this version is
    reused unless `force` is set.
    """
    resolved_system = (system or platform.system()).strip().lower()
    existing = installed_executable(tools_path, system=resolved_system)
    if existing is not None and not force:
        return existing
    asset = asset_for_platform(system, machine)
    payload = (download or _download)(asset.url)
    verify_digest(payload, asset.sha256)
    bootstrap_payload: bytes | None = None
    if resolved_system == "windows":
        bootstrap_payload = (download or _download)(WINDOWS_BOOTSTRAP_ASSET.url)
        verify_digest(bootstrap_payload, WINDOWS_BOOTSTRAP_ASSET.sha256)
    root = install_root(tools_path)
    staging = root.with_name(f"{root.name}.incoming")
    shutil.rmtree(staging, ignore_errors=True)
    try:
        if bootstrap_payload is None:
            extract_binaries(payload, staging)
        else:
            extract_windows_binaries(
                payload, bootstrap_payload, staging, runner=runner
            )
        root.parent.mkdir(parents=True, exist_ok=True)
        shutil.rmtree(root, ignore_errors=True)
        staging.replace(root)
    finally:
        shutil.rmtree(staging, ignore_errors=True)
    executable = installed_executable(tools_path, system=resolved_system)
    if executable is None:
        raise ToolchainError(
            "TOOLCHAIN_BINARY_MISSING",
            "\u5b89\u88c5\u540e\u672a\u627e\u5230\u53ef\u6267\u884c\u7684 7-Zip \u4e8c\u8fdb\u5236",
        )
    return executable


__all__ = [
    "DOWNLOAD_TIMEOUT_SECONDS",
    "PREFERRED_BINARIES",
    "RELEASE_ASSETS",
    "RELEASE_BASE_URL",
    "SEVEN_ZIP_VERSION",
    "WINDOWS_BOOTSTRAP_ASSET",
    "WINDOWS_EXECUTABLE",
    "WINDOWS_LIBRARY",
    "ReleaseAsset",
    "ToolchainError",
    "asset_for_platform",
    "extract_binaries",
    "extract_windows_binaries",
    "install",
    "install_root",
    "installed_executable",
    "normalize_machine",
    "verify_digest",
]
