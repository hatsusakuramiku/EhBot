from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
from collections.abc import Callable
from pathlib import Path, PurePosixPath

from app.archive.errors import (
    ArchiveError,
    ArchivePasswordRequired,
    ArchiveVolumesMissing,
)
from app.archive.processor import ArchiveProcessor
from app.archive.quality import quality_note
from app.ai.errors import (
    AI_CHAIN_EMPTY,
    AI_PATH_MISSING,
    AI_PATH_UNAVAILABLE,
    AiError,
)
from app.ai.paths import AiPathService
from app.archive.service import (
    PATH_SOURCE_AI,
    ArchiveSettingsService,
    TITLE_SOURCE_JAPANESE,
)
from app.conversion.comicinfo import build_comicinfo_xml
from app.conversion.convert import ConversionError
from app.conversion.naming import (
    CBZ_SUFFIX,
    DEFAULT_LIBRARY_TEMPLATE,
    LibraryPathError,
    LibraryTemplateError,
    check_library_segment,
    detect_library_limits,
    file_name_max,
    plan_library_path,
    render_library_path,
    unique_library_target,
)
from app.db.database import Database
from app.downloads.models import (
    CONVERSION_STATE_COMPLETED,
    CONVERSION_STATE_FAILED,
    CONVERSION_STATE_PENDING,
    CONVERSION_STATE_RUNNING,
    CONVERSION_STATE_WAITING_PASSWORD,
    CONVERSION_STATE_WAITING_PATH,
    CONVERSION_STATE_WAITING_VOLUMES,
    DOWNLOAD_STATE_COMPLETED,
    PROVIDER_CONVERSION,
    RECOVERABLE_CONVERSION_STATES,
)
from app.review.models import (
    STATUS_NEEDS_INFO,
    STATUS_NEEDS_REVISION,
    STATUS_PENDING_REVIEW,
    STATUS_REJECTED,
)

#: Candidate states that must never be packed, whatever is on disk.
#:
#: Deliberately a denylist. The readiness question -- 「is there a downloaded
#: archive?」 -- is answered by looking for the artifact, not by the candidate's
#: status, because a candidate has one status field for every job it spawned and
#: any claimed job sets it to `PROCESSING`. An allowlist therefore refused a book
#: whose preview-image source had completed while its torrent was still running,
#: which is an ordinary situation and not an error.
#:
#: What remains here is about *intent*, which a status is the right place to
#: record: a rejected book must not reach the library, and one still awaiting
#: review has not been approved for it. `PROCESSING`, `DOWNLOADED`, `APPROVED` and
#: `FAILED` are all packable when an archive exists -- `FAILED` included, because
#: the failure may have been one source of several and the operator re-packing is
#: how they recover it.
_PACK_FORBIDDEN_STATUSES: frozenset[str] = frozenset(
    {
        STATUS_REJECTED,
        STATUS_PENDING_REVIEW,
        STATUS_NEEDS_INFO,
        STATUS_NEEDS_REVISION,
    }
)


def _title_values(
    metadata, *, source: str, display_title: str
) -> dict[str, str | None]:
    """The three title placeholders, resolved from one metadata read.

    Built in one place because `_library_target` (inside a job) and
    `planned_library_path` (answering an operator) must name the same book the
    same way -- the previous arrangement had each assembling its own `values`
    dict, which is how they would drift.

    `{title}` follows the 标题来源 setting and falls back to the other language
    before the caller's display title: a gallery with only a `title_jpn` must
    still render under an `english` preference, or the setting would turn into a
    way to lose a name. `{japanese_title}` and `{english_title}` do not fall back
    across languages -- a template asking for one language explicitly should get
    that language or the untitled fallback, not a silent substitution.
    """
    japanese = _metadata_lookup(metadata, "JapaneseTitle")
    english = _metadata_lookup(metadata, "Title")
    preferred = japanese if source == TITLE_SOURCE_JAPANESE else english
    alternate = english if source == TITLE_SOURCE_JAPANESE else japanese
    return {
        "title": preferred or alternate or display_title,
        "japanese_title": japanese,
        "english_title": english,
    }


def _metadata_lookup(metadata, field_name: str) -> str | None:
    for entry in metadata:
        if entry.field_name == field_name:
            return entry.field_value
    return None


def _scan_information(metadata, image_quality: str | None) -> str | None:
    """Append the re-encode policy to the source grade already recorded.

    The provider grade (for example `EH_TORRENT original 121.0MiB`) says where
    the pages came from; the appended note says what EhBot did to them. Keeping
    both in one field means a re-encoded book can be told apart from an
    untouched one without opening a single page.
    """
    source = _metadata_lookup(metadata, "ScanInformation")
    note = quality_note(image_quality)
    if not note:
        return source
    return f"{source} {note}" if source else note


def _metadata_tags(metadata) -> tuple[str, ...]:
    tags: list[str] = []
    for field_name in ("TagsRaw", "Tags"):
        value = _metadata_lookup(metadata, field_name)
        if not value:
            continue
        for item in value.replace("\n", ",").split(","):
            tag = item.strip()
            if tag and tag not in tags:
                tags.append(tag)
    return tuple(tags)


class ConversionService:
    def __init__(
        self,
        database: Database,
        work_path: Path,
        library_path: Path,
        settings_service: ArchiveSettingsService | None = None,
        data_path: Path | None = None,
        notify: Callable[..., object] | None = None,
        metadata_enricher: Callable[[int], object] | None = None,
        ai_service: object | None = None,
        refile: Callable[[int, str], object] | None = None,
    ) -> None:
        self._database = database
        self._work_path = work_path
        self._library_path = library_path
        self._settings = settings_service or ArchiveSettingsService(
            database,
            data_path or work_path,
            default_library_path=library_path,
            default_work_path=work_path,
        )
        self._worker_task: asyncio.Task[None] | None = None
        self._notify = notify
        # Optional because a deployment without ExHentai configured has nothing
        # to enrich from, and because most tests construct this service directly
        # and do not care where metadata came from.
        self._metadata_enricher = metadata_enricher
        # Optional in the same spirit, and for one more reason: AI mode is off
        # by default and needs a provider before it means anything, so a service
        # built without one still has to serve every template-mode deployment.
        # When it is absent and the operator has switched AI mode on, the AI
        # branch refuses with 「没有配置」 rather than silently rendering a
        # template path that nobody chose.
        self._ai_paths = (
            AiPathService(database, ai_service, self._settings)
            if ai_service is not None
            else None
        )
        # The move half of a 「重新计算路径」 job. Injected for the same reason the
        # metadata enricher is: this service must not import the archive service
        # (the two are constructed in dependency order in `wiring`), and a test
        # of the job needs to watch the move without a filesystem.
        self._refile = refile

    async def _effective_paths(self) -> tuple[Path, Path]:
        """Read the directories per job so an operator change applies at once.

        The environment values remain the defaults; a stored override wins.
        Resolving here instead of in `__init__` means the setting takes effect
        on the next job rather than only after a restart.
        """
        library = await self._settings.library_path()
        work = await self._settings.work_path()
        return (library or self._library_path, work or self._work_path)

    async def path_source(self) -> str:
        """Which layer decides a book's path right now.

        Exposed so a caller that manages pins itself -- the batch repack -- can
        branch without reading settings, and so a page can label a form with the
        mode it will actually use.
        """
        return await self._settings.path_source()

    async def ai_sweep_settings(self) -> dict[str, object]:
        """The whole-library re-archive knobs, read in one place.

        Returned as a plain dict rather than four accessors because the sweep
        reads all four together and nothing else reads any of them: a caller that
        wants one of these wants the group.
        """
        return {
            #: How many works the sweep prepares before moving on to the next set.
            "batch_size": await self._settings.ai_batch_size(),
            #: How many of those may be enriching their metadata at once.
            "concurrency": await self._settings.ai_concurrency(),
            #: Whether 「已按当前提示词定好路径」 is in the default scope.
            "include_current": await self._settings.ai_default_include_current(),
        }

    async def current_ai_path(
        self, candidate_id: int, metadata
    ) -> PurePosixPath | None:
        """The cached AI path, if it was produced from today's inputs.

        Cache only, by construction: no model is asked, so this is safe to call
        from a page render, a batch plan or a sweep. None means 「没有 / 已过期」,
        which callers report rather than paper over -- see `_ai_relative_path`.
        """
        if self._ai_paths is None:
            return None
        cached = await self._ai_paths.current(candidate_id, metadata)
        return PurePosixPath(cached.relative_path) if cached is not None else None

    async def _ai_relative_path(
        self,
        candidate_id: int,
        metadata,
        *,
        allow_model: bool,
        refresh: bool = False,
    ) -> PurePosixPath | None:
        """The AI's answer for this book, or None to let the rules answer.

        One implementation for both callers, which is the point of the proposal's
        §3 chain: `_library_target` (inside a job) and `planned_library_path`
        (answering an operator) differ only in `allow_model`, so they cannot
        start disagreeing about what the model said.

        * `allow_model=True` -- a packing job, the only place a request is ever
          made. Cache first, model second, cache written on success.
        * `allow_model=False` -- an operator is waiting. Cache only. Nothing is
          invented and nothing is fetched: a missing or stale answer raises
          `AI_PATH_MISSING`, which the detail page turns into 「不预填」 and the
          re-archive plan turns into 「需要新答案」.

        Returns None only when the operator turned on 「AI 不可用时回退到规则匹配」
        and the model chain failed; the caller then renders the template. Without
        that switch the failure is raised as `AI_PATH_UNAVAILABLE`, which parks
        the job in 需干预 carrying the reason.

        `refresh` asks the model even when the cache would answer, and is only
        ever set from a 强制 sweep or a 「重新计算路径」 job -- see
        `AiPathService.resolve`.
        """
        if not allow_model:
            cached = await self.current_ai_path(candidate_id, metadata)
            if cached is None:
                raise LibraryPathError(
                    AI_PATH_MISSING,
                    "这本书还没有 AI 路径记录，打包时会向模型询问",
                )
            return cached
        try:
            if self._ai_paths is None:
                raise AiError(
                    AI_CHAIN_EMPTY,
                    "还没有配置 AI 供应商，请到「设置 → AI 供应商」添加并验证模型",
                )
            outcome = await self._ai_paths.resolve(
                candidate_id, metadata, refresh=refresh
            )
        except AiError as exc:
            if self._ai_paths is not None and await self._ai_paths.fallback_to_rules():
                logging.getLogger(__name__).info(
                    "ai_path_fallback",
                    extra={
                        "candidate_id": candidate_id,
                        "error_code": exc.code,
                        "error_message": exc.public_message,
                    },
                )
                return None
            message = (
                exc.public_message
                if exc.code == AI_PATH_UNAVAILABLE
                else f"AI 路径不可用：{exc.public_message}"
            )
            raise LibraryPathError(AI_PATH_UNAVAILABLE, message) from exc
        return PurePosixPath(outcome.suggestion.relative_path)

    async def ai_path_for_refile(
        self, candidate_id: int, *, refresh: bool = True
    ) -> PurePosixPath:
        """The path a 「重新计算路径」 job should move this book to.

        Deliberately *not* `_library_target`: that one is right for a pack, where
        a pin is the operator's decision about where the file must land, but this
        job exists precisely because the recorded path is about to be replaced.
        Reading the pin here would answer with the path the book is already on
        and the job would move nothing.

        So the order is 「AI, then the fallback template only when the operator
        asked for it」 -- the same decision `_library_target` makes, minus the
        pin. The move itself is handed to the archive service, which re-validates
        the path and records it as a computed (non-manual) decision.

        `refresh` comes from the job: a book whose cache is simply missing or
        stale does not need it (the fingerprint check already sends it to the
        model), while 强制 and 「重新起个名字」 do.

        Raises `LibraryPathError` when there is no usable path at all, which the
        job turns into 待定归档路径 with the reason on it.
        """
        metadata = await self.metadata_for(candidate_id)
        title = self.title_of(metadata, candidate_id)
        if await self._settings.path_source() != PATH_SOURCE_AI:
            # Only reachable if the operator switched back to template mode
            # between planning the sweep and running the job. Rendering the
            # template is the honest answer: it is what the current setting says.
            return await self.planned_library_path(candidate_id, title, metadata)
        from_ai = await self._ai_relative_path(
            candidate_id, metadata, allow_model=True, refresh=refresh
        )
        if from_ai is not None:
            return from_ai
        return await self.planned_library_path(candidate_id, title, metadata)

    async def _library_target(
        self,
        candidate_id: int,
        library_path: Path,
        metadata,
        title: str,
        *,
        refresh_ai_path: bool = False,
    ) -> Path:
        """Where this book's CBZ goes, per the operator's layout template.

        Read per job for the same reason the directories are: a template saved
        now applies to the next pack rather than after a restart. A stored
        template that no longer validates falls back to the flat default instead
        of failing the job -- the book is already downloaded, and refusing to
        publish it over a settings mistake is the worse outcome. The settings
        page is where an invalid template is caught, and it cannot be saved.
        """
        fallback = f"candidate-{candidate_id}"
        reserved = frozenset(
            await asyncio.to_thread(self._existing_cbz_paths_sync, candidate_id)
        )
        # An operator who renamed or moved this book has pinned where it lives,
        # and a repack must land there. Re-rendering the template would move the
        # file back and `unique_library_target` would then see the operator's
        # copy as somebody else's book and grow a ` (2)` beside it -- so the
        # rename would read as undone *and* duplicated. The pin wins over the
        # template for the same reason `is_locked` wins over a scrape: it is a
        # judgement the operator already made about this one book.
        pinned = await asyncio.to_thread(
            self._pinned_library_path_sync, candidate_id
        )
        if pinned is not None:
            # Re-checked here rather than trusted from the write. The limits
            # belong to the *filesystem*, and the library root is a setting:
            # moving the library onto a different disk (or deeper into one) can
            # put a path that was legal when it was pinned past what the new one
            # takes. The refusal parks the job with the reason on it, which is
            # the only way the operator finds out at all.
            limits = detect_library_limits(library_path)
            ceilings = [
                (part, limits.name_max) for part in pinned.parent.parts
            ]
            # The stem is fitted without the suffix; the published component
            # has it, so `file_name_max` is the allowance to compare against.
            ceilings.append((pinned.stem, file_name_max(limits)))
            refusal = next(
                (
                    found
                    for segment, ceiling in ceilings
                    if (found := check_library_segment(
                        segment, name_max=ceiling
                    ))
                    is not None
                ),
                None,
            )
            if refusal is not None:
                raise LibraryPathError(*refusal)
            encoded = len(str(pinned).encode("utf-8"))
            if encoded > limits.relative_max:
                raise LibraryPathError(
                    "PATH_TOO_LONG",
                    f"归档路径占 {encoded} 字节，超过本机文件系统给这个书库留下的 "
                    f"{limits.relative_max} 字节，请在作品详情页改短归档路径",
                )
            return await asyncio.to_thread(
                unique_library_target, library_path / pinned, reserved=reserved
            )
        # AI mode replaces the template entirely (proposal §2): the two answer
        # one question and 「两个都开」 would be a third rule engine. The pinned
        # path above still wins -- 「手动指定优先」 is older than this feature and
        # this branch does not touch it.
        if await self._settings.path_source() == PATH_SOURCE_AI:
            from_ai = await self._ai_relative_path(
                candidate_id,
                metadata,
                allow_model=True,
                refresh=refresh_ai_path,
            )
            if from_ai is not None:
                return await asyncio.to_thread(
                    unique_library_target,
                    library_path / from_ai,
                    reserved=reserved,
                )
            # Fallback to the rules only happens when the operator turned the
            # switch on (proposal §7); the AI helper logs `ai_path_fallback`.
        # A routing rule decides the template when its condition matches; the
        # global template is the "no rule hit" default. Logged so an operator
        # who sees an unexpected layout can trace which rule chose it.
        template, matched_rule = await self._settings.library_template_for(
            candidate_id
        )
        if matched_rule is not None:
            logging.getLogger(__name__).info(
                "archive_path_rule_matched",
                extra={"rule_id": matched_rule.rule_id},
            )
        values = {
            "category": _metadata_lookup(metadata, "Category"),
            "artist": _metadata_lookup(metadata, "Artist"),
            **_title_values(
                metadata,
                source=await self._settings.title_source(),
                display_title=title,
            ),
        }
        limits = detect_library_limits(library_path)
        try:
            relative = render_library_path(
                template, values, title_fallback=fallback, limits=limits
            )
        except LibraryTemplateError:
            logging.getLogger(__name__).warning(
                "library_template_unusable",
                extra={"error_code": "TEMPLATE_INVALID"},
            )
            relative = render_library_path(
                DEFAULT_LIBRARY_TEMPLATE,
                values,
                title_fallback=fallback,
                limits=limits,
            )
        # Appended rather than `with_suffix`, which would read 「Vol. 1」 as a
        # name with a `. 1` extension and publish the book as `Vol.cbz`.
        target = library_path / relative.parent / f"{relative.name}{CBZ_SUFFIX}"
        return await asyncio.to_thread(
            unique_library_target, target, reserved=reserved
        )

    async def planned_library_path(
        self, candidate_id: int, title: str, metadata
    ) -> PurePosixPath:
        """What the current routing decision gives this book, refusing if unusable.

        The strict counterpart of `_library_target`, and the split is deliberate.
        `_library_target` runs inside a job for a book that is already
        downloaded, so it repairs what it can and never refuses -- failing a job
        over a punctuation mark would leave the book unpublished for a reason the
        operator did not ask about. This runs while the operator is waiting for
        an answer, on a path they have not agreed to yet, so it reports instead:
        a batch re-file that silently sanitised fifty titles would move fifty
        books to names nobody chose.

        Raises `LibraryPathError`, which the batch turns into a per-work reason.
        """
        if await self._settings.path_source() == PATH_SOURCE_AI:
            # Cache only, never the model: this runs while an operator waits, and
            # an answer the packer has not committed to must not be shown as one
            # (proposal §7, 「操作员侧不撒谎」). No cache yet is a normal state for
            # a book that has never been packed -- it is reported as
            # `AI_PATH_MISSING`, which the callers turn into 「不预填」 or
            # 「需要新答案」 rather than into a wrong path.
            from_ai = await self._ai_relative_path(
                candidate_id, metadata, allow_model=False
            )
            if from_ai is not None:
                return from_ai
        template, _matched = await self._settings.library_template_for(
            candidate_id
        )
        library_path = await self._settings.library_path()
        return plan_library_path(
            template,
            {
                "category": _metadata_lookup(metadata, "Category"),
                "artist": _metadata_lookup(metadata, "Artist"),
                **_title_values(
                    metadata,
                    source=await self._settings.title_source(),
                    display_title=title,
                ),
            },
            title_fallback=f"candidate-{candidate_id}",
            limits=detect_library_limits(library_path),
        )

    async def planned_path_for_candidate(
        self, candidate_id: int
    ) -> PurePosixPath | None:
        """The relative CBZ path the current rules give this work, if it has a name.

        The one-step form of `planned_library_path` for the two callers that hold
        no metadata: the detail page's prefilled 归档路径 field, and the re-archive
        sweep that plans a whole library in one pass. Deriving it here rather than
        at each call site is what keeps those two from disagreeing about what
        「按最新路径规则生成的路由」 means -- a second copy of the three lookups
        below is exactly how one of them would start rendering a different path.

        `None` when the work has no title at all, and deliberately *not* the
        packer's `candidate-<id>` fallback. Both callers are operator-facing: a
        path built from a placeholder is not a name to show in a form (the
        operator would read it as the book's own) and not one to pin (a pin
        outlives the metadata fetch that would have replaced it -- which is how a
        `Candidate 57.cbz`, once pinned, becomes permanent). Inside a job the
        fallback stays the right answer, because refusing to publish a book
        already downloaded is worse than publishing it under a temporary name.

        Raises `LibraryPathError` for a path this filesystem will not take; the
        caller decides whether that is a refusal, a parked task or an empty form.
        """
        metadata = await self.metadata_for(candidate_id)
        if not (
            _metadata_lookup(metadata, "Title")
            or _metadata_lookup(metadata, "JapaneseTitle")
        ):
            return None
        return await self.planned_library_path(
            candidate_id, self.title_of(metadata, candidate_id), metadata
        )

    async def metadata_for(self, candidate_id: int):
        """This work's metadata rows, for a caller planning its path.

        Exposed because `planned_library_path` needs them and the batch has no
        business reaching into `_fetch_metadata_sync`.
        """
        return await asyncio.to_thread(self._fetch_metadata_sync, candidate_id)

    @staticmethod
    def title_of(metadata, candidate_id: int) -> str:
        """The title the packer would use, resolved the one way it resolves it."""
        return (
            _metadata_lookup(metadata, "Title") or f"Candidate {candidate_id}"
        )

    def _pinned_library_path_sync(self, candidate_id: int) -> PurePosixPath | None:
        """The library-relative path this book is pinned to, if any.

        Two sources, newest first. `work_archive_paths` is where an explicit path
        is recorded now: it is keyed by candidate, so it exists before the first
        pack and survives a work's jobs being removed. `artifacts
        .library_relative_path` is what renames written before migration 015
        recorded, and reading it as a fallback is what keeps those working
        without a data migration that would have to guess.

        Read as a relative path and re-joined onto the *current* library root
        rather than stored absolute, so moving the library directory carries a
        renamed book with it. Validated before use even though this process
        wrote it: a path read back out of the database and joined onto a root is
        exactly the shape that must not be trusted twice -- an absolute value or
        a `..` would escape the library, so it is ignored and the template
        renders the path instead.
        """
        with self._database.connection() as connection:
            row = connection.execute(
                "SELECT relative_path FROM work_archive_paths "
                "WHERE candidate_id = ?",
                (candidate_id,),
            ).fetchone()
            if row is None or not row[0]:
                row = connection.execute(
                    "SELECT artifacts.library_relative_path FROM artifacts "
                    "JOIN download_jobs ON download_jobs.id = artifacts.job_id "
                    "WHERE download_jobs.candidate_id = ? "
                    "AND artifacts.artifact_type = 'CBZ' "
                    "AND artifacts.library_relative_path IS NOT NULL "
                    "ORDER BY artifacts.id DESC LIMIT 1",
                    (candidate_id,),
                ).fetchone()
        if row is None or not row[0]:
            return None
        relative = PurePosixPath(str(row[0]).replace("\\", "/"))
        if relative.is_absolute() or ".." in relative.parts:
            logging.getLogger(__name__).warning(
                "library_relative_path_refused",
                extra={"error_code": "PATH_OUTSIDE_ROOT"},
            )
            return None
        return relative

    def _existing_cbz_paths_sync(self, candidate_id: int) -> tuple[str, ...]:
        """Paths already recorded as this book's own CBZ.

        Re-packing must land on the file it replaces. Without this, the conflict
        suffix would treat the previous CBZ as somebody else's book and every
        重新打包 would leave `book.cbz`, `book (2).cbz`, `book (3).cbz` behind.
        """
        with self._database.connection() as connection:
            rows = connection.execute(
                "SELECT artifacts.path FROM artifacts "
                "JOIN download_jobs ON download_jobs.id = artifacts.job_id "
                "WHERE download_jobs.candidate_id = ? "
                "AND artifacts.artifact_type = 'CBZ'",
                (candidate_id,),
            ).fetchall()
        return tuple(str(row[0]) for row in rows if row[0])

    async def enqueue_for_candidate(
        self,
        candidate_id: int,
        *,
        refile: bool = False,
        refresh_ai_path: bool = False,
    ) -> int:
        """Queue this work for packaging, or for a path-only re-file.

        Two flags, both about AI mode and both carried on the job row rather
        than inferred:

        * `refile` -- the book is already packed and only its path needs
          recomputing. The job asks the model and *moves* the CBZ; it never
          re-derives the pages (proposal §9, 「已打包不再重新打包」).
        * `refresh_ai_path` -- ask the model even though the cache would answer.
          Set by a 强制 sweep, whose instruction is 「按当前 prompt 重新询问」.

        Either way the flags ride in `details_json`, which is already the job's
        free-form column: a new column for two booleans one branch reads would
        be a schema change for nothing.
        """
        return await asyncio.to_thread(
            self._enqueue_sync, candidate_id, refile, refresh_ai_path
        )

    def _enqueue_sync(
        self,
        candidate_id: int,
        refile: bool = False,
        refresh_ai_path: bool = False,
    ) -> int:
        details: dict[str, object] = {}
        if refile:
            details["refile"] = True
        if refresh_ai_path:
            details["refresh_ai_path"] = True
        details_json = json.dumps(details, separators=(",", ":"))
        with self._database.connection() as connection:
            candidate_row = connection.execute(
                "SELECT status FROM candidates WHERE id = ?",
                (candidate_id,),
            ).fetchone()
            if candidate_row is None:
                raise ConversionError(
                    "CANDIDATE_NOT_FOUND",
                    "Candidate does not exist",
                )
            # What packing actually requires is a downloaded archive, which the
            # next query establishes. The candidate's status is a *different*
            # question, and gating on it was wrong for a case that happens
            # routinely: a candidate has ONE status field shared by every job it
            # spawned, and claiming any job sets it to PROCESSING. So a book whose
            # preview-image source had already finished, while its qBittorrent
            # torrent was still running, sat at PROCESSING with a complete archive
            # on disk -- and 打包 answered 「Only approved candidates can be
            # converted」, which is both a refusal of something legitimate and a
            # sentence that does not describe the situation.
            #
            # REJECTED and the pre-approval states are still refused, because
            # packing a book the operator rejected would publish it into the
            # library. That is a statement about intent, so it is a status check;
            # readiness is not.
            status = str(candidate_row[0])
            if status in _PACK_FORBIDDEN_STATUSES:
                raise ConversionError(
                    "CANDIDATE_NOT_READY",
                    f"候选状态为 {status}，不能打包",
                )
            # What 「ready」 means depends on what the job will do, and the two
            # answers are genuinely different:
            #
            # * a pack needs the downloaded archive, because that is the input it
            #   compresses;
            # * a re-file needs the *published* CBZ instead, and asking it for
            #   the source archive would refuse the ordinary case -- with
            #   「保留原始压缩包」 off (the default) the source is deleted the
            #   moment the pack succeeds, so every book the sweep would re-file
            #   has no archive left.
            if refile:
                packable = connection.execute(
                    "SELECT a.path FROM download_jobs dj "
                    "JOIN artifacts a ON a.job_id = dj.id "
                    "WHERE dj.candidate_id = ? AND a.artifact_type = 'CBZ' "
                    "ORDER BY a.id DESC LIMIT 1",
                    (candidate_id,),
                ).fetchone()
                if packable is None:
                    raise ConversionError(
                        "WORK_NOT_PACKAGED",
                        "该作品还没有打包产物，无法重算路径",
                    )
            else:
                artifact_row = connection.execute(
                    "SELECT a.path FROM download_jobs dj "
                    "JOIN artifacts a ON a.job_id = dj.id "
                    "WHERE dj.candidate_id = ? AND dj.state = ? "
                    "AND a.artifact_type = 'ARCHIVE' "
                    "ORDER BY dj.id DESC LIMIT 1",
                    (candidate_id, DOWNLOAD_STATE_COMPLETED),
                ).fetchone()
                if artifact_row is None:
                    # This is now the readiness gate, so the message has to be the
                    # one an operator reads when 打包 is pressed too early.
                    raise ConversionError(
                        "ARCHIVE_NOT_READY",
                        "该作品还没有下载完成的压缩包，无法打包",
                    )
            before = connection.total_changes
            connection.execute(
                "INSERT INTO download_jobs "
                "(candidate_id, idempotency_key, provider, state, "
                "details_json) VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT(idempotency_key) DO NOTHING",
                (
                    candidate_id,
                    f"convert:{candidate_id}",
                    PROVIDER_CONVERSION,
                    CONVERSION_STATE_PENDING,
                    details_json,
                ),
            )
            created = connection.total_changes > before
            if not created:
                # The task already exists, so requeueing it is an UPDATE. Every
                # state except RUNNING is requeueable, and each for its own
                # reason:
                #
                # * WAITING_VOLUMES / WAITING_PASSWORD / WAITING_PATH -- the
                #   operator has supplied what was missing, or fixed the path.
                # * FAILED -- a retry after fixing the cause.
                # * COMPLETED -- 重新打包. This one is the whole point of the
                #   action and was missing until 2026-08-28: `DO NOTHING` above
                #   left the row COMPLETED, the worker claims only PENDING, so
                #   the request 303'd back to a page reporting success while
                #   nothing had been re-packed and the CBZ on disk was
                #   untouched. Landing on the file it replaces is already
                #   handled -- `_existing_cbz_paths_sync` reserves this book's
                #   own path so the conflict suffix does not invent
                #   `book (2).cbz`.
                #
                # RUNNING is excluded because the worker holds that row; the
                # requeue would be overwritten by whatever it writes next.
                connection.execute(
                    "UPDATE download_jobs SET state = ?, error_code = NULL, "
                    "error_message = NULL, details_json = ?, "
                    "updated_at = CURRENT_TIMESTAMP "
                    "WHERE idempotency_key = ? AND state IN (?, ?, ?, ?, ?)",
                    (
                        CONVERSION_STATE_PENDING,
                        details_json,
                        f"convert:{candidate_id}",
                        CONVERSION_STATE_WAITING_VOLUMES,
                        CONVERSION_STATE_WAITING_PASSWORD,
                        CONVERSION_STATE_WAITING_PATH,
                        CONVERSION_STATE_FAILED,
                        CONVERSION_STATE_COMPLETED,
                    ),
                )
            row = connection.execute(
                "SELECT id FROM download_jobs WHERE idempotency_key = ?",
                (f"convert:{candidate_id}",),
            ).fetchone()
            return int(row[0])

    async def start(self) -> None:
        if self._worker_task is not None:
            return
        await self.reclaim_running_jobs()
        self._worker_task = asyncio.create_task(
            self._run_worker(), name="conversion-worker"
        )

    async def reclaim_running_jobs(self) -> int:
        """Return packaging jobs a dead process left mid-run to the queue.

        The download queue answers this question with `lease_expires_at`; this
        one has no lease column to read, because `_claim_pending_job_sync` here
        never wrote one. What stands in for it is the single fact that makes the
        lease unnecessary: there is one conversion worker per process, and this
        runs inside `start`, before that worker has claimed anything. A
        `CONVERSION_RUNNING` row at this moment therefore cannot belong to
        anybody -- it is always the residue of a process that died holding it.

        Without this the row stayed `CONVERSION_RUNNING` forever, and every
        operator action on the work refused with 「该作品正在打包」: remove,
        redownload, rename and re-path all guard on that state, so an
        interrupted pack locked the book out of its own detail page.

        Re-queued rather than failed. Packing is idempotent -- it reads the
        archive artifact and republishes -- so the honest outcome of 「我们不知道
        它做完了没有」 is to do it again, where marking it failed would ask an
        operator to press retry for a failure that never happened.
        """
        reclaimed = await asyncio.to_thread(self._reclaim_running_jobs_sync)
        if reclaimed:
            logging.getLogger(__name__).warning(
                "conversion_jobs_reclaimed jobs=%d",
                reclaimed,
                extra={"error_code": "CONVERSION_JOB_RECLAIMED"},
            )
        return reclaimed

    def _reclaim_running_jobs_sync(self) -> int:
        with self._database.connection() as connection:
            cursor = connection.execute(
                "UPDATE download_jobs SET state = ?, "
                "updated_at = CURRENT_TIMESTAMP "
                "WHERE state = ? AND provider = ?",
                (
                    CONVERSION_STATE_PENDING,
                    CONVERSION_STATE_RUNNING,
                    PROVIDER_CONVERSION,
                ),
            )
            return int(cursor.rowcount or 0)

    async def stop(self) -> None:
        if self._worker_task is None:
            return
        self._worker_task.cancel()
        await asyncio.gather(
            self._worker_task, return_exceptions=True
        )
        self._worker_task = None

    async def _run_worker(self) -> None:
        while True:
            try:
                processed = await self._process_one()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                logging.getLogger(__name__).exception(
                    "conversion_worker_error",
                    extra={"error_code": "CONVERSION_WORKER_ERROR"},
                )
                processed = False
            if not processed:
                await asyncio.sleep(1.0)

    async def _process_one(self) -> bool:
        job = await asyncio.to_thread(self._claim_pending_job_sync)
        if job is None:
            return False
        # Paired claimed / finished records, at the same single point
        # `_announce` uses. `provider` is not logged: this queue only ever holds
        # `PROVIDER_CONVERSION`, and a constant field would be noise.
        logger = logging.getLogger(__name__)
        job_context = {
            "job_id": job["job_id"],
            "candidate_id": job["candidate_id"],
        }
        logger.info("conversion_job_claimed", extra=job_context)
        started = time.monotonic()
        try:
            await self._handle_job(job)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            await asyncio.to_thread(
                self._mark_failed_sync, job["job_id"], "WORKER_EXCEPTION", str(exc)
            )
            logger.exception(
                "conversion_job_exception",
                extra={
                    **job_context,
                    "error_code": "WORKER_EXCEPTION",
                    "duration_ms": int((time.monotonic() - started) * 1000),
                },
            )
        else:
            # `_handle_job` returns normally whether it packed the book, parked
            # it for a missing volume / password / path, or marked it failed;
            # only the row itself tells the difference. Read the final state
            # and log accordingly so a packaging failure is not recorded as
            # `conversion_job_finished` -- the bug that hid the original
            # complaint behind a misleading "finished" line.
            final_state, error_code, error_message = await asyncio.to_thread(
                self._read_final_state_sync, job["job_id"]
            )
            duration_ms = int((time.monotonic() - started) * 1000)
            base = {**job_context, "duration_ms": duration_ms}
            if final_state == CONVERSION_STATE_COMPLETED:
                logger.info("conversion_job_completed", extra=base)
            elif final_state in RECOVERABLE_CONVERSION_STATES:
                logger.info(
                    "conversion_job_parked",
                    extra={
                        **base,
                        "status": final_state,
                        "error_code": error_code,
                    },
                )
            else:
                logger.warning(
                    "conversion_job_failed",
                    extra={
                        **base,
                        "status": final_state,
                        "error_code": error_code,
                        "error_message": error_message,
                    },
                )
        # Announced once here rather than at each of the five terminal writes
        # inside `_handle_job`. Every exit -- packed, failed, waiting for a
        # volume, waiting for a password, worker crash -- has already committed
        # its row by the time control reaches this line, so one publish covers
        # all of them and a future branch cannot forget to notify.
        self._announce(job["job_id"], job["candidate_id"])
        return True

    def _announce(self, job_id: int, candidate_id: int) -> None:
        """Tell the interface a packaging job moved, if anything is listening.

        Never allowed to disturb the worker: the activity page falls back to
        polling, so a subscriber problem must not fail a CBZ that was written
        successfully.
        """
        if self._notify is None:
            return
        try:
            self._notify(job_id=job_id, candidate_id=candidate_id)
        except Exception:  # noqa: BLE001 - notification is best-effort
            logging.getLogger(__name__).warning(
                "conversion_notify_failed",
                extra={"error_code": "CONVERSION_NOTIFY_FAILED"},
            )

    def _claim_pending_job_sync(self) -> dict | None:
        with self._database.connection() as connection:
            row = connection.execute(
                "SELECT id, candidate_id, details_json FROM download_jobs "
                "WHERE state = ? AND provider = ? "
                # Same ordering as the download claim: a promoted packaging job
                # runs first, and within one priority the queue stays FIFO. The
                # two queues are separate but an operator adjusts priority the
                # same way in both, so they must honour it the same way.
                "ORDER BY priority, id LIMIT 1",
                (CONVERSION_STATE_PENDING, PROVIDER_CONVERSION),
            ).fetchone()
            if row is None:
                return None
            connection.execute(
                "UPDATE download_jobs SET state = ?, "
                "attempt_count = attempt_count + 1, "
                "updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                (CONVERSION_STATE_RUNNING, int(row[0])),
            )
            details: dict = {}
            if row[2]:
                try:
                    parsed = json.loads(str(row[2]))
                except (ValueError, TypeError):
                    parsed = None
                if isinstance(parsed, dict):
                    details = parsed
            return {
                "job_id": int(row[0]),
                "candidate_id": int(row[1]),
                # `refile` sends the job down the path-only branch below;
                # `refresh_ai_path` tells the AI branch to ignore its cache.
                "refile": bool(details.get("refile")),
                "refresh_ai_path": bool(details.get("refresh_ai_path")),
            }

    async def _handle_job(self, job: dict) -> None:
        # Same shape the worker logs at claim time, so the catch-site log
        # lines below carry the same correlation fields. Duplicated rather
        # than threaded through because `_handle_job` is called from exactly
        # one place and the duplication is shorter than a parameter.
        job_context = {
            "job_id": job["job_id"],
            "candidate_id": job["candidate_id"],
        }
        if job.get("refile"):
            # Before the archive-artifact check, because this job does not need
            # one: the book is already published and its source archive may
            # legitimately be gone.
            await self._handle_refile_job(job)
            return
        source_artifact = await asyncio.to_thread(
            self._fetch_source_artifact_sync, job["candidate_id"]
        )
        if source_artifact is None:
            await asyncio.to_thread(
                self._mark_failed_sync,
                job["job_id"],
                "ARCHIVE_NOT_READY",
                "Archive artifact no longer exists",
            )
            return
        source_path = Path(source_artifact["path"])
        if not source_path.exists():
            await asyncio.to_thread(
                self._mark_failed_sync,
                job["job_id"],
                "ARCHIVE_MISSING",
                "Archive file was removed before conversion",
            )
            return

        # Metadata first, always. Everything below this line is derived from it:
        # the library path the template renders, the filename, and the whole of
        # ComicInfo.xml. A book packed before its gallery was read lands as
        # `candidate-57.cbz` with an empty ComicInfo, and re-fetching afterwards
        # does not move it -- the path is written into an artifact row by then, so
        # the damage is a file the operator has to rename by hand.
        #
        # The order was previously the other way round for the automatic path: a
        # download finishing enqueued a pack immediately, and enrichment only ran
        # when somebody opened 待审核. So an unattended deployment reliably
        # produced exactly the badly-named files nobody was there to notice.
        await self.ensure_metadata(job["candidate_id"])
        metadata = await asyncio.to_thread(
            self._fetch_metadata_sync, job["candidate_id"]
        )
        title = (
            _metadata_lookup(metadata, "Title")
            or f"Candidate {job['candidate_id']}"
        )
        library_path, work_path = await self._effective_paths()
        try:
            library_target = await self._library_target(
                job["candidate_id"],
                library_path,
                metadata,
                title,
                refresh_ai_path=bool(job.get("refresh_ai_path")),
            )
        except LibraryPathError as exc:
            # Two things raise here, and both park rather than fail. The template
            # branch sanitises, so the only path it can refuse is a *pinned* one
            # an operator or a batch recorded that this filesystem will not take.
            # AI mode adds the other: every model failed and no fallback was
            # asked for, so there is no path at all. Either way nothing was
            # attempted and the archive is intact, so the remedy is a settings or
            # metadata edit followed by a requeue -- the shape 待补分卷 already has.
            logging.getLogger(__name__).exception(
                "conversion_path_rejected",
                extra={
                    **job_context,
                    "status": CONVERSION_STATE_WAITING_PATH,
                    "error_code": exc.code,
                },
            )
            await asyncio.to_thread(
                self._mark_waiting_sync,
                job["job_id"],
                CONVERSION_STATE_WAITING_PATH,
                exc.code,
                exc.public_message,
                {},
            )
            return
        image_quality = await self._settings.image_quality()
        processor = await self._build_processor(image_quality)
        try:
            result = await asyncio.to_thread(
                processor.process,
                source_path,
                destination=library_target,
                work_directory=work_path / "conversion",
                comicinfo_builder=lambda page_count: self._build_comicinfo(
                    metadata, title, page_count, image_quality
                ),
                library_path=library_path,
            )
        except ArchiveVolumesMissing as exc:
            logging.getLogger(__name__).exception(
                "conversion_volumes_missing",
                extra={
                    **job_context,
                    "status": CONVERSION_STATE_WAITING_VOLUMES,
                    "error_code": exc.code,
                },
            )
            await asyncio.to_thread(
                self._mark_waiting_sync,
                job["job_id"],
                CONVERSION_STATE_WAITING_VOLUMES,
                exc.code,
                exc.public_message,
                {"missing_volumes": list(exc.missing)},
            )
            return
        except ArchivePasswordRequired as exc:
            logging.getLogger(__name__).exception(
                "conversion_password_required",
                extra={
                    **job_context,
                    "status": CONVERSION_STATE_WAITING_PASSWORD,
                    "error_code": exc.code,
                },
            )
            await asyncio.to_thread(
                self._mark_waiting_sync,
                job["job_id"],
                CONVERSION_STATE_WAITING_PASSWORD,
                exc.code,
                exc.public_message,
                {},
            )
            return
        except (ArchiveError, ConversionError) as exc:
            # The original traceback is the part an operator needs; the
            # `_handle_job` row only carries the public message. Log the
            # exception here so the archive's full stack reaches the JSON
            # payload, then mark the row failed with the same code.
            logging.getLogger(__name__).exception(
                "conversion_archive_failed",
                extra={
                    **job_context,
                    "status": CONVERSION_STATE_FAILED,
                    "error_code": exc.code,
                    "error_message": exc.public_message,
                },
            )
            await asyncio.to_thread(
                self._mark_failed_sync,
                job["job_id"],
                exc.code,
                exc.public_message,
            )
            return
        if result.password_id is not None:
            await self._settings.mark_password_success(result.password_id)
        await asyncio.to_thread(
            self._record_cbz_artifact_sync,
            job["job_id"],
            result.cbz_path,
            result.page_count,
            library_path,
        )
        await asyncio.to_thread(
            self._mark_completed_sync,
            job["job_id"],
            {
                **result.snapshot.as_dict(),
                "page_count": result.page_count,
                "volume_count": result.volume_count,
                "skipped_members": list(result.skipped_members),
                "password_entry_id": result.password_id,
                "image_quality": result.image_quality,
                "rewritten_pages": result.rewritten_pages,
            },
        )
        if not await self._settings.keep_original():
            await asyncio.to_thread(self._remove_original_sync, source_path)

    async def _handle_refile_job(self, job: dict) -> None:
        """Recompute one published book's path and move the file there.

        The job R30's sweep queues for a book that already has a CBZ: the file
        on disk is this book's content, so it is *moved*, never rebuilt. Nothing
        here touches the packer -- the only expensive step is the model call,
        and the only filesystem step is the rename the archive service performs
        (which also re-validates the path and records it as a computed
        decision).

        Every failure maps onto a state the operator already knows: no usable
        path parks in 待定归档路径 (the same shape a failed pack takes), and a
        refused move is a failure with the move's own code on it.
        """
        job_context = {
            "job_id": job["job_id"],
            "candidate_id": job["candidate_id"],
        }
        candidate_id = job["candidate_id"]
        await self.ensure_metadata(candidate_id)
        try:
            relative = await self.ai_path_for_refile(
                candidate_id, refresh=bool(job.get("refresh_ai_path"))
            )
        except LibraryPathError as exc:
            logging.getLogger(__name__).exception(
                "conversion_path_rejected",
                extra={
                    **job_context,
                    "status": CONVERSION_STATE_WAITING_PATH,
                    "error_code": exc.code,
                },
            )
            await asyncio.to_thread(
                self._mark_waiting_sync,
                job["job_id"],
                CONVERSION_STATE_WAITING_PATH,
                exc.code,
                exc.public_message,
                {},
            )
            return
        if self._refile is None:
            await asyncio.to_thread(
                self._mark_failed_sync,
                job["job_id"],
                "REFILE_UNAVAILABLE",
                "归档服务不可用，无法移动已打包的作品",
            )
            return
        try:
            result = await self._refile(candidate_id, relative.as_posix())
        except Exception as exc:  # noqa: BLE001 - classified below
            code = str(getattr(exc, "code", None) or "REFILE_FAILED")
            message = str(getattr(exc, "public_message", None) or exc)
            logging.getLogger(__name__).exception(
                "conversion_refile_failed",
                extra={**job_context, "error_code": code},
            )
            await asyncio.to_thread(
                self._mark_failed_sync, job["job_id"], code, message
            )
            return
        await asyncio.to_thread(
            self._mark_completed_sync,
            job["job_id"],
            {
                "refiled": True,
                "moved": bool(result.get("moved")),
                "relative_path": result.get("relative_path")
                or relative.as_posix(),
            },
        )

    async def ensure_metadata(self, candidate_id: int) -> None:
        """Pull the gallery's metadata before packing, if it is still missing.

        Public because packing is not the only step that derives a name from
        this metadata. The batch re-file on `/downloaded` computes a library
        path and *pins* it before the job is even enqueued, and a pin beats
        the template forever -- so a batch that read the metadata before it
        was fetched did not merely produce a bad name once, it recorded that
        name as the operator's own decision. Both callers now go through here
        first, which is what makes 「先拉元数据」 one rule rather than two.

        A no-op when the candidate has no ExHentai reference or already has its
        metadata -- the enricher answers that with one query and no HTTP call, so
        this costs nothing on the ordinary path and only does work in the case
        that would otherwise publish an unnamed book.

        A failure is logged, not raised. The archive is downloaded and packing it
        under a fallback name is a worse outcome than not packing it at all only
        if the operator never finds out; parking the job because ExHentai was
        briefly unreachable would strand a complete download behind an outage.
        """
        if self._metadata_enricher is None:
            return
        try:
            await self._metadata_enricher(candidate_id)
        except Exception as exc:  # noqa: BLE001 - enrichment is best-effort
            logging.getLogger(__name__).warning(
                "conversion_metadata_enrichment_failed candidate=%d error=%s",
                candidate_id,
                exc,
                extra={
                    "candidate_id": candidate_id,
                    "error_code": "CONVERSION_METADATA_ENRICH_FAILED",
                },
            )

    async def _build_processor(
        self, image_quality: str | None = None
    ) -> ArchiveProcessor:
        profiles = await self._settings.profiles(enabled_only=True)
        limits = await self._settings.limits()
        passwords = await self._settings.password_attempts()
        if image_quality is None:
            image_quality = await self._settings.image_quality()
        return ArchiveProcessor(
            profiles=profiles,
            limits=limits,
            passwords=passwords,
            tools_path=self._settings.tools_path,
            image_quality=image_quality,
        )

    @staticmethod
    def _build_comicinfo(
        metadata, title: str, page_count: int, image_quality: str | None = None
    ) -> bytes:
        rating_value = _metadata_lookup(metadata, "Rating")
        try:
            rating = float(rating_value) if rating_value else None
        except ValueError:
            rating = None
        return build_comicinfo_xml(
            title=title,
            artist=_metadata_lookup(metadata, "Artist"),
            language=_metadata_lookup(metadata, "Language"),
            category=_metadata_lookup(metadata, "Category"),
            tags=_metadata_tags(metadata),
            rating=rating,
            description=_metadata_lookup(metadata, "Description"),
            page_count=page_count,
            japanese_title=_metadata_lookup(metadata, "JapaneseTitle"),
            group=_metadata_lookup(metadata, "Group"),
            parody=_metadata_lookup(metadata, "Parody"),
            character=_metadata_lookup(metadata, "Character"),
            web=_metadata_lookup(metadata, "Web"),
            scan_information=_scan_information(metadata, image_quality),
        )

    @staticmethod
    def _remove_original_sync(source_path: Path) -> None:
        """Delete the original archive only after the CBZ record is committed."""
        try:
            source_path.unlink(missing_ok=True)
        except OSError:
            logging.getLogger(__name__).warning(
                "original_archive_removal_failed",
                extra={"error_code": "ARCHIVE_CLEANUP_FAILED"},
            )

    def _fetch_source_artifact_sync(
        self, candidate_id: int
    ) -> dict | None:
        with self._database.connection() as connection:
            row = connection.execute(
                "SELECT a.path FROM download_jobs dj "
                "JOIN artifacts a ON a.job_id = dj.id "
                "WHERE dj.candidate_id = ? AND dj.state = ? "
                "AND a.artifact_type = 'ARCHIVE' "
                "ORDER BY dj.id DESC LIMIT 1",
                (candidate_id, DOWNLOAD_STATE_COMPLETED),
            ).fetchone()
            return {"path": str(row[0])} if row else None

    def _fetch_metadata_sync(self, candidate_id: int) -> list:
        with self._database.connection() as connection:
            rows = connection.execute(
                "SELECT field_name, field_value FROM metadata_values "
                "WHERE candidate_id = ?",
                (candidate_id,),
            ).fetchall()
        return [
            type("MetaRow", (), {"field_name": r[0], "field_value": r[1]})
            for r in rows
        ]

    def _record_cbz_artifact_sync(
        self,
        job_id: int,
        destination: Path,
        page_count: int,
        library_path: Path | None = None,
    ) -> None:
        """Record the packed CBZ the way the download path records an archive.

        `size_bytes` used to receive `page_count`, so every packed CBZ reported
        a size of a few dozen bytes and the column could not be compared
        against the archive row it was produced from. It now carries the real
        file size, and the digest is computed the same way — streamed in
        chunks, because a CBZ is arbitrarily large and must not be read into
        memory to be hashed.
        """
        sha256 = hashlib.sha256()
        with destination.open("rb") as handle:
            while True:
                chunk = handle.read(64 * 1024)
                if not chunk:
                    break
                sha256.update(chunk)
        # Recorded on *every* pack, not only after a rename. The column arrived
        # in 014 for the rename case alone, which left a freshly packed book with
        # no answer to 「相对于书库它在哪里」 -- so the archive-path form on the
        # detail page had nothing to prefill its 目录 field with, and an operator
        # editing only the filename would have submitted an empty directory and
        # moved the book to the library root. Deriving it here is also the only
        # place that can: `library_path` is the effective root for this job.
        relative: str | None = None
        if library_path is not None:
            try:
                relative = destination.resolve().relative_to(
                    library_path.resolve()
                ).as_posix()
            except (OSError, ValueError):
                # A destination outside the library is already impossible by the
                # time we get here, but a root that cannot be resolved must not
                # fail a pack that has otherwise succeeded.
                relative = None
        with self._database.connection() as connection:
            connection.execute(
                "INSERT INTO artifacts "
                "(job_id, artifact_type, path, sha256, size_bytes, "
                "page_count, library_relative_path) "
                "VALUES (?, 'CBZ', ?, ?, ?, ?, ?) "
                "ON CONFLICT(job_id, artifact_type) DO UPDATE SET "
                "path = excluded.path, sha256 = excluded.sha256, "
                "size_bytes = excluded.size_bytes, "
                "page_count = excluded.page_count, "
                "library_relative_path = excluded.library_relative_path",
                (
                    job_id,
                    str(destination),
                    sha256.hexdigest(),
                    destination.stat().st_size,
                    int(page_count),
                    relative,
                ),
            )

    def _mark_completed_sync(self, job_id: int, details: dict) -> None:
        with self._database.connection() as connection:
            connection.execute(
                "UPDATE download_jobs SET state = ?, error_code = NULL, "
                "error_message = NULL, details_json = ?, "
                "updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                (
                    CONVERSION_STATE_COMPLETED,
                    json.dumps(details, ensure_ascii=False, separators=(",", ":")),
                    job_id,
                ),
            )

    def _mark_waiting_sync(
        self,
        job_id: int,
        state: str,
        code: str,
        message: str,
        details: dict,
    ) -> None:
        """Park a task in a recoverable state without losing its snapshot."""
        with self._database.connection() as connection:
            connection.execute(
                "UPDATE download_jobs SET state = ?, error_code = ?, "
                "error_message = ?, details_json = ?, "
                "updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                (
                    state,
                    code,
                    message,
                    json.dumps(details, ensure_ascii=False, separators=(",", ":")),
                    job_id,
                ),
            )

    def _mark_failed_sync(
        self, job_id: int, code: str, message: str
    ) -> None:
        with self._database.connection() as connection:
            connection.execute(
                "UPDATE download_jobs SET state = ?, error_code = ?, "
                "error_message = ?, updated_at = CURRENT_TIMESTAMP "
                "WHERE id = ?",
                (CONVERSION_STATE_FAILED, code, message, job_id),
            )

    def _read_final_state_sync(
        self, job_id: int
    ) -> tuple[str, str | None, str | None]:
        """Read the row back so the worker loop can log the real outcome.

        `_handle_job` returns normally whether the job packed, parked or
        failed; the only signal in-band is the row itself. Reading it here
        keeps the terminal log line in lock-step with what the page is about
        to render.
        """
        with self._database.connection() as connection:
            row = connection.execute(
                "SELECT state, error_code, error_message "
                "FROM download_jobs WHERE id = ?",
                (job_id,),
            ).fetchone()
        if row is None:
            return CONVERSION_STATE_FAILED, None, "job row vanished"
        return str(row[0]), row[1], row[2]


__all__ = [
    "ConversionError",
    "ConversionService",
    "CONVERSION_STATE_PENDING",
    "CONVERSION_STATE_RUNNING",
    "CONVERSION_STATE_COMPLETED",
    "CONVERSION_STATE_FAILED",
    "CONVERSION_STATE_WAITING_PASSWORD",
    "CONVERSION_STATE_WAITING_PATH",
    "CONVERSION_STATE_WAITING_VOLUMES",
    "RECOVERABLE_CONVERSION_STATES",
]
