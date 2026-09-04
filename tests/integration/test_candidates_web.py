import asyncio
from html import unescape
from pathlib import Path
import re
import sqlite3

from fastapi.testclient import TestClient

from app.candidates.ingestor import CandidateIngestor
from app.config import Settings
from app.db.database import Database
from app.main import create_app
from tests.integration.markup import (
    gated_targets,
    nested_form_lines,
    ungated_targets,
)


def make_settings(root: Path) -> Settings:
    return Settings(
        data_path=root / "data",
        library_path=root / "library",
        work_path=root / "work",
        app_secret_key="test-secret-key-with-at-least-32-characters",
        tag_translation_enabled=False,
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


async def configure_source(database: Database) -> None:
    await database.initialize()
    await database.configure_telegram_source(
        source_type="CHANNEL",
        chat_id=-100123,
        display_name="Fixture Channel",
        enabled=True,
        allowed_archive_formats=("zip", "rar", "7z", "cbz"),
        max_attachment_size_mb=0,
    )


async def seed_candidate(
    database: Database,
    *,
    update_id: int = 300,
    message_id: int = 60,
    title: str = "Queue Fixture Comic",
) -> None:
    """One candidate whose only attachment is a photo.

    A photo is not a downloadable archive, which is what makes this fixture the
    right one for the read-only assertions: the candidate stays in 待审核 no
    matter what the review rules do.
    """
    await configure_source(database)
    await database.save_telegram_updates(
        [
            {
                "update_id": update_id,
                "channel_post": {
                    "message_id": message_id,
                    "date": 1_700_000_100,
                    "chat": {"id": -100123, "title": "Fixture Channel"},
                    "caption": title,
                    "photo": [
                        {
                            "file_id": f"queue-photo-{update_id}",
                            "file_unique_id": f"queue-photo-{update_id}-uniq",
                            "width": 800,
                            "height": 1200,
                        }
                    ],
                },
            }
        ]
    )
    await CandidateIngestor(database).process_pending_updates()


async def seed_archive_candidate(
    database: Database,
    *,
    update_id: int,
    message_id: int,
    title: str,
) -> int:
    """A candidate with a real archive attachment, so it can be approved."""
    await configure_source(database)
    await database.save_telegram_updates(
        [
            {
                "update_id": update_id,
                "channel_post": {
                    "message_id": message_id,
                    "date": 1_700_000_200,
                    "chat": {"id": -100123, "title": "Fixture Channel"},
                    "caption": title,
                    "document": {
                        "file_id": f"archive-{update_id}",
                        "file_unique_id": f"archive-{update_id}-uniq",
                        "file_name": f"archive-{update_id}.zip",
                        "mime_type": "application/zip",
                        "file_size": 4096,
                    },
                },
            }
        ]
    )
    await CandidateIngestor(database).process_pending_updates()
    candidates = await database.list_candidates()
    match = [item for item in candidates if item.title == title]
    assert match, f"expected a candidate titled {title}"
    return match[0].candidate_id


def set_status(database: Database, candidate_id: int, status: str) -> None:
    with sqlite3.connect(database.path) as connection:
        connection.execute(
            "UPDATE candidates SET status = ? WHERE id = ?",
            (status, candidate_id),
        )


def set_metadata(
    database: Database, candidate_id: int, field_name: str, field_value: str
) -> None:
    """Write a scraped metadata value, the way an enrichment pass would.

    `value_source` is EXHENTAI rather than OPERATOR so the row is indistinguishable
    from a real scrape: the facet sidebar reads whatever is in the table, and a
    fixture that only ever wrote manual values would not exercise that.
    """
    with sqlite3.connect(database.path) as connection:
        connection.execute(
            "INSERT INTO metadata_values (candidate_id, field_name, field_value,"
            " value_source, confidence, is_manual) VALUES (?,?,?,'EXHENTAI',0.9,0)",
            (candidate_id, field_name, field_value),
        )


def href_with(text: str, marker: str) -> str:
    """The `href` of the one element carrying `marker`, as a browser reads it.

    Written as a search rather than an equality assertion because the query
    string's parameter order is Starlette's business, not this test's -- what
    matters is which parameters survive. `unescape` is what makes the result
    usable as a URL: the document escapes `&` as `&amp;`, and a client that
    replayed the raw attribute would request `amp;page=2`.
    """
    match = re.search(r'href="([^"]*)"[^>]*' + marker, text) or re.search(
        marker + r'[^>]*href="([^"]*)"', text
    )
    assert match, f"no element matching {marker}"
    return unescape(match.group(1))


def test_authenticated_user_can_view_pending_candidate_queue(
    tmp_path: Path,
) -> None:
    settings = make_settings(tmp_path)
    database = Database(settings.data_path / "ehbot.db")
    asyncio.run(seed_candidate(database))

    with TestClient(create_app(settings)) as client:
        authenticate(client, settings)
        response = client.get("/candidates")

    assert response.status_code == 200
    # The tab's name comes from `candidate_tab_view`, so the heading is the tab
    # name and nothing else -- the old page's「待审核队列」was a second label.
    assert "待审核" in response.text
    assert "Queue Fixture Comic" in response.text
    # The card links wherever the DTO says, which R5 leaves as the work path.
    assert 'href="/works/1"' in response.text
    # Rewritten on the design system, so the legacy light lock is gone.
    assert 'data-legacy="true"' not in response.text
    # Grid by default; the tab strip carries all six tabs with their counts.
    assert "ui-cover-grid" in response.text
    for href in (
        "/candidates/all",
        "/candidates/needs-info",
        "/candidates/approved",
        "/candidates/rejected",
        "/candidates/failed",
    ):
        assert f'href="{href}"' in response.text


def test_each_tab_shows_only_its_own_states(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    database = Database(settings.data_path / "ehbot.db")
    pending_id = asyncio.run(
        seed_archive_candidate(
            database, update_id=310, message_id=70, title="Pending Work"
        )
    )
    needs_info_id = asyncio.run(
        seed_archive_candidate(
            database, update_id=311, message_id=71, title="Needs Info Work"
        )
    )
    approved_id = asyncio.run(
        seed_archive_candidate(
            database, update_id=312, message_id=72, title="Approved Work"
        )
    )
    rejected_id = asyncio.run(
        seed_archive_candidate(
            database, update_id=313, message_id=73, title="Rejected Work"
        )
    )
    failed_id = asyncio.run(
        seed_archive_candidate(
            database, update_id=314, message_id=74, title="Failed Work"
        )
    )
    set_status(database, needs_info_id, "NEEDS_INFO")
    set_status(database, approved_id, "PROCESSING")
    set_status(database, rejected_id, "REJECTED")
    set_status(database, failed_id, "FAILED")
    assert pending_id

    expected = {
        "/candidates": "Pending Work",
        "/candidates/needs-info": "Needs Info Work",
        # 已通过 covers APPROVED, PROCESSING and DOWNLOADED: they are one stage
        # from the operator's side, which is why 处理中 stopped being its own tab.
        "/candidates/approved": "Approved Work",
        "/candidates/rejected": "Rejected Work",
        "/candidates/failed": "Failed Work",
    }
    with TestClient(create_app(settings)) as client:
        authenticate(client, settings)
        for path, title in expected.items():
            response = client.get(path)
            assert response.status_code == 200, path
            assert title in response.text, path
            for other in set(expected.values()) - {title}:
                assert other not in response.text, (path, other)

        everything = client.get("/candidates/all")
        assert everything.status_code == 200
        for title in expected.values():
            assert title in everything.text


def test_retired_processing_path_redirects_to_approved(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    database = Database(settings.data_path / "ehbot.db")
    asyncio.run(seed_candidate(database))

    with TestClient(create_app(settings), follow_redirects=False) as client:
        authenticate(client, settings)
        response = client.get("/candidates/processing")

    # 307 rather than 301: a browser that cached a permanent redirect would make
    # the path impossible to reclaim.
    assert response.status_code == 307
    assert response.headers["location"] == "/candidates/approved"


def test_the_link_the_api_hands_out_leads_somewhere(tmp_path: Path) -> None:
    """`candidate_summary` links `/works/{id}`, and R6 made it the real page.

    The card and every JSON client already follow that link. It renders the
    unified detail page now instead of redirecting, and the retired
    `/candidates/{id}` is what redirects -- 307, so a cached response cannot make
    the old path unreclaimable.
    """
    settings = make_settings(tmp_path)
    database = Database(settings.data_path / "ehbot.db")
    asyncio.run(seed_candidate(database))

    with TestClient(create_app(settings), follow_redirects=False) as client:
        authenticate(client, settings)
        response = client.get("/works/1")
        retired = client.get("/candidates/1")

    assert response.status_code == 200
    assert retired.status_code == 307
    assert retired.headers["location"] == "/works/1"


def test_search_narrows_the_list_and_says_so_when_nothing_matches(
    tmp_path: Path,
) -> None:
    settings = make_settings(tmp_path)
    database = Database(settings.data_path / "ehbot.db")
    asyncio.run(
        seed_archive_candidate(
            database, update_id=320, message_id=80, title="Alpha Volume"
        )
    )
    asyncio.run(
        seed_archive_candidate(
            database, update_id=321, message_id=81, title="Beta Volume"
        )
    )

    with TestClient(create_app(settings)) as client:
        authenticate(client, settings)
        hit = client.get("/candidates", params={"search": "Alpha"})
        miss = client.get("/candidates", params={"search": "Gamma"})

    assert "Alpha Volume" in hit.text
    assert "Beta Volume" not in hit.text
    # A filtered empty list must not read as an empty queue.
    assert "没有符合条件的候选" in miss.text
    assert "暂无待审核候选" not in miss.text


def test_view_switch_renders_a_table_and_keeps_the_search(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    database = Database(settings.data_path / "ehbot.db")
    asyncio.run(seed_candidate(database))

    with TestClient(create_app(settings)) as client:
        authenticate(client, settings)
        listed = client.get(
            "/candidates", params={"view": "list", "search": "Fixture"}
        )

    assert "ui-table" in listed.text
    assert "ui-cover-grid" not in listed.text
    # Switching back to the grid must not throw the search away: the view is one
    # parameter of the URL, not a different page. Matched on the link's own words
    # rather than on a state attribute: the inactive link carries none, since
    # `aria-current` is written only on the view actually in effect.
    assert "search=Fixture" in href_with(listed.text, ">封面<")
    # The link that *is* in effect says so, and says it in the one way an anchor
    # can -- `aria-pressed` is defined on a button, so on these two links it
    # announced nothing. (The theme and density toggles in the shell are real
    # buttons and keep `aria-pressed`, which is why this checks the anchors.)
    assert 'aria-current="true"' in listed.text
    assert not re.search(r"<a\b[^>]*aria-pressed", listed.text)


def test_pagination_links_carry_the_current_filter(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    database = Database(settings.data_path / "ehbot.db")
    asyncio.run(
        seed_archive_candidate(
            database, update_id=330, message_id=90, title="Paged Volume One"
        )
    )
    asyncio.run(
        seed_archive_candidate(
            database, update_id=331, message_id=91, title="Paged Volume Two"
        )
    )

    with TestClient(create_app(settings)) as client:
        authenticate(client, settings)
        first = client.get(
            "/candidates", params={"page_size": 1, "search": "Paged"}
        )
        assert first.status_code == 200
        next_href = href_with(first.text, 'rel="next"')
        assert "page=2" in next_href
        assert "search=Paged" in next_href
        assert "page_size=1" in next_href
        second = client.get(next_href)

    assert second.status_code == 200
    assert "共 2 条，第 2 / 2 页" in second.text


def test_the_filter_sidebar_offers_and_applies_the_values_it_lists(
    tmp_path: Path,
) -> None:
    """The facet groups, and the two ways selections combine.

    Tags are comma-joined in one row, so picking two means 「同时带这两个」 and
    each selection narrows. 作者 holds one value per candidate, so picking two
    means 「任选其一」 -- requiring both would always return nothing, which reads
    as a broken filter rather than a narrow one.
    """
    settings = make_settings(tmp_path)
    database = Database(settings.data_path / "ehbot.db")
    first = asyncio.run(
        seed_archive_candidate(
            database, update_id=360, message_id=120, title="Facet One"
        )
    )
    second = asyncio.run(
        seed_archive_candidate(
            database, update_id=361, message_id=121, title="Facet Two"
        )
    )
    set_metadata(database, first, "Artist", "Alice")
    set_metadata(database, first, "Tags", "巨乳, 汉语")
    set_metadata(database, second, "Artist", "Bob")
    set_metadata(database, second, "Tags", "巨乳")

    with TestClient(create_app(settings)) as client:
        authenticate(client, settings)
        listing = client.get("/candidates")
        by_artist = client.get("/candidates", params={"artist": "Alice"})
        either_artist = client.get(
            "/candidates", params=[("artist", "Alice"), ("artist", "Bob")]
        )
        both_tags = client.get(
            "/candidates", params=[("tags", "巨乳"), ("tags", "汉语")]
        )
        unknown_facet = client.get("/candidates", params={"nonsense": "x"})

    # The sidebar offers what the tab actually contains, with its count.
    assert 'name="artist" value="Alice"' in listing.text
    assert 'name="tags" value="巨乳"' in listing.text

    assert "Facet One" in by_artist.text and "Facet Two" not in by_artist.text
    assert "Facet One" in either_artist.text and "Facet Two" in either_artist.text
    assert "Facet One" in both_tags.text and "Facet Two" not in both_tags.text
    # A name that is not a facet is not a filter: it must not narrow anything,
    # and it must not reach the SQL either.
    assert unknown_facet.status_code == 200
    assert "Facet One" in unknown_facet.text and "Facet Two" in unknown_facet.text


def test_too_many_values_in_one_facet_is_refused_with_a_message(
    tmp_path: Path,
) -> None:
    """A hand-edited or looping URL must not turn a filter into a table scan."""
    settings = make_settings(tmp_path)
    database = Database(settings.data_path / "ehbot.db")
    asyncio.run(seed_candidate(database))

    with TestClient(create_app(settings)) as client:
        authenticate(client, settings)
        response = client.get(
            "/candidates", params=[("tags", f"tag-{index}") for index in range(9)]
        )

    # The page still renders -- an operator who followed a bad link needs the
    # list back, not a stack trace -- and says why the filter was dropped.
    assert response.status_code == 200
    assert "最多选" in response.text


def test_the_page_ships_the_scaffolding_its_script_upgrades(
    tmp_path: Path,
) -> None:
    """What `candidates.js` needs is in the HTML, and starts inert.

    The keyboard cursor moves between `data-candidate-id` elements and the
    drawer clones a template row -- both are contracts with the markup, so a
    renamed hook would silently cost the operator the keyboard flow and the
    metadata editor. The controls that cannot work without the script ship
    `hidden`: a dead button is worse than no button.
    """
    settings = make_settings(tmp_path)
    database = Database(settings.data_path / "ehbot.db")
    asyncio.run(seed_candidate(database))

    with TestClient(create_app(settings)) as client:
        authenticate(client, settings)
        body = client.get("/candidates").text

    assert 'data-candidate-id="1"' in body
    assert 'name="candidate_ids" value="1"' in body
    assert "static/candidates.js" in body
    # The drawer's row template, with the two slots R5 adds: where a value came
    # from, and whether the next scrape may overwrite it.
    assert "data-metadata-row" in body
    assert 'data-field="source"' in body
    assert 'data-field="lock"' in body
    # Revealed by the script, not by the server.
    assert 'data-metadata-open="1"' in body
    for marker in ("data-metadata-open", "data-select-tools"):
        assert re.search(marker + r'[^>]*hidden', body) or re.search(
            r'hidden[^>]*' + marker, body
        ), f"{marker} must ship hidden"


def test_quick_approve_returns_to_the_tab_it_was_fired_from(
    tmp_path: Path,
) -> None:
    settings = make_settings(tmp_path)
    database = Database(settings.data_path / "ehbot.db")
    candidate_id = asyncio.run(
        seed_archive_candidate(
            database, update_id=340, message_id=100, title="Inline Approve"
        )
    )
    set_status(database, candidate_id, "NEEDS_INFO")

    with TestClient(create_app(settings), follow_redirects=False) as client:
        authenticate(client, settings)
        page = client.get("/candidates/needs-info")
        response = client.post(
            f"/candidates/{candidate_id}/approve",
            data={
                "csrf_token": page.context["csrf_token"],
                # What the list sends and the detail page does not: its presence
                # is the whole difference between the two callers.
                "tab": "needs_info",
            },
        )

    assert response.status_code == 303
    assert response.headers["location"] == "/candidates/needs-info"
    candidate = asyncio.run(database.get_candidate(candidate_id))
    assert candidate is not None
    assert candidate.status == "APPROVED"


def test_quick_approve_without_a_tab_still_lands_on_the_detail_page(
    tmp_path: Path,
) -> None:
    settings = make_settings(tmp_path)
    database = Database(settings.data_path / "ehbot.db")
    candidate_id = asyncio.run(
        seed_archive_candidate(
            database, update_id=341, message_id=101, title="Detail Approve"
        )
    )

    with TestClient(create_app(settings), follow_redirects=False) as client:
        authenticate(client, settings)
        detail = client.get(f"/works/{candidate_id}")
        response = client.post(
            f"/candidates/{candidate_id}/approve",
            data={"csrf_token": detail.context["csrf_token"]},
        )

    assert response.status_code == 303
    assert response.headers["location"] == f"/works/{candidate_id}"


def test_work_detail_shows_source_message_and_attachment(
    tmp_path: Path,
) -> None:
    settings = make_settings(tmp_path)
    database = Database(settings.data_path / "ehbot.db")
    asyncio.run(seed_candidate(database))

    with TestClient(create_app(settings)) as client:
        authenticate(client, settings)
        response = client.get("/works/1")

    assert response.status_code == 200
    assert "Queue Fixture Comic" in response.text
    assert "Fixture Channel" in response.text
    assert "消息 #60" in response.text
    assert "图片预览" in response.text


def test_dashboard_uses_persisted_candidate_counts(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    database = Database(settings.data_path / "ehbot.db")
    asyncio.run(seed_candidate(database))

    with TestClient(create_app(settings)) as client:
        authenticate(client, settings)
        response = client.get("/")

    assert response.status_code == 200
    # Counted by tab, and named by the tab vocabulary, so these four metrics
    # cannot disagree with the tab strip they link to. Matched as a pair rather
    # than as literal adjacent markup: the assertion is that this number belongs
    # to this label, and reindenting the template must not be able to break it.
    metrics = dict(
        (label, int(count))
        for count, label in re.findall(
            r"<strong>(\d+)</strong>\s*<span>([^<]+)</span>", response.text
        )
    )
    assert metrics["待审核"] == 1
    assert metrics["待补充"] == 0
    assert metrics["已通过"] == 0
    assert "/candidates/approved" in response.text
    assert "/candidates/processing" not in response.text


def test_pending_queue_excludes_candidates_in_other_states(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    database = Database(settings.data_path / "ehbot.db")
    asyncio.run(seed_candidate(database))
    with sqlite3.connect(database.path) as connection:
        connection.execute("UPDATE candidates SET status = 'NEEDS_INFO'")

    with TestClient(create_app(settings)) as client:
        authenticate(client, settings)
        response = client.get("/candidates")

    assert response.status_code == 200
    assert "Queue Fixture Comic" not in response.text
    assert "暂无待审核候选" in response.text


# ------------------------------------------------ the markup a click needs


def test_the_batch_forms_are_not_nested_in_one_another(tmp_path: Path) -> None:
    # Both views put the whole list inside one batch form, and HTML drops a
    # `<form>` nested in another rather than reporting it.
    settings = make_settings(tmp_path)
    database = Database(settings.data_path / "ehbot.db")
    asyncio.run(seed_candidate(database))

    with TestClient(create_app(settings)) as client:
        authenticate(client, settings)
        pages = [
            client.get("/candidates").text,
            client.get("/candidates?view=table").text,
        ]

    for page in pages:
        assert nested_form_lines(page) == []


def test_a_batch_rejection_asks_before_it_runs(tmp_path: Path) -> None:
    """EHBot.md 8.8, for the action that can move a whole page of works.

    The dialog is teleported to `<body>`, so its button is outside the batch
    form and HTML's `form` attribute is what reconnects it. Without that the
    click submits nothing, which looks exactly like a rejection that worked.
    """
    settings = make_settings(tmp_path)
    database = Database(settings.data_path / "ehbot.db")
    asyncio.run(seed_candidate(database))

    with TestClient(create_app(settings)) as client:
        authenticate(client, settings)
        body = client.get("/candidates").text

    assert "candidate-batch" in gated_targets(body)
    assert 'id="candidate-batch"' in body
    # The gated button names the action itself, because the batch form serves
    # several of them and the endpoint reads `action` to tell them apart.
    # Sliced rather than matched with a regex: the button carries an Alpine
    # `x-init` whose arrow function contains a `>`, so no attribute pattern
    # bounded by `>` can reach the end of the tag.
    closing = body.index("确认驳回</button>")
    reject = body[body.rindex("<button", 0, closing) : closing]
    assert 'form="candidate-batch"' in reject
    assert 'name="action"' in reject
    assert 'value="reject"' in reject

    # Approving is not gated: it is the action the queue exists for, and it is
    # reversible from the 已通过 tab.
    assert "/candidates/1/approve" in ungated_targets(body)
