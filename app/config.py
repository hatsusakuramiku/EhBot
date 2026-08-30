from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

#: The three log levels an operator can set via `LOG_LEVEL`. Defined here
#: (foundational layer) so the runtime config and the UI dropdown can both
#: import it without the API layer depending on configuration.
LOG_LEVEL_CHOICES: frozenset[str] = frozenset({"ERROR", "WARNING", "INFO"})


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


def _read_log_level(name: str, default: str, allowed: frozenset[str]) -> str:
    """Read a runtime log level, refusing anything outside the choice set.

    The choices are narrowed to `ERROR` / `WARNING` / `INFO` because those
    three are what an operator toggles in the UI; `DEBUG` is too noisy for a
    long-running service and `CRITICAL` is a programming convention rather
    than something to set. A typo in `LOG_LEVEL` falls back to the default
    rather than aborting startup, for the same reason the other deployment
    settings do.
    """
    value = os.getenv(name, "").strip().upper() or default
    if value not in allowed:
        return default
    return value


def _read_int(name: str, default: int) -> int:
    """Read a positive integer setting, ignoring anything unusable.

    A malformed limit falls back to the default rather than aborting startup:
    an operator typo in one bound must not take the whole service down, and the
    default is always the safe value.
    """
    value = os.getenv(name)
    if value is None:
        return default
    try:
        parsed = int(value.strip())
    except ValueError:
        return default
    return parsed if parsed > 0 else default


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
    archive_toolchain_auto_install: bool = True
    telegraph_enabled: bool = True
    telegraph_concurrency: int = 3
    telegraph_max_images: int = 400
    telegraph_max_image_bytes: int = 20 * 1024 * 1024
    telegraph_max_total_bytes: int = 1024 * 1024 * 1024
    telegraph_timeout_seconds: int = 600
    telegraph_require_filecount_match: bool = True
    torrent_enabled: bool = True
    torrent_poll_seconds: int = 15
    torrent_category: str = "ehbot"
    torrent_keep_seeding: bool = True
    thumbnails_enabled: bool = True
    # Logging is a deployment-level concern, so it stays in the environment
    # rather than joining the three preferences in `system_settings`: a
    # deployment whose log level lives in the database cannot raise it to
    # debug the startup that failed before the database opened.
    log_level: str = "INFO"
    log_access: bool = True
    log_to_file: bool = True
    log_file_max_bytes: int = 10 * 1024 * 1024
    log_file_backups: int = 5

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
            archive_toolchain_auto_install=_read_bool(
                "ARCHIVE_TOOLCHAIN_AUTO_INSTALL", True
            ),
            telegraph_enabled=_read_bool("TELEGRAPH_ENABLED", True),
            telegraph_concurrency=_read_int("TELEGRAPH_CONCURRENCY", 3),
            telegraph_max_images=_read_int("TELEGRAPH_MAX_IMAGES", 400),
            telegraph_max_image_bytes=_read_int(
                "TELEGRAPH_MAX_IMAGE_BYTES", 20 * 1024 * 1024
            ),
            telegraph_max_total_bytes=_read_int(
                "TELEGRAPH_MAX_TOTAL_BYTES", 1024 * 1024 * 1024
            ),
            telegraph_timeout_seconds=_read_int(
                "TELEGRAPH_TIMEOUT_SECONDS", 600
            ),
            telegraph_require_filecount_match=_read_bool(
                "TELEGRAPH_REQUIRE_FILECOUNT_MATCH", True
            ),
            torrent_enabled=_read_bool("TORRENT_ENABLED", True),
            torrent_poll_seconds=_read_int("TORRENT_POLL_SECONDS", 15),
            torrent_category=(
                os.getenv("TORRENT_CATEGORY", "").strip() or "ehbot"
            ),
            torrent_keep_seeding=_read_bool("TORRENT_KEEP_SEEDING", True),
            thumbnails_enabled=_read_bool("THUMBNAILS_ENABLED", True),
            log_level=_read_log_level("LOG_LEVEL", "INFO", LOG_LEVEL_CHOICES),
            log_access=_read_bool("LOG_ACCESS", True),
            log_to_file=_read_bool("LOG_TO_FILE", True),
            log_file_max_bytes=_read_int(
                "LOG_FILE_MAX_BYTES", 10 * 1024 * 1024
            ),
            log_file_backups=_read_int("LOG_FILE_BACKUPS", 5),
        )

    @property
    def log_dir(self) -> Path:
        """Where the rotating log file goes.

        Under `data/` because that directory is already a bind mount the
        documentation tells operators to keep, so retention survives a
        container rebuild without asking them to mount anything new.
        """
        return self.data_path / "logs"

    def readiness_errors(self) -> list[str]:
        """Report only what an operator must fix; the session key is not one.

        `APP_SECRET_KEY` is optional: when it is unset the application generates
        and persists one under `<data>/private`. It is still validated when
        supplied, because a deliberately configured key that is too short is a
        mistake worth reporting rather than silently accepting.
        """
        errors: list[str] = []
        if self.app_secret_key is not None and len(self.app_secret_key) < 32:
            errors.append("APP_SECRET_KEY must contain at least 32 characters")
        if self.trust_proxy_headers and not self.trusted_proxy_ips:
            errors.append("TRUSTED_PROXY_IPS is required when proxy headers are trusted")
        return errors
