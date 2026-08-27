"""Rendered-shell tests (R3).

These assert on the HTML rather than on the navigation model, because the
failures R3 exists to prevent are template failures: a second navigation
structure that drifts from the first, a page that loses the design tokens, an
overlay with no way out for a keyboard. `tests/unit/test_web_shell.py` covers the
model underneath.
"""

import re
from pathlib import Path

from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app
from app.web.routes import NAV_ITEMS


def make_settings(root: Path) -> Settings:
    return Settings(
        data_path=root / "data",
        library_path=root / "library",
        work_path=root / "work",
        app_secret_key="test-secret-key-with-at-least-32-characters",
        tag_translation_enabled=False,
        archive_toolchain_auto_install=False,
    )


def authenticate(client: TestClient, settings: Settings) -> None:
    bootstrap_password = (
        settings.data_path / "bootstrap_admin_password"
    ).read_text(encoding="utf-8")
    login_page = client.get("/login")
    client.post(
        "/login",
        data={
            "password": bootstrap_password,
            "csrf_token": login_page.context["csrf_token"],
        },
    )
    change_page = client.get("/change-password")
    client.post(
        "/change-password",
        data={
            "current_password": bootstrap_password,
            "new_password": "new-password-with-12-characters",
            "confirmation": "new-password-with-12-characters",
            "csrf_token": change_page.context["csrf_token"],
        },
    )


def _client(tmp_path: Path) -> tuple[TestClient, Settings]:
    settings = make_settings(tmp_path)
    client = TestClient(create_app(settings))
    client.__enter__()
    authenticate(client, settings)
    return client, settings


def test_every_page_renders_exactly_one_current_page_marker(
    tmp_path: Path,
) -> None:
    client, _ = _client(tmp_path)
    try:
        for path in (
            "/",
            "/candidates",
            "/candidates/needs-info",
            "/candidates/processing",
            "/candidates/failed",
            "/manual-add",
            "/downloads",
            "/downloads/history",
            "/connections",
            "/sources",
            "/auto-approval-rules",
            "/archive-settings",
        ):
            body = client.get(path).text
            # The sidebar and the tab bar each render the marker, so two is the
            # correct count for one logical current page. Three means a prefix
            # is matching a sibling; zero means the page fell out of the tree.
            count = body.count('aria-current="page"')
            assert count == 2, f"{path} rendered {count} current markers"
    finally:
        client.__exit__(None, None, None)


def test_the_two_navigations_render_the_same_destinations(
    tmp_path: Path,
) -> None:
    # This is the regression the phase exists to close: the old `base.html` had
    # two hand-written lists, and the mobile one carried a 历史 link the sidebar
    # never gained. One data source means the sets cannot diverge -- so assert
    # they do not.
    client, _ = _client(tmp_path)
    try:
        body = client.get("/candidates").text
    finally:
        client.__exit__(None, None, None)

    expected = set()
    for item in NAV_ITEMS:
        expected.add(item.path)
        for child in item.children:
            expected.add(child.path)

    for path in expected:
        assert f'href="{path}"' in body, f"{path} is missing from the shell"


def test_shell_provides_the_accessibility_primitives(tmp_path: Path) -> None:
    client, _ = _client(tmp_path)
    try:
        body = client.get("/").text
    finally:
        client.__exit__(None, None, None)

    # A keyboard user must be able to get past 14 navigation links.
    assert 'class="skip-link" href="#main"' in body
    assert 'id="main"' in body
    # The live region has to exist before anything writes to it: creating a
    # region and filling it in the same tick is the classic silent announcement.
    assert 'aria-live="polite"' in body
    assert 'aria-label="主导航"' in body


def test_theme_is_applied_before_first_paint(tmp_path: Path) -> None:
    client, _ = _client(tmp_path)
    try:
        body = client.get("/").text
    finally:
        client.__exit__(None, None, None)

    head = body.split("</head>")[0]
    # Inline and in <head>: a deferred script would paint the light theme for
    # one frame on every navigation, which reads as a flash on every click.
    assert "ehbot.theme" in head
    assert "<script>" in head
    # `auto` must never reach the DOM as an attribute value -- neither theme
    # selector matches `[data-theme="auto"]`, so it would silently mean "light".
    assert 'data-theme="auto"' not in body


def test_legacy_pages_keep_the_light_lock(tmp_path: Path) -> None:
    # `app.css` still hardcodes a light background and a light `--ink`. Until a
    # page is rewritten it must stay pinned to light, or dark mode renders its
    # muted text at about 3.2:1 -- below the 4.5:1 this phase must meet.
    client, _ = _client(tmp_path)
    try:
        legacy = client.get("/sources").text
        rewritten = client.get("/ui-kit").text
    finally:
        client.__exit__(None, None, None)

    assert 'data-legacy="true"' in legacy
    assert 'data-legacy="true"' not in rewritten


def test_both_stylesheets_load_in_order(tmp_path: Path) -> None:
    client, _ = _client(tmp_path)
    try:
        body = client.get("/").text
    finally:
        client.__exit__(None, None, None)

    # `ui.css` must come second: it is the authoritative layer, and the split
    # exists so R9's cleanup is `rm app.css` rather than a careful cut.
    assert body.index("app.css") < body.index("ui.css")


def test_vendored_assets_are_served_not_fetched(tmp_path: Path) -> None:
    client, _ = _client(tmp_path)
    try:
        body = client.get("/").text
        for asset in re.findall(r'src="(/static/[^"]+)"', body):
            assert client.get(asset).status_code == 200, asset
    finally:
        client.__exit__(None, None, None)

    # No CDN: the container runs without outbound access to unpkg or jsdelivr,
    # and a pinned vendored file is the only way the version stays fixed.
    assert "unpkg.com" not in body
    assert "cdn.jsdelivr.net" not in body


def test_ui_kit_requires_authentication(tmp_path: Path) -> None:
    with TestClient(create_app(make_settings(tmp_path))) as client:
        response = client.get("/ui-kit", follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"] == "/login"


def test_ui_kit_renders_every_state_label_from_python(tmp_path: Path) -> None:
    from app.api.status import (
        CANDIDATE_STATUS,
        CONNECTION_STATUS,
        CONVERSION_STATUS,
        DOWNLOAD_STATUS,
    )

    client, _ = _client(tmp_path)
    try:
        body = client.get("/ui-kit").text
    finally:
        client.__exit__(None, None, None)

    # Every label in the gallery comes from `app/api/status.py`. If a template
    # ever writes 「已连接」 itself, the two can disagree and this is the phase
    # that removed that duplication.
    for registry in (
        CANDIDATE_STATUS,
        DOWNLOAD_STATUS,
        CONVERSION_STATUS,
        CONNECTION_STATUS,
    ):
        for code, view in registry.items():
            assert view.label in body, f"{code} label missing"
            assert f'data-tone="{view.tone}"' in body, f"{code} tone missing"


def test_ui_kit_renders_the_component_set(tmp_path: Path) -> None:
    client, _ = _client(tmp_path)
    try:
        body = client.get("/ui-kit").text
    finally:
        client.__exit__(None, None, None)

    for marker in (
        'class="badge"',            # status badge
        "ui-card-cover",            # cover card
        "ui-card-cover-empty",      # ...including the no-cover branch
        "ui-cover-grid",            # cover grid
        "ui-table",                 # data table
        "ui-sort",                  # sorting
        "ui-pagination",            # pagination
        "ui-bulkbar",               # bulk toolbar
        "ui-filters",               # filter sidebar
        "ui-skeleton",              # skeleton
        "ui-progress",              # progress
        "ui-drawer",                # drawer
        "ui-dialog",                # confirm dialog
        "ui-empty",                 # empty state
        "ui-tabs",                  # tabs
        "ui-segmented",             # theme / density controls
    ):
        assert marker in body, f"{marker} is not on the gallery page"


def test_gallery_overlays_are_dismissable_by_keyboard(tmp_path: Path) -> None:
    client, _ = _client(tmp_path)
    try:
        body = client.get("/ui-kit").text
    finally:
        client.__exit__(None, None, None)

    # `x-trap` is not vendored, so Escape and an explicit close button are the
    # whole story. An overlay a keyboard user cannot leave is worse than none.
    assert body.count("@keydown.escape.window") >= 2
    assert 'aria-modal="true"' in body
    assert 'aria-label="关闭"' in body


def test_gallery_cover_urls_stay_on_the_local_proxy(tmp_path: Path) -> None:
    client, _ = _client(tmp_path)
    try:
        body = client.get("/ui-kit").text
    finally:
        client.__exit__(None, None, None)

    srcs = re.findall(r'<img src="([^"]+)"', body)
    covers = [s for s in srcs if "/api/v1/thumbnails/" in s]
    assert covers, "no cover image rendered, so this test proves nothing"

    for src in srcs:
        # No image may be fetched from a third party. An upstream image host in
        # a cover `src` would leak the operator's IP to that server on every
        # page load, which is the whole reason the thumbnail proxy exists.
        host = re.match(r"https?://([^/]+)", src)
        assert host is None or host.group(1) == "testserver", src

    # And a cover in particular is the proxy path used verbatim: never rebuilt
    # here from a hash, so the route stays the single admission point.
    for src in covers:
        assert src.startswith("/api/v1/thumbnails/"), src


def test_table_cells_carry_labels_for_the_phone_layout(tmp_path: Path) -> None:
    client, _ = _client(tmp_path)
    try:
        body = client.get("/ui-kit").text
    finally:
        client.__exit__(None, None, None)

    # Below 640px the header row is dropped and each cell is prefixed with its
    # `data-label`. A cell without one becomes an anonymous value on a phone.
    assert 'data-label="状态"' in body
    assert 'data-label="来源"' in body
