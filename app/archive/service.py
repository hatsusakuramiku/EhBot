from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path

from app.archive.models import (
    ArchivePasswordEntry,
    SafetyLimits,
    ToolProfile,
)
from app.archive.backends.seven_zip import resolve_seven_zip_executable
from app.archive.quality import (
    IMAGE_QUALITY_LEVELS,
    IMAGE_QUALITY_PROFILES,
    QUALITY_LABELS,
    QUALITY_ORIGINAL,
    normalize_quality,
)
from app.archive.toolchain import (
    SEVEN_ZIP_VERSION,
    ToolchainError,
    asset_for_platform,
    install as install_seven_zip,
    installed_executable,
)
from app.archive.vault import (
    VaultError,
    decrypt_password,
    encrypt_password,
    generate_master_key,
)
# The layout template is validated with the same code that renders it, so the
# settings page and the packing path cannot disagree about what is legal. The
# module holds nothing but string handling, so importing it here creates no
# archive -> conversion dependency worth the name.
from app.conversion.naming import (
    DEFAULT_LIBRARY_TEMPLATE,
    LibraryTemplateError,
    validate_library_template,
)
from app.db.database import Database
from app.private_files import write_private_text
from app.storage.readiness import ensure_writable_directory
from app.torrent.models import TorrentClientConfig


MASTER_KEY_NAME = "archive_password_key"

SETTING_KEEP_ORIGINAL = "keep_original"
SETTING_LIBRARY_TEMPLATE = "library_template"

#: Lossy re-encode level applied while packing the CBZ. Stored as a level name
#: rather than a JPEG number so the presets can be retuned without rewriting
#: what operators already saved.
SETTING_IMAGE_QUALITY = "image_quality"

#: Operator-editable directory overrides. The environment supplies the default,
#: and a stored value wins so the paths can be changed without a redeploy.
SETTING_LIBRARY_PATH = "library_path"
SETTING_WORK_PATH = "work_path"

PATH_SETTING_KEYS: tuple[str, ...] = (
    SETTING_LIBRARY_PATH,
    SETTING_WORK_PATH,
)

#: qBittorrent connection settings. The password is the only secret here, so
#: it goes through the same vault the archive passwords use and is never read
#: back into the page.
SETTING_TORRENT_URL = "torrent_client_url"
SETTING_TORRENT_USERNAME = "torrent_client_username"
SETTING_TORRENT_PASSWORD = "torrent_client_password"
SETTING_TORRENT_CATEGORY = "torrent_category"
SETTING_TORRENT_SAVE_PATH = "torrent_save_path"
SETTING_TORRENT_LOCAL_SAVE_PATH = "torrent_local_save_path"
SETTING_TORRENT_KEEP_SEEDING = "torrent_keep_seeding"
SETTING_TORRENT_AUTO_PACK = "torrent_auto_pack"
#: Auto-pack on any download completion (Telegram, ExHentai, Telegraph, and
#: the torrent route), independent of the torrent-specific toggle above.
SETTING_AUTO_PACK_AFTER_DOWNLOAD = "auto_pack_after_download"


def _is_readable_directory(path: Path) -> bool:
    """Prove the directory can actually be listed, not just that it exists.

    `is_dir()` succeeds on a mount EhBot has no permission to read, which is
    exactly the case that would strand an automatic pack hours later.
    """
    try:
        with os.scandir(path):
            return True
    except OSError:
        return False

LIMIT_KEYS: tuple[str, ...] = (
    "max_members",
    "max_total_bytes",
    "max_member_bytes",
    "max_compression_ratio",
    "max_depth",
)


class ArchiveSettingsError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.public_message = message


class ArchiveSettingsService:
    """Own archive tool profiles, safety limits, and the password vault."""

    def __init__(
        self,
        database: Database,
        data_path: Path,
        *,
        default_library_path: Path | None = None,
        default_work_path: Path | None = None,
        default_torrent_category: str = "ehbot",
        default_torrent_keep_seeding: bool = True,
    ) -> None:
        self._database = database
        self._data_path = data_path
        self._default_library_path = default_library_path
        self._default_work_path = default_work_path
        self._default_torrent_category = default_torrent_category
        self._default_torrent_keep_seeding = default_torrent_keep_seeding

    @property
    def tools_path(self) -> Path:
        """Managed tool installs live beside the database, not in the image."""
        return self._data_path / "tools"

    def _master_key_path(self) -> Path:
        return self._data_path / "private" / MASTER_KEY_NAME

    def _load_master_key_sync(self) -> bytes:
        path = self._master_key_path()
        if path.is_file():
            return bytes.fromhex(path.read_text(encoding="utf-8").strip())
        key = generate_master_key()
        write_private_text(path, key.hex())
        return key

    async def master_key(self) -> bytes:
        return await asyncio.to_thread(self._load_master_key_sync)

    async def limits(self) -> SafetyLimits:
        stored = await self._database.archive_settings()
        return SafetyLimits.from_mapping(
            {key: stored[key] for key in LIMIT_KEYS if key in stored}
        )

    async def save_limits(self, values: dict[str, str]) -> SafetyLimits:
        cleaned: dict[str, str] = {}
        for key in LIMIT_KEYS:
            if key not in values or str(values[key]).strip() == "":
                continue
            raw = str(values[key]).strip()
            try:
                number = float(raw)
            except ValueError as exc:
                raise ArchiveSettingsError(
                    "ARCHIVE_LIMIT_INVALID", f"{key} \u5fc5\u987b\u662f\u6570\u5b57"
                ) from exc
            if number <= 0:
                raise ArchiveSettingsError(
                    "ARCHIVE_LIMIT_INVALID", f"{key} \u5fc5\u987b\u5927\u4e8e 0"
                )
            cleaned[key] = raw
        if cleaned:
            await self._database.save_archive_settings(cleaned)
        return await self.limits()

    async def paths(self) -> dict[str, str]:
        """Resolve the effective runtime directories, overrides included."""
        stored = await self._database.archive_settings()
        library = stored.get(SETTING_LIBRARY_PATH) or (
            str(self._default_library_path)
            if self._default_library_path is not None
            else ""
        )
        work = stored.get(SETTING_WORK_PATH) or (
            str(self._default_work_path)
            if self._default_work_path is not None
            else ""
        )
        return {
            "data": str(self._data_path),
            "library": library,
            "work": work,
            "library_overridden": bool(stored.get(SETTING_LIBRARY_PATH)),
            "work_overridden": bool(stored.get(SETTING_WORK_PATH)),
        }

    async def library_path(self) -> Path | None:
        resolved = (await self.paths())["library"]
        return Path(resolved) if resolved else None

    async def work_path(self) -> Path | None:
        resolved = (await self.paths())["work"]
        return Path(resolved) if resolved else None

    async def save_paths(self, values: dict[str, str]) -> dict[str, str]:
        """Validate and store directory overrides.

        A path is only accepted once it exists (or can be created) and proves
        writable, because a bad value would otherwise break every download and
        publish with no way back through the UI. Submitting an empty field
        clears the override and restores the environment default.
        """
        cleaned: dict[str, str] = {}
        for key in PATH_SETTING_KEYS:
            if key not in values:
                continue
            raw = str(values[key]).strip()
            if raw == "":
                cleaned[key] = ""
                continue
            candidate = Path(raw)
            if not candidate.is_absolute():
                raise ArchiveSettingsError(
                    "PATH_NOT_ABSOLUTE",
                    "\u76ee\u5f55\u5fc5\u987b\u4f7f\u7528\u7edd\u5bf9\u8def\u5f84",
                )
            try:
                await asyncio.to_thread(ensure_writable_directory, candidate)
            except OSError as exc:
                raise ArchiveSettingsError(
                    "PATH_NOT_WRITABLE",
                    f"\u76ee\u5f55\u4e0d\u53ef\u5199\u5165\uff1a{exc}",
                ) from exc
            cleaned[key] = str(candidate)
        if cleaned:
            await self._database.save_archive_settings(cleaned)
        return await self.paths()

    async def torrent_client(self) -> TorrentClientConfig:
        """Assemble the qBittorrent configuration, password decrypted.

        A password that cannot be opened is reported as empty rather than
        raising: the operator sees an authentication failure they can fix by
        re-entering it, instead of a startup crash in a route that only some
        candidates take.
        """
        stored = await self._database.archive_settings()
        password = ""
        envelope = stored.get(SETTING_TORRENT_PASSWORD, "")
        if envelope:
            key = await self.master_key()
            try:
                password = await asyncio.to_thread(
                    decrypt_password, key, envelope
                )
            except VaultError:
                logging.getLogger(__name__).warning(
                    "torrent_client_password_unreadable",
                    extra={"error_code": "TORRENT_CLIENT_AUTH"},
                )
        return TorrentClientConfig(
            base_url=stored.get(SETTING_TORRENT_URL, ""),
            username=stored.get(SETTING_TORRENT_USERNAME, ""),
            password=password,
            category=(
                stored.get(SETTING_TORRENT_CATEGORY, "")
                or self._default_torrent_category
            ),
            save_path=stored.get(SETTING_TORRENT_SAVE_PATH, ""),
            local_save_path=stored.get(
                SETTING_TORRENT_LOCAL_SAVE_PATH, ""
            ),
            keep_seeding=stored.get(
                SETTING_TORRENT_KEEP_SEEDING,
                "1" if self._default_torrent_keep_seeding else "0",
            )
            not in {"0", "false", "no"},
            # Off unless the operator turned it on: packing publishes to the
            # library, and doing that without being asked would bypass the
            # review the rest of the pipeline is built around.
            auto_pack=stored.get(SETTING_TORRENT_AUTO_PACK, "0")
            in {"1", "true", "yes"},
        )

    async def torrent_client_view(self) -> dict[str, object]:
        """What the settings page may display; the password never appears."""
        config = await self.torrent_client()
        return {
            "base_url": config.base_url,
            "username": config.username,
            "category": config.category,
            "save_path": config.save_path,
            "local_save_path": config.local_save_path,
            "keep_seeding": config.keep_seeding,
            "auto_pack": config.auto_pack,
            "configured": config.is_configured,
            "password_set": bool(config.password),
        }

    async def save_torrent_client(self, values: dict[str, str]) -> None:
        """Store the qBittorrent settings after validating what can be checked.

        The local save path is verified to be readable now rather than at
        download time, because a typo discovered three hours into a torrent is
        a wasted transfer. An empty password field leaves the stored one alone
        so saving an unrelated field does not silently clear it.
        """
        base_url = str(values.get("base_url") or "").strip().rstrip("/")
        if base_url and not base_url.startswith(("http://", "https://")):
            raise ArchiveSettingsError(
                "TORRENT_URL_INVALID",
                "qBittorrent \u5730\u5740\u5fc5\u987b\u4ee5 http:// "
                "\u6216 https:// \u5f00\u5934",
            )
        local_save_path = str(
            values.get("local_save_path") or ""
        ).strip()
        auto_pack = bool(values.get("auto_pack"))
        if auto_pack and not local_save_path:
            # Automatic packing reads the finished payload without an operator
            # present, so the directory it reads from cannot be left unproven.
            raise ArchiveSettingsError(
                "TORRENT_LOCAL_PATH_REQUIRED",
                "\u5f00\u542f\u4e0b\u8f7d\u540e\u81ea\u52a8\u6253\u5305"
                "\u65f6\uff0c\u5fc5\u987b\u586b\u5199\u4fdd\u5b58\u76ee"
                "\u5f55\uff08EhBot \u89c6\u89d2\uff09",
            )
        if local_save_path:
            candidate = Path(local_save_path)
            if not candidate.is_absolute():
                raise ArchiveSettingsError(
                    "PATH_NOT_ABSOLUTE",
                    "\u4fdd\u5b58\u76ee\u5f55\u5fc5\u987b\u4f7f\u7528"
                    "\u7edd\u5bf9\u8def\u5f84",
                )
            if not await asyncio.to_thread(candidate.is_dir):
                raise ArchiveSettingsError(
                    "TORRENT_CONTENT_UNREACHABLE",
                    f"EhBot \u8bfb\u4e0d\u5230\u8be5\u76ee\u5f55\uff1a"
                    f"{local_save_path}",
                )
            if auto_pack and not await asyncio.to_thread(
                _is_readable_directory, candidate
            ):
                raise ArchiveSettingsError(
                    "TORRENT_CONTENT_UNREACHABLE",
                    f"\u81ea\u52a8\u6253\u5305\u9700\u8981\u8bfb\u53d6"
                    f"\u6743\u9650\uff0cEhBot \u65e0\u6cd5\u5217\u51fa"
                    f"\u8be5\u76ee\u5f55\uff1a{local_save_path}",
                )
        cleaned: dict[str, str] = {
            SETTING_TORRENT_URL: base_url,
            SETTING_TORRENT_USERNAME: str(
                values.get("username") or ""
            ).strip(),
            SETTING_TORRENT_CATEGORY: str(
                values.get("category") or ""
            ).strip()
            or self._default_torrent_category,
            SETTING_TORRENT_SAVE_PATH: str(
                values.get("save_path") or ""
            ).strip(),
            SETTING_TORRENT_LOCAL_SAVE_PATH: local_save_path,
            SETTING_TORRENT_KEEP_SEEDING: (
                "1" if values.get("keep_seeding") else "0"
            ),
            SETTING_TORRENT_AUTO_PACK: "1" if auto_pack else "0",
        }
        password = str(values.get("password") or "")
        if password:
            key = await self.master_key()
            cleaned[SETTING_TORRENT_PASSWORD] = await asyncio.to_thread(
                encrypt_password, key, password
            )
        await self._database.save_archive_settings(cleaned)

    async def auto_pack_after_download(self) -> bool:
        """Whether a finished download is handed straight to the packer.

        Defaults to off, matching the torrent route's existing auto-pack toggle:
        the pipeline stays quiet until the operator opts in. Conversion itself
        is idempotent per candidate, so a later enable repacks existing work.
        """
        stored = await self._database.archive_settings()
        return stored.get(SETTING_AUTO_PACK_AFTER_DOWNLOAD, "0") not in {
            "0",
            "false",
            "no",
        }

    async def save_auto_pack_after_download(self, enabled: bool) -> None:
        await self._database.save_archive_settings(
            {SETTING_AUTO_PACK_AFTER_DOWNLOAD: "1" if enabled else "0"}
        )

    async def library_template(self) -> str:
        """The stored layout template, or the flat default.

        Read without validating: a template stored by an older version, or one
        whose placeholder set has since changed, still has to reach the settings
        page so an operator can see and fix it. The packing path validates when
        it renders, and falls back to the default there.
        """
        stored = await self._database.archive_settings()
        return (
            stored.get(SETTING_LIBRARY_TEMPLATE, "").strip()
            or DEFAULT_LIBRARY_TEMPLATE
        )

    async def save_library_template(self, raw: str) -> str:
        """Store a layout template, refusing one that cannot render safely.

        Validation happens here rather than at packing time because that is
        hours later, with the book already downloaded and no operator watching.
        An empty submission restores the flat default instead of storing a
        template that puts every book in the library root by accident.
        """
        text = (raw or "").strip()
        if not text:
            await self._database.save_archive_settings(
                {SETTING_LIBRARY_TEMPLATE: ""}
            )
            return DEFAULT_LIBRARY_TEMPLATE
        try:
            template = validate_library_template(text)
        except LibraryTemplateError as exc:
            raise ArchiveSettingsError(exc.code, exc.public_message) from exc
        await self._database.save_archive_settings(
            {SETTING_LIBRARY_TEMPLATE: template}
        )
        return template

    async def image_quality(self) -> str:
        """The stored re-encode level, defaulting to the lossless original."""
        stored = await self._database.archive_settings()
        return normalize_quality(stored.get(SETTING_IMAGE_QUALITY))

    async def image_quality_view(self) -> dict[str, object]:
        selected = await self.image_quality()
        return {
            "selected": selected,
            "levels": [
                {
                    "value": level,
                    "label": QUALITY_LABELS[level],
                    "selected": level == selected,
                }
                for level in IMAGE_QUALITY_LEVELS
            ],
        }

    async def save_image_quality(self, level: str) -> str:
        """Store the re-encode level, refusing anything not a known preset.

        Only the four presets are accepted: an unknown value would silently
        fall back to ``original`` later and quietly publish books at a quality
        nobody asked for.
        """
        candidate = (level or "").strip().lower() or QUALITY_ORIGINAL
        if candidate not in IMAGE_QUALITY_PROFILES:
            raise ArchiveSettingsError(
                "ARCHIVE_QUALITY_INVALID",
                "\u56fe\u50cf\u8d28\u91cf\u5fc5\u987b\u662f"
                "\u539f\u59cb\u6587\u4ef6\u3001\u9ad8\u3001\u4e2d"
                "\u3001\u4f4e\u4e4b\u4e00",
            )
        await self._database.save_archive_settings(
            {SETTING_IMAGE_QUALITY: candidate}
        )
        return candidate

    async def keep_original(self) -> bool:
        stored = await self._database.archive_settings()
        return stored.get(SETTING_KEEP_ORIGINAL, "1") not in {"0", "false", "no"}

    async def save_keep_original(self, keep: bool) -> None:
        await self._database.save_archive_settings(
            {SETTING_KEEP_ORIGINAL: "1" if keep else "0"}
        )

    async def profiles(self, *, enabled_only: bool = False) -> tuple[ToolProfile, ...]:
        return await self._database.list_archive_tool_profiles(
            enabled_only=enabled_only
        )

    async def set_profile_state(
        self,
        name: str,
        *,
        enabled: bool | None = None,
        executable_path: str | None = None,
        timeout_seconds: int | None = None,
    ) -> None:
        try:
            await self._database.set_archive_tool_profile_state(
                name,
                enabled=enabled,
                executable_path=executable_path,
                timeout_seconds=timeout_seconds,
            )
        except LookupError as exc:
            raise ArchiveSettingsError(
                "ARCHIVE_PROFILE_NOT_FOUND",
                f"\u5de5\u5177 profile {name} \u4e0d\u5b58\u5728",
            ) from exc
        except ValueError as exc:
            raise ArchiveSettingsError(
                "ARCHIVE_PROFILE_INVALID",
                "\u8d85\u65f6\u65f6\u957f\u5fc5\u987b\u5927\u4e8e 0",
            ) from exc

    async def toolchain_status(self) -> dict[str, object]:
        """Report whether a usable 7-Zip binary is available and where from."""
        managed = await asyncio.to_thread(installed_executable, self.tools_path)
        resolved = await asyncio.to_thread(
            resolve_seven_zip_executable, "7zz", self.tools_path
        )
        try:
            asset = asset_for_platform()
            supported = True
            asset_name: str | None = asset.file_name
        except ToolchainError:
            supported = False
            asset_name = None
        return {
            "version": SEVEN_ZIP_VERSION,
            "managed_path": str(managed) if managed else None,
            "resolved_path": resolved,
            "available": resolved is not None,
            "platform_supported": supported,
            "asset_name": asset_name,
        }

    async def install_toolchain(self, *, force: bool = False) -> Path:
        """Download and verify the pinned official 7-Zip build."""
        try:
            executable = await asyncio.to_thread(
                install_seven_zip, self.tools_path, force=force
            )
        except ToolchainError as exc:
            raise ArchiveSettingsError(exc.code, exc.public_message) from exc
        logging.getLogger(__name__).info(
            "seven_zip_toolchain_installed",
            extra={"error_code": "TOOLCHAIN_INSTALLED"},
        )
        return executable

    async def ensure_toolchain(self) -> Path | None:
        """Install 7-Zip on startup when no usable binary is present.

        A failure here is never fatal: ZIP/CBZ conversion still works through
        the built-in backend, and RAR/7Z tasks fail with a recoverable tool
        error. Because this runs inside the application lifespan, every failure
        mode is contained here, including unexpected ones such as a proxy
        returning garbage or a read-only tools directory. Letting any exception
        escape would take down a service whose main features do not need 7-Zip.
        """
        try:
            resolved = await asyncio.to_thread(
                resolve_seven_zip_executable, "7zz", self.tools_path
            )
            if resolved is not None:
                return Path(resolved)
            return await self.install_toolchain()
        except ArchiveSettingsError as exc:
            logging.getLogger(__name__).warning(
                "seven_zip_toolchain_unavailable",
                extra={"error_code": exc.code},
            )
            return None
        except Exception:
            logging.getLogger(__name__).warning(
                "seven_zip_toolchain_unavailable",
                exc_info=True,
                extra={"error_code": "TOOLCHAIN_PROVISION_FAILED"},
            )
            return None

    async def passwords(self) -> tuple[ArchivePasswordEntry, ...]:
        return await self._database.list_archive_passwords()

    async def add_password(
        self, *, name: str, password: str, priority: int, enabled: bool = True
    ) -> int:
        cleaned_name = name.strip()
        if not cleaned_name:
            raise ArchiveSettingsError(
                "ARCHIVE_PASSWORD_NAME_REQUIRED",
                "\u5bc6\u7801\u6761\u76ee\u540d\u79f0\u4e0d\u80fd\u4e3a\u7a7a",
            )
        if not password:
            raise ArchiveSettingsError(
                "ARCHIVE_PASSWORD_REQUIRED",
                "\u5bc6\u7801\u4e0d\u80fd\u4e3a\u7a7a",
            )
        key = await self.master_key()
        secret_json = await asyncio.to_thread(encrypt_password, key, password)
        return await self._database.save_archive_password(
            name=cleaned_name,
            secret_json=secret_json,
            priority=priority,
            enabled=enabled,
        )

    async def delete_password(self, password_id: int) -> None:
        await self._database.delete_archive_password(password_id)

    async def password_attempts(self) -> tuple[tuple[int, str], ...]:
        """Decrypt enabled vault entries in attempt order.

        Entries that fail integrity verification are skipped instead of
        aborting the task, and the plaintext never enters logs or audits.
        """
        secrets = await self._database.list_archive_password_secrets()
        if not secrets:
            return ()
        key = await self.master_key()
        attempts: list[tuple[int, str]] = []
        for password_id, envelope in secrets:
            try:
                plaintext = await asyncio.to_thread(
                    decrypt_password, key, envelope
                )
            except VaultError:
                logging.getLogger(__name__).warning(
                    "archive_password_undecryptable",
                    extra={"error_code": "ARCHIVE_PASSWORD_UNDECRYPTABLE"},
                )
                continue
            attempts.append((password_id, plaintext))
        return tuple(attempts)

    async def mark_password_success(self, password_id: int) -> None:
        await self._database.mark_archive_password_success(password_id)


__all__ = [
    "ArchiveSettingsError",
    "ArchiveSettingsService",
    "DEFAULT_LIBRARY_TEMPLATE",
    "LIMIT_KEYS",
    "MASTER_KEY_NAME",
    "PATH_SETTING_KEYS",
    "SETTING_AUTO_PACK_AFTER_DOWNLOAD",
    "SETTING_IMAGE_QUALITY",
    "SETTING_KEEP_ORIGINAL",
    "SETTING_LIBRARY_PATH",
    "SETTING_TORRENT_AUTO_PACK",
    "SETTING_TORRENT_CATEGORY",
    "SETTING_TORRENT_KEEP_SEEDING",
    "SETTING_TORRENT_LOCAL_SAVE_PATH",
    "SETTING_TORRENT_PASSWORD",
    "SETTING_TORRENT_SAVE_PATH",
    "SETTING_TORRENT_URL",
    "SETTING_TORRENT_USERNAME",
    "SETTING_WORK_PATH",
]