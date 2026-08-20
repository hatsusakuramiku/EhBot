from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class DownloadState(str, Enum):
    PENDING = "PENDING"
    DOWNLOADING = "DOWNLOADING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"
    PAUSED = "PAUSED"


PROVIDER_TELEGRAM = "TELEGRAM"
PROVIDER_EXHENTAI = "EXHENTAI"

SUPPORTED_PROVIDERS: tuple[str, ...] = (PROVIDER_TELEGRAM, PROVIDER_EXHENTAI)

TERMINAL_DOWNLOAD_STATES: frozenset[str] = frozenset(
    {
        DownloadState.COMPLETED.value,
        DownloadState.FAILED.value,
        DownloadState.CANCELLED.value,
    }
)

ACTIVE_DOWNLOAD_STATES: frozenset[str] = frozenset(
    {DownloadState.PENDING.value, DownloadState.DOWNLOADING.value}
)

#: States the operator can still act on, so the dashboard keeps showing them.
OPEN_DOWNLOAD_STATES: frozenset[str] = frozenset(
    {
        DownloadState.PENDING.value,
        DownloadState.DOWNLOADING.value,
        DownloadState.PAUSED.value,
        DownloadState.FAILED.value,
    }
)

#: Error codes that can never succeed on a retry, so the UI hides the button.
PERMANENT_DOWNLOAD_ERRORS: frozenset[str] = frozenset(
    {
        "TELEGRAM_FILE_TOO_BIG",
        "ATTACHMENT_INVALID",
        "PROVIDER_UNSUPPORTED",
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
            DownloadState.PAUSED.value,
            DownloadState.FAILED.value,
        }


@dataclass(frozen=True, slots=True)
class DownloadEnqueueResult:
    job_id: int
    created: bool
    state: str


DOWNLOAD_STATE_PENDING = "PENDING"
DOWNLOAD_STATE_DOWNLOADING = "DOWNLOADING"
DOWNLOAD_STATE_COMPLETED = "COMPLETED"
DOWNLOAD_STATE_FAILED = "FAILED"
DOWNLOAD_STATE_CANCELLED = "CANCELLED"
DOWNLOAD_STATE_PAUSED = "PAUSED"
