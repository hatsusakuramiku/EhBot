"""The settings tabs and every form that writes one.

`/settings` and `/archive-settings/*` are one module because they are one page:
the archive paths, the toolchain and the passwords are all tabs of `/settings`,
and the POST paths kept their pre-R8 URLs so a bookmarked form action still
works.
"""

from __future__ import annotations

import json

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import RedirectResponse

from app.api.events import EVENT_DOWNLOAD
from app.api.serializers import auto_approval_dry_run
from app.ai.errors import AI_CHAIN_ENTRY_MISSING, AiError
from app.ai.models import (
    CHAIN_SCOPE_ARCHIVE_PATH,
    CHAIN_SCOPE_CANDIDATE,
    CHAIN_SCOPE_DEFAULT,
)
from app.api.status import (
    SETTINGS_AI,
    SETTINGS_ARCHIVE,
    SETTINGS_CONNECTIONS,
    SETTINGS_PARSE,
    SETTINGS_PASSWORDS,
    SETTINGS_PATHS,
    SETTINGS_SOURCES,
    SETTINGS_SYSTEM,
    SETTINGS_SECTIONS,
)
from app.connections.models import ProviderConnectionError
from app.credentials import KIND_API_KEY, hash_secret, new_token
from app.auto_approval.rules import (
    RuleValidationError,
    editor_rows,
    validate_rule_ast,
)
from app.auto_approval.service import AutomaticApprovalService
from app.ai.prompt import DEFAULT_CANDIDATE_PROMPT
from app.candidates.parse_rules import ARCHIVE_FORMATS as PARSE_ARCHIVE_FORMATS
from app.web.rule_forms import parse_rule_condition
from app.archive.rearchive import rearchive_works
from app.archive.service import (
    LIMIT_KEYS as ARCHIVE_LIMIT_KEYS,
    TITLE_SOURCE_JAPANESE,
    TITLE_SOURCES,
    ArchiveSettingsError,
)
from app.downloads.models import AUTO_DOWNLOAD_PROVIDERS
from app.conversion.naming import (
    CBZ_SUFFIX,
    LibraryLimits,
    LibraryTemplateError,
    detect_library_limits,
    render_library_path,
    validate_library_template,
)
from app.logging import apply_runtime_log_level
from app.review.models import field_label
from app.settings.service import SystemSettingsError
from app.torrent.models import TorrentError
from app.web import deps
from app.web.settings_view import render_settings, settings_redirect

router = APIRouter()


def _parse_csv_tags(raw: object) -> tuple[str, ...]:
    if raw is None:
        return ()
    cleaned: list[str] = []
    for item in str(raw).replace("\n", ",").split(","):
        token = item.strip().lower()
        if token:
            cleaned.append(token)
    return tuple(cleaned)


@router.get("/sources")
async def sources_page(request: Request):
    """Retired: 来源规则 is a tab of `/settings`."""
    return RedirectResponse(
        request.url_for("settings_section", section=SETTINGS_SOURCES).path,
        status_code=307,
    )


@router.post("/sources")
async def configure_source(request: Request):
    redirect = deps.require_authenticated(request)
    if redirect:
        return redirect
    form = await request.form()
    deps.validate_csrf(request, str(form.get("csrf_token") or ""))
    source_type = str(form.get("source_type") or "")
    display_name = str(form.get("display_name") or "").strip()
    try:
        chat_id = int(str(form.get("chat_id") or ""))
        max_attachment_size_mb = int(
            str(form.get("max_attachment_size_mb") or "0")
        )
    except ValueError:
        chat_id = 0
        max_attachment_size_mb = -1
    valid_identity = (
        source_type == "CHANNEL" and chat_id < 0
    ) or (
        source_type == "PRIVATE_CHAT" and chat_id > 0
    )
    if not valid_identity or not display_name or max_attachment_size_mb < 0:
        return await render_settings(
            request,
            SETTINGS_SOURCES,
            error="来源类型、ID、名称或附件上限无效",
            status_code=400,
        )
    submitted_formats = set(form.getlist("allowed_archive_formats"))
    allowed_archive_formats = tuple(
        archive_format
        for archive_format in ("zip", "rar", "7z", "cbz")
        if archive_format in submitted_formats
    )
    required_tags = _parse_csv_tags(
        form.get("required_tags")
    )
    forbidden_tags = _parse_csv_tags(
        form.get("forbidden_tags")
    )
    allowed_languages = _parse_csv_tags(
        form.get("allowed_languages")
    )
    allowed_categories = _parse_csv_tags(
        form.get("allowed_categories")
    )
    min_rating_raw = str(form.get("min_rating") or "").strip()
    min_rating: float | None = None
    if min_rating_raw:
        try:
            min_rating = float(min_rating_raw)
        except ValueError:
            min_rating = -1.0
    if min_rating is not None and min_rating < 0:
        return await render_settings(
            request,
            SETTINGS_SOURCES,
            error="最低评分格式无效",
            status_code=400,
        )
    await deps.database(request).configure_telegram_source(
        source_type=source_type,
        chat_id=chat_id,
        display_name=display_name,
        enabled=form.get("enabled") == "on",
        allowed_archive_formats=allowed_archive_formats,
        max_attachment_size_mb=max_attachment_size_mb,
        required_tags=required_tags,
        forbidden_tags=forbidden_tags,
        allowed_languages=allowed_languages,
        allowed_categories=allowed_categories,
        min_rating=min_rating,
    )
    # A save is the operator asking the MTProto ingester to look at this source
    # again: the row may be byte-identical, but the account may have just been
    # added to the channel, and nothing about the stored set would show it.
    deps.connection_manager(request).note_sources_changed()
    return settings_redirect(request, SETTINGS_SOURCES)


@router.post("/sources/dialogs")
async def browse_telegram_dialogs(request: Request, csrf_token: str = Form()):
    """Read the account's own chats so an id can be picked, not waited for.

    The whitelist asks for a Telegram Chat ID, and until now the only way to
    learn one was to receive a message from that chat and read it off the
    candidate. The logged-in user account already knows every chat it is in --
    that list is what the ingest capability check walks -- so the page can ask
    for it and offer the ids with their names. Nothing is stored here; picking
    one fills the 添加来源 form, and a save still goes through the same
    validation it always did.
    """
    redirect = deps.require_authenticated(request)
    if redirect:
        return redirect
    deps.validate_csrf(request, csrf_token)
    try:
        dialogs = await deps.connection_manager(
            request
        ).list_telegram_user_dialogs()
    except ProviderConnectionError as exc:
        return await render_settings(
            request,
            SETTINGS_SOURCES,
            error=exc.public_message,
            status_code=400,
        )
    if not dialogs:
        return await render_settings(
            request,
            SETTINGS_SOURCES,
            notice="账户里没有可读取的会话。",
        )
    return await render_settings(
        request,
        SETTINGS_SOURCES,
        notice="读到 {} 个会话，点「填入表单」把 ID 与名称带进左侧表单。".format(
            len(dialogs)
        ),
        dialogs=dialogs,
    )


@router.post("/sources/dialogs/select")
async def select_telegram_dialog(
    request: Request,
    csrf_token: str = Form(),
    source_type: str = Form(),
    chat_id: str = Form(),
    display_name: str = Form(),
):
    """Put one picked dialog into the 添加来源 form. Stores nothing.

    The values come back from the list the page itself rendered, which makes
    them the operator's own submission -- and they are checked here with the
    same identity rule `configure_source` applies on save, because a prefill
    that could not be saved would be a form that lies about what 保存来源 does.
    """
    redirect = deps.require_authenticated(request)
    if redirect:
        return redirect
    deps.validate_csrf(request, csrf_token)
    name = display_name.strip()
    try:
        number = int(chat_id)
    except ValueError:
        number = 0
    valid_identity = (source_type == "CHANNEL" and number < 0) or (
        source_type == "PRIVATE_CHAT" and number > 0
    )
    if not valid_identity or not name:
        return await render_settings(
            request,
            SETTINGS_SOURCES,
            error="来源类型、ID 或名称无效",
            status_code=400,
        )
    return await render_settings(
        request,
        SETTINGS_SOURCES,
        notice="已填入「{}」（{}），确认过滤规则后保存。".format(name, number),
        prefill={
            "source_type": source_type,
            "chat_id": number,
            "display_name": name,
        },
    )


def _source_rules_from_form(form) -> tuple[dict | None, str | None]:
    """Read one source's filter rules out of a submitted form.

    Shared by the per-row 更新规则 form and the batch 套用规则 action, so the two
    cannot disagree about what an empty field means -- it clears that rule, which
    is the only reading an operator can predict from an empty box.
    """
    try:
        max_attachment_size_mb = int(
            str(form.get("max_attachment_size_mb") or "0")
        )
    except ValueError:
        return None, "附件上限格式无效"
    if max_attachment_size_mb < 0:
        return None, "附件上限无效"
    min_rating_raw = str(form.get("min_rating") or "").strip()
    min_rating: float | None = None
    if min_rating_raw:
        try:
            min_rating = float(min_rating_raw)
        except ValueError:
            return None, "最低评分格式无效"
        if min_rating < 0:
            return None, "最低评分格式无效"
    submitted_formats = set(form.getlist("allowed_archive_formats"))
    return (
        {
            "allowed_archive_formats": [
                archive_format
                for archive_format in ("zip", "rar", "7z", "cbz")
                if archive_format in submitted_formats
            ],
            "max_attachment_size_mb": max_attachment_size_mb,
            "required_tags": list(_parse_csv_tags(form.get("required_tags"))),
            "forbidden_tags": list(_parse_csv_tags(form.get("forbidden_tags"))),
            "allowed_languages": list(
                _parse_csv_tags(form.get("allowed_languages"))
            ),
            "allowed_categories": list(
                _parse_csv_tags(form.get("allowed_categories"))
            ),
            "min_rating": min_rating,
        },
        None,
    )


def _selected_source_ids(form) -> list[int]:
    ids: list[int] = []
    for value in form.getlist("source_ids"):
        try:
            number = int(str(value))
        except ValueError:
            continue
        if number not in ids:
            ids.append(number)
    return ids


@router.post("/sources/batch")
async def sources_batch_action(request: Request):
    """One action over a selection of stored sources.

    `enable`/`disable` flip the whitelist flag; `delete` tombstones (see
    `dismiss_telegram_sources`); `apply` overwrites the whole filter rule set
    with whatever the batch form carried, so an operator can fix a dozen
    sources at once instead of opening twelve rows.
    """
    redirect = deps.require_authenticated(request)
    if redirect:
        return redirect
    form = await request.form()
    deps.validate_csrf(request, str(form.get("csrf_token") or ""))
    source_ids = _selected_source_ids(form)
    if not source_ids:
        return await render_settings(
            request,
            SETTINGS_SOURCES,
            error="请至少选择一个来源",
            status_code=400,
        )
    action = str(form.get("action") or "")
    database = deps.database(request)
    if action == "enable":
        await database.update_telegram_sources_bulk(source_ids, enabled=True)
    elif action == "disable":
        await database.update_telegram_sources_bulk(source_ids, enabled=False)
    elif action == "delete":
        await database.dismiss_telegram_sources(source_ids)
    elif action == "apply":
        rules, error = _source_rules_from_form(form)
        if error is not None:
            return await render_settings(
                request, SETTINGS_SOURCES, error=error, status_code=400
            )
        await database.update_telegram_sources_bulk(
            source_ids, rules=rules
        )
    else:
        return await render_settings(
            request,
            SETTINGS_SOURCES,
            error=f"未知的来源动作：{action}",
            status_code=400,
        )
    # The MTProto ingester reads its target list from the same rows, so any of
    # these four actions can change what it should be polling.
    deps.connection_manager(request).note_sources_changed()
    return settings_redirect(request, SETTINGS_SOURCES)


@router.post("/sources/batch-add")
async def add_sources_bulk(request: Request):
    """Create several sources at once from the account's dialog list.

    The chat list is the account's own, so every entry is already known to be
    reachable; the identity check is repeated here only because the browser can
    send anything. Rows are created disabled with no rules, the same shape a
    single 保存来源 of a new chat produces, so the operator confirms filters
    before a source starts admitting messages.
    """
    redirect = deps.require_authenticated(request)
    if redirect:
        return redirect
    form = await request.form()
    deps.validate_csrf(request, str(form.get("csrf_token") or ""))
    entries: list[dict] = []
    seen: set[int] = set()
    for token in form.getlist("dialogs"):
        try:
            payload = json.loads(str(token))
            source_type = str(payload["source_type"])
            chat_id = int(payload["chat_id"])
            display_name = str(payload["display_name"]).strip()
        except (KeyError, TypeError, ValueError):
            continue
        valid_identity = (
            source_type == "CHANNEL" and chat_id < 0
        ) or (source_type == "PRIVATE_CHAT" and chat_id > 0)
        if not valid_identity or not display_name or chat_id in seen:
            continue
        seen.add(chat_id)
        entries.append(
            {
                "source_type": source_type,
                "chat_id": chat_id,
                "display_name": display_name,
            }
        )
    if not entries:
        return await render_settings(
            request,
            SETTINGS_SOURCES,
            error="请至少选择一个会话",
            status_code=400,
        )
    created = await deps.database(request).add_telegram_sources_bulk(entries)
    deps.connection_manager(request).note_sources_changed()
    return await render_settings(
        request,
        SETTINGS_SOURCES,
        notice=(
            f"已新增 {created} 个来源（其余已存在），均为停用状态，"
            "请在右侧逐个确认过滤规则后再启用。"
        ),
    )


@router.get("/settings")
async def settings_index(request: Request):
    """The settings domain has no landing page of its own -- open a tab.

    Declared above `/settings/{section}` so the literal path wins the match,
    and 307 so the browser does not cache a move that is really a default.
    """
    return RedirectResponse(
        request.url_for(
            "settings_section", section=SETTINGS_CONNECTIONS
        ).path,
        status_code=307,
    )


@router.get("/settings/{section}")
async def settings_section(request: Request, section: str):
    """One settings tab.

    `allow_password_change` because 密码库 is where the bootstrap password is
    replaced: it is the destination `require_authenticated` bounces to, so it
    must not bounce. Every other tab stays behind the bounce, which is what
    keeps an operator from configuring a deployment they have not finished
    securing.

    An unknown section is a 404 rather than a redirect to the first tab: a
    mistyped URL is a mistake to report, and quietly rendering 外部连接 for
    `/settings/nonsense` would invent a tab.
    """
    redirect = deps.require_authenticated(
        request, allow_password_change=section == SETTINGS_PASSWORDS
    )
    if redirect:
        return redirect
    if section not in SETTINGS_SECTIONS:
        raise HTTPException(status_code=404, detail="设置分区不存在")
    return await render_settings(request, section)


@router.get("/archive-settings")
async def archive_settings_page(request: Request):
    """Retired: 归档 is a tab of `/settings`."""
    return RedirectResponse(
        request.url_for("settings_section", section=SETTINGS_ARCHIVE).path,
        status_code=307,
    )


@router.post("/archive-settings/auto-pack")
async def save_auto_pack_after_download(
    request: Request, csrf_token: str = Form()
):
    redirect = deps.require_authenticated(request)
    if redirect:
        return redirect
    deps.validate_csrf(request, csrf_token)
    form = await request.form()
    await deps.archive_settings_service(request).save_auto_pack_after_download(
        form.get("enabled") == "on"
    )
    return settings_redirect(request, SETTINGS_ARCHIVE)


@router.post("/archive-settings/paths")
async def save_archive_paths(request: Request, csrf_token: str = Form()):
    redirect = deps.require_authenticated(request)
    if redirect:
        return redirect
    deps.validate_csrf(request, csrf_token)
    form = await request.form()
    try:
        await deps.archive_settings_service(request).save_paths(
            {
                "library_path": str(form.get("library_path") or ""),
                "work_path": str(form.get("work_path") or ""),
            }
        )
    except ArchiveSettingsError as exc:
        return await render_settings(
            request,
            SETTINGS_PATHS,
            error=exc.public_message,
            status_code=400,
        )
    return settings_redirect(request, SETTINGS_PATHS)


def _rearchive_notice(result: dict) -> str:
    """One line for the page's notice slot, from the sweep's own counts.

    Built here rather than in the template so the JSON-less page and any later
    caller describe the same run the same way, and so a zero is never spelled as
    a count: 「已检查 12 件：移动 0 件」 reads worse than saying what happened.

    A 试跑 says so in its first word. The counts below it are then 「将会」 rather
    than 「已经」, and a notice that left that out would describe a run that did
    not happen.
    """
    parts: list[str] = []
    if result["queued"]:
        parts.append(f"入队打包 {len(result['queued'])} 件")
    if result.get("refiling"):
        parts.append(f"入队重算路径 {len(result['refiling'])} 件")
    if result["moved"]:
        parts.append(f"移动文件 {len(result['moved'])} 件")
    if result["unchanged"]:
        parts.append(f"{len(result['unchanged'])} 件已在正确位置")
    if result["skipped"]:
        parts.append(f"跳过 {len(result['skipped'])} 件")
    prefix = "试跑（未执行）：" if result.get("dry_run") else ""
    if not parts:
        return (
            f"{prefix}已检查 {result['scanned']} 件作品，没有需要重新归档的"
        )
    return (
        f"{prefix}已检查 {result['scanned']} 件作品：" + "、".join(parts)
    )


@router.post("/archive-settings/paths/rearchive")
async def rearchive_archive_paths(
    request: Request,
    csrf_token: str = Form(),
    force: str | None = Form(default=None),
    dry_run: str | None = Form(default=None),
):
    """一键重新归档: re-file the library onto the current path rules.

    Lives on the tab that owns the template, because the template is the change
    this action applies -- an operator who edits the layout wants the books that
    are already in the library to follow it, and the batch repack on
    `/downloaded` only ever reached a selection they had to pick by hand.

    `force` is a checkbox, hence `str | None`: an unchecked box sends nothing, so
    the default scope is what absence means -- works that are not archived yet,
    plus works whose recomputed path differs. It is a checkbox rather than
    `ui.confirm` because the label it carries is the whole of the decision, and a
    dialog would say it a second time on a page whose other confirm is reserved
    for a delete.

    `dry_run` is 试跑: plan, report, and do nothing. In AI mode it is also the
    cost warning 强制 needs -- 「全库重新询问」 is one press away from a very large
    bill, and this is how the operator reads the list first.

    Answers by re-rendering this tab with the run's outcome rather than with a 303
    and a flash line: the per-work reasons are what the operator acts on, and a
    notice slot is one sentence long. The same shape the rule 试跑 uses.
    """
    redirect = deps.require_authenticated(request)
    if redirect:
        return redirect
    deps.validate_csrf(request, csrf_token)
    result = await rearchive_works(
        deps.database(request),
        deps.archived_work_service(request),
        deps.conversion_service(request),
        force=force is not None,
        dry_run=dry_run is not None,
        operator_name=str(request.session.get("username") or "admin"),
    )
    # The event is a signal, not a payload: a page subscribed to `download`
    # re-reads the snapshot whatever it carries, so this one tells the open
    # tabs that the library moved without naming a work.
    request.app.state.event_bus.publish(EVENT_DOWNLOAD)
    return await render_settings(
        request,
        SETTINGS_PATHS,
        notice=_rearchive_notice(result),
        rearchive=result,
    )


#: The book a layout template is previewed against. Fixed rather than taken
#: from the queue, for two reasons: a preview has to be reproducible, and the
#: interesting half of the answer is what happens to characters a filesystem
#: will not take. So the English sample title carries a colon and a slash on
#: purpose -- those are the characters that make a real upload's English title
#: unusable as a filename, and an operator who sees them come back replaced has
#: learned both the sanitising and why 日文标题 is the default.
LIBRARY_TEMPLATE_SAMPLE: dict[str, str] = {
    "category": "同人志",
    "artist": "示例作者",
    "japanese_title": "サンプル作品",
    "english_title": "Sample Work: Vol.1/2",
}


def _render_template_preview(
    template: str,
    title_source: str,
    limits: LibraryLimits,
) -> dict[str, object]:
    """Render the sample book's path, exactly as the packer would.

    `limits` is threaded in for the same reason `title_source` is: the preview
    has to answer the way the packer will, and the packer fits the name to the
    filesystem the library sits on.

    `title_source` is threaded in rather than defaulted because the preview's
    whole job is to be the packer's answer: `{title}` resolves through the same
    preference at pack time, and a preview that assumed one language would show
    a path the packer does not produce as soon as the operator picks the other.

    The suffix is appended rather than substituted for the same reason the
    packer appends it: `with_suffix` would read 「Vol. 1」 as a name with a
    `. 1` extension and publish the book as `Vol.cbz`. Reproducing the
    packer's own two lines here keeps the preview from being a second,
    prettier answer.
    """
    values = dict(LIBRARY_TEMPLATE_SAMPLE)
    preferred = (
        values["japanese_title"]
        if title_source == TITLE_SOURCE_JAPANESE
        else values["english_title"]
    )
    relative = render_library_path(
        template,
        {**values, "title": preferred},
        title_fallback="candidate-1",
        limits=limits,
    )
    rendered = (relative.parent / f"{relative.name}{CBZ_SUFFIX}").as_posix()
    return {
        "template": template,
        "rendered": rendered,
        "sample": [
            {
                "label": field_label("Category"),
                "value": LIBRARY_TEMPLATE_SAMPLE["category"],
            },
            {
                "label": field_label("Artist"),
                "value": LIBRARY_TEMPLATE_SAMPLE["artist"],
            },
            {
                "label": field_label("JapaneseTitle"),
                "value": LIBRARY_TEMPLATE_SAMPLE["japanese_title"],
                "note": "当前 {title}" if title_source == TITLE_SOURCE_JAPANESE else None,
            },
            {
                "label": field_label("Title"),
                "value": LIBRARY_TEMPLATE_SAMPLE["english_title"],
                "note": None if title_source == TITLE_SOURCE_JAPANESE else "当前 {title}",
            },
        ],
    }


@router.post("/archive-settings/paths/template/preview")
async def preview_library_template(request: Request, csrf_token: str = Form()):
    """Show what a layout template would produce. Stores nothing.

    The same field the save button submits, sent by the same form to a
    different endpoint, so what was previewed is what gets saved. Preview is
    a convenience and never the gate: `save_library_template` validates
    again, which is what keeps an absolute template or a `..` out of the
    store whether or not this button was pressed.
    """
    redirect = deps.require_authenticated(request)
    if redirect:
        return redirect
    deps.validate_csrf(request, csrf_token)
    form = await request.form()
    raw = str(form.get("library_template") or "")
    try:
        template = validate_library_template(raw)
    except LibraryTemplateError as exc:
        return await render_settings(
            request,
            SETTINGS_PATHS,
            error=exc.public_message,
            status_code=400,
        )
    service = deps.archive_settings_service(request)
    # The filesystem the preview has to fit is the one the *stored* library path
    # points at -- the form being previewed is the template, not the root.
    library_path = (
        await service.library_path() or request.app.state.settings.library_path
    )
    # The radio as submitted, falling back to what is stored, so previewing a
    # preference change shows its effect before it is saved.
    submitted = str(form.get("title_source") or "").strip().lower()
    title_source = (
        submitted
        if submitted in TITLE_SOURCES
        else await service.title_source()
    )
    return await render_settings(
        request,
        SETTINGS_PATHS,
        template_preview=_render_template_preview(
            template, title_source, detect_library_limits(library_path)
        ),
    )


@router.post("/archive-settings/paths/template")
async def save_library_template(request: Request, csrf_token: str = Form()):
    """Store the layout template and the title preference together.

    One endpoint because they are one form: the template says where a book goes
    and the preference says what `{title}` resolves to, and previewing one
    without the other would show a path the packer would not produce. The
    preference is written first so a template refusal does not silently discard
    it -- both are validated independently, and neither can be stored in a state
    the other contradicts.
    """
    redirect = deps.require_authenticated(request)
    if redirect:
        return redirect
    deps.validate_csrf(request, csrf_token)
    form = await request.form()
    try:
        if "title_source" in form:
            await deps.archive_settings_service(request).save_title_source(
                str(form.get("title_source") or "")
            )
        await deps.archive_settings_service(request).save_library_template(
            str(form.get("library_template") or "")
        )
    except ArchiveSettingsError as exc:
        return await render_settings(
            request,
            SETTINGS_PATHS,
            error=exc.public_message,
            status_code=400,
        )
    return settings_redirect(request, SETTINGS_PATHS)


@router.post("/archive-settings/paths/ai")
async def save_ai_path_settings(request: Request, csrf_token: str = Form()):
    """Store the 路径来源 choice and the AI sub-panel in one write.

    One endpoint because it is one form: the radio says whether the model or the
    template decides a path, and the prompt and the toggles only mean anything
    under the AI choice. The numbers are written first because they are the field
    that realistically fails validation, so a rejected value aborts before the
    mode is changed -- the same ordering the archive tab uses for its limits.
    """
    redirect = deps.require_authenticated(request)
    if redirect:
        return redirect
    deps.validate_csrf(request, csrf_token)
    form = await request.form()
    service = deps.archive_settings_service(request)
    try:
        await service.save_ai_batch_size(form.get("ai_batch_size"))
        await service.save_ai_concurrency(form.get("ai_concurrency"))
        await service.save_path_source(str(form.get("path_source") or ""))
        await service.save_ai_prompt(str(form.get("ai_prompt") or ""))
        await service.save_ai_fallback_to_rules(
            form.get("ai_fallback_to_rules") == "on"
        )
        await service.save_ai_stream(form.get("ai_stream") == "on")
        await service.save_ai_default_include_current(
            form.get("ai_default_include_current") == "on"
        )
    except ArchiveSettingsError as exc:
        return await render_settings(
            request,
            SETTINGS_PATHS,
            error=exc.public_message,
            status_code=400,
        )
    return settings_redirect(request, SETTINGS_PATHS)


@router.post("/archive-settings/paths/ai/prompt-default")
async def reset_ai_prompt(request: Request, csrf_token: str = Form()):
    """Restore the reference prompt. Stores blank, which reads back as default.

    Blank rather than a copy of the text: the default lives in
    `app.ai.prompt`, and writing a snapshot of it into the database would freeze
    today's wording for a deployment that upgrades to a better one tomorrow.
    """
    redirect = deps.require_authenticated(request)
    if redirect:
        return redirect
    deps.validate_csrf(request, csrf_token)
    await deps.archive_settings_service(request).save_ai_prompt("")
    return settings_redirect(request, SETTINGS_PATHS)


@router.post("/archive-settings/paths/ai/cache/clear")
async def clear_ai_path_cache(request: Request, csrf_token: str = Form()):
    """Drop the answer cache and say how much was dropped.

    Renders in place rather than redirecting so the count lands in the notice
    slot: 「已清除 0 条」 and 「已清除 37 条」 are different facts, and a flash
    line that always says 「已完成」 would hide which one happened.
    """
    redirect = deps.require_authenticated(request)
    if redirect:
        return redirect
    deps.validate_csrf(request, csrf_token)
    removed = await deps.database(request).clear_ai_path_suggestions()
    return await render_settings(
        request,
        SETTINGS_PATHS,
        notice=(
            f"已清除 {removed} 条 AI 路径缓存；再次打包时会重新询问模型。"
            if removed
            else "缓存本来就是空的，没有可清除的 AI 路径记录。"
        ),
    )


def _error_text(exc: Exception) -> str:
    """The operator-facing text for a refused settings change.

    `ArchiveSettingsError` carries its own phrasing; the rule engine raises
    plain `ValueError` subclasses whose `str` is the message. One helper keeps
    the two in the same error slot on the page.
    """
    if isinstance(exc, ArchiveSettingsError):
        return exc.public_message
    return str(exc)


@router.post("/archive-settings/paths/rules")
async def save_archive_path_rule(request: Request):
    """Create a routing rule, or overwrite the one `path_rule_id` names.

    One endpoint for both because it is one form: the editor renders with the
    fields filled when editing and blank when creating, and the only difference
    on the wire is a hidden `path_rule_id`. The condition is the same row-based
    DSL the auto-approval tab uses (`parse_rule_condition`), validated through
    the engine's own `validate_rule_ast`; the path template is validated the
    same way as the global one (`validate_library_template`, inside the service),
    so a rule that could not render is refused while the operator is watching
    the page rather than at pack time.
    """
    redirect = deps.require_authenticated(request)
    if redirect:
        return redirect
    form = await request.form()
    deps.validate_csrf(request, str(form.get("csrf_token") or ""))
    try:
        name = str(form.get("name") or "").strip()
        if not name:
            raise RuleValidationError("规则名称不能为空")
        priority = int(str(form.get("priority") or "100"))
        # Absent means create. An unparsable one is a refusal rather than a
        # fallback to create: silently inserting a second rule when an edit was
        # meant is how an operator ends up with two rules routing everything.
        raw_rule_id = str(form.get("path_rule_id") or "").strip()
        rule_id = int(raw_rule_id) if raw_rule_id else None
        condition = parse_rule_condition(form)
        if condition is None:
            raise RuleValidationError("请至少填写一个条件")
        condition = validate_rule_ast(condition)
        await deps.archive_settings_service(request).save_path_rule(
            rule_id=rule_id,
            name=name,
            enabled=form.get("enabled") == "on",
            priority=priority,
            condition=condition,
            path_template=str(form.get("path_template") or ""),
            case_sensitive=form.get("case_sensitive") == "on",
        )
    except LookupError:
        # The rule was deleted between the page render and the save. Reported as
        # a 404 rather than re-created under its old id, which would resurrect a
        # rule the operator had removed.
        raise HTTPException(status_code=404, detail="规则不存在") from None
    except (ArchiveSettingsError, RuleValidationError, ValueError, json.JSONDecodeError) as exc:
        return await render_settings(
            request,
            SETTINGS_PATHS,
            error=_error_text(exc),
            status_code=400,
        )
    return settings_redirect(request, SETTINGS_PATHS)


@router.post("/archive-settings/paths/rules/dry-run")
async def dry_run_archive_path_rule(request: Request):
    """Report which works the edited rule would route. Writes nothing.

    The same fields the save button submits, sent to a different endpoint by
    the same form, so what was tried is what gets saved. The condition is
    validated first: a trial run of an unusable rule would report 「命中 0」 and
    read as 「这条规则没用」 rather than 「这条规则写错了」.
    """
    redirect = deps.require_authenticated(request)
    if redirect:
        return redirect
    form = await request.form()
    deps.validate_csrf(request, str(form.get("csrf_token") or ""))
    try:
        condition = parse_rule_condition(form)
        if condition is None:
            raise RuleValidationError("请至少填写一个条件")
        condition = validate_rule_ast(condition)
    except (RuleValidationError, ValueError) as exc:
        return await render_settings(
            request,
            SETTINGS_PATHS,
            error=str(exc),
            status_code=400,
        )
    result = await AutomaticApprovalService(deps.database(request)).dry_run(
        condition, case_sensitive=form.get("case_sensitive") == "on"
    )
    return await render_settings(
        request,
        SETTINGS_PATHS,
        dry_run=auto_approval_dry_run(result),
    )


@router.get("/archive-settings/paths/rules/{rule_id}/edit")
async def edit_archive_path_rule(rule_id: int, request: Request):
    """Render the 路径 tab with this routing rule loaded into the editor.

    A GET, so 编辑 is a link an operator can open in a new tab and the URL says
    what is being edited. It renders the same tab through `render_settings`
    rather than a form of its own -- there is one editor, and a second copy
    filled from a stored rule is how the two would drift.

    `editor_rows` returns None for a nested condition group, which the flat
    editor cannot represent. That is passed through as `edit_unsupported` rather
    than as an error: the tab still renders, the rule is still listed, and the
    page explains that this one has to be replaced rather than edited.
    """
    redirect = deps.require_authenticated(request)
    if redirect:
        return redirect
    rule = await deps.database(request).get_archive_path_rule(rule_id)
    if rule is None:
        raise HTTPException(status_code=404, detail="规则不存在")
    decomposed = editor_rows(rule.condition)
    if decomposed is None:
        return await render_settings(
            request,
            SETTINGS_PATHS,
            edit_rule_id=rule_id,
            edit_unsupported=True,
        )
    group_operator, rows = decomposed
    return await render_settings(
        request,
        SETTINGS_PATHS,
        edit_rule_id=rule_id,
        edit_rule={
            "rule_id": rule.rule_id,
            "name": rule.name,
            "priority": rule.priority,
            "enabled": rule.enabled,
            "group_operator": group_operator,
            "case_sensitive": rule.case_sensitive,
            "path_template": rule.path_template,
            "rows": list(rows),
        },
    )


@router.post("/archive-settings/paths/rules/{rule_id}/toggle")
async def toggle_archive_path_rule(rule_id: int, request: Request):
    redirect = deps.require_authenticated(request)
    if redirect:
        return redirect
    form = await request.form()
    deps.validate_csrf(request, str(form.get("csrf_token") or ""))
    await deps.database(request).set_archive_path_rule_enabled(
        rule_id, form.get("enabled") == "on"
    )
    return settings_redirect(request, SETTINGS_PATHS)


@router.post("/archive-settings/paths/rules/{rule_id}/delete")
async def delete_archive_path_rule(rule_id: int, request: Request):
    """Delete a routing rule for good.

    A POST behind `ui.confirm`, because it is the one action on this panel that
    cannot be undone from the interface -- 停用 is the reversible half and is
    deliberately still its own button, so an operator parking a rule for an
    afternoon is never pushed toward deleting it.
    """
    redirect = deps.require_authenticated(request)
    if redirect:
        return redirect
    form = await request.form()
    deps.validate_csrf(request, str(form.get("csrf_token") or ""))
    try:
        await deps.database(request).delete_archive_path_rule(rule_id)
    except LookupError:
        raise HTTPException(status_code=404, detail="规则不存在") from None
    return settings_redirect(request, SETTINGS_PATHS)


@router.post("/settings/parse")
async def save_parse_rules(request: Request):
    """Store the candidate-admission parse scheme.

    Every checkbox is read explicitly rather than defaulted through: an
    unchecked box sends nothing, so absence means off, and letting the
    validator fill in a default for an omitted key would make unchecking a rule
    silently keep it on.
    """
    redirect = deps.require_authenticated(request)
    if redirect:
        return redirect
    form = await request.form()
    deps.validate_csrf(request, str(form.get("csrf_token") or ""))
    submitted_formats = set(form.getlist("archive_formats"))
    raw = {
        "require_gallery_link": form.get("require_gallery_link") == "on",
        "accept_photo": form.get("accept_photo") == "on",
        "accept_archive": form.get("accept_archive") == "on",
        "accept_preview": form.get("accept_preview") == "on",
        "title_required": form.get("title_required") == "on",
        "archive_formats": [
            archive_format
            for archive_format in PARSE_ARCHIVE_FORMATS
            if archive_format in submitted_formats
        ],
    }
    try:
        await deps.system_settings_service(request).save_parse_rules(raw)
    except SystemSettingsError as exc:
        return await render_settings(
            request, SETTINGS_PARSE, error=exc.public_message, status_code=400
        )
    return settings_redirect(request, SETTINGS_PARSE)


@router.post("/settings/parse/reset")
async def reset_parse_rules(request: Request, csrf_token: str = Form()):
    """Put the shipped scheme back, so a bad experiment is one click from gone."""
    redirect = deps.require_authenticated(request)
    if redirect:
        return redirect
    deps.validate_csrf(request, csrf_token)
    await deps.system_settings_service(request).reset_parse_rules()
    return settings_redirect(request, SETTINGS_PARSE)


@router.post("/settings/parse/ai")
async def save_candidate_admission(request: Request):
    """Store the AI candidate gate's switches and prompt.

    A prompt submitted byte-identical to the shipped default is stored as an
    empty string, i.e. 「use the default」: otherwise virtually every save of
    this form would pin the default text as an override, and the page could
    never say whether the operator had actually customised it.
    """
    redirect = deps.require_authenticated(request)
    if redirect:
        return redirect
    form = await request.form()
    deps.validate_csrf(request, str(form.get("csrf_token") or ""))
    prompt = str(form.get("ai_candidate_prompt") or "").strip()
    if prompt == DEFAULT_CANDIDATE_PROMPT.strip():
        prompt = ""
    try:
        await deps.system_settings_service(request).save_candidate_admission(
            {
                "ai_candidate_enabled": form.get("ai_candidate_enabled") == "on",
                "ai_candidate_override_parse_rules": (
                    form.get("ai_candidate_override_parse_rules") == "on"
                ),
                "ai_candidate_prompt": prompt,
                "ai_candidate_fallback": str(
                    form.get("ai_candidate_fallback") or ""
                ),
            }
        )
    except SystemSettingsError as exc:
        return await render_settings(
            request, SETTINGS_PARSE, error=exc.public_message, status_code=400
        )
    return settings_redirect(request, SETTINGS_PARSE)


#: The 系统 tab's only writer, and the one settings endpoint with no legacy
#: path to inherit -- its preferences had no page before R8 -- so it
#: is named for where it lives rather than for a retired form.
@router.post("/settings/system")
async def save_system_settings(request: Request, csrf_token: str = Form()):
    """Store the system preferences and make them current.

    The display timezone is cached on `app.state`, while the logging level is
    process state, so both are applied immediately after a successful write.
    Cadences are read through the settings service per job or per sweep and need
    no explicit refresh here.
    """
    redirect = deps.require_authenticated(request)
    if redirect:
        return redirect
    deps.validate_csrf(request, csrf_token)
    form = await request.form()
    values = {
        key: str(form.get(key) or "")
        for key in (
            "source_concurrency",
            "poll_interval_ms",
            "timezone",
            "auto_approval_interval_minutes",
            "mobile_access_ttl_seconds",
            "mobile_refresh_ttl_seconds",
            "log_level",
        )
        if key in form
    }
    # The priority is submitted as one rank per source, which is the shape a
    # form can express without JavaScript; the service stores the single
    # ordered string, because that is the shape the router reads.
    ranks: dict[int, str] = {}
    for code in AUTO_DOWNLOAD_PROVIDERS:
        field = f"priority_{code}"
        if field not in form:
            continue
        try:
            rank = int(str(form.get(field) or "").strip())
        except ValueError:
            rank = 0
        if (
            rank < 1
            or rank > len(AUTO_DOWNLOAD_PROVIDERS)
            or rank in ranks
        ):
            return await render_settings(
                request,
                SETTINGS_SYSTEM,
                error="下载来源优先级必须是 1–4 且每个来源各一个顺位",
                status_code=400,
            )
        ranks[rank] = code
    if ranks:
        if len(ranks) != len(AUTO_DOWNLOAD_PROVIDERS):
            return await render_settings(
                request,
                SETTINGS_SYSTEM,
                error="每个下载来源都要选一个顺位",
                status_code=400,
            )
        values["download_source_priority"] = ",".join(
            ranks[rank] for rank in range(1, len(ranks) + 1)
        )
    try:
        await deps.system_settings_service(request).save(values)
    except SystemSettingsError as exc:
        return await render_settings(
            request,
            SETTINGS_SYSTEM,
            error=exc.public_message,
            status_code=400,
        )
    await deps.refresh_display_timezone(request)
    await deps.refresh_download_source_priority(request)
    apply_runtime_log_level(
        await deps.system_settings_service(request).log_level()
    )
    return settings_redirect(request, SETTINGS_SYSTEM)


@router.post("/archive-settings/torrent")
async def save_torrent_client_settings(
    request: Request, csrf_token: str = Form()
):
    redirect = deps.require_authenticated(request)
    if redirect:
        return redirect
    deps.validate_csrf(request, csrf_token)
    form = await request.form()
    try:
        await deps.archive_settings_service(request).save_torrent_client(
            {
                "base_url": form.get("base_url"),
                "username": form.get("username"),
                "password": form.get("password"),
                "category": form.get("category"),
                "save_path": form.get("save_path"),
                "local_save_path": form.get("local_save_path"),
                "keep_seeding": bool(form.get("keep_seeding")),
                "auto_pack": bool(form.get("auto_pack")),
            }
        )
    except ArchiveSettingsError as exc:
        return await render_settings(
            request,
            SETTINGS_ARCHIVE,
            error=exc.public_message,
            status_code=400,
        )
    return settings_redirect(request, SETTINGS_ARCHIVE)


@router.post("/archive-settings/torrent-test")
async def test_torrent_client(request: Request, csrf_token: str = Form()):
    """Prove the stored settings reach a real client before a book needs it."""
    redirect = deps.require_authenticated(request)
    if redirect:
        return redirect
    deps.validate_csrf(request, csrf_token)
    try:
        version = await deps.torrent_service(request).check_connection()
    except TorrentError as exc:
        return await render_settings(
            request,
            SETTINGS_ARCHIVE,
            error=exc.public_message,
            status_code=400,
        )
    return await render_settings(
        request,
        SETTINGS_ARCHIVE,
        notice=f"qBittorrent 连通，版本 {version}",
    )


@router.post("/archive-settings/limits")
async def save_archive_limits(request: Request, csrf_token: str = Form()):
    redirect = deps.require_authenticated(request)
    if redirect:
        return redirect
    deps.validate_csrf(request, csrf_token)
    form = await request.form()
    try:
        # Limits are typed by hand and are the field that realistically
        # fails validation, so they are stored first: a rejected number
        # aborts before the quality level is touched.
        await deps.archive_settings_service(request).save_limits(
            {key: str(form.get(key) or "") for key in ARCHIVE_LIMIT_KEYS}
        )
        await deps.archive_settings_service(request).save_keep_original(
            form.get("keep_original") == "on"
        )
        await deps.archive_settings_service(request).save_image_quality(
            str(form.get("image_quality") or "")
        )
    except ArchiveSettingsError as exc:
        return await render_settings(
            request,
            SETTINGS_ARCHIVE,
            error=exc.public_message,
            status_code=400,
        )
    return settings_redirect(request, SETTINGS_ARCHIVE)


@router.post("/archive-settings/profiles/{name}")
async def save_archive_tool_profile(
    request: Request, name: str, csrf_token: str = Form()
):
    redirect = deps.require_authenticated(request)
    if redirect:
        return redirect
    deps.validate_csrf(request, csrf_token)
    form = await request.form()
    timeout_raw = str(form.get("timeout_seconds") or "").strip()
    try:
        timeout_seconds = int(timeout_raw) if timeout_raw else None
    except ValueError:
        return await render_settings(
            request,
            SETTINGS_ARCHIVE,
            error="超时时长必须是整数",
            status_code=400,
        )
    executable_raw = str(form.get("executable_path") or "").strip()
    try:
        await deps.archive_settings_service(request).set_profile_state(
            name,
            enabled=form.get("enabled") == "on",
            executable_path=executable_raw or None,
            timeout_seconds=timeout_seconds,
        )
    except ArchiveSettingsError as exc:
        return await render_settings(
            request,
            SETTINGS_ARCHIVE,
            error=exc.public_message,
            status_code=400,
        )
    return settings_redirect(request, SETTINGS_ARCHIVE)


@router.post("/archive-settings/toolchain/install")
async def install_archive_toolchain(
    request: Request, csrf_token: str = Form()
):
    redirect = deps.require_authenticated(request)
    if redirect:
        return redirect
    deps.validate_csrf(request, csrf_token)
    try:
        await deps.archive_settings_service(request).install_toolchain(force=True)
    except ArchiveSettingsError as exc:
        return await render_settings(
            request,
            SETTINGS_ARCHIVE,
            error=exc.public_message,
            status_code=400,
        )
    return settings_redirect(request, SETTINGS_ARCHIVE)


@router.post("/archive-settings/passwords")
async def add_archive_password(request: Request, csrf_token: str = Form()):
    redirect = deps.require_authenticated(request)
    if redirect:
        return redirect
    deps.validate_csrf(request, csrf_token)
    form = await request.form()
    priority_raw = str(form.get("priority") or "100").strip()
    try:
        priority = int(priority_raw)
    except ValueError:
        return await render_settings(
            request,
            SETTINGS_PASSWORDS,
            error="优先级必须是整数",
            status_code=400,
        )
    try:
        await deps.archive_settings_service(request).add_password(
            name=str(form.get("name") or ""),
            password=str(form.get("password") or ""),
            priority=priority,
            enabled=form.get("enabled") == "on",
        )
    except ArchiveSettingsError as exc:
        return await render_settings(
            request,
            SETTINGS_PASSWORDS,
            error=exc.public_message,
            status_code=400,
        )
    return settings_redirect(request, SETTINGS_PASSWORDS)


#: The single mobile API key. It shares the 密码库 tab with the archive
#: passwords because it is the same kind of thing -- a credential the operator
#: stores once -- and it is minted here rather than through the mobile API so a
#: leaked client can never mint a replacement for itself.
@router.post("/settings/api-keys/generate")
async def generate_api_key(request: Request, csrf_token: str = Form()):
    redirect = deps.require_authenticated(request)
    if redirect:
        return redirect
    deps.validate_csrf(request, csrf_token)
    database = deps.database(request)
    token, public_id, secret = new_token(KIND_API_KEY)
    # One transaction: revoke the old key and insert the new one together, so
    # the partial unique index can never see two valid keys (a double-submitted
    # form would otherwise lose the race with a constraint error).
    await database.replace_api_key(
        label="移动端",
        public_id=public_id,
        secret_hash=hash_secret(secret),
    )
    # Shown once, by the GET that follows this redirect; see
    # `app/web/settings_view.py`. Never in the cookie, the URL or the database.
    request.app.state.pending_api_key = token
    target = request.url_for("settings_section", section=SETTINGS_PASSWORDS)
    return RedirectResponse(f"{target.path}?reveal=1", status_code=303)


@router.post("/settings/api-keys/revoke")
async def revoke_api_key(request: Request, csrf_token: str = Form()):
    redirect = deps.require_authenticated(request)
    if redirect:
        return redirect
    deps.validate_csrf(request, csrf_token)
    await deps.database(request).revoke_active_api_key()
    return settings_redirect(request, SETTINGS_PASSWORDS)


@router.post("/archive-settings/passwords/{password_id}/delete")
async def delete_archive_password(
    request: Request, password_id: int, csrf_token: str = Form()
):
    redirect = deps.require_authenticated(request)
    if redirect:
        return redirect
    deps.validate_csrf(request, csrf_token)
    await deps.archive_settings_service(request).delete_password(password_id)
    return settings_redirect(request, SETTINGS_PASSWORDS)


# ---------------------------------------------------------------------------
#  AI 供应商 (SETTINGS_AI)
# ---------------------------------------------------------------------------

def _ai_form_values(form) -> dict[str, object]:
    """The provider form as the service's validator wants it.

    A thin translation and nothing else: the refusals live in the service so the
    page, the JSON client and the chain editor all enforce one set of rules.
    `enabled` is a checkbox, so absence is 「off」.
    """
    return {
        "provider_id": form.get("provider_id"),
        "name": form.get("name"),
        "code": form.get("code"),
        "base_url": form.get("base_url"),
        "timeout_seconds": form.get("timeout_seconds"),
        "max_retries": form.get("max_retries"),
        "enabled": form.get("enabled") == "on",
        "custom_headers": form.get("custom_headers"),
        "default_params": form.get("default_params"),
    }


def _ai_location(provider_id: object = None) -> str:
    """The AI tab, optionally with one provider selected.

    Back to the row the operator was editing rather than to the top of a list:
    the right pane is a query parameter, so 「保存后回到哪一个供应商」 is part of
    the URL and a reload keeps the answer.
    """
    path = f"/settings/{SETTINGS_AI}"
    text = str(provider_id or "").strip()
    if text.isdigit():
        return f"{path}?provider={text}"
    return path


async def _ai_refused(request: Request, exc: AiError):
    """Re-render the AI tab with the refusal.

    The selected provider survives because it travels in the POST URL's query
    string (`?provider=3`), not in a hidden field: the page is rendered from the
    same snapshot either way, and one source for 「在看哪一个供应商」 is one source
    to get wrong.
    """
    return await render_settings(
        request, SETTINGS_AI, error=exc.public_message, status_code=400
    )


@router.post("/settings/ai/providers")
async def save_ai_provider(request: Request, csrf_token: str = Form()):
    redirect = deps.require_authenticated(request)
    if redirect:
        return redirect
    deps.validate_csrf(request, csrf_token)
    form = await request.form()
    try:
        provider = await deps.ai_service(request).save_provider(
            _ai_form_values(form)
        )
    except AiError as exc:
        return await _ai_refused(request, exc)
    return RedirectResponse(
        _ai_location(provider.provider_id), status_code=303
    )


@router.post("/settings/ai/providers/{provider_id}/delete")
async def delete_ai_provider(
    request: Request, provider_id: int, csrf_token: str = Form()
):
    redirect = deps.require_authenticated(request)
    if redirect:
        return redirect
    deps.validate_csrf(request, csrf_token)
    try:
        await deps.ai_service(request).delete_provider(provider_id)
    except AiError as exc:
        return await _ai_refused(request, exc)
    return RedirectResponse(_ai_location(), status_code=303)


@router.post("/settings/ai/providers/{provider_id}/keys")
async def add_ai_provider_key(
    request: Request, provider_id: int, csrf_token: str = Form()
):
    """Store one or more API keys from a textarea, one per line.

    The field is a plain textarea and its value is never echoed back: not in
    the list, not in a value attribute, and (`_ai_refused` renders the page
    fresh) not in an error message either.
    """
    redirect = deps.require_authenticated(request)
    if redirect:
        return redirect
    deps.validate_csrf(request, csrf_token)
    form = await request.form()
    try:
        await deps.ai_service(request).add_keys(
            provider_id, str(form.get("api_keys") or "")
        )
    except AiError as exc:
        return await _ai_refused(request, exc)
    return RedirectResponse(_ai_location(provider_id), status_code=303)


@router.post("/settings/ai/keys/{key_id}/toggle")
async def toggle_ai_provider_key(
    request: Request, key_id: int, csrf_token: str = Form()
):
    redirect = deps.require_authenticated(request)
    if redirect:
        return redirect
    deps.validate_csrf(request, csrf_token)
    form = await request.form()
    try:
        await deps.ai_service(request).set_key_enabled(
            key_id, str(form.get("enabled") or "") == "on"
        )
    except AiError as exc:
        return await _ai_refused(request, exc)
    return RedirectResponse(_ai_location(), status_code=303)


@router.post("/settings/ai/keys/{key_id}/reset")
async def reset_ai_provider_key(
    request: Request, key_id: int, csrf_token: str = Form()
):
    redirect = deps.require_authenticated(request)
    if redirect:
        return redirect
    deps.validate_csrf(request, csrf_token)
    try:
        await deps.ai_service(request).reset_key_cooldown(key_id)
    except AiError as exc:
        return await _ai_refused(request, exc)
    return RedirectResponse(_ai_location(), status_code=303)


@router.post("/settings/ai/keys/{key_id}/delete")
async def delete_ai_provider_key(
    request: Request, key_id: int, csrf_token: str = Form()
):
    redirect = deps.require_authenticated(request)
    if redirect:
        return redirect
    deps.validate_csrf(request, csrf_token)
    try:
        await deps.ai_service(request).delete_key(key_id)
    except AiError as exc:
        return await _ai_refused(request, exc)
    return RedirectResponse(_ai_location(), status_code=303)


@router.post("/settings/ai/providers/{provider_id}/models")
async def add_ai_provider_models(
    request: Request, provider_id: int, csrf_token: str = Form()
):
    """Add one or more model names.

    Repeated `model_name` fields because the 「拉取模型」 result arrives as a
    checkbox list and the manual field is the same form: ticking five models is
    one request, not five.
    """
    redirect = deps.require_authenticated(request)
    if redirect:
        return redirect
    deps.validate_csrf(request, csrf_token)
    form = await request.form()
    names = [str(raw) for raw in form.getlist("model_name")]
    try:
        await deps.ai_service(request).add_models(provider_id, names)
    except AiError as exc:
        return await _ai_refused(request, exc)
    return RedirectResponse(_ai_location(provider_id), status_code=303)


@router.post("/settings/ai/providers/{provider_id}/models/fetch")
async def fetch_ai_provider_models(
    request: Request, provider_id: int, csrf_token: str = Form()
):
    """`GET /v1/models`, rendered as a checklist for the operator to pick from.

    Fetched names are not added, and not tested: a listing says a model exists,
    not that this deployment can talk to it.
    """
    redirect = deps.require_authenticated(request)
    if redirect:
        return redirect
    deps.validate_csrf(request, csrf_token)
    try:
        names = await deps.ai_service(request).list_remote_models(provider_id)
    except AiError as exc:
        return await _ai_refused(request, exc)
    if not names:
        return await render_settings(
            request, SETTINGS_AI, notice="供应商没有返回任何模型名称。"
        )
    return await render_settings(
        request,
        SETTINGS_AI,
        notice=f"拉到 {len(names)} 个模型名称，勾选后点「添加所选」。",
        discovered={"provider_id": provider_id, "names": list(names)},
    )


@router.post("/settings/ai/models/{model_id}/toggle")
async def toggle_ai_provider_model(
    request: Request, model_id: int, csrf_token: str = Form()
):
    redirect = deps.require_authenticated(request)
    if redirect:
        return redirect
    deps.validate_csrf(request, csrf_token)
    form = await request.form()
    try:
        await deps.ai_service(request).set_model_enabled(
            model_id, str(form.get("enabled") or "") == "on"
        )
    except AiError as exc:
        return await _ai_refused(request, exc)
    return RedirectResponse(_ai_location(), status_code=303)


@router.post("/settings/ai/models/{model_id}/params")
async def save_ai_provider_model_params(
    request: Request, model_id: int, csrf_token: str = Form()
):
    redirect = deps.require_authenticated(request)
    if redirect:
        return redirect
    deps.validate_csrf(request, csrf_token)
    form = await request.form()
    try:
        await deps.ai_service(request).save_model_params(
            model_id, form.get("params")
        )
    except AiError as exc:
        return await _ai_refused(request, exc)
    return RedirectResponse(_ai_location(), status_code=303)


@router.post("/settings/ai/models/{model_id}/delete")
async def delete_ai_provider_model(
    request: Request, model_id: int, csrf_token: str = Form()
):
    redirect = deps.require_authenticated(request)
    if redirect:
        return redirect
    deps.validate_csrf(request, csrf_token)
    try:
        await deps.ai_service(request).delete_model(model_id)
    except AiError as exc:
        return await _ai_refused(request, exc)
    return RedirectResponse(_ai_location(), status_code=303)


@router.post("/settings/ai/models/{model_id}/verify")
async def verify_ai_provider_model(
    request: Request, model_id: int, csrf_token: str = Form()
):
    """Send the minimal chat request and store what came back.

    A failure answers 400 with the provider's own words, the same shape the
    qBittorrent 连通测试 uses: the operator asked a question, and the answer is
    「不，原因是 …」 rather than a redirect that hides it.
    """
    redirect = deps.require_authenticated(request)
    if redirect:
        return redirect
    deps.validate_csrf(request, csrf_token)
    try:
        verification = await deps.ai_service(request).verify_model(model_id)
    except AiError as exc:
        return await _ai_refused(request, exc)
    if not verification.ok:
        return await render_settings(
            request,
            SETTINGS_AI,
            error=f"验证失败：{verification.message}",
            status_code=400,
        )
    return await render_settings(request, SETTINGS_AI, notice=verification.message)


@router.post("/settings/ai/providers/{provider_id}/verify")
async def verify_ai_provider_models(
    request: Request, provider_id: int, csrf_token: str = Form()
):
    """Test every enabled model of one provider, in order."""
    redirect = deps.require_authenticated(request)
    if redirect:
        return redirect
    deps.validate_csrf(request, csrf_token)
    try:
        results = await deps.ai_service(request).verify_all(provider_id)
    except AiError as exc:
        return await _ai_refused(request, exc)
    return await render_settings(
        request, SETTINGS_AI, notice=_verify_summary(results)
    )


@router.post("/settings/ai/verify")
async def verify_all_ai_models(request: Request, csrf_token: str = Form()):
    """Test every enabled model of every provider, in order."""
    redirect = deps.require_authenticated(request)
    if redirect:
        return redirect
    deps.validate_csrf(request, csrf_token)
    try:
        results = await deps.ai_service(request).verify_all()
    except AiError as exc:
        return await _ai_refused(request, exc)
    return await render_settings(
        request, SETTINGS_AI, notice=_verify_summary(results)
    )


def _verify_summary(results) -> str:
    ok = sum(1 for item in results if item.ok)
    if not results:
        return "没有启用的模型可测试。"
    return f"测试完成：{ok}/{len(results)} 个模型通过。"


@router.post("/settings/ai/master")
async def save_ai_master_switch(request: Request, csrf_token: str = Form()):
    """Toggle the global AI master switch.

    State only: turning it off stops every feature from calling a model and
    deletes nothing -- providers, keys, chains and per-feature switches all stay
    where the operator left them, so turning it back on restores the exact
    configuration that was there before.
    """
    redirect = deps.require_authenticated(request)
    if redirect:
        return redirect
    deps.validate_csrf(request, csrf_token)
    form = await request.form()
    await deps.system_settings_service(request).save_ai_enabled(
        form.get("ai_enabled") == "on"
    )
    return settings_redirect(request, SETTINGS_AI)


async def _chain_model_id(request: Request, form) -> int:
    raw = str(form.get("model_id") or "").strip()
    if not raw.isdigit():
        raise AiError(AI_CHAIN_ENTRY_MISSING, "请选择一个模型")
    return int(raw)


@router.post("/settings/ai/chain/primary")
async def set_ai_chain_primary(request: Request, csrf_token: str = Form()):
    return await _chain_action(request, csrf_token, "primary")


@router.post("/settings/ai/chain/append")
async def append_ai_chain_model(request: Request, csrf_token: str = Form()):
    return await _chain_action(request, csrf_token, "append")


@router.post("/settings/ai/chain/shift")
async def shift_ai_chain_model(request: Request, csrf_token: str = Form()):
    return await _chain_action(request, csrf_token, "shift")


@router.post("/settings/ai/chain/remove")
async def remove_ai_chain_model(request: Request, csrf_token: str = Form()):
    return await _chain_action(request, csrf_token, "remove")


@router.post("/settings/paths/chain/primary")
async def set_ai_path_chain_primary(request: Request, csrf_token: str = Form()):
    return await _chain_action(
        request, csrf_token, "primary", scope=CHAIN_SCOPE_ARCHIVE_PATH
    )


@router.post("/settings/paths/chain/append")
async def append_ai_path_chain_model(request: Request, csrf_token: str = Form()):
    return await _chain_action(
        request, csrf_token, "append", scope=CHAIN_SCOPE_ARCHIVE_PATH
    )


@router.post("/settings/paths/chain/shift")
async def shift_ai_path_chain_model(request: Request, csrf_token: str = Form()):
    return await _chain_action(
        request, csrf_token, "shift", scope=CHAIN_SCOPE_ARCHIVE_PATH
    )


@router.post("/settings/paths/chain/remove")
async def remove_ai_path_chain_model(request: Request, csrf_token: str = Form()):
    return await _chain_action(
        request, csrf_token, "remove", scope=CHAIN_SCOPE_ARCHIVE_PATH
    )


@router.post("/settings/parse/chain/primary")
async def set_ai_candidate_chain_primary(request: Request, csrf_token: str = Form()):
    return await _chain_action(
        request, csrf_token, "primary", scope=CHAIN_SCOPE_CANDIDATE
    )


@router.post("/settings/parse/chain/append")
async def append_ai_candidate_chain_model(request: Request, csrf_token: str = Form()):
    return await _chain_action(
        request, csrf_token, "append", scope=CHAIN_SCOPE_CANDIDATE
    )


@router.post("/settings/parse/chain/shift")
async def shift_ai_candidate_chain_model(request: Request, csrf_token: str = Form()):
    return await _chain_action(
        request, csrf_token, "shift", scope=CHAIN_SCOPE_CANDIDATE
    )


@router.post("/settings/parse/chain/remove")
async def remove_ai_candidate_chain_model(request: Request, csrf_token: str = Form()):
    return await _chain_action(
        request, csrf_token, "remove", scope=CHAIN_SCOPE_CANDIDATE
    )


@router.post("/settings/parse/model-source")
async def save_ai_candidate_model_source(request: Request, csrf_token: str = Form()):
    """Choose where the AI candidate gate gets its models from.

    The same two values and the same wording as the archive-path switch:
    「跟随全局默认」 reads the AI tab's list, 「本页单独指定」 reads the one edited
    on this tab. The choice is stored rather than inferred from 「列表是不是空的」
    so an intentionally empty custom list stays a loud error.
    """
    redirect = deps.require_authenticated(request)
    if redirect:
        return redirect
    deps.validate_csrf(request, csrf_token)
    form = await request.form()
    try:
        await deps.system_settings_service(
            request
        ).save_ai_candidate_model_source(str(form.get("ai_model_source") or ""))
    except SystemSettingsError as exc:
        return await render_settings(
            request, SETTINGS_PARSE, error=exc.public_message, status_code=400
        )
    return settings_redirect(request, SETTINGS_PARSE)


#: Which settings tab owns each chain scope. One table instead of a ternary in
#: each of the four verbs, because the section and the redirect must agree and
#: a new scope should be one line here rather than two edits that can drift.
_CHAIN_SECTIONS: dict[str, str] = {
    CHAIN_SCOPE_DEFAULT: SETTINGS_AI,
    CHAIN_SCOPE_ARCHIVE_PATH: SETTINGS_PATHS,
    CHAIN_SCOPE_CANDIDATE: SETTINGS_PARSE,
}


async def _chain_action(
    request: Request, csrf_token: str, action: str, *, scope: str = CHAIN_SCOPE_DEFAULT
):
    """One ordered-list verb, for either scope.

    One implementation for the global default and the archive-path override:
    the two editors differ in *which* list they edit and in nothing else, and
    two copies of 「上移/下移」 is how one of them quietly stops supporting the
    last position.
    """
    redirect = deps.require_authenticated(request)
    if redirect:
        return redirect
    deps.validate_csrf(request, csrf_token)
    form = await request.form()
    service = deps.ai_service(request)
    section = _CHAIN_SECTIONS.get(scope, SETTINGS_AI)
    try:
        if action == "shift":
            delta = int(str(form.get("delta") or "").strip())
            await service.shift_chain(
                await _chain_model_id(request, form), delta, scope
            )
        elif action == "remove":
            await service.remove_from_chain(
                await _chain_model_id(request, form), scope
            )
        elif action == "primary":
            await service.set_chain_primary(
                await _chain_model_id(request, form), scope
            )
        else:
            await service.append_to_chain(
                await _chain_model_id(request, form), scope
            )
    except (AiError, ValueError) as exc:
        message = exc.public_message if isinstance(exc, AiError) else "移动方向无效"
        return await render_settings(
            request, section, error=message, status_code=400
        )
    if scope == CHAIN_SCOPE_DEFAULT:
        return RedirectResponse(_ai_location(), status_code=303)
    return RedirectResponse(f"/settings/{section}", status_code=303)


@router.post("/archive-settings/paths/ai/models")
async def save_ai_model_source(request: Request, csrf_token: str = Form()):
    """Choose where the archive-path feature gets its models from.

    Two values and nothing else: 「跟随全局默认」 reads the list on the AI tab,
    「本页单独指定」 reads the one edited here. Storing the choice rather than
    inferring it from 「列表是不是空的」 keeps an intentionally empty custom list
    from silently turning into the default.
    """
    redirect = deps.require_authenticated(request)
    if redirect:
        return redirect
    deps.validate_csrf(request, csrf_token)
    form = await request.form()
    try:
        await deps.archive_settings_service(request).save_ai_model_source(
            str(form.get("ai_model_source") or "")
        )
    except ArchiveSettingsError as exc:
        return await render_settings(
            request, SETTINGS_PATHS, error=str(exc), status_code=400
        )
    return settings_redirect(request, SETTINGS_PATHS)
