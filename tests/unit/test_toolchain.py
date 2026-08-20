"""Toolchain installation coverage.

These tests never touch the network: the download step is injected, and the
payloads are real `.tar.xz` archives built in-process. That keeps digest
verification, allowlisted extraction, idempotency, and failure handling
provable on any host.
"""

from __future__ import annotations

import hashlib
import io
import tarfile
from pathlib import Path

import pytest

from app.archive.toolchain import (
    PREFERRED_BINARIES,
    RELEASE_ASSETS,
    RELEASE_BASE_URL,
    SEVEN_ZIP_VERSION,
    ToolchainError,
    asset_for_platform,
    extract_binaries,
    install,
    install_root,
    installed_executable,
    normalize_machine,
    verify_digest,
)


def build_tar_xz(entries: dict[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:xz") as archive:
        for name, payload in entries.items():
            info = tarfile.TarInfo(name)
            info.size = len(payload)
            info.mode = 0o755
            archive.addfile(info, io.BytesIO(payload))
    return buffer.getvalue()


def release_payload() -> bytes:
    return build_tar_xz(
        {
            "7zz": b"#!/bin/sh\nexit 0\n",
            "7zzs": b"#!/bin/sh\nexit 0\n",
            "readme.txt": b"documentation",
            "MANUAL/start.htm": b"<html></html>",
        }
    )


def install_with(tools_path: Path, payload: bytes, **kwargs) -> Path:
    """Install using a pinned digest that matches the supplied payload."""
    digest = hashlib.sha256(payload).hexdigest()
    monkey_asset = RELEASE_ASSETS[("linux", "x64")]
    patched = type(monkey_asset)(file_name=monkey_asset.file_name, sha256=digest)
    original = RELEASE_ASSETS[("linux", "x64")]
    RELEASE_ASSETS[("linux", "x64")] = patched
    try:
        return install(
            tools_path,
            system="linux",
            machine="x86_64",
            download=lambda url: payload,
            **kwargs,
        )
    finally:
        RELEASE_ASSETS[("linux", "x64")] = original


# --- asset selection ------------------------------------------------------


def test_normalize_machine_maps_common_architectures() -> None:
    assert normalize_machine("x86_64") == "x64"
    assert normalize_machine("AMD64") == "x64"
    assert normalize_machine("aarch64") == "arm64"
    assert normalize_machine("armv7l") == "arm"
    assert normalize_machine("i686") == "x86"


def test_asset_for_platform_targets_linux_and_macos() -> None:
    linux = asset_for_platform("Linux", "x86_64")
    assert linux.file_name == "7z2602-linux-x64.tar.xz"
    assert linux.url == (
        f"{RELEASE_BASE_URL}/{SEVEN_ZIP_VERSION}/7z2602-linux-x64.tar.xz"
    )
    assert asset_for_platform("Linux", "aarch64").file_name.endswith(
        "linux-arm64.tar.xz"
    )
    assert asset_for_platform("Darwin", "arm64").file_name.endswith("mac.tar.xz")


def test_asset_for_platform_rejects_unsupported_platform() -> None:
    with pytest.raises(ToolchainError) as error:
        asset_for_platform("Plan9", "sparc")
    assert error.value.code == "TOOLCHAIN_PLATFORM_UNSUPPORTED"


def test_every_pinned_asset_declares_a_sha256() -> None:
    for key, asset in RELEASE_ASSETS.items():
        assert len(asset.sha256) == 64, key
        assert asset.sha256 == asset.sha256.lower(), key
        assert SEVEN_ZIP_VERSION.replace(".", "") in asset.file_name, key


# --- digest verification --------------------------------------------------


def test_verify_digest_accepts_matching_payload() -> None:
    payload = b"official binary"
    digest = hashlib.sha256(payload).hexdigest()
    assert verify_digest(payload, digest) == digest


def test_verify_digest_rejects_mismatched_payload() -> None:
    with pytest.raises(ToolchainError) as error:
        verify_digest(b"tampered", hashlib.sha256(b"original").hexdigest())
    assert error.value.code == "TOOLCHAIN_DIGEST_MISMATCH"


# --- extraction -----------------------------------------------------------


def test_extract_binaries_only_writes_known_executables(tmp_path: Path) -> None:
    extracted = extract_binaries(release_payload(), tmp_path / "out")

    names = sorted(path.name for path in extracted)
    assert names == sorted(PREFERRED_BINARIES)
    written = sorted(path.name for path in (tmp_path / "out").iterdir())
    assert written == sorted(PREFERRED_BINARIES)
    assert not (tmp_path / "out" / "readme.txt").exists()
    assert not (tmp_path / "out" / "MANUAL").exists()


def test_extract_binaries_ignores_unexpected_member_paths(tmp_path: Path) -> None:
    """A crafted archive cannot place files outside the destination."""
    payload = build_tar_xz(
        {
            "../escape": b"nope",
            "/absolute": b"nope",
            "nested/7zz": b"nope",
            "7zz": b"#!/bin/sh\nexit 0\n",
        }
    )

    extract_binaries(payload, tmp_path / "out")

    assert sorted(p.name for p in (tmp_path / "out").iterdir()) == ["7zz"]
    assert not (tmp_path / "escape").exists()
    assert not (tmp_path.parent / "escape").exists()


def test_extract_binaries_rejects_archive_without_executables(
    tmp_path: Path,
) -> None:
    payload = build_tar_xz({"readme.txt": b"docs only"})
    with pytest.raises(ToolchainError) as error:
        extract_binaries(payload, tmp_path / "out")
    assert error.value.code == "TOOLCHAIN_BINARY_MISSING"


def test_extract_binaries_rejects_corrupt_archive(tmp_path: Path) -> None:
    with pytest.raises(ToolchainError) as error:
        extract_binaries(b"not a tar.xz archive", tmp_path / "out")
    assert error.value.code == "TOOLCHAIN_EXTRACT_FAILED"


# --- installation ---------------------------------------------------------


def test_install_places_binary_in_versioned_directory(tmp_path: Path) -> None:
    executable = install_with(tmp_path / "tools", release_payload())

    assert executable.parent == install_root(tmp_path / "tools")
    assert SEVEN_ZIP_VERSION in str(executable)
    # The statically linked build is preferred for slim images.
    assert executable.name == "7zzs"
    assert installed_executable(tmp_path / "tools") == executable


def test_install_is_idempotent_and_skips_repeat_downloads(tmp_path: Path) -> None:
    payload = release_payload()
    digest = hashlib.sha256(payload).hexdigest()
    original = RELEASE_ASSETS[("linux", "x64")]
    RELEASE_ASSETS[("linux", "x64")] = type(original)(
        file_name=original.file_name, sha256=digest
    )
    downloads: list[str] = []

    def download(url: str) -> bytes:
        downloads.append(url)
        return payload

    try:
        first = install(
            tmp_path / "tools",
            system="linux",
            machine="x86_64",
            download=download,
        )
        second = install(
            tmp_path / "tools",
            system="linux",
            machine="x86_64",
            download=download,
        )
    finally:
        RELEASE_ASSETS[("linux", "x64")] = original

    assert first == second
    assert len(downloads) == 1
    assert downloads[0].endswith("7z2602-linux-x64.tar.xz")


def test_install_rejects_payload_failing_digest_check(tmp_path: Path) -> None:
    with pytest.raises(ToolchainError) as error:
        install(
            tmp_path / "tools",
            system="linux",
            machine="x86_64",
            download=lambda url: b"a mirror served something else",
        )
    assert error.value.code == "TOOLCHAIN_DIGEST_MISMATCH"
    assert installed_executable(tmp_path / "tools") is None


def test_failed_reinstall_keeps_the_existing_binary(tmp_path: Path) -> None:
    tools = tmp_path / "tools"
    good = install_with(tools, release_payload())
    assert good.is_file()

    with pytest.raises(ToolchainError):
        install(
            tools,
            system="linux",
            machine="x86_64",
            force=True,
            download=lambda url: b"corrupted",
        )

    assert installed_executable(tools) == good
    assert not install_root(tools).with_name(
        f"{install_root(tools).name}.incoming"
    ).exists()


def test_force_reinstall_replaces_the_binary(tmp_path: Path) -> None:
    tools = tmp_path / "tools"
    install_with(tools, release_payload())
    replacement = build_tar_xz(
        {"7zz": b"#!/bin/sh\nexit 1\n", "7zzs": b"#!/bin/sh\nexit 7\n"}
    )

    executable = install_with(tools, replacement, force=True)

    assert executable.read_bytes() == b"#!/bin/sh\nexit 7\n"


def test_installed_executable_returns_none_before_install(tmp_path: Path) -> None:
    assert installed_executable(tmp_path / "tools") is None