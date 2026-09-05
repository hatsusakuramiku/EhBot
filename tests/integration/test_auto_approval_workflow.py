import asyncio
from pathlib import Path

from fastapi.testclient import TestClient

from app.auto_approval.sweeper import SWEEP_BATCH_SIZE, AutoApprovalSweeper
from app.candidates.ingestor import CandidateIngestor
from app.config import Settings
from app.db.database import Database
from app.downloads.service import DownloadService
from app.main import create_app


def _settings(root: Path) -> Settings:
    return Settings(
        data_path=root / "data",
        library_path=root / "library",
        work_path=root / "work",
        app_secret_key="test-secret-key-with-at-least-32-characters",
        tag_translation_enabled=False,
    )


async def _seed_candidate(database: Database) -> int:
    await database.initialize()
    await database.configure_telegram_source(
        source_type="CHANNEL",
        chat_id=-100987,
        display_name="Automatic Approval",
        enabled=True,
        allowed_archive_formats=("zip",),
        max_attachment_size_mb=0,
    )
    await database.save_telegram_updates(
        [{
            "update_id": 987,
            "channel_post": {
                "message_id": 1,
                "date": 1_700_000_000,
                "chat": {"id": -100987, "title": "Automatic Approval"},
                "caption": "Automatic Title",
                "document": {
                    "file_id": "archive",
                    "file_unique_id": "archive-unique",
                    "file_name": "archive.zip",
                    "file_size": 1024,
                },
            },
        }]
    )
    await CandidateIngestor(database).process_pending_updates()
    return (await database.list_candidates())[0].candidate_id


def _authenticate(client: TestClient, settings: Settings) -> None:
    password = (settings.data_path / "bootstrap_admin_password").read_text(
        encoding="utf-8"
    )
    login = client.get("/login")
    client.post("/login", data={"password": password, "csrf_token": login.context["csrf_token"]})
    change = client.get("/settings/passwords")
    client.post("/change-password", data={"current_password": password, "new_password": "new-password-with-12-characters", "confirmation": "new-password-with-12-characters", "csrf_token": change.context["csrf_token"]})


def test_first_matching_auto_rule_approves_enqueues_and_audits(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    database = Database(settings.data_path / "ehbot.db")
    candidate_id = asyncio.run(_seed_candidate(database))
    first = asyncio.run(database.save_auto_approval_rule(
        rule_id=None,
        name="First",
        enabled=True,
        priority=10,
        condition={"kind": "condition", "field": "Title", "operator": "=", "value": "Automatic Title"},
        dsl_snapshot='{Title} = "Automatic Title"',
    ))
    asyncio.run(database.save_auto_approval_rule(
        rule_id=None,
        name="Second",
        enabled=True,
        priority=20,
        condition={"kind": "condition", "field": "Title", "operator": "=", "value": "Automatic Title"},
        dsl_snapshot='{Title} = "Automatic Title"',
    ))

    with TestClient(create_app(settings)) as client:
        _authenticate(client, settings)
        response = client.get("/candidates")
        assert response.status_code == 200
        page = client.get("/settings/auto-approval")
        assert "已保存规则" in page.text
        assert "First" in page.text

    jobs = asyncio.run(DownloadService(database, settings.work_path).list_jobs_for_candidate(candidate_id))
    actions = asyncio.run(database.list_review_actions(candidate_id))
    audit = next(action for action in actions if action.action == "AUTO_APPROVE")
    assert len(jobs) == 1
    assert audit.details["rule_id"] == first.rule_id
    assert audit.details["rule_version"] == 1
    assert audit.details["metadata"]["Title"] == "Automatic Title"
    assert audit.details["download_job_ids"] == [jobs[0].job_id]


def test_the_sweeper_approves_without_anybody_opening_the_page(
    tmp_path: Path,
) -> None:
    """The reported bug: rules only fired while 待审核 was open.

    `sweep_once` is driven directly rather than by starting the task and
    sleeping -- the schedule is what the setting controls, and a test that waited
    for it would be a slow test of `asyncio.sleep`. No page is requested here at
    all, which is exactly the condition that used to approve nothing.
    """
    settings = _settings(tmp_path)
    database = Database(settings.data_path / "ehbot.db")
    candidate_id = asyncio.run(_seed_candidate(database))
    asyncio.run(
        database.save_auto_approval_rule(
            rule_id=None,
            name="Sweep",
            enabled=True,
            priority=10,
            condition={
                "kind": "condition",
                "field": "Title",
                "operator": "=",
                "value": "Automatic Title",
            },
            dsl_snapshot='{Title} = "Automatic Title"',
        )
    )

    with TestClient(create_app(settings)) as client:
        sweeper = client.app.state.auto_approval_sweeper
        assert sweeper is not None
        assert asyncio.run(sweeper.sweep_once()) == 1

    detail = asyncio.run(database.get_candidate(candidate_id))
    assert detail is not None
    assert detail.status in {"APPROVED", "PROCESSING", "DOWNLOADED"}
    actions = asyncio.run(database.list_review_actions(candidate_id))
    assert any(action.action == "AUTO_APPROVE" for action in actions)


def test_a_sweep_with_no_matching_rule_approves_nothing(tmp_path: Path) -> None:
    """A sweep is not an approval: with no rule, the queue is left alone."""
    settings = _settings(tmp_path)
    database = Database(settings.data_path / "ehbot.db")
    candidate_id = asyncio.run(_seed_candidate(database))

    with TestClient(create_app(settings)) as client:
        assert asyncio.run(client.app.state.auto_approval_sweeper.sweep_once()) == 0

    detail = asyncio.run(database.get_candidate(candidate_id))
    assert detail is not None
    assert detail.status == "PENDING_REVIEW"


def test_one_unapprovable_candidate_does_not_stop_the_sweep() -> None:
    """A sweep is a batch, and a batch must survive one bad row.

    The failure mode this guards is a queue that stops being swept forever
    because the oldest candidate in it happens to raise -- a misconfigured
    provider, or metadata that will not parse. It stays pending for a human and
    the sweep carries on.
    """

    class FakeDatabase:
        async def pending_candidate_ids(
            self, limit: int = 100, *, oldest_first: bool = False
        ) -> tuple[int, ...]:
            # The sweeper asks for oldest-first, so the fake has to accept it or
            # this test would pass against a sweeper that stopped asking.
            assert oldest_first is True
            return (1, 2, 3)

    class FakeOrchestrator:
        def __init__(self) -> None:
            self.seen: list[int] = []

        async def apply_automatic_approval(self, candidate_id: int) -> bool:
            self.seen.append(candidate_id)
            if candidate_id == 2:
                raise RuntimeError("provider is not configured")
            return True

    class FakeSettings:
        async def auto_approval_interval_minutes(self) -> int:
            return 30

    orchestrator = FakeOrchestrator()
    sweeper = AutoApprovalSweeper(
        FakeDatabase(), orchestrator, FakeSettings()
    )

    assert asyncio.run(sweeper.sweep_once()) == 2
    assert orchestrator.seen == [1, 2, 3]


def test_the_sweep_reads_the_oldest_candidates_first(tmp_path: Path) -> None:
    """A batch ceiling only works as a queue if the oldest row is next.

    `pending_candidate_ids` answers newest-first for the trial run, which reads
    「这条规则会命中刚到的东西吗」. For the sweeper that order is a window that
    never moves: with more pending candidates than `SWEEP_BATCH_SIZE`, every pass
    re-read the same newest page, so a candidate no rule matched sat at the top of
    it forever and everything older than the hundredth row was never evaluated at
    all. The sweeper therefore asks for the other order.
    """
    settings = _settings(tmp_path)
    database = Database(settings.data_path / "ehbot.db")
    asyncio.run(_seed_candidate(database))

    def _add_pending(count: int) -> None:
        with database._connect() as connection:  # noqa: SLF001
            for _ in range(count):
                connection.execute(
                    "INSERT INTO candidates (status, filter_result, filter_reason) "
                    "VALUES ('PENDING_REVIEW', 'ACCEPT', '')"
                )

    _add_pending(SWEEP_BATCH_SIZE + 5)

    newest = asyncio.run(database.pending_candidate_ids(limit=SWEEP_BATCH_SIZE))
    oldest = asyncio.run(
        database.pending_candidate_ids(
            limit=SWEEP_BATCH_SIZE, oldest_first=True
        )
    )

    assert len(newest) == len(oldest) == SWEEP_BATCH_SIZE
    # The five oldest rows are the ones a newest-first window could never reach.
    stranded = set(oldest) - set(newest)
    assert stranded, "a full batch must leave older rows outside the window"

    seen: list[int] = []

    class RecordingOrchestrator:
        async def apply_automatic_approval(self, candidate_id: int) -> bool:
            seen.append(candidate_id)
            return False

    class FakeSettings:
        async def auto_approval_interval_minutes(self) -> int:
            return 30

    sweeper = AutoApprovalSweeper(
        database, RecordingOrchestrator(), FakeSettings()
    )
    assert asyncio.run(sweeper.sweep_once()) == 0

    assert seen == list(oldest)
    assert stranded <= set(seen)
