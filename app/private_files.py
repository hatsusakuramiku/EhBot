from __future__ import annotations

import csv
import os
from pathlib import Path
import subprocess


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
