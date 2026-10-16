"""Hand-fed links: an ExHentai gallery URL (R63).

The ingest helper creates the candidate already approved and routes it to the
same download queue the review pipeline uses, so a manual add is not a second
path into the system -- only a second way to reach the first one.

Since R63 the form takes an ExHentai gallery link and nothing else. A pasted
link is a claim that a work exists, and the only way to check a claim is to ask
the gallery: the route fetches before it inserts, so a link that does not
resolve -- a deleted gallery, a login-walled one, an outage -- leaves no
candidate behind. A magnet used to be accepted here too; that is gone, because
a magnet has no gallery and therefore no metadata, which is the whole reason
this page exists.
"""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import RedirectResponse

from app.candidates import manual as manual_ingest
from app.review.commands import WorkCommandError, WorkCommands
from app.review.manual import add_manual_work, manual_result
from app.web import deps

router = APIRouter()

#: Re-exported for the template context. The wording and the gate live in
#: `app.candidates.manual`, which the JSON API reads too -- one copy, so the page
#: and the API cannot disagree about when a paste is acceptable.
NOT_CONFIGURED_MESSAGE = manual_ingest.NOT_CONFIGURED_MESSAGE


def _exhentai_configured(request: Request) -> bool:
    """Whether an ExHentai connection is configured (see the shared module)."""
    return manual_ingest.exhentai_configured(
        getattr(request.app.state, "connection_manager", None)
    )


async def _render_manual_add(
    request: Request,
    *,
    error: str | None = None,
    status_code: int = 200,
    existing_candidate_id: int | None = None,
    existing_work_id: str | None = None,
    draft: dict | None = None,
    operation: dict | None = None,
):
    """Render the form, with the two outcome blocks it can carry.

    A duplicate resolves to a card naming the existing work -- id, and the name
    the page shows for it -- rather than to a red line, because the interesting
    thing is not that the paste was refused but which work it already is.
    """
    context: dict[str, object] = {
        "csrf_token": request.session["csrf_token"],
        "exhentai_configured": _exhentai_configured(request),
        "error": error,
        "not_configured_message": NOT_CONFIGURED_MESSAGE,
        "existing_candidate_id": existing_candidate_id,
        "existing_candidate_title": None,
        "existing_work_id": existing_work_id,
        'draft': draft, 'operation': operation,
        'layout': deps.page_layout(request),
    }
    if existing_candidate_id is not None:
        existing = await deps.database(request).get_candidate(
            existing_candidate_id
        )
        if existing_work_id and existing is not None and existing.work_id == existing_work_id:
            context["existing_candidate_title"] = existing.display_title
        else:
            context['existing_candidate_id'] = None
            context['error'] = '原作品已被删除或无法核实身份，请重新查询作品列表。'
    return deps.templates(request).TemplateResponse(
        request=request,
        name="manual_add.html",
        context=context,
        status_code=status_code,
    )


@router.get("/manual-add")
async def manual_add_page(request: Request):
    redirect = deps.require_authenticated(request)
    if redirect:
        return redirect
    return await _render_manual_add(request)


@router.get('/manual-add/operations/{operation_id}')
async def manual_add_operation(request: Request, operation_id: str):
    redirect = deps.require_authenticated(request)
    if redirect:
        return redirect
    try:
        result = await manual_result(deps.database(request), operation_id)
    except WorkCommandError as exc:
        return await _render_manual_add(request, error=exc.public_message, status_code=exc.status_code)
    if result['status'] == 'COMPLETED':
        return await _completed_result(request, result)
    details = result.get('error', {}).get('details', {})
    return await _render_manual_add(request, operation=result,
        existing_candidate_id=details.get('existing_candidate_id'), existing_work_id=details.get('existing_work_id'),
        error=result.get('error', {}).get('message'), status_code=200)


async def _completed_result(request: Request, result: dict):
    """Follow the original work identity even when its numeric row was reused."""
    try:
        current = await WorkCommands(deps.database(request)).version(result['candidate_id'])
    except WorkCommandError as exc:
        if exc.status_code != 404:
            raise
        current = None
    if current is None or current['work_id'] != result['work_id']:
        return await _render_manual_add(request, error='原操作已完成，但原作品已被删除；不会跳转到复用编号的新作品。',
                                        status_code=410)
    return RedirectResponse(request.url_for('work_detail', candidate_id=result['candidate_id']).path
                            + '?work_id=' + result['work_id'], status_code=303)


@router.post("/manual-add")
async def manual_add_submit(request: Request):
    redirect = deps.require_authenticated(request)
    if redirect:
        return redirect
    form = await request.form()
    deps.validate_csrf(request, str(form.get("csrf_token") or ""))
    raw = str(form.get("input") or "").strip()
    draft = {'url': raw, 'idempotency_key': form.get('idempotency_key')}
    try:
        result = await add_manual_work(deps.database(request), deps.exhentai_service(request),
            deps.review_orchestrator(request), deps.download_service(request), draft,
            configured=_exhentai_configured(request))
    except WorkCommandError as exc:
        return await _render_manual_add(request, error=exc.public_message, status_code=exc.status_code,
            existing_candidate_id=exc.details.get('existing_candidate_id'),
            existing_work_id=exc.details.get('existing_work_id'), draft=draft)
    if result['status'] != 'COMPLETED':
        return await _render_manual_add(request, draft=draft, operation=result,
                                        status_code=202 if result['status'] == 'RUNNING' else 409)
    return await _completed_result(request, result)
