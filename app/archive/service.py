from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path

from app.archive.models import (
    ArchivePasswordEntry,
    ArchivePathRule,
    SafetyLimits,
    ToolProfile,
)
from app.auto_approval.rules import (
    RuleValidationError,
    evaluate_rule,
    render_rule_dsl,
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
from app.ai.prompt import DEFAULT_AI_PROMPT
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

#: Which language's title `{title}` renders as. It exists because the English
#: title an upload carries is frequently the one with the illegal characters and
#: the transliterated punctuation, while `title_jpn` is the name the book
#: actually has -- so an operator kept getting names the filesystem refused, or
#: accepted and mangled.
#:
#: The default is the Japanese title, which is a deliberate change of behaviour:
#: `{title}` used to be `Title` unconditionally. It is safe because the fallback
#: chain never leaves a book unnamed -- a gallery with no `title_jpn` renders its
#: `Title`, exactly as before.
#:
#: **This setting governs the path and not the metadata.** ComicInfo's `<Title>`
#: stays the English `Title` with the Japanese one beside it in
#: `<JapaneseTitle>`, whichever way this is set. The two answer different
#: questions: a path has to survive a filesystem, so it is a preference, while
#: ComicInfo is a record of what the gallery said and a reader that shows the
#: wrong field is a reader problem. Making the metadata follow this would mean a
#: setting silently rewrote the archive's contents.
SETTING_TITLE_SOURCE = "library_title_source"

#: Which layer decides where a book lands. **The two are mutually exclusive**
#: (proposal §2): 「模板 + 规则」 and the model answer the same question, and
#: 「AI 命中就用 AI，否则用规则」 would itself be a third rule engine. `template`
#: is the default so an upgrade moves nothing, and because AI mode needs a
#: provider and a verified model chain before it can do anything at all.
SETTING_PATH_SOURCE = "path_source"
PATH_SOURCE_TEMPLATE = "template"
PATH_SOURCE_AI = "ai"
PATH_SOURCES: tuple[str, ...] = (PATH_SOURCE_TEMPLATE, PATH_SOURCE_AI)
DEFAULT_PATH_SOURCE = PATH_SOURCE_TEMPLATE

#: The operator's prompt, verbatim, and the only input to the model besides the
#: metadata. Stored as text because it is theirs; the default is the proposal's
#: §6.1 reference, and an emptied value means 「回到默认」 rather than 「问一个空
#: 问题」 -- see `ai_prompt`.
SETTING_AI_PROMPT = "ai_prompt"

#: Whether a model chain that fails completely falls back to the rules. Default
#: off: a silent substitution would let an operator believe AI chose a path when
#: the template did, and a path once written is a fact. Off means the book parks
#: in 需干预, where the failure is on a list instead of behind the operator's back.
SETTING_AI_FALLBACK_TO_RULES = "ai_fallback_to_rules"

#: Re-archive scheduling for AI mode (proposal §9). Read by R30; stored here
#: with the rest of the path settings so one page owns them.
SETTING_AI_BATCH_SIZE = "ai_batch_size"
SETTING_AI_CONCURRENCY = "ai_concurrency"
SETTING_AI_STREAM = "ai_stream"
SETTING_AI_DEFAULT_INCLUDE_CURRENT = "ai_default_include_current"

#: Which models the *archive-path* feature uses: the global default chain, or a
#: list of its own. The AI page owns the default (AstrBot's 「全局默认模型」);
#: every other page inherits it unless it says otherwise, which is why the
#: default value here is `default` and why an empty custom list is an error
#: rather than a second inheritance.
SETTING_AI_MODEL_SOURCE = "ai_model_source"
MODEL_SOURCE_DEFAULT = "default"
MODEL_SOURCE_CUSTOM = "custom"
MODEL_SOURCES: tuple[str, ...] = (MODEL_SOURCE_DEFAULT, MODEL_SOURCE_CUSTOM)
DEFAULT_AI_MODEL_SOURCE = MODEL_SOURCE_DEFAULT

DEFAULT_AI_BATCH_SIZE = 20
MIN_AI_BATCH_SIZE = 1
MAX_AI_BATCH_SIZE = 500
DEFAULT_AI_CONCURRENCY = 2
MIN_AI_CONCURRENCY = 1
MAX_AI_CONCURRENCY = 16

#: `japanese` prefers `JapaneseTitle` and falls back to `Title`; `english` is the
#: reverse. Two values rather than a free-form field name: these are the only two
#: titles a gallery has, and a setting naming an arbitrary metadata field would
#: let `{title}` resolve to a rating.
TITLE_SOURCE_JAPANESE = "japanese"
TITLE_SOURCE_ENGLISH = "english"
TITLE_SOURCES: tuple[str, ...] = (TITLE_SOURCE_JAPANESE, TITLE_SOURCE_ENGLISH)
DEFAULT_TITLE_SOURCE = TITLE_SOURCE_JAPANESE

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

    async def title_source(self) -> str:
        """Which language `{title}` prefers, defaulting to Japanese.

        Read tolerantly like every other setting here: an unrecognised stored
        value falls back rather than raising, because this is called from inside
        a packing job where a refusal would leave the book unpublished over a
        preference.
        """
        stored = await self._database.archive_settings()
        value = stored.get(SETTING_TITLE_SOURCE, "").strip().lower()
        return value if value in TITLE_SOURCES else DEFAULT_TITLE_SOURCE

    async def save_title_source(self, raw: str) -> str:
        """Store the title preference, refusing anything outside the two."""
        value = (raw or "").strip().lower()
        if not value:
            await self._database.save_archive_settings(
                {SETTING_TITLE_SOURCE: ""}
            )
            return DEFAULT_TITLE_SOURCE
        if value not in TITLE_SOURCES:
            raise ArchiveSettingsError(
                "TITLE_SOURCE_INVALID", "标题来源只能是日文标题或英文标题"
            )
        await self._database.save_archive_settings(
            {SETTING_TITLE_SOURCE: value}
        )
        return value

    # ------------------------------------------------------------------
    #  路径来源：模板与规则，或 AI（二者互斥）
    # ------------------------------------------------------------------
    async def path_source(self) -> str:
        """Which layer decides a book's path. Defaults to the template."""
        stored = await self._database.archive_settings()
        value = (stored.get(SETTING_PATH_SOURCE) or "").strip().lower()
        return value if value in PATH_SOURCES else DEFAULT_PATH_SOURCE

    async def save_path_source(self, raw: str) -> str:
        value = (raw or "").strip().lower()
        if value not in PATH_SOURCES:
            raise ArchiveSettingsError(
                "PATH_SOURCE_INVALID", "归档路径来源取值无效"
            )
        await self._database.save_archive_settings(
            {SETTING_PATH_SOURCE: value}
        )
        return value

    async def ai_prompt(self) -> str:
        """The stored prompt, or the reference one.

        Blank means the default rather than an empty prompt: the page's
        「恢复默认」 writes blank, and an operator who clears the box has asked
        for the reference text back, not for a request whose system message is
        empty. Read without validating -- the text is free-form by design, and a
        prompt saved by an older version still has to reach the page for editing.
        """
        stored = await self._database.archive_settings()
        text = stored.get(SETTING_AI_PROMPT)
        if text is None:
            return DEFAULT_AI_PROMPT
        return text.strip() or DEFAULT_AI_PROMPT

    async def save_ai_prompt(self, raw: str) -> str:
        text = str(raw or "").strip()
        await self._database.save_archive_settings(
            {SETTING_AI_PROMPT: text}
        )
        return text or DEFAULT_AI_PROMPT

    async def ai_fallback_to_rules(self) -> bool:
        stored = await self._database.archive_settings()
        return stored.get(SETTING_AI_FALLBACK_TO_RULES, "0") not in {
            "0",
            "false",
            "no",
        }

    async def save_ai_fallback_to_rules(self, enabled: bool) -> None:
        await self._database.save_archive_settings(
            {SETTING_AI_FALLBACK_TO_RULES: "1" if enabled else "0"}
        )

    async def ai_batch_size(self) -> int:
        return await self._int_setting(
            SETTING_AI_BATCH_SIZE, DEFAULT_AI_BATCH_SIZE
        )

    async def save_ai_batch_size(self, raw: object) -> int:
        return await self._save_int_setting(
            raw,
            key=SETTING_AI_BATCH_SIZE,
            code="AI_BATCH_SIZE_INVALID",
            label="每批处理数量",
            minimum=MIN_AI_BATCH_SIZE,
            maximum=MAX_AI_BATCH_SIZE,
        )

    async def ai_concurrency(self) -> int:
        return await self._int_setting(
            SETTING_AI_CONCURRENCY, DEFAULT_AI_CONCURRENCY
        )

    async def save_ai_concurrency(self, raw: object) -> int:
        return await self._save_int_setting(
            raw,
            key=SETTING_AI_CONCURRENCY,
            code="AI_CONCURRENCY_INVALID",
            label="并发数",
            minimum=MIN_AI_CONCURRENCY,
            maximum=MAX_AI_CONCURRENCY,
        )

    async def ai_model_source(self) -> str:
        stored = await self._database.archive_settings()
        value = (stored.get(SETTING_AI_MODEL_SOURCE) or "").strip().lower()
        return value if value in MODEL_SOURCES else DEFAULT_AI_MODEL_SOURCE

    async def save_ai_model_source(self, raw: str) -> str:
        value = (raw or "").strip().lower()
        if value not in MODEL_SOURCES:
            raise ArchiveSettingsError(
                "AI_MODEL_SOURCE_INVALID", "模型来源取值无效"
            )
        await self._database.save_archive_settings(
            {SETTING_AI_MODEL_SOURCE: value}
        )
        return value

    async def ai_stream(self) -> bool:
        stored = await self._database.archive_settings()
        return stored.get(SETTING_AI_STREAM, "0") not in {"0", "false", "no"}

    async def save_ai_stream(self, enabled: bool) -> None:
        await self._database.save_archive_settings(
            {SETTING_AI_STREAM: "1" if enabled else "0"}
        )

    async def ai_default_include_current(self) -> bool:
        stored = await self._database.archive_settings()
        return stored.get(SETTING_AI_DEFAULT_INCLUDE_CURRENT, "0") not in {
            "0",
            "false",
            "no",
        }

    async def save_ai_default_include_current(self, enabled: bool) -> None:
        await self._database.save_archive_settings(
            {SETTING_AI_DEFAULT_INCLUDE_CURRENT: "1" if enabled else "0"}
        )

    async def _int_setting(self, key: str, default: int) -> int:
        stored = await self._database.archive_settings()
        try:
            return int(str(stored.get(key, "")).strip())
        except (TypeError, ValueError):
            return default

    async def _save_int_setting(
        self,
        raw: object,
        *,
        key: str,
        code: str,
        label: str,
        minimum: int,
        maximum: int,
    ) -> int:
        """Store one whole number, refusing rather than silently clamping.

        A clamped value is a setting the operator did not choose and the page
        would show back to them as if they had. The bounds are in the message so
        the next attempt is a correction rather than a guess.
        """
        text = str(raw or "").strip()
        try:
            value = int(text)
        except ValueError as exc:
            raise ArchiveSettingsError(
                code, f"{label}必须是整数（{minimum}–{maximum}）"
            ) from exc
        if not minimum <= value <= maximum:
            raise ArchiveSettingsError(
                code, f"{label}必须在 {minimum} 到 {maximum} 之间"
            )
        await self._database.save_archive_settings({key: str(value)})
        return value

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

    async def library_template_for(
        self, candidate_id: int
    ) -> tuple[str, ArchivePathRule | None]:
        """The layout template this work's path uses.

        The first enabled routing rule whose condition matches the work's
        effective metadata wins; a work no rule matches keeps the global
        `library_template`. Rules evaluate on `effective_metadata` rather than
        the row list a packing job already holds, because only that query
        applies the source-precedence ordering -- a rule must see exactly what
        auto-approval sees, or the two engines would disagree about a gallery
        with several metadata sources.

        Read tolerantly in the same spirit as `library_template`: a rule whose
        stored condition has gone stale is skipped and logged rather than
        failing the job. A work with no metadata rows matches nothing, which is
        deliberate -- rules describe enriched books, and an unenriched one keeps
        the default path.
        """
        metadata = await self._database.effective_metadata(candidate_id)
        for rule in await self._database.list_archive_path_rules(
            enabled_only=True
        ):
            try:
                matched = evaluate_rule(
                    rule.condition,
                    metadata,
                    case_sensitive=rule.case_sensitive,
                ).matched
            except (RuleValidationError, TypeError, KeyError, ValueError):
                # `RuleValidationError` is the engine's documented failure; the
                # other three are what an arbitrarily corrupt `condition_json`
                # can still produce (a null value in a comparison, a node with
                # no `field`). Every one of them means "this rule cannot decide
                # a path", and deciding a path is never worth failing the pack,
                # so the rule is skipped and logged the way a stale global
                # template falls back.
                logging.getLogger(__name__).warning(
                    "archive_path_rule_unusable",
                    extra={
                        "error_code": "RULE_INVALID",
                        "rule_id": rule.rule_id,
                    },
                )
                continue
            if matched:
                return rule.path_template, rule
        return await self.library_template(), None

    async def save_path_rule(
        self,
        *,
        rule_id: int | None,
        name: str,
        enabled: bool,
        priority: int,
        condition: dict,
        path_template: str,
        case_sensitive: bool = False,
    ) -> ArchivePathRule:
        """Store a routing rule, refusing a template that cannot render safely.

        The template is validated here rather than at packing time for the same
        reason the global one is: validation happens hours earlier, with the
        operator watching the page. The condition has already passed
        `validate_rule_ast` in the route -- the auto-approval editor's gate is
        shared through `parse_rule_condition`. The stored `dsl_snapshot` is
        computed from the validated AST, so it is the engine's own rendering and
        nothing the page typed.
        """
        dsl_snapshot = render_rule_dsl(condition)
        text = (path_template or "").strip()
        if not text:
            raise ArchiveSettingsError(
                "PATH_RULE_TEMPLATE_EMPTY", "路径模板不能为空"
            )
        try:
            cleaned = validate_library_template(text)
        except LibraryTemplateError as exc:
            raise ArchiveSettingsError(exc.code, exc.public_message) from exc
        return await self._database.save_archive_path_rule(
            rule_id=rule_id,
            name=name,
            enabled=enabled,
            priority=priority,
            condition=condition,
            dsl_snapshot=dsl_snapshot,
            path_template=cleaned,
            case_sensitive=case_sensitive,
        )

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
    "DEFAULT_AI_BATCH_SIZE",
    "DEFAULT_AI_CONCURRENCY",
    "DEFAULT_PATH_SOURCE",
    "MAX_AI_BATCH_SIZE",
    "MAX_AI_CONCURRENCY",
    "MIN_AI_BATCH_SIZE",
    "MIN_AI_CONCURRENCY",
    "PATH_SETTING_KEYS",
    "PATH_SOURCES",
    "PATH_SOURCE_AI",
    "PATH_SOURCE_TEMPLATE",
    "SETTING_AI_BATCH_SIZE",
    "SETTING_AI_CONCURRENCY",
    "SETTING_AI_DEFAULT_INCLUDE_CURRENT",
    "SETTING_AI_FALLBACK_TO_RULES",
    "SETTING_AI_PROMPT",
    "SETTING_AI_STREAM",
    "SETTING_PATH_SOURCE",
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