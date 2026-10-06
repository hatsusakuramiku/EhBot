"""Approval orchestration shared by the page and JSON layers.

Approving a candidate is two coupled steps -- a status transition and an
enqueue on the routed source -- plus the rule that decides which source. That
logic lived inside `create_app` as a closure, so the JSON API could not reach
it without either importing `main` or reimplementing it. A second
implementation is exactly how the two layers would drift into approving
candidates the other would refuse, so it moves here and both call it.

Source routing is quality first, cost second, and ExHentai Archive Download is
deliberately never routed automatically: it spends GP, and spending a limited
resource stays an explicit operator decision.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
import logging

from app.auto_approval.models import AutoApprovalMatch
from app.auto_approval.service import AutomaticApprovalService
from app.downloads.models import (
    AUTO_DOWNLOAD_PROVIDERS,
    PROVIDER_EH_TORRENT,
    PROVIDER_EXHENTAI,
    PROVIDER_TELEGRAM,
    PROVIDER_TELEGRAM_USER,
    PROVIDER_TELEGRAPH,
)
from app.downloads.service import DownloadError
from app.review.models import (
    AUTO_OPERATOR,
    REVIEWABLE_STATUSES,
    REVIEW_APPROVE,
    REVIEW_AUTO_APPROVE,
    REVIEW_AUTO_REJECT,
    REVIEW_REJECT,
)
from app.review.service import ReviewError, ReviewService


LOGGER = logging.getLogger(__name__)

#: Bot API `getFile` ceiling for a single attachment. Above this the Telegram
#: route cannot serve the file at all, so routing skips it rather than
#: enqueueing a job that is guaranteed to fail.
TELEGRAM_FILE_LIMIT = 20 * 1024 * 1024

#: Re-exported so existing importers keep working; the name itself now lives in
#: `app.review.models`, beside the actions it signs, because the timeline has to
#: resolve it into 「自动规则」 and a second copy of the string would be a second
#: thing to keep in step.


@dataclass(frozen=True, slots=True)
class RoutedSource:
    """The provider chosen for a candidate, and the attachment if any."""

    provider: str | None
    attachment: dict | None = None

    @property
    def is_downloadable(self) -> bool:
        return self.provider is not None


class ReviewOrchestrator:
    """Coordinates review transitions with download enqueueing.

    Provider availability is supplied as callables rather than booleans because
    the torrent and Telegraph services are attached during lifespan startup:
    a value captured at construction time would report them missing forever.
    """

    def __init__(
        self,
        database,
        download_service: Callable[[], object],
        *,
        torrent_available: Callable[[], bool],
        telegraph_available: Callable[[], bool],
        telegram_user_available: Callable[[], bool] | None = None,
        telegram_available: Callable[[], bool] | None = None,
        download_source_priority: Callable[[], tuple[str, ...]] | None = None,
    ) -> None:
        self._database = database
        self._download_service = download_service
        self._torrent_available = torrent_available
        self._telegraph_available = telegraph_available
        # Defaulted so every existing construction of this class keeps working
        # and reads as「no user account」, which is what a deployment that never
        # configures one has.
        self._telegram_user_available = (
            telegram_user_available if telegram_user_available else lambda: False
        )
        # Defaulted to 「available」 rather than to False: a caller that never
        # wires the bot's health is every test and every embedding that only
        # cares about the other three sources, and refusing to route to the bot
        # for them would be a silent behaviour change dressed as a safety check.
        self._telegram_available = (
            telegram_available if telegram_available else lambda: True
        )
        self._download_source_priority = (
            download_source_priority
            if download_source_priority
            else lambda: AUTO_DOWNLOAD_PROVIDERS
        )

    def _review_service(self) -> ReviewService:
        return ReviewService(self._database)

    def route_source(self, candidate) -> RoutedSource:
        """Pick the first usable source in the operator's configured order.

        The order is a preference, not a guarantee: each provider is asked
        whether it *can* serve this particular candidate, and a provider that
        cannot is skipped rather than reported as the answer. That is what makes
        the same list work for a book with an attachment and a book that only
        has a preview link -- and what lets a deployment with a broken bot fall
        through to the user account instead of queueing a job that can only
        fail.

        The four eligibilities, all cheap reads of data already on the
        candidate:

        * `TELEGRAM` -- some archive attachment is at or under the Bot API
          ceiling *and* carries a `file_id`. Only the bot mints those, so an
          attachment ingested over MTProto (or one predating a removed token)
          is not something this route can fetch.
        * `TELEGRAM_USER` -- any archive attachment, because MTProto re-reads
          the message by `(chat_id, message_id)` and has no ceiling. It also
          needs the stored session to be valid right now, which is the one
          thing the connection manager tracks.
        * `EH_TORRENT` -- gdata reported a torrent and a client is configured.
        * `TELEGRAPH` -- the message carried a preview page.
        """
        archives = [
            item
            for message in candidate.messages
            for item in message.attachments
            if item.get("type") == "archive"
        ]
        for provider in self._download_source_priority():
            if provider == PROVIDER_TELEGRAM:
                attachment = self._bot_fetchable_attachment(archives)
                if attachment is not None and self._telegram_available():
                    return RoutedSource(PROVIDER_TELEGRAM, attachment)
            elif provider == PROVIDER_TELEGRAM_USER:
                if archives and self._telegram_user_available():
                    return RoutedSource(PROVIDER_TELEGRAM_USER, archives[0])
            elif provider == PROVIDER_EH_TORRENT:
                if candidate.torrent_hash and self._torrent_available():
                    return RoutedSource(PROVIDER_EH_TORRENT)
            elif provider == PROVIDER_TELEGRAPH:
                if candidate.preview_url and self._telegraph_available():
                    return RoutedSource(PROVIDER_TELEGRAPH)
        return RoutedSource(None)

    @staticmethod
    def _bot_fetchable_attachment(archives: list[dict]) -> dict | None:
        """The attachment the Bot API could actually download, if any.

        Two conditions, both required: under the 20 MB ceiling, and carrying a
        `file_id`. The id is the stricter of the two -- it exists only on
        attachments the Bot API itself reported, so an MTProto-only deployment
        can never route to a provider it does not have.
        """
        return next(
            (
                item
                for item in archives
                if item.get("file_id")
                and int(item.get("size_bytes") or 0) <= TELEGRAM_FILE_LIMIT
            ),
            None,
        )

    async def _load_reviewable(self, candidate_id: int):
        """Fetch a candidate and assert it is in a reviewable state."""
        candidate = await self._database.get_candidate(candidate_id)
        if candidate is None:
            raise ReviewError(
                "CANDIDATE_NOT_FOUND", "候选不存在或已被删除"
            )
        if candidate.status not in REVIEWABLE_STATUSES:
            raise ReviewError(
                "REVIEW_INVALID_TRANSITION",
                f"候选 #{candidate_id} 当前状态不可审核",
            )
        return candidate

    async def approve_and_enqueue(
        self, candidate_ids: list[int], operator: str
    ) -> tuple[int, ...]:
        """Approve every candidate, then enqueue its download.

        Both loops are separate on purpose: every candidate is validated and
        routed before anything is written, so a batch containing one
        unroutable item fails without having half-approved the rest.
        """
        targets: list[tuple[int, RoutedSource]] = []
        for candidate_id in candidate_ids:
            candidate = await self._load_reviewable(candidate_id)
            routed = self.route_source(candidate)
            if not routed.is_downloadable:
                raise ReviewError(
                    "CANDIDATE_NOT_DOWNLOADABLE",
                    f"候选 #{candidate_id} 没有可用的下载来源",
                )
            targets.append((candidate_id, routed))

        job_ids: list[int] = []
        for candidate_id, routed in targets:
            await self._review_service().approve_candidate(
                candidate_id, operator
            )
            try:
                job_ids.append(await self._enqueue(candidate_id, routed))
            except DownloadError as exc:
                # Re-raised as a ReviewError so the caller has one exception
                # type to translate, while keeping the original code and
                # operator-facing message.
                raise ReviewError(exc.code, exc.public_message) from exc
        return tuple(job_ids)

    async def _enqueue(self, candidate_id: int, routed: RoutedSource) -> int:
        service = self._download_service()
        if routed.provider == PROVIDER_TELEGRAM:
            result = await service.enqueue_telegram_download(
                candidate_id, routed.attachment or {}
            )
        elif routed.provider == PROVIDER_TELEGRAM_USER:
            result = await service.enqueue_telegram_user_download(
                candidate_id, routed.attachment or {}
            )
        elif routed.provider == PROVIDER_EH_TORRENT:
            result = await service.enqueue_torrent_download(candidate_id)
        elif routed.provider == PROVIDER_TELEGRAPH:
            result = await service.enqueue_telegraph_download(candidate_id)
        elif routed.provider == PROVIDER_EXHENTAI:
            result = await service.enqueue_exhentai_download(candidate_id)
        else:
            # Reached only if a provider is added to routing without a branch
            # here. Failing loudly beats silently enqueueing the wrong source.
            raise ReviewError(
                "PROVIDER_UNSUPPORTED",
                f"\u4e0d\u652f\u6301\u7684\u4e0b\u8f7d\u6765\u6e90\uff1a{routed.provider}",
            )
        return result.job_id

    async def reject(
        self,
        candidate_ids: list[int],
        operator: str,
        note: str | None = None,
    ) -> None:
        """Reject a batch, validating all of it before writing any of it.

        `note` is optional so the manual batch path is unchanged; automatic
        rejection passes the rule it matched so the rejected row can explain
        itself without opening the timeline.
        """
        for candidate_id in candidate_ids:
            await self._load_reviewable(candidate_id)
        for candidate_id in candidate_ids:
            await self._review_service().reject_candidate(
                candidate_id, operator, note
            )

    async def apply_automatic_decision(self, candidate_id: int) -> str | None:
        """Apply the first matching automatic rule to a candidate.

        Returns the action taken -- `REVIEW_APPROVE` or `REVIEW_REJECT` -- or
        `None` when no rule matched, so the sweeper can count the two kinds
        apart. Declines rather than raising when the decision cannot proceed:
        automatic rules are an optimisation, and a candidate they decline
        simply stays in the queue for a human.

        Approve and reject share one rule pool and one priority order --
        `matching_rule` already returns the first enabled match -- so this
        method only dispatches on the winner's action. It never applies a
        second rule: a candidate is decided by exactly one rule or by nobody.
        """
        match = await AutomaticApprovalService(self._database).matching_rule(
            candidate_id
        )
        if match is None:
            return None
        if match.rule.action == REVIEW_REJECT:
            # The rule name goes on the candidate as its filter_reason, so the
            # 「已驳回」 row says why it was rejected. Manual rejection passes no
            # note and keeps its empty reason, exactly as before.
            try:
                await self.reject(
                    [candidate_id],
                    AUTO_OPERATOR,
                    f"命中规则「{match.rule.name}」",
                )
            except ReviewError as exc:
                LOGGER.info(
                    "auto_reject_skipped candidate=%d error=%s",
                    candidate_id,
                    exc.public_message,
                )
                return None
            await self._record_automatic_decision(
                candidate_id,
                REVIEW_AUTO_REJECT,
                match,
                download_job_ids=[],
            )
            return REVIEW_REJECT
        try:
            job_ids = await self.approve_and_enqueue(
                [candidate_id], AUTO_OPERATOR
            )
        except ReviewError as exc:
            LOGGER.info(
                "auto_approval_skipped candidate=%d error=%s",
                candidate_id,
                exc.public_message,
            )
            return None
        await self._record_automatic_decision(
            candidate_id,
            REVIEW_AUTO_APPROVE,
            match,
            download_job_ids=list(job_ids),
        )
        return REVIEW_APPROVE

    async def _record_automatic_decision(
        self,
        candidate_id: int,
        action: str,
        match: AutoApprovalMatch,
        *,
        download_job_ids: list[int],
    ) -> None:
        """Record one rule-driven decision against the rule as it was.

        The full rule snapshot is stored so a later dispute can be settled
        against the rule at the version that fired, not as it has since been
        edited. Both actions write the same keys; a rejection's job list is
        empty because there is nothing to download.
        """
        await self._database.record_review_action(
            candidate_id,
            action,
            AUTO_OPERATOR,
            {
                "rule_id": match.rule.rule_id,
                "rule_name": match.rule.name,
                "rule_version": match.rule.version,
                "dsl_snapshot": match.rule.dsl_snapshot,
                "condition": match.rule.condition,
                "conditions": match.conditions,
                "metadata": match.metadata,
                "download_job_ids": download_job_ids,
            },
        )


__all__ = [
    "AUTO_OPERATOR",
    "TELEGRAM_FILE_LIMIT",
    "ReviewOrchestrator",
    "RoutedSource",
]
