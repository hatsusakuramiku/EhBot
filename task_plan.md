# Task Plan: EhBot Development Plan

## Goal
Produce an implementation-ready development plan for a Docker-deployed Telegram and ExHentai comic ingestion, review, download, and CBZ conversion service.

## Current Phase
Implementation phase 13 (download queue controls and configurable paths) complete; phase 6 (low-resource optimization and release) remains deferred; a full `docker compose up` acceptance run with real credentials is still outstanding

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

### Implementation Phase 10: Extensible Archive Processing
- [x] Add `ArchiveBackend` and `ArchiveProcessor` interfaces with backend selection by task snapshot
- [x] Implement ZIP/CBZ backend using the streaming Python ZIP path
- [x] Implement 7zz subprocess backend for RAR, 7Z, split archives, and fallback encrypted ZIP
- [x] Add isolated external-tool/DLL bridge boundary with registered tool profiles
- [x] Add split-volume inspection and `WAITING_VOLUMES` recovery
- [x] Add encrypted archive password vault and `WAITING_PASSWORD` recovery
- [x] Add pre-extraction safety manifest and limits
- [x] Use the selected backend for both extraction and CBZ packing
- [x] Add authenticated settings pages for paths, tool profiles, safety limits, and password entries
- [x] Add unit, integration, subprocess-fixture, and recovery tests
- **Status:** complete; 211 tests pass and the archive settings page was verified against a live application instance

## Phase 10 Delivered Behavior
- `app/archive/` contains models, format/volume detection, safety validation, the password vault, backends, the processor, and the settings service.
- `ConversionService` no longer contains format logic; it builds an `ArchiveProcessor` from enabled profiles, stored limits, and vault entries, then persists the backend snapshot on the task.
- Migration `009_archive_processing.sql` adds `archive_tool_profiles`, `archive_passwords`, and `archive_settings`, and seeds the `zipfile-default` and `7zz-default` profiles.
- `/archive-settings` exposes runtime paths, safety limits, keep-original, per-profile executable/timeout/enable state, and password-vault entries.
- The superseded `stream_zip_to_cbz` helper was removed; `app/conversion/convert.py` now only defines `ConversionError`.

### Deferred After Phase 10
- Real RAR/7Z end-to-end verification needs a host with `7zz` installed; the subprocess boundary is currently covered by injected-runner fixtures.
- Encrypted-archive password success is covered through the vault and the ZIP encryption flag; a recorded encrypted RAR/7Z fixture is still missing.
- Library path templating beyond `{title}.cbz` is scaffolded in `app/conversion/naming.py` but not exposed as an operator-editable template.
- Docker acceptance and the phase 6 low-resource pass remain untouched.

## Phase 10 Assumptions And Boundaries
- The main process never loads arbitrary DLLs; DLL-capable tools run behind a controlled bridge subprocess registered as a `BRIDGE` profile.
- Operators select registered tool profiles and may only adjust the executable path, timeout, and enabled state; no command line is accepted from the UI.
- Split naming supports `.partN.rar`, `.rar` + `.rNN`, and `.zip/.7z/.rar` + `.NNN`; incomplete series park in `CONVERSION_WAITING_VOLUMES` and never drive the tool.
- The selected backend performs both extraction and CBZ packing for a task, and CBZ output uses `ZIP_STORED` so already-compressed images are not recompressed.
- Passwords are encrypted at rest with a private-file master key and are never written to logs, task details, or audit payloads.
- Every published CBZ is written as `<name>.cbz.part` first and atomically renamed; failures delete the partial file and the task's temporary directory.

### Implementation Phase 11: Managed 7-Zip Toolchain
- [x] Pin the official upstream 7-Zip release per platform with a verified SHA-256
- [x] Install the binary from the upstream `.tar.xz` using only the Python standard library
- [x] Prefer the statically linked binary so slim Linux images need no extra runtime library
- [x] Make the install idempotent, version-isolated, and safe against a tampered download
- [x] Resolve the executable as managed install, then `PATH`, then platform default
- [x] Provision automatically at startup behind `ARCHIVE_TOOLCHAIN_AUTO_INSTALL`
- [x] Expose toolchain status and a manual install action on the archive settings page
- [x] Provide a build/provisioning entry point and drop the distribution `7zip` package from the image
- [x] Add offline unit coverage and real-binary end-to-end coverage that skips when absent
- [x] Update handover records and the deployment environment surface
- **Status:** complete; 241 tests pass and real-binary QA covers install, tamper rejection, and archive processing

## Phase 11 Delivered Behavior
- `app/archive/toolchain.py` owns the pinned version, the per-platform asset table with digests, download verification, allowlist extraction, and the idempotent install into `<data>/tools/7zip/<version>/`.
- `ArchiveSettingsService.ensure_toolchain()` runs at startup when auto-install is enabled and only warns on failure, so a missing archiver never blocks the service.
- The archive settings page reports whether 7-Zip is ready, where it came from, and which upstream asset applies; `POST /archive-settings/toolchain/install` forces a reinstall.
- `scripts/install_seven_zip.py` pre-seeds the binary for image builds or offline provisioning.
- Two real defects surfaced during real-binary QA and were fixed: header-encrypted archives now resolve passwords through an inspect probe, and CBZ packing runs with `cwd=staging` so member names are not full paths.

## Phase 11 Assumptions And Boundaries
- Linux and Docker are the deployment targets. Platforms upstream does not publish binaries for, including Windows, fall back to an operator-provided path and never attempt a download.
- Only pinned assets are installable. The version and its digests are updated together in source; nothing discovers or trusts an upstream "latest" pointer at runtime.
- Only the known 7-Zip executable names are extracted from the release archive, and archive-controlled member names are never joined onto a filesystem path.
- An unavailable toolchain degrades the 7-Zip-backed formats only; the built-in streaming ZIP path stays fully functional.
- Tests never reach the network: `tests/conftest.py` disables auto-install and blocks the download function.

### Deferred After Phase 11
- First-boot download inside a real Linux container, plus the wider Docker image acceptance, is still unverified because this host has no Docker.
- No recorded encrypted RAR fixture exists; RAR support rests on 7-Zip's reported format list and 7z equivalents.
- `BRIDGE` profiles remain schema-only, library path templating is still `{title}.cbz`, and the phase 6 low-resource pass is untouched.
- Refreshing the pinned 7-Zip version is a manual source change; there is no upstream release check.

### Implementation Phase 12: Docker Linux Verification
- [x] Add a repeatable script that verifies the archive toolchain inside a Linux container
- [x] Prove the managed install end to end on a container that starts with no 7-Zip
- [x] Prove the install fails closed with no network and leaves nothing behind
- [x] Run the full pytest suite on Linux with dependencies resolved from `uv.lock`
- [x] Build the application image and pre-seed 7-Zip inside it
- [x] Fix every defect the Linux run exposed and add regression coverage
- **Status:** complete; 5/5 Docker stages pass and the Windows suite is 242 passed

## Phase 12 Delivered Behavior
- `scripts/verify_docker_linux.py` runs four independent stages (`--offline`, `--suite`, `--build`) and reports a per-stage PASS/FAIL summary with a non-zero exit on any failure.
- Startup provisioning is now genuinely best effort: `ensure_toolchain()` contains all failure modes and logs `TOOLCHAIN_PROVISION_FAILED` instead of aborting the lifespan.
- The test suite can no longer reach the network even if the environment enables auto-install, and its download guard now raises the same error type the application handles in production.
- `test_startup_survives_a_failing_toolchain_install` locks in that a host unable to fetch 7-Zip still serves `/healthz` and `/archive-settings`.

## Phase 12 Assumptions And Boundaries
- Windows is not expected to install the managed binary; upstream publishes no such asset and the platform check refuses it deliberately. Windows verification is limited to the unit suite plus an operator-provided `7z.exe`.
- The verification script needs a running Docker engine and network access; it exits with code 2 and a clear message when the engine is unreachable, rather than reporting a false failure.
- Linux suite dependencies come from `uv.lock` so the check cannot silently drift from the project's declared dependencies.

### Deferred After Phase 12
- A full `docker compose up` acceptance run with real credentials is still outstanding, as is the phase 6 low-resource pass.
- Encrypted RAR coverage still rests on 7-Zip's reported format support rather than a recorded RAR fixture.

### Implementation Phase 13: Download Queue Controls And Configurable Paths
- [x] Diagnose the reported download failure and stop the error path from hiding the real cause
- [x] Add retry, pause, resume and cancel for download jobs, with the candidate state kept consistent
- [x] Keep failed jobs visible on the queue page so they can be acted on
- [x] Let the operator change the library and work directories from the archive settings page
- [x] Make a directory change take effect without restarting the service
- [x] Cover every transition and path rule with tests on Windows and inside a Linux container
- **Status:** complete; Windows suite is 255 passed and the in-container checks are 16/16

## Phase 13 Delivered Behavior
- The reported failure was the Telegram Bot API 20 MB download ceiling, reported as a generic connection error. There is now a `TELEGRAM_FILE_TOO_BIG` code with an actionable message, and the generic branch preserves Telegram's own `description`.
- `POST /downloads/{job_id}/retry|pause|resume|cancel` drive the queue. Retry reuses the same job row, so the idempotency contract and the attempt history both survive; cancel returns the candidate to `PENDING_REVIEW`; errors are surfaced on `/downloads?error=`.
- The queue page renders each button from `is_retryable` / `is_pausable` / `is_cancellable`, and shows the failure message next to the job. Permanently failed jobs offer no retry button at all.
- The library and work directories can be set on `/archive-settings`. Both are validated as absolute and writable, clearing a field restores the environment default, and the change applies to the next task with no restart.

## Phase 13 Assumptions And Boundaries
- Pause applies only to a job that has not been claimed. An in-flight download cannot be suspended and resumed safely, so `DOWNLOADING` offers cancel instead.
- `PAUSED` is deliberately neither active nor terminal: the worker never claims it, but it is still an open job the operator owns.
- The data directory remains environment-only (`DATA_PATH`). It holds the settings database, so relocating it from a page backed by that database is not a coherent operation.
- Files above 20 MB are out of reach for the Bot API regardless of retries; the ExHentai source is the documented workaround.

### Deferred After Phase 13
- MTProto (Telethon) large-file downloads: not installed, not declared, and a product decision the operator has not made.
- `SevenZipBackend.pack_cbz` still accepts a `str` ComicInfo payload that would raise `TypeError`; unreachable from production callers, but the boundary is loose.
- Still outstanding from earlier phases: the full `docker compose up` acceptance run with real credentials, the phase 6 low-resource pass, a recorded encrypted RAR fixture, the `BRIDGE` profile protocol, and the `{category}/{artist}/{title}` library layout.
