"""Stage, actions and timeline for the unified work detail page (R6).

Unit tests over `app.api.works` and the two helpers R6 added elsewhere, for the
same reason `test_activity_grouping.py` is a unit test: what is worth pinning is
the *policy* -- which stage a work is in, which buttons that stage may offer, and
what the timeline is allowed to claim -- while
`tests/integration/test_candidates_web.py` covers that the page renders whatever
these return.
"""

from __future__ import annotations

from app.api.status import (
    ACTOR_AUTO_RULE,
    ACTOR_OPERATOR,
    ACTOR_SYSTEM,
    ATTACHMENT_ARCHIVE,
    ATTACHMENT_PHOTO,
    STAGE_ARCHIVED,
    STAGE_CANDIDATE,
    STAGE_DOWNLOAD,
    actor_kind,
    attachment_kind_view,
)
from app.api.works import work_actions, work_stage, work_timeline
from app.candidates.models import CandidateDetail, CandidateMessage
from app.downloads.models import (
    DEFAULT_JOB_PRIORITY,
    PROVIDER_CONVERSION,
    PROVIDER_EH_TORRENT,
    PROVIDER_EXHENTAI,
    PROVIDER_TELEGRAM,
    PROVIDER_TELEGRAM_USER,
    PROVIDER_TELEGRAPH,
    DownloadJobSummary,
)
from app.review.models import AUTO_OPERATOR, SYSTEM_OPERATOR, ReviewActionEntry
from app.web.deps import local_return_to


ALL_SOURCES = frozenset(
    {
        PROVIDER_TELEGRAM,
        PROVIDER_TELEGRAM_USER,
        PROVIDER_EXHENTAI,
        PROVIDER_TELEGRAPH,
        PROVIDER_EH_TORRENT,
    }
)


def make_candidate(
    *,
    status: str = "PENDING_REVIEW",
    ex_gid: int | None = 1234,
    preview_url: str | None = "https://telegra.ph/example",
    torrent_count: int | None = 2,
    torrent_hash: str | None = "a" * 40,
    attachments: tuple[dict, ...] = ({"type": ATTACHMENT_ARCHIVE, "file_name": "a.zip"},),
) -> CandidateDetail:
    return CandidateDetail(
        candidate_id=7,
        status=status,
        filter_result="ACCEPTED",
        filter_reason="",
        title="Fixture Work",
        ex_gid=ex_gid,
        ex_gallery_token="token",
        messages=(
            CandidateMessage(
                chat_title="Fixture Channel",
                message_id=60,
                message_text="hello",
                attachments=attachments,
                message_date="2026-08-26T00:00:00Z",
            ),
        ),
        preview_url=preview_url,
        torrent_count=torrent_count,
        torrent_hash=torrent_hash,
    )


def make_job(
    job_id: int = 1,
    *,
    provider: str = PROVIDER_TELEGRAM,
    state: str = "PENDING",
    artifact_path: str | None = None,
    artifact_cbz_path: str | None = None,
    error_message: str | None = None,
    created_at: str = "2026-08-26T00:30:00Z",
) -> DownloadJobSummary:
    return DownloadJobSummary(
        job_id=job_id,
        candidate_id=7,
        provider=provider,
        state=state,
        attempt_count=0,
        error_code=None,
        error_message=error_message,
        artifact_path=artifact_path,
        artifact_size=None,
        created_at=created_at,
        # A node is placed where the task was born, so `updated_at` is not what
        # the timeline sorts on and this fixture leaves it fixed.
        updated_at="2026-08-26T09:00:00Z",
        details={},
        artifact_cbz_path=artifact_cbz_path,
        priority=DEFAULT_JOB_PRIORITY,
    )


def packaged_job(job_id: int = 9) -> DownloadJobSummary:
    return make_job(
        job_id,
        provider=PROVIDER_CONVERSION,
        state="COMPLETED",
        artifact_cbz_path="/works/7/book.cbz",
        created_at="2026-08-26T02:00:00Z",
    )


# --- stage -----------------------------------------------------------------


def test_a_reviewable_candidate_with_no_jobs_is_in_the_candidate_stage() -> None:
    assert work_stage("PENDING_REVIEW", ()) == STAGE_CANDIDATE
    assert work_stage("NEEDS_INFO", ()) == STAGE_CANDIDATE
    assert work_stage("REJECTED", ()) == STAGE_CANDIDATE


def test_an_approved_work_is_in_the_download_stage_before_any_job_exists() -> None:
    """The stage follows the decision, not the queue.

    An operator who has just pressed 通过并下载 should not see 候选期 for the
    second it takes the worker to insert the job.
    """
    assert work_stage("APPROVED", ()) == STAGE_DOWNLOAD


def test_a_failed_download_stays_in_the_download_stage() -> None:
    """`FAILED` is a download-stage state, not a return to review.

    The operator's next action is a task action -- 重试 -- so the action bar has
    to be the download one.
    """
    assert work_stage("FAILED", (make_job(state="FAILED"),)) == STAGE_DOWNLOAD


def test_a_packaged_work_stays_archived_even_with_a_new_job_in_flight() -> None:
    """入库期 wins over an open task.

    A re-download or a re-package puts a live job on a work that already has a
    book on disk. The book is what the operator came for, so the stage keeps
    saying 入库期 and the live task shows up on the timeline instead.
    """
    jobs = (packaged_job(), make_job(2, state="DOWNLOADING"))
    assert work_stage("DOWNLOADED", jobs) == STAGE_ARCHIVED


def test_a_reviewable_candidate_that_somehow_has_a_job_is_still_a_candidate() -> None:
    """REVIEWABLE_STATUSES is checked before the job list.

    A candidate that was requeued after a failed download keeps its review
    actions, because those are the ones that can still change anything.
    """
    jobs = (make_job(state="FAILED"),)
    assert work_stage("NEEDS_REVISION", jobs) == STAGE_CANDIDATE


def test_an_unknown_status_with_no_jobs_falls_back_to_the_candidate_stage() -> None:
    assert work_stage("SOMETHING_NEW", ()) == STAGE_CANDIDATE
    assert work_stage(None, ()) == STAGE_CANDIDATE


def test_an_unknown_status_with_a_job_is_in_the_download_stage() -> None:
    assert work_stage("SOMETHING_NEW", (make_job(),)) == STAGE_DOWNLOAD


# --- actions ---------------------------------------------------------------


def test_review_actions_are_offered_only_while_the_work_is_reviewable() -> None:
    reviewable = work_actions(make_candidate(), (), ALL_SOURCES)
    assert reviewable["approve"] is True
    assert reviewable["reject"] is True
    assert reviewable["needs_revision"] is True

    approved = work_actions(make_candidate(status="APPROVED"), (), ALL_SOURCES)
    assert approved["approve"] is False
    assert approved["reject"] is False
    assert approved["needs_revision"] is False


def test_requeue_is_offered_only_where_it_changes_something() -> None:
    """A PENDING_REVIEW candidate is already in the queue requeue moves it to.

    `FAILED` is here and in no reviewable state, which is the whole reason
    `REQUEUEABLE_STATUSES` is a separate set: a failed download must not be
    approvable without a second look, but it has to have *some* way back, or the
    work is a dead end with no button that can move it.
    """
    for status, expected in (
        ("REJECTED", True),
        ("NEEDS_REVISION", True),
        ("FAILED", True),
        ("PENDING_REVIEW", False),
        ("NEEDS_INFO", False),
        ("APPROVED", False),
    ):
        actions = work_actions(make_candidate(status=status), (), ALL_SOURCES)
        assert actions["requeue"] is expected, status


def test_a_failed_candidate_is_requeueable_but_not_reviewable() -> None:
    """The two sets are deliberately different, and the page reads both."""
    actions = work_actions(make_candidate(status="FAILED"), (), ALL_SOURCES)
    assert actions["requeue"] is True
    assert actions["approve"] is False
    assert actions["reject"] is False
    assert actions["needs_revision"] is False


def test_metadata_stays_editable_at_every_stage() -> None:
    """A wrong title found after packaging is exactly when it gets fixed."""
    for status in ("PENDING_REVIEW", "APPROVED", "DOWNLOADED", "FAILED"):
        actions = work_actions(
            make_candidate(status=status), (packaged_job(),), ALL_SOURCES
        )
        assert actions["edit_metadata"] is True, status


def test_fetching_metadata_needs_both_a_gallery_and_a_configured_account() -> None:
    with_gallery = work_actions(make_candidate(), (), ALL_SOURCES)
    assert with_gallery["fetch_metadata"] is True

    no_gallery = work_actions(make_candidate(ex_gid=None), (), ALL_SOURCES)
    assert no_gallery["fetch_metadata"] is False

    unconfigured = work_actions(make_candidate(), (), frozenset())
    assert unconfigured["fetch_metadata"] is False


def test_packaging_is_offered_once_any_provider_has_left_an_archive() -> None:
    """Any completed download is packaging's input, not only Telegram's."""
    for provider in (PROVIDER_TELEGRAM, PROVIDER_EH_TORRENT, PROVIDER_TELEGRAPH):
        jobs = (
            make_job(provider=provider, state="COMPLETED", artifact_path="/a.zip"),
        )
        actions = work_actions(make_candidate(status="DOWNLOADED"), jobs, ALL_SOURCES)
        assert actions["convert"] is True, provider


def test_an_incomplete_download_does_not_offer_packaging() -> None:
    jobs = (make_job(state="DOWNLOADING"),)
    actions = work_actions(make_candidate(status="PROCESSING"), jobs, ALL_SOURCES)
    assert actions["convert"] is False
    assert actions["reconvert"] is False


def test_a_packaged_work_offers_repackaging_instead_of_packaging() -> None:
    jobs = (
        make_job(state="COMPLETED", artifact_path="/a.zip"),
        packaged_job(),
    )
    actions = work_actions(make_candidate(status="DOWNLOADED"), jobs, ALL_SOURCES)
    assert actions["reconvert"] is True


def test_every_source_is_listed_even_when_it_cannot_run() -> None:
    """Every source, always -- an absent button is unreadable.

    An operator who cannot find 「EH 种子」 cannot tell a gallery with no torrent
    from a page that forgot to render the button, so an unavailable source stays
    on the bar carrying its reason.
    """
    candidate = make_candidate(
        ex_gid=None,
        preview_url=None,
        torrent_count=0,
        torrent_hash=None,
        attachments=({"type": ATTACHMENT_PHOTO, "file_id": "abc"},),
    )
    sources = work_actions(candidate, (), ALL_SOURCES)["sources"]

    assert [entry["provider"]["code"] for entry in sources] == [
        PROVIDER_EH_TORRENT,
        PROVIDER_EXHENTAI,
        PROVIDER_TELEGRAPH,
        PROVIDER_TELEGRAM,
        PROVIDER_TELEGRAM_USER,
    ]
    assert [entry["available"] for entry in sources] == [
        False,
        False,
        False,
        False,
        False,
    ]
    by_provider = {entry["provider"]["code"]: entry for entry in sources}
    assert by_provider[PROVIDER_EH_TORRENT]["hint"] == "画廊没有可用种子"
    assert by_provider[PROVIDER_EXHENTAI]["hint"] == "没有关联画廊"
    assert by_provider[PROVIDER_TELEGRAPH]["hint"] == "没有预览页"
    # This candidate arrived as a photo, so the user account has nothing to
    # fetch either -- and says which of the two reasons it is.
    assert by_provider[PROVIDER_TELEGRAM_USER]["hint"] == "来源消息没有压缩附件"


def test_an_unqueried_gallery_says_so_instead_of_claiming_no_torrent() -> None:
    """NULL `torrent_count` is 「尚未拉取」, an explicit 0 is 「无种」."""
    candidate = make_candidate(torrent_count=None, torrent_hash=None)
    sources = work_actions(candidate, (), ALL_SOURCES)["sources"]
    by_provider = {entry["provider"]["code"]: entry for entry in sources}
    assert by_provider[PROVIDER_EH_TORRENT]["hint"] == "尚未拉取种子信息"


def test_a_source_the_deployment_cannot_reach_is_unavailable() -> None:
    """A button for an unconfigured qBittorrent could only ever fail."""
    sources = work_actions(make_candidate(), (), frozenset())["sources"]
    by_provider = {entry["provider"]["code"]: entry for entry in sources}
    assert by_provider[PROVIDER_EH_TORRENT]["available"] is False
    assert by_provider[PROVIDER_EXHENTAI]["available"] is False
    assert by_provider[PROVIDER_TELEGRAPH]["available"] is False
    # Telegram is not gated on a connection: the archive is already in the
    # message this candidate came from.
    assert by_provider[PROVIDER_TELEGRAM]["available"] is True


def test_telegram_is_offered_only_when_a_message_carried_an_archive() -> None:
    photo_only = make_candidate(
        attachments=({"type": ATTACHMENT_PHOTO, "file_id": "abc"},)
    )
    sources = work_actions(photo_only, (), ALL_SOURCES)["sources"]
    by_provider = {entry["provider"]["code"]: entry for entry in sources}
    assert by_provider[PROVIDER_TELEGRAM]["available"] is False


def test_every_source_carries_the_route_that_runs_it() -> None:
    """The provider-to-route mapping lives here, not in Jinja.

    The page and the JSON client post to the same paths because they read the
    same table.
    """
    sources = work_actions(make_candidate(), (), ALL_SOURCES)["sources"]
    assert {
        entry["provider"]["code"]: entry["action"] for entry in sources
    } == {
        PROVIDER_EH_TORRENT: "/candidates/7/torrent",
        PROVIDER_EXHENTAI: "/candidates/7/exhentai-archive",
        PROVIDER_TELEGRAPH: "/candidates/7/telegraph",
        PROVIDER_TELEGRAM: "/candidates/7/download",
        PROVIDER_TELEGRAM_USER: "/candidates/7/telegram-user",
    }


# --- timeline --------------------------------------------------------------


def review_entry(
    action: str = "APPROVE",
    *,
    operator_name: str = "admin",
    details: dict | None = None,
    created_at: str = "2026-08-26T00:20:00Z",
) -> ReviewActionEntry:
    return ReviewActionEntry(
        action=action,
        operator_name=operator_name,
        details=details or {},
        created_at=created_at,
    )


def test_the_timeline_merges_both_tables_newest_first() -> None:
    """One story told in two tables, interleaved by the server.

    An operator reading a review list beside a job list has to do this sort in
    their head, which is exactly the thing R6 exists to stop.
    """
    nodes = work_timeline(
        (
            review_entry(created_at="2026-08-26T00:20:00Z"),
            review_entry("REQUEUE", created_at="2026-08-26T03:00:00Z"),
        ),
        (make_job(created_at="2026-08-26T01:00:00Z"), packaged_job()),
    )
    assert [node["at"] for node in nodes] == [
        "2026-08-26T03:00:00Z",
        "2026-08-26T02:00:00Z",
        "2026-08-26T01:00:00Z",
        "2026-08-26T00:20:00Z",
    ]
    assert [node["kind"] for node in nodes] == ["REVIEW", "JOB", "JOB", "REVIEW"]


def test_a_job_contributes_one_node_carrying_its_actions() -> None:
    """One node per job, not one per transition.

    The queue keeps no transition history, so inventing nodes for states a job
    passed through would be a timeline claiming to know more than the database.
    """
    nodes = work_timeline((), (make_job(state="PENDING"),))
    assert len(nodes) == 1
    node = nodes[0]
    assert node["kind"] == "JOB"
    assert node["state"]["code"] == "PENDING"
    assert node["actions"] == {
        "retry": False,
        "pause": True,
        "resume": False,
        "cancel": True,
    }


def test_a_failed_job_says_why_on_its_own_node() -> None:
    """A task is retried where the operator finds out it failed."""
    jobs = (make_job(state="FAILED", error_message="连接超时"),)
    node = work_timeline((), jobs)[0]
    assert node["reason"] == "连接超时"
    assert node["actions"]["retry"] is True


def test_a_healthy_job_says_everything_through_its_state() -> None:
    node = work_timeline((), (make_job(state="DOWNLOADING"),))[0]
    assert node["reason"] is None


def test_the_timeline_names_the_actor_behind_every_review_row() -> None:
    """「谁决定的」 decides whether the operator argues with a rule or a person."""
    nodes = work_timeline(
        (
            review_entry(operator_name="admin"),
            review_entry(
                "AUTO_APPROVE",
                operator_name=AUTO_OPERATOR,
                created_at="2026-08-26T00:10:00Z",
            ),
            review_entry(
                "METADATA_RULE",
                operator_name=SYSTEM_OPERATOR,
                created_at="2026-08-26T00:05:00Z",
            ),
        ),
        (),
    )
    assert [node["actor"]["code"] for node in nodes] == [
        ACTOR_OPERATOR,
        ACTOR_AUTO_RULE,
        ACTOR_SYSTEM,
    ]


def test_a_retired_verb_still_renders_rather_than_blanking_the_row() -> None:
    """A timeline is history: a verb dropped from the code is still in the table."""
    node = work_timeline((review_entry("SOMETHING_OLD"),), ())[0]
    assert node["action"]["label"] == "SOMETHING_OLD"


def test_a_job_and_the_approval_that_created_it_keep_their_order() -> None:
    """Ties put the review row after the job it enqueued.

    Both land in the same second, and a stable sort over the build order is what
    settles them, so the reading order stays cause-then-effect downwards.
    """
    same_second = "2026-08-26T01:00:00Z"
    nodes = work_timeline(
        (review_entry(created_at=same_second),),
        (make_job(created_at=same_second),),
    )
    assert [node["kind"] for node in nodes] == ["JOB", "REVIEW"]


def test_a_row_with_no_timestamp_sorts_last_instead_of_raising() -> None:
    nodes = work_timeline((review_entry(created_at=None),), (make_job(),))
    assert [node["kind"] for node in nodes] == ["JOB", "REVIEW"]


# --- attachment vocabulary -------------------------------------------------


def test_the_two_attachment_kinds_have_words() -> None:
    assert attachment_kind_view(ATTACHMENT_PHOTO).label == "图片预览"
    assert attachment_kind_view(ATTACHMENT_ARCHIVE).label == "压缩包"


def test_an_unknown_attachment_kind_shows_itself_rather_than_raising() -> None:
    """The ingestor may learn a third kind before this registry does."""
    assert attachment_kind_view("video").label == "video"
    assert attachment_kind_view(None).label == "附件"


def test_the_actor_kind_is_derived_from_the_two_reserved_names() -> None:
    assert actor_kind(AUTO_OPERATOR) == ACTOR_AUTO_RULE
    assert actor_kind(SYSTEM_OPERATOR) == ACTOR_SYSTEM
    assert actor_kind("admin") == ACTOR_OPERATOR
    # A row written before this vocabulary existed still gets an actor.
    assert actor_kind(None) == ACTOR_OPERATOR


# --- return_to -------------------------------------------------------------


def test_a_rooted_local_path_is_honoured() -> None:
    assert local_return_to("/works/12") == "/works/12"
    assert local_return_to("  /works/12  ") == "/works/12"
    assert local_return_to("/works/12?tab=all") == "/works/12?tab=all"


def test_an_absolute_url_is_refused() -> None:
    """The hidden field is an open-redirect surface, so only paths get through."""
    assert local_return_to("https://evil.example/x") is None
    assert local_return_to("http://evil.example/x") is None
    assert local_return_to("javascript:alert(1)") is None


def test_a_protocol_relative_target_is_refused() -> None:
    """Browsers treat `//host` as another origin, however local it looks."""
    assert local_return_to("//evil.example/works/12") is None


def test_a_backslash_is_refused_because_parsers_normalise_it() -> None:
    assert local_return_to("/\\evil.example") is None
    assert local_return_to("\\\\evil.example") is None


def test_a_control_character_is_refused() -> None:
    """`/\\nLocation: …` must not become a second header anywhere downstream."""
    assert local_return_to("/works/12\nX") is None
    assert local_return_to("/works/12\r\nX") is None
    assert local_return_to("/works/12\x7f") is None


def test_nothing_at_all_is_not_a_target() -> None:
    assert local_return_to(None) is None
    assert local_return_to("") is None
    assert local_return_to("   ") is None
    assert local_return_to("works/12") is None
