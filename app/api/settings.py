"""One snapshot per settings section.

`/settings/{section}` and `GET /api/v1/settings/{section}` render the same eight
sections, so the assembly happens once here and both layers read the result. The
page template gets a dict; the endpoint returns the same dict as JSON. Anything
computed in a template would be invisible to the endpoint, and anything computed
in the endpoint alone would leave the page with a second, drifting version.

Nothing in these payloads is a secret. The bot token, the ExHentai cookies, the
qBittorrent password and every archive password stay in the credential store: a
section reports *whether* a credential is configured, never what it is. The
services already draw that line -- `torrent_client_view` omits the password,
`ArchivePasswordEntry` carries no plaintext -- and the builders below are written
to stay on the same side of it, because a settings page is exactly where a
careless field would leak one.

A section is assembled defensively. The connection manager only exists after
lifespan startup and the torrent service is optional by configuration, so a
missing piece degrades its own section rather than failing the request: an
operator who has not configured seeding still needs to reach the archive tab.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any, Callable, Coroutine

from fastapi import APIRouter, Request

from app.api import deps
from app.api.contracts import ApiError
from app.connections.exhentai import ExHentaiCredentials
from app.connections.models import ProviderConnectionError
from app.logging import apply_runtime_log_level
from app.credentials import KIND_API_KEY, hash_secret, new_token
from app.settings import runtime
from app.settings import auto_approval as approval_form
from app.settings import archive as archive_form
from app.settings import ai as ai_form
from app.ai.errors import AiError
from app.settings.forms import checkbox
from app.settings import parse as parse_form
from app.settings import paths as paths_form
from app.settings import sources as source_form
from app.archive.service import ArchiveSettingsError
from app.torrent.models import TorrentError
from app.settings.service import SystemSettingsError
from app.api.serializers import (
    ai_chain_entry,
    ai_provider,
    ai_provider_key,
    ai_provider_model,
    ai_selectable_model,
    api_key_view,
    auto_approval_dry_run,
    log_entry_payload,
    archive_password,
    archive_path_rule,
    auto_approval_rule,
    connection_snapshot,
    safety_limits,
    telegram_source,
    tool_profile,
)
from app.api.status import (
    PROVIDER_STATUS,
    SETTINGS_AI,
    LOG_LEVELS,
    SETTINGS_ARCHIVE,
    SETTINGS_AUTO_APPROVAL,
    SETTINGS_CONNECTIONS,
    SETTINGS_PASSWORDS,
    SETTINGS_PATHS,
    SETTINGS_PARSE,
    SETTINGS_SECTIONS,
    SETTINGS_SOURCES,
    SETTINGS_SYSTEM,
    log_level_view,
    dependency_view,
    settings_section_view,
)
from app.ai.prompt import DEFAULT_AI_PROMPT, DEFAULT_CANDIDATE_PROMPT
from app.candidates.parse_rules import (
    ARCHIVE_FORMATS as PARSE_ARCHIVE_FORMATS,
    DEFAULT_PARSE_RULES,
)
from app.downloads.models import AUTO_DOWNLOAD_PROVIDERS
from app.archive.service import (
    MAX_AI_BATCH_SIZE,
    MAX_AI_CONCURRENCY,
    MIN_AI_BATCH_SIZE,
    MIN_AI_CONCURRENCY,
    PATH_SOURCE_AI,
    PATH_SOURCE_TEMPLATE,
    TITLE_SOURCE_ENGLISH,
    TITLE_SOURCE_JAPANESE,
)
from app.auto_approval.rules import (
    ALL_OPERATORS,
    ALLOWED_FIELDS,
    COLLECTION_OPERATORS,
    EXISTENCE_OPS,
    NUMERIC_OPERATORS,
    RuleValidationError,
    TEXT_OPERATORS,
)
from app.auto_approval.service import DRY_RUN_SCAN_LIMIT, AutomaticApprovalService
from app.ai.models import (
    CHAIN_SCOPE_ARCHIVE_PATH,
    CHAIN_SCOPE_CANDIDATE,
    CHAIN_SCOPE_DEFAULT,
    DEFAULT_MAX_RETRIES,
    DEFAULT_TIMEOUT_SECONDS,
    KEY_COOLDOWN_MINUTES,
    MAX_RETRIES,
    MAX_TIMEOUT_SECONDS,
    MIN_TIMEOUT_SECONDS,
    MODEL_SOURCE_CUSTOM,
    MODEL_SOURCE_DEFAULT,
    PROVIDER_CODE_LABELS,
    SUPPORTED_PROVIDER_CODES,
)
from app.conversion.naming import (
    DEFAULT_LIBRARY_TEMPLATE,
    PLACEHOLDER_LABELS,
    TEMPLATE_PLACEHOLDERS,
    detect_library_limits,
)
from app.logs.reader import MAX_LIMIT, clamp_limit, read_log_tail
from app.review.models import field_label
from app.credentials import MAX_TOKEN_TTL_SECONDS, MIN_TOKEN_TTL_SECONDS
from app.settings.service import (
    AI_CANDIDATE_FALLBACKS,
    MAX_AUTO_APPROVAL_INTERVAL_MINUTES,
    MAX_POLL_INTERVAL_MS,
    MAX_SOURCE_CONCURRENCY,
    MIN_AUTO_APPROVAL_INTERVAL_MINUTES,
    MIN_POLL_INTERVAL_MS,
    MIN_SOURCE_CONCURRENCY,
)


router = APIRouter(tags=["settings"])

#: Archive formats a Telegram source may accept. Ordered smallest-ecosystem-first
#: the way the form has always listed them, so the checkbox order does not change
#: under an operator who knows the page.
SOURCE_ARCHIVE_FORMATS: tuple[str, ...] = ("zip", "rar", "7z", "cbz")

#: The two source kinds and the sign their chat id must carry. Telegram gives a
#: channel a negative id and a private chat a positive one, which is the only
#: check that can be made before the bot has ever seen the chat.
SOURCE_TYPES: tuple[dict[str, Any], ...] = (
    {"code": "CHANNEL", "label": "频道", "chat_id_sign": -1},
    {"code": "PRIVATE_CHAT", "label": "私聊", "chat_id_sign": 1},
)

#: The chat kinds the MTProto dialog list reports, and the whitelist entry each
#: one becomes. A group and a channel are both a negative chat id and both are
#: stored as `CHANNEL` -- the sign is the whole of what that source type means
#: -- but the picker names them apart, so an operator does not read their own
#: group back as a 频道. `source_type` is what the saved row actually carries.
DIALOG_KINDS: tuple[dict[str, Any], ...] = (
    {"code": "CHANNEL", "label": "频道", "source_type": "CHANNEL"},
    {"code": "GROUP", "label": "群组", "source_type": "CHANNEL"},
    {"code": "PRIVATE_CHAT", "label": "私聊", "source_type": "PRIVATE_CHAT"},
)


def section_tabs(active: str) -> list[dict[str, Any]]:
    """The tab strip, in order, with exactly one marked current.

    Built from `SETTINGS_SECTIONS` rather than written in the template so a new
    section appears in the strip, the nav and the endpoint at once. The shape is
    `{key, label, href}` -- what `ui.tabs` already takes for the candidate and
    activity strips -- plus `current`, so a JSON client knows which one is open
    without re-deriving it from the URL. No `tone`: a section is a place, and a
    tab strip that coloured one would be claiming a state it does not have.
    """
    return [
        {
            "key": code,
            "label": settings_section_view(code).label,
            "href": f"/settings/{code}",
            "current": code == active,
        }
        for code in SETTINGS_SECTIONS
    ]


async def _connections_section(request: Request) -> dict[str, Any]:
    manager = deps.optional_service(request, "connection_manager")
    return {
        # None rather than an empty snapshot when the manager is absent: the page
        # says 「尚未就绪」 for that, which is true, where zeroed health would be a
        # claim that both providers are disconnected.
        "connections": (
            connection_snapshot(manager.snapshot()) if manager is not None else None
        ),
    }


async def _sources_section(request: Request) -> dict[str, Any]:
    database = deps.database(request)
    return {
        "sources": [
            telegram_source(source)
            for source in await database.list_telegram_sources()
        ],
        "source_types": [dict(entry) for entry in SOURCE_TYPES],
        "dialog_kinds": [dict(entry) for entry in DIALOG_KINDS],
        "archive_formats": list(SOURCE_ARCHIVE_FORMATS),
    }


async def _chain_editor(ai_service: Any, scope: str) -> dict[str, Any]:
    """Everything one scope's 「主力 + 备用」 editor needs, in one shape.

    The global default, the archive-path override and the candidate-gate
    override all render `settings/_model_chain.html`, so they must all be handed
    the same three things: the *effective* list (what a call will actually use),
    the scope's *own* list (what the editor writes to -- an empty custom list has
    to look empty, not inherited), and every selectable model with its
    `in_chain` flag. Factoring it here is what keeps a fourth scope from
    re-deriving the payload slightly differently.
    """
    effective = await ai_service.effective_chain(scope)
    own = await ai_service.chain(scope)
    in_chain = {entry.model.model_id for entry in own}
    selectable: list[dict[str, Any]] = []
    for provider in await ai_service.providers():
        for model in await ai_service.models(provider.provider_id):
            selectable.append(
                ai_selectable_model(
                    provider, model, in_chain=model.model_id in in_chain
                )
            )
    return {
        "chain": [ai_chain_entry(entry) for entry in effective],
        "own_chain": [ai_chain_entry(entry) for entry in own],
        "selectable_models": selectable,
    }


def _model_source_choices(custom_hint: str) -> list[dict[str, str]]:
    """The 「跟随全局默认 / 本页单独指定」 radio pair, worded per feature."""
    return [
        {
            "code": MODEL_SOURCE_DEFAULT,
            "label": "跟随全局默认",
            "hint": "使用「设置 → AI 供应商」页的全局默认模型。",
        },
        {
            "code": MODEL_SOURCE_CUSTOM,
            "label": "本页单独指定",
            "hint": custom_hint,
        },
    ]


async def _parse_section(request: Request) -> dict[str, Any]:
    """The candidate-admission scheme and the AI gate over it.

    One section for both because they are one pipeline: the AI gate runs in
    front of the parse rules and may be told to override them, and an operator
    tuning 「什么消息能进候选」 has to see the two together or they will set one
    and wonder why the other still fires. Nothing here is a secret.
    """
    settings = deps.system_settings_service(request)
    admission = await settings.candidate_admission()
    ai_service = deps.optional_service(request, "ai_service")
    candidate_ai = (
        await _chain_editor(ai_service, CHAIN_SCOPE_CANDIDATE)
        if ai_service is not None
        else {"chain": [], "own_chain": [], "selectable_models": []}
    )
    return {
        "parse_rules": await settings.parse_rules(),
        "parse_defaults": DEFAULT_PARSE_RULES,
        "archive_formats": list(PARSE_ARCHIVE_FORMATS),
        "candidate_admission": admission,
        "candidate_prompt_default": DEFAULT_CANDIDATE_PROMPT,
        "candidate_fallbacks": list(AI_CANDIDATE_FALLBACKS),
        "ai_enabled": await settings.ai_enabled(),
        # The gate's own 「主力 + 备用」 list, or its inheritance of the global
        # one -- the same editor and the same vocabulary as the archive-path
        # page, just mounted on this tab.
        "candidate_ai": {
            "model_source": admission["model_source"],
            "model_sources": _model_source_choices(
                "候选判定用下面这张列表，与全局默认互不影响。"
            ),
            **candidate_ai,
        },
    }


def _condition_vocabulary() -> dict[str, object]:
    """Field and operator lists every rule editor renders.

    Built from the engine's own tables, so a field the evaluator does not
    support can never be offered. Sorted because a frozenset has no order and
    an editor whose list reshuffles between requests is unusable.
    """
    return {
        "fields": [
            {"code": field, "label": field_label(field)}
            for field in sorted(ALLOWED_FIELDS)
        ],
        "operators": sorted(ALL_OPERATORS),
        "text_operators": sorted(TEXT_OPERATORS),
        "numeric_operators": sorted(NUMERIC_OPERATORS),
        "collection_operators": sorted(COLLECTION_OPERATORS),
        "existence_operators": sorted(EXISTENCE_OPS),
    }


async def _auto_approval_section(request: Request) -> dict[str, Any]:
    database = deps.database(request)
    return {
        "rules": [
            auto_approval_rule(rule)
            for rule in await database.list_auto_approval_rules()
        ],
        "vocabulary": _condition_vocabulary(),
        # How far a trial run reads, so the page can say what 「命中 3」 is out of
        # before the operator asks.
        "dry_run_scan_limit": DRY_RUN_SCAN_LIMIT,
    }


async def _archive_section(request: Request) -> dict[str, Any]:
    service = deps.archive_settings_service(request)
    # Both come back as plain dicts from the service, and both get one derived
    # key: whether the thing is usable, resolved here rather than in the template
    # so 「未就绪」 is a word the vocabulary owns. `available` and `configured` are
    # the services' own field names and are left alone.
    toolchain = dict(await service.toolchain_status())
    toolchain["readiness"] = dependency_view(toolchain.get("available")).to_payload()
    torrent = dict(await service.torrent_client_view())
    torrent["readiness"] = dependency_view(torrent.get("configured")).to_payload()
    return {
        "limits": safety_limits(await service.limits()),
        "keep_original": await service.keep_original(),
        # Carries its own level list with labels and the current selection, so
        # the form's radio set is built from one value.
        "image_quality": await service.image_quality_view(),
        "profiles": [
            tool_profile(profile) for profile in await service.profiles()
        ],
        "toolchain": toolchain,
        "torrent": torrent,
        "torrent_enabled": deps.optional_service(request, "torrent_service")
        is not None,
        "auto_pack_after_download": await service.auto_pack_after_download(),
    }


async def _paths_section(request: Request) -> dict[str, Any]:
    service = deps.archive_settings_service(request)
    database = deps.database(request)
    app_settings = request.app.state.settings
    ai_enabled = await deps.system_settings_service(request).ai_enabled()
    library_path = await service.library_path() or app_settings.library_path
    limits = detect_library_limits(library_path)
    ai_service = deps.optional_service(request, "ai_service")
    chain = (
        await ai_service.effective_chain(CHAIN_SCOPE_ARCHIVE_PATH)
        if ai_service is not None
        else ()
    )
    path_chain = (
        await ai_service.chain(CHAIN_SCOPE_ARCHIVE_PATH)
        if ai_service is not None
        else ()
    )
    ai_selectable: list[dict[str, Any]] = []
    if ai_service is not None:
        in_chain = {entry.model.model_id for entry in path_chain}
        for provider in await ai_service.providers():
            for model in await ai_service.models(provider.provider_id):
                ai_selectable.append(
                    ai_selectable_model(
                        provider, model, in_chain=model.model_id in in_chain
                    )
                )
    return {
        "path_rules": [
            archive_path_rule(rule)
            for rule in await database.list_archive_path_rules()
        ],
        # The same field/operator vocabulary the auto-approval tab offers: the
        # routing editor is the same engine with one extra answer.
        "vocabulary": _condition_vocabulary(),
        "dry_run_scan_limit": DRY_RUN_SCAN_LIMIT,
        "paths": await service.paths(),
        "default_paths": {
            "library": str(app_settings.library_path),
            "work": str(app_settings.work_path),
        },
        "library_template": await service.library_template(),
        "title_source": await service.title_source(),
        # The two choices, with their words, so the radio group is generated from
        # the same table the validator accepts rather than hand-listed in Jinja.
        "title_sources": [
            {
                "code": TITLE_SOURCE_JAPANESE,
                "label": field_label("JapaneseTitle"),
                "hint": "优先使用画廊的 title_jpn，缺失时回退到英文标题。",
            },
            {
                "code": TITLE_SOURCE_ENGLISH,
                "label": field_label("Title"),
                "hint": "优先使用画廊的英文标题，缺失时回退到日文标题。",
            },
        ],
        # 路径来源 and the AI sub-panel. One payload rather than two so the
        # template can render the radio set, the prompt and the toggles from the
        # same values the validator will accept.
        "ai_enabled": ai_enabled,
        # 「自动添加系列」 only means anything with the master switch on and a
        # chain to ask (R83). The template greys the box out on this, and the
        # routes refuse enabling it when it is False.
        "ai_usable": ai_enabled and bool(chain),
        "path_source": await service.path_source(),
        "path_sources": [
            {
                "code": PATH_SOURCE_TEMPLATE,
                "label": "模板与规则（现状）",
                "hint": "用下方模板与按规则匹配的模板决定路径，不使用 AI。",
            },
            {
                "code": PATH_SOURCE_AI,
                "label": "AI 生成",
                "hint": "由「设置 → AI 供应商」里配置的模型链读元数据决定路径；下方的模板与规则不生效。",
            },
        ],
        "ai": {
            "prompt": await service.ai_prompt(),
            "default_prompt": DEFAULT_AI_PROMPT,
            "fallback_to_rules": await service.ai_fallback_to_rules(),
            "batch_size": await service.ai_batch_size(),
            "concurrency": await service.ai_concurrency(),
            "stream": await service.ai_stream(),
            "default_include_current": await service.ai_default_include_current(),
            "auto_series": await service.auto_series(),
            "cache_count": await database.ai_path_suggestion_count(),
            # Which models this feature asks: the global default, or a list of
            # its own. `chain` below is the *effective* list (what a pack will
            # actually use); `path_chain` is the override as configured, so the
            # editor can show an empty custom list without pretending the
            # inherited one is what is being edited.
            "model_source": await service.ai_model_source(),
            "model_sources": [
                {
                    "code": MODEL_SOURCE_DEFAULT,
                    "label": "跟随全局默认",
                    "hint": "使用「设置 → AI 供应商」页的全局默认模型。",
                },
                {
                    "code": MODEL_SOURCE_CUSTOM,
                    "label": "本页单独指定",
                    "hint": "路径决策用下面这张列表，与全局默认互不影响。",
                },
            ],
            "path_chain": [ai_chain_entry(entry) for entry in path_chain],
            "selectable_models": ai_selectable,
            # What the model chain currently holds, so the page can say whether
            # AI mode is ready instead of letting an operator switch it on and
            # discover 需干预 entries later.
            "chain": [
                {
                    "position": entry.position,
                    "label": f"{entry.provider.name} / {entry.model.name}",
                    "is_primary": entry.is_primary,
                }
                for entry in chain
            ],
            "bounds": {
                "batch_size": {
                    "minimum": MIN_AI_BATCH_SIZE,
                    "maximum": MAX_AI_BATCH_SIZE,
                },
                "concurrency": {
                    "minimum": MIN_AI_CONCURRENCY,
                    "maximum": MAX_AI_CONCURRENCY,
                },
            },
        },
        "template": {
            "default": DEFAULT_LIBRARY_TEMPLATE,
            # Not a policy number: what the filesystem under the library root
            # says it takes, in bytes, so the hint under the box states the same
            # ceiling the validator applies (R35).
            "limits": {
                "name_max_bytes": limits.name_max,
                "relative_max_bytes": limits.relative_max,
                "source": str(library_path),
            },
            "placeholders": [
                {
                    "code": name,
                    "label": PLACEHOLDER_LABELS[name],
                    "token": "{" + name + "}",
                }
                for name in TEMPLATE_PLACEHOLDERS
            ],
        },
    }


async def _ai_section(request: Request) -> dict[str, Any]:
    """The AI tab: the providers, their models, and the global default chain.

    AstrBot's layout, in one server-rendered page: a list of providers on the
    left, the selected provider's settings/keys/models on the right, and the
    global default model below. 「哪个页面用哪个模型」 is not answered here --
    this page owns the default, and a feature that wants its own list (the
    archive-path page) says so on its own tab.
    """
    service = deps.ai_service(request)
    providers = await service.providers()
    catalogue: list[dict[str, Any]] = []
    selectable: list[dict[str, Any]] = []
    for provider in providers:
        keys = await service.keys(provider.provider_id)
        usable_keys = await service.keys(provider.provider_id, usable_only=True)
        usable_ids = {key.key_id for key in usable_keys}
        models = await service.models(provider.provider_id)
        catalogue.append(
            {
                **ai_provider(provider),
                # `api_keys`, not `keys`: Jinja resolves `.keys` on a dict to
                # the dict's own method before it looks for the key, so
                # `provider.keys` in a template would render a builtin.
                "api_keys": [
                    ai_provider_key(key, usable=key.key_id in usable_ids)
                    for key in keys
                ],
                "models": [ai_provider_model(model) for model in models],
                "key_count": len(keys),
                "usable_key_count": len(usable_ids),
                "model_count": len(models),
                "enabled_model_count": sum(1 for model in models if model.enabled),
            }
        )
        selectable.extend(
            ai_selectable_model(provider, model, in_chain=False)
            for model in models
        )

    selected_id = _selected_provider_id(request, catalogue)
    selected = next(
        (entry for entry in catalogue if entry["provider_id"] == selected_id),
        catalogue[0] if catalogue else None,
    )
    default_chain = [
        ai_chain_entry(entry) for entry in await service.chain(CHAIN_SCOPE_DEFAULT)
    ]
    chosen = {entry["model_id"] for entry in default_chain}
    for entry in selectable:
        entry["in_chain"] = entry["model_id"] in chosen
        entry["selected"] = (
            selected is not None and entry["provider_id"] == selected["provider_id"]
        )
    return {
        "ai_enabled": await deps.system_settings_service(request).ai_enabled(),
        "providers": catalogue,
        "selected_provider": selected,
        # Every model of every provider, for the 「加为主力/备用」 pickers. The
        # template filters; it never has to ask a second question of the
        # database to render a dropdown.
        "selectable_models": selectable,
        "default_chain": default_chain,
        "default_chain_ids": sorted(chosen),
        "provider_codes": [
            {"code": code, "label": PROVIDER_CODE_LABELS.get(code, code)}
            for code in SUPPORTED_PROVIDER_CODES
        ],
        "defaults": {
            "code": SUPPORTED_PROVIDER_CODES[0],
            "timeout_seconds": DEFAULT_TIMEOUT_SECONDS,
            "max_retries": DEFAULT_MAX_RETRIES,
        },
        "bounds": {
            "timeout_seconds": {
                "minimum": MIN_TIMEOUT_SECONDS,
                "maximum": MAX_TIMEOUT_SECONDS,
            },
            "max_retries": {"minimum": 0, "maximum": MAX_RETRIES},
        },
        "key_cooldown_minutes": KEY_COOLDOWN_MINUTES,
    }


def _selected_provider_id(request: Request, catalogue: list[dict[str, Any]]) -> int:
    """Which provider the right pane shows: `?provider=` if it names one.

    Falling back to the first row rather than to an empty pane: after 「新增供应
    商」 the operator wants to see what they just created, and a page that
    rendered nothing until a second click would look like the save failed.
    """
    raw = request.query_params.get("provider")
    if raw and str(raw).isdigit():
        wanted = int(raw)
        if any(entry["provider_id"] == wanted for entry in catalogue):
            return wanted
    return catalogue[0]["provider_id"] if catalogue else 0


async def _passwords_section(request: Request) -> dict[str, Any]:
    service = deps.archive_settings_service(request)
    return {
        "passwords": [
            archive_password(entry) for entry in await service.passwords()
        ],
        # The single mobile API key, metadata only: the plaintext is shown once
        # by the page that mints it and is never read back.
        "api_key": api_key_view(await deps.database(request).active_api_key()),
        # The login password lives on this tab too, so the section reports the
        # one fact a form needs about it: whether the initial password is still
        # in place. The hash is never read here.
        "must_change_password": bool(
            request.session.get("must_change_password")
        ),
    }


async def _system_section(request: Request) -> dict[str, Any]:
    service = deps.system_settings_service(request)
    system = await service.snapshot()
    return {
        "system": system,
        # One row per source the router may use, with the rank it currently
        # holds. Rendered from the service's own order rather than from a list
        # written in the template, so a source added to the vocabulary shows up
        # here without a second edit -- and `PROVIDER_STATUS` supplies the label
        # so the page cannot drift from what the queue calls the same provider.
        "download_sources": [
            {
                "code": code,
                "label": PROVIDER_STATUS[code].label,
                "rank": index + 1,
            }
            for index, code in enumerate(system["download_source_priority"])
        ],
        "download_source_ranks": list(range(1, len(AUTO_DOWNLOAD_PROVIDERS) + 1)),
        "bounds": {
            "poll_interval_ms": {
                "minimum": MIN_POLL_INTERVAL_MS,
                "maximum": MAX_POLL_INTERVAL_MS,
            },
            "source_concurrency": {
                "minimum": MIN_SOURCE_CONCURRENCY,
                "maximum": MAX_SOURCE_CONCURRENCY,
            },
            "auto_approval_interval_minutes": {
                "minimum": MIN_AUTO_APPROVAL_INTERVAL_MINUTES,
                "maximum": MAX_AUTO_APPROVAL_INTERVAL_MINUTES,
            },
            "mobile_access_ttl_seconds": {
                "minimum": MIN_TOKEN_TTL_SECONDS,
                "maximum": MAX_TOKEN_TTL_SECONDS,
            },
            "mobile_refresh_ttl_seconds": {
                "minimum": MIN_TOKEN_TTL_SECONDS,
                "maximum": MAX_TOKEN_TTL_SECONDS,
            },
        },
        **await _log_view(request, configured_level=str(system["log_level"])),
    }


async def _log_view(request: Request, *, configured_level: str) -> dict[str, Any]:
    """The log tail for the 系统 tab, read off disk on demand.

    Read here rather than in the page route so the JSON endpoint and the render
    cannot show different logs -- the same rule every other section follows. The
    file read is pushed to a thread because this is an async handler and the tail
    of a rotating file is real disk I/O.
    """
    settings = request.app.state.settings
    level = (
        request.query_params.get("log_level") or configured_level
    ).strip().upper()
    if level is not None and level not in LOG_LEVELS:
        # An unknown level is dropped rather than refused: unlike a settings
        # section this arrives from a filter link, and showing everything is a
        # safe answer where a 404 on the whole tab is not.
        level = configured_level
    limit = clamp_limit(request.query_params.get("log_limit"))
    entries, present = await asyncio.to_thread(
        read_log_tail, settings.log_dir, limit=limit, min_level=level
    )
    return {
        "logs": {
            "entries": [log_entry_payload(entry) for entry in entries],
            "file_present": present,
            "enabled": settings.log_to_file,
            "level": level,
            "levels": [log_level_view(name).to_payload() for name in LOG_LEVELS],
            "filters": _log_filters(request, level),
            "limit": limit,
            "max_limit": MAX_LIMIT,
            "configured_level": configured_level,
            "access_log": configured_level == "DEBUG",
            "retention_days": settings.log_file_backups,
        }
    }


def _log_filters(request: Request, active: str | None) -> list[dict[str, Any]]:
    """The level filter as links, merged into the current URL.

    Built here rather than in the template because the page and the JSON body
    read one builder, and because merging a parameter is the thing a template
    doing it by hand gets wrong -- the first one to forget `log_limit` silently
    resets the line count.
    """
    choices: list[dict[str, Any]] = [
        {
            "code": "",
            "label": "全部",
            "selected": active is None,
            "href": _log_href(request, ""),
        }
    ]
    for name in LOG_LEVELS:
        view = log_level_view(name)
        choices.append(
            {
                "code": view.code,
                "label": view.label,
                "selected": active == view.code,
                "href": _log_href(request, view.code),
            }
        )
    return choices


def _log_href(request: Request, level: str) -> str:
    url = request.url.include_query_params(log_level=level)
    return f"{url.path}?{url.query}" if url.query else url.path


_SECTION_BUILDERS: dict[
    str, Callable[[Request], Coroutine[Any, Any, dict[str, Any]]]
] = {
    SETTINGS_CONNECTIONS: _connections_section,
    SETTINGS_SOURCES: _sources_section,
    SETTINGS_PARSE: _parse_section,
    SETTINGS_AUTO_APPROVAL: _auto_approval_section,
    SETTINGS_ARCHIVE: _archive_section,
    SETTINGS_PATHS: _paths_section,
    SETTINGS_AI: _ai_section,
    SETTINGS_PASSWORDS: _passwords_section,
    SETTINGS_SYSTEM: _system_section,
}


async def settings_snapshot(request: Request, section: str) -> dict[str, Any]:
    """Everything one settings tab renders, tab strip included.

    Raises `KeyError` for a section that does not exist -- `settings_section_view`
    deliberately does not fall back, because answering `/settings/nonsense` with
    a page would invent a tab. Each caller turns that into its own 404.
    """
    view = settings_section_view(section)
    body = await _SECTION_BUILDERS[view.code](request)
    return {
        "section": view.to_payload(),
        "tabs": section_tabs(view.code),
        **body,
    }


@router.get("/settings")
async def list_settings_sections(request: Request) -> dict:
    """The sections and where they live -- no settings values.

    Declared above `/settings/{section}` so the literal path wins the match.
    """
    await deps.require_session(request)
    return {"tabs": section_tabs("")}


#: The connection actions the JSON layer accepts, one word each. A table would
#: not fit (the arguments differ per action), so the check is a set plus a chain
#: -- the same「unknown word is one 400」shape `app/api/actions.py` uses, and the
#: allowed list is built from this set so the two cannot drift.
CONNECTION_ACTIONS: frozenset[str] = frozenset(
    {
        "telegram",
        "telegram-disconnect",
        "telegram-user-login",
        "telegram-user-verify",
        "telegram-user-disconnect",
        "exhentai",
        "exhentai-disconnect",
    }
)


async def _json_body(request: Request) -> dict:
    """The JSON object a write expects, or a 400 naming what arrived."""
    try:
        payload = await request.json()
    except ValueError as exc:
        raise ApiError("BODY_INVALID", "请求体不是合法 JSON") from exc
    if not isinstance(payload, dict):
        raise ApiError("BODY_INVALID", "请求体必须是对象")
    return payload


def _required_text(payload: dict, field: str, label: str) -> str:
    value = str(payload.get(field) or "").strip()
    if not value:
        raise ApiError(
            f"{field.upper()}_REQUIRED",
            f"请填写{label}",
            details={"field": field},
        )
    return value


@router.put("/settings/connections")
async def put_connections(request: Request) -> dict:
    """Configure or drop an external connection (the 连接 tab, over JSON).

    The mobile client cannot reach `/connections/*`: those routes want a session
    cookie plus a `csrf_token` form field and answer with a 302. This runs the
    same `ConnectionManager` calls, so a credential the page would refuse cannot
    be stored from here.

    A credential entered here goes straight into the credential store. The
    response is the section snapshot, which reports *whether* a credential is
    configured and never what it is -- the line the section builders already
    hold, and the reason this returns the snapshot rather than echoing the body.
    """
    await deps.require_session(request)
    deps.require_csrf(request)
    payload = await _json_body(request)
    action = str(payload.get("action") or "")
    if action not in CONNECTION_ACTIONS:
        raise ApiError(
            "ACTION_UNKNOWN",
            f"未知的连接动作：{action}",
            details={"allowed": sorted(CONNECTION_ACTIONS)},
        )
    manager = deps.connection_manager(request)
    try:
        if action == "telegram":
            await manager.configure_telegram(
                _required_text(payload, "bot_token", "Bot Token")
            )
        elif action == "telegram-disconnect":
            await manager.disconnect_telegram()
        elif action == "telegram-user-login":
            await manager.start_telegram_user_login(
                _required_text(payload, "api_id", "API ID"),
                _required_text(payload, "api_hash", "API Hash"),
                _required_text(payload, "phone", "手机号"),
            )
        elif action == "telegram-user-verify":
            code = str(payload.get("code") or "").strip()
            password = str(payload.get("password") or "")
            # Which one is required depends on how far the login got, so the
            # pair is validated here rather than by two `required` fields.
            if not code and not password:
                raise ApiError(
                    "CODE_OR_PASSWORD_REQUIRED", "请填写验证码，或两步验证密码"
                )
            await manager.complete_telegram_user_login(
                code=code or None, password=password or None
            )
        elif action == "telegram-user-disconnect":
            await manager.disconnect_telegram_user()
        elif action == "exhentai":
            await manager.configure_exhentai(
                ExHentaiCredentials(
                    _required_text(payload, "ipb_member_id", "ipb_member_id"),
                    _required_text(payload, "ipb_pass_hash", "ipb_pass_hash"),
                    _required_text(payload, "igneous", "igneous"),
                )
            )
        else:  # exhentai-disconnect
            await manager.disconnect_exhentai()
    except ProviderConnectionError as exc:
        # The refusal is named rather than left to the snapshot: the provider
        # records its own error, but a credential rejected before it could be
        # stored leaves nothing there.
        raise ApiError(
            str(exc.code),
            exc.public_message,
            details=(
                {"retry_after": exc.retry_after}
                if exc.retry_after is not None
                else None
            ),
        ) from exc
    return await settings_snapshot(request, "connections")


#: The system preferences the JSON layer accepts. The same names the 系统 form
#: posts, so the two write paths cannot drift over what a field is called; the
#: values are validated by `SystemSettingsService`, once, for both.
SYSTEM_SETTING_FIELDS: tuple[str, ...] = (
    "source_concurrency",
    "poll_interval_ms",
    "timezone",
    "auto_approval_interval_minutes",
    "mobile_access_ttl_seconds",
    "mobile_refresh_ttl_seconds",
    "log_level",
    # The ordered, comma-separated provider chain. The page builds it from one
    # rank per source because that is what a form can express; the service stores
    # this string, so a JSON caller sends the shape that is stored.
    "download_source_priority",
)


@router.put("/settings/system")
async def put_system(request: Request) -> dict:
    """Store the 系统 preferences and make them current.

    Only the fields present in the body are written, so a client can change one
    cadence without restating the rest -- the page's behaviour, where an absent
    form field is left alone.

    The post-save refreshes are the page's, through the same
    `app.settings.runtime` helpers: the display zone is cached on `app.state` and
    the log level is process state, so a write that skipped them would look like
    it did nothing until the next restart.
    """
    await deps.require_session(request)
    deps.require_csrf(request)
    payload = await _json_body(request)
    values = {
        key: str(payload[key])
        for key in SYSTEM_SETTING_FIELDS
        if key in payload and payload[key] is not None
    }
    if not values:
        raise ApiError(
            "SETTINGS_REQUIRED",
            "请提供要修改的设置项",
            details={"allowed": list(SYSTEM_SETTING_FIELDS)},
        )
    service = deps.system_settings_service(request)
    try:
        await service.save(values)
    except SystemSettingsError as exc:
        raise ApiError(str(exc.code), exc.public_message) from exc
    await runtime.refresh_display_timezone(request, service)
    await runtime.refresh_download_source_priority(request, service)
    apply_runtime_log_level(await service.log_level())
    return await settings_snapshot(request, "system")


#: The archive-password and API-key actions the JSON layer accepts.
PASSWORD_ACTIONS: frozenset[str] = frozenset(
    {"add", "delete", "api-key-generate", "api-key-revoke"}
)


@router.put("/settings/passwords")
async def put_passwords(request: Request) -> dict:
    """Add or drop an archive password, or mint / revoke the mobile API key.

    Both live on the 密码库 tab because they are the same kind of thing -- a
    credential stored once -- and the API key is minted here rather than through
    the mobile API so a leaked client can never mint a replacement for itself.

    Nothing in the response is a stored secret. The one exception is by design
    and is the reason this returns a body rather than a snapshot alone:
    `api-key-generate` answers with the new plaintext **once**, because that is
    the only moment it exists outside the credential store -- the page shows it
    the same way, through a one-shot reveal. Every other action answers with the
    section snapshot, which reports *whether* a credential is configured and
    never what it is.
    """
    await deps.require_session(request)
    deps.require_csrf(request)
    payload = await _json_body(request)
    action = str(payload.get("action") or "")
    if action not in PASSWORD_ACTIONS:
        raise ApiError(
            "ACTION_UNKNOWN",
            f"未知的密码库动作：{action}",
            details={"allowed": sorted(PASSWORD_ACTIONS)},
        )
    plaintext_api_key: str | None = None
    if action == "add":
        raw_priority = str(payload.get("priority", "100")).strip() or "100"
        try:
            priority = int(raw_priority)
        except ValueError as exc:
            raise ApiError("PRIORITY_INVALID", "优先级必须是整数") from exc
        try:
            await deps.archive_settings_service(request).add_password(
                name=str(payload.get("name") or ""),
                password=str(payload.get("password") or ""),
                priority=priority,
                enabled=bool(payload.get("enabled", True)),
            )
        except ArchiveSettingsError as exc:
            raise ApiError(str(exc.code), exc.public_message) from exc
    elif action == "delete":
        password_id = payload.get("password_id")
        if not isinstance(password_id, int):
            raise ApiError(
                "PASSWORD_ID_REQUIRED",
                "请指定要删除的密码条目",
                details={"field": "password_id"},
            )
        await deps.archive_settings_service(request).delete_password(password_id)
    elif action == "api-key-generate":
        token, public_id, secret = new_token(KIND_API_KEY)
        # One transaction: revoke the old key and insert the new one together, so
        # the partial unique index can never see two valid keys.
        await deps.database(request).replace_api_key(
            label="移动端",
            public_id=public_id,
            secret_hash=hash_secret(secret),
        )
        plaintext_api_key = token
    else:  # api-key-revoke
        await deps.database(request).revoke_active_api_key()
    snapshot = await settings_snapshot(request, "passwords")
    if plaintext_api_key is not None:
        snapshot["api_key_plaintext"] = plaintext_api_key
    return snapshot


#: The batch actions over a selection of stored sources -- the 来源规则 tab's
#: bulk toolbar, one word each. `apply` is a word of its own rather than an
#: implication of the body, because an operator pressing 「批量启用」must not have
#: their filters overwritten by whatever rule fields the form happened to carry.
SOURCE_BATCH_ACTIONS: frozenset[str] = frozenset(
    {"enable", "disable", "delete", "apply"}
)


@router.post("/settings/sources")
async def post_source(request: Request) -> dict:
    """Create or update one listening source (the tab's 保存来源, over JSON).

    The reading half is `app/settings/sources.py`'s, shared with the page's
    `/sources` form: which chat ids are a channel, what an empty rule field
    means, and how a comma-separated tag list is cleaned are decided in one
    place, so the form and this endpoint cannot accept different sources.

    `configure_telegram_source` upserts on the chat id, which is what makes
    「保存」 work for a new row and an edit alike without the client having to say
    which one it is.
    """
    await deps.require_session(request)
    deps.require_csrf(request)
    payload = await _json_body(request)
    values, error = source_form.parse_source(payload)
    if error is not None or values is None:
        raise ApiError(
            str(getattr(error, "code", "SOURCE_INVALID")),
            str(getattr(error, "public_message", "来源无效")),
            details={"field": "source"},
        )
    await deps.database(request).configure_telegram_source(**values)
    # The MTProto ingester reads its target list from these rows, so a save is
    # also 「再看一眼这个来源」: the row may be byte-identical while the account
    # was just added to the channel, and nothing stored would show it.
    deps.connection_manager(request).note_sources_changed()
    return await settings_snapshot(request, "sources")


@router.put("/settings/sources/batch")
async def put_sources_batch(request: Request) -> dict:
    """Run one action over a selection of stored sources.

    `enable` / `disable` flip the whitelist flag, `delete` tombstones the rows,
    and `apply` overwrites the selection's whole rule set with the body's rule
    fields. All four can change what the MTProto ingester should be polling,
    which is why the refresh call sits after the branch rather than inside it.
    """
    await deps.require_session(request)
    deps.require_csrf(request)
    payload = await _json_body(request)
    action = str(payload.get("action") or "")
    if action not in SOURCE_BATCH_ACTIONS:
        raise ApiError(
            "ACTION_UNKNOWN",
            f"未知的来源动作：{action}",
            details={"allowed": sorted(SOURCE_BATCH_ACTIONS)},
        )
    source_ids = source_form.selected_source_ids(payload)
    if not source_ids:
        raise ApiError(
            "SOURCE_IDS_REQUIRED",
            "请至少选择一个来源",
            details={"field": "source_ids"},
        )
    database = deps.database(request)
    if action == "enable":
        await database.update_telegram_sources_bulk(source_ids, enabled=True)
    elif action == "disable":
        await database.update_telegram_sources_bulk(source_ids, enabled=False)
    elif action == "delete":
        await database.dismiss_telegram_sources(source_ids)
    else:  # apply
        rules, error = source_form.parse_source_rules(payload)
        if error is not None or rules is None:
            raise ApiError(
                str(getattr(error, "code", "SOURCE_RULES_INVALID")),
                str(getattr(error, "public_message", "来源规则无效")),
            )
        await database.update_telegram_sources_bulk(source_ids, rules=rules)
    deps.connection_manager(request).note_sources_changed()
    return await settings_snapshot(request, "sources")


@router.put("/settings/parse")
async def put_parse_rules(request: Request) -> dict:
    """Store the candidate-admission parse scheme (the 解析规则 tab, over JSON).

    The reading half is `app/settings/parse.py`'s, shared with the page's
    `/settings/parse` form: an unchecked switch is an absent field in both
    worlds, and the archive-format list is filtered against the parser's own
    vocabulary rather than the caller's.
    """
    await deps.require_session(request)
    deps.require_csrf(request)
    payload = await _json_body(request)
    service = deps.system_settings_service(request)
    try:
        await service.save_parse_rules(
            parse_form.parse_rules_submission(payload)
        )
    except SystemSettingsError as exc:
        raise ApiError(str(exc.code), exc.public_message) from exc
    return await settings_snapshot(request, "parse")


@router.post("/settings/parse/reset")
async def reset_parse_rules(request: Request) -> dict:
    """Put the shipped scheme back, so a bad experiment is one call from gone."""
    await deps.require_session(request)
    deps.require_csrf(request)
    await deps.system_settings_service(request).reset_parse_rules()
    return await settings_snapshot(request, "parse")


@router.put("/settings/parse/ai")
async def put_candidate_admission(request: Request) -> dict:
    """Store the AI candidate gate's switches and prompt.

    The default-prompt rule lives in `app/settings/parse.py` beside the page's
    copy of it: a prompt submitted byte-identical to the shipped default is
    stored as 「use the default」, because otherwise every save would pin the
    default text as an override and the page could never say whether the
    operator had actually customised it.
    """
    await deps.require_session(request)
    deps.require_csrf(request)
    payload = await _json_body(request)
    service = deps.system_settings_service(request)
    try:
        await service.save_candidate_admission(
            parse_form.candidate_admission(
                payload, default_prompt=DEFAULT_CANDIDATE_PROMPT
            )
        )
    except SystemSettingsError as exc:
        raise ApiError(str(exc.code), exc.public_message) from exc
    return await settings_snapshot(request, "parse")


@router.post("/settings/auto-approval/rules")
async def post_auto_approval_rule(request: Request) -> dict:
    """Create a rule, or overwrite the one `rule_id` names.

    One endpoint for both because it is one editor: absent `rule_id` means
    create, and a `rule_id` that no longer exists is a 404 rather than a
    re-create under the old id -- resurrecting a rule the operator deleted is
    the one outcome nobody asked for.

    The body carries the condition **AST** (`app/auto_approval/rules.py`'s
    shape, `{"kind": "condition", "field": …, "operator": …, "value": …}` or a
    group of them). The page builds that AST from its row editor; a JSON client
    sends it, which is why the validation is shared and the *parsing* is not.
    """
    await deps.require_session(request)
    deps.require_csrf(request)
    payload = await _json_body(request)
    try:
        values = approval_form.rule_payload(
            payload, condition=payload.get("condition")
        )
    except (RuleValidationError, ValueError, json.JSONDecodeError) as exc:
        raise ApiError("RULE_INVALID", str(exc)) from exc
    database = deps.database(request)
    try:
        await database.save_auto_approval_rule(**values)
    except LookupError as exc:
        raise ApiError("RULE_NOT_FOUND", "规则不存在", status_code=404) from exc
    return await settings_snapshot(request, "auto-approval")


@router.post("/settings/auto-approval/rules/dry-run")
async def post_auto_approval_dry_run(request: Request) -> dict:
    """Report what the edited rule would match. Writes nothing.

    Same fields as the save, sent here instead: what was tried is what gets
    saved, and an operator gets to see 「命中 12 条」before the rule starts
    deciding on its own.
    """
    await deps.require_session(request)
    deps.require_csrf(request)
    payload = await _json_body(request)
    try:
        values = approval_form.dry_run_payload(
            payload, condition=payload.get("condition")
        )
    except (RuleValidationError, ValueError) as exc:
        raise ApiError("RULE_INVALID", str(exc)) from exc
    result = await AutomaticApprovalService(deps.database(request)).dry_run(
        values["condition"],
        case_sensitive=values["case_sensitive"],
        action=values["action"],
    )
    return {"dry_run": auto_approval_dry_run(result)}


@router.put("/settings/auto-approval/rules/{rule_id}/toggle")
async def api_toggle_auto_approval_rule(rule_id: int, request: Request) -> dict:
    """Park a rule or put it back, without touching anything else about it."""
    await deps.require_session(request)
    deps.require_csrf(request)
    payload = await _json_body(request)
    try:
        await deps.database(request).set_auto_approval_rule_enabled(
            rule_id, checkbox(payload.get("enabled"))
        )
    except LookupError as exc:
        raise ApiError("RULE_NOT_FOUND", "规则不存在", status_code=404) from exc
    return await settings_snapshot(request, "auto-approval")


@router.delete("/settings/auto-approval/rules/{rule_id}")
async def api_delete_auto_approval_rule(rule_id: int, request: Request) -> dict:
    """Delete a rule for good.

    停用 is the reversible half and keeps its own call; this one cannot be undone
    from the interface, which is why the page puts a confirmation in front of it.
    """
    await deps.require_session(request)
    deps.require_csrf(request)
    try:
        await deps.database(request).delete_auto_approval_rule(rule_id)
    except LookupError as exc:
        raise ApiError("RULE_NOT_FOUND", "规则不存在", status_code=404) from exc
    return await settings_snapshot(request, "auto-approval")


@router.put("/settings/archive/torrent")
async def put_torrent_client(request: Request) -> dict:
    """Store the qBittorrent client settings (the 归档与工具 tab, over JSON).

    The reading half is `app/settings/archive.py`'s, shared with the page's
    `/archive-settings/torrent` form, so the two switches cannot mean different
    things in the two worlds. The password is written straight into the
    credential store and never read back -- the snapshot reports whether a client
    is configured, not what its password is.
    """
    await deps.require_session(request)
    deps.require_csrf(request)
    payload = await _json_body(request)
    try:
        await deps.archive_settings_service(request).save_torrent_client(
            archive_form.torrent_submission(payload)
        )
    except ArchiveSettingsError as exc:
        raise ApiError(str(exc.code), exc.public_message) from exc
    return await settings_snapshot(request, "archive")


@router.post("/settings/archive/torrent/test")
async def post_torrent_test(request: Request) -> dict:
    """Prove the stored settings reach a real client before a book needs it.

    A read of the client, not a write: the page's 「测试连接」 button, and the one
    way to find out that a host is unreachable without queueing a download into
    it. The version comes back so the operator can see *which* client answered.
    """
    await deps.require_session(request)
    deps.require_csrf(request)
    try:
        version = await deps.torrent_service(request).check_connection()
    except TorrentError as exc:
        raise ApiError(str(exc.code), exc.public_message) from exc
    return {"connected": True, "version": version}


@router.put("/settings/archive/limits")
async def put_archive_limits(request: Request) -> dict:
    """Store the safety limits, the keep-original switch and the image quality.

    Limits go first, on the page's reason: they are typed by hand and are the
    field that realistically fails validation, so a rejected number aborts before
    the quality level is touched.
    """
    await deps.require_session(request)
    deps.require_csrf(request)
    payload = await _json_body(request)
    service = deps.archive_settings_service(request)
    try:
        await service.save_limits(archive_form.limits_submission(payload))
        if "keep_original" in payload:
            await service.save_keep_original(checkbox(payload.get("keep_original")))
        if "image_quality" in payload:
            await service.save_image_quality(str(payload.get("image_quality") or ""))
    except ArchiveSettingsError as exc:
        raise ApiError(str(exc.code), exc.public_message) from exc
    return await settings_snapshot(request, "archive")


@router.put("/settings/archive/auto-pack")
async def put_auto_pack(request: Request) -> dict:
    """Store whether a finished download packs itself."""
    await deps.require_session(request)
    deps.require_csrf(request)
    payload = await _json_body(request)
    try:
        await deps.archive_settings_service(
            request
        ).save_auto_pack_after_download(checkbox(payload.get("enabled")))
    except ArchiveSettingsError as exc:
        raise ApiError(str(exc.code), exc.public_message) from exc
    return await settings_snapshot(request, "archive")


@router.put("/settings/paths/template")
async def put_library_template(request: Request) -> dict:
    """Store the layout template and, when given, the title preference.

    One call for both because they are one form: the template says where a book
    goes and the preference says what `{title}` resolves to, and previewing one
    without the other would show a path the packer would not produce. The
    preference is written first, on the page's reason -- a template refusal must
    not silently discard it, since the two are validated independently.
    """
    await deps.require_session(request)
    deps.require_csrf(request)
    payload = await _json_body(request)
    values = paths_form.template_payload(payload)
    service = deps.archive_settings_service(request)
    try:
        if "title_source" in values:
            await service.save_title_source(str(values["title_source"]))
        await service.save_library_template(str(values["library_template"]))
    except ArchiveSettingsError as exc:
        raise ApiError(str(exc.code), exc.public_message) from exc
    return await settings_snapshot(request, "paths")


@router.post("/settings/paths/rules")
async def post_path_rule(request: Request) -> dict:
    """Create a routing rule, or overwrite the one `path_rule_id` names.

    The condition arrives as the AST the engine evaluates (`validate_rule_ast`
    is the gate the page's row editor also goes through), and the template is
    validated by the archive settings service -- the same check the global
    template gets, so a rule that could not render is refused now rather than at
    pack time.
    """
    await deps.require_session(request)
    deps.require_csrf(request)
    payload = await _json_body(request)
    try:
        values = paths_form.path_rule_payload(
            payload, condition=payload.get("condition")
        )
    except (RuleValidationError, ValueError, json.JSONDecodeError) as exc:
        raise ApiError("RULE_INVALID", str(exc)) from exc
    try:
        await deps.archive_settings_service(request).save_path_rule(**values)
    except LookupError as exc:
        raise ApiError("RULE_NOT_FOUND", "规则不存在", status_code=404) from exc
    except ArchiveSettingsError as exc:
        raise ApiError(str(exc.code), exc.public_message) from exc
    return await settings_snapshot(request, "paths")


@router.put("/settings/paths/rules/{rule_id}/toggle")
async def toggle_path_rule(rule_id: int, request: Request) -> dict:
    """Park a routing rule or put it back, without touching anything else."""
    await deps.require_session(request)
    deps.require_csrf(request)
    payload = await _json_body(request)
    await deps.database(request).set_archive_path_rule_enabled(
        rule_id, checkbox(payload.get("enabled"))
    )
    return await settings_snapshot(request, "paths")


@router.delete("/settings/paths/rules/{rule_id}")
async def delete_path_rule(rule_id: int, request: Request) -> dict:
    """Delete a routing rule for good.

    停用 is the reversible half and keeps its own call; this one cannot be undone
    from the interface, which is why the page puts a confirmation in front of it.
    """
    await deps.require_session(request)
    deps.require_csrf(request)
    try:
        await deps.database(request).delete_archive_path_rule(rule_id)
    except LookupError as exc:
        raise ApiError("RULE_NOT_FOUND", "规则不存在", status_code=404) from exc
    return await settings_snapshot(request, "paths")


def _ai_section_of(scope: str) -> str:
    """Which settings section a chain scope's editor lives on.

    The three chain editors are the same widget mounted on three tabs, so the
    response after a chain edit is the tab the operator is looking at.
    """
    return {
        CHAIN_SCOPE_ARCHIVE_PATH: "paths",
        CHAIN_SCOPE_CANDIDATE: "parse",
    }.get(scope, "ai")


async def _ai_call(request: Request, operation: str, **kwargs: Any) -> dict:
    """Run one `AiService` call and answer with the section's snapshot.

    Every route below is this function plus a section name: the refusals are the
    service's (`AiError` carries the code and the operator's sentence), and a
    deployment with no AI service is a 503 from `deps`, not an AttributeError.
    """
    service = deps.ai_service(request)
    try:
        await getattr(service, operation)(**kwargs)
    except AiError as exc:
        raise ApiError(str(exc.code), exc.public_message) from exc
    return await settings_snapshot(request, _ai_section_of(kwargs.get("scope", "")))


@router.put("/settings/ai/master")
async def put_ai_master(request: Request) -> dict:
    """Toggle the global AI master switch.

    State only: turning it off stops every feature from calling a model and
    deletes nothing -- providers, keys, chains and per-feature switches all stay
    where the operator left them, so turning it back on restores the exact
    configuration that was there before.
    """
    await deps.require_session(request)
    deps.require_csrf(request)
    payload = await _json_body(request)
    await deps.system_settings_service(request).save_ai_enabled(
        checkbox(payload.get("enabled"))
    )
    return await settings_snapshot(request, "ai")


@router.post("/settings/ai/providers")
async def post_ai_provider(request: Request) -> dict:
    """Create a provider, or overwrite the one `provider_id` names."""
    await deps.require_session(request)
    deps.require_csrf(request)
    payload = await _json_body(request)
    return await _ai_call(
        request, "save_provider", values=ai_form.provider_submission(payload)
    )


@router.delete("/settings/ai/providers/{provider_id}")
async def delete_ai_provider(provider_id: int, request: Request) -> dict:
    await deps.require_session(request)
    deps.require_csrf(request)
    return await _ai_call(request, "delete_provider", provider_id=provider_id)


@router.post("/settings/ai/providers/{provider_id}/keys")
async def post_ai_provider_keys(provider_id: int, request: Request) -> dict:
    """Store one or more API keys from a textarea, one per line.

    The value is never echoed back -- not in the snapshot, not in an error
    message: `keys` in the section payload are identifiers and states.
    """
    await deps.require_session(request)
    deps.require_csrf(request)
    payload = await _json_body(request)
    return await _ai_call(
        request,
        "add_keys",
        provider_id=provider_id,
        raw=ai_form.key_text(payload),
    )


@router.put("/settings/ai/keys/{key_id}/toggle")
async def toggle_ai_key(key_id: int, request: Request) -> dict:
    await deps.require_session(request)
    deps.require_csrf(request)
    payload = await _json_body(request)
    return await _ai_call(
        request,
        "set_key_enabled",
        key_id=key_id,
        enabled=checkbox(payload.get("enabled")),
    )


@router.post("/settings/ai/keys/{key_id}/reset")
async def reset_ai_key(key_id: int, request: Request) -> dict:
    """Release a key from its cooldown, so it is tried again."""
    await deps.require_session(request)
    deps.require_csrf(request)
    return await _ai_call(request, "reset_key_cooldown", key_id=key_id)


@router.delete("/settings/ai/keys/{key_id}")
async def delete_ai_key(key_id: int, request: Request) -> dict:
    await deps.require_session(request)
    deps.require_csrf(request)
    return await _ai_call(request, "delete_key", key_id=key_id)


@router.post("/settings/ai/providers/{provider_id}/models")
async def post_ai_provider_models(provider_id: int, request: Request) -> dict:
    """Add one or more model names to a provider."""
    await deps.require_session(request)
    deps.require_csrf(request)
    payload = await _json_body(request)
    return await _ai_call(
        request,
        "add_models",
        provider_id=provider_id,
        names=ai_form.model_names(payload),
    )


@router.post("/settings/ai/providers/{provider_id}/models/fetch")
async def fetch_ai_provider_models(provider_id: int, request: Request) -> dict:
    """`GET /v1/models`, as a list for the operator to pick from.

    Fetched names are not added and not tested: a listing says a model exists,
    not that this deployment can talk to it. So this is a read, and it answers
    with the names rather than a section snapshot.
    """
    await deps.require_session(request)
    deps.require_csrf(request)
    service = deps.ai_service(request)
    try:
        names = await service.list_remote_models(provider_id)
    except AiError as exc:
        raise ApiError(str(exc.code), exc.public_message) from exc
    return {"provider_id": provider_id, "names": list(names)}


@router.put("/settings/ai/models/{model_id}/toggle")
async def toggle_ai_model(model_id: int, request: Request) -> dict:
    await deps.require_session(request)
    deps.require_csrf(request)
    payload = await _json_body(request)
    return await _ai_call(
        request,
        "set_model_enabled",
        model_id=model_id,
        enabled=checkbox(payload.get("enabled")),
    )


@router.put("/settings/ai/models/{model_id}/params")
async def put_ai_model_params(model_id: int, request: Request) -> dict:
    """Store a model's request parameters (a JSON string, as the page submits)."""
    await deps.require_session(request)
    deps.require_csrf(request)
    payload = await _json_body(request)
    return await _ai_call(
        request, "save_model_params", model_id=model_id, raw=payload.get("params")
    )


@router.delete("/settings/ai/models/{model_id}")
async def delete_ai_model(model_id: int, request: Request) -> dict:
    await deps.require_session(request)
    deps.require_csrf(request)
    return await _ai_call(request, "delete_model", model_id=model_id)


@router.post("/settings/ai/models/{model_id}/verify")
async def verify_ai_model(model_id: int, request: Request) -> dict:
    """Send the minimal chat request and report what came back.

    A failure answers 400 with the provider's own words, the same shape the
    qBittorrent 连通测试 uses: the operator asked a question, and the answer is
    「不，原因是 …」 rather than a 200 that hides it.
    """
    await deps.require_session(request)
    deps.require_csrf(request)
    service = deps.ai_service(request)
    try:
        verification = await service.verify_model(model_id)
    except AiError as exc:
        raise ApiError(str(exc.code), exc.public_message) from exc
    if not verification.ok:
        raise ApiError("MODEL_VERIFY_FAILED", f"验证失败：{verification.message}")
    return {"ok": True, "message": verification.message}


@router.post("/settings/ai/providers/{provider_id}/verify")
async def verify_ai_provider(provider_id: int, request: Request) -> dict:
    """Test every enabled model of one provider, in order."""
    await deps.require_session(request)
    deps.require_csrf(request)
    service = deps.ai_service(request)
    try:
        results = await service.verify_all(provider_id)
    except AiError as exc:
        raise ApiError(str(exc.code), exc.public_message) from exc
    return {"results": _verify_payload(results)}


@router.post("/settings/ai/verify")
async def verify_all_ai_models(request: Request) -> dict:
    """Test every enabled model of every provider, in order."""
    await deps.require_session(request)
    deps.require_csrf(request)
    service = deps.ai_service(request)
    try:
        results = await service.verify_all()
    except AiError as exc:
        raise ApiError(str(exc.code), exc.public_message) from exc
    return {"results": _verify_payload(results)}


def _verify_payload(results: Any) -> dict[str, Any]:
    """A verification run as `{ok, failed, message}`.

    The summary is built here rather than in each caller because the page says
    the same sentence: 「测试完成：N/M 个模型通过」, or the empty case's own line.
    """
    items = list(results)
    passed = sum(1 for item in items if item.ok)
    if not items:
        return {"ok": 0, "total": 0, "message": "没有启用的模型可测试。"}
    return {
        "ok": passed,
        "total": len(items),
        "message": f"测试完成：{passed}/{len(items)} 个模型通过。",
        "entries": [
            {"ok": bool(item.ok), "message": str(item.message)} for item in items
        ],
    }


@router.put("/settings/ai/chain/{scope}")
async def put_ai_chain(scope: str, request: Request) -> dict:
    """One ordered-list verb against one scope's model chain.

    Four verbs, three scopes, one endpoint: the editors differ in *which* list
    they edit and in nothing else, and two copies of 「上移/下移」 is how one of
    them quietly stops supporting the last position.

    `primary` makes the model the head, `append` puts it at the end, `shift`
    moves an entry that is already in the list by `delta`, and `remove` takes it
    out.
    """
    await deps.require_session(request)
    deps.require_csrf(request)
    payload = await _json_body(request)
    try:
        code = ai_form.chain_scope(scope)
        action = ai_form.chain_action(str(payload.get("action") or ""))
        model_id = ai_form.chain_model_id(payload)
    except AiError as exc:
        raise ApiError(str(exc.code), exc.public_message) from exc
    service = deps.ai_service(request)
    try:
        if action == "shift":
            await service.shift_chain(model_id, int(payload.get("delta") or 0), code)
        elif action == "remove":
            await service.remove_from_chain(model_id, code)
        elif action == "primary":
            await service.set_chain_primary(model_id, code)
        else:
            await service.append_to_chain(model_id, code)
    except (AiError, ValueError) as exc:
        message = exc.public_message if isinstance(exc, AiError) else "移动方向无效"
        raise ApiError(str(getattr(exc, "code", "CHAIN_SHIFT_INVALID")), message) from exc
    return await settings_snapshot(request, _ai_section_of(code))


@router.put("/settings/paths/ai")
async def put_ai_path_settings(request: Request) -> dict:
    """Store the 路径来源 choice and the AI sub-panel in one write.

    One call because it is one form: the radio says whether the model or the
    template decides a path, and the prompt, the two numbers and the three
    switches only mean anything under the AI choice. The numbers are written
    first, on the archive tab's reason -- they are the field that realistically
    fails validation, so a rejected value aborts before the mode is changed.
    """
    await deps.require_session(request)
    deps.require_csrf(request)
    payload = await _json_body(request)
    values = paths_form.ai_path_submission(payload)
    service = deps.archive_settings_service(request)
    if bool(values["auto_series"]):
        # Same rule as the page: 「自动添加系列」 needs the master switch on and
        # a chain to ask. Refused before any write so a rejected call changes
        # nothing at all.
        if not await deps.system_settings_service(request).ai_enabled():
            raise ApiError(
                "SERIES_AI_UNAVAILABLE", "AI 总开关关闭，无法开启「自动添加系列」"
            )
        ai_service = deps.optional_service(request, "ai_service")
        if ai_service is None or not await ai_service.effective_chain(
            CHAIN_SCOPE_ARCHIVE_PATH
        ):
            raise ApiError(
                "SERIES_AI_UNAVAILABLE",
                "还没有可用的模型链，无法开启「自动添加系列」",
            )
    try:
        await service.save_ai_batch_size(values["ai_batch_size"])
        await service.save_ai_concurrency(values["ai_concurrency"])
        await service.save_path_source(str(values["path_source"]))
        await service.save_ai_prompt(str(values["ai_prompt"]))
        await service.save_ai_fallback_to_rules(
            bool(values["ai_fallback_to_rules"])
        )
        await service.save_ai_stream(bool(values["ai_stream"]))
        await service.save_ai_default_include_current(
            bool(values["ai_default_include_current"])
        )
        await service.save_auto_series(bool(values["auto_series"]))
    except ArchiveSettingsError as exc:
        raise ApiError(str(exc.code), exc.public_message) from exc
    return await settings_snapshot(request, "paths")


@router.put("/settings/paths/ai/prompt")
async def put_ai_path_prompt(request: Request) -> dict:
    """Store the archive-path prompt, or reset it to the shipped one.

    `{"prompt": ""}` is how the page's 「恢复默认」 button submits, and it is why
    this is a separate call from the panel: blank means 「use the default」 rather
    than 「ask nothing」, and the default lives in `app/ai/prompt.py` -- writing a
    copy of today's wording into the database would freeze it for a deployment
    that upgrades to a better one tomorrow.
    """
    await deps.require_session(request)
    deps.require_csrf(request)
    payload = await _json_body(request)
    try:
        await deps.archive_settings_service(request).save_ai_prompt(
            str(payload.get("prompt") or "")
        )
    except ArchiveSettingsError as exc:
        raise ApiError(str(exc.code), exc.public_message) from exc
    return await settings_snapshot(request, "paths")


@router.put("/settings/parse/model-source")
async def put_candidate_model_source(request: Request) -> dict:
    """Choose whether the AI candidate gate inherits the default model chain or
    carries its own.

    Its own call rather than part of the admission write because it is a
    different question: the gate's switches say what to do with a verdict, this
    says which list of models produces one.
    """
    await deps.require_session(request)
    deps.require_csrf(request)
    payload = await _json_body(request)
    try:
        await deps.system_settings_service(
            request
        ).save_ai_candidate_model_source(str(payload.get("model_source") or ""))
    except SystemSettingsError as exc:
        raise ApiError(str(exc.code), exc.public_message) from exc
    return await settings_snapshot(request, "parse")


@router.get("/settings/{section}")
async def get_settings_section(request: Request, section: str) -> dict:
    """One section's stored settings, read-only."""
    await deps.require_session(request)
    try:
        return await settings_snapshot(request, section)
    except KeyError as exc:
        raise ApiError(
            "SETTINGS_SECTION_NOT_FOUND",
            "设置分区不存在",
            status_code=404,
            details={"section": section},
        ) from exc


__all__ = [
    "DIALOG_KINDS",
    "SOURCE_ARCHIVE_FORMATS",
    "SOURCE_TYPES",
    "router",
    "section_tabs",
    "settings_snapshot",
]
