"""The Jinja environment every page renders through.

One place, because the filters registered here are what enforce the rule the
templates are built on: a state's Chinese comes from `app/api/status.py` and is
never typed into a template. A router that constructed its own environment would
compile the same files without them, and the first symptom would be a page
printing `WAITING_TORRENT` at an operator.

Registered as both a filter and a global almost everywhere: a macro is called
(`ui.badge(status_view(code))`) and a value is piped (`job.state | status_view`),
and which one reads better depends on the call site.
"""

from __future__ import annotations

from pathlib import Path

from fastapi.templating import Jinja2Templates

from app.api.status import (
    candidate_tab_view,
    connection_view,
    dependency_view,
    provider_label,
    status_label,
    status_tone,
    status_view,
)
from app.web.routes import shell_context

#: Where the templates live. Resolved from this file rather than the working
#: directory so the app runs the same from a container's WORKDIR and from a
#: developer's checkout.
TEMPLATE_DIR = Path(__file__).parent / "templates"

#: Static assets. `ui.css` has been the only stylesheet since R9 removed
#: `app.css`; the vendored HTMX and Alpine builds live under `vendor/` and are
#: served from here rather than fetched, because the container runs with no
#: outbound access to a CDN.
STATIC_DIR = Path(__file__).parent / "static"


def build_templates() -> Jinja2Templates:
    """Build the environment, with the shell context and the status vocabulary.

    `shell_context` supplies `nav_items`, `current_path` and `active_domain` to
    every rendered page. A context processor rather than 25 edited
    `TemplateResponse` calls: the shell is a property of the response, not of each
    handler, and a handler that forgot to pass it would render a page with no
    navigation at all.
    """
    templates = Jinja2Templates(
        directory=TEMPLATE_DIR, context_processors=[shell_context]
    )
    # Labels, tones and provider names come from `app.api.status`, so a state
    # reads the same in a template as it does in a JSON response.
    templates.env.filters["status_label"] = status_label
    templates.env.globals["status_label"] = status_label
    templates.env.filters["status_tone"] = status_tone
    templates.env.globals["status_tone"] = status_tone
    templates.env.filters["provider_label"] = provider_label
    templates.env.globals["provider_label"] = provider_label
    # The badge macro takes a whole `StatusView`, not a label and a tone
    # separately, so that a template can never pair one state's label with
    # another's colour.
    templates.env.filters["status_view"] = status_view
    templates.env.globals["status_view"] = status_view
    templates.env.filters["connection_view"] = connection_view
    templates.env.globals["connection_view"] = connection_view
    # Whether a prerequisite is usable —「已就绪 / 未就绪」. The manual-add page
    # used to spell 「已配置」 and 「未配置」 inline, once per source, so the two
    # rows could disagree about the same fact after one edit.
    templates.env.filters["dependency_view"] = dependency_view
    templates.env.globals["dependency_view"] = dependency_view
    # Tab names for the workbench metrics, from the same vocabulary the tab strip
    # uses: 待审核 on the dashboard and 待审核 on `/candidates` are one string in
    # `app/api/status.py`.
    templates.env.globals["candidate_tab_view"] = candidate_tab_view
    return templates


__all__ = ["STATIC_DIR", "TEMPLATE_DIR", "build_templates"]
