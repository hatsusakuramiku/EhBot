"""The 已下载内容 page as an operator meets it (R10).

Restores the domain cut on 2026-08-26 and reinstated on 2026-08-28, per §1.3.1
of the requirements document: a page listing downloaded works in a grid or a
list, with multi-select and batch 重新打包 / 移除 / 重新下载, plus per-work
rename and relocate.

Downloads are seeded straight into `download_jobs` in COMPLETED, which the
download worker does not claim, so the rows under test do not move mid-assertion
-- the same reason `test_activity_web.py` avoids PENDING. The one test that does
produce a PENDING row asserts on it *after* the app has been shut down.
"""

from __future__ import annotations

import asyncio
from html import unescape
import itertools
from pathlib import Path
import re

from fastapi.testclient import TestClient

from app.config import Settings
from app.db.database import Database
from app.downloads.models import (
    CONVERSION_STATE_COMPLETED,
    CONVERSION_STATE_FAILED,
    CONVERSION_STATE_WAITING_PASSWORD,
    DOWNLOAD_STATE_COMPLETED,
    PROVIDER_CONVERSION,
    PROVIDER_TELEGRAM,
)
from app.main import create_app
from tests.integration.markup import (
    gated_targets,
    nested_form_lines,
    ungated_targets,
)


#: The five tabs, which is every page that renders a work row.
DOWNLOADED_TABS = (
    "/downloaded",
    "/downloaded/unpacked",
    "/downloaded/packed",
    "/downloaded/attention",
    "/downloaded/failed",
)

_KEYS = itertools.count(1)


def make_settings(root: Path) -> Settings:
    return Settings(
        data_path=root / "data",
        library_path=root / "library",
        work_path=root / "work",
        app_secret_key="test-secret-key-with-at-least-32-characters",
        tag_translation_enabled=False,
        archive_toolchain_auto_install=False,
        torrent_enabled=False,
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
    change_page = client.get("/settings/passwords")
    client.post(
        "/change-password",
        data={
            "current_password": bootstrap_password,
            "new_password": "new-password-with-12-characters",
            "confirmation": "new-password-with-12-characters",
            "csrf_token": change_page.context["csrf_token"],
        },
    )


class Library:
    """Seeded downloaded works, with their files really on disk."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.database = Database(settings.data_path / "ehbot.db")
        asyncio.run(self.database.initialize())
        settings.library_path.mkdir(parents=True, exist_ok=True)
        settings.work_path.mkdir(parents=True, exist_ok=True)

    def add(
        self,
        *,
        title: str,
        pack_state: str | None = None,
        packaged: bool = False,
        pack_error: str | None = None,
        cbz_relative: str = None,
    ) -> int:
        with self.database._connect() as connection:  # noqa: SLF001
            candidate_id = int(
                connection.execute(
                    "INSERT INTO candidates (status) VALUES ('DOWNLOADED')"
                ).lastrowid
            )
            connection.execute(
                "INSERT INTO metadata_values (candidate_id, field_name, "
                "field_value, value_source, confidence, is_manual) "
                "VALUES (?, 'Title', ?, 'MANUAL', 1.0, 1)",
                (candidate_id, title),
            )
            job_id = int(
                connection.execute(
                    "INSERT INTO download_jobs (candidate_id, "
                    "idempotency_key, provider, state, details_json) "
                    "VALUES (?, ?, ?, ?, '{}')",
                    (
                        candidate_id,
                        f"downloaded:{next(_KEYS)}",
                        PROVIDER_TELEGRAM,
                        DOWNLOAD_STATE_COMPLETED,
                    ),
                ).lastrowid
            )
        archive = self.settings.work_path / f"source-{candidate_id}.zip"
        archive.write_bytes(b"archive payload")
        self._artifact(job_id, "ARCHIVE", archive)

        if pack_state is not None:
            with self.database._connect() as connection:  # noqa: SLF001
                pack_id = int(
                    connection.execute(
                        "INSERT INTO download_jobs (candidate_id, "
                        "idempotency_key, provider, state, error_code, "
                        "error_message, details_json) "
                        "VALUES (?, ?, ?, ?, ?, ?, '{}')",
                        (
                            candidate_id,
                            f"convert:{candidate_id}",
                            PROVIDER_CONVERSION,
                            pack_state,
                            "CONVERSION_FAILED" if pack_error else None,
                            pack_error,
                        ),
                    ).lastrowid
                )
            if packaged:
                relative = cbz_relative or f"作者/{title}.cbz"
                cbz = self.settings.library_path / relative
                cbz.parent.mkdir(parents=True, exist_ok=True)
                cbz.write_bytes(b"cbz payload")
                self._artifact(pack_id, "CBZ", cbz)
        return candidate_id

    def _artifact(self, job_id: int, kind: str, path: Path) -> None:
        with self.database._connect() as connection:  # noqa: SLF001
            connection.execute(
                "INSERT INTO artifacts (job_id, artifact_type, path, "
                "size_bytes, page_count) VALUES (?, ?, ?, ?, 12)",
                (job_id, kind, str(path), path.stat().st_size),
            )

    def job_states(self, candidate_id: int) -> list[tuple[str, str, int]]:
        with self.database._connect() as connection:  # noqa: SLF001
            return [
                (str(row[0]), str(row[1]), int(row[2]))
                for row in connection.execute(
                    "SELECT provider, state, attempt_count FROM download_jobs "
                    "WHERE candidate_id = ? ORDER BY id",
                    (candidate_id,),
                )
            ]

    def removals(self) -> list[tuple[int, int]]:
        with self.database._connect() as connection:  # noqa: SLF001
            return [
                (int(row[0]), int(row[1]))
                for row in connection.execute(
                    "SELECT candidate_id, deleted_files FROM removed_works "
                    "ORDER BY id"
                )
            ]

    def cbz_path(self, candidate_id: int) -> str | None:
        work = asyncio.run(self.database.downloaded_work(candidate_id))
        return None if work is None else work.cbz_path


def seeded(tmp_path: Path) -> tuple[Settings, Library, dict[str, int]]:
    """One work in each of the four non-trivial states."""
    settings = make_settings(tmp_path)
    library = Library(settings)
    ids = {
        "unpacked": library.add(title="未打包作品"),
        "packed": library.add(
            title="已打包作品",
            pack_state=CONVERSION_STATE_COMPLETED,
            packaged=True,
        ),
        "attention": library.add(
            title="缺密码作品", pack_state=CONVERSION_STATE_WAITING_PASSWORD
        ),
        "failed": library.add(
            title="打包失败作品",
            pack_state=CONVERSION_STATE_FAILED,
            pack_error="压缩包缺少分卷",
        ),
    }
    return settings, library, ids


def logged_in(settings: Settings) -> TestClient:
    client = TestClient(create_app(settings))
    client.__enter__()
    authenticate(client, settings)
    return client


def work_ids(text: str) -> list[int]:
    return [int(value) for value in re.findall(r'data-work-id="(\d+)"', text)]


def test_each_tab_shows_exactly_the_works_it_is_named_for(
    tmp_path: Path,
) -> None:
    """The five tabs, filtered on facts rather than on the candidate's status.

    Packaging never touches `candidates.status`, so the status column cannot
    answer 「打好包了吗」. Each filter reads the CBZ artifact or the packing job.
    """
    settings, _library, ids = seeded(tmp_path)
    client = logged_in(settings)
    try:
        pages = {path: client.get(path) for path in DOWNLOADED_TABS}
    finally:
        client.__exit__(None, None, None)

    for path, response in pages.items():
        assert response.status_code == 200, path

    assert set(work_ids(pages["/downloaded"].text)) == set(ids.values())
    assert work_ids(pages["/downloaded/unpacked"].text) == [ids["unpacked"]]
    assert work_ids(pages["/downloaded/packed"].text) == [ids["packed"]]
    assert work_ids(pages["/downloaded/attention"].text) == [ids["attention"]]
    assert work_ids(pages["/downloaded/failed"].text) == [ids["failed"]]

    # The tab strip counts what the list will produce, from the same join.
    counts = pages["/downloaded"].context["counts"]
    assert counts == {
        "all": 4,
        "packed": 1,
        "unpacked": 1,
        "attention": 1,
        "failed": 1,
    }


def test_a_work_still_downloading_is_not_downloaded_content(
    tmp_path: Path,
) -> None:
    """The list is the finished shelf, so an in-flight job is excluded.

    Asserted through re-download, which is also the action that creates the
    situation: the row goes back to PENDING and drops off the page until the
    archive lands again.
    """
    settings, library, ids = seeded(tmp_path)
    client = logged_in(settings)
    try:
        page = client.get("/downloaded/packed")
        response = client.post(
            "/downloaded/batch",
            data={
                "csrf_token": page.context["csrf_token"],
                "tab": "packed",
                "action": "redownload",
                "candidate_ids": [ids["packed"]],
            },
            follow_redirects=False,
        )
        assert response.status_code == 303
    finally:
        # Shut the app down before reading: the download worker is running and
        # would claim the PENDING row this just created.
        client.__exit__(None, None, None)

    works, total = asyncio.run(
        library.database.list_downloaded_works(pack_filter="all")
    )
    assert ids["packed"] not in {work.candidate_id for work in works}
    assert total == 3
    # ...but the work itself is still findable, which is what keeps the guards
    # on the action paths reachable.
    assert asyncio.run(
        library.database.downloaded_work(ids["packed"])
    ) is not None


def test_an_unknown_tab_is_a_404_not_a_page_titled_with_the_typo(
    tmp_path: Path,
) -> None:
    settings, _library, _ids = seeded(tmp_path)
    client = logged_in(settings)
    try:
        assert client.get("/downloaded/nonsense").status_code == 404
        assert client.get("/api/v1/downloaded?tab=nonsense").status_code == 400
    finally:
        client.__exit__(None, None, None)


def test_the_page_and_the_endpoint_cannot_disagree(tmp_path: Path) -> None:
    """One snapshot feeds both, so the page context is a superset of the JSON.

    The page adds `csrf_token`, its tab strip, the empty-state copy and the
    paging hrefs; every key the endpoint serves has to be in there too.
    """
    settings, _library, _ids = seeded(tmp_path)
    client = logged_in(settings)
    try:
        for tab, path in (
            ("all", "/downloaded"),
            ("unpacked", "/downloaded/unpacked"),
            ("packed", "/downloaded/packed"),
            ("attention", "/downloaded/attention"),
            ("failed", "/downloaded/failed"),
        ):
            page = client.get(path)
            api = client.get(f"/api/v1/downloaded?tab={tab}")
            assert api.status_code == 200
            page_keys = set(page.context)
            api_keys = set(api.json())
            assert page_keys.issuperset(api_keys), (
                f"{tab}: page missing {api_keys - page_keys}"
            )
            assert page.context["works"] == api.json()["works"]
    finally:
        client.__exit__(None, None, None)


def test_the_grid_and_the_list_render_the_same_selection(
    tmp_path: Path,
) -> None:
    """§1.3.1「可按网格、列表展示」-- two renderings of one form, not two pages.

    The view lives in the query string with the rest of the page state, so a
    filtered grid is a link an operator can send themselves.
    """
    settings, _library, ids = seeded(tmp_path)
    client = logged_in(settings)
    try:
        grid = client.get("/downloaded?view=grid")
        listing = client.get("/downloaded?view=list")
    finally:
        client.__exit__(None, None, None)

    assert set(work_ids(grid.text)) == set(work_ids(listing.text)) == set(
        ids.values()
    )
    assert "ui-cover-grid" in grid.text
    assert "ui-cover-grid" not in listing.text
    assert "ui-table" in listing.text
    # Both carry one checkbox per work, named for the batch endpoint.
    for body in (grid.text, listing.text):
        assert body.count('name="candidate_ids"') == len(ids)


def test_a_state_word_is_never_written_by_the_page(tmp_path: Path) -> None:
    """Every badge arrives as a resolved `StatusView`.

    Asserted through the raw code riding in `data-code`: if the template were
    writing its own words, the badge would carry no code to read.
    """
    settings, _library, _ids = seeded(tmp_path)
    client = logged_in(settings)
    try:
        body = client.get("/downloaded").text
    finally:
        client.__exit__(None, None, None)

    codes = set(re.findall(r'<span class="badge"[^>]*data-code="([^"]*)"', body))
    assert {"unpacked", "packed", "attention", "failed"} <= codes
    # And the raw code never reaches a tooltip or the accessibility tree.
    assert 'title="unpacked"' not in body
    assert 'title="CONVERSION_WAITING_PASSWORD"' not in body


def test_no_row_action_is_a_nested_form(tmp_path: Path) -> None:
    """Every row lives inside the batch form, and HTML does not nest forms.

    A `<form>` start tag inside another is *ignored*, so a row action written as
    its own form would submit the batch endpoint with no action -- 200 the whole
    time. That shipped broken once on `/activity`.
    """
    settings, _library, _ids = seeded(tmp_path)
    client = logged_in(settings)
    try:
        bodies = [
            client.get(f"{path}?view={view}").text
            for path in DOWNLOADED_TABS
            for view in ("grid", "list")
        ]
    finally:
        client.__exit__(None, None, None)

    for body in bodies:
        assert nested_form_lines(body) == []


def test_only_the_destructive_actions_ask_first(tmp_path: Path) -> None:
    """A confirmation on every button is how an operator learns to dismiss them.

    Packing costs CPU and replaces a file the operator just asked to replace, so
    it goes through on one click. Removal and re-download destroy or overwrite,
    so they stand behind a dialog -- and the two removals are two dialogs,
    because 「默认不移除文件」 is only true if the operator can see which one
    they picked.
    """
    settings, _library, _ids = seeded(tmp_path)
    client = logged_in(settings)
    try:
        body = client.get("/downloaded").text
    finally:
        client.__exit__(None, None, None)

    gated, ungated = gated_targets(body), ungated_targets(body)
    # The gated controls all submit the batch form by id.
    assert "downloaded-batch" in gated
    assert "/downloaded/batch" in ungated

    # Four dialog bodies: re-download, and the two removal variants...
    dialog_values = set(
        re.findall(r'form="downloaded-batch"[^>]*value="([^"]+)"', body)
    )
    assert dialog_values == {"redownload", "remove", "remove-files"}
    # ...while 批量打包 is a plain submit button inside the form.
    assert 'name="action" value="repack"' in body


def test_packing_one_work_uses_the_row_button_and_the_one_packing_path(
    tmp_path: Path,
) -> None:
    settings, library, ids = seeded(tmp_path)
    client = logged_in(settings)
    try:
        page = client.get("/downloaded/unpacked")
        assert f'/downloaded/{ids["unpacked"]}/repack' in page.text
        response = client.post(
            f"/downloaded/{ids['unpacked']}/repack",
            data={
                "csrf_token": page.context["csrf_token"],
                "tab": "unpacked",
            },
            follow_redirects=False,
        )
        assert response.status_code == 303
        assert response.headers["location"].startswith("/downloaded/unpacked?")
    finally:
        client.__exit__(None, None, None)

    states = library.job_states(ids["unpacked"])
    assert (PROVIDER_CONVERSION, "CONVERSION_PENDING", 0) in states


def test_repacking_a_finished_book_requeues_it_rather_than_reporting_success(
    tmp_path: Path,
) -> None:
    """The bug this page was built on top of.

    `_enqueue_sync` used to requeue only WAITING_VOLUMES and WAITING_PASSWORD,
    so 重新打包 on a COMPLETED task hit `ON CONFLICT DO NOTHING`, left the row
    COMPLETED -- a state the worker never claims -- and 303'd back to a page
    reporting success while the CBZ on disk was untouched.
    """
    settings, library, ids = seeded(tmp_path)
    client = logged_in(settings)
    try:
        page = client.get("/downloaded/packed")
        client.post(
            "/downloaded/batch",
            data={
                "csrf_token": page.context["csrf_token"],
                "tab": "packed",
                "action": "repack",
                "candidate_ids": [ids["packed"]],
            },
            follow_redirects=False,
        )
    finally:
        client.__exit__(None, None, None)

    packing = [
        entry for entry in library.job_states(ids["packed"])
        if entry[0] == PROVIDER_CONVERSION
    ]
    # One row, requeued -- never a second packing task for the same book.
    assert len(packing) == 1
    assert packing[0][1] != CONVERSION_STATE_COMPLETED


def test_removing_records_keeps_the_files_and_removing_files_does_not(
    tmp_path: Path,
) -> None:
    """§1.2.3「默认不移除已下载文件」, end to end through the two buttons."""
    settings, library, ids = seeded(tmp_path)
    keep, purge = ids["packed"], ids["failed"]
    kept_file = Path(library.cbz_path(keep))
    purged_archive = settings.work_path / f"source-{purge}.zip"

    client = logged_in(settings)
    try:
        page = client.get("/downloaded")
        records_only = client.post(
            "/downloaded/batch",
            data={
                "csrf_token": page.context["csrf_token"],
                "tab": "all",
                "action": "remove",
                "candidate_ids": [keep],
            },
            follow_redirects=False,
        )
        with_files = client.post(
            "/downloaded/batch",
            data={
                "csrf_token": page.context["csrf_token"],
                "tab": "all",
                "action": "remove-files",
                "candidate_ids": [purge],
            },
            follow_redirects=False,
        )
    finally:
        client.__exit__(None, None, None)

    assert records_only.status_code == with_files.status_code == 303
    assert kept_file.exists()
    assert not purged_archive.exists()
    # The audit row is what an operator reads later to tell the two apart.
    assert library.removals() == [(keep, 0), (purge, 1)]
    assert library.job_states(keep) == []


def test_a_batch_that_partly_applies_reports_both_halves(
    tmp_path: Path,
) -> None:
    """A replay approves what is left and says so, rather than refusing.

    The form has nowhere but the redirect to report it, so the count and the
    first reason are folded into the query string -- the same compromise
    `/activity` makes.
    """
    settings, library, ids = seeded(tmp_path)
    client = logged_in(settings)
    try:
        page = client.get("/downloaded")
        first = client.post(
            "/downloaded/batch",
            data={
                "csrf_token": page.context["csrf_token"],
                "tab": "all",
                "action": "remove",
                "candidate_ids": [ids["packed"]],
            },
            follow_redirects=False,
        )
        replay = client.post(
            "/downloaded/batch",
            data={
                "csrf_token": page.context["csrf_token"],
                "tab": "all",
                "action": "remove",
                "candidate_ids": [ids["packed"], ids["failed"]],
            },
            follow_redirects=False,
        )
    finally:
        client.__exit__(None, None, None)

    assert "notice=" in first.headers["location"]
    message = unescape(replay.headers["location"])
    assert "error=" in message
    assert "1" in message
    # The second work still went, which is the point of acting one at a time.
    assert library.removals() == [(ids["packed"], 0), (ids["failed"], 0)]


def test_renaming_a_book_moves_it_and_pins_where_a_repack_lands(
    tmp_path: Path,
) -> None:
    """§1.3.1「可修改文件保存路径、打包输出路径、文件名称」."""
    settings, library, ids = seeded(tmp_path)
    client = logged_in(settings)
    try:
        page = client.get("/downloaded/packed")
        # The drawer's trigger carries the work it acts on, and the form that
        # posts is a sibling of the batch form rather than nested inside it.
        assert f'data-rename-open="{ids["packed"]}"' in page.text
        response = client.post(
            f"/downloaded/{ids['packed']}/rename",
            data={
                "csrf_token": page.context["csrf_token"],
                "tab": "packed",
                "filename": "整理后的书名",
                "directory": "分类/作者",
            },
            follow_redirects=False,
        )
    finally:
        client.__exit__(None, None, None)

    assert response.status_code == 303
    moved = settings.library_path / "分类" / "作者" / "整理后的书名.cbz"
    assert moved.exists()
    assert library.cbz_path(ids["packed"]) == str(moved)
    work = asyncio.run(library.database.downloaded_work(ids["packed"]))
    assert work.library_relative_path == "分类/作者/整理后的书名.cbz"


def test_a_rename_cannot_walk_out_of_the_library(tmp_path: Path) -> None:
    settings, library, ids = seeded(tmp_path)
    original = Path(library.cbz_path(ids["packed"]))
    client = logged_in(settings)
    try:
        page = client.get("/downloaded/packed")
        response = client.post(
            f"/downloaded/{ids['packed']}/rename",
            data={
                "csrf_token": page.context["csrf_token"],
                "tab": "packed",
                "filename": "escaped",
                "directory": "../../escaped",
            },
            follow_redirects=False,
        )
    finally:
        client.__exit__(None, None, None)

    assert response.status_code == 303
    assert "error=" in response.headers["location"]
    assert original.exists()
    assert not (tmp_path.parent / "escaped").exists()


def test_the_page_state_survives_in_the_query_string(tmp_path: Path) -> None:
    settings, _library, ids = seeded(tmp_path)
    client = logged_in(settings)
    try:
        filtered = client.get("/downloaded?search=已打包&sort=title&view=list")
    finally:
        client.__exit__(None, None, None)

    assert work_ids(filtered.text) == [ids["packed"]]
    assert filtered.context["search"] == "已打包"
    assert filtered.context["sort"] == "title"
    assert filtered.context["view"] == "list"
    # Every generated href keeps the search, so no link widens the list the
    # operator is working in.
    assert "search=%E5%B7%B2%E6%89%93%E5%8C%85" in filtered.text


def test_a_bookmarked_url_with_junk_still_renders(tmp_path: Path) -> None:
    """A hand-edited page or sort is page one and the default sort, not a 422."""
    settings, _library, _ids = seeded(tmp_path)
    client = logged_in(settings)
    try:
        page = client.get("/downloaded?page=abc&sort=whatever&view=chart")
    finally:
        client.__exit__(None, None, None)

    assert page.status_code == 200
    assert page.context["page"] == 1
    assert page.context["sort"] == "newest"
    assert page.context["view"] == "grid"


def test_the_pack_error_is_shown_where_the_operator_can_act_on_it(
    tmp_path: Path,
) -> None:
    settings, _library, _ids = seeded(tmp_path)
    client = logged_in(settings)
    try:
        body = client.get("/downloaded/failed?view=list").text
    finally:
        client.__exit__(None, None, None)

    assert "压缩包缺少分卷" in body
    assert 'data-field="pack-error"' in body


def test_the_page_links_the_one_stylesheet_and_its_own_script(
    tmp_path: Path,
) -> None:
    settings, _library, _ids = seeded(tmp_path)
    client = logged_in(settings)
    try:
        body = client.get("/downloaded").text
        script = client.get("/static/downloaded.js")
    finally:
        client.__exit__(None, None, None)

    assert "ui.css" in body
    assert "app.css" not in body
    assert "data-legacy" not in body
    assert script.status_code == 200


def test_the_page_tells_its_script_whether_to_poll(tmp_path: Path) -> None:
    """`data-live` is the server's answer, and the reason a finished shelf is quiet.

    A library of packaged books must not wake the process every two seconds, so
    polling starts only when something on the page is actually moving.
    """
    settings, library, _ids = seeded(tmp_path)
    client = logged_in(settings)
    try:
        idle = client.get("/downloaded").text
        library.add(title="正在打包", pack_state="CONVERSION_RUNNING")
        busy = client.get("/downloaded").text
    finally:
        client.__exit__(None, None, None)

    assert 'data-live="false"' in idle
    assert 'data-live="true"' in busy


def test_every_action_needs_a_session_and_a_token(tmp_path: Path) -> None:
    settings, library, ids = seeded(tmp_path)
    with TestClient(create_app(settings)) as anonymous:
        for path in DOWNLOADED_TABS:
            response = anonymous.get(path, follow_redirects=False)
            assert response.status_code == 303
            assert response.headers["location"] == "/login"
        # A JSON caller gets 401, never a redirect: `fetch` follows one
        # silently and hands back a login page as 200.
        assert anonymous.get("/api/v1/downloaded").status_code == 401

    client = logged_in(settings)
    try:
        forged = client.post(
            "/downloaded/batch",
            data={
                "csrf_token": "wrong",
                "tab": "all",
                "action": "remove",
                "candidate_ids": [ids["packed"]],
            },
            follow_redirects=False,
        )
    finally:
        client.__exit__(None, None, None)

    assert forged.status_code == 403
    assert library.removals() == []