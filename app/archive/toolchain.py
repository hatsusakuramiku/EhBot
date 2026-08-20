"""Fetch the official 7-Zip binary from the upstream GitHub release.

The deployment targets are Linux and Docker, where 7-Zip is not part of the
base image. Upstream publishes `.tar.xz` archives that Python's own `tarfile`
can unpack, so installing the tool needs no pre-existing archiver and no
distribution package.

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
import tarfile
from dataclasses import dataclass
from pathlib import Path


SEVEN_ZIP_VERSION = "26.02"

RELEASE_BASE_URL = "https://github.com/ip7z/7zip/releases/download"

#: Statically linked binary first: it runs on slim images that ship no
#: `libstdc++`. The dynamically linked `7zz` is the fallback.
PREFERRED_BINARIES: tuple[str, ...] = ("7zzs", "7zz")

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
            "\u7684\u4e8c\u8fdb\u5236\uff0c\u8bf7\u624b\u52a8\u5b89\u88c5\u5e76\u5728\u5f52\u6863\u8bbe\u7f6e\u4e2d\u586b\u5199\u8def\u5f84",
        )
    return asset


def install_root(tools_path: Path) -> Path:
    """Version the install directory so an upgrade never overwrites in place."""
    return Path(tools_path) / "7zip" / SEVEN_ZIP_VERSION


def installed_executable(tools_path: Path) -> Path | None:
    root = install_root(tools_path)
    for name in PREFERRED_BINARIES:
        candidate = root / name
        if candidate.is_file() and os.access(candidate, os.X_OK):
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


def install(
    tools_path: Path,
    *,
    system: str | None = None,
    machine: str | None = None,
    force: bool = False,
    download=None,
) -> Path:
    """Install the pinned official 7-Zip build and return its executable.

    The install is idempotent: an existing verified binary for this version is
    reused unless `force` is set.
    """
    existing = installed_executable(tools_path)
    if existing is not None and not force:
        return existing
    asset = asset_for_platform(system, machine)
    payload = (download or _download)(asset.url)
    verify_digest(payload, asset.sha256)
    root = install_root(tools_path)
    staging = root.with_name(f"{root.name}.incoming")
    shutil.rmtree(staging, ignore_errors=True)
    try:
        extract_binaries(payload, staging)
        root.parent.mkdir(parents=True, exist_ok=True)
        shutil.rmtree(root, ignore_errors=True)
        staging.replace(root)
    finally:
        shutil.rmtree(staging, ignore_errors=True)
    executable = installed_executable(tools_path)
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
    "ReleaseAsset",
    "ToolchainError",
    "asset_for_platform",
    "extract_binaries",
    "install",
    "install_root",
    "installed_executable",
    "normalize_machine",
    "verify_digest",
]