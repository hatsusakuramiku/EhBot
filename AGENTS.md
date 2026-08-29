# EhBot — Agent Notes (pointer)

The working notes moved to **`AgentHelp/AGENTS.md`**. Read that file before
touching code; it carries the environment constraints, the architecture rules and
the business invariants, and its scope is this whole repository.

This stub stays at the root because that is where the agent convention looks for
it. Nothing else belongs here — add a rule to `AgentHelp/AGENTS.md` instead, so
there is one copy of it.

| Document | What it is |
|----------|------------|
| `AgentHelp/AGENTS.md` | Environment, architecture rules, business invariants, test baseline |
| `AgentHelp/progress.md` | Phase-by-phase implementation log; read bottom-up for current state |
| `AgentHelp/EHBot.md` | Requirements spec |
| `AgentHelp/DEVELOPMENT_PLAN.md` | Phased plan |
| `AgentHelp/COMPETITIVE_ANALYSIS.md` | Research behind the UI refactor |
| `AgentHelp/task_plan.md` | Phase ledger |
| `AgentHelp/findings.md` | External research notes |
| `AgentHelp/*_PROPOSAL.md` | Per-feature design records (`LOGGING_PROPOSAL.md` is open, not yet implemented) |

Operator-facing documentation is `README.md`: what the service does, how to
deploy it with Docker, and how to configure it.
