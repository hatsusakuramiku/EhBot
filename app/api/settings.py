"""One snapshot per settings section.

`/settings/{section}` and `GET /api/v1/settings/{section}` render the same seven
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
    log_entry_payload,
    archive_password,
    auto_approval_rule,
    connection_snapshot,
    safety_limits,
    telegram_source,
    tool_profile,
)
from app.api.status import (
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
from app.archive.service import (
    TITLE_SOURCE_ENGLISH,
    TITLE_SOURCE_JAPANESE,
)
from app.auto_approval.rules import (
    ALL_OPERATORS,
    ALLOWED_FIELDS,
    COLLECTION_OPERATORS,
    EXISTENCE_OPERATORS,
    NUMERIC_OPERATORS,
    REGEX_FIELDS,
    TEXT_OPERATORS,
)
from app.auto_approval.service import DRY_RUN_SCAN_LIMIT
from app.conversion.naming import (
    DEFAULT_LIBRARY_TEMPLATE,
    MAX_SEGMENT_LENGTH,
    PLACEHOLDER_LABELS,
    TEMPLATE_PLACEHOLDERS,
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
        "archive_formats": list(SOURCE_ARCHIVE_FORMATS),
    }


async def _auto_approval_section(request: Request) -> dict[str, Any]:
    database = deps.database(request)
    return {
        "rules": [
            auto_approval_rule(rule)
            for rule in await database.list_auto_approval_rules()
        ],
        # The editor's dropdowns are filled from the engine's own tables, so a
        # field the evaluator does not support can never be offered. Sorted
        # because a frozenset has no order and an editor whose list reshuffles
        # between requests is unusable.
        "vocabulary": {
            "fields": [
                {"code": field, "label": field_label(field)}
                for field in sorted(ALLOWED_FIELDS)
            ],
            "regex_fields": sorted(REGEX_FIELDS),
            "operators": sorted(ALL_OPERATORS),
            "text_operators": sorted(TEXT_OPERATORS),
            "numeric_operators": sorted(NUMERIC_OPERATORS),
            "collection_operators": sorted(COLLECTION_OPERATORS),
            "existence_operators": sorted(EXISTENCE_OPERATORS),
        },
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
    app_settings = request.app.state.settings
    return {
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
        "template": {
            "default": DEFAULT_LIBRARY_TEMPLATE,
            "max_segment_length": MAX_SEGMENT_LENGTH,
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
    return {
        "system": await service.snapshot(),
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
        **await _log_view(request),
    }


async def _log_view(request: Request) -> dict[str, Any]:
    """The log tail for the 系统 tab, read off disk on demand.

    Read here rather than in the page route so the JSON endpoint and the render
    cannot show different logs -- the same rule every other section follows. The
    file read is pushed to a thread because this is an async handler and the tail
    of a rotating file is real disk I/O.
    """
    settings = request.app.state.settings
    level = (request.query_params.get("log_level") or "").strip().upper() or None
    if level is not None and level not in LOG_LEVELS:
        # An unknown level is dropped rather than refused: unlike a settings
        # section this arrives from a filter link, and showing everything is a
        # safe answer where a 404 on the whole tab is not.
        level = None
    limit = clamp_limit(request.query_params.get("log_limit"))
    entries, present = await asyncio.to_thread(
        read_log_tail, settings.log_dir, limit=limit, level=level
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
            "configured_level": settings.log_level,
            "access_log": settings.log_access,
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
    "SOURCE_ARCHIVE_FORMATS",
    "SOURCE_TYPES",
    "router",
    "section_tabs",
    "settings_snapshot",
]
