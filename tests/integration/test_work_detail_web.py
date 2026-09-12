"""One URL for a work at every stage of its life (R6).

`/works/{id}` is the acceptance criterion for this phase: a work opens at the
same address whether it is waiting for review, downloading, or already packaged,
and its timeline can be read back to both the automatic rule that admitted it and
every operator action since. These tests drive the page through all three stages
against a real app.

Jobs are written straight into `download_jobs` in states no worker claims, for
the reason `test_activity_web.py` gives: a PENDING job would race the running
download worker and the row under test would move mid-assertion.
"""

from __future__ import annotations

import asyncio
import itertools
import json
from pathlib import Path
import re

from fastapi.testclient import TestClient

from app.api.status import STAGE_ARCHIVED, STAGE_CANDIDATE, STAGE_DOWNLOAD
from app.candidates.ingestor import CandidateIngestor
from app.config import Settings
from app.db.database import Database
from app.downloads.models import PROVIDER_CONVERSION, PROVIDER_TELEGRAM
from app.main import create_app
from app.review.models import AUTO_OPERATOR
from tests.integration.markup import (
    gated_targets,
    nested_form_lines,
    ungated_targets,
)


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


async def seed_work(database: Database, *, status: str = "PENDING_REVIEW") -> int:
    """One candidate from a real message, so the page has a source to show."""
    await database.initialize()
    await database.configure_telegram_source(
        source_type="CHANNEL",
        chat_id=-100123,
        display_name="Fixture Channel",
        enabled=True,
        allowed_archive_formats=("zip", "rar", "7z", "cbz"),
        max_attachment_size_mb=0,
    )
    await database.save_telegram_updates(
        [
            {
                "update_id": 900,
                "channel_post": {
                    "message_id": 77,
                    "date": 1_700_000_300,
                    "chat": {"id": -100123, "title": "Fixture Channel"},
                    "caption": "Work Detail Fixture",
                    "document": {
                        "file_id": "work-detail-archive",
                        "file_unique_id": "work-detail-archive-uniq",
                        "file_name": "work-detail.zip",
                        "mime_type": "application/zip",
                        "file_size": 4096,
                    },
                },
            }
        ]
    )
    await CandidateIngestor(database).process_pending_updates()
    candidates = await database.list_candidates()
    candidate_id = candidates[0].candidate_id
    if status != candidates[0].status:
        set_status(database, candidate_id, status)
    return candidate_id


async def add_candidate(
    database: Database, *, message_id: int, update_id: int
) -> int:
    """A second candidate in the same channel, for the tests that need two works."""
    await database.save_telegram_updates(
        [
            {
                "update_id": update_id,
                "channel_post": {
                    "message_id": message_id,
                    "date": 1_700_000_400,
                    "chat": {"id": -100123, "title": "Fixture Channel"},
                    "caption": f"Second Fixture {message_id}",
                    "document": {
                        "file_id": f"second-archive-{message_id}",
                        "file_unique_id": f"second-archive-uniq-{message_id}",
                        "file_name": "second.zip",
                        "mime_type": "application/zip",
                        "file_size": 2048,
                    },
                },
            }
        ]
    )
    await CandidateIngestor(database).process_pending_updates()
    candidates = await database.list_candidates()
    return max(item.candidate_id for item in candidates)


def set_status(database: Database, candidate_id: int, status: str) -> None:
    with database._connect() as connection:  # noqa: SLF001
        connection.execute(
            "UPDATE candidates SET status = ? WHERE id = ?", (status, candidate_id)
        )


def insert_job(
    database: Database,
    candidate_id: int,
    *,
    state: str,
    provider: str = PROVIDER_TELEGRAM,
    error_code: str | None = None,
    error_message: str | None = None,
) -> int:
    with database._connect() as connection:  # noqa: SLF001
        cursor = connection.execute(
            "INSERT INTO download_jobs "
            "(candidate_id, idempotency_key, provider, state, error_code, "
            "error_message, details_json) VALUES (?, ?, ?, ?, ?, ?, '{}')",
            (
                candidate_id,
                f"work-detail:{state}:{next(_KEYS)}",
                provider,
                state,
                error_code,
                error_message,
            ),
        )
        return int(cursor.lastrowid)


def insert_artifact(
    database: Database, job_id: int, *, artifact_type: str, path: str
) -> None:
    with database._connect() as connection:  # noqa: SLF001
        connection.execute(
            "INSERT INTO artifacts (job_id, artifact_type, path, size_bytes) "
            "VALUES (?, ?, ?, 4096)",
            (job_id, artifact_type, path),
        )


def record(
    database: Database,
    candidate_id: int,
    action: str,
    operator: str,
    details: dict | None = None,
) -> None:
    asyncio.run(
        database.record_review_action(
            candidate_id, action, operator, details or {}
        )
    )


def timeline_keys(text: str) -> list[str]:
    """The `data-timeline-key` of every node, in document order.

    Read from the attribute rather than from the rendered words because the
    ordering is the thing under test, and the words belong to
    `app/api/status.py`.
    """
    return re.findall(r'data-timeline-key="([^"]+)"', text)


def test_the_same_url_opens_a_work_at_every_stage(tmp_path: Path) -> None:
    """The R6 acceptance criterion, as one test.

    A candidate, a work with a task in flight, and a packaged one all answer at
    `/works/{id}`; only the stage badge and the action bar change.
    """
    settings = make_settings(tmp_path)
    database = Database(settings.data_path / "ehbot.db")
    candidate_id = asyncio.run(seed_work(database))

    with TestClient(create_app(settings)) as client:
        authenticate(client, settings)

        candidate_page = client.get(f"/works/{candidate_id}")
        assert candidate_page.status_code == 200
        assert candidate_page.context["work"]["stage"]["code"] == STAGE_CANDIDATE
        # Review is what the operator can do here, and nothing about packaging.
        assert "通过并下载" in candidate_page.text
        assert "重新打包" not in candidate_page.text

        set_status(database, candidate_id, "PROCESSING")
        job_id = insert_job(database, candidate_id, state="PAUSED")
        download_page = client.get(f"/works/{candidate_id}")
        assert download_page.status_code == 200
        assert download_page.context["work"]["stage"]["code"] == STAGE_DOWNLOAD
        assert "通过并下载" not in download_page.text
        # A paused task is resumed and cancelled on its own timeline node.
        assert f"/activity/jobs/{job_id}/resume" in download_page.text
        assert f"/activity/jobs/{job_id}/cancel" in download_page.text

        set_status(database, candidate_id, "DOWNLOADED")
        insert_artifact(
            database, job_id, artifact_type="ARCHIVE", path="/work/source.zip"
        )
        pack_id = insert_job(
            database,
            candidate_id,
            state="COMPLETED",
            provider=PROVIDER_CONVERSION,
        )
        insert_artifact(
            database, pack_id, artifact_type="CBZ", path="/library/book.cbz"
        )
        archived_page = client.get(f"/works/{candidate_id}")
        assert archived_page.status_code == 200
        assert archived_page.context["work"]["stage"]["code"] == STAGE_ARCHIVED
        # The book is promoted out of the job list: the operator came for it.
        assert archived_page.context["work"]["archive"]["path"] == "/library/book.cbz"
        assert "/library/book.cbz" in archived_page.text
        assert "重新打包" in archived_page.text


def test_the_timeline_reaches_back_to_the_rule_that_admitted_the_work(
    tmp_path: Path,
) -> None:
    """回溯到自动规则命中与操作员动作 -- both, in one list, newest first."""
    settings = make_settings(tmp_path)
    database = Database(settings.data_path / "ehbot.db")
    candidate_id = asyncio.run(seed_work(database, status="NEEDS_REVISION"))

    record(
        database,
        candidate_id,
        "AUTO_APPROVE",
        AUTO_OPERATOR,
        {"rule_name": "汉化组白名单"},
    )
    record(
        database,
        candidate_id,
        "NEEDS_REVISION",
        "admin",
        {"note": "缺少第 3 卷"},
    )

    with TestClient(create_app(settings)) as client:
        authenticate(client, settings)
        page = client.get(f"/works/{candidate_id}")

    assert page.status_code == 200
    # Both actors are named, because「谁决定的」decides whether the operator goes
    # and argues with a rule or with a person.
    assert "自动规则" in page.text
    assert "操作员" in page.text
    # Both reasons survive: the rule that hit, and the note the operator left.
    assert "命中规则「汉化组白名单」" in page.text
    assert "缺少第 3 卷" in page.text
    # Newest first, so the operator reads the latest decision without scrolling.
    nodes = page.context["work"]["timeline"]
    assert [node["action"]["code"] for node in nodes] == [
        "NEEDS_REVISION",
        "AUTO_APPROVE",
    ]


def test_a_failed_task_is_retried_on_the_node_that_reports_it(
    tmp_path: Path,
) -> None:
    settings = make_settings(tmp_path)
    database = Database(settings.data_path / "ehbot.db")
    candidate_id = asyncio.run(seed_work(database, status="FAILED"))
    job_id = insert_job(
        database,
        candidate_id,
        state="FAILED",
        error_code="TELEGRAM_TEMPORARY",
        error_message="连接超时",
    )

    with TestClient(create_app(settings)) as client:
        authenticate(client, settings)
        page = client.get(f"/works/{candidate_id}")

    assert page.status_code == 200
    assert timeline_keys(page.text) == [f"job:{job_id}"]
    assert "连接超时" in page.text
    assert f"/activity/jobs/{job_id}/retry" in page.text


def test_a_job_action_taken_here_comes_back_here(tmp_path: Path) -> None:
    """The queue owns the job routes, but the page says where to return.

    An operator who paused a download from `/works/12` must not be dropped on
    the activity queue, so the form carries a `return_to`.
    """
    settings = make_settings(tmp_path)
    database = Database(settings.data_path / "ehbot.db")
    candidate_id = asyncio.run(seed_work(database, status="PROCESSING"))
    job_id = insert_job(database, candidate_id, state="PAUSED")

    with TestClient(create_app(settings), follow_redirects=False) as client:
        authenticate(client, settings)
        page = client.get(f"/works/{candidate_id}")
        csrf = page.context["csrf_token"]
        assert f'value="/works/{candidate_id}"' in page.text

        response = client.post(
            f"/activity/jobs/{job_id}/resume",
            data={"csrf_token": csrf, "return_to": f"/works/{candidate_id}"},
        )

    assert response.status_code == 303
    assert response.headers["location"] == f"/works/{candidate_id}"


def test_an_off_site_return_target_is_ignored_rather_than_followed(
    tmp_path: Path,
) -> None:
    """A hidden field is an open-redirect surface, so it is validated server-side.

    A crafted form that posts an absolute URL falls back to the queue redirect
    the route had before R6 -- the action still runs, it just cannot be used to
    send anyone off-site with the app's own `Location`.
    """
    settings = make_settings(tmp_path)
    database = Database(settings.data_path / "ehbot.db")
    candidate_id = asyncio.run(seed_work(database, status="PROCESSING"))
    job_id = insert_job(database, candidate_id, state="PAUSED")

    with TestClient(create_app(settings), follow_redirects=False) as client:
        authenticate(client, settings)
        csrf = client.get(f"/works/{candidate_id}").context["csrf_token"]
        response = client.post(
            f"/activity/jobs/{job_id}/resume",
            data={"csrf_token": csrf, "return_to": "https://evil.example/x"},
        )

    assert response.status_code == 303
    assert not response.headers["location"].startswith("https://evil.example")
    assert response.headers["location"].startswith("/activity")


def test_a_refused_action_shows_the_whole_page_again(tmp_path: Path) -> None:
    """A rejection re-renders the work, timeline included, not a bare error.

    Approving a work that is no longer reviewable is the cheapest way to make the
    route refuse, and the point is what comes back: the same page, with the
    reason on it.
    """
    settings = make_settings(tmp_path)
    database = Database(settings.data_path / "ehbot.db")
    candidate_id = asyncio.run(seed_work(database, status="APPROVED"))
    record(database, candidate_id, "APPROVE", "admin", {})

    with TestClient(create_app(settings), follow_redirects=False) as client:
        authenticate(client, settings)
        csrf = client.get(f"/works/{candidate_id}").context["csrf_token"]
        response = client.post(
            f"/candidates/{candidate_id}/approve", data={"csrf_token": csrf}
        )

    assert response.status_code == 400
    assert response.context["work"]["candidate_id"] == candidate_id
    assert response.context["error"]
    # The timeline is still there, which is the difference from a bare error.
    assert timeline_keys(response.text) or "已通过" in response.text


def test_the_source_message_and_its_attachment_are_named(tmp_path: Path) -> None:
    """The attachment kind is resolved server-side, not by a template ternary."""
    settings = make_settings(tmp_path)
    database = Database(settings.data_path / "ehbot.db")
    candidate_id = asyncio.run(seed_work(database))

    with TestClient(create_app(settings)) as client:
        authenticate(client, settings)
        page = client.get(f"/works/{candidate_id}")

    assert page.status_code == 200
    assert "Fixture Channel" in page.text
    assert "消息 #77" in page.text
    assert "压缩包" in page.text
    assert "work-detail.zip" in page.text
    attachments = page.context["work"]["messages"][0]["attachments"]
    assert attachments[0]["kind"]["code"] == "archive"


def test_the_json_endpoint_and_the_page_read_the_same_snapshot(
    tmp_path: Path,
) -> None:
    """One snapshot per domain, so the two surfaces cannot disagree."""
    settings = make_settings(tmp_path)
    database = Database(settings.data_path / "ehbot.db")
    candidate_id = asyncio.run(seed_work(database, status="PROCESSING"))
    insert_job(database, candidate_id, state="PAUSED")

    with TestClient(create_app(settings)) as client:
        authenticate(client, settings)
        page = client.get(f"/works/{candidate_id}")
        api = client.get(f"/api/v1/works/{candidate_id}")

    assert api.status_code == 200
    assert json.loads(api.text) == page.context["work"]


def test_a_work_that_does_not_exist_is_a_404_at_both_surfaces(
    tmp_path: Path,
) -> None:
    settings = make_settings(tmp_path)
    Database(settings.data_path / "ehbot.db")

    with TestClient(create_app(settings)) as client:
        authenticate(client, settings)
        assert client.get("/works/9999").status_code == 404
        assert client.get("/api/v1/works/9999").status_code == 404


# ------------------------------------------------ the markup a click needs


def attach_gallery(database: Database, candidate_id: int) -> None:
    """Give the work an ExHentai gallery, which is what offers the GP source.

    `_source_actions` only offers Archive Download when the candidate has a
    gallery id, so without this the button renders disabled and the test would
    pass on a control nobody can press.
    """
    with database._connect() as connection:  # noqa: SLF001
        connection.execute(
            "UPDATE candidates SET ex_gid = ?, ex_gallery_token = ? WHERE id = ?",
            (987654, "appletoken99", candidate_id),
        )


def test_the_actions_that_spend_or_destroy_ask_first(tmp_path: Path) -> None:
    """EHBot.md 8.8, on the page that offers every action at once.

    Archive Download spends GP and rejecting sends a work back out of the queue;
    both get a dialog. Taking a torrent or a preview page costs nothing and is
    left as one click, because a confirmation on every button is a confirmation
    on none.
    """
    settings = make_settings(tmp_path)
    database = Database(settings.data_path / "ehbot.db")
    candidate_id = asyncio.run(seed_work(database))
    attach_gallery(database, candidate_id)

    with TestClient(create_app(settings)) as client:
        authenticate(client, settings)
        page = client.get(f"/works/{candidate_id}")
        assert page.status_code == 200
        body = page.text

        set_status(database, candidate_id, "PROCESSING")
        job_id = insert_job(database, candidate_id, state="PAUSED")
        download_body = client.get(f"/works/{candidate_id}").text

    base = f"/candidates/{candidate_id}"
    gated, ungated = gated_targets(body), ungated_targets(body)
    # Offered at all -- otherwise the assertion below holds vacuously.
    assert any(
        source["provider"]["code"] == "EXHENTAI" and source["available"]
        for source in page.context["work"]["actions"]["sources"]
    )
    assert f"{base}/exhentai-archive" in gated
    assert f"{base}/exhentai-archive" not in ungated
    assert f"{base}/reject" in gated
    assert f"{base}/reject" not in ungated

    assert f"{base}/torrent" in ungated
    assert f"{base}/telegraph" in ungated
    assert f"{base}/approve" in ungated

    # A task on the timeline is cancelled here as well, and cancelling is as
    # destructive here as it is on the queue.
    assert f"/activity/jobs/{job_id}/cancel" in gated_targets(download_body)
    assert f"/activity/jobs/{job_id}/resume" in ungated_targets(download_body)


def test_no_action_form_is_swallowed_by_another(tmp_path: Path) -> None:
    # HTML drops a `<form>` nested in another, and this page stacks a review
    # bar, a source bar and one form per timeline node.
    settings = make_settings(tmp_path)
    database = Database(settings.data_path / "ehbot.db")
    candidate_id = asyncio.run(seed_work(database))
    attach_gallery(database, candidate_id)

    with TestClient(create_app(settings)) as client:
        authenticate(client, settings)
        pages = [client.get(f"/works/{candidate_id}").text]
        set_status(database, candidate_id, "PROCESSING")
        insert_job(database, candidate_id, state="PAUSED")
        pages.append(client.get(f"/works/{candidate_id}").text)

    for page in pages:
        assert nested_form_lines(page) == []


def test_a_gated_action_returns_to_this_page(tmp_path: Path) -> None:
    """The dialog carries the same `return_to` the plain button did.

    A confirmation that loses the return target sends the operator to the queue
    instead of back to the work they were reading.
    """
    settings = make_settings(tmp_path)
    database = Database(settings.data_path / "ehbot.db")
    candidate_id = asyncio.run(seed_work(database, status="PROCESSING"))
    job_id = insert_job(database, candidate_id, state="PAUSED")

    with TestClient(create_app(settings)) as client:
        authenticate(client, settings)
        body = client.get(f"/works/{candidate_id}").text

    dialog = body.split(f'action="/activity/jobs/{job_id}/cancel"')[1]
    dialog = dialog.split("</form>")[0]
    assert 'name="return_to"' in dialog
    assert f'value="/works/{candidate_id}"' in dialog


def test_the_return_button_goes_back_to_the_page_it_was_opened_from(
    tmp_path: Path,
) -> None:
    """The reported inconvenience: 返回 always claimed 「候选列表」.

    Opening a work from 已下载 left the sidebar as the only way back, which loses
    the tab, filter, sort and page -- all of which live in the query string. The
    button now names and reaches the list the operator actually came from.
    """
    settings = make_settings(tmp_path)
    database = Database(settings.data_path / "ehbot.db")
    candidate_id = asyncio.run(seed_work(database, status="DOWNLOADED"))

    with TestClient(create_app(settings)) as client:
        authenticate(client, settings)
        from_downloaded = client.get(
            f"/works/{candidate_id}",
            headers={"referer": "http://testserver/downloaded?tab=packed&page=2"},
        )
        from_candidates = client.get(
            f"/works/{candidate_id}",
            headers={"referer": "http://testserver/candidates?tab=pending"},
        )

    # A session that has never opened this work -- a bookmark, or a cold start --
    # behaves exactly like the hardcoded link that used to be there. Its own
    # deployment, not just its own client: within one session the origin is
    # deliberately remembered (the next test is about that), and the bootstrap
    # password a fresh `create_app` writes is single-use, so a second app over the
    # same data path cannot be logged into.
    cold_settings = make_settings(tmp_path / "cold")
    cold_database = Database(cold_settings.data_path / "ehbot.db")
    cold_id = asyncio.run(seed_work(cold_database, status="DOWNLOADED"))
    with TestClient(create_app(cold_settings)) as client:
        authenticate(client, cold_settings)
        cold = client.get(f"/works/{cold_id}")

    assert from_downloaded.context["origin"] == {
        "href": "/downloaded?tab=packed&page=2",
        "label": "返回已下载",
    }
    assert "返回已下载" in from_downloaded.text
    assert 'href="/downloaded?tab=packed&amp;page=2"' in from_downloaded.text

    assert from_candidates.context["origin"]["label"] == "返回候选列表"
    assert from_candidates.context["origin"]["href"] == "/candidates?tab=pending"

    assert cold.context["origin"] == {
        "href": "/candidates",
        "label": "返回候选列表",
    }


def test_the_return_button_cannot_be_pointed_off_site(tmp_path: Path) -> None:
    """A referrer and a query parameter are both attacker-supplied.

    Neither may put an absolute URL in the page's own 返回 link, or the app would
    be lending its origin to somebody else's destination.
    """
    settings = make_settings(tmp_path)
    database = Database(settings.data_path / "ehbot.db")
    candidate_id = asyncio.run(seed_work(database))

    with TestClient(create_app(settings)) as client:
        authenticate(client, settings)
        by_referer = client.get(
            f"/works/{candidate_id}",
            headers={"referer": "https://evil.example/downloaded"},
        )
        by_query = client.get(
            f"/works/{candidate_id}?return_to=https://evil.example/x"
        )

    for page in (by_referer, by_query):
        assert page.status_code == 200
        assert page.context["origin"]["href"] == "/candidates"
        assert "evil.example" not in page.text


def test_an_action_does_not_demote_the_return_button(tmp_path: Path) -> None:
    """The reported inconvenience: 返回已下载 became 返回候选列表 after any action.

    Every action on this page is a POST that answers 303 back to `/works/{id}`,
    and the referrer the browser sends on that redirect is the detail page
    itself -- which `resolve_origin` excludes, correctly, because a 返回 pointing
    at the page it is drawn on goes nowhere. So the origin fell through to the
    default and an operator working from 已下载 was thrown into 候选列表 the
    moment they pressed 拉取元数据.

    The work's origin is remembered for the session, so it survives the round
    trip that has nothing to say about it.
    """
    settings = make_settings(tmp_path)
    database = Database(settings.data_path / "ehbot.db")
    candidate_id = asyncio.run(seed_work(database, status="DOWNLOADED"))

    with TestClient(create_app(settings)) as client:
        authenticate(client, settings)
        opened = client.get(
            f"/works/{candidate_id}",
            headers={"referer": "http://testserver/downloaded?tab=all&sort=title"},
        )
        assert opened.context["origin"]["label"] == "返回已下载"

        # The redirect a POST lands on: no query string, and a referrer that names
        # this same page.
        after_action = client.get(
            f"/works/{candidate_id}",
            headers={"referer": f"http://testserver/works/{candidate_id}"},
        )

    assert after_action.context["origin"] == {
        "href": "/downloaded?tab=all&sort=title",
        "label": "返回已下载",
    }


def test_a_remembered_origin_belongs_to_the_work_it_was_recorded_for(
    tmp_path: Path,
) -> None:
    """Opening a second work must not inherit the first one's list.

    One entry keyed by candidate, rather than a map that would grow with every
    book an operator looked at -- so the guard is that the key is checked, not
    that the value happens to be right.
    """
    settings = make_settings(tmp_path)
    database = Database(settings.data_path / "ehbot.db")
    first = asyncio.run(seed_work(database, status="DOWNLOADED"))
    second = asyncio.run(add_candidate(database, message_id=78, update_id=901))

    with TestClient(create_app(settings)) as client:
        authenticate(client, settings)
        client.get(
            f"/works/{first}",
            headers={"referer": "http://testserver/downloaded?tab=packed"},
        )
        other = client.get(f"/works/{second}")

    assert other.context["origin"] == {
        "href": "/candidates",
        "label": "返回候选列表",
    }


def test_a_remembered_origin_survives_an_unusable_return_to(
    tmp_path: Path,
) -> None:
    """A `return_to` naming no known page does not erase what was remembered.

    `_label_for` would call such a path 返回候选列表 while sending the operator to
    it, which is a button that lies about where it goes. It is refused, and the
    origin recorded when the work was opened is what the page keeps offering.
    """
    settings = make_settings(tmp_path)
    database = Database(settings.data_path / "ehbot.db")
    candidate_id = asyncio.run(seed_work(database, status="DOWNLOADED"))

    with TestClient(create_app(settings)) as client:
        authenticate(client, settings)
        client.get(
            f"/works/{candidate_id}",
            headers={"referer": "http://testserver/downloaded?tab=all"},
        )
        page = client.get(f"/works/{candidate_id}?return_to=/retired-page")

    assert page.context["origin"] == {
        "href": "/downloaded?tab=all",
        "label": "返回已下载",
    }
    assert "/retired-page" not in page.text


def test_adjacent_works_offer_previous_and_next_navigation(
    tmp_path: Path,
) -> None:
    """The detail page links the neighbouring works, in id order.

    The 候选 list is newest-first by candidate id, so id order is also the
    order an operator walks the list in: 上一个 is the id immediately below,
    下一个 immediately above. Link sets vanish at the ends rather than link
    somewhere dead.
    """
    settings = make_settings(tmp_path)
    database = Database(settings.data_path / "ehbot.db")
    first = asyncio.run(seed_work(database, status="PENDING_REVIEW"))
    second = asyncio.run(
        add_candidate(database, message_id=1001, update_id=1101)
    )

    with TestClient(create_app(settings)) as client:
        authenticate(client, settings)

        first_page = client.get(f"/works/{first}")
        assert first_page.status_code == 200
        # Newest first: 上一个 is the only neighbour of the oldest work.
        assert first_page.context["prev_work"] is None
        assert first_page.context["next_work"] == second
        assert "上一个作品" not in first_page.text
        assert f'/works/{second}' in first_page.text

        second_page = client.get(f"/works/{second}")
        assert second_page.status_code == 200
        assert second_page.context["prev_work"] == first
        assert second_page.context["next_work"] is None
        assert f'/works/{first}' in second_page.text
        assert "下一个作品" not in second_page.text


def test_manual_eh_ref_repoints_and_clears_stale_torrent(
    tmp_path: Path,
) -> None:
    """A URL or bare gid re-points the work and drops the old gallery's facts.

    Setting a new gallery makes the previously fetched torrent meaningless, so
    the write clears `torrent_count`/`torrent_hash`; the metadata scrape pulls
    whatever the *new* gallery actually has. A link is parsed for both gid and
    token; a bare gid is accepted too because the operator may only know the
    number.
    """
    settings = make_settings(tmp_path)
    database = Database(settings.data_path / "ehbot.db")
    candidate_id = asyncio.run(seed_work(database, status="DOWNLOADED"))
    with database._connect() as connection:  # noqa: SLF001
        connection.execute(
            "UPDATE candidates SET ex_gid = 111, ex_gallery_token = 'old', "
            "torrent_count = 3, torrent_hash = 'abc' WHERE id = ?",
            (candidate_id,),
        )

    with TestClient(create_app(settings), follow_redirects=False) as client:
        authenticate(client, settings)
        csrf = client.get(f"/works/{candidate_id}").context["csrf_token"]
        response = client.post(
            f"/works/{candidate_id}/eh-ref",
            data={
                "csrf_token": csrf,
                "ex_gid": "https://exhentai.org/g/12345/deadbeef",
            },
        )

    assert response.status_code == 303
    assert f"/works/{candidate_id}" in response.headers["location"]
    with database._connect() as connection:  # noqa: SLF001
        row = connection.execute(
            "SELECT ex_gid, ex_gallery_token, torrent_count, torrent_hash "
            "FROM candidates WHERE id = ?",
            (candidate_id,),
        ).fetchone()
    assert row[0] == 12345
    assert row[1] == "deadbeef"
    assert row[2] is None
    assert row[3] is None

    # A bare gid is accepted by itself.
    csrf = client.get(f"/works/{candidate_id}").context["csrf_token"]
    bare = client.post(
        f"/works/{candidate_id}/eh-ref",
        data={"csrf_token": csrf, "ex_gid": "99999"},
    )
    assert bare.status_code == 303
    with database._connect() as connection:  # noqa: SLF001
        row = connection.execute(
            "SELECT ex_gid, ex_gallery_token FROM candidates WHERE id = ?",
            (candidate_id,),
        ).fetchone()
    assert row[0] == 99999
    assert row[1] is None


def test_manual_eh_ref_refuses_a_garbage_gallery_id(tmp_path: Path) -> None:
    """Neither URL nor number -> the page comes back with the reason on it."""
    settings = make_settings(tmp_path)
    database = Database(settings.data_path / "ehbot.db")
    candidate_id = asyncio.run(seed_work(database, status="PENDING_REVIEW"))

    with TestClient(create_app(settings)) as client:
        authenticate(client, settings)
        csrf = client.get(f"/works/{candidate_id}").context["csrf_token"]
        response = client.post(
            f"/works/{candidate_id}/eh-ref",
            data={"csrf_token": csrf, "ex_gid": "not-a-gallery"},
        )

    assert response.status_code == 400
    assert response.context["work"]["candidate_id"] == candidate_id
    assert "无法识别画廊编号" in response.text
