from __future__ import annotations

import csv
import os
from pathlib import Path
import subprocess
import tempfile


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


def write_private_text(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        dir=path.parent,
        prefix=f".{path.name}-",
    )
    os.close(descriptor)
    temporary_path = Path(temporary_name)
    try:
        restrict_private_path(temporary_path)
        temporary_path.write_text(value, encoding="utf-8")
        os.replace(temporary_path, path)
        restrict_private_path(path)
    except BaseException:
        temporary_path.unlink(missing_ok=True)
        raise
