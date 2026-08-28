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

from app.review.models import AUTO_OPERATOR, SYSTEM_OPERATOR


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

#: What kind of file rode along with the source message. `app.candidates.ingestor`
#: produces exactly these two kinds, and the detail page needs a word for each
#: because a photo has no filename worth showing while an archive is nothing but
#: its filename. Kept here rather than as a template ternary for the same reason
#: every other label is: the page and the JSON client must say the same thing.
ATTACHMENT_PHOTO = "photo"
ATTACHMENT_ARCHIVE = "archive"

ATTACHMENT_KIND_STATUS: dict[str, StatusView] = {
    ATTACHMENT_PHOTO: _view(ATTACHMENT_PHOTO, "图片预览", TONE_NEUTRAL),
    ATTACHMENT_ARCHIVE: _view(ATTACHMENT_ARCHIVE, "压缩包", TONE_NEUTRAL),
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
    "TELEGRAM_USER": _view("TELEGRAM_USER", "Telegram 大文件", TONE_SUCCESS),
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
    # The MTProto account adds two states a token-based connection cannot be
    # in: a login is a multi-step exchange, and an operator who requested a code
    # must be able to tell「等待验证码」from「尚未配置」. Both are `waiting`, and
    # neither is `live`: the next step is the operator's, not the server's, so
    # polling for it would only spend requests.
    "awaiting_code": _view("awaiting_code", "等待验证码", TONE_WAITING),
    "awaiting_password": _view(
        "awaiting_password", "等待两步验证密码", TONE_WAITING
    ),
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

#: The three stages one work passes through. A candidate, a download in flight
#: and a packaged book are the same row at different points, which is why they
#: share one detail page; the stage decides which actions that page offers, and
#: it is derived from the candidate's status and its jobs rather than stored --
#: a fourth column to keep in step with both would be a third answer to a
#: question the other two already answer.
STAGE_CANDIDATE = "CANDIDATE"
STAGE_DOWNLOAD = "DOWNLOAD"
STAGE_ARCHIVED = "ARCHIVED"

WORK_STAGE_STATUS: dict[str, StatusView] = {
    STAGE_CANDIDATE: _view(STAGE_CANDIDATE, "候选期", TONE_WAITING),
    STAGE_DOWNLOAD: _view(STAGE_DOWNLOAD, "下载期", TONE_ACTIVE, live=True),
    STAGE_ARCHIVED: _view(STAGE_ARCHIVED, "入库期", TONE_SUCCESS),
}

#: The seven settings sections. Codes are the URL segment of `/settings/{section}`
#: as well as the tab label's key, so a tab, its link, its JSON payload and the
#: nav entry that reaches it all name the same thing once. They are `neutral`
#: throughout: a section is a place, not a state, and giving one a tone would
#: imply the settings inside it were healthy or in trouble.
SETTINGS_CONNECTIONS = "connections"
SETTINGS_SOURCES = "sources"
SETTINGS_AUTO_APPROVAL = "auto-approval"
SETTINGS_ARCHIVE = "archive"
SETTINGS_PATHS = "paths"
SETTINGS_PASSWORDS = "passwords"
SETTINGS_SYSTEM = "system"

SETTINGS_SECTION_STATUS: dict[str, StatusView] = {
    SETTINGS_CONNECTIONS: _view(SETTINGS_CONNECTIONS, "外部连接", TONE_NEUTRAL),
    SETTINGS_SOURCES: _view(SETTINGS_SOURCES, "来源规则", TONE_NEUTRAL),
    SETTINGS_AUTO_APPROVAL: _view(
        SETTINGS_AUTO_APPROVAL, "自动审批", TONE_NEUTRAL
    ),
    SETTINGS_ARCHIVE: _view(SETTINGS_ARCHIVE, "归档", TONE_NEUTRAL),
    SETTINGS_PATHS: _view(SETTINGS_PATHS, "路径", TONE_NEUTRAL),
    SETTINGS_PASSWORDS: _view(SETTINGS_PASSWORDS, "密码库", TONE_NEUTRAL),
    SETTINGS_SYSTEM: _view(SETTINGS_SYSTEM, "系统", TONE_NEUTRAL),
}

#: Tab order, derived from the mapping rather than written out again: a second
#: list would be a second place to forget a section.
SETTINGS_SECTIONS: tuple[str, ...] = tuple(SETTINGS_SECTION_STATUS)

#: Whether a stored row is switched on. The settings page shows this for a
#: Telegram source, an approval rule, a tool profile and a vault password -- four
#: lists that would otherwise each write 「已启用」 by hand, which is exactly the
#: duplication the vocabulary exists to prevent. `active` and `muted` rather than
#: success and danger: a disabled rule is a decision the operator made, not a
#: fault, and an enabled one is working rather than finished.
TOGGLE_ENABLED = "ENABLED"
TOGGLE_DISABLED = "DISABLED"

TOGGLE_STATUS: dict[str, StatusView] = {
    TOGGLE_ENABLED: _view(TOGGLE_ENABLED, "已启用", TONE_ACTIVE),
    TOGGLE_DISABLED: _view(TOGGLE_DISABLED, "已停用", TONE_MUTED),
}

#: Whether something the pipeline depends on is present -- the 7-Zip binary, a
#: registered qBittorrent WebUI. Distinct from `TOGGLE_STATUS` because it is not
#: a switch the operator flipped: 「未就绪」 is a thing to go and fix, where
#: 「已停用」 is a decision already made. `muted` rather than `danger` on the
#: missing side, because neither dependency is required to run: an installation
#: with no torrent client still ingests from Telegram perfectly well.
DEPENDENCY_READY = "READY"
DEPENDENCY_MISSING = "MISSING"

DEPENDENCY_STATUS: dict[str, StatusView] = {
    DEPENDENCY_READY: _view(DEPENDENCY_READY, "已就绪", TONE_SUCCESS),
    DEPENDENCY_MISSING: _view(DEPENDENCY_MISSING, "未就绪", TONE_MUTED),
}

#: Whether the process itself came up clean. `Settings.readiness_errors()` plus
#: the writability checks feed `/readyz`; this is the same fact rendered for a
#: person. The workbench used to print 「系统正常」 as a literal beside a green
#: dot, which was a lie in the one situation that matters -- a deployment whose
#: library directory is read-only said 「系统正常」 while every pack failed. The
#: codes are prefixed because `READY` already belongs to `DEPENDENCY_STATUS`: an
#: unprefixed pair would put two different meanings behind one code.
SYSTEM_HEALTHY = "SYSTEM_HEALTHY"
SYSTEM_DEGRADED = "SYSTEM_DEGRADED"

SYSTEM_HEALTH_STATUS: dict[str, StatusView] = {
    SYSTEM_HEALTHY: _view(SYSTEM_HEALTHY, "运行正常", TONE_SUCCESS),
    SYSTEM_DEGRADED: _view(SYSTEM_DEGRADED, "启动异常", TONE_DANGER),
}

#: Audit-trail verbs (`review_actions.action`), for the timeline. The codes are
#: exactly what the writers insert -- `REVIEW_ACTIONS` plus the two nobody types
#: -- rather than a parallel list, so an entry can never render as a raw
#: `NEEDS_REVISION` beside the same word spelled out. A rejection is `muted` for
#: the same reason `REJECTED` is: it is a decision, not a fault.
REVIEW_ACTION_STATUS: dict[str, StatusView] = {
    "APPROVE": _view("APPROVE", "通过", TONE_SUCCESS),
    "REJECT": _view("REJECT", "驳回", TONE_MUTED),
    "NEEDS_REVISION": _view("NEEDS_REVISION", "要求修订", TONE_WAITING),
    "REQUEUE": _view("REQUEUE", "重新排队", TONE_WAITING),
    "EDIT_METADATA": _view("EDIT_METADATA", "编辑元数据", TONE_NEUTRAL),
    "LOCK_METADATA": _view("LOCK_METADATA", "锁定字段", TONE_NEUTRAL),
    "AUTO_APPROVE": _view("AUTO_APPROVE", "自动通过", TONE_ACTIVE),
    "METADATA_RULE": _view("METADATA_RULE", "规则判定", TONE_NEUTRAL),
}

#: Who did it. The audit trail stores a name, not a kind, so the kind is
#: resolved from the two reserved names in `app.review.models` -- which is also
#: why the timeline can say 「自动规则」 for a row written before this vocabulary
#: existed. An operator reading a timeline needs this distinction before they
#: need anything else on the row: 「谁决定的」 decides whether they go argue with
#: a rule or with a person.
ACTOR_OPERATOR = "OPERATOR"
ACTOR_AUTO_RULE = "AUTO_RULE"
ACTOR_SYSTEM = "SYSTEM"

ACTOR_STATUS: dict[str, StatusView] = {
    ACTOR_OPERATOR: _view(ACTOR_OPERATOR, "操作员", TONE_ACTIVE),
    ACTOR_AUTO_RULE: _view(ACTOR_AUTO_RULE, "自动规则", TONE_NEUTRAL),
    ACTOR_SYSTEM: _view(ACTOR_SYSTEM, "系统", TONE_MUTED),
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


def attachment_kind_view(kind: str | None) -> StatusView:
    """Resolve an attachment kind, falling back to the raw kind.

    Falls back rather than raising because the ingestor may learn a third kind
    before this registry does, and an unlabelled chip beside a real file is a
    better outcome than a detail page that will not render.
    """
    if not kind:
        return _view("", "附件", TONE_MUTED)
    return ATTACHMENT_KIND_STATUS.get(kind, _view(kind, kind, TONE_NEUTRAL))


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


def work_stage_view(stage: str | None) -> StatusView:
    """Resolve a work's stage into its label and tone."""
    return WORK_STAGE_STATUS.get(
        stage or "", _view(stage or "", stage or "—", TONE_NEUTRAL)
    )


def settings_section_view(section: str | None) -> StatusView:
    """Resolve a settings section, raising on one that does not exist.

    Unlike every other resolver here this one does not fall back. The others
    describe stored history, where an unknown code is a row written by an older
    version and must still render; a section code arrives in a URL an operator
    typed, and answering `/settings/nonsense` with a page titled 「nonsense」
    would invent a tab. `KeyError` is what the route turns into a 404.
    """
    return SETTINGS_SECTION_STATUS[section or ""]


def toggle_view(enabled: bool | int | None) -> StatusView:
    """Resolve whether a stored row is switched on.

    Takes the stored value rather than a code because that is what the callers
    have: `enabled` is an SQLite integer on a source, a rule and a tool profile.
    Coercing here means four templates ask 「这条开着吗」 the same way instead of
    each deciding what `0` looks like.
    """
    return TOGGLE_STATUS[TOGGLE_ENABLED if enabled else TOGGLE_DISABLED]


def dependency_view(ready: bool | None) -> StatusView:
    """Resolve whether an external dependency is usable."""
    return DEPENDENCY_STATUS[DEPENDENCY_READY if ready else DEPENDENCY_MISSING]


def system_health_view(errors: object = None) -> StatusView:
    """Resolve the startup-error list into one badge for the workbench.

    Takes the collection rather than a boolean because that is what
    `app.state.startup_errors` holds, and an empty list is the healthy case —
    the caller does not have to decide what「没有错误」looks like. Anything
    truthy is degraded: a single unwritable directory is enough, since `/readyz`
    answers 503 for exactly the same input.
    """
    return SYSTEM_HEALTH_STATUS[SYSTEM_DEGRADED if errors else SYSTEM_HEALTHY]


def review_action_view(action: str | None) -> StatusView:
    """Resolve an audit-trail verb, falling back to the raw code.

    Falling back rather than raising for the same reason `status_view` does: a
    timeline is history, and a verb retired from the code is still in the table.
    An operator seeing `SOMETHING_OLD` learns more than one seeing a blank row.
    """
    if not action:
        return _view("", "—", TONE_MUTED)
    return REVIEW_ACTION_STATUS.get(action, _view(action, action, TONE_NEUTRAL))


def actor_kind(operator_name: str | None) -> str:
    """Which of the three actors a stored `operator_name` is.

    The column holds a login for a person and one of two reserved names
    otherwise, so the kind is derived rather than stored. Deriving it also means
    every row already in the table gets the right actor, including the ones
    written before the timeline existed.
    """
    if operator_name == AUTO_OPERATOR:
        return ACTOR_AUTO_RULE
    if operator_name == SYSTEM_OPERATOR:
        return ACTOR_SYSTEM
    return ACTOR_OPERATOR


def actor_view(operator_name: str | None) -> StatusView:
    """Resolve who performed an action into 操作员 / 自动规则 / 系统."""
    return ACTOR_STATUS[actor_kind(operator_name)]


__all__ = [
    "ACTOR_AUTO_RULE",
    "ACTOR_OPERATOR",
    "ACTOR_STATUS",
    "ACTOR_SYSTEM",
    "ATTACHMENT_ARCHIVE",
    "ATTACHMENT_KIND_STATUS",
    "ATTACHMENT_PHOTO",
    "ATTENTION_STATUS",
    "CANDIDATE_STATUS",
    "CANDIDATE_TAB_STATUS",
    "CONNECTION_STATUS",
    "CONVERSION_STATUS",
    "DEPENDENCY_MISSING",
    "DEPENDENCY_READY",
    "DEPENDENCY_STATUS",
    "DOWNLOAD_STATUS",
    "METADATA_SOURCE_STATUS",
    "NOTE_SEEDING",
    "PROVIDER_STATUS",
    "QUEUE_GROUP_STATUS",
    "REVIEW_ACTION_STATUS",
    "ROW_NOTE_STATUS",
    "SETTINGS_ARCHIVE",
    "SETTINGS_AUTO_APPROVAL",
    "SETTINGS_CONNECTIONS",
    "SETTINGS_PASSWORDS",
    "SETTINGS_PATHS",
    "SETTINGS_SECTIONS",
    "SETTINGS_SECTION_STATUS",
    "SETTINGS_SOURCES",
    "SETTINGS_SYSTEM",
    "STAGE_ARCHIVED",
    "STAGE_CANDIDATE",
    "STAGE_DOWNLOAD",
    "SYSTEM_DEGRADED",
    "SYSTEM_HEALTHY",
    "SYSTEM_HEALTH_STATUS",
    "StatusView",
    "TONE_ACTIVE",
    "TONE_DANGER",
    "TONE_MUTED",
    "TONE_NEUTRAL",
    "TONE_SUCCESS",
    "TONE_WAITING",
    "TOGGLE_DISABLED",
    "TOGGLE_ENABLED",
    "TOGGLE_STATUS",
    "WORK_STAGE_STATUS",
    "actor_kind",
    "actor_view",
    "attachment_kind_view",
    "attention_view",
    "candidate_tab_view",
    "connection_view",
    "dependency_view",
    "is_live",
    "metadata_source_view",
    "provider_label",
    "queue_group_view",
    "review_action_view",
    "row_note_view",
    "settings_section_view",
    "status_label",
    "status_tone",
    "status_view",
    "system_health_view",
    "toggle_view",
    "work_stage_view",
]