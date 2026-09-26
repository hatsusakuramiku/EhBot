"""一键重新归档: put every book where the current decision says it belongs.

Why this exists. A layout template (and the routing rules above it) is a setting
an operator changes, and every book already in the library was filed under
whatever it said at the time. Changing the template therefore used to reach a
book only if someone happened to press 重新打包 on it -- the packer prefers the
pin the last pack wrote, so a book nobody touched kept its old path forever.
`/downloaded`'s batch repack closed that for a *selection*; this closes it for
the library, from the page that owns the setting. AI mode gives the same button
a second meaning: the decision being applied is the model's, with the answer
cache standing in for the rules (proposal §9).

The two entry points are one button apart:

* **默认** re-archives the works that are not on the current decision yet -- an
  archive that never completed (never packed, failed, or parked waiting for a
  volume or a password), a book whose recomputed path differs from the recorded
  one, and (AI mode) a book whose cached answer is missing or stale.
* **强制** adds the rest: every other book that already archived, including the
  ones whose path an operator typed by hand. That last part is the whole
  difference -- 「模板不得覆盖人工决定」 is the standing policy, and 强制 is the
  operator saying「这次覆盖」. In AI mode 强制 goes further and *re-asks*: the
  model is asked again for every archived book, cache or no cache (proposal §9,
  「所有作品一律按当前 prompt 重新询问」).

One rule shapes every decision below: **a book that is already packed is moved,
never repacked.** Its CBZ on disk is this book's content; re-deriving the same
pages to reach a new *name* would burn the host's CPU for bytes that do not
change, which is the host pressure the instruction asks us to avoid. So a packed
work goes through `ArchivedWorkService.refile_work` (a file move plus a record)
or, when a fresh answer is needed, through a path-only job that asks the model
and then calls the same method -- and only a work with no CBZ is queued for
packing.

A third entry point: **试跑** (`dry_run`). It stops after the plan and reports it
in the same shape a real run would, because 强制 in AI mode is one press away
from a whole-library model spend and the operator should be able to read the list
first.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

from app.archive.service import PATH_SOURCE_AI
from app.conversion.naming import LibraryPathError, LibraryTemplateError
from app.downloads.models import (
    CONVERSION_STATE_PENDING,
    CONVERSION_STATE_RUNNING,
)


#: Packing states that mean 「this book already has a task in flight」. Neither is
#: 「尚未成功完成归档」 in a sense the sweep can act on: requeueing a pending task
#: changes nothing, and re-pinning one while the worker is about to read the pin
#: is a race for no gain. WAITING_VOLUMES / WAITING_PASSWORD are deliberately
#: absent -- those are parked asks, and pressing the button after supplying the
#: volume or the password is exactly how they get retried.
_IN_FLIGHT_STATES: frozenset[str] = frozenset(
    {CONVERSION_STATE_PENDING, CONVERSION_STATE_RUNNING}
)


@dataclass(frozen=True, slots=True)
class ReArchiveTask:
    """One work the sweep will act on, and what the actor should do with it.

    `mode` is the whole of the R27 rule 「已打包不再重新打包」, spelled out:

    * `queue` -- no CBZ yet, so a packing task is enqueued. `target` is the path
      to pin first when one is known (an AI cache hit, or a rule's answer);
      None means the job renders its own path inside the job, which is where the
      `candidate-<id>` fallback belongs.
    * `move` -- packed, and the current answer is already known to differ. The
      file is moved now, in this request: no job, no model call.
    * `refile` -- packed, and a *new* answer is needed (the cache is missing or
      stale, or the operator asked for a re-ask). Enqueued as a path-only job:
      the packing worker asks the model and moves the existing CBZ.

    `refresh` is 「问一遍，即使缓存已经能回答」. Only 强制 and the
    「让 AI 再给同一本书起个名字」 switch set it, and only because the
    instruction is 「重新询问」 rather than 「按缓存执行」. A `refile` whose cache
    is simply missing or stale does not need it: the fingerprint check already
    sends it to the model.

    `clear_pin` drops an operator's typed path *without* a move, which is the
    case 强制 reaches for a work that has not been packed yet: its path is still
    a decision rather than a file, so there is nothing to move and everything to
    un-decide.
    """

    candidate_id: int
    title: str | None
    mode: str
    target: str | None
    #: Where the book is recorded now, for the report's 「从 … 到 …」. None when
    #: nothing was ever recorded.
    current: str | None
    refresh: bool = False
    clear_pin: bool = False

    @property
    def packed(self) -> bool:
        """True when a CBZ exists, i.e. the book is moved and never repacked."""
        return self.mode != "queue"


@dataclass(frozen=True, slots=True)
class ReArchiveSkip:
    """A work the sweep decided not to touch, and why.

    A reason per work rather than a count: 「共跳过 3 件」 tells an operator
    nothing about which book they still have to deal with by hand.
    """

    candidate_id: int
    title: str | None
    code: str
    message: str


@dataclass(frozen=True, slots=True)
class ReArchivePlan:
    force: bool
    scanned: int
    tasks: tuple[ReArchiveTask, ...]
    skipped: tuple[ReArchiveSkip, ...]
    dry_run: bool = False


def _refusal(exc: Exception) -> tuple[str, str]:
    """The `(code, message)` pair of a path refusal.

    `LibraryPathError` and `LibraryTemplateError` both carry a stable code and a
    sentence written for the operator, and both mean the same thing here: this
    book cannot be given a path right now.
    """
    code = getattr(exc, "code", None) or "PATH_UNUSABLE"
    message = getattr(exc, "public_message", None) or str(exc)
    return str(code), str(message)


async def plan_rearchive(
    database,
    archived,
    conversion,
    *,
    force: bool = False,
    dry_run: bool = False,
):
    """Decide which works to re-archive, and where each one goes.

    Two planners, one per `path_source`, because the two answer 「这本书放哪」
    differently and a shared loop would have to branch on every other line.
    Neither calls a model (§9): the AI planner reads the answer cache, and a book
    that needs a new answer becomes a job that asks for one.

    The one write a *plan* is allowed: an unarchived work whose path cannot
    render is parked in `CONVERSION_WAITING_PATH` (through `archived`) so the
    reason survives the page load that reported it. Everything else here reads,
    and `dry_run` suppresses even that one -- 试跑 must be safe to press.
    """
    candidates = await database.list_rearchive_candidates()
    if await conversion.path_source() == PATH_SOURCE_AI:
        tasks, skipped = await _plan_ai_library(
            conversion, candidates, force=force
        )
    else:
        tasks, skipped = await _plan_template_library(
            archived, conversion, candidates, force=force, dry_run=dry_run
        )
    return ReArchivePlan(
        force=force,
        scanned=len(candidates),
        tasks=tuple(tasks),
        skipped=tuple(skipped),
        dry_run=dry_run,
    )


async def _plan_template_library(
    archived, conversion, candidates, *, force, dry_run
):
    """The R27 sweep: recompute each book's path from the template and rules."""
    tasks: list[ReArchiveTask] = []
    skipped: list[ReArchiveSkip] = []
    for candidate in candidates:
        outcome = await _plan_template_candidate(
            archived, conversion, candidate, force=force, dry_run=dry_run
        )
        if isinstance(outcome, ReArchiveSkip):
            skipped.append(outcome)
        elif outcome is not None:
            tasks.append(outcome)
    return tasks, skipped


async def _plan_template_candidate(
    archived, conversion, candidate, *, force, dry_run
) -> ReArchiveTask | ReArchiveSkip | None:
    """One work under template mode: a task, a skip with a reason, or nothing.

    Metadata is enriched per work before its path is derived, for the reason the
    batch repack does it: a path computed while the gallery is unread pins a
    fallback name, and a pin outlives the fetch that would have corrected it. The
    enricher is a no-op when the work already has its metadata, so the cost lands
    only on the books that would otherwise be filed under a placeholder.
    """
    packed = candidate.is_packaged
    manual = candidate.pinned_is_manual and bool(candidate.pinned_path)
    if packed and manual and not force:
        # A template is a default and a default does not overwrite a
        # decision. 强制 is the operator overriding that for this run.
        return ReArchiveSkip(
            candidate.candidate_id,
            candidate.title,
            "PATH_MANUAL",
            "归档路径是手动指定的，本次不重算（需要重算请用强制重新归档）",
        )
    if not packed and candidate.pack_state in _IN_FLIGHT_STATES:
        return ReArchiveSkip(
            candidate.candidate_id,
            candidate.title,
            "PACK_IN_FLIGHT",
            "该作品正在打包或已排队，本次不动它",
        )
    if not packed and manual:
        # Still has to be archived -- it is not on the shelf yet -- but the
        # path it will land on is the operator's, so nothing is recomputed
        # and nothing is pinned.
        return ReArchiveTask(
            candidate.candidate_id,
            candidate.title,
            mode="queue",
            target=None,
            current=candidate.relative_path,
        )
    await conversion.ensure_metadata(candidate.candidate_id)
    try:
        planned = await conversion.planned_path_for_candidate(
            candidate.candidate_id
        )
    except (LibraryPathError, LibraryTemplateError) as exc:
        code, message = _refusal(exc)
        if not packed and not dry_run:
            await archived.park_for_invalid_path(
                candidate.candidate_id, code, message
            )
        return ReArchiveSkip(candidate.candidate_id, candidate.title, code, message)
    if planned is None:
        if packed:
            # A packed book with no title at all: there is no name to move it
            # to, and leaving it where it is beats moving it to a placeholder.
            return ReArchiveSkip(
                candidate.candidate_id,
                candidate.title,
                "NO_TITLE",
                "没有标题元数据，无法按模板重算路径",
            )
        # Nothing to pin. The packer enriches and renders its own path inside
        # the job, which is where a fallback name belongs.
        return ReArchiveTask(
            candidate.candidate_id,
            candidate.title,
            mode="queue",
            target=None,
            current=candidate.relative_path,
        )
    target = planned.as_posix()
    if packed:
        if not force and target == candidate.relative_path:
            # Already where the rules put it. The default run has nothing to
            # say about it, so it is not even a skip -- it is out of scope.
            return None
        return ReArchiveTask(
            candidate.candidate_id,
            candidate.title,
            mode="move",
            target=target,
            current=candidate.relative_path,
        )
    return ReArchiveTask(
        candidate.candidate_id,
        candidate.title,
        mode="queue",
        target=target,
        current=candidate.relative_path,
    )


async def _plan_ai_library(conversion, candidates, *, force):
    """The AI sweep: cache first, and a job for every book that needs an answer.

    Three settings shape the loop, and each one is honouring something the
    operator asked for rather than a performance guess:

    * `batch_size` -- how many works are prepared before moving on to the next
      set. One run still covers the whole in-scope library; the batching is what
      keeps a five-thousand-book sweep from reading every metadata row into
      memory at once, and it is why the loop yields between sets.
    * `concurrency` -- how many of those may be fetching gallery metadata at the
      same time. That fetch is the only network work a *plan* may do; the model
      is asked later, inside a job, where one worker owns the rate.
    * `include_current` -- whether 「已按当前提示词定好路径」 is in the default
      scope. It is the switch that turns 「重新归档」 into 「让 AI 再给同一本书起个
      名字」. 强制 ignores it: 强制 already re-asks everything.
    """
    settings = await conversion.ai_sweep_settings()
    concurrency = max(1, int(settings.get("concurrency") or 1))
    batch_size = max(1, int(settings.get("batch_size") or 1))
    include_current = bool(settings.get("include_current")) and not force
    tasks: list[ReArchiveTask] = []
    skipped: list[ReArchiveSkip] = []
    for start in range(0, len(candidates), batch_size):
        chunk = candidates[start : start + batch_size]
        outcomes = await _plan_ai_chunk(
            conversion,
            chunk,
            force=force,
            include_current=include_current,
            concurrency=concurrency,
        )
        for outcome in outcomes:
            if isinstance(outcome, ReArchiveSkip):
                skipped.append(outcome)
            elif outcome is not None:
                tasks.append(outcome)
    return tasks, skipped


async def _plan_ai_chunk(
    conversion, chunk, *, force, include_current, concurrency
):
    """Plan one set of works, enriching at most `concurrency` of them at once.

    A semaphore rather than `asyncio.gather` over the whole library: the
    interesting resource here is the gallery fetch, and a plan that opened two
    thousand of them at once would be the host pressure the instruction asks us
    to avoid. Order is preserved by `gather`, so the report reads in candidate
    order however the fetches interleave.
    """
    semaphore = asyncio.Semaphore(concurrency)

    async def plan_one(candidate):
        async with semaphore:
            return await _plan_ai_candidate(
                conversion,
                candidate,
                force=force,
                include_current=include_current,
            )

    return await asyncio.gather(*(plan_one(item) for item in chunk))


async def _plan_ai_candidate(
    conversion, candidate, *, force, include_current
) -> ReArchiveTask | ReArchiveSkip | None:
    """One work under AI mode: a task, a skip with a reason, or nothing to do.

    `None` means 「out of scope」, the same thing the template branch's bare
    `return None` means: a book that is already where the model put it, and that
    the operator did not ask to have re-asked. It is not reported as a skip --
    on a library of a thousand books, a thousand 「已是最新」 rows would bury the
    handful the operator can act on. Turning on `include_current` is how they ask
    to see those, and then they appear as work rather than as an excuse.
    """
    packed = candidate.is_packaged
    manual = candidate.pinned_is_manual and bool(candidate.pinned_path)
    if candidate.pack_state in _IN_FLIGHT_STATES:
        # Covers both halves: an unarchived book being packed (nothing to add)
        # and a packed book whose re-file is already queued (a second job would
        # be a race for no gain).
        return ReArchiveSkip(
            candidate.candidate_id,
            candidate.title,
            "PACK_IN_FLIGHT",
            "该作品已有打包或重算任务在排队或执行中，本次不动它",
        )
    if packed and manual and not force:
        # 「模板是默认值，默认值不推翻决定」 applies to the model's answer too.
        return ReArchiveSkip(
            candidate.candidate_id,
            candidate.title,
            "PATH_MANUAL",
            "归档路径是手动指定的，本次不重算（需要覆盖请用强制重新归档）",
        )
    await conversion.ensure_metadata(candidate.candidate_id)
    metadata = await conversion.metadata_for(candidate.candidate_id)
    # Cache only: this is a plan, and a plan that asked a model would make
    # pressing the button a whole-library spend with no preview. What the cache
    # *is* decides which of the three things happens below.
    cached = await conversion.current_ai_path(candidate.candidate_id, metadata)
    if not packed:
        # Nothing on the shelf yet, so the book has to be queued whatever the
        # cache says. A current answer is pinned so the pack lands exactly there;
        # without one the job asks. 强制 drops the pin instead, because 「重新询问」
        # is the instruction and a pin would pre-empt the question.
        if manual and force:
            return ReArchiveTask(
                candidate.candidate_id,
                candidate.title,
                mode="queue",
                target=None,
                current=candidate.relative_path,
                refresh=True,
                clear_pin=True,
            )
        if force:
            return ReArchiveTask(
                candidate.candidate_id,
                candidate.title,
                mode="queue",
                target=None,
                current=candidate.relative_path,
                refresh=True,
            )
        return ReArchiveTask(
            candidate.candidate_id,
            candidate.title,
            mode="queue",
            target=cached.as_posix() if cached is not None else None,
            current=candidate.relative_path,
        )
    if force:
        # 全库重新询问. Already packed, so it is the path-only job: the model is
        # asked again and the existing CBZ is moved to whatever it answers.
        return ReArchiveTask(
            candidate.candidate_id,
            candidate.title,
            mode="refile",
            target=None,
            current=candidate.relative_path,
            refresh=True,
        )
    if cached is None:
        # Missing or stale: the model owes this book an answer, and the job that
        # asks is also the job that moves the file.
        return ReArchiveTask(
            candidate.candidate_id,
            candidate.title,
            mode="refile",
            target=None,
            current=candidate.relative_path,
        )
    target = cached.as_posix()
    if target != candidate.relative_path:
        # The answer is already in hand and differs from the record: move now,
        # in this request. No model call, no job.
        return ReArchiveTask(
            candidate.candidate_id,
            candidate.title,
            mode="move",
            target=target,
            current=candidate.relative_path,
        )
    if include_current:
        # §4's predicate holds -- re-asking would return the same answer -- and
        # the operator asked anyway, which is the 「换个名字」 entry.
        return ReArchiveTask(
            candidate.candidate_id,
            candidate.title,
            mode="refile",
            target=None,
            current=candidate.relative_path,
            refresh=True,
        )
    return None


async def apply_rearchive(
    plan: ReArchivePlan,
    archived,
    conversion,
    *,
    operator_name: str = "admin",
) -> dict:
    """Run a plan and report what each work became.

    Four outcomes per work, and the distinction between the last three is the
    point of the report:

    * **queued** -- no CBZ yet, so a packing task was enqueued (after pinning the
      path the current rules gave it, when there was one to pin).
    * **moved** -- packed, and the file really changed location.
    * **unchanged** -- packed, and the rules' path is the one it already has. A
      强制 run reports these rather than hiding them: 「所有作品都处理过」 is only
      true if the ones with nothing to do are visible.
    * **skipped** -- refused, with the reason the service gave.

    A refusal is per work. One book whose move fails must not abandon the other
    ninety-nine, which is also what makes a replayed run safe: every work is
    idempotent on its own.
    """
    queued: list[dict] = []
    refiling: list[dict] = []
    moved: list[dict] = []
    unchanged: list[dict] = []
    skipped: list[dict] = [
        {
            "candidate_id": entry.candidate_id,
            "title": entry.title,
            "code": entry.code,
            "message": entry.message,
        }
        for entry in plan.skipped
    ]
    for task in plan.tasks:
        try:
            if task.mode == "move":
                result = await archived.refile_work(
                    task.candidate_id,
                    task.target,
                    operator_name=operator_name,
                )
            elif task.mode == "refile":
                await conversion.enqueue_for_candidate(
                    task.candidate_id,
                    refile=True,
                    refresh_ai_path=task.refresh,
                )
                refiling.append(
                    {
                        "candidate_id": task.candidate_id,
                        "title": task.title,
                        "from": task.current,
                    }
                )
                continue
            else:
                if task.clear_pin:
                    # Before the pin below, and before the job can read it.
                    await archived.clear_path_pin(task.candidate_id)
                if task.target is not None:
                    await archived.pin_computed_path(
                        task.candidate_id,
                        task.target,
                        operator_name=operator_name,
                    )
                await conversion.enqueue_for_candidate(
                    task.candidate_id,
                    refresh_ai_path=task.refresh,
                )
                result = None
        except Exception as exc:  # noqa: BLE001 - re-raised when unexpected
            refusal = _translate(exc)
            if refusal is None:
                raise
            code, message = refusal
            skipped.append(
                {
                    "candidate_id": task.candidate_id,
                    "title": task.title,
                    "code": code,
                    "message": message,
                }
            )
            continue
        if result is None:
            queued.append(
                {
                    "candidate_id": task.candidate_id,
                    "title": task.title,
                    "target": task.target,
                }
            )
        elif result["moved"]:
            moved.append(
                {
                    "candidate_id": task.candidate_id,
                    "title": task.title,
                    "from": task.current,
                    "to": result["relative_path"],
                }
            )
        else:
            unchanged.append(
                {
                    "candidate_id": task.candidate_id,
                    "title": task.title,
                    "target": result["relative_path"],
                }
            )
    return {
        "force": plan.force,
        "dry_run": False,
        "scanned": plan.scanned,
        "queued": tuple(queued),
        "refiling": tuple(refiling),
        "moved": tuple(moved),
        "unchanged": tuple(unchanged),
        "skipped": tuple(skipped),
    }


def _translate(exc: Exception) -> tuple[str, str] | None:
    """Map a domain refusal onto `(code, message)`, or None for a real fault.

    Anything without the pair is re-raised by the caller: a broken filesystem
    must not read as 「99 已重归档，1 跳过」, which is the same rule the batch
    actions follow.
    """
    code = getattr(exc, "code", None)
    message = getattr(exc, "public_message", None)
    if code and message:
        return str(code), str(message)
    return None


async def rearchive_works(
    database,
    archived,
    conversion,
    *,
    force: bool = False,
    dry_run: bool = False,
    operator_name: str = "admin",
) -> dict:
    """Plan and run one 一键重新归档, in that order.

    `dry_run` stops after the plan and reports it in the same shape, so the page
    that renders 试跑 is the page that renders a real run. It is the answer to
    强制's cost: 「全库重新询问」 is one press away from a very large bill, and the
    operator gets to look at the list first.
    """
    plan = await plan_rearchive(
        database, archived, conversion, force=force, dry_run=dry_run
    )
    if dry_run:
        return _preview_report(plan)
    return await apply_rearchive(
        plan, archived, conversion, operator_name=operator_name
    )


def _preview_report(plan: ReArchivePlan) -> dict:
    """The plan as the run report would have described it, with nothing done.

    Every list is the same shape its real counterpart has, and it is filled from
    the same tasks -- a preview that summarised differently would be a second
    description of the sweep, free to drift from the first.
    """
    queued: list[dict] = []
    refiling: list[dict] = []
    moved: list[dict] = []
    for task in plan.tasks:
        if task.mode == "move":
            moved.append(
                {
                    "candidate_id": task.candidate_id,
                    "title": task.title,
                    "from": task.current,
                    "to": task.target,
                }
            )
        elif task.mode == "refile":
            refiling.append(
                {
                    "candidate_id": task.candidate_id,
                    "title": task.title,
                    "from": task.current,
                }
            )
        else:
            queued.append(
                {
                    "candidate_id": task.candidate_id,
                    "title": task.title,
                    "target": task.target,
                }
            )
    return {
        "force": plan.force,
        "dry_run": True,
        "scanned": plan.scanned,
        "queued": tuple(queued),
        "refiling": tuple(refiling),
        "moved": tuple(moved),
        "unchanged": (),
        "skipped": tuple(
            {
                "candidate_id": entry.candidate_id,
                "title": entry.title,
                "code": entry.code,
                "message": entry.message,
            }
            for entry in plan.skipped
        ),
    }


__all__ = [
    "ReArchivePlan",
    "ReArchiveSkip",
    "ReArchiveTask",
    "apply_rearchive",
    "plan_rearchive",
    "rearchive_works",
]
