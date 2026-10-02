# EhBot — Agent Notes (pointer)

The working notes moved to **`AgentHelp/AGENTS.md`**. Read that file before
touching code; it carries the environment constraints, the architecture rules and
the business invariants, and its scope is this whole repository.

This stub stays at the root because that is where the agent convention looks for
it. Nothing else belongs here — add a rule to `AgentHelp/AGENTS.md` instead, so
there is one copy of it.

> **Workflow rule (master copy in `AgentHelp/AGENTS.md`):** write a proposal
> (`AgentHelp/<FEATURE>_PROPOSAL.md`) first, wait for the operator's review,
> then implement. 「先寫方案後開工，審閱之後再動手」.

**Before starting any work in this repository, read `AgentHelp/PHASES.md`** — it
is the one-screen index of every phase (one line each) with pointers into the
detailed log. Only dive into `AgentHelp/progress.md` after that. Run
`codegraph sync` at the start of a session so symbol/graph lookup is fresh
(`.codegraph/` is local and git-ignored).

| Document | What it is |
|----------|------------|
| `AgentHelp/AGENTS.md` | Environment, architecture rules, business invariants, test baseline |
| `AgentHelp/PHASES.md` | **Read first**: one-line summary of every phase + pointers into `progress.md` |
| `AgentHelp/progress.md` | Phase-by-phase implementation log; read bottom-up for current state |
| `AgentHelp/EHBot.md` | Requirements spec |
| `AgentHelp/DEVELOPMENT_PLAN.md` | Phased plan |
| `AgentHelp/COMPETITIVE_ANALYSIS.md` | Research behind the UI refactor |
| `AgentHelp/task_plan.md` | Phase ledger |
| `AgentHelp/findings.md` | External research notes |
| `AgentHelp/*_PROPOSAL.md` | Per-feature design records (`LOGGING_PROPOSAL.md` was implemented; the daily-log redesign supersedes it) |

Operator-facing documentation is `README.md`: what the service does, how to
deploy it with Docker, and how to configure it.

## 文档同步是任务的一部分

改动之后必须同步调整文档，这是每次交付的**必选步骤**，不是可选项：

- **功能、界面或行为一变，就同步改** `README.md`、`docs/USAGE.md`（必要时
  `AgentHelp/EHBot.md`）里对应的描述——包括删除「已经不存在的东西」的段落
  （被移除的环境变量、被废弃的交互、被改名的页面），文档里残留旧事实比没有文档更糟。
- **每完成一个阶段**，在 `AgentHelp/progress.md` 追加一条 R 编号条目（对照相邻
  条目保持相同格式），版本号、测试基线、数据库迁移编号一并更新。
- 环境变量增删、配置项改名这类**表格型事实**，改完代码立刻去核对 `README.md` /
  `docs/USAGE.md` / `.env.example` 是否一致。
- 若这次改动确实不触碰任何用户可见行为，提交说明里写一句「无文档变更」，说明你考虑过而不是忘了。

Master copy lives in `AgentHelp/AGENTS.md`; keep this rule and that file's copy in
agreement.
