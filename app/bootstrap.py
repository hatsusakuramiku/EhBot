from pathlib import Path

from app.private_files import write_private_text


BOOTSTRAP_PASSWORD_FILE = "bootstrap_admin_password"


def write_bootstrap_password(data_path: Path, password: str) -> Path:
    destination = data_path / BOOTSTRAP_PASSWORD_FILE
    write_private_text(destination, password)
    return destination


def remove_bootstrap_password(data_path: Path) -> None:
    (data_path / BOOTSTRAP_PASSWORD_FILE).unlink(missing_ok=True)
