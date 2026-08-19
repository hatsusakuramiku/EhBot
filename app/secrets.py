from __future__ import annotations

from pathlib import Path
import re

from app.private_files import restrict_private_path, write_private_text


_SECRET_NAME = re.compile(r"^[a-z0-9_]+$")


class SecretStore:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)
        restrict_private_path(self.root, directory=True)

    def _path(self, name: str) -> Path:
        if not _SECRET_NAME.fullmatch(name):
            raise ValueError("Secret name may contain only lowercase letters, digits, and _")
        return self.root / name

    def write(self, name: str, value: str) -> None:
        write_private_text(self._path(name), value)

    def read(self, name: str) -> str | None:
        path = self._path(name)
        if not path.exists():
            return None
        return path.read_text(encoding="utf-8")

    def is_configured(self, name: str) -> bool:
        return self._path(name).is_file()

    def delete(self, name: str) -> None:
        self._path(name).unlink(missing_ok=True)
