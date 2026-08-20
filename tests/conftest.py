"""Test-wide safety net for the archive toolchain.

Startup normally installs the pinned official 7-Zip build when none is
present. Tests must never depend on the network, so automatic installation is
disabled for the whole session and any download attempt is turned into the
same failure the application would see with no connectivity.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from app.archive import toolchain


@pytest.fixture(autouse=True, scope="session")
def disable_toolchain_auto_install() -> Iterator[None]:
    """Force the auto-install switch off for every test.

    This overwrites the variable instead of defaulting it, because an operator
    shell that exports `ARCHIVE_TOOLCHAIN_AUTO_INSTALL=true` must not make the
    suite reach the network.
    """
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setenv("ARCHIVE_TOOLCHAIN_AUTO_INSTALL", "false")
    yield
    monkeypatch.undo()


@pytest.fixture(autouse=True)
def block_toolchain_downloads(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make every unmocked download fail exactly like an offline host.

    A `ToolchainError` is used rather than an `AssertionError` for two reasons:
    tests that call `install()` directly still fail loudly with a stable code,
    and the startup path exercises its real degraded branch instead of an
    exception type it would never see in production. Tests that need a payload
    inject their own `download` callable.
    """

    def refuse(url: str) -> bytes:
        raise toolchain.ToolchainError(
            "TOOLCHAIN_DOWNLOAD_FAILED",
            f"a test attempted to download {url}; inject a download callable",
        )

    monkeypatch.setattr(toolchain, "_download", refuse)
