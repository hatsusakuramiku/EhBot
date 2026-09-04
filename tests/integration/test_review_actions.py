import asyncio
import json
from pathlib import Path

import httpx
from fastapi.testclient import TestClient

from app.candidates.ingestor import CandidateIngestor
from app.config import Settings
from app.db.database import Database
from app.downloads.service import DownloadService
from app.main import create_app
from app.review.service import ReviewError, ReviewService


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


async def seed_candidate(
    database: Database,
    *,
    update_id: int = 900,
    message_id: int = 1,
    title: str = "Original Title",
    gallery_ref: str = "",
) -> int:
    await database.initialize()
    await database.configure_telegram_source(
        source_type="CHANNEL",
        chat_id=-100456,
        display_name="Review Channel",
        enabled=True,
        allowed_archive_formats=("zip", "rar", "7z", "cbz"),
        max_attachment_size_mb=0,
    )
    await database.save_telegram_updates(
        [
            {
                "update_id": update_id,
                "channel_post": {
                    "message_id": message_id,
                    "date": 1_700_000_300,
                    "chat": {"id": -100456, "title": "Review Channel"},
                    "caption": f"{title}\n{gallery_ref}".strip(),
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
    assert candidates, "expected candidate to be created"
    return candidates[0].candidate_id


def replace_candidate_with_photo(
    database: Database, candidate_id: int
) -> None:
    attachment_json = json.dumps(
        [
            {
                "type": "photo",
                "file_id": f"photo-{candidate_id}",
                "file_unique_id": f"photo-{candidate_id}-uniq",
                "width": 800,
                "height": 1200,
                "size_bytes": 0,
            }
        ]
    )
    with database._connect() as connection:  # noqa: SLF001
        connection.execute(
            "UPDATE source_messages SET attachment_json = ? WHERE id IN ("
            "SELECT source_message_id FROM candidate_messages "
            "WHERE candidate_id = ?)",
            (attachment_json, candidate_id),
        )


def persist_bilingual_tags(database: Database, candidate_id: int) -> None:
    with database._connect() as connection:  # noqa: SLF001
        for field_name, field_value in (
            ("TagsRaw", "female:big breasts, language:chinese"),
            ("Tags", "巨乳, 汉语"),
        ):
            connection.execute(
                "INSERT INTO metadata_values "
                "(candidate_id, field_name, field_value, value_source, "
                "confidence, is_manual) VALUES (?, ?, ?, 'EXHENTAI', 0.6, 0)",
                (candidate_id, field_name, field_value),
            )


def test_approve_automatically_enqueues_download(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    database = Database(settings.data_path / "ehbot.db")
    candidate_id = asyncio.run(seed_candidate(database))

    with TestClient(create_app(settings), follow_redirects=False) as client:
        authenticate(client, settings)
        detail = client.get(f"/works/{candidate_id}")
        assert detail.status_code == 200
        csrf = detail.context["csrf_token"]
        response = client.post(
            f"/candidates/{candidate_id}/approve",
            data={"csrf_token": csrf},
        )
        assert response.status_code == 303

    jobs = asyncio.run(
        DownloadService(database, settings.work_path).list_jobs_for_candidate(
            candidate_id
        )
    )
    assert len(jobs) == 1
    assert jobs[0].provider == "TELEGRAM"

def test_reject_does_not_require_reason(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    database = Database(settings.data_path / "ehbot.db")
    candidate_id = asyncio.run(seed_candidate(database))

    with TestClient(create_app(settings), follow_redirects=False) as client:
        authenticate(client, settings)
        detail = client.get(f"/works/{candidate_id}")
        csrf = detail.context["csrf_token"]
        response = client.post(
            f"/candidates/{candidate_id}/reject",
            data={"csrf_token": csrf},
        )
        assert response.status_code == 303

    candidate = asyncio.run(database.get_candidate(candidate_id))
    assert candidate is not None
    assert candidate.status == "REJECTED"


def test_approve_without_download_source_keeps_candidate_pending(
    tmp_path: Path,
) -> None:
    settings = make_settings(tmp_path)
    database = Database(settings.data_path / "ehbot.db")
    candidate_id = asyncio.run(seed_candidate(database))
    replace_candidate_with_photo(database, candidate_id)

    with TestClient(create_app(settings), follow_redirects=False) as client:
        authenticate(client, settings)
        detail = client.get(f"/works/{candidate_id}")
        response = client.post(
            f"/candidates/{candidate_id}/approve",
            data={"csrf_token": detail.context["csrf_token"]},
        )
        assert response.status_code == 400
        # The message is source-agnostic now that four routes can supply a
        # candidate; naming only two of them would go stale again.
        assert "没有可用的下载来源" in response.text

    candidate = asyncio.run(database.get_candidate(candidate_id))
    assert candidate is not None
    assert candidate.status == "PENDING_REVIEW"


def test_a_gallery_with_a_torrent_routes_to_the_torrent(
    tmp_path: Path,
) -> None:
    """An oversized book takes the free original-quality route, not GP.

    Archive Download used to be the automatic fallback here. It spends GP, so
    it is a button now and the torrent is what routing picks.
    """
    settings = make_settings(tmp_path)
    database = Database(settings.data_path / "ehbot.db")
    candidate_id = asyncio.run(
        seed_candidate(
            database,
            gallery_ref="https://exhentai.org/g/4116328/c722b9009c/",
        )
    )
    replace_candidate_with_photo(database, candidate_id)
    with database._connect() as connection:  # noqa: SLF001
        connection.execute(
            "UPDATE candidates SET torrent_count = 1, torrent_hash = ? "
            "WHERE id = ?",
            ("4acbd66e5d0518977ece30c343eb75c4ca92b031", candidate_id),
        )

    with TestClient(create_app(settings), follow_redirects=False) as client:
        authenticate(client, settings)
        detail = client.get(f"/works/{candidate_id}")
        response = client.post(
            f"/candidates/{candidate_id}/approve",
            data={"csrf_token": detail.context["csrf_token"]},
        )
        assert response.status_code == 303

    jobs = asyncio.run(
        DownloadService(database, settings.work_path).list_jobs_for_candidate(
            candidate_id
        )
    )
    assert len(jobs) == 1
    assert jobs[0].provider == "EH_TORRENT"


def test_archive_download_is_never_an_automatic_route(
    tmp_path: Path,
) -> None:
    """A gallery with no torrent and no preview page has no automatic source.

    Spending GP is an operator decision, so approval refuses rather than
    quietly reaching for Archive Download.
    """
    settings = make_settings(tmp_path)
    database = Database(settings.data_path / "ehbot.db")
    candidate_id = asyncio.run(
        seed_candidate(
            database,
            gallery_ref="https://exhentai.org/g/4116328/c722b9009c/",
        )
    )
    replace_candidate_with_photo(database, candidate_id)
    with database._connect() as connection:  # noqa: SLF001
        connection.execute(
            "UPDATE candidates SET torrent_count = 0 WHERE id = ?",
            (candidate_id,),
        )

    with TestClient(create_app(settings), follow_redirects=False) as client:
        authenticate(client, settings)
        detail = client.get(f"/works/{candidate_id}")
        response = client.post(
            f"/candidates/{candidate_id}/approve",
            data={"csrf_token": detail.context["csrf_token"]},
        )
        assert response.status_code == 400
        assert "\u6ca1\u6709\u53ef\u7528\u7684\u4e0b\u8f7d\u6765\u6e90" in response.text

    jobs = asyncio.run(
        DownloadService(database, settings.work_path).list_jobs_for_candidate(
            candidate_id
        )
    )
    assert jobs == ()


def test_metadata_edit_persists_and_creates_action(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    database = Database(settings.data_path / "ehbot.db")
    candidate_id = asyncio.run(seed_candidate(database))

    with TestClient(create_app(settings), follow_redirects=False) as client:
        authenticate(client, settings)
        detail = client.get(f"/works/{candidate_id}")
        csrf = detail.context["csrf_token"]
        response = client.post(
            f"/candidates/{candidate_id}/metadata",
            data={
                "csrf_token": csrf,
                "field_name": "Title",
                "field_value": "Edited Title",
            },
        )
        assert response.status_code == 303

    metadata = asyncio.run(database.list_metadata(candidate_id))
    title_entry = next(m for m in metadata if m.field_name == "Title")
    assert title_entry.field_value == "Edited Title"
    assert title_entry.is_manual

    history = asyncio.run(database.list_review_actions(candidate_id))
    assert any(h.action == "EDIT_METADATA" for h in history)


def test_metadata_edit_rejects_unknown_field(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    database = Database(settings.data_path / "ehbot.db")
    candidate_id = asyncio.run(seed_candidate(database))

    with TestClient(create_app(settings), follow_redirects=False) as client:
        authenticate(client, settings)
        detail = client.get(f"/works/{candidate_id}")
        csrf = detail.context["csrf_token"]
        response = client.post(
            f"/candidates/{candidate_id}/metadata",
            data={
                "csrf_token": csrf,
                "field_name": "UnsupportedField",
                "field_value": "value",
            },
        )
        assert response.status_code == 400


def test_requeue_restores_pending_review(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    database = Database(settings.data_path / "ehbot.db")
    candidate_id = asyncio.run(seed_candidate(database))

    with TestClient(create_app(settings), follow_redirects=False) as client:
        authenticate(client, settings)
        detail = client.get(f"/works/{candidate_id}")
        csrf = detail.context["csrf_token"]
        client.post(
            f"/candidates/{candidate_id}/reject",
            data={"csrf_token": csrf},
        )
        rejected_detail = client.get(f"/works/{candidate_id}")
        rejected_csrf = rejected_detail.context["csrf_token"]
        response = client.post(
            f"/candidates/{candidate_id}/requeue",
            data={"csrf_token": rejected_csrf, "note": "double check"},
        )
        assert response.status_code == 303

    detail = asyncio.run(database.get_candidate(candidate_id))
    assert detail is not None
    assert detail.status == "PENDING_REVIEW"


def test_a_failed_candidate_can_be_requeued_but_not_approved(
    tmp_path: Path,
) -> None:
    """The dead end 历史下载记录 could put a work into.

    A download that fails leaves the candidate `FAILED`, which is in no
    reviewable state. Before `REQUEUEABLE_STATUSES` existed it also had no
    requeue, so a work whose retry was refused had no button on its page that
    could move it anywhere. Requeue is allowed and approve is still refused:
    the decision has to be taken again by a human looking at it.
    """
    database = Database(tmp_path / "ehbot.db")
    candidate_id = asyncio.run(seed_candidate(database, update_id=940))
    with database._connect() as connection:  # noqa: SLF001
        connection.execute(
            "UPDATE candidates SET status = 'FAILED' WHERE id = ?",
            (candidate_id,),
        )

    service = ReviewService(database)
    raised = False
    try:
        asyncio.run(service.approve_candidate(candidate_id, "admin"))
    except ReviewError:
        raised = True
    assert raised

    asyncio.run(service.requeue_candidate(candidate_id, "admin"))
    detail = asyncio.run(database.get_candidate(candidate_id))
    assert detail is not None
    assert detail.status == "PENDING_REVIEW"


def test_the_work_page_offers_requeue_on_a_failed_candidate(
    tmp_path: Path,
) -> None:
    """The button has to be *on the page*, not merely allowed by the service.

    The report that produced this test was 「点重试后需要审核，但没有审核按钮」,
    and both halves matter: the page reads `work.actions`, so a permission the
    action list does not expose is one the operator cannot reach.
    """
    settings = make_settings(tmp_path)
    database = Database(settings.data_path / "ehbot.db")
    candidate_id = asyncio.run(seed_candidate(database, update_id=941))
    with database._connect() as connection:  # noqa: SLF001
        connection.execute(
            "UPDATE candidates SET status = 'FAILED' WHERE id = ?",
            (candidate_id,),
        )

    with TestClient(create_app(settings), follow_redirects=False) as client:
        authenticate(client, settings)
        detail = client.get(f"/works/{candidate_id}")
        assert detail.status_code == 200
        assert detail.context["work"]["actions"]["requeue"] is True
        assert "重新排队" in detail.text
        response = client.post(
            f"/candidates/{candidate_id}/requeue",
            data={"csrf_token": detail.context["csrf_token"]},
        )
        assert response.status_code == 303

    detail = asyncio.run(database.get_candidate(candidate_id))
    assert detail is not None
    assert detail.status == "PENDING_REVIEW"


def test_review_service_rejects_invalid_status_transition(tmp_path: Path) -> None:
    database = Database(tmp_path / "ehbot.db")
    asyncio.run(database.initialize())

    raised = False
    try:
        asyncio.run(
            ReviewService(database).approve_candidate(999, "admin")
        )
    except ReviewError:
        raised = True
    assert raised


def test_rating_field_requires_numeric_value(tmp_path: Path) -> None:
    database = Database(tmp_path / "ehbot.db")
    asyncio.run(database.initialize())

    async def run() -> None:
        await database.configure_telegram_source(
            source_type="CHANNEL",
            chat_id=-100999,
            display_name="Rating Channel",
            enabled=True,
            allowed_archive_formats=("zip",),
            max_attachment_size_mb=0,
        )
        await database.save_telegram_updates(
            [
                {
                    "update_id": 901,
                    "channel_post": {
                        "message_id": 1,
                        "date": 1_700_000_400,
                        "chat": {
                            "id": -100999,
                            "title": "Rating Channel",
                        },
                        "caption": "Rating Test",
                        "photo": [
                            {
                                "file_id": "p",
                                "file_unique_id": "p-uniq",
                                "width": 100,
                                "height": 100,
                            }
                        ],
                    },
                }
            ]
        )
        await CandidateIngestor(database).process_pending_updates()
        candidates = await database.list_candidates()
        candidate_id = candidates[0].candidate_id
        try:
            await ReviewService(database).set_manual_metadata(
                candidate_id, "admin", "Rating", "not-a-number"
            )
        except ReviewError:
            return
        raise AssertionError("expected ReviewError for non-numeric rating")

    asyncio.run(run())


def test_batch_review_approves_and_rejects_multiple_candidates(
    tmp_path: Path,
) -> None:
    settings = make_settings(tmp_path)
    database = Database(settings.data_path / "ehbot.db")
    approve_ids = [
        asyncio.run(
            seed_candidate(
                database,
                update_id=910 + index,
                message_id=10 + index,
                title=f"Approve {index}",
            )
        )
        for index in range(2)
    ]
    reject_ids = [
        asyncio.run(
            seed_candidate(
                database,
                update_id=920 + index,
                message_id=20 + index,
                title=f"Reject {index}",
            )
        )
        for index in range(2)
    ]

    with TestClient(create_app(settings), follow_redirects=False) as client:
        authenticate(client, settings)
        queue = client.get("/candidates")
        csrf = queue.context["csrf_token"]
        approved = client.post(
            "/candidates/batch-review",
            data={
                "csrf_token": csrf,
                "action": "approve",
                "candidate_ids": [str(value) for value in approve_ids],
            },
        )
        assert approved.status_code == 303
        rejected = client.post(
            "/candidates/batch-review",
            data={
                "csrf_token": csrf,
                "action": "reject",
                "candidate_ids": [str(value) for value in reject_ids],
            },
        )
        assert rejected.status_code == 303

    for candidate_id in approve_ids:
        jobs = asyncio.run(
            DownloadService(
                database, settings.work_path
            ).list_jobs_for_candidate(candidate_id)
        )
        assert len(jobs) == 1
    for candidate_id in reject_ids:
        candidate = asyncio.run(database.get_candidate(candidate_id))
        assert candidate is not None
        assert candidate.status == "REJECTED"


def test_review_queue_fetches_and_caches_exhentai_metadata(
    tmp_path: Path,
) -> None:
    settings = make_settings(tmp_path)
    database = Database(settings.data_path / "ehbot.db")
    asyncio.run(
        seed_candidate(
            database,
            update_id=930,
            message_id=30,
            title="Gallery Candidate",
            gallery_ref="https://exhentai.org/g/4116328/c722b9009c/",
        )
    )
    requests = 0

    async def handler(_: httpx.Request) -> httpx.Response:
        nonlocal requests
        requests += 1
        return httpx.Response(
            200,
            json={
                "gmetadata": [
                    {
                        "gid": 4116328,
                        "token": "c722b9009c",
                        "title": "Gallery Candidate",
                        "category": "Doujinshi",
                        "uploader": "tester",
                        "filecount": "12",
                        "rating": "4.5",
                        "tags": [
                            "artist:kamisiro ryu",
                            "language:chinese",
                            "female:big breasts",
                        ],
                    }
                ]
            },
        )

    with TestClient(
        create_app(
            settings,
            exhentai_transport=httpx.MockTransport(handler),
        )
    ) as client:
        authenticate(client, settings)
        first = client.get("/candidates")
        assert first.status_code == 200
        assert "kamisiro ryu" in first.text
        assert "Doujinshi" in first.text
        assert "female:big breasts" in first.text
        second = client.get("/candidates")
        assert second.status_code == 200

    assert requests == 1


def test_review_views_show_original_and_chinese_tag_rows(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    database = Database(settings.data_path / "ehbot.db")
    candidate_id = asyncio.run(seed_candidate(database))
    persist_bilingual_tags(database, candidate_id)

    with TestClient(create_app(settings)) as client:
        authenticate(client, settings)
        queue = client.get("/candidates")
        assert queue.status_code == 200
        # The R5 list shows the translated tags on the card -- a grid of fifty
        # has no room for both rows -- and keeps the originals reachable through
        # the 「标签」 facet, which reads `Tags` and `TagsRaw` together. Both
        # halves of a bilingual candidate are therefore still on the page.
        assert '<span class="ui-tag">巨乳</span>' in queue.text
        assert '<span class="ui-tag">汉语</span>' in queue.text
        assert 'name="tags" value="female:big breasts"' in queue.text
        assert 'name="tags" value="language:chinese"' in queue.text

        detail = client.get(f"/works/{candidate_id}")
        assert detail.status_code == 200
        work = detail.context["work"]
        assert [entry["field_name"] for entry in work["metadata"][-2:]] == [
            "TagsRaw",
            "Tags",
        ]
        assert all(
            entry["field_name"] != "TagsRaw" for entry in work["raw_metadata"]
        )
        # Both rows reach the page, so an operator comparing a translation with
        # its original never has to open the JSON to do it.
        assert "巨乳" in detail.text
        assert "female:big breasts" in detail.text


def test_processing_and_failed_dashboard_queues_filter_candidates(
    tmp_path: Path,
) -> None:
    settings = make_settings(tmp_path)
    database = Database(settings.data_path / "ehbot.db")
    processing_id = asyncio.run(
        seed_candidate(
            database,
            update_id=940,
            message_id=40,
            title="Processing Candidate",
        )
    )
    failed_id = asyncio.run(
        seed_candidate(
            database,
            update_id=941,
            message_id=41,
            title="Failed Candidate",
        )
    )
    with database._connect() as connection:  # noqa: SLF001
        connection.execute(
            "UPDATE candidates SET status = 'PROCESSING' WHERE id = ?",
            (processing_id,),
        )
        connection.execute(
            "UPDATE candidates SET status = 'FAILED' WHERE id = ?",
            (failed_id,),
        )

    with TestClient(create_app(settings)) as client:
        authenticate(client, settings)
        dashboard = client.get("/")
        # PROCESSING lives under 「已通过」: the tab covers everything the
        # operator has already let through, from APPROVED to DOWNLOADED.
        assert "/candidates/approved" in dashboard.text
        assert "/candidates/failed" in dashboard.text
        processing = client.get("/candidates/approved")
        assert "Processing Candidate" in processing.text
        assert "Failed Candidate" not in processing.text
        failed = client.get("/candidates/failed")
        assert "Failed Candidate" in failed.text
        assert "Processing Candidate" not in failed.text
