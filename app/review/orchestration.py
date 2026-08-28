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

from app.auto_approval.service import AutomaticApprovalService
from app.downloads.models import (
    PROVIDER_EH_TORRENT,
    PROVIDER_EXHENTAI,
    PROVIDER_TELEGRAM,
    PROVIDER_TELEGRAM_USER,
    PROVIDER_TELEGRAPH,
)
from app.downloads.service import DownloadError
from app.review.models import AUTO_OPERATOR, REVIEWABLE_STATUSES
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

    def _review_service(self) -> ReviewService:
        return ReviewService(self._database)

    def route_source(self, candidate) -> RoutedSource:
        """Pick the best available source for a candidate.

        1. `TELEGRAM` -- the uploader's own archive over the Bot API: original
           quality, free, and no extra credential, but only up to 20 MB.
        2. `TELEGRAM_USER` -- the *same* archive over MTProto, when a user
           account is logged in. Preferred over the torrent for an oversized
           book: it is the identical file the uploader posted, needs no swarm,
           and cannot stall on peers.
        3. `EH_TORRENT` -- original quality and free, whenever gdata reported a
           torrent and a client is configured.
        4. `TELEGRAPH` -- the preview page: complete, but re-encoded to 1280 px
           and therefore a fraction of the original bytes. Last resort.
        """
        archives = [
            item
            for message in candidate.messages
            for item in message.attachments
            if item.get("type") == "archive"
        ]
        attachment = next(
            (
                item
                for item in archives
                if int(item.get("size_bytes") or 0) <= TELEGRAM_FILE_LIMIT
            ),
            None,
        )
        if attachment is not None:
            return RoutedSource(PROVIDER_TELEGRAM, attachment)
        # Above the Bot API ceiling the bytes are still right there in the
        # channel; only the protocol was the problem. An oversized attachment
        # therefore stops being the reason to fall back to a re-encode.
        if archives and self._telegram_user_available():
            return RoutedSource(PROVIDER_TELEGRAM_USER, archives[0])
        if candidate.torrent_hash and self._torrent_available():
            return RoutedSource(PROVIDER_EH_TORRENT)
        if candidate.preview_url and self._telegraph_available():
            return RoutedSource(PROVIDER_TELEGRAPH)
        return RoutedSource(None)

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

    async def reject(self, candidate_ids: list[int], operator: str) -> None:
        """Reject a batch, validating all of it before writing any of it."""
        for candidate_id in candidate_ids:
            await self._load_reviewable(candidate_id)
        for candidate_id in candidate_ids:
            await self._review_service().reject_candidate(
                candidate_id, operator
            )

    async def apply_automatic_approval(self, candidate_id: int) -> bool:
        """Approve a candidate if a rule matches it.

        Returns False rather than raising when no rule matches or the approval
        cannot proceed: automatic approval is an optimisation, and a candidate
        it declines simply stays in the queue for a human.
        """
        match = await AutomaticApprovalService(self._database).matching_rule(
            candidate_id
        )
        if match is None:
            return False
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
            return False
        # The full rule snapshot is recorded so a later dispute can be settled
        # against the rule as it was, not as it has since been edited.
        await self._database.record_review_action(
            candidate_id,
            "AUTO_APPROVE",
            AUTO_OPERATOR,
            {
                "rule_id": match.rule.rule_id,
                "rule_name": match.rule.name,
                "rule_version": match.rule.version,
                "dsl_snapshot": match.rule.dsl_snapshot,
                "condition": match.rule.condition,
                "conditions": match.conditions,
                "metadata": match.metadata,
                "download_job_ids": list(job_ids),
            },
        )
        return True


__all__ = [
    "AUTO_OPERATOR",
    "TELEGRAM_FILE_LIMIT",
    "ReviewOrchestrator",
    "RoutedSource",
]
