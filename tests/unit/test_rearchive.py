"""一键重新归档: which works it picks up, and what it does to each.

Unit tests over `app.archive.rearchive` with the three collaborators faked, for
the same reason `test_downloaded_api.py` fakes its two: what is worth pinning is
the *policy* -- the scope a default run has, what 强制 adds, and the
「已打包不再重新打包」 split between queueing and moving -- while
`tests/integration/test_settings_web.py` covers that the button renders and
reaches this code.

The distinction the whole feature turns on is asserted per work rather than by
counting: 「未归档的入队、已归档的移动、手动指定的按情况跳过或覆盖」 has to be true
of a particular book, not of an average.
"""

from __future__ import annotations

import asyncio
from pathlib import PurePosixPath

import pytest

from app.archive.rearchive import (
    apply_rearchive,
    plan_rearchive,
    rearchive_works,
)
from app.conversion.naming import LibraryPathError
from app.downloads.models import (
    CONVERSION_STATE_COMPLETED,
    CONVERSION_STATE_FAILED,
    CONVERSION_STATE_PENDING,
    CONVERSION_STATE_WAITING_PATH,
    ReArchiveCandidate,
)


def candidate(
    candidate_id: int,
    *,
    title: str | None = "作品 1",
    pack_state: str | None = None,
    cbz_path: str | None = None,
    library_relative_path: str | None = None,
    pinned_path: str | None = None,
    pinned_is_manual: bool = False,
) -> ReArchiveCandidate:
    return ReArchiveCandidate(
        candidate_id=candidate_id,
        title=title,
        pack_state=pack_state,
        cbz_path=cbz_path,
        library_relative_path=library_relative_path,
        pinned_path=pinned_path,
        pinned_is_manual=pinned_is_manual,
    )


class FakeDatabase:
    def __init__(self, candidates) -> None:
        self.candidates = tuple(candidates)

    async def list_rearchive_candidates(self):
        return list(self.candidates)


class FakeArchived:
    """The actions the sweep may take, recorded in the order they were asked for."""

    def __init__(self, *, unchanged=(), refusals=None) -> None:
        self.pinned: list[tuple[int, str]] = []
        self.parked: list[tuple[int, str, str]] = []
        self.refiled: list[tuple[int, str]] = []
        self.cleared: list[int] = []
        # Works whose file is already at the recomputed path: `refile_work`
        # answers `moved=False`, which the report distinguishes from a move.
        self.unchanged = set(unchanged)
        # id -> exception, for a move or a pin the service refuses.
        self.refusals = dict(refusals or {})

    async def pin_computed_path(self, candidate_id, relative_path, *, operator_name="admin"):
        self.pinned.append((candidate_id, relative_path))
        self._maybe_refuse(candidate_id)

    async def clear_path_pin(self, candidate_id):
        self.cleared.append(candidate_id)

    async def park_for_invalid_path(self, candidate_id, code, message):
        self.parked.append((candidate_id, code, message))

    async def refile_work(
        self,
        candidate_id,
        relative_path,
        *,
        operator_name="admin",
        allow_while_running=False,
    ):
        self.refiled.append((candidate_id, relative_path))
        self._maybe_refuse(candidate_id)
        return {
            "candidate_id": candidate_id,
            "path": f"/library/{relative_path}",
            "relative_path": relative_path,
            "moved": candidate_id not in self.unchanged,
        }

    def _maybe_refuse(self, candidate_id: int) -> None:
        refusal = self.refusals.get(candidate_id)
        if refusal is not None:
            raise refusal


class FakeConversion:
    """Path planning and the packing queue, without a packer."""

    def __init__(self, *, plans=None, unusable=(), refusals=None, no_title=()) -> None:
        self.plans = dict(plans or {})
        self.unusable = set(unusable)
        self.no_title = set(no_title)
        # id -> exception from `enqueue_for_candidate`.
        self.refusals = dict(refusals or {})
        self.enriched: list[int] = []
        self.planned: list[int] = []
        self.enqueued: list[int] = []
        # (candidate_id, refile, refresh_ai_path) in the order they were queued.
        self.enqueue_calls: list[tuple[int, bool, bool]] = []

    #: Template mode, which is what every case in this file except
    #: `TestAiModeScope` is about. The AI branch replaces `planned_path_for_candidate`
    #: with a cache read, so both are provided and neither is assumed.
    async def path_source(self) -> str:
        return "template"

    async def metadata_for(self, candidate_id: int):
        return ()

    async def current_ai_path(self, candidate_id: int, metadata):
        return None

    async def ensure_metadata(self, candidate_id: int) -> None:
        self.enriched.append(candidate_id)

    async def planned_path_for_candidate(self, candidate_id: int):
        self.planned.append(candidate_id)
        if candidate_id in self.unusable:
            raise LibraryPathError("SEGMENT_TOO_LONG", "作品标题过长")
        if candidate_id in self.no_title:
            return None
        return PurePosixPath(self.plans.get(candidate_id, f"作者/作品 {candidate_id}.cbz"))

    async def enqueue_for_candidate(
        self, candidate_id: int, *, refile: bool = False, refresh_ai_path: bool = False
    ) -> int:
        refusal = self.refusals.get(candidate_id)
        if refusal is not None:
            raise refusal
        self.enqueued.append(candidate_id)
        self.enqueue_calls.append((candidate_id, refile, refresh_ai_path))
        return candidate_id * 100


def plan(database, conversion, *, force=False, dry_run=False, archived=None):
    return asyncio.run(
        plan_rearchive(
            database,
            archived if archived is not None else FakeArchived(),
            conversion,
            force=force,
            dry_run=dry_run,
        )
    )


def run_sweep(database, conversion, *, force=False, dry_run=False, archived=None):
    """Plan and execute, the way the page's button does."""
    return asyncio.run(
        rearchive_works(
            database,
            archived if archived is not None else FakeArchived(),
            conversion,
            force=force,
            dry_run=dry_run,
        )
    )


def targets(plan_result) -> list[tuple[int, bool, str | None]]:
    return [(t.candidate_id, t.packed, t.target) for t in plan_result.tasks]


def modes(plan_result) -> list[tuple[int, str, str | None]]:
    return [(t.candidate_id, t.mode, t.target) for t in plan_result.tasks]


def skip_codes(plan_result) -> list[tuple[int, str]]:
    return [(s.candidate_id, s.code) for s in plan_result.skipped]


class TestDefaultScope:
    """未归档的、以及路径已变动的 -- and nothing else."""

    def test_a_work_that_was_never_packed_is_queued(self) -> None:
        result = plan(
            FakeDatabase([candidate(1)]), FakeConversion(plans={1: "作者/书.cbz"})
        )
        assert targets(result) == [(1, False, "作者/书.cbz")]

    def test_a_failed_pack_is_queued_again(self) -> None:
        """「尚未成功完成归档」 includes the ones that tried and broke."""
        result = plan(
            FakeDatabase([candidate(1, pack_state=CONVERSION_STATE_FAILED)]),
            FakeConversion(plans={1: "作者/书.cbz"}),
        )
        assert targets(result) == [(1, False, "作者/书.cbz")]

    def test_a_parked_work_is_queued_again(self) -> None:
        """WAITING_PATH is an ask, not a failure -- pressing the button answers it."""
        result = plan(
            FakeDatabase([candidate(1, pack_state=CONVERSION_STATE_WAITING_PATH)]),
            FakeConversion(plans={1: "作者/书.cbz"}),
        )
        assert targets(result) == [(1, False, "作者/书.cbz")]

    def test_a_pack_already_in_flight_is_left_alone(self) -> None:
        result = plan(
            FakeDatabase([candidate(1, pack_state=CONVERSION_STATE_PENDING)]),
            FakeConversion(),
        )
        assert result.tasks == ()
        assert skip_codes(result) == [(1, "PACK_IN_FLIGHT")]

    def test_a_packed_work_whose_path_changed_is_moved(self) -> None:
        work = candidate(
            1,
            pack_state=CONVERSION_STATE_COMPLETED,
            cbz_path="/library/old/书.cbz",
            library_relative_path="old/书.cbz",
        )
        result = plan(
            FakeDatabase([work]), FakeConversion(plans={1: "new/书.cbz"})
        )
        assert targets(result) == [(1, True, "new/书.cbz")]

    def test_a_packed_work_already_on_the_rules_path_is_out_of_scope(self) -> None:
        """Not even a skip: there is nothing to report and nothing to do."""
        work = candidate(
            1,
            pack_state=CONVERSION_STATE_COMPLETED,
            cbz_path="/library/新/书.cbz",
            library_relative_path="新/书.cbz",
        )
        result = plan(
            FakeDatabase([work]), FakeConversion(plans={1: "新/书.cbz"})
        )
        assert result.tasks == ()
        assert result.skipped == ()
        assert result.scanned == 1

    def test_a_manual_pin_is_never_recomputed(self) -> None:
        """「模板是默认值，默认值不推翻决定」 -- the standing policy."""
        work = candidate(
            1,
            pack_state=CONVERSION_STATE_COMPLETED,
            cbz_path="/library/亲手/命名.cbz",
            library_relative_path="亲手/命名.cbz",
            pinned_path="亲手/命名.cbz",
            pinned_is_manual=True,
        )
        conversion = FakeConversion(plans={1: "模板/命名.cbz"})
        result = plan(FakeDatabase([work]), conversion)

        assert result.tasks == ()
        assert skip_codes(result) == [(1, "PATH_MANUAL")]
        # Not even enriched: there is no path to derive, so a scrape would be an
        # HTTP call whose answer is discarded.
        assert conversion.enriched == []

    def test_an_unpacked_work_with_a_manual_pin_is_archived_to_that_pin(self) -> None:
        """It is not on the shelf yet, so it still has to be packed -- but the
        path it lands on is the operator's, so nothing is recomputed."""
        work = candidate(
            1, pinned_path="亲手/命名.cbz", pinned_is_manual=True
        )
        conversion = FakeConversion(plans={1: "模板/命名.cbz"})
        result = plan(FakeDatabase([work]), conversion)

        assert targets(result) == [(1, False, None)]
        assert conversion.planned == []

    def test_the_recorded_path_is_carried_for_the_report(self) -> None:
        work = candidate(
            1,
            pack_state=CONVERSION_STATE_COMPLETED,
            cbz_path="/library/old/书.cbz",
            library_relative_path="old/书.cbz",
        )
        result = plan(FakeDatabase([work]), FakeConversion(plans={1: "new/书.cbz"}))
        assert result.tasks[0].current == "old/书.cbz"


class TestForceScope:
    """强制 adds the books that are already correct, and overrides typed paths."""

    def test_a_packed_work_already_correct_is_reported_rather_than_hidden(self) -> None:
        work = candidate(
            1,
            pack_state=CONVERSION_STATE_COMPLETED,
            cbz_path="/library/新/书.cbz",
            library_relative_path="新/书.cbz",
        )
        result = plan(
            FakeDatabase([work]), FakeConversion(plans={1: "新/书.cbz"}), force=True
        )
        assert targets(result) == [(1, True, "新/书.cbz")]

    def test_a_manual_pin_is_recomputed_because_that_is_what_强制_means(self) -> None:
        work = candidate(
            1,
            pack_state=CONVERSION_STATE_COMPLETED,
            cbz_path="/library/亲手/命名.cbz",
            library_relative_path="亲手/命名.cbz",
            pinned_path="亲手/命名.cbz",
            pinned_is_manual=True,
        )
        result = plan(
            FakeDatabase([work]), FakeConversion(plans={1: "模板/命名.cbz"}), force=True
        )
        assert targets(result) == [(1, True, "模板/命名.cbz")]
        assert result.skipped == ()

    def test_force_still_queues_what_is_not_archived(self) -> None:
        result = plan(FakeDatabase([candidate(1)]), FakeConversion(), force=True)
        assert [t.packed for t in result.tasks] == [False]


class TestUnrenderablePaths:
    """A path the filesystem will not take parks the packing row, as the batch does."""

    def test_an_unpacked_work_is_parked_with_the_reason(self) -> None:
        archived = FakeArchived()
        result = plan(
            FakeDatabase([candidate(1)]),
            FakeConversion(unusable={1}),
            archived=archived,
        )

        assert result.tasks == ()
        assert archived.parked == [(1, "SEGMENT_TOO_LONG", "作品标题过长")]
        assert skip_codes(result) == [(1, "SEGMENT_TOO_LONG")]
        assert "过长" in result.skipped[0].message

    def test_a_packed_work_is_reported_without_a_park(self) -> None:
        """There is no packing row to park -- the book is already published."""
        archived = FakeArchived()
        work = candidate(
            1,
            pack_state=CONVERSION_STATE_COMPLETED,
            cbz_path="/library/书.cbz",
            library_relative_path="书.cbz",
        )
        result = plan(
            FakeDatabase([work]), FakeConversion(unusable={1}), archived=archived
        )

        assert archived.parked == []
        assert skip_codes(result) == [(1, "SEGMENT_TOO_LONG")]


class TestNoTitle:
    def test_an_unpacked_work_is_queued_without_a_pin(self) -> None:
        """Nothing to pin: the packer renders its own fallback inside the job."""
        result = plan(FakeDatabase([candidate(1, title=None)]), FakeConversion(no_title={1}))
        assert targets(result) == [(1, False, None)]

    def test_a_packed_work_is_skipped_rather_than_moved_to_a_placeholder(self) -> None:
        work = candidate(
            1,
            title=None,
            pack_state=CONVERSION_STATE_COMPLETED,
            cbz_path="/library/书.cbz",
            library_relative_path="书.cbz",
        )
        result = plan(FakeDatabase([work]), FakeConversion(no_title={1}))
        assert result.tasks == ()
        assert skip_codes(result) == [(1, "NO_TITLE")]


def run(plan_result, archived, conversion, *, operator_name="admin"):
    return asyncio.run(
        apply_rearchive(
            plan_result, archived, conversion, operator_name=operator_name
        )
    )


class TestExecution:
    def test_an_unpacked_work_is_pinned_before_it_is_queued(self) -> None:
        conversion = FakeConversion(plans={1: "作者/书.cbz"})
        archived = FakeArchived()
        result = plan(FakeDatabase([candidate(1)]), conversion)

        outcome = run(result, archived, conversion)

        assert archived.pinned == [(1, "作者/书.cbz")]
        assert conversion.enqueued == [1]
        assert [entry["candidate_id"] for entry in outcome["queued"]] == [1]
        assert outcome["moved"] == ()

    def test_a_work_with_nothing_to_pin_is_queued_without_a_pin(self) -> None:
        conversion = FakeConversion(no_title={1})
        archived = FakeArchived()
        result = plan(FakeDatabase([candidate(1, title=None)]), conversion)

        run(result, archived, conversion)

        assert archived.pinned == []
        assert conversion.enqueued == [1]

    def test_a_packed_work_is_moved_never_repacked(self) -> None:
        """The instruction that keeps a full re-file cheap: no packer involved."""
        work = candidate(
            1,
            pack_state=CONVERSION_STATE_COMPLETED,
            cbz_path="/library/old/书.cbz",
            library_relative_path="old/书.cbz",
        )
        conversion = FakeConversion(plans={1: "new/书.cbz"})
        archived = FakeArchived()
        result = plan(FakeDatabase([work]), conversion)

        outcome = run(result, archived, conversion)

        assert archived.refiled == [(1, "new/书.cbz")]
        assert conversion.enqueued == []
        assert outcome["moved"] == (
            {
                "candidate_id": 1,
                "title": "作品 1",
                "from": "old/书.cbz",
                "to": "new/书.cbz",
            },
        )

    def test_a_强制_run_counts_the_books_that_did_not_have_to_move(self) -> None:
        work = candidate(
            1,
            pack_state=CONVERSION_STATE_COMPLETED,
            cbz_path="/library/新/书.cbz",
            library_relative_path="新/书.cbz",
        )
        conversion = FakeConversion(plans={1: "新/书.cbz"})
        archived = FakeArchived(unchanged={1})
        result = plan(FakeDatabase([work]), conversion, force=True)

        outcome = run(result, archived, conversion)

        assert outcome["moved"] == ()
        assert [entry["candidate_id"] for entry in outcome["unchanged"]] == [1]

    def test_a_refusal_is_per_work_and_does_not_abandon_the_rest(self) -> None:
        conversion = FakeConversion(
            plans={2: "作者/书 2.cbz"},
            refusals={2: LibraryPathError("PATH_TAKEN_ON_DISK", "目标位置已有同名文件")},
        )
        archived = FakeArchived()
        result = plan(FakeDatabase([candidate(1), candidate(2)]), conversion)

        outcome = run(result, archived, conversion)

        assert [entry["candidate_id"] for entry in outcome["queued"]] == [1]
        assert outcome["skipped"] == (
            {
                "candidate_id": 2,
                "title": "作品 1",
                "code": "PATH_TAKEN_ON_DISK",
                "message": "目标位置已有同名文件",
            },
        )

    def test_a_broken_filesystem_surfaces_rather_than_reads_as_a_skip(self) -> None:
        """「99 已重归档，1 跳过」 must never hide a real fault."""
        work = candidate(
            1,
            pack_state=CONVERSION_STATE_COMPLETED,
            cbz_path="/library/old/书.cbz",
            library_relative_path="old/书.cbz",
        )
        conversion = FakeConversion(plans={1: "new/书.cbz"})
        archived = FakeArchived(refusals={1: RuntimeError("disk on fire")})
        result = plan(FakeDatabase([work]), conversion)

        with pytest.raises(RuntimeError):
            run(result, archived, conversion)

    def test_the_plan_skips_are_reported_first(self) -> None:
        work = candidate(
            1,
            pack_state=CONVERSION_STATE_COMPLETED,
            cbz_path="/library/亲手/命名.cbz",
            library_relative_path="亲手/命名.cbz",
            pinned_path="亲手/命名.cbz",
            pinned_is_manual=True,
        )
        conversion = FakeConversion()
        archived = FakeArchived()
        result = plan(FakeDatabase([work]), conversion)

        outcome = run(result, archived, conversion)

        assert [entry["code"] for entry in outcome["skipped"]] == ["PATH_MANUAL"]

    def test_an_unarchived_work_whose_manual_pin_skips_the_recompute(self) -> None:
        """The pack is enqueued with the operator's path still pinned."""
        work = candidate(1, pinned_path="亲手/命名.cbz", pinned_is_manual=True)
        conversion = FakeConversion()
        archived = FakeArchived()
        result = plan(FakeDatabase([work]), conversion)

        run(result, archived, conversion)

        assert archived.pinned == []
        assert conversion.enqueued == [1]


class TestReArchiveWorks:
    def test_it_plans_and_runs_in_one_call(self) -> None:
        work = candidate(
            1,
            pack_state=CONVERSION_STATE_COMPLETED,
            cbz_path="/library/old/书.cbz",
            library_relative_path="old/书.cbz",
        )
        conversion = FakeConversion(plans={1: "new/书.cbz"})
        archived = FakeArchived()

        outcome = asyncio.run(
            rearchive_works(
                FakeDatabase([work]), archived, conversion, operator_name="alice"
            )
        )

        assert outcome["scanned"] == 1
        assert len(outcome["moved"]) == 1
        assert outcome["force"] is False

class FakeAiConversion(FakeConversion):
    """The same collaborator with `path_source = ai` and a scripted cache.

    `cached` is what `current_ai_path` answers: a `PurePosixPath` for a book with
    a current answer, absent for one that would need a model call. The plan
    phase must never do more than read this, so `planned_path_for_candidate` is
    inherited unchanged and its call log is what proves it was not reached.
    """

    def __init__(self, *, cached=None, sweep=None, **kwargs) -> None:
        super().__init__(**kwargs)
        self.cached = dict(cached or {})
        self.cache_lookups: list[int] = []
        self.sweep = {
            "batch_size": 20,
            "concurrency": 2,
            "include_current": False,
            **(sweep or {}),
        }
        # High-water mark of works enriching at once, so a test can prove the
        # concurrency setting is a bound rather than a comment.
        self.active = 0
        self.max_active = 0

    async def path_source(self) -> str:
        return "ai"

    async def ai_sweep_settings(self):
        return dict(self.sweep)

    async def current_ai_path(self, candidate_id: int, metadata):
        self.cache_lookups.append(candidate_id)
        return self.cached.get(candidate_id)

    async def ensure_metadata(self, candidate_id: int) -> None:
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        try:
            await asyncio.sleep(0)
            await super().ensure_metadata(candidate_id)
        finally:
            self.active -= 1


class TestAiModeScope:
    """AI 模式下默认范围：未归档的入队、缓存已变的移动、其余交给任务.

    The model is never called from the plan phase (§9), so everything here is
    about which of four things happens to a book: queued, moved, queued as a
    path-only re-file, or skipped with a reason an operator can act on.
    """

    def test_an_unarchived_work_is_queued_without_pinning_when_there_is_no_cache(
        self,
    ) -> None:
        conversion = FakeAiConversion()
        result = plan(FakeDatabase([candidate(1)]), conversion)
        assert modes(result) == [(1, "queue", None)]
        # No template planning happened: AI mode replaces it entirely.
        assert conversion.planned == []
        assert conversion.enqueued == []

    def test_a_current_cache_is_pinned_for_an_unarchived_work(self) -> None:
        result = plan(
            FakeDatabase([candidate(1)]),
            FakeAiConversion(cached={1: PurePosixPath("同人志/作者/书.cbz")}),
        )
        assert modes(result) == [(1, "queue", "同人志/作者/书.cbz")]

    def test_a_packed_work_moves_onto_a_changed_current_cache(self) -> None:
        result = plan(
            FakeDatabase(
                [
                    candidate(
                        2,
                        cbz_path="/library/旧/书.cbz",
                        library_relative_path="旧/书.cbz",
                    )
                ]
            ),
            FakeAiConversion(cached={2: PurePosixPath("新/书.cbz")}),
        )
        assert modes(result) == [(2, "move", "新/书.cbz")]

    def test_a_packed_book_already_at_the_cached_path_is_out_of_scope(self) -> None:
        """The §4 predicate: re-asking would return the same answer.

        Out of scope rather than a reported skip: on a thousand-book library the
        「已是最新」 rows would bury the handful worth acting on. The
        `include_current` switch is how an operator asks to see them.
        """
        result = plan(
            FakeDatabase(
                [
                    candidate(
                        2,
                        cbz_path="/library/同人志/书.cbz",
                        library_relative_path="同人志/书.cbz",
                    )
                ]
            ),
            FakeAiConversion(cached={2: PurePosixPath("同人志/书.cbz")}),
        )
        assert result.tasks == ()
        assert result.skipped == ()

    def test_include_current_turns_that_same_book_into_a_re_ask(self) -> None:
        """The switch is the 「让 AI 再给同一本书起个名字」 entry.

        `refresh` is set because the point is a *new* answer: the cache the
        fingerprint would accept is exactly what the operator is asking to
        bypass.
        """
        conversion = FakeAiConversion(
            cached={2: PurePosixPath("同人志/书.cbz")},
            sweep={"include_current": True},
        )
        result = plan(
            FakeDatabase(
                [
                    candidate(
                        2,
                        cbz_path="/library/同人志/书.cbz",
                        library_relative_path="同人志/书.cbz",
                    )
                ]
            ),
            conversion,
        )
        assert modes(result) == [(2, "refile", None)]
        assert result.tasks[0].refresh is True

    def test_include_current_does_not_re_ask_a_book_the_cache_already_moved(
        self,
    ) -> None:
        """A known-different answer is moved, not re-asked.

        Turning the switch on says 「把已是最新的也算进来」, not 「把已经知道答案的
        问题再问一遍」 -- the move is free and the answer is already in hand.
        """
        result = plan(
            FakeDatabase(
                [
                    candidate(
                        2,
                        cbz_path="/library/旧/书.cbz",
                        library_relative_path="旧/书.cbz",
                    )
                ]
            ),
            FakeAiConversion(
                cached={2: PurePosixPath("新/书.cbz")},
                sweep={"include_current": True},
            ),
        )
        assert modes(result) == [(2, "move", "新/书.cbz")]

    def test_a_packed_book_without_a_current_cache_becomes_a_refile_job(
        self,
    ) -> None:
        """The R29 gap, closed: a stale answer no longer skips the book.

        The job it is queued for asks the model and moves the CBZ. It never
        repacks -- that is `refile`, not `queue`.
        """
        archived = FakeArchived()
        conversion = FakeAiConversion()
        plan_result = plan(
            FakeDatabase(
                [
                    candidate(
                        2,
                        cbz_path="/library/旧/书.cbz",
                        library_relative_path="旧/书.cbz",
                    )
                ]
            ),
            conversion,
            archived=archived,
        )
        assert modes(plan_result) == [(2, "refile", None)]
        # No cache to refresh: the fingerprint check will make the job ask.
        assert plan_result.tasks[0].refresh is False
        archived_after = run(plan_result, archived, conversion)
        assert conversion.enqueue_calls == [(2, True, False)]
        assert len(archived_after["refiling"]) == 1
        assert archived.refiled == []
        assert archived.parked == []

    def test_force_re_asks_an_already_current_book_instead_of_moving_it(
        self,
    ) -> None:
        archived = FakeArchived()
        conversion = FakeAiConversion(cached={2: PurePosixPath("同人志/书.cbz")})
        plan_result = plan(
            FakeDatabase(
                [
                    candidate(
                        2,
                        cbz_path="/library/同人志/书.cbz",
                        library_relative_path="同人志/书.cbz",
                    )
                ]
            ),
            conversion,
            force=True,
            archived=archived,
        )
        assert modes(plan_result) == [(2, "refile", None)]
        assert plan_result.tasks[0].refresh is True
        outcome = run(plan_result, archived, conversion)
        # 强制 asks again rather than moving the answer it already has; the job
        # is what decides whether a move is needed.
        assert conversion.enqueue_calls == [(2, True, True)]
        assert outcome["moved"] == ()
        assert len(outcome["refiling"]) == 1

    def test_force_overrides_a_manual_pin_without_a_skip(self) -> None:
        conversion = FakeAiConversion(cached={2: PurePosixPath("同人志/书.cbz")})
        result = plan(
            FakeDatabase(
                [
                    candidate(
                        2,
                        cbz_path="/library/手动/书.cbz",
                        library_relative_path="手动/书.cbz",
                        pinned_path="手动/书.cbz",
                        pinned_is_manual=True,
                    )
                ]
            ),
            conversion,
            force=True,
        )
        assert skip_codes(result) == []
        assert modes(result) == [(2, "refile", None)]

    def test_a_manual_pin_is_respected_without_force(self) -> None:
        result = plan(
            FakeDatabase(
                [
                    candidate(
                        2,
                        cbz_path="/library/手动/书.cbz",
                        library_relative_path="手动/书.cbz",
                        pinned_path="手动/书.cbz",
                        pinned_is_manual=True,
                    )
                ]
            ),
            FakeAiConversion(cached={2: PurePosixPath("同人志/书.cbz")}),
        )
        assert skip_codes(result) == [(2, "PATH_MANUAL")]

    def test_an_unpacked_book_in_flight_is_left_alone(self) -> None:
        archived = FakeArchived()
        result = plan(
            FakeDatabase(
                [candidate(1, pack_state=CONVERSION_STATE_PENDING)]
            ),
            FakeAiConversion(),
            archived=archived,
        )
        assert skip_codes(result) == [(1, "PACK_IN_FLIGHT")]
        assert archived.parked == []

    def test_a_packed_book_whose_re_file_is_already_queued_is_left_alone(
        self,
    ) -> None:
        """A second job for the same book would be a race for no gain."""
        result = plan(
            FakeDatabase(
                [
                    candidate(
                        2,
                        pack_state=CONVERSION_STATE_PENDING,
                        cbz_path="/library/旧/书.cbz",
                        library_relative_path="旧/书.cbz",
                    )
                ]
            ),
            FakeAiConversion(),
            force=True,
        )
        assert skip_codes(result) == [(2, "PACK_IN_FLIGHT")]


class TestAiForceScope:
    """强制 = 全库重新询问, including the books a default run leaves alone."""

    def test_an_unarchived_work_is_queued_with_a_refresh_and_no_pin(self) -> None:
        archived = FakeArchived()
        conversion = FakeAiConversion(cached={1: PurePosixPath("旧/书.cbz")})
        plan_result = plan(
            FakeDatabase([candidate(1)]),
            conversion,
            force=True,
            archived=archived,
        )
        # The cached answer is not pinned: a pin would pre-empt the question.
        assert modes(plan_result) == [(1, "queue", None)]
        assert plan_result.tasks[0].refresh is True
        run(plan_result, archived, conversion)
        assert conversion.enqueue_calls == [(1, False, True)]
        assert archived.pinned == []

    def test_a_manual_pin_on_an_unpacked_work_is_dropped_before_queueing(
        self,
    ) -> None:
        archived = FakeArchived()
        conversion = FakeAiConversion()
        plan_result = plan(
            FakeDatabase(
                [candidate(1, pinned_path="手动/书.cbz", pinned_is_manual=True)]
            ),
            conversion,
            force=True,
            archived=archived,
        )
        assert plan_result.tasks[0].clear_pin is True
        run(plan_result, archived, conversion)
        assert archived.cleared == [1]
        assert archived.pinned == []

    def test_force_ignores_include_current_and_covers_the_whole_library(
        self,
    ) -> None:
        conversion = FakeAiConversion(
            cached={
                2: PurePosixPath("同人志/书.cbz"),
                3: PurePosixPath("同人志/书2.cbz"),
            },
            sweep={"include_current": False},
        )
        result = plan(
            FakeDatabase(
                [
                    candidate(
                        2,
                        cbz_path="/library/同人志/书.cbz",
                        library_relative_path="同人志/书.cbz",
                    ),
                    candidate(
                        3,
                        cbz_path="/library/别处/书2.cbz",
                        library_relative_path="别处/书2.cbz",
                    ),
                ]
            ),
            conversion,
            force=True,
        )
        assert [item[0] for item in modes(result)] == [2, 3]
        assert all(item[1] == "refile" for item in modes(result))


class TestAiSweepSettings:
    """批量与并发只影响遍历方式，不改变一网打尽的范围."""

    def test_the_whole_library_is_planned_however_small_the_batch(self) -> None:
        conversion = FakeAiConversion(sweep={"batch_size": 1, "concurrency": 1})
        result = plan(
            FakeDatabase([candidate(1), candidate(2), candidate(3)]),
            conversion,
        )
        assert conversion.cache_lookups == [1, 2, 3]
        assert [item[0] for item in modes(result)] == [1, 2, 3]

    def test_concurrency_bounds_how_many_enrich_at_once(self) -> None:
        conversion = FakeAiConversion(sweep={"batch_size": 4, "concurrency": 2})
        plan(FakeDatabase([candidate(i) for i in range(1, 5)]), conversion)
        assert conversion.max_active == 2

    def test_one_book_per_enrichment_when_concurrency_is_one(self) -> None:
        conversion = FakeAiConversion(sweep={"batch_size": 3, "concurrency": 1})
        plan(FakeDatabase([candidate(i) for i in range(1, 4)]), conversion)
        assert conversion.max_active == 1


class TestDryRun:
    """试跑 plans and reports; it must not move, queue, park or ask anything."""

    def test_an_ai_sweep_changes_nothing(self) -> None:
        archived = FakeArchived()
        conversion = FakeAiConversion(cached={2: PurePosixPath("新/书.cbz")})
        outcome = run_sweep(
            FakeDatabase(
                [
                    candidate(1),
                    candidate(
                        2,
                        cbz_path="/library/旧/书.cbz",
                        library_relative_path="旧/书.cbz",
                    ),
                ]
            ),
            conversion,
            dry_run=True,
            archived=archived,
        )
        assert outcome["dry_run"] is True
        assert [item["candidate_id"] for item in outcome["queued"]] == [1]
        assert [item["candidate_id"] for item in outcome["moved"]] == [2]
        assert outcome["moved"][0]["from"] == "旧/书.cbz"
        assert outcome["moved"][0]["to"] == "新/书.cbz"
        # Nothing was done: no enqueue, no move, no pin.
        assert conversion.enqueued == []
        assert archived.refiled == []
        assert archived.pinned == []

    def test_a_unusable_path_is_reported_without_parking(self) -> None:
        archived = FakeArchived()
        outcome = run_sweep(
            FakeDatabase([candidate(1)]),
            FakeConversion(unusable={1}),
            dry_run=True,
            archived=archived,
        )
        assert archived.parked == []
        assert [item["code"] for item in outcome["skipped"]] == ["SEGMENT_TOO_LONG"]

    def test_a_real_run_still_parks_an_unusable_path(self) -> None:
        archived = FakeArchived()
        run_sweep(
            FakeDatabase([candidate(1)]),
            FakeConversion(unusable={1}),
            archived=archived,
        )
        assert len(archived.parked) == 1
