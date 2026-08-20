# Task Plan: EhBot Development Plan

## Goal
Produce an implementation-ready development plan for a Docker-deployed Telegram and ExHentai comic ingestion, review, download, and CBZ conversion service.

## Current Phase
Implementation phase 9 (automatic approval rules) complete; phase 6 remains deferred; phase 10 archive-processing proposal drafted and awaiting approval

## Phases

### Phase 1: Requirements And Discovery
- [x] Consolidate user requirements and constraints
- [x] Verify Telegram, ExHentai, and ComicPacker feasibility
- [x] Record findings in `findings.md`
- **Status:** complete

### Phase 2: Plan Authoring
- [x] Define scope, architecture, data model, workflows, and interfaces
- [x] Define Docker deployment and resource profiles
- [x] Define delivery phases and acceptance criteria
- **Status:** complete

### Phase 3: Verification And Delivery
- [x] Check the plan against every confirmed requirement
- [x] Check internal consistency and implementation boundaries
- [x] Deliver the plan for user review before implementation
- **Status:** complete

## Key Questions
1. Can both Bot-admin and user-account channel sources be supported? Yes, through separate Telethon sessions.
2. Can the service run in 1C/512 MB? Yes for the low-memory profile and primarily ZIP workloads; 1 GB is recommended for RAR/7Z.
3. Should ComicPacker be a runtime dependency? No; reference its behavior and metadata, but use a streaming converter.
4. Who owns TLS and reverse proxy configuration? The deployer; EhBot exposes HTTP on a configurable port.

## Decisions Made
| Decision | Rationale |
|----------|-----------|
| Python 3.12 asynchronous monolith | Lowest operational overhead while fitting Telegram, HTTP, web, and job processing into one process |
| Telethon Bot and User sessions | Covers Bot-admin channels, private sharing, and channels where a Bot cannot be added |
| FastAPI, Jinja2, and HTMX | Provides a complete review UI without a Node runtime |
| SQLite WAL and a durable in-process worker | Avoids Redis/PostgreSQL/Celery overhead while retaining restart recovery |
| Streaming ZIP-to-CBZ converter | Avoids ComicPacker's whole-output-in-memory behavior |
| Docker as a supported primary deployment | Meets the deployment requirement while retaining native execution for development |
| HTTP port only | TLS and reverse proxy remain deployment concerns |

## Errors Encountered
| Error | Attempt | Resolution |
|-------|---------|------------|
| In-app browser navigation to Telegram documentation timed out | 1 | Used read-only official documentation mirrors and project repositories |
| `rg.exe` could not start in the empty workspace | 1 | Used PowerShell file enumeration |

## Notes
- The plan was approved for implementation on 2026-08-19.
- Do not place external research text in this file; external findings belong in `findings.md`.

### Implementation Phase 1: Foundation And Persistence
- [x] Initialize the Git repository and commit the approved plan as the baseline
- [x] Add the Python 3.12 project and locked dependencies
- [x] Add SQLite WAL initialization and the initial core-schema migration
- [x] Add bootstrap administrator creation, forced password change, CSRF protection, short-term login lockout, and logout
- [x] Add the Web dashboard plus liveness and readiness endpoints
- [x] Add Docker, Compose, secret-file bootstrap, and local development documentation
- [x] Pass the complete automated test suite
- [ ] Build and start the Docker image on a host with Docker installed
- **Status:** code complete; runtime container acceptance pending because Docker is not installed on this machine

### Implementation Phase 2: Token-Only Connections And UI
- [x] Add a private runtime secret store for Telegram and ExHentai credentials
- [x] Add Telegram Bot API verification, long polling, status, and idempotent update persistence
- [x] Add ExHentai Cookie verification, status, and reconnect/disconnect lifecycle
- [x] Add authenticated connection-management routes and status API
- [x] Redesign login, dashboard, navigation, connection page, and responsive styles
- [x] Pass adapter, lifecycle, Web integration, and full regression tests
- [x] Verify desktop/mobile UI in the browser
- [x] Complete two-axis review and commit the implementation
- **Status:** complete

### Implementation Phase 3: Offline Candidate Ingestion And Review Queue
- [x] Add Telegram Update normalization for preview, gallery link, and archive messages
- [x] Add deterministic media-group, reply, and ExHentai gallery merging
- [x] Add ACCEPT/IGNORE processing state and idempotent Update consumption
- [x] Add authenticated candidate queue and read-only detail page
- [x] Add live dashboard candidate counts and startup/polling ingestion
- [x] Pass offline integration and Web regression tests
- [x] Verify candidate queue and detail layouts in desktop/mobile browsers
- [x] Add source-channel and private-sender allowlists plus rule configuration
- [x] Add allowed archive-format and maximum attachment-size rules
- [x] Add `NEEDS_INFO` classification and its follow-up queue
- [x] Add manual metadata editing and review actions
- [x] Add Telegram media download and persistent download jobs
- **Status:** complete

### Implementation Phase 4: Source Rules And Needs Info
- [x] Default newly discovered Telegram sources to disabled
- [x] Add channel and private-sender whitelist configuration
- [x] Add archive-format and attachment-size filtering
- [x] Route missing titles and unknown bounded sizes to `NEEDS_INFO`
- [x] Add authenticated source-rule UI and separate needs-info queue
- [x] Pass offline integration and Web regression tests
- [x] Add tag, language, category, and rating rules after Ex metadata ingestion exists
- **Status:** code, automated verification, and two-axis review complete; browser visual verification blocked by local plugin initialization

## Phase 2 Interface Decisions
- Telegram Token-only mode uses the official Bot API. Telethon/MTProto remains a later optional path because it also requires API ID and API Hash.
- Raw Telegram Token and ExHentai Cookies are stored only in private files under the data directory, never in ordinary database fields or rendered HTML.
- Telegram updates are durably stored before later candidate parsing is introduced.
- ExHentai connection in this phase validates authenticated site access; metadata and archive download remain later adapters.

### Implementation Phase 5: ExHentai Metadata And Chinese Tags
- [x] Classify Telegram polling errors (409/429/403/401/5xx) instead of one opaque message
- [x] Switch metadata ingestion to the official `gdata` API with HTML fallback for expunged galleries
- [x] Expand stored metadata to title, Japanese title, artist, group, parody, character, language, category, tags, rating, pages, uploader
- [x] Rebuild the ComicInfo mapping to match plan section 8.4, including ISO 639-1 language codes
- [x] Sync, cache, and index the EhTagTranslation database with ETag validation and offline degradation
- [x] Translate metadata into Chinese while keeping the upstream English values in `*Raw` fields
- [x] Show Chinese field labels in the review UI and collapse the untranslated originals
- [x] Pass the complete automated test suite (148 tests)
- [ ] Operator acceptance run against live Telegram and ExHentai traffic
- **Status:** code and automated verification complete; live operator acceptance pending

### Implementation Phase 6: Hardening And Release
- [ ] Add archive safety checks: path traversal, zip bombs, magic-number validation, atomic `.part` publishing (plan sections 8.8, 17.2, 21.9)
- [ ] Add tests for `app/candidates/reference.py`
- [ ] Handle `my_chat_member` so sources appear without requiring a qualifying message first
- [ ] Verify the low-memory profile and the Docker runtime on a host with Docker installed
- **Status:** not started

### Implementation Phase 7: Review And Download Queue Workflow
- [x] Make the Processing and Failed download status controls display their matching job queues
- [x] Remove approval/rejection reason input from the review workflow
- [x] Add multi-candidate approve and reject operations
- [x] Make approval automatically enqueue the candidate's available Telegram or ExHentai download
- [x] Synchronize candidate `PROCESSING` / `FAILED` states with download worker progress so the dashboard queues contain meaningful records
- [x] Automatically fetch available ExHentai metadata before candidates are presented for review
- [x] Show the fetched title, author, tags, and related metadata in the review queue for bulk confirmation
- [x] Add focused regression tests for filters, batch review, automatic enqueue, and metadata presentation
- [x] Record an implementation-neutral proposal for user-defined automatic approval rules; do not implement it before user review
- **Status:** code and automated verification complete; in-app Browser visual QA blocked by plugin trusted-path initialization

## Phase 7 Assumptions And Boundaries
- `PROCESSING` and `FAILED` refer to candidate states counted on the dashboard. The dashboard controls must link to candidate queues filtered by those states.
- Approval means the selected candidate should be placed into the download queue immediately. The existing idempotency contract must prevent duplicate jobs.
- Rejection only excludes a candidate from downloading; neither approval nor rejection requires a human-entered reason. System-generated failure and rule reasons remain part of the audit trail.
- Automatic metadata acquisition applies when entering or refreshing the review queue and a candidate has an ExHentai reference. Telegram-derived metadata remains the first available source; ExHentai enriches it.
- Automatic approval syntax and execution are proposal-only in this phase because the user explicitly requested a scheme for review first.
- Existing user changes to `.gitignore` are out of scope and must be preserved.

### Implementation Phase 8: Metadata Labels And Bilingual Tags
- [x] Add a red-capable test proving every known metadata field has a real Chinese label rather than `?`
- [x] Add a red-capable test proving original tags and matched Chinese tags remain separate
- [x] Restore Chinese field labels without changing metadata field keys
- [x] Keep every upstream tag in `TagsRaw` and only successfully matched Chinese names in `Tags`
- [x] Display original and matched Chinese tags as two tag rows in review views
- [x] Ensure metadata rules and ComicInfo treat both original and matched Chinese values as tags
- [x] Run focused and full regression tests
- [x] Update handover findings and progress
- **Status:** complete; automated HTML verification passed, screenshot QA blocked by the local Browser plugin trust-path error

## Phase 8 Assumptions And Boundaries
- “原 Tag” means the complete upstream `namespace:value` set from E-Hentai/ExHentai.
- “匹配出的中文” means only successful EhTagTranslation matches; unmatched upstream values must not be copied into the Chinese row.
- Both rows remain tag semantics for filtering/export; they are not general metadata notes.
- Metadata field keys and stored source contracts remain unchanged.

### Implementation Phase 9: Automatic Approval Rules
- [x] Persist rule name, enabled state, priority, version, structured condition AST, and DSL snapshot
- [x] Evaluate only the allowlisted AST operators and metadata fields, including the merged `{TAG}` collection
- [x] Add authenticated configuration, preview, and enable/disable management UI
- [x] Evaluate eligible enriched candidates by priority and auto-approve only the first matching rule
- [x] Reuse existing approval and download enqueue idempotency; record rule, snapshot, condition result, and job IDs in review history
- [x] Add focused unit/integration/Web regression coverage and run the full suite
- [x] Update handover records
- **Status:** complete; full regression passed

## Phase 9 Assumptions And Boundaries
- Rules are server-stored JSON ASTs. The DSL text is a readable snapshot only and is never evaluated.
- Version 1 supports the proposal's text, numeric, collection, existence, boolean, and `LIKE` operators; no regex, functions, field-to-field calculation, SQL, or Python execution.
- Evaluation is restricted to `PENDING_REVIEW` candidates with at least one usable download source and complete available metadata. Failure to fetch metadata or evaluate a rule leaves the candidate for manual review.
- First enabled matching rule by ascending priority wins; the action is always `AUTO_APPROVE`.

### Implementation Phase 10: Extensible Archive Processing (Proposal Only)
- [ ] Add `ArchiveBackend` and `ArchiveProcessor` interfaces with backend selection by task snapshot
- [ ] Implement ZIP/CBZ backend using the existing Python ZIP path
- [ ] Implement 7zz subprocess backend for RAR, 7Z, split archives, and fallback encrypted ZIP
- [ ] Add isolated external-tool/DLL bridge boundary with registered tool profiles
- [ ] Add split-volume inspection and `WAITING_VOLUMES` recovery
- [ ] Add encrypted archive password vault and `WAITING_PASSWORD` recovery
- [ ] Add pre-extraction safety manifest and limits
- [ ] Use the selected backend for both extraction and CBZ packing
- [ ] Add authenticated settings pages for paths, tool profiles, safety limits, and password entries
- [ ] Add unit, integration, subprocess-fixture, and recovery tests
- **Status:** proposal drafted; implementation awaits user approval

## Phase 10 Assumptions And Boundaries
- The main process never loads arbitrary DLLs; DLL-capable tools run behind a controlled bridge subprocess.
- Users select registered tool profiles; they cannot submit arbitrary executable paths or command lines.
- The first implementation supports common RAR/7Z/ZIP split naming and delegates format semantics to the selected backend.
- The selected backend performs both extraction and CBZ packing for a task.
- Passwords are encrypted at rest and are never written to logs or audit payloads.
