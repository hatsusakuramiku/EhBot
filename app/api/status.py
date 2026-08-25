"""Single source of truth for turning a state enum into display vocabulary.

The download page currently prints ``{{ job.state }}``, so an operator reads
``WAITING_TORRENT`` instead of 「等待做种」. Labels also lived inline in
`create_app`, which meant the API layer had no way to reuse them. Both the
templates and the JSON endpoints now read this module, so a state is described
identically wherever it appears.

``tone`` is a semantic name, not a colour: the stylesheet decides what
``waiting`` looks like, and a theme change never has to touch Python.
"""

from __future__ import annotations

from dataclasses import dataclass


#: Semantic tones. The stylesheet maps each to a colour pair; nothing here
#: knows about hex values.
TONE_NEUTRAL = "neutral"
TONE_ACTIVE = "active"
TONE_WAITING = "waiting"
TONE_SUCCESS = "success"
TONE_DANGER = "danger"
TONE_MUTED = "muted"


@dataclass(frozen=True, slots=True)
class StatusView:
    """How one state should read in the interface."""

    code: str
    label: str
    tone: str
    #: Whether the state is still moving on its own. The interface keeps
    #: polling while any visible row reports True, and stops when none do.
    live: bool = False

    def to_payload(self) -> dict[str, object]:
        return {
            "code": self.code,
            "label": self.label,
            "tone": self.tone,
            "live": self.live,
        }


def _view(code: str, label: str, tone: str, live: bool = False) -> StatusView:
    return StatusView(code=code, label=label, tone=tone, live=live)


#: Candidate lifecycle (`candidates.status`).
CANDIDATE_STATUS: dict[str, StatusView] = {
    "DISCOVERED": _view("DISCOVERED", "已发现", TONE_MUTED),
    "PENDING_REVIEW": _view("PENDING_REVIEW", "待审核", TONE_WAITING),
    "NEEDS_INFO": _view("NEEDS_INFO", "待补充", TONE_WAITING),
    "NEEDS_REVISION": _view("NEEDS_REVISION", "需要修订", TONE_WAITING),
    "APPROVED": _view("APPROVED", "已通过", TONE_ACTIVE),
    "PROCESSING": _view("PROCESSING", "处理中", TONE_ACTIVE, live=True),
    "DOWNLOADED": _view("DOWNLOADED", "已下载", TONE_SUCCESS),
    "REJECTED": _view("REJECTED", "已驳回", TONE_MUTED),
    "FAILED": _view("FAILED", "失败", TONE_DANGER),
}

#: Download queue (`download_jobs.state` for the four download providers).
DOWNLOAD_STATUS: dict[str, StatusView] = {
    "PENDING": _view("PENDING", "排队中", TONE_WAITING, live=True),
    "DOWNLOADING": _view("DOWNLOADING", "下载中", TONE_ACTIVE, live=True),
    # The transfer belongs to qBittorrent here, so it is live even though this
    # process holds no lease for it.
    "WAITING_TORRENT": _view(
        "WAITING_TORRENT", "等待做种", TONE_WAITING, live=True
    ),
    "PAUSED": _view("PAUSED", "已暂停", TONE_MUTED),
    "COMPLETED": _view("COMPLETED", "已完成", TONE_SUCCESS),
    "FAILED": _view("FAILED", "失败", TONE_DANGER),
    "CANCELLED": _view("CANCELLED", "已取消", TONE_MUTED),
}

#: Conversion / packaging (`download_jobs.state` for `provider='CONVERSION'`).
CONVERSION_STATUS: dict[str, StatusView] = {
    "CONVERSION_PENDING": _view(
        "CONVERSION_PENDING", "待打包", TONE_WAITING, live=True
    ),
    "CONVERSION_RUNNING": _view(
        "CONVERSION_RUNNING", "打包中", TONE_ACTIVE, live=True
    ),
    "CONVERSION_COMPLETED": _view(
        "CONVERSION_COMPLETED", "已打包", TONE_SUCCESS
    ),
    "CONVERSION_FAILED": _view("CONVERSION_FAILED", "打包失败", TONE_DANGER),
    # Recoverable: the operator supplies the missing piece and requeues the
    # same task, so these read as an ask rather than a failure.
    "CONVERSION_WAITING_VOLUMES": _view(
        "CONVERSION_WAITING_VOLUMES", "待补分卷", TONE_WAITING
    ),
    "CONVERSION_WAITING_PASSWORD": _view(
        "CONVERSION_WAITING_PASSWORD", "待补密码", TONE_WAITING
    ),
}

#: Download providers, so the interface stops showing raw `EH_TORRENT`.
PROVIDER_STATUS: dict[str, StatusView] = {
    "TELEGRAM": _view("TELEGRAM", "Telegram 原档", TONE_SUCCESS),
    "EH_TORRENT": _view("EH_TORRENT", "EH 种子", TONE_SUCCESS),
    "EXHENTAI": _view("EXHENTAI", "EH 归档", TONE_ACTIVE),
    "TELEGRAPH": _view("TELEGRAPH", "预览页图源", TONE_WAITING),
    "CONVERSION": _view("CONVERSION", "打包", TONE_ACTIVE),
}

#: Connection health, reused by the dashboard and the settings page. The codes
#: are exactly the four `ProviderStatus.state` literals -- an invented fifth
#: name here would render as a fallback and quietly mislabel a real provider.
CONNECTION_STATUS: dict[str, StatusView] = {
    "connected": _view("connected", "已连接", TONE_SUCCESS),
    "connecting": _view("connecting", "连接中", TONE_WAITING, live=True),
    "error": _view("error", "连接异常", TONE_DANGER),
    "not_configured": _view("not_configured", "尚未配置", TONE_MUTED),
}

#: Lookup order for the generic helpers. Candidate statuses come first because
#: `FAILED` means「候选失败」in the review context, which is the one an
#: operator sees most often.
_REGISTRIES: tuple[dict[str, StatusView], ...] = (
    CANDIDATE_STATUS,
    DOWNLOAD_STATUS,
    CONVERSION_STATUS,
    PROVIDER_STATUS,
)


def status_view(code: str | None) -> StatusView:
    """Resolve any known state code, falling back to the code itself.

    An unmapped code is shown verbatim with a neutral tone rather than raising:
    a new backend state must never blank out a page, and the raw value is still
    a usable clue for whoever sees it.
    """
    if not code:
        return _view("", "—", TONE_MUTED)
    for registry in _REGISTRIES:
        found = registry.get(code)
        if found is not None:
            return found
    return _view(code, code, TONE_NEUTRAL)


def status_label(code: str | None) -> str:
    """Chinese label for a state code, for use as a Jinja filter."""
    return status_view(code).label


def status_tone(code: str | None) -> str:
    """Semantic tone for a state code, for use as a Jinja filter."""
    return status_view(code).tone


def provider_label(code: str | None) -> str:
    """Human name for a download provider."""
    if not code:
        return "—"
    found = PROVIDER_STATUS.get(code)
    return found.label if found is not None else code


def connection_view(state: str | None) -> StatusView:
    """Resolve a `ProviderStatus.state`, defaulting to「尚未配置」."""
    return CONNECTION_STATUS.get(
        state or "not_configured", CONNECTION_STATUS["not_configured"]
    )


def is_live(code: str | None) -> bool:
    """Whether a state advances on its own and therefore needs polling."""
    return status_view(code).live


__all__ = [
    "CANDIDATE_STATUS",
    "CONNECTION_STATUS",
    "CONVERSION_STATUS",
    "DOWNLOAD_STATUS",
    "PROVIDER_STATUS",
    "StatusView",
    "TONE_ACTIVE",
    "TONE_DANGER",
    "TONE_MUTED",
    "TONE_NEUTRAL",
    "TONE_SUCCESS",
    "TONE_WAITING",
    "connection_view",
    "is_live",
    "provider_label",
    "status_label",
    "status_tone",
    "status_view",
]