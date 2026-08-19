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


def _read_bool(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.lower() in {"1", "true", "yes"}


@dataclass(frozen=True, slots=True)
class Settings:
    data_path: Path = Path("data")
    library_path: Path = Path("library")
    work_path: Path = Path("work")
    app_secret_key: str | None = None
    session_cookie_secure: bool = False
    trust_proxy_headers: bool = False
    trusted_proxy_ips: tuple[str, ...] = ()
    app_root_path: str = ""
    tag_translation_enabled: bool = True

    @classmethod
    def from_env(cls) -> Settings:
        return cls(
            data_path=Path(os.getenv("DATA_PATH", "data")),
            library_path=Path(os.getenv("LIBRARY_PATH", "library")),
            work_path=Path(os.getenv("WORK_PATH", "work")),
            app_secret_key=_read_secret("APP_SECRET_KEY"),
            session_cookie_secure=_read_bool("SESSION_COOKIE_SECURE"),
            trust_proxy_headers=_read_bool("TRUST_PROXY_HEADERS"),
            trusted_proxy_ips=tuple(
                item.strip()
                for item in os.getenv("TRUSTED_PROXY_IPS", "").split(",")
                if item.strip()
            ),
            app_root_path=os.getenv("APP_ROOT_PATH", ""),
            tag_translation_enabled=_read_bool(
                "TAG_TRANSLATION_ENABLED", True
            ),
        )

    def readiness_errors(self) -> list[str]:
        errors: list[str] = []
        if not self.app_secret_key or len(self.app_secret_key) < 32:
            errors.append("APP_SECRET_KEY must contain at least 32 characters")
        if self.trust_proxy_headers and not self.trusted_proxy_ips:
            errors.append("TRUSTED_PROXY_IPS is required when proxy headers are trusted")
        return errors
