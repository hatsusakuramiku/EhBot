"""The one detail page, for a work at any stage of the pipeline.

`render_review_error` lives here rather than with the candidate actions that
raise: a refused action re-renders this page, and putting the renderer next to
the page it renders is what stops a second assembly of the same context from
appearing beside it.
"""

from __future__ import annotations

import asyncio
import json
from urllib.parse import quote_plus, urlsplit

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import RedirectResponse

from app.api.events import EVENT_DOWNLOAD
from app.api.contracts import ApiError
from app.api.ingestion import retry_ingest_operation
from app.api.works import (
    configured_sources,
    effective_library_path,
    work_snapshot,
)
from app.candidates.operations import list_operations
from app.review.models import METADATA_FIELDS, field_label
from app.review.application import edit_metadata as apply_metadata_edit
from app.review.application import rename_work
from app.review.commands import WorkCommandError, WorkCommands
from app.review.service import ReviewError
from app.web import deps

router = APIRouter()


@router.post('/works/{candidate_id}/ingest-operations/{operation_id}/retry')
async def retry_work_ingest(request: Request, candidate_id: int, operation_id: str):
    """Thin form adapter to the shared ingestion command."""
    redirect = deps.require_authenticated(request)
    if redirect:
        return redirect
    form = await request.form()
    deps.validate_csrf(request, str(form.get('csrf_token', '')))
    try:
        revision = int(str(form.get('expected_revision', '')))
    except ValueError:
        revision = None
    try:
        await retry_ingest_operation(request, operation_id, {
            'expected_revision': revision, 'idempotency_key': form.get('idempotency_key'),
        })
    except ApiError as exc:
        return await render_review_error(request, candidate_id, exc.message)
    return RedirectResponse(request.url_for('work_detail', candidate_id=candidate_id).path
                            + '?message=' + quote_plus('重试已受理，完成结果将在此处更新。'), status_code=303)


@router.post("/works/{candidate_id}/metadata")
async def edit_work_metadata(request: Request, candidate_id: int):
    """Save only changed inputs; JavaScript uses the same JSON edit service."""
    redirect = deps.require_authenticated(request)
    if redirect:
        return redirect
    form = await request.form()
    deps.validate_csrf(request, str(form.get("csrf_token", "")))
    fields = {name.removeprefix("field__"): str(value) for name, value in form.items()
              if name.startswith("field__") and value != form.get("original__" + name.removeprefix("field__"), "")}
    custom = str(form.get("new_field", "")).strip()
    if custom:
        fields[custom] = str(form.get("new_value", ""))
    locks = {name.removeprefix("lock_original__"): ("lock__" + name.removeprefix("lock_original__") in form)
             for name, value in form.items() if name.startswith("lock_original__")
             and ("lock__" + name.removeprefix("lock_original__") in form) != (value == "1")}
    try:
        revision = int(str(form.get('expected_revision', '')))
    except ValueError:
        revision = None
    try:
        result = await apply_metadata_edit(deps.database(request), deps.archived_work_service(request),
            candidate_id, {"fields": fields, "locks": locks,
            'work_id': form.get('work_id'), 'expected_revision': revision,
            'idempotency_key': form.get('idempotency_key')},
                                            request.session.get("username", "admin"))
    except (WorkCommandError, ReviewError) as exc:
        return await render_work(request, candidate_id, error=exc.public_message,
                                 status_code=exc.status_code if isinstance(exc, WorkCommandError) else 400,
                                 metadata_draft=dict(form))
    return RedirectResponse(request.url_for("work_detail", candidate_id=candidate_id).path
                            + "?message=" + quote_plus(result["sync_message"]), status_code=303)

#: Where 返回 can send an operator, longest prefix first.
#:
#: The detail page is reached from four lists, and until now it offered exactly
#: one way out: a hardcoded 「返回候选列表」. Arriving from 已下载 and wanting to
#: go back meant using the sidebar, which loses the tab, the filter, the sort
#: and the page the operator had -- all of which live in the query string.
#:
#: Longest first, and matched that way: `/candidates/manual-add` is its own page
#: and has to be recognised before the `/candidates` prefix swallows it -- a
#: button reading 「返回候选列表」 that lands on 手动添加 would be a wrong label,
#: not a harmless one. The label is the destination's own name, taken from the
#: `NAV_ITEMS` wording, so the button says where it goes, not 「返回上一页」.
_ORIGINS: tuple[tuple[str, str], ...] = (
    ("/candidates/manual-add", "返回手动添加"),
    ("/candidates", "返回候选列表"),
    ("/downloaded", "返回已下载"),
    ("/activity", "返回活动"),
    ("/logs", "返回运行日志"),
    ("/", "返回工作台"),
)

#: Fallback when nothing else identifies the origin. The candidate list is the
#: page this button has always pointed at, so an unrecognised referrer behaves
#: exactly the way the old hardcoded link did.
_DEFAULT_ORIGIN = ("/candidates", "返回候选列表")

#: Session key holding the list this work was opened from.
#:
#: The reason it has to be remembered at all: every action on the detail page is
#: a POST that answers 303 back to `/works/{id}`, and the `Referer` the browser
#: then sends is the detail page itself -- which `resolve_origin` excludes, for
#: the good reason that pointing 返回 at the page it is drawn on is useless. So
#: without this, 拉取元数据 or 重新打包 silently demoted 「返回已下载」 to the
#: default 「返回候选列表」, and the operator was thrown into a list their work
#: was not in. One entry, not a map: it is scoped to the work it was recorded
#: for and replaced when another work is opened, so the cookie cannot grow with
#: every book an operator visits.
_ORIGIN_SESSION_KEY = "work_origin"


def resolve_origin(request: Request, candidate_id: int | None = None) -> dict[str, str]:
    """Where 返回 goes, and what it is called.

    Three sources, in order of trustworthiness:

    * An explicit `?return_to=`, which is what a list page appends to its own
      links. It carries the query string, so 返回 lands on the same filtered,
      sorted, paginated view the operator left -- which is the whole point.
    * The `Referer` header, for a link that predates this and for a bookmark
      followed from elsewhere in the app. Header-derived, so it is advisory: it
      only ever selects among the fixed paths below.
    * What this work's origin was the last time one of the two above answered.
      This is what survives an action: a POST redirects to `/works/{id}` with no
      query string and a referrer naming the detail page, so the first two
      sources have nothing to say and only memory does.

    The first two go through `local_return_to`, so a crafted `return_to` cannot
    turn this into an open redirect -- the same guard the job actions already
    use. The remembered value was itself resolved through that guard before it
    was stored, and is re-checked on the way out rather than trusted: a session
    outlives a deploy, and a path that stopped being an origin must not keep
    being offered. A value that does not resolve falls back to the candidate list
    rather than being rejected: this is a navigation affordance, and answering a
    whole page with an error because a referrer looked odd would be worse than
    sending the operator somewhere sensible.
    """
    explicit = deps.local_return_to(request.query_params.get("return_to"))
    # Checked against the known origins for the same reason the referrer is: the
    # label is looked up from the path, so a destination no prefix matches would
    # be sent with whatever `_label_for` falls back to -- a button reading
    # 「返回候选列表」 that goes somewhere else. Being local is enough to be safe
    # and not enough to be nameable.
    if explicit and _matches_known_origin(urlsplit(explicit).path or "/"):
        return _remember(request, candidate_id, explicit)

    referer = request.headers.get("referer") or ""
    if referer:
        parsed = urlsplit(referer)
        # Same-origin only. A referrer from another site says nothing about
        # where this operator is working, and honouring its path would put a
        # foreign query string on our own URL.
        same_origin = not parsed.netloc or parsed.netloc == request.url.netloc
        if same_origin:
            path = parsed.path or "/"
            # Never point 返回 at the page it is rendered on: a detail page
            # reached from another detail page would otherwise offer a button
            # that reloads the current URL.
            if not path.startswith("/works/"):
                candidate = path + (f"?{parsed.query}" if parsed.query else "")
                target = deps.local_return_to(candidate)
                if target and _matches_known_origin(path):
                    return _remember(request, candidate_id, target)

    remembered = _remembered_origin(request, candidate_id)
    if remembered:
        return {"href": remembered, "label": _label_for(remembered)}

    href, label = _DEFAULT_ORIGIN
    return {"href": href, "label": label}


def _remember(
    request: Request, candidate_id: int | None, href: str
) -> dict[str, str]:
    """Record this work's origin, and return it ready for the template.

    Recording happens on the way *out* of a successful resolution rather than at
    a call site, so there is no path that resolves an origin and forgets to keep
    it -- which is the bug this whole mechanism exists to prevent, one level up.
    """
    if candidate_id is not None:
        try:
            request.session[_ORIGIN_SESSION_KEY] = {
                "id": int(candidate_id),
                "href": href,
            }
        except (AttributeError, TypeError, ValueError):
            # No session on this request (an internal render, a test double).
            # Losing the memory costs one button label; raising would cost the
            # page.
            pass
    return {"href": href, "label": _label_for(href)}


def _remembered_origin(
    request: Request, candidate_id: int | None
) -> str | None:
    """The stored origin, if it was stored for *this* work and still resolves."""
    if candidate_id is None:
        return None
    try:
        stored = request.session.get(_ORIGIN_SESSION_KEY)
    except (AttributeError, TypeError, ValueError):
        return None
    if not isinstance(stored, dict) or stored.get("id") != int(candidate_id):
        return None
    href = deps.local_return_to(stored.get("href"))
    if not href or not _matches_known_origin(urlsplit(href).path or "/"):
        return None
    return href


def _matches_known_origin(path: str) -> bool:
    return any(_is_under(path, prefix) for prefix, _ in _ORIGINS)


def _label_for(target: str) -> str:
    path = urlsplit(target).path or "/"
    for prefix, label in _ORIGINS:
        if _is_under(path, prefix):
            return label
    return _DEFAULT_ORIGIN[1]


def _is_under(path: str, prefix: str) -> bool:
    """Whether `path` is `prefix` or a page inside it.

    `/downloadedX` must not count as being under `/downloaded`, which a bare
    `startswith` would allow, so anything past the prefix has to begin a new
    segment.
    """
    if prefix == "/":
        return path == "/"
    return path == prefix or path.startswith(prefix + "/")


async def render_work(
    request: Request,
    candidate_id: int,
    error: str | None = None,
    message: str | None = None,
    status_code: int = 200,
    metadata_draft: dict | None = None,
    review_draft: dict | None = None,
    enqueue_draft: dict | None = None,
    removal_draft: dict | None = None,
    path_draft: dict | None = None,
    source_draft: dict | None = None,
):
    """The one detail page, for a work at any stage.

    Everything on it comes from `work_snapshot`, the same dict
    `GET /api/v1/works/{id}` returns, so the page cannot offer an action the
    API would refuse. The error path renders this same page rather than a
    stripped-down variant: an operator whose approval was refused needs the
    timeline and the metadata in front of them to decide what to do next.

    An HTMX request gets the same render through the fragment layout, which is
    what makes an action update this page in place instead of navigating. There
    is no second assembler and no partial template: `layout` swaps the shell out,
    nothing else changes, so the page an update produces is the page a reload
    produces.
    """
    snapshot = await work_snapshot(
        deps.database(request),
        candidate_id,
        download=deps.download_service(request),
        # Optional rather than required: unfilled 归档路径 fields are a worse
        # page, a 503 is no page at all. Same reason `download` is optional here,
        # and the same accessor the JSON endpoint uses so the two cannot answer
        # the form differently.
        conversion=deps.optional_service(request, "conversion_service"),
        sources=configured_sources(request),
        library_path=await effective_library_path(request),
        archived=deps.optional_service(request, "archived_work_service"),
    )
    expected_work_id = request.query_params.get('work_id')
    if expected_work_id is not None:
        try:
            current = await WorkCommands(deps.database(request)).version(candidate_id)
        except WorkCommandError as exc:
            if exc.status_code != 404:
                raise
            current = None
        if (snapshot is None or snapshot['work_id'] != expected_work_id
                or current is None or current['work_id'] != expected_work_id):
            raise HTTPException(status_code=410, detail='原作品已被删除，不会打开复用编号的新作品')
    if snapshot is None:
        operations = await asyncio.to_thread(list_operations, deps.database(request), candidate_id)
        for operation in operations:
            result_id = operation['result_candidate_id']
            if (operation['status'] == 'COMPLETED' and result_id and result_id != candidate_id
                    and await deps.database(request).get_candidate(result_id) is not None):
                return RedirectResponse(request.url_for('work_detail', candidate_id=result_id).path, status_code=303)
        raise HTTPException(status_code=404, detail="Candidate not found")
    prev_id, next_id = await deps.database(request).adjacent_candidate_ids(
        candidate_id
    )
    return deps.templates(request).TemplateResponse(
        request=request,
        name="work_detail.html",
        context={
            "csrf_token": request.session["csrf_token"],
            "work": snapshot,
            "error": error,
            "message": message,
            "metadata_fields": METADATA_FIELDS,
            "metadata_draft": metadata_draft,
            "review_draft": review_draft,
            "enqueue_draft": enqueue_draft,
            "removal_draft": removal_draft,
            "path_draft": path_draft,
            "source_draft": source_draft,
            "field_label": field_label,
            "current_user": request.session.get("username", "admin"),
            # Resolved server-side rather than with `history.back()`: the page
            # has to work without JavaScript, and a back-stack entry is not the
            # same thing as the list this work belongs to -- an operator who got
            # here through three metadata saves would be sent to the last of
            # them.
            "origin": resolve_origin(request, candidate_id),
            "prev_work": prev_id,
            "next_work": next_id,
            "layout": deps.page_layout(request),
        },
        status_code=status_code,
    )


@router.post("/works/{candidate_id}/archive-path")
async def save_archive_path(
    request: Request,
    candidate_id: int,
    csrf_token: str = Form(),
    directory: str = Form(default=""),
    filename: str = Form(default=""),
    repack: str | None = Form(default=None),
):
    """Set where this work's CBZ belongs, and optionally repack it there now.

    On this page rather than on `/downloaded` because a path is specific to one
    book, and this is the one detail page a work has at every stage. It is a new
    write route on `/works/{id}` -- the first -- and that is deliberate: unlike
    approve, which already had a home under `/candidates/{id}`, there is no
    existing endpoint that sets an archive path, so routing it through one would
    have meant inventing a second meaning for a candidate action.

    A refusal re-renders this page with the reason on it rather than redirecting
    with a query parameter, because the operator has a form open and needs to see
    which value was rejected while they fix it. `repack` is a checkbox, hence
    `str | None`: an unchecked box sends nothing, so not repacking is what
    absence means.
    """
    redirect = deps.require_authenticated(request)
    if redirect:
        return redirect
    deps.validate_csrf(request, csrf_token)
    service = deps.archived_work_service(request)
    from app.web.routes.candidates import review_preconditions
    form = await request.form()
    payload = {**review_preconditions(form), 'directory': directory, 'filename': filename, 'repack': repack is not None}
    try:
        result = await rename_work(deps.database(request), service, deps.conversion_service(request),
            candidate_id, payload, str(request.session.get('username') or 'admin'), strict=True)
    except Exception as exc:  # noqa: BLE001 - domain refusals carry a message
        message = getattr(exc, "public_message", None)
        if message is None:
            raise
        return await render_work(
            request, candidate_id, error=str(message), status_code=getattr(exc, 'status_code', 400), path_draft=payload
        )
    request.app.state.event_bus.publish(
        EVENT_DOWNLOAD, candidate_id=candidate_id
    )
    notice = f"归档路径已设为 {result['relative_path']}"
    if result["moved"]:
        notice = f"已移动到 {result['relative_path']}"
    elif repack is not None:
        notice = f"{notice}，重新打包后生效"
    return RedirectResponse(
        f"/works/{candidate_id}?message={quote_plus(notice)}",
        status_code=303,
    )


@router.get("/works/{candidate_id}")
async def work_detail(
    request: Request,
    candidate_id: int,
    error: str | None = None,
    message: str | None = None,
):
    """The unified detail page: 候选期, 下载期 and 入库期 at one URL.

    R6 replaced a 307 to `/candidates/{id}` with the page itself, and turned
    that path around into the redirect. `/works/{id}` is what
    `candidate_summary` has handed every client since R5, and what a work
    keeps being called after it stops being a candidate.

    `error` arrives in the query string because a redirect is the only way a
    form post can report a refusal it could not render itself -- a job action
    that came back here via `return_to`.
    """
    redirect = deps.require_authenticated(request)
    if redirect:
        return redirect
    return await render_work(
        request, candidate_id, error=error, message=message
    )


async def _source_form(request, candidate_id, action):
    from app.review.source_commands import SourceCommands
    redirect = deps.require_authenticated(request)
    if redirect:
        return redirect
    form = await request.form()
    deps.validate_csrf(request, str(form.get('csrf_token') or ''))
    raw = dict(form)
    payload = dict(raw)
    try:
        if raw.get('confirmed_command'):
            confirmation = json.loads(raw['confirmed_command'])
            payload.update({name: confirmation.get(name) for name in
                            ('expected_revision', 'idempotency_key', 'target_work_id', 'target_revision')})
        else:
            payload['expected_revision'] = int(str(raw.get('expected_revision') or ''))
            if raw.get('target_revision'):
                payload['target_revision'] = int(raw['target_revision'])
    except (ValueError, TypeError, KeyError):
        payload['expected_revision'] = None
    try:
        result = await SourceCommands(deps.database(request)).execute(candidate_id, action, payload)
    except WorkCommandError as exc:
        return await render_work(request, candidate_id, error=exc.public_message, status_code=exc.status_code,
            source_draft={'action': action, 'values': payload, 'target': exc.details.get('target',
                {'work_id': payload.get('target_work_id'), 'revision': payload.get('target_revision')})})
    request.app.state.event_bus.publish(EVENT_DOWNLOAD, candidate_id=candidate_id)
    result_id = result.get('result_candidate_id') or candidate_id
    result_identity = result.get('result_work_id') or result['work_id']
    return RedirectResponse(request.url_for('work_detail', candidate_id=result_id).path
                            + '?work_id=' + quote_plus(result_identity), status_code=303)


@router.post('/works/{candidate_id}/eh-ref')
async def set_work_eh_ref(request: Request, candidate_id: int):
    return await _source_form(request, candidate_id, 'eh-ref')


@router.post('/works/{candidate_id}/preview')
async def set_work_preview(request: Request, candidate_id: int):
    return await _source_form(request, candidate_id, 'preview')


@router.post('/works/{candidate_id}/torrent')
async def set_work_torrent(request: Request, candidate_id: int):
    return await _source_form(request, candidate_id, 'torrent')


async def render_review_error(
    request: Request, candidate_id: int, message: str
):
    """A refused action re-renders the detail page with the reason on it.

    This is one call into `_render_work` rather than a second assembly of the
    same context: R5's lesson was that two renderings of one page drift, and
    an operator reading「无法通过」needs the timeline that explains why, not a
    reduced page that only carries the message.
    """
    return await render_work(
        request, candidate_id, error=message, status_code=400
    )
