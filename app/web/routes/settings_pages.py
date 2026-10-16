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
from app.ai.errors import AiError
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
from app.settings.forms import checkbox, form_values
from app.settings.sources import (
    SourceFieldError,
    parse_source,
    parse_source_rules,
    parse_tags,
    selected_source_ids,
)
from app.settings.source_commands import SourceCommands, SourceCommandError
from app.settings.commands import SettingsCommands, ACTION_SCOPES
from app.settings.credential_commands import CredentialCommands
from app.settings import runtime
from app.review.commands import WorkCommandError
from app.auto_approval.rules import (
    RuleValidationError,
    editor_rows,
    validate_rule_ast,
)
from app.auto_approval.service import AutomaticApprovalService
from app.web.rule_forms import parse_rule_condition
from app.archive.maintenance import RearchiveCommands
from app.archive.preview import template_preview
from app.archive.service import (
    ArchiveSettingsError,
)
from app.downloads.models import AUTO_DOWNLOAD_PROVIDERS
from app.conversion.naming import (
    LibraryTemplateError,
)
from app.torrent.models import TorrentError
from app.web import deps
from app.web.settings_view import render_settings, settings_redirect
from app.web.rule_commands import rule_form, rule_draft

router = APIRouter()


def _settings_draft(action, values):
    draft = dict(values)
    if action == 'torrent':
        draft.pop('password', None)
    return {'action': action, 'scope': ACTION_SCOPES[action], 'values': draft}


async def _preference_form(request, action, section, *, values=None):
    """Keep plain forms on the same command and preserve rejected drafts."""
    redirect = deps.require_authenticated(request)
    if redirect:
        return redirect
    form = await request.form()
    deps.validate_csrf(request, str(form.get('csrf_token') or ''))
    raw = form_values(form)
    if raw.get('confirmed_command'):
        try:
            confirmed = json.loads(str(raw['confirmed_command']))
            raw.update({name: confirmed[name] for name in ('expected_revision', 'idempotency_key')})
        except (ValueError, KeyError, TypeError):
            return await render_settings(request, section, error='保存确认无效，请重新核对草稿', status_code=400,
                                         settings_draft=_settings_draft(action, raw))
    payload = dict(raw if values is None else values)
    try:
        payload['expected_revision'] = int(str(raw.get('expected_revision') or ''))
    except ValueError:
        payload['expected_revision'] = None
    payload['idempotency_key'] = raw.get('idempotency_key')
    if action == 'limits':
        payload['keep_original'] = checkbox(raw.get('keep_original'))
    if action == 'torrent':
        for name in ('keep_seeding', 'auto_pack'):
            payload[name] = checkbox(raw.get(name))
    try:
        await SettingsCommands(deps.database(request), deps.system_settings_service(request),
            deps.archive_settings_service(request)).execute(action, payload)
    except WorkCommandError as exc:
        return await render_settings(request, section, error=exc.public_message, status_code=exc.status_code,
                                     settings_draft=_settings_draft(action, raw))
    if action == 'system':
        await runtime.refresh_system(request, deps.system_settings_service(request))
    return settings_redirect(request, section)


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
    # The reading half is `app/settings/sources.py`'s, shared with
    # `POST /api/v1/settings/sources` (R74), so the two cannot disagree about
    # which chat ids are a channel or about what an empty box means.
    values = form_values(form)
    # Only this explicit confirmation button adopts the conflict snapshot's
    # version. Re-rendering alone must never advance the draft's version.
    if "confirmed_revision" in values:
        values["expected_revision"] = values.pop("confirmed_revision")
    payload, error = parse_source(values)
    if error is not None or payload is None:
        return await render_settings(
            request,
            SETTINGS_SOURCES,
            error=(error or SourceFieldError("SOURCE_INVALID", "来源无效"))
            .public_message,
            status_code=400,
            prefill=values, source_draft=values,
        )
    try:
        await SourceCommands(deps.database(request)).execute("save", values)
    except SourceCommandError as exc:
        return await render_settings(request, SETTINGS_SOURCES,
                                     error=exc.public_message, status_code=exc.status_code,
                                     prefill=values, source_draft=values,
                                     source_conflict=exc.details.get("current"))
    # A save is the operator asking the MTProto ingester to look at this source
    # again: the row may be byte-identical, but the account may have just been
    # added to the channel, and nothing about the stored set would show it.
    deps.connection_manager(request).note_sources_changed()
    return settings_redirect(request, SETTINGS_SOURCES)


@router.post("/sources/dialogs")
async def browse_telegram_dialogs(request: Request, csrf_token: str = Form(), account_id: str = Form("")):
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
        ).list_telegram_user_dialogs(account_id.strip() or None)
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
    account_id: str = Form(""),
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
    versions = await SourceCommands(deps.database(request)).versions()
    expected_revision = next((row["revision"] for row in versions
                              if row["account_id"] == (account_id.strip() or None) and row["chat_id"] == number), 0)
    return await render_settings(
        request,
        SETTINGS_SOURCES,
        notice="已填入「{}」（{}），确认过滤规则后保存。".format(name, number),
        prefill={
            "source_type": source_type,
            "chat_id": number,
            "display_name": name,
            "account_id": account_id.strip() or None,
            "expected_revision": expected_revision,
        },
    )


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
    values = form_values(form)
    source_ids = selected_source_ids(values)
    if not source_ids:
        return await render_settings(
            request,
            SETTINGS_SOURCES,
            error="请至少选择一个来源",
            status_code=400,
            source_draft=values,
        )
    action = str(form.get("action") or "")
    if action == "apply":
        rules, error = parse_source_rules(values)
        if error is not None:
            return await render_settings(
                request,
                SETTINGS_SOURCES,
                error=error.public_message,
                status_code=400,
                source_draft=values,
            )
    elif action not in {"enable", "disable", "delete"}:
        return await render_settings(
            request,
            SETTINGS_SOURCES,
            error=f"未知的来源动作：{action}",
            status_code=400,
            source_draft=values,
        )
    try:
        command = await SourceCommands(deps.database(request)).execute(action, values)
    except SourceCommandError as exc:
        return await render_settings(request, SETTINGS_SOURCES, error=exc.public_message,
                                     status_code=exc.status_code, source_draft=values)
    if command["status"] != "COMPLETED":
        deps.connection_manager(request).note_sources_changed()
        return await render_settings(request, SETTINGS_SOURCES, error="部分来源未修改，请查看逐项结果并核对草稿",
                                     source_command=command, source_draft=values)
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
                "expected_revision": payload.get("expected_revision"),
            }
        )
    if not entries:
        return await render_settings(
            request,
            SETTINGS_SOURCES,
            error="请至少选择一个会话",
            status_code=400,
        )
    account_id = str(form.get("account_id") or "").strip() or None
    try:
        command = await SourceCommands(deps.database(request)).execute("add", {
            "entries": entries, "account_id": account_id, "idempotency_key": form.get("idempotency_key"),
        })
    except SourceCommandError as exc:
        return await render_settings(request, SETTINGS_SOURCES,
                                     error=exc.public_message, status_code=exc.status_code,
                                     source_draft=form_values(form))
    created = sum(item.get("created", False) for item in command["items"])
    deps.connection_manager(request).note_sources_changed()
    return await render_settings(
        request,
        SETTINGS_SOURCES,
        source_command=command,
        source_draft=form_values(form) if command["status"] != "COMPLETED" else None,
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
    """Apply the shared preference command, preserving conflicts as editable drafts."""
    return await _preference_form(request, 'auto-pack', SETTINGS_ARCHIVE)


@router.post("/archive-settings/paths")
async def save_archive_paths(request: Request, csrf_token: str = Form()):
    """Apply the shared preference command, preserving conflicts as editable drafts."""
    return await _preference_form(request, 'paths', SETTINGS_PATHS)


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
    return await _maintenance_form(request, 'rearchive')


async def _maintenance_form(request, action):
    redirect = deps.require_authenticated(request)
    if redirect:
        return redirect
    form = await request.form()
    deps.validate_csrf(request, str(form.get('csrf_token') or ''))
    raw = form_values(form)
    scope = 'rearchive' if action == 'rearchive' else 'path-cache'
    draft = {'action': action, 'scope': scope, 'values': raw}
    try:
        if raw.get('confirmed_command'):
            try:
                confirmed = json.loads(str(raw['confirmed_command']))
                raw.update({name: confirmed[name] for name in ('expected_revision', 'idempotency_key')})
            except (ValueError, KeyError, TypeError):
                raise WorkCommandError('BODY_INVALID', '操作确认无效，请重新核对草稿') from None
        try:
            revision = int(str(raw.get('expected_revision') or ''))
        except ValueError:
            revision = None
        payload = {**raw, 'expected_revision': revision, 'force': checkbox(raw.get('force')),
                   'dry_run': checkbox(raw.get('dry_run'))}
        if action == 'rearchive':
            result = await RearchiveCommands(deps.database(request), deps.archived_work_service(request),
                deps.conversion_service(request)).execute(payload, (await deps.database(request).get_administrator()).username)
        else:
            result = await SettingsCommands(deps.database(request), deps.system_settings_service(request),
                deps.archive_settings_service(request)).execute(action, payload)
    except WorkCommandError as exc:
        return await render_settings(request, SETTINGS_PATHS, error=exc.public_message,
                                     status_code=exc.status_code, settings_draft=draft)
    # The event is a signal, not a payload: a page subscribed to `download`
    # re-reads the snapshot whatever it carries, so this one tells the open
    # tabs that the library moved without naming a work.
    request.app.state.event_bus.publish(EVENT_DOWNLOAD)
    if action == 'rearchive':
        return await render_settings(request, SETTINGS_PATHS, notice=_rearchive_notice(result), rearchive=result)
    removed = result['removed']
    return await render_settings(request, SETTINGS_PATHS,
        notice=f'已清除 {removed} 条 AI 路径缓存；再次打包时会重新询问模型。' if removed else '缓存本来就是空的，没有可清除的 AI 路径记录。')


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
    draft = _settings_draft('template', form_values(form))
    try:
        preview = await template_preview(deps.archive_settings_service(request), request.app.state.settings.library_path, form)
    except LibraryTemplateError as exc:
        return await render_settings(
            request,
            SETTINGS_PATHS,
            error=exc.public_message,
            status_code=400,
            settings_draft=draft,
        )
    return await render_settings(
        request,
        SETTINGS_PATHS,
        template_preview=preview,
        settings_draft=draft,
    )


@router.post("/archive-settings/paths/template")
async def save_library_template(request: Request, csrf_token: str = Form()):
    """Apply the shared preference command, preserving conflicts as editable drafts."""
    return await _preference_form(request, 'template', SETTINGS_PATHS)


@router.post("/archive-settings/paths/ai")
async def save_ai_path_settings(request: Request):
    """Save the whole panel atomically and retain rejected drafts."""
    return await _preference_form(request, 'path-ai', SETTINGS_PATHS)


@router.post("/archive-settings/paths/ai/prompt-default")
async def reset_ai_prompt(request: Request):
    """Store blank to follow the shipped default prompt."""
    return await _preference_form(request, 'path-ai-prompt', SETTINGS_PATHS, values={'prompt': ''})


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
    return await _maintenance_form(request, 'path-cache-clear')


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
    """Apply the shared rule command, preserving the original draft."""
    return await rule_form(request, 'paths', 'save')


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
            rule_draft=rule_draft(form, 'paths'),
        )
    result = await AutomaticApprovalService(deps.database(request)).dry_run(
        condition, case_sensitive=form.get("case_sensitive") == "on"
    )
    return await render_settings(
        request,
        SETTINGS_PATHS,
        dry_run=auto_approval_dry_run(result),
        rule_draft=rule_draft(form, 'paths'),
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
            "rule_uuid": rule.rule_uuid,
            "expected_revision": rule.version,
            "rows": list(rows),
        },
    )


@router.post("/archive-settings/paths/rules/{rule_id}/toggle")
async def toggle_archive_path_rule(rule_id: int, request: Request):
    """Apply the shared rule command, preserving the original draft."""
    return await rule_form(request, 'paths', 'toggle', rule_id=rule_id)


@router.post("/archive-settings/paths/rules/{rule_id}/delete")
async def delete_archive_path_rule(rule_id: int, request: Request):
    """Apply the shared rule command, preserving the original draft."""
    return await rule_form(request, 'paths', 'delete', rule_id=rule_id)


@router.post("/settings/parse")
async def save_parse_rules(request: Request):
    """Apply the shared preference command, preserving conflicts as editable drafts."""
    return await _preference_form(request, 'parse', SETTINGS_PARSE)


@router.post("/settings/parse/reset")
async def reset_parse_rules(request: Request, csrf_token: str = Form()):
    """Apply the shared preference command, preserving conflicts as editable drafts."""
    return await _preference_form(request, 'parse-reset', SETTINGS_PARSE)


@router.post("/settings/parse/ai")
async def save_candidate_admission(request: Request):
    """Apply the shared preference command, preserving conflicts as editable drafts."""
    return await _preference_form(request, 'admission', SETTINGS_PARSE)


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
                settings_draft=_settings_draft('system', form_values(form)),
            )
        ranks[rank] = code
    if ranks:
        if len(ranks) != len(AUTO_DOWNLOAD_PROVIDERS):
            return await render_settings(
                request,
                SETTINGS_SYSTEM,
                error="每个下载来源都要选一个顺位",
                status_code=400,
                settings_draft=_settings_draft('system', form_values(form)),
            )
        values["download_source_priority"] = ",".join(
            ranks[rank] for rank in range(1, len(ranks) + 1)
        )
    return await _preference_form(request, 'system', SETTINGS_SYSTEM, values=values)


@router.post("/archive-settings/torrent")
async def save_torrent_client_settings(
    request: Request, csrf_token: str = Form()
):
    return await _preference_form(request, 'torrent', SETTINGS_ARCHIVE)


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
    """Apply the shared preference command, preserving conflicts as editable drafts."""
    return await _preference_form(request, 'limits', SETTINGS_ARCHIVE)


@router.post("/archive-settings/profiles/{name}")
async def save_archive_tool_profile(
    request: Request, name: str, csrf_token: str = Form()
):
    redirect = deps.require_authenticated(request)
    if redirect:
        return redirect
    deps.validate_csrf(request, csrf_token)
    form = await request.form()
    raw = form_values(form)
    try:
        if raw.get('confirmed_command'):
            try:
                confirmed = json.loads(str(raw['confirmed_command']))
                raw.update({field: confirmed[field] for field in ('expected_revision', 'idempotency_key')})
            except (ValueError, KeyError, TypeError):
                raise WorkCommandError('CONFIRMATION_INVALID', '保存确认无效，请重新核对草稿') from None
        payload = {**raw, 'enabled': checkbox(raw.get('enabled'))}
        try:
            payload['expected_revision'] = int(str(raw.get('expected_revision') or ''))
        except ValueError:
            payload['expected_revision'] = None
        from app.settings.profile_commands import ProfileCommands
        await ProfileCommands(deps.database(request)).execute(name, payload)
    except WorkCommandError as exc:
        return await render_settings(request, SETTINGS_ARCHIVE, error=exc.public_message, status_code=exc.status_code,
                                     profile_draft={'name': name, 'values': raw})
    return settings_redirect(request, SETTINGS_ARCHIVE)


@router.post("/archive-settings/toolchain/install")
async def install_archive_toolchain(
    request: Request, csrf_token: str = Form()
):
    redirect = deps.require_authenticated(request)
    if redirect:
        return redirect
    deps.validate_csrf(request, csrf_token)
    form = await request.form()
    raw = form_values(form)
    try:
        if raw.get('confirmed_command'):
            try:
                confirmed = json.loads(str(raw['confirmed_command']))
                raw.update({name: confirmed[name] for name in ('expected_revision', 'idempotency_key')})
            except (ValueError, KeyError, TypeError):
                raise WorkCommandError('CONFIRMATION_INVALID', '安装确认无效，请重新核对') from None
        try:
            revision = int(str(raw.get('expected_revision') or ''))
        except ValueError:
            revision = None
        result = await request.app.state.toolchain_commands.submit({
            'expected_revision': revision, 'idempotency_key': raw.get('idempotency_key'), 'force': True})
    except WorkCommandError as exc:
        return await render_settings(request, SETTINGS_ARCHIVE, error=exc.public_message, status_code=exc.status_code,
                                     toolchain_draft=raw)
    return await render_settings(request, SETTINGS_ARCHIVE, notice='工具安装已受理，可离开页面后查询结果。',
        status_code=202, toolchain_operation=await request.app.state.toolchain_commands.result(result['toolchain_operation_id']))


@router.get('/archive-settings/toolchain/operations/{operation_id}')
async def toolchain_operation_page(request: Request, operation_id: str):
    redirect = deps.require_authenticated(request)
    if redirect:
        return redirect
    try:
        operation = await request.app.state.toolchain_commands.result(operation_id)
    except WorkCommandError as exc:
        return await render_settings(request, SETTINGS_ARCHIVE, error=exc.public_message, status_code=exc.status_code)
    return await render_settings(request, SETTINGS_ARCHIVE, toolchain_operation=operation)


@router.post("/archive-settings/passwords")
async def add_archive_password(request: Request, csrf_token: str = Form()):
    redirect = deps.require_authenticated(request)
    if redirect:
        return redirect
    deps.validate_csrf(request, csrf_token)
    return await _credential_form(request, 'add')


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
    return await _credential_form(request, 'api-key-generate')


@router.post("/settings/api-keys/revoke")
async def revoke_api_key(request: Request, csrf_token: str = Form()):
    redirect = deps.require_authenticated(request)
    if redirect:
        return redirect
    deps.validate_csrf(request, csrf_token)
    return await _credential_form(request, 'api-key-revoke')


@router.post("/archive-settings/passwords/{password_id}/delete")
async def delete_archive_password(
    request: Request, password_id: int, csrf_token: str = Form()
):
    redirect = deps.require_authenticated(request)
    if redirect:
        return redirect
    deps.validate_csrf(request, csrf_token)
    return await _credential_form(request, 'delete', password_id=password_id)


async def _credential_form(request, action, *, password_id=None):
    raw = form_values(await request.form())
    raw['action'] = action
    if password_id is not None:
        raw['password_id'] = password_id
    if action == 'add':
        raw['enabled'] = raw.get('enabled') == 'on'
    try:
        if raw.get('confirmed_command'):
            try:
                confirmed = json.loads(str(raw['confirmed_command']))
                raw.update({name: confirmed[name] for name in ('expected_revision', 'idempotency_key')})
            except (ValueError, TypeError, KeyError):
                raise WorkCommandError('CONFIRMATION_INVALID', '确认无效，请重新核对原操作') from None
        try:
            revision = int(str(raw.get('expected_revision') or ''))
        except ValueError:
            revision = None
        _, plaintext = await CredentialCommands(deps.database(request), deps.archive_settings_service(request)).execute(
            action, {**raw, 'expected_revision': revision})
    except (WorkCommandError, ArchiveSettingsError) as exc:
        raw.pop('password', None)
        response = await render_settings(request, SETTINGS_PASSWORDS, error=exc.public_message,
            status_code=getattr(exc, 'status_code', 400), credential_draft=raw)
        response.headers['Cache-Control'] = 'no-store'
        return response
    if action == 'api-key-generate':
        response = await render_settings(request, SETTINGS_PASSWORDS, api_key_plaintext=plaintext,
            notice=None if plaintext else '原生成操作已完成，明文不会再次返回；当前状态以页面为准。未收到密钥时请核对后明确重新生成。')
    else:
        response = settings_redirect(request, SETTINGS_PASSWORDS)
    response.headers['Cache-Control'] = 'no-store'
    return response


# ---------------------------------------------------------------------------
#  AI 供应商 (SETTINGS_AI)
# ---------------------------------------------------------------------------

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
    return await _ai_provider_form(request, 'save')


@router.post("/settings/ai/providers/{provider_id}/delete")
async def delete_ai_provider(
    request: Request, provider_id: int, csrf_token: str = Form()
):
    redirect = deps.require_authenticated(request)
    if redirect:
        return redirect
    deps.validate_csrf(request, csrf_token)
    return await _ai_provider_form(request, 'delete', provider_id=provider_id)


async def _ai_provider_form(request, action, *, provider_id=None):
    from app.settings.ai_commands import AiProviderCommands
    raw = form_values(await request.form())
    if provider_id is not None:
        raw['provider_id'] = provider_id
    try:
        if raw.get('confirmed_command'):
            try:
                confirmed = json.loads(str(raw['confirmed_command']))
                raw.update({name: confirmed[name] for name in ('expected_revision', 'idempotency_key')})
                if action == 'delete':
                    raw.update({name: confirmed[name] for name in ('keys_revision', 'models_revision')})
            except (ValueError, KeyError, TypeError):
                raise WorkCommandError('CONFIRMATION_INVALID', '保存确认无效，请重新核对草稿') from None
        try:
            revision = int(str(raw.get('expected_revision') or ''))
        except ValueError:
            revision = None
        children = {}
        if action == 'delete':
            for name in ('keys_revision', 'models_revision'):
                try:
                    children[name] = int(str(raw.get(name) or ''))
                except ValueError:
                    children[name] = None
        result = await AiProviderCommands(deps.database(request), deps.archive_settings_service(request)).execute(
            action, {**raw, **children, 'expected_revision': revision}, provider_id=provider_id)
    except (WorkCommandError, AiError) as exc:
        return await render_settings(request, SETTINGS_AI, error=exc.public_message,
            status_code=getattr(exc, 'status_code', 400), ai_provider_draft={'action': action, 'values': raw})
    location = (_ai_location(result['provider_id']) + '&provider_uuid=' + result['provider_uuid']
                if action == 'save' else _ai_location())
    return RedirectResponse(location, status_code=303)


@router.post("/settings/ai/providers/{provider_id}/keys")
async def add_ai_provider_key(
    request: Request, provider_id: int, csrf_token: str = Form()
):
    """Store one or more API keys from a textarea, one per line.

    The field is a plain textarea and its value is never echoed back: not in
    the list, not in a value attribute, and not in an error-page draft either.
    """
    redirect = deps.require_authenticated(request)
    if redirect:
        return redirect
    deps.validate_csrf(request, csrf_token)
    return await _ai_catalogue_form(request, 'keys-add', provider_id)


@router.post("/settings/ai/keys/{key_id}/toggle")
async def toggle_ai_provider_key(
    request: Request, key_id: int, csrf_token: str = Form()
):
    redirect = deps.require_authenticated(request)
    if redirect:
        return redirect
    deps.validate_csrf(request, csrf_token)
    return await _ai_catalogue_form(request, 'key-toggle', key_id)


@router.post("/settings/ai/keys/{key_id}/reset")
async def reset_ai_provider_key(
    request: Request, key_id: int, csrf_token: str = Form()
):
    redirect = deps.require_authenticated(request)
    if redirect:
        return redirect
    deps.validate_csrf(request, csrf_token)
    return await _ai_catalogue_form(request, 'key-reset', key_id)


@router.post("/settings/ai/keys/{key_id}/delete")
async def delete_ai_provider_key(
    request: Request, key_id: int, csrf_token: str = Form()
):
    redirect = deps.require_authenticated(request)
    if redirect:
        return redirect
    deps.validate_csrf(request, csrf_token)
    return await _ai_catalogue_form(request, 'key-delete', key_id)


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
    return await _ai_catalogue_form(request, 'models-add', provider_id)


async def _ai_network_form(request, action, *, target_id=None):
    redirect = deps.require_authenticated(request)
    if redirect:
        return redirect
    form = await request.form()
    deps.validate_csrf(request, str(form.get('csrf_token') or ''))
    names = ('expected_revision', 'provider_revision', 'keys_revision', 'models_revision',
             'provider_uuid', 'model_uuid', 'idempotency_key')
    raw = {name: form[name] for name in names if name in form}
    if form.get('confirmed_command'):
        try:
            confirmation = json.loads(str(form['confirmed_command']))
            raw.update({name: confirmation[name] for name in names if not name.endswith('_uuid') and name in confirmation})
        except (ValueError, TypeError):
            return await render_settings(request, SETTINGS_AI, error='操作确认无效，请重新核对配置', status_code=400,
                ai_network_draft={'action': action, 'target_id': target_id, 'values': raw})
    body = dict(raw)
    for name in names[:4]:
        if name in body:
            try:
                body[name] = int(str(body[name]))
            except ValueError:
                body[name] = None
    try:
        receipt = await request.app.state.ai_network_commands.submit(action, body, target_id=target_id)
    except WorkCommandError as exc:
        return await render_settings(request, SETTINGS_AI, error=exc.public_message, status_code=exc.status_code,
            ai_network_draft={'action': action, 'target_id': target_id, 'values': raw})
    return RedirectResponse(request.url_for('settings_section', section='ai').path +
                            '?ai_operation=' + receipt['ai_operation_id'], status_code=303)


@router.post("/settings/ai/providers/{provider_id}/models/fetch")
async def fetch_ai_provider_models(request: Request, provider_id: int):
    """Admit a listing; its result can be reopened without sending it again."""
    return await _ai_network_form(request, 'fetch', target_id=provider_id)


@router.post("/settings/ai/models/{model_id}/toggle")
async def toggle_ai_provider_model(
    request: Request, model_id: int, csrf_token: str = Form()
):
    redirect = deps.require_authenticated(request)
    if redirect:
        return redirect
    deps.validate_csrf(request, csrf_token)
    return await _ai_catalogue_form(request, 'model-toggle', model_id)


@router.post("/settings/ai/models/{model_id}/params")
async def save_ai_provider_model_params(
    request: Request, model_id: int, csrf_token: str = Form()
):
    redirect = deps.require_authenticated(request)
    if redirect:
        return redirect
    deps.validate_csrf(request, csrf_token)
    return await _ai_catalogue_form(request, 'model-params', model_id)


@router.post("/settings/ai/models/{model_id}/delete")
async def delete_ai_provider_model(
    request: Request, model_id: int, csrf_token: str = Form()
):
    redirect = deps.require_authenticated(request)
    if redirect:
        return redirect
    deps.validate_csrf(request, csrf_token)
    return await _ai_catalogue_form(request, 'model-delete', model_id)


async def _ai_catalogue_form(request, action, target_id):
    from app.settings.ai_commands import AiCatalogueCommands
    form = await request.form()
    raw = form_values(form)
    if action == 'models-add':
        raw['model_name'] = form.getlist('model_name')
    try:
        if raw.get('confirmed_command'):
            try:
                confirmation = json.loads(str(raw['confirmed_command']))
                raw.update({name: confirmation[name] for name in ('expected_revision', 'idempotency_key')})
                if action == 'key-reset':
                    raw['expected_health_revision'] = confirmation['expected_health_revision']
            except (ValueError, KeyError, TypeError):
                raise WorkCommandError('CONFIRMATION_INVALID', '操作确认无效，请重新核对草稿') from None
        payload = dict(raw)
        for name in ('expected_revision', 'expected_health_revision'):
            try:
                payload[name] = int(str(raw.get(name) or ''))
            except ValueError:
                payload[name] = None
        result = await AiCatalogueCommands(deps.database(request), deps.archive_settings_service(request)).execute(
            action, target_id, payload)
    except (WorkCommandError, AiError) as exc:
        raw.pop('api_keys', None)
        return await render_settings(request, SETTINGS_AI, error=exc.public_message,
            status_code=getattr(exc, 'status_code', 400),
            ai_catalogue_draft={'action': action, 'target_id': target_id, 'values': raw},
            ai_catalogue_retry_url=request.url.path)
    return RedirectResponse(_ai_location(result['provider_id']) + '&provider_uuid=' + result['provider_uuid'], status_code=303)


@router.post("/settings/ai/models/{model_id}/verify")
async def verify_ai_provider_model(request: Request, model_id: int):
    """Admit a single-model probe using its observed configuration."""
    return await _ai_network_form(request, 'model', target_id=model_id)


@router.post("/settings/ai/providers/{provider_id}/verify")
async def verify_ai_provider_models(request: Request, provider_id: int):
    """Admit a fixed provider/model/key snapshot for sequential verification."""
    return await _ai_network_form(request, 'provider', target_id=provider_id)


@router.post("/settings/ai/verify")
async def verify_all_ai_models(request: Request):
    """Admit the observed catalogue once."""
    return await _ai_network_form(request, 'all')


@router.post("/settings/ai/master")
async def save_ai_master_switch(request: Request, csrf_token: str = Form()):
    """Toggle the global AI master switch.

    State only: turning it off stops every feature from calling a model and
    deletes nothing -- providers, keys, chains and per-feature switches all stay
    where the operator left them, so turning it back on restores the exact
    configuration that was there before.
    """
    return await _preference_form(request, 'ai-master', SETTINGS_AI)


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
async def save_ai_candidate_model_source(request: Request):
    """Save the gate model source with its observed version."""
    return await _preference_form(request, 'candidate-model-source', SETTINGS_PARSE)


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
    raw = form_values(await request.form())
    section = _CHAIN_SECTIONS.get(scope, SETTINGS_AI)
    from app.settings.ai_commands import AiChainCommands
    try:
        if raw.get('model_identity'):
            raw['model_id'], raw['model_uuid'] = str(raw['model_identity']).split(':', 1)
        if raw.get('confirmed_command'):
            confirmed = json.loads(str(raw['confirmed_command']))
            raw.update({name: confirmed[name] for name in ('expected_revision', 'idempotency_key')})
        payload = dict(raw, action=action)
        try:
            payload['expected_revision'] = int(str(raw.get('expected_revision') or ''))
        except ValueError:
            payload['expected_revision'] = None
        await AiChainCommands(deps.database(request)).execute(scope, payload)
    except (AiError, WorkCommandError, ValueError, KeyError, TypeError) as exc:
        message = exc.public_message if isinstance(exc, (AiError, WorkCommandError)) else '操作确认无效，请重新核对原操作'
        return await render_settings(request, section, error=message, status_code=getattr(exc, 'status_code', 400),
            ai_chain_draft={'scope': scope, 'action': action, 'values': raw}, ai_chain_retry_url=request.url.path)
    if scope == CHAIN_SCOPE_DEFAULT:
        return RedirectResponse(_ai_location(), status_code=303)
    return RedirectResponse(f"/settings/{section}", status_code=303)


@router.post("/archive-settings/paths/ai/models")
async def save_ai_model_source(request: Request):
    """Save the path model source with its observed version."""
    return await _preference_form(request, 'path-model-source', SETTINGS_PATHS)
