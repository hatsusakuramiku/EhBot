"""Data shapes for the EH torrent download route."""

from __future__ import annotations

from dataclasses import dataclass


class TorrentError(ValueError):
    """A failure the download queue can record verbatim.

    Carries `code` and `public_message` like every other provider error, so the
    worker records the outcome without knowing which provider produced it.
    """

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.public_message = message


@dataclass(frozen=True, slots=True)
class TorrentClientConfig:
    """How to reach the operator's qBittorrent instance.

    `save_path` is the client's view of the download directory and travels with
    the add request; `local_save_path` is EhBot's view of the same directory and
    is used to read the finished payload. They differ whenever the client runs
    in another container or on a NAS.
    """

    base_url: str
    username: str
    password: str
    category: str = "ehbot"
    save_path: str = ""
    local_save_path: str = ""
    keep_seeding: bool = True
    #: Publish the finished payload to the library without an operator pressing
    #: anything. Off by default: packing is the step that makes a book public,
    #: so it stays a decision rather than a side effect of the download.
    auto_pack: bool = False

    @property
    def is_configured(self) -> bool:
        return bool(self.base_url)


@dataclass(frozen=True, slots=True)
class TorrentStatus:
    """One qBittorrent `torrents/info` row, reduced to what the UI needs."""

    hash: str
    state: str
    progress: float
    num_seeds: int
    dlspeed: int
    eta: int | None
    content_path: str
    size: int
    #: Trails the required fields with a default because it matters only after
    #: the download finishes, when the client switches to sharing the payload.
    upspeed: int = 0

    #: States where the client is trying but has nothing to talk to.
    STALLED_STATES = frozenset({"stalledDL", "metaDL"})
    #: States where the payload is complete.
    DONE_STATES = frozenset(
        {
            "uploading",
            "stalledUP",
            "queuedUP",
            "forcedUP",
            "pausedUP",
            "stoppedUP",
            "checkingUP",
        }
    )
    #: States the client cannot recover from on its own.
    ERROR_STATES = frozenset({"error", "missingFiles"})

    @property
    def is_complete(self) -> bool:
        return self.progress >= 1.0 or self.state in self.DONE_STATES

    @property
    def is_failed(self) -> bool:
        return self.state in self.ERROR_STATES

    @property
    def is_stalled(self) -> bool:
        """No seeder in sight.

        Deliberately not an error: an EH torrent with no seeder may pick one up
        hours later, so the job waits and shows the stall instead of failing or
        silently degrading to preview grade.
        """
        return self.state in self.STALLED_STATES and self.num_seeds == 0


@dataclass(frozen=True, slots=True)
class TorrentDelivery:
    """What taking delivery of a finished torrent produced."""

    archive_path: str
    size_bytes: int
    was_directory: bool


__all__ = [
    "TorrentClientConfig",
    "TorrentDelivery",
    "TorrentError",
    "TorrentStatus",
]