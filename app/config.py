from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


def _read_secret(name: str) -> str | None:
    direct_value = os.getenv(name)
    file_name = os.getenv(f"{name}_FILE")
    if direct_value and file_name:
        raise ValueError(f"Set either {name} or {name}_FILE, not both")
    if file_name:
        return Path(file_name).read_text(encoding="utf-8").strip()
    return direct_value


@dataclass(frozen=True, slots=True)
class Settings:
    data_path: Path = Path("data")
    library_path: Path = Path("library")
    work_path: Path = Path("work")
    app_secret_key: str | None = None
    admin_password_hash: str | None = None
    session_cookie_secure: bool = False

    @classmethod
    def from_env(cls) -> Settings:
        return cls(
            data_path=Path(os.getenv("DATA_PATH", "data")),
            library_path=Path(os.getenv("LIBRARY_PATH", "library")),
            work_path=Path(os.getenv("WORK_PATH", "work")),
            app_secret_key=_read_secret("APP_SECRET_KEY"),
            admin_password_hash=_read_secret("ADMIN_PASSWORD_HASH"),
            session_cookie_secure=os.getenv("SESSION_COOKIE_SECURE", "false").lower()
            in {"1", "true", "yes"},
        )

    def readiness_errors(self) -> list[str]:
        errors: list[str] = []
        if not self.app_secret_key or len(self.app_secret_key) < 32:
            errors.append("APP_SECRET_KEY must contain at least 32 characters")
        if not self.admin_password_hash:
            errors.append("ADMIN_PASSWORD_HASH is required")
        return errors
