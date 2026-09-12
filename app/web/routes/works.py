"""The one detail page, for a work at any stage of the pipeline.

`render_review_error` lives here rather than with the candidate actions that
raise: a refused action re-renders this page, and putting the renderer next to
the page it renders is what stops a second assembly of the same context from
appearing beside it.
"""

from __future__ import annotations

from urllib.parse import quote_plus, urlsplit

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import RedirectResponse

from app.api.events import EVENT_DOWNLOAD
from app.api.works import (
    configured_sources,
    effective_library_path,
    work_snapshot,
)
from app.candidates.links import GALLERY_URL_PATTERN
from app.review.models import METADATA_FIELDS, field_label
from app.web import deps

router = APIRouter()

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
        sources=configured_sources(request),
        library_path=await effective_library_path(request),
    )
    if snapshot is None:
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
    try:
        result = await service.set_archive_path(
            candidate_id,
            directory=directory,
            filename=filename,
            operator_name=str(request.session.get("username") or "admin"),
        )
        if repack is not None:
            await deps.conversion_service(request).enqueue_for_candidate(
                candidate_id
            )
    except Exception as exc:  # noqa: BLE001 - domain refusals carry a message
        message = getattr(exc, "public_message", None)
        if message is None:
            raise
        return await render_work(
            request, candidate_id, error=str(message), status_code=400
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


@router.post("/works/{candidate_id}/eh-ref")
async def set_work_eh_ref(
    request: Request,
    candidate_id: int,
    ex_gid: str = Form(),
    ex_gallery_token: str | None = Form(default=None),
    csrf_token: str = Form(),
):
    """Re-point a work at a different ExHentai gallery, for metadata.

    ``ex_gid`` accepts either an ExHentai/e-hentai gallery URL or a bare
    gallery id; a URL also carries its token across. This is the manual escape
    hatch for an operator who knows the right gallery when the ingestor did
    not pick it up -- a fast way to relink and then pull the metadata without
    re-running ingestion. The refreshed page redraws through the same render
    as every other action, so nothing drifts.
    """
    redirect = deps.require_authenticated(request)
    if redirect:
        return redirect
    deps.validate_csrf(request, csrf_token)

    gid: int | None = None
    token: str | None = None
    match = GALLERY_URL_PATTERN.search(ex_gid)
    if match:
        gid = int(match.group(1))
        token = match.group(2)
    elif ex_gid.strip().isdigit():
        gid = int(ex_gid.strip())
        token = ex_gallery_token.strip() if ex_gallery_token else ""
    if gid is None:
        return await render_review_error(
            request,
            candidate_id,
            "无法识别画廊编号：请粘贴 ExHentai 画廊链接，或直接填写纯数字编号。",
        )
    if token == "":
        token = None

    await deps.database(request).set_candidate_eh_ref(
        candidate_id, gid, token
    )
    notice = f"已切换画廊编号为 {gid}"
    return RedirectResponse(
        f"/works/{candidate_id}?message={quote_plus(notice)}",
        status_code=303,
    )


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
