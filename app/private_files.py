from __future__ import annotations

import csv
import os
from pathlib import Path
import secrets
import subprocess


#: Bounded because a name collision is a coincidence, not a condition to wait
#: out. See `_create_exclusive_temp` for why the standard library cannot be
#: used here.
_TEMP_ATTEMPTS = 16


def restrict_private_path(path: Path, *, directory: bool = False) -> None:
    if os.name != "nt":
        path.chmod(0o700 if directory else 0o600)
        return

    identity = subprocess.run(
        ["whoami", "/user", "/fo", "csv", "/nh"],
        check=True,
        capture_output=True,
        text=True,
    )
    rows = list(csv.reader(identity.stdout.splitlines()))
    if len(rows) != 1 or len(rows[0]) < 2:
        raise OSError("Unable to resolve the current Windows user SID")
    sid = rows[0][1]
    permission = f"*{sid}:(OI)(CI)F" if directory else f"*{sid}:F"
    subprocess.run(
        ["icacls", str(path), "/inheritance:r", "/grant:r", permission],
        check=True,
        capture_output=True,
        text=True,
    )


def _create_exclusive_temp(directory: Path, prefix: str) -> Path:
    """Create a new private temp file, failing fast if the directory refuses it.

    `tempfile.mkstemp` cannot be used here. On Windows it treats *every*
    `PermissionError` as "a directory of that name already exists" and retries
    up to `TMP_MAX` (2**31-1) times, because `os.access(dir, W_OK)` only reports
    the read-only attribute and not the ACL. A private directory owned by
    another account therefore makes it spin at 100% CPU indefinitely instead of
    raising -- which is exactly what a `<data>/private` directory left behind by
    a container running as root looks like. A bounded loop turns the same
    situation into an immediate OSError.
    """
    last_error: OSError | None = None
    for _ in range(_TEMP_ATTEMPTS):
        candidate = directory / f"{prefix}{secrets.token_hex(8)}"
        try:
            descriptor = os.open(
                candidate, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600
            )
        except FileExistsError as exc:
            last_error = exc
            continue
        os.close(descriptor)
        return candidate
    raise last_error or OSError(
        f"Unable to create a temporary file in {directory}"
    )


def write_private_text(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = _create_exclusive_temp(path.parent, f".{path.name}-")
    try:
        restrict_private_path(temporary_path)
        temporary_path.write_text(value, encoding="utf-8")
        os.replace(temporary_path, path)
        restrict_private_path(path)
    except BaseException:
        temporary_path.unlink(missing_ok=True)
        raise
