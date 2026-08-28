"""The activity domain as an operator meets it: three tabs, no page jump (R4).

Jobs are written straight into `download_jobs` in states no worker claims
(PAUSED, FAILED, WAITING_TORRENT, CONVERSION_WAITING_PASSWORD). The app runs a
real download worker, so seeding a PENDING job would race it: the row under test
would move between the seed and the assertion. The states here are exactly the
ones the queue exists to show -- work that is waiting on the *operator*.
"""

from __future__ import annotations

import asyncio
import itertools
import json
from pathlib import Path
import re
import time
from urllib.parse import unquote_plus, urlsplit

from fastapi.testclient import TestClient

from app.config import Settings
from app.db.database import Database
from app.downloads.models import (
    CONVERSION_STATE_WAITING_PASSWORD,
    PROVIDER_CONVERSION,
    PROVIDER_EH_TORRENT,
)
from app.main import create_app
from tests.integration.markup import (
    gated_targets,
    nested_form_lines,
    ungated_targets,
)


#: The three tabs, which is every page that renders a job row.
ACTIVITY_TABS = ("/activity", "/activity/packing", "/activity/history")

#: Distinct idempotency keys across the whole module.
_KEYS = itertools.count(1)


def make_settings(root: Path) -> Settings:
    return Settings(
        data_path=root / "data",
        library_path=root / "library",
        work_path=root / "work",
        app_secret_key="test-secret-key-with-at-least-32-characters",
        tag_translation_enabled=False,
        archive_toolchain_auto_install=False,
        # The poller would otherwise reach for a qBittorrent that is not there
        # and could rewrite the parked torrent row mid-assertion.
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


def seed_candidate(database: Database) -> int:
    """One candidate the jobs can hang off. Its own state is irrelevant here."""

    async def run() -> int:
        await database.initialize()
        with database._connect() as connection:  # noqa: SLF001
            cursor = connection.execute(
                "INSERT INTO candidates (status) VALUES ('APPROVED')"
            )
            return int(cursor.lastrowid)

    return asyncio.run(run())


def insert_job(
    database: Database,
    candidate_id: int,
    *,
    state: str,
    provider: str = "TELEGRAM",
    error_code: str | None = None,
    details: dict | None = None,
) -> int:
    """One job in a chosen state, written straight into the table.

    The idempotency key is numbered from a counter rather than from a clock:
    `download_jobs.idempotency_key` is UNIQUE, and Windows' monotonic clock has a
    ~16ms granularity, so two jobs seeded back to back collided.
    """
    with database._connect() as connection:  # noqa: SLF001
        cursor = connection.execute(
            "INSERT INTO download_jobs "
            "(candidate_id, idempotency_key, provider, state, error_code, "
            "details_json) VALUES (?, ?, ?, ?, ?, ?)",
            (
                candidate_id,
                f"fixture:{state}:{next(_KEYS)}",
                provider,
                state,
                error_code,
                json.dumps(details or {}),
            ),
        )
        return int(cursor.lastrowid)


def job_row(database: Database, job_id: int) -> tuple[str, int]:
    with database._connect() as connection:  # noqa: SLF001
        row = connection.execute(
            "SELECT state, priority FROM download_jobs WHERE id = ?",
            (job_id,),
        ).fetchone()
    return str(row[0]), int(row[1])


class Fixture:
    """A client, its settings, and one job in each interesting state."""

    def __init__(self, tmp_path: Path) -> None:
        self.settings = make_settings(tmp_path)
        self.database = Database(self.settings.data_path / "ehbot.db")
        self.candidate_id = seed_candidate(self.database)
        self.paused = insert_job(
            self.database, self.candidate_id, state="PAUSED"
        )
        self.paused_second = insert_job(
            self.database, self.candidate_id, state="PAUSED"
        )
        self.failed = insert_job(
            self.database,
            self.candidate_id,
            state="FAILED",
            error_code="TELEGRAM_TEMPORARY",
        )
        self.stalled = insert_job(
            self.database,
            self.candidate_id,
            state="WAITING_TORRENT",
            provider=PROVIDER_EH_TORRENT,
            details={
                "progress": 0.42,
                "num_seeds": 0,
                "stalled_since": time.time() - 1800,
            },
        )
        self.packing = insert_job(
            self.database,
            self.candidate_id,
            state=CONVERSION_STATE_WAITING_PASSWORD,
            provider=PROVIDER_CONVERSION,
        )
        self.client = TestClient(
            create_app(self.settings), follow_redirects=False
        )
        self.client.__enter__()
        authenticate(self.client, self.settings)
        self.csrf = self.client.get("/activity").context["csrf_token"]

    def close(self) -> None:
        self.client.__exit__(None, None, None)

    def batch(self, action: str, job_ids: list[int], **extra):
        data: dict = {
            "csrf_token": self.csrf,
            "action": action,
            "job_ids": [str(job_id) for job_id in job_ids],
        }
        data.update(extra)
        return self.client.post("/activity/jobs/batch", data=data)


def visible_text(body: str) -> str:
    """The page with its tags removed -- what a person actually reads."""
    return re.sub(r"<[^>]+>", " ", body)


def redirect_message(response) -> str:
    """The message a form post carries back, decoded.

    `_activity_redirect` puts it in the query string through `quote_plus`, so a
    Chinese message arrives percent-encoded and a raw `in` check against the
    Location header would pass no matter what it said.
    """
    query = urlsplit(response.headers["location"]).query
    if not query.startswith("error="):
        return ""
    return unquote_plus(query[len("error=") :])


def page_heading(body: str) -> str:
    """The `<h1>` this tab renders, which is the only place it names itself.

    The tab strip repeats all three labels on every tab, so a plain substring
    check would pass on the wrong page.
    """
    match = re.search(r'<h1 class="ui-title">([^<]*)</h1>', body)
    return match.group(1).strip() if match else ""



# ------------------------------------------------------------ the three tabs


def test_each_tab_renders_and_names_itself(tmp_path: Path) -> None:
    fixture = Fixture(tmp_path)
    try:
        for path, heading in (
            ("/activity", "队列"),
            ("/activity/packing", "打包"),
            ("/activity/history", "历史"),
        ):
            page = fixture.client.get(path)
            assert page.status_code == 200, path
            assert page_heading(page.text) == heading, path
    finally:
        fixture.close()


def test_no_tab_reloads_itself_with_a_meta_refresh(tmp_path: Path) -> None:
    # The acceptance criterion:「进度局部更新且页面不跳动」. A meta refresh threw
    # away scroll position, an open menu and any checkbox selection with it.
    fixture = Fixture(tmp_path)
    try:
        for path in ("/activity", "/activity/packing", "/activity/history"):
            body = fixture.client.get(path).text
            assert 'http-equiv="refresh"' not in body, path
            assert "data-activity-root" in body, path
    finally:
        fixture.close()


def test_polling_is_armed_only_while_something_can_still_move(
    tmp_path: Path,
) -> None:
    fixture = Fixture(tmp_path)
    try:
        # The parked torrent is live: only another request can reveal a peer.
        assert 'data-live="true"' in fixture.client.get("/activity").text
        fixture.batch("cancel", [fixture.stalled])
        # Nothing left advances on its own, so the page stops asking. This is
        # what keeps an idle tab from waking the process every two seconds.
        assert 'data-live="false"' in fixture.client.get("/activity").text
    finally:
        fixture.close()


def test_the_old_paths_still_work_and_land_on_the_new_ones(
    tmp_path: Path,
) -> None:
    # A bookmark and any link in an old Telegram notification both point at the
    # old page, and a 404 on a page that used to work is worse than a hop.
    fixture = Fixture(tmp_path)
    try:
        for old, new in (
            ("/downloads", "/activity"),
            ("/downloads/history", "/activity/history"),
        ):
            response = fixture.client.get(old)
            assert response.status_code == 307, old
            assert response.headers["location"] == new
    finally:
        fixture.close()


# --------------------------------------------------------------- grouping


def test_the_queue_is_grouped_and_each_heading_counts_its_own_rows(
    tmp_path: Path,
) -> None:
    fixture = Fixture(tmp_path)
    try:
        body = fixture.client.get("/activity").text
        # Two sections: the failed job and the stalled torrent need the
        # operator, the two paused jobs are held back.
        assert body.count('data-group="attention"') == 1
        assert body.count('data-group="paused"') == 1
        # Nothing is running or queued, so those two sections are absent
        # entirely rather than rendered as「进行中 0」.
        assert 'data-group="active"' not in body
        assert 'data-group="waiting"' not in body
        counts = re.findall(r'data-field="group-count">(\d+)<', body)
        assert counts == ["2", "2"]
    finally:
        fixture.close()


def test_a_packaging_job_appears_only_on_its_own_tab(tmp_path: Path) -> None:
    # The confusion this split exists to end: a packaging job carries
    # `provider='CONVERSION'`, never competes for a download slot, and used to
    # be counted in with the downloads.
    fixture = Fixture(tmp_path)
    try:
        queue = fixture.client.get("/activity").text
        packing = fixture.client.get("/activity/packing").text
        assert f'data-job-id="{fixture.packing}"' not in queue
        assert f'data-job-id="{fixture.packing}"' in packing
        assert f'data-job-id="{fixture.stalled}"' in queue
        assert f'data-job-id="{fixture.stalled}"' not in packing
    finally:
        fixture.close()


def test_a_state_never_reaches_the_operator_as_an_enum(tmp_path: Path) -> None:
    fixture = Fixture(tmp_path)
    try:
        for path in ("/activity", "/activity/packing"):
            text = visible_text(fixture.client.get(path).text)
            for enum in (
                "WAITING_TORRENT",
                "CONVERSION_WAITING_PASSWORD",
                "PAUSED",
                "EH_TORRENT",
            ):
                assert enum not in text, f"{enum} leaked onto {path}"
        text = visible_text(fixture.client.get("/activity").text)
        assert "等待做种" in text
        assert "已暂停" in text
        assert "EH 种子" in text
    finally:
        fixture.close()


# -------------------------------------------------- the attention roll-up


def test_the_roll_up_is_on_every_tab_and_on_the_workbench(
    tmp_path: Path,
) -> None:
    fixture = Fixture(tmp_path)
    try:
        # Three: the failed download, the stalled torrent, the packaging job
        # waiting on a password. The last is on another tab, which is the point
        # -- an operator must not have to go looking for it.
        for path in (
            "/activity",
            "/activity/packing",
            "/activity/history",
            "/",
        ):
            text = visible_text(fixture.client.get(path).text)
            assert "有 3 项任务需要处理" in text, path
            assert "缺少解压密码" in text, path
            assert "种子无做种者" in text, path
    finally:
        fixture.close()


def test_the_banner_is_absent_rather_than_zero_when_nothing_is_waiting(
    tmp_path: Path,
) -> None:
    settings = make_settings(tmp_path)
    Database(settings.data_path / "ehbot.db")
    client = TestClient(create_app(settings), follow_redirects=False)
    client.__enter__()
    try:
        authenticate(client, settings)
        for path in ("/activity", "/"):
            text = visible_text(client.get(path).text)
            assert "需要处理" not in text, path
    finally:
        client.__exit__(None, None, None)


# --------------------------------------------------------- batch actions


def test_a_batch_repeated_changes_nothing_the_first_one_did(
    tmp_path: Path,
) -> None:
    # 「批量动作幂等」. The operator double-clicks「批量取消」, or the browser
    # replays the post: the second one has to be a no-op, not a 500 and not a
    # second state transition.
    fixture = Fixture(tmp_path)
    try:
        selection = [fixture.paused, fixture.paused_second]
        first = fixture.batch("cancel", selection)
        assert first.status_code == 303
        assert redirect_message(first) == ""
        after_first = [job_row(fixture.database, j) for j in selection]
        assert [state for state, _ in after_first] == ["CANCELLED", "CANCELLED"]

        second = fixture.batch("cancel", selection)
        assert second.status_code == 303
        assert redirect_message(second) == ""
        assert [job_row(fixture.database, j) for j in selection] == after_first
    finally:
        fixture.close()


def test_one_impossible_job_does_not_stop_the_rest_of_the_selection(
    tmp_path: Path,
) -> None:
    fixture = Fixture(tmp_path)
    try:
        # A job id that no longer exists, mixed in with two real ones. A batch
        # that refused the whole selection would leave the operator re-selecting
        # 48 rows to find the one that moved -- so the bad id is reported as a
        # skip, with its reason, and the other two still run.
        response = fixture.batch(
            "cancel", [fixture.paused, 999_999, fixture.paused_second]
        )
        assert response.status_code == 303
        message = redirect_message(response)
        assert "2 个任务已执行，1 个跳过" in message
        assert "下载任务不存在" in message
        for job_id in (fixture.paused, fixture.paused_second):
            assert job_row(fixture.database, job_id)[0] == "CANCELLED"
    finally:
        fixture.close()


def test_a_batch_reports_the_jobs_that_could_not_take_the_action(
    tmp_path: Path,
) -> None:
    # A selection is rarely uniform: the operator ticks a section and one row in
    # it has since moved on. The rest of the selection still runs, and the ones
    # that did not are named with the reason -- not dropped, and not a 500.
    fixture = Fixture(tmp_path)
    try:
        fixture.batch("cancel", [fixture.paused])
        response = fixture.batch(
            "priority", [fixture.paused, fixture.paused_second], priority="7"
        )
        assert response.status_code == 303
        message = redirect_message(response)
        assert "1 个任务已执行，1 个跳过" in message
        assert "已结束的任务没有队列位置" in message
        # The cancelled job keeps the default it never left; the live one moved.
        assert job_row(fixture.database, fixture.paused)[1] == 100
        assert job_row(fixture.database, fixture.paused_second)[1] == 7
    finally:
        fixture.close()


def test_a_priority_batch_reorders_every_job_it_names(tmp_path: Path) -> None:
    fixture = Fixture(tmp_path)
    try:
        response = fixture.batch(
            "priority",
            [fixture.paused, fixture.stalled],
            priority="5",
        )
        assert response.status_code == 303
        assert redirect_message(response) == ""
        for job_id in (fixture.paused, fixture.stalled):
            assert job_row(fixture.database, job_id)[1] == 5
        # Untouched jobs keep the default, so a batch cannot reorder the queue
        # wider than the selection.
        assert job_row(fixture.database, fixture.paused_second)[1] == 100
    finally:
        fixture.close()


def test_a_priority_batch_with_no_number_is_refused_not_a_500(
    tmp_path: Path,
) -> None:
    # The toolbar has a number input, but a form can always arrive without it.
    # `apply_job_batch` bounds the argument before touching a job, so this comes
    # back as a message on the page rather than as a crash.
    fixture = Fixture(tmp_path)
    try:
        response = fixture.batch("priority", [fixture.paused])
        assert response.status_code == 303
        assert redirect_message(response) == "请指定优先级"
        assert job_row(fixture.database, fixture.paused)[1] == 100
    finally:
        fixture.close()


def test_a_priority_outside_the_range_is_refused_before_any_job_moves(
    tmp_path: Path,
) -> None:
    # Checked before the loop, not per job, so a bad number cannot reorder the
    # first half of a selection and then give up.
    fixture = Fixture(tmp_path)
    try:
        response = fixture.batch(
            "priority", [fixture.paused, fixture.stalled], priority="0"
        )
        assert response.status_code == 303
        assert "优先级需在 1 到 999 之间" in redirect_message(response)
        for job_id in (fixture.paused, fixture.stalled):
            assert job_row(fixture.database, job_id)[1] == 100
    finally:
        fixture.close()


def test_an_unknown_batch_action_is_refused(tmp_path: Path) -> None:
    fixture = Fixture(tmp_path)
    try:
        response = fixture.batch("delete-everything", [fixture.paused])
        assert response.status_code == 303
        assert "未知的任务动作" in redirect_message(response)
        assert job_row(fixture.database, fixture.paused)[0] == "PAUSED"
    finally:
        fixture.close()


def test_a_batch_with_nothing_selected_says_so(tmp_path: Path) -> None:
    fixture = Fixture(tmp_path)
    try:
        response = fixture.batch("cancel", [])
        assert response.status_code == 303
        assert redirect_message(response) == "请至少选择一个任务"
    finally:
        fixture.close()


def test_a_batch_needs_the_csrf_token(tmp_path: Path) -> None:
    fixture = Fixture(tmp_path)
    try:
        response = fixture.client.post(
            "/activity/jobs/batch",
            data={
                "csrf_token": "forged",
                "action": "cancel",
                "job_ids": [str(fixture.paused)],
            },
        )
        assert response.status_code == 403
        assert job_row(fixture.database, fixture.paused)[0] == "PAUSED"
    finally:
        fixture.close()


# ----------------------------------------------------- single-row actions


def test_a_row_action_returns_to_the_tab_it_came_from(tmp_path: Path) -> None:
    fixture = Fixture(tmp_path)
    try:
        response = fixture.client.post(
            f"/activity/jobs/{fixture.packing}/cancel",
            data={"csrf_token": fixture.csrf},
            headers={"referer": "http://testserver/activity/packing"},
        )
        assert response.status_code == 303
        assert response.headers["location"].startswith("/activity/packing")

        response = fixture.client.post(
            f"/activity/jobs/{fixture.paused}/cancel",
            data={"csrf_token": fixture.csrf},
        )
        assert response.status_code == 303
        assert response.headers["location"].startswith("/activity")
    finally:
        fixture.close()


def test_a_refused_row_action_says_why_instead_of_failing(
    tmp_path: Path,
) -> None:
    fixture = Fixture(tmp_path)
    try:
        # Resume only means anything for a paused job; the stalled torrent is
        # not paused, and the answer is a message, not a 500.
        response = fixture.client.post(
            f"/activity/jobs/{fixture.stalled}/resume",
            data={"csrf_token": fixture.csrf},
        )
        assert response.status_code == 303
        assert redirect_message(response) != ""
        assert job_row(fixture.database, fixture.stalled)[0] == "WAITING_TORRENT"
    finally:
        fixture.close()


def test_switch_source_is_reachable_and_not_shadowed(tmp_path: Path) -> None:
    # Starlette matches in declaration order, so this route has to stay above
    # the catch-all `/activity/jobs/{job_id}/{action}`. Declared after it, every
    # request landed on the catch-all and was refused as an unknown action --
    # which is what this asserts against. An unsupported provider is used so the
    # check costs nothing: it proves the request reached `switch_source`, since
    # only `switch_source` knows the word「不支持切换到」, while the catch-all
    # would have answered「未知的任务动作」.
    fixture = Fixture(tmp_path)
    try:
        response = fixture.client.post(
            f"/activity/jobs/{fixture.stalled}/switch-source",
            data={"csrf_token": fixture.csrf, "provider": "NOT_A_PROVIDER"},
        )
        assert response.status_code == 303
        message = redirect_message(response)
        assert "不支持切换到" in message
        assert "未知的任务动作" not in message
        # Refused, so the torrent is still parked and still the operator's to
        # decide about.
        assert job_row(fixture.database, fixture.stalled)[0] == "WAITING_TORRENT"
    finally:
        fixture.close()


# ------------------------------------------------ the markup a click needs


def test_no_row_action_is_swallowed_by_the_batch_form(tmp_path: Path) -> None:
    """The bug this file did not catch until R9.

    Every row sits inside the batch form, and HTML does not nest forms: a
    `<form>` start tag inside another one is *ignored*, so each row's own form
    was discarded and its buttons submitted the batch endpoint with no action.
    The page rendered, returned 200, and did nothing anyone asked for. Only the
    parser sees it, which is why this asserts on the parse and not on a string.
    """
    fixture = Fixture(tmp_path)
    try:
        pages = [fixture.client.get(path).text for path in ACTIVITY_TABS]
    finally:
        fixture.close()

    for path, page in zip(ACTIVITY_TABS, pages):
        assert nested_form_lines(page) == [], path


def test_a_row_action_posts_to_its_own_job(tmp_path: Path) -> None:
    # `formaction` is what replaced the nested forms, so every row control has
    # to carry its own job id rather than inherit the batch form's action.
    fixture = Fixture(tmp_path)
    try:
        body = fixture.client.get("/activity").text
    finally:
        fixture.close()

    reachable = gated_targets(body) | ungated_targets(body)
    assert f"/activity/jobs/{fixture.paused}/resume" in reachable
    assert f"/activity/jobs/{fixture.paused_second}/resume" in reachable
    assert f"/activity/jobs/{fixture.failed}/retry" in reachable
    # Two paused rows, two distinct targets -- the failure mode being ruled out
    # is one shared endpoint that acts on whichever job the server picks.
    assert f"/activity/jobs/{fixture.paused}/retry" != (
        f"/activity/jobs/{fixture.paused_second}/retry"
    )


def test_the_costly_and_destructive_row_actions_ask_first(
    tmp_path: Path,
) -> None:
    """EHBot.md 8.8: a second step in front of anything hard to undo.

    Cancelling abandons work that has already run, and switching source can
    spend GP. Retrying or resuming a job costs nothing and stays one click --
    a confirmation on every button teaches an operator to dismiss them.
    """
    fixture = Fixture(tmp_path)
    try:
        body = fixture.client.get("/activity").text
    finally:
        fixture.close()

    gated = gated_targets(body)
    ungated = ungated_targets(body)

    for job_id in (fixture.paused, fixture.failed, fixture.stalled):
        assert f"/activity/jobs/{job_id}/cancel" in gated
        assert f"/activity/jobs/{job_id}/cancel" not in ungated
    # Both providers reached from the stalled torrent go through the same
    # dialog-gated endpoint; one of them spends GP.
    assert f"/activity/jobs/{fixture.stalled}/switch-source" in gated

    assert f"/activity/jobs/{fixture.paused}/resume" in ungated
    assert f"/activity/jobs/{fixture.failed}/retry" in ungated


def test_a_gated_row_action_names_its_own_dialog(tmp_path: Path) -> None:
    # `aria-labelledby` points at an id, and a queue of rows each offering
    # 取消 would otherwise aim every one of them at the first dialog's title.
    fixture = Fixture(tmp_path)
    try:
        body = fixture.client.get("/activity").text
    finally:
        fixture.close()

    ids = re.findall(r'<h2 class="ui-dialog-title" id="([^"]+)"', body)
    assert len(ids) == len(set(ids)), "two dialogs share one title id"
    labels = re.findall(r'aria-labelledby="([^"]+)"', body)
    assert labels, "no dialog rendered, so this test proves nothing"
    for label in labels:
        assert label in ids, label
