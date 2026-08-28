"""The settings page, rendered from the same snapshot the JSON layer serves.

Not a router: four route modules answer with a settings tab -- the settings pages
themselves, the auto-approval forms, the connection forms and the password change
-- and a rejected save has to come back as the tab it was submitted from. Sharing
the renderer is what keeps those four from drifting into four settings pages.
"""

from __future__ import annotations

from fastapi import Request
from fastapi.responses import RedirectResponse

from app.api.settings import settings_snapshot
from app.web import deps


async def render_settings(
    request: Request,
    section: str,
    *,
    error: str | None = None,
    notice: str | None = None,
    status_code: int = 200,
    **extra: object,
):
    """Render one settings tab from the shared snapshot.

    Every settings response comes through here -- the seven GETs, and each of
    the POSTs that refuses something -- so a rejected save re-renders the
    page the operator was looking at rather than a reduced copy of it. R5's
    lesson, applied to the last domain that still had two renderings of one
    page.

    The context is `settings_snapshot`, the same coroutine
    `GET /api/v1/settings/{section}` serves. Anything a template needed that
    the snapshot does not carry would be invisible to the endpoint, so there
    is nothing computed here except the three things that belong to this one
    request: the CSRF token and the outcome message.

    `extra` is for the result of an action rather than a stored setting -- a
    trial run, a rendered path preview. Those exist for exactly one response
    and have no place in a snapshot of what is saved.
    """
    context = await settings_snapshot(request, section)
    context.update(
        {
            "csrf_token": request.session["csrf_token"],
            "error": error,
            "notice": notice,
        }
    )
    context.update(extra)
    return deps.templates(request).TemplateResponse(
        request=request,
        name="settings.html",
        context=context,
        status_code=status_code,
    )


def settings_redirect(request: Request, section: str) -> RedirectResponse:
    """See-other back to the tab a save was submitted from.

    Every settings POST keeps its own path -- an operator's bookmarked form
    action still works -- and lands the browser back on the tab that owns the
    form, so a save never navigates away from what was being edited.
    """
    return RedirectResponse(
        request.url_for("settings_section", section=section).path,
        status_code=303,
    )
