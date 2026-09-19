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

from app.api.serializers import auto_approval_dry_run
from app.api.status import (
    SETTINGS_ARCHIVE,
    SETTINGS_CONNECTIONS,
    SETTINGS_PASSWORDS,
    SETTINGS_PATHS,
    SETTINGS_SOURCES,
    SETTINGS_SYSTEM,
    SETTINGS_SECTIONS,
)
from app.auto_approval.rules import (
    RuleValidationError,
    editor_rows,
    validate_rule_ast,
)
from app.auto_approval.service import AutomaticApprovalService
from app.web.rule_forms import parse_rule_condition
from app.archive.service import (
    LIMIT_KEYS as ARCHIVE_LIMIT_KEYS,
    TITLE_SOURCE_JAPANESE,
    TITLE_SOURCES,
    ArchiveSettingsError,
)
from app.conversion.naming import (
    LibraryTemplateError,
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
    return settings_redirect(request, SETTINGS_SOURCES)


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
    template: str, title_source: str
) -> dict[str, object]:
    """Render the sample book's path, exactly as the packer would.

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
    )
    rendered = (relative.parent / f"{relative.name}.cbz").as_posix()
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
    # The radio as submitted, falling back to what is stored, so previewing a
    # preference change shows its effect before it is saved.
    submitted = str(form.get("title_source") or "").strip().lower()
    title_source = (
        submitted
        if submitted in TITLE_SOURCES
        else await deps.archive_settings_service(request).title_source()
    )
    return await render_settings(
        request,
        SETTINGS_PATHS,
        template_preview=_render_template_preview(template, title_source),
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
    try:
        await deps.system_settings_service(request).save(
            {
                key: str(form.get(key) or "")
                for key in (
                    "source_concurrency",
                    "poll_interval_ms",
                    "timezone",
                    "auto_approval_interval_minutes",
                    "log_level",
                )
                if key in form
            }
        )
    except SystemSettingsError as exc:
        return await render_settings(
            request,
            SETTINGS_SYSTEM,
            error=exc.public_message,
            status_code=400,
        )
    await deps.refresh_display_timezone(request)
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
