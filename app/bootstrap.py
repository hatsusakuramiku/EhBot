from __future__ import annotations

import os
from pathlib import Path
import tempfile

from app.private_files import restrict_private_path


BOOTSTRAP_PASSWORD_FILE = "bootstrap_admin_password"


def write_bootstrap_password(data_path: Path, password: str) -> Path:
    destination = data_path / BOOTSTRAP_PASSWORD_FILE
    descriptor, temporary_name = tempfile.mkstemp(
        dir=data_path,
        prefix=".bootstrap-password-",
    )
    os.close(descriptor)
    temporary_path = Path(temporary_name)
    try:
        restrict_private_path(temporary_path)
        temporary_path.write_text(password, encoding="utf-8")
        os.replace(temporary_path, destination)
        restrict_private_path(destination)
    except BaseException:
        temporary_path.unlink(missing_ok=True)
        raise
    return destination


def remove_bootstrap_password(data_path: Path) -> None:
    (data_path / BOOTSTRAP_PASSWORD_FILE).unlink(missing_ok=True)
