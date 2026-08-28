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

#: The six candidate tabs. Keyed by the tab names in `app.api.candidates`,
#: which owns the tab -> statuses mapping; only the words live here, so the
#: template never writes 「待审核」 next to a link and drifts from the status set
#: the link actually selects. `all` is deliberately not a status union: it is
#: the escape hatch that shows a candidate whose state has no tab yet.
#: 「已通过」 is `live` because it covers PROCESSING -- that tab keeps moving
#: without the operator touching anything, and the page polls while it is open.
CANDIDATE_TAB_STATUS: dict[str, StatusView] = {
    "all": _view("all", "全部", TONE_NEUTRAL),
    "pending": _view("pending", "待审核", TONE_WAITING),
    "needs_info": _view("needs_info", "待补充", TONE_WAITING),
    "approved": _view("approved", "已通过", TONE_ACTIVE, live=True),
    "rejected": _view("rejected", "驳回", TONE_MUTED),
    "failed": _view("failed", "失败", TONE_DANGER),
}

#: Where a metadata value came from, for the review drawer. An operator
#: approving a title needs to know whether it came off the gallery page, out of
#: the message that introduced the candidate, or from their own earlier edit --
#: the three carry very different confidence, and「这个字段是谁写的」is the
#: question the drawer exists to answer. Codes are the `value_source` literals
#: the writers actually insert, not a parallel vocabulary: EXHENTAI from
#: `app/exhentai`, TELEGRAPH / EH_TORRENT from the two `ScanInformation`
#: writers, OPERATOR from `set_manual_metadata`, and FILENAME / INFERRED /
#: TELEGRAM / MANUAL_ADD from the message parser and the manual-add form.
METADATA_SOURCE_STATUS: dict[str, StatusView] = {
    "EXHENTAI": _view("EXHENTAI", "画廊数据", TONE_SUCCESS),
    "OPERATOR": _view("OPERATOR", "手动编辑", TONE_ACTIVE),
    "MANUAL_ADD": _view("MANUAL_ADD", "手动添加", TONE_ACTIVE),
    "TELEGRAM": _view("TELEGRAM", "消息标题", TONE_NEUTRAL),
    "TELEGRAPH": _view("TELEGRAPH", "预览页", TONE_NEUTRAL),
    "EH_TORRENT": _view("EH_TORRENT", "种子内容", TONE_NEUTRAL),
    "FILENAME": _view("FILENAME", "文件名", TONE_MUTED),
    "INFERRED": _view("INFERRED", "推断", TONE_MUTED),
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

#: The four queue sections. Keyed by `DownloadJobSummary.queue_group`; the
#: policy deciding which one a job is in lives on the DTO, and only the words
#: live here. `live` is what arms the activity page's poll: the interface keeps
#: asking while an「进行中」or「等待中」row exists, and stops once every visible
#: job is parked, paused or waiting on the operator.
QUEUE_GROUP_STATUS: dict[str, StatusView] = {
    "attention": _view("attention", "需干预", TONE_DANGER),
    "active": _view("active", "进行中", TONE_ACTIVE, live=True),
    "waiting": _view("waiting", "等待中", TONE_WAITING, live=True),
    "paused": _view("paused", "已暂停", TONE_MUTED),
}

#: Why a job needs the operator, from `DownloadJobSummary.attention_reason`.
#: A stalled torrent is `waiting`, not `danger`: the swarm may still deliver it,
#: and colouring it as a failure would push an operator into cancelling a
#: transfer that was going to finish.
ATTENTION_STATUS: dict[str, StatusView] = {
    "MISSING_VOLUMES": _view("MISSING_VOLUMES", "缺少分卷", TONE_WAITING),
    "MISSING_PASSWORD": _view("MISSING_PASSWORD", "缺少解压密码", TONE_WAITING),
    "MISSING_PAGES": _view("MISSING_PAGES", "预览页缺页", TONE_WAITING),
    "STALLED_TORRENT": _view("STALLED_TORRENT", "种子无做种者", TONE_WAITING),
    "FAILED": _view("FAILED", "任务失败", TONE_DANGER),
}

#: What a row is doing that its lifecycle state does not name. A torrent whose
#: payload the client is still sharing is `COMPLETED` as far as the pipeline is
#: concerned, and「已完成」on its own tells an operator the job has stopped using
#: their upstream -- which is exactly what it has not done. A note is a second
#: badge beside the state, never a replacement for it: the state stays
#: `COMPLETED`, so grouping, history and the JSON contract are unchanged.
NOTE_SEEDING = "SEEDING"

ROW_NOTE_STATUS: dict[str, StatusView] = {
    NOTE_SEEDING: _view(NOTE_SEEDING, "正在做种", TONE_ACTIVE),
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


def queue_group_view(group: str | None) -> StatusView:
    """Resolve a queue section name into its heading vocabulary."""
    return QUEUE_GROUP_STATUS.get(
        group or "", _view(group or "", group or "—", TONE_NEUTRAL)
    )


def candidate_tab_view(tab: str | None) -> StatusView:
    """Resolve a candidate tab name into its label and tone.

    Falls back to the raw name rather than raising, for the same reason
    `status_view` does: the routes validate the tab before it ever gets here, so
    a miss means a new tab was added without its words -- which should look
    unfinished, not take the page down.
    """
    return CANDIDATE_TAB_STATUS.get(
        tab or "", _view(tab or "", tab or "—", TONE_NEUTRAL)
    )


def metadata_source_view(source: str | None) -> StatusView:
    """Resolve where a metadata value came from, for the review drawer."""
    if not source:
        return _view("", "未知", TONE_MUTED)
    return METADATA_SOURCE_STATUS.get(source, _view(source, source, TONE_NEUTRAL))


def attention_view(reason: str | None) -> StatusView | None:
    """Resolve an attention reason, or None when the job needs nothing.

    Returning None rather than a placeholder keeps the caller honest: a page
    asks「这条要我做什么」and gets either an answer or silence, never an empty
    badge that looks like a state.
    """
    if not reason:
        return None
    return ATTENTION_STATUS.get(reason, _view(reason, reason, TONE_DANGER))


def is_live(code: str | None) -> bool:
    """Whether a state advances on its own and therefore needs polling."""
    return status_view(code).live


def row_note_view(code: str | None) -> StatusView | None:
    """Resolve a row note, or None when the row has nothing extra to say.

    Unknown notes return None rather than a fallback badge: unlike a state, a
    note is optional by design, so an unrecognised one is better left unsaid
    than rendered as a raw code beside a perfectly good state.
    """
    if not code:
        return None
    return ROW_NOTE_STATUS.get(code)


__all__ = [
    "ATTENTION_STATUS",
    "CANDIDATE_STATUS",
    "CANDIDATE_TAB_STATUS",
    "CONNECTION_STATUS",
    "CONVERSION_STATUS",
    "DOWNLOAD_STATUS",
    "METADATA_SOURCE_STATUS",
    "NOTE_SEEDING",
    "PROVIDER_STATUS",
    "QUEUE_GROUP_STATUS",
    "ROW_NOTE_STATUS",
    "StatusView",
    "TONE_ACTIVE",
    "TONE_DANGER",
    "TONE_MUTED",
    "TONE_NEUTRAL",
    "TONE_SUCCESS",
    "TONE_WAITING",
    "attention_view",
    "candidate_tab_view",
    "connection_view",
    "is_live",
    "metadata_source_view",
    "provider_label",
    "queue_group_view",
    "row_note_view",
    "status_label",
    "status_tone",
    "status_view",
]