from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
import time


class DownloadState(str, Enum):
    PENDING = "PENDING"
    DOWNLOADING = "DOWNLOADING"
    #: Pushed to qBittorrent and waiting on peers. The worker does not hold a
    #: lease in this state; a separate poller advances it.
    WAITING_TORRENT = "WAITING_TORRENT"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"
    PAUSED = "PAUSED"


PROVIDER_TELEGRAM = "TELEGRAM"
PROVIDER_EXHENTAI = "EXHENTAI"
PROVIDER_TELEGRAPH = "TELEGRAPH"
PROVIDER_EH_TORRENT = "EH_TORRENT"

#: Packaging. Deliberately *not* in `SUPPORTED_PROVIDERS`: it shares the
#: `download_jobs` table but the download worker must never claim one, and the
#: conversion worker claims nothing else. It sat as a bare `'CONVERSION'` inside
#: three SQL statements while its four siblings had names, which is what let the
#: two queues read as one list.
PROVIDER_CONVERSION = "CONVERSION"

#: Providers the worker claims work for. `_claim_pending_job_sync` expands this
#: into its placeholder list, so a provider added here is picked up; a provider
#: added anywhere else would have its jobs silently left in PENDING forever.
SUPPORTED_PROVIDERS: tuple[str, ...] = (
    PROVIDER_TELEGRAM,
    PROVIDER_EXHENTAI,
    PROVIDER_TELEGRAPH,
    PROVIDER_EH_TORRENT,
)

TERMINAL_DOWNLOAD_STATES: frozenset[str] = frozenset(
    {
        DownloadState.COMPLETED.value,
        DownloadState.FAILED.value,
        DownloadState.CANCELLED.value,
    }
)

#: States that count against concurrency. WAITING_TORRENT is absent on
#: purpose: the transfer is the client's work, not this process's, so a job
#: parked on peers must not occupy a download slot.
ACTIVE_DOWNLOAD_STATES: frozenset[str] = frozenset(
    {DownloadState.PENDING.value, DownloadState.DOWNLOADING.value}
)

#: States the operator can still act on, so the dashboard keeps showing them.
OPEN_DOWNLOAD_STATES: frozenset[str] = frozenset(
    {
        DownloadState.PENDING.value,
        DownloadState.DOWNLOADING.value,
        DownloadState.WAITING_TORRENT.value,
        DownloadState.PAUSED.value,
        DownloadState.FAILED.value,
    }
)

#: Failures that mean the operator has to supply something, not that the job
#: is broken. The candidate parks in NEEDS_INFO with the reason on display and
#: stays reviewable, instead of being buried in the failed queue.
NEEDS_INFO_DOWNLOAD_ERRORS: frozenset[str] = frozenset(
    {"TELEGRAPH_PAGE_COUNT_MISMATCH"}
)

#: Packaging states. They live here rather than in `app.conversion.service`
#: because they are values of `download_jobs.state`, the same column the
#: download states above occupy, and because the queue grouping below has to
#: classify both kinds of job without importing the conversion worker (which
#: imports this module). `app.conversion.service` re-exports these names, so
#: existing `from app.conversion.service import CONVERSION_STATE_*` keeps
#: resolving to the one definition here.
CONVERSION_STATE_PENDING = "CONVERSION_PENDING"
CONVERSION_STATE_RUNNING = "CONVERSION_RUNNING"
CONVERSION_STATE_COMPLETED = "CONVERSION_COMPLETED"
CONVERSION_STATE_FAILED = "CONVERSION_FAILED"
#: Recoverable states: the operator can supply the missing volume or password
#: and requeue the same task without losing the backend snapshot.
CONVERSION_STATE_WAITING_VOLUMES = "CONVERSION_WAITING_VOLUMES"
CONVERSION_STATE_WAITING_PASSWORD = "CONVERSION_WAITING_PASSWORD"

RECOVERABLE_CONVERSION_STATES: frozenset[str] = frozenset(
    {
        CONVERSION_STATE_WAITING_VOLUMES,
        CONVERSION_STATE_WAITING_PASSWORD,
    }
)

#: Queue ordering. Lower runs first; the default leaves room to promote and to
#: demote without renumbering the rest of the queue. The bounds exist so the
#: interface can offer a slider and the API can refuse a value that would sort
#: ahead of, or behind, everything forever.
DEFAULT_JOB_PRIORITY = 100
MIN_JOB_PRIORITY = 1
MAX_JOB_PRIORITY = 999

#: The four buckets the queue is shown in. Which one a job lands in is decided
#: by `DownloadJobSummary.queue_group`; the Chinese labels live in
#: `app.api.status` with every other piece of display vocabulary.
QUEUE_GROUP_ACTIVE = "active"
QUEUE_GROUP_WAITING = "waiting"
QUEUE_GROUP_ATTENTION = "attention"
QUEUE_GROUP_PAUSED = "paused"

#: Display order, not alphabetical: an operator scans top-down and the things
#: that need them come before the things that do not.
QUEUE_GROUPS: tuple[str, ...] = (
    QUEUE_GROUP_ATTENTION,
    QUEUE_GROUP_ACTIVE,
    QUEUE_GROUP_WAITING,
    QUEUE_GROUP_PAUSED,
)

#: Why a job needs the operator. Each is something they can act on, which is
#: what separates these from a generic failure: a missing volume or password is
#: an ask, and a stalled torrent is a decision (wait, or switch source).
ATTENTION_MISSING_VOLUMES = "MISSING_VOLUMES"
ATTENTION_MISSING_PASSWORD = "MISSING_PASSWORD"
ATTENTION_MISSING_PAGES = "MISSING_PAGES"
ATTENTION_STALLED_TORRENT = "STALLED_TORRENT"
ATTENTION_FAILED = "FAILED"

#: Error codes that can never succeed on a retry, so the UI hides the button.
#: `TORRENT_CONTENT_UNREACHABLE` / `TORRENT_CONTENT_UNEXPECTED` are deliberately
#: absent: a readability failure on the saved path is usually the operator's
#: misconfigured path being fixed, after which the retry can re-verify the file
#: and succeed without a fresh push.
PERMANENT_DOWNLOAD_ERRORS: frozenset[str] = frozenset(
    {
        "TELEGRAM_FILE_TOO_BIG",
        "ATTACHMENT_INVALID",
        "PROVIDER_UNSUPPORTED",
        "CANDIDATE_NOT_DOWNLOADABLE",
        "TELEGRAPH_NO_IMAGES",
        "TELEGRAPH_IMAGE_BLOCKED",
        "TELEGRAPH_LIMIT_EXCEEDED",
        "TORRENT_CLIENT_NOT_CONFIG",
        "TORRENT_CLIENT_AUTH",
        "TORRENT_NOT_AVAILABLE",
        "TORRENT_FILE_INVALID",
    }
)


@dataclass(frozen=True, slots=True)
class DownloadJobSummary:
    job_id: int
    candidate_id: int
    provider: str
    state: str
    attempt_count: int
    error_code: str | None
    error_message: str | None
    artifact_path: str | None
    artifact_size: int | None
    created_at: str
    updated_at: str
    #: Provider-specific progress. Only the torrent route writes anything the
    #: dashboard reads, and it is a dict rather than columns because the shape
    #: belongs to the provider, not to the queue.
    details: dict = field(default_factory=dict)
    #: A conversion job's finished CBZ, read from the artifact table so the
    #: detail page can show the packaging output next to the download source.
    artifact_cbz_path: str | None = None
    #: Queue position. Both workers claim `ORDER BY priority, id`, so this is
    #: the only thing an operator can change to reorder pending work.
    priority: int = DEFAULT_JOB_PRIORITY

    @property
    def is_retryable(self) -> bool:
        """A failed or paused job can be requeued unless the cause is permanent."""
        if self.state == DownloadState.PAUSED.value:
            return True
        if self.state != DownloadState.FAILED.value:
            return False
        return self.error_code not in PERMANENT_DOWNLOAD_ERRORS

    @property
    def is_pausable(self) -> bool:
        """Only a job that has not been claimed yet can be held back."""
        return self.state == DownloadState.PENDING.value

    @property
    def is_cancellable(self) -> bool:
        return self.state in {
            DownloadState.PENDING.value,
            DownloadState.DOWNLOADING.value,
            DownloadState.WAITING_TORRENT.value,
            DownloadState.PAUSED.value,
            DownloadState.FAILED.value,
        }

    @property
    def is_waiting_for_peers(self) -> bool:
        return self.state == DownloadState.WAITING_TORRENT.value

    @property
    def progress_percent(self) -> int:
        try:
            return int(round(float(self.details.get("progress") or 0.0) * 100))
        except (TypeError, ValueError):
            return 0

    @property
    def is_seeding(self) -> bool:
        """A finished torrent whose payload the client is still sharing.

        Only meaningful once the job is COMPLETED: before that the client is
        still fetching, and `is_waiting_for_peers` is the state to read.
        """
        return (
            self.state == DownloadState.COMPLETED.value
            and self.provider == PROVIDER_EH_TORRENT
            and bool(self.details.get("seeding"))
        )

    @property
    def was_already_in_client(self) -> bool:
        """The push found this infohash already registered in qBittorrent.

        Not a failure, but it means the entry EhBot reads from was created by
        someone else, so its save path may differ from the configured one.
        """
        return bool(self.details.get("was_already_in_client"))

    @property
    def upload_speed(self) -> int:
        try:
            return int(self.details.get("upspeed") or 0)
        except (TypeError, ValueError):
            return 0

    @property
    def download_speed(self) -> int:
        """Bytes per second the client is currently pulling, 0 if unknown."""
        try:
            return int(self.details.get("dlspeed") or 0)
        except (TypeError, ValueError):
            return 0

    @property
    def num_seeds(self) -> int:
        """Seeders the client can see. Zero is the number that matters here."""
        try:
            return int(self.details.get("num_seeds") or 0)
        except (TypeError, ValueError):
            return 0

    @property
    def eta_seconds(self) -> int | None:
        """The client's estimate, or None when it has not made one.

        qBittorrent reports 8640000 for「unknown」rather than omitting the field,
        so that sentinel is treated as no estimate: showing「剩 100000 分」would
        be worse than showing nothing.
        """
        raw = self.details.get("eta")
        try:
            value = int(raw)
        except (TypeError, ValueError):
            return None
        if value <= 0 or value >= 8640000:
            return None
        return value

    @property
    def torrent_state(self) -> str:
        """The client's own state name, e.g. `downloading` or `stalledUP`."""
        return str(self.details.get("state") or "")

    @property
    def stalled_minutes(self) -> int | None:
        """How long the torrent has had no seeder, or None if it has one.

        A stall is deliberately not a failure, so this is the number the
        operator uses to decide between waiting and switching sources.
        """
        since = self.details.get("stalled_since")
        if not isinstance(since, (int, float)):
            return None
        return max(0, int((time.time() - float(since)) // 60))

    @property
    def attention_reason(self) -> str | None:
        """What the operator has to do about this job, or None if nothing.

        Ordered by specificity: a missing password is a more useful thing to
        read than「失败」, so the specific asks are tested before the generic
        failure. A stalled torrent appears here without being FAILED anywhere --
        the swarm may still deliver it, and marking it failed would both lose
        the transfer and lie about what happened.
        """
        if self.state == CONVERSION_STATE_WAITING_VOLUMES:
            return ATTENTION_MISSING_VOLUMES
        if self.state == CONVERSION_STATE_WAITING_PASSWORD:
            return ATTENTION_MISSING_PASSWORD
        if self.state in {
            DownloadState.FAILED.value,
            CONVERSION_STATE_FAILED,
        }:
            if self.error_code in NEEDS_INFO_DOWNLOAD_ERRORS:
                return ATTENTION_MISSING_PAGES
            return ATTENTION_FAILED
        if self.is_waiting_for_peers and self.stalled_minutes is not None:
            return ATTENTION_STALLED_TORRENT
        return None

    @property
    def queue_group(self) -> str:
        """Which of the four queue sections this job belongs in.

        Attention is tested first so a stalled or parked job is never filed
        under「进行中」where an operator would scroll past it. Everything else
        follows from the state: work happening now, work queued behind it, and
        work the operator has held back.
        """
        if self.attention_reason is not None:
            return QUEUE_GROUP_ATTENTION
        if self.state == DownloadState.PAUSED.value:
            return QUEUE_GROUP_PAUSED
        if self.state in {
            DownloadState.PENDING.value,
            CONVERSION_STATE_PENDING,
        }:
            return QUEUE_GROUP_WAITING
        # DOWNLOADING, WAITING_TORRENT with a live peer, CONVERSION_RUNNING and
        # a COMPLETED torrent still seeding: in all four something is moving,
        # whether this process is doing it or the torrent client is.
        return QUEUE_GROUP_ACTIVE


@dataclass(frozen=True, slots=True)
class DownloadEnqueueResult:
    job_id: int
    created: bool
    state: str


DOWNLOAD_STATE_PENDING = "PENDING"
DOWNLOAD_STATE_DOWNLOADING = "DOWNLOADING"
DOWNLOAD_STATE_WAITING_TORRENT = "WAITING_TORRENT"
DOWNLOAD_STATE_COMPLETED = "COMPLETED"
DOWNLOAD_STATE_FAILED = "FAILED"
DOWNLOAD_STATE_CANCELLED = "CANCELLED"
DOWNLOAD_STATE_PAUSED = "PAUSED"
