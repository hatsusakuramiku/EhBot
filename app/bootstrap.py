from __future__ import annotations

import os
from pathlib import Path
from tempfile import NamedTemporaryFile


BOOTSTRAP_PASSWORD_FILE = "bootstrap_admin_password"


def write_bootstrap_password(data_path: Path, password: str) -> Path:
    destination = data_path / BOOTSTRAP_PASSWORD_FILE
    with NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        dir=data_path,
        prefix=".bootstrap-password-",
        delete=False,
    ) as temporary_file:
        temporary_file.write(password)
        temporary_path = Path(temporary_file.name)
    try:
        temporary_path.chmod(0o600)
        os.replace(temporary_path, destination)
        destination.chmod(0o600)
    except BaseException:
        temporary_path.unlink(missing_ok=True)
        raise
    return destination


def remove_bootstrap_password(data_path: Path) -> None:
    (data_path / BOOTSTRAP_PASSWORD_FILE).unlink(missing_ok=True)
