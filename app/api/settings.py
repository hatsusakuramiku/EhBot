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
from typing import Any, Callable, Coroutine

from fastapi import APIRouter, Request

from app.api import deps
from app.api.contracts import ApiError
from app.api.serializers import (
    ai_chain_entry,
    ai_provider,
    ai_provider_key,
    ai_provider_model,
    ai_selectable_model,
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
    SETTINGS_SECTIONS,
    SETTINGS_SOURCES,
    SETTINGS_SYSTEM,
    log_level_view,
    dependency_view,
    settings_section_view,
)
from app.ai.prompt import DEFAULT_AI_PROMPT
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
    TEXT_OPERATORS,
)
from app.auto_approval.service import DRY_RUN_SCAN_LIMIT
from app.ai.models import (
    CHAIN_SCOPE_ARCHIVE_PATH,
    CHAIN_SCOPE_DEFAULT,
    DEFAULT_MAX_RETRIES,
    DEFAULT_TIMEOUT_SECONDS,
    KEY_COOLDOWN_MINUTES,
    MAX_RETRIES,
    MAX_TIMEOUT_SECONDS,
    MIN_TIMEOUT_SECONDS,
    PROVIDER_CODE_LABELS,
    SUPPORTED_PROVIDER_CODES,
)
from app.archive.service import (
    MODEL_SOURCE_DEFAULT,
    MODEL_SOURCE_CUSTOM,
)
from app.conversion.naming import (
    DEFAULT_LIBRARY_TEMPLATE,
    PLACEHOLDER_LABELS,
    TEMPLATE_PLACEHOLDERS,
    detect_library_limits,
)
from app.logs.reader import MAX_LIMIT, clamp_limit, read_log_tail
from app.review.models import field_label
from app.settings.service import (
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
    deps.require_session(request)
    return {"tabs": section_tabs("")}


@router.get("/settings/{section}")
async def get_settings_section(request: Request, section: str) -> dict:
    """One section's stored settings, read-only."""
    deps.require_session(request)
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
