from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class DownloadState(str, Enum):
    PENDING = "PENDING"
    DOWNLOADING = "DOWNLOADING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


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
