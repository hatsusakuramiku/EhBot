"""Tests for the session key resolution that removed the manual secret step."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from app.session_secret import (
    MINIMUM_LENGTH,
    SESSION_SECRET_NAME,
    resolve_session_secret,
)


def _stored_path(data_path: Path) -> Path:
    return data_path / "private" / SESSION_SECRET_NAME


def test_a_configured_key_is_used_and_never_persisted(tmp_path: Path) -> None:
    """An operator-supplied key belongs to their secret manager, not to us."""
    resolved = resolve_session_secret(tmp_path, "configured-key-long-enough-32chars")

    assert resolved.key == "configured-key-long-enough-32chars"
    assert resolved.source == "configured"
    assert resolved.error is None
    assert not _stored_path(tmp_path).exists()


def test_a_missing_key_is_generated_and_stored(tmp_path: Path) -> None:
    """First start must not require a hand-created file to come up."""
    resolved = resolve_session_secret(tmp_path, None)

    assert resolved.source == "generated"
    assert len(resolved.key) >= MINIMUM_LENGTH
    assert _stored_path(tmp_path).read_text(encoding="utf-8") == resolved.key


def test_a_stored_key_is_reused_across_restarts(tmp_path: Path) -> None:
    """Regenerating on every boot would log every operator out at random."""
    first = resolve_session_secret(tmp_path, None)
    second = resolve_session_secret(tmp_path, None)

    assert second.source == "stored"
    assert second.key == first.key


def test_an_empty_configured_value_falls_back_to_generation(tmp_path: Path) -> None:
    """`APP_SECRET_KEY=` in an env file must not produce an unsigned session."""
    resolved = resolve_session_secret(tmp_path, "")

    assert resolved.source == "generated"
    assert len(resolved.key) >= MINIMUM_LENGTH


def test_a_truncated_stored_key_is_replaced(tmp_path: Path) -> None:
    """A short stored key can only come from a damaged write; do not trust it."""
    path = _stored_path(tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("short", encoding="utf-8")

    resolved = resolve_session_secret(tmp_path, None)

    assert resolved.source == "generated"
    assert len(resolved.key) >= MINIMUM_LENGTH
    assert path.read_text(encoding="utf-8") == resolved.key


def test_a_rejecting_private_directory_fails_fast_instead_of_spinning(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A directory that refuses every create must raise, not retry forever.

    `tempfile.mkstemp` treats any `PermissionError` on Windows as a name
    collision and retries up to `TMP_MAX` times, so a `<data>/private` owned by
    another account used to spin at 100% CPU with the process never starting.
    """
    attempts = 0
    real_open = os.open

    def refusing_open(path, flags, mode=0o777, **kwargs):
        nonlocal attempts
        if os.O_CREAT & flags and ".session_secret_key-" in str(path):
            attempts += 1
            raise PermissionError(13, "Permission denied")
        return real_open(path, flags, mode, **kwargs)

    monkeypatch.setattr(os, "open", refusing_open)

    resolved = resolve_session_secret(tmp_path, None)

    assert resolved.is_ephemeral
    assert resolved.error is not None
    # Bounded: the old implementation would have attempted 2**31-1 times.
    assert 0 < attempts <= 16


def test_an_unwritable_private_directory_still_starts_but_reports_it(
    tmp_path: Path,
) -> None:
    """Sessions not surviving a restart is a warning, not a reason to refuse service."""
    blocker = tmp_path / "private"
    blocker.write_text("not a directory", encoding="utf-8")

    resolved = resolve_session_secret(tmp_path, None)

    assert resolved.is_ephemeral
    assert len(resolved.key) >= MINIMUM_LENGTH
    assert resolved.error is not None
    assert SESSION_SECRET_NAME in resolved.error
