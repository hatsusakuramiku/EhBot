# Task Plan: EhBot Development Plan

## Goal
Produce an implementation-ready development plan for a Docker-deployed Telegram and ExHentai comic ingestion, review, download, and CBZ conversion service.

## Current Phase
Implementation phase 15 (operator-facing bugfix and workflow completion: history page, auto-pack, pack feedback, regex auto-approval, manual add-task, approve/gate fix, and a read-first torrent retry flow) is complete; phase 14 (download source chain) is code complete on all four levels; phase 6 (low-resource optimization and release) remains deferred; a full `docker compose up` acceptance run with real credentials and a real-network pass against a live qBittorrent are still outstanding

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
- Page bytes are published unchanged unless the operator selects an image quality level (`original` / `high` / `medium` / `low`); `original` is the default because a lossy re-encode is irreversible. Only JPEG pages are re-encoded, and a result that is not smaller is discarded in favour of the original.
- Passwords are encrypted at rest with a private-file master key and are never written to logs, task details, or audit payloads.
- The session signing key is generated and persisted under `<data>/private` on first start; `APP_SECRET_KEY` is optional and only needed when several replicas share one key. Private-file writes use a bounded exclusive-create loop, never `tempfile.mkstemp`, which retries indefinitely on a Windows directory it cannot open.
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
- Superseded by R22: Windows now installs the pinned official `7z.exe` and `7z.dll` portably under `data/tools`; it does not depend on an operator-installed copy.
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

### Implementation Phase 14: Download Source Chain
- [x] Extract preview page URLs from `text_link` entities, persist `preview_url`, and accept preview-only messages as candidates
- [x] Persist `torrent_count` and `torrent_hash` from the gdata response so routing needs no extra request
- [x] Add `app/torrent/`: torrent selection, `.torrent` retrieval, local infohash verification, and a qBittorrent WebAPI adapter
- [x] Add the `WAITING_TORRENT` job state with a poller that reports progress, seeds and stall time without auto-failing
- [x] Take delivery by hard-linking or copying out of the seeding directory, never moving it
- [x] Add the qBittorrent settings block with an encrypted password, two save paths and a connectivity test
- [x] Offer manual source switching on a stalled torrent rather than degrading automatically
- [x] Add `app/telegraph/`: page client, URL guard, bounded image fetcher, and ZIP packer
- [x] Add the `TELEGRAPH` and `EH_TORRENT` providers and stop the worker from hard-coding its provider list
- [x] Route approvals through `TELEGRAM` → `EH_TORRENT` → `TELEGRAPH` and demote ExHentai Archive Download to a manual button
- [x] Park a preview page-count mismatch in `NEEDS_INFO` instead of publishing a partial book
- [x] Record source-grade provenance in `details_json` and ComicInfo `ScanInformation`
- **Status:** code complete. The live chain is `TELEGRAM → EH_TORRENT → TELEGRAPH`, with ExHentai Archive Download as a manual button only. 392 tests pass (66 new for the torrent branch). Design in `DOWNLOAD_SOURCE_CHAIN_PROPOSAL.md`; only real-network verification against a live qBittorrent remains.

## Phase 14 Delivered Behavior (TELEGRAPH branch)
- `app/telegraph/` holds `models.py`, `guard.py` (scheme, literal address, DNS-resolved address, per-hop redirect checks), `client.py` (getPage API first, HTML fallback, document-order dedupe, `/embed/` skipped, `/file/` completion), `fetcher.py` (bounded concurrency, per-image retry with a Telegraph referer, magic-number check, zero-padded names) and `packer.py` (`ZIP_STORED`, `.part`-then-rename; `pack_directory` exists for the torrent branch to reuse).
- `TelegraphService.download_for_candidate()` produces a ZIP, registers it as an `ARCHIVE` artifact on a `COMPLETED` `telegraph:<candidate_id>` job, and writes a `ScanInformation` metadata row. Everything after that is the existing conversion pipeline unchanged.
- `_route_download_source()` in `app/main.py` is the four-level router; it skips an attachment above the 20 MB Bot API limit instead of queueing a job that can only fail.
- `DownloadService._claim_pending_job_sync` now expands `SUPPORTED_PROVIDERS` into its placeholder list, and the ExHentai/Telegraph branches share one `_run_delegated_provider` helper.
- `NEEDS_INFO_DOWNLOAD_ERRORS` routes a page-count mismatch to a reviewable candidate state while the job row stays `FAILED` and retryable.
- `build_comicinfo_xml` takes `scan_information`; `<ScanInformation>` is emitted for every source that records one.

## Phase 14 Measured Basis
- The gdata response already carries `torrentcount` and `torrents[{hash, added, name, tsize, fsize}]`, so discovering a torrent costs no extra request and no cookie.
- Torrents are not always there: gid 1655718 reports `torrentcount=0` while gid 4108964 and gid 4076223 each report 1. Every level of the chain is load-bearing.
- A torrent's `fsize` does not equal the gallery `filesize` (126,838,245 B vs 139,262,241 B on gid 4108964), so the page-count consistency gate cannot be applied to the torrent route.
- `torrents[].hash` is only an infohash; the `.torrent` file must be resolved through `gallerytorrents.php` with a logged-in session, and it embeds the account passkey.
- The preview link is a `telegra.ph` page whose URL lives in a caption `text_link` entity, so the current text-only regex in `CandidateIngestor._parse_message` cannot see it.
- `api.telegra.ph/getPage?return_content=true` returns an ordered node tree, so the images can be read from the official API rather than scraped from HTML.
- The images are not on `telegra.ph/file/`; each channel runs its own Telegram file proxy (`image.dangernsfw.win`, `pic.850123.xyz`) behind Cloudflare, and both require a `User-Agent` and a `Referer` or answer 403.
- Preview page coverage is complete: 22/22, 15/15 and 78/78 against the ExHentai gdata `filecount` on the sampled galleries.
- Preview quality is not: gid 1655718 is 145,185,851 B over 15 pages at the source and 7,895,214 B over the same 15 pages on the preview page, normalized to 1280 px wide. The preview route is reading-grade, roughly 5–10 % of the original bytes.

## Phase 14 Delivered Behavior (EH_TORRENT branch)
- `app/torrent/` holds `models.py`, `bencode.py` (strict decoder so a canonical round-trip yields the infohash a client computes), `fetcher.py` (`gallerytorrents.php` link parsed off the page, never hard-coded, infohash verified before pushing), `client.py` (qBittorrent WebAPI with one silent re-login on 403), `delivery.py` (client-to-EhBot path translation, hard link then copy, never move) and `service.py` (push, poll, delivery, provenance).
- `EH_TORRENT` is the only provider whose transfer runs outside this process, so `DownloadService._push_torrent_job` parks it in `WAITING_TORRENT` and releases the concurrency slot; `TorrentService.poll_once` advances it.
- Restart recovery needed no separate path: the poller reads parked jobs from the database each pass, so a new process re-attaches by hash.
- A stall is never resolved automatically. `DownloadService.switch_source` is the operator's explicit action, and it removes the torrent from the client before queueing the replacement.
- qBittorrent settings live in `archive_settings` with the password in the existing vault; `save_torrent_client` verifies the EhBot-side save path is readable at save time.
- The client adapter tolerates both WebAPI generations, because a real instance disagreed with the fakes on two points: login may answer `204` rather than `200 Ok.`, and `torrents/add` answers a JSON report on 2.11+ with `409 Conflict` for a hash the client already holds. A duplicate is not an error but is reported to the operator, since the pre-existing entry decides the save path and category.
- The poller logs `torrent_poll_started` and a `torrent_progress` line per observation, so progress can be followed without opening the dashboard.
- A `COMPLETED` torrent that is still seeding stays on the downloads dashboard with its upload speed, and `POST /downloads/{job_id}/stop-seeding` ends it without deleting files. Seeding is the one thing that outlives a finished job, so it is the one thing a finished job still shows.
- The dashboard self-refreshes at the poll interval while anything is downloading or seeding, and not at all otherwise.
- `torrent_auto_pack` (default **off**) hands a finished delivery straight to `ConversionService`. Enabling it requires `local_save_path` and requires that directory to be listable rather than merely present, because an unattended pack must not discover a permission problem hours later. A failed pack leaves the download `COMPLETED` and logs `TORRENT_AUTO_PACK_FAILED`.

## Phase 14 Deferred Items
- The qBittorrent side of §14 step 8 is **done**: a live `v5.2.3` / WebAPI `2.15.1` instance ran real galleries end to end, which is what exposed the login, add-body and duplicate-hash mismatches. `telegra.ph` and `gallerytorrents.php` remain fakes in the suite.
- **Deployment step still open:** `local_save_path` is unset in the live install, so EhBot cannot read payloads the client saves on another host. Auto-pack cannot be enabled until that directory is mounted and registered.
- Selection scoring lives in `app/exhentai/gdata.py::select_torrent` and is exercised through gdata unit tests rather than the torrent service.

## Phase 14 Assumptions And Boundaries
- This phase reverses two approved boundaries in `DEVELOPMENT_PLAN.md` 3.2 and needs that confirmed on approval: torrents become the preferred route for oversized books, and bounded preview-page image fetching is allowed. The EH gallery itself is still never scraped page by page.
- The archive pipeline is reused unchanged: every provider produces or registers an `ARCHIVE` artifact, which is exactly what `ConversionService` already consumes.
- Original-quality downloads stay the priority; the preview source is the last resort, never the default route.
- BitTorrent runs in an external qBittorrent instance reached over its WebAPI. The main process loads no P2P library, so the 1C/512 MB target is unaffected.
- qBittorrent downloads into its configured `savepath` and EhBot reads the finished payload from that path, so the client-side and EhBot-side views of it are configured separately for split-host deployments.
- A stalled torrent is not an error. It stays in `WAITING_TORRENT` with its stall time on display and waits for an operator decision rather than silently degrading to preview grade.
- Archive Download stays manual because it costs GP; spending a limited resource is an operator decision, not a routing default.
- Image URLs come from untrusted third-party content, so scheme, host, resolved address, redirect depth, byte caps and image magic numbers are all enforced before anything is written. Torrent contents still pass the existing archive safety gates.
- MTProto (Telethon) remains out of scope; it is the only real route to original files above 20 MB straight from Telegram and is a separate product decision.
- Channels that post only an archive plus a `t.me/c/...` link gain nothing from this phase.


### Implementation Phase 15: Operator-Facing Bugfix And Workflow Completion
- [x] Add a history page that auto-archives completed download tasks
- [x] Add auto-pack after download (off by default) and show packaging status/path on the task page
- [x] Give the pack button real feedback on the candidate detail page
- [x] Replace the auto-approval condition builder with `Regex({Title}, 'regexstring')` and validate the regex before save
- [x] Add a manual add-task entry point for ExHentai and magnet links, approved immediately
- [x] Stop an already-approved candidate from walking into a spurious "must approve first" error
- [x] Add a retry action with a read-first torrent retry flow that re-reads settings each attempt
- **Status:** complete; 427 passed, 12 skipped (the skips are the real-7-Zip cases)

## Phase 15 Delivered Behavior
- Completed downloads auto-archive to `/downloads/history`; the current queue and the archive are separate navigation entries.
- `auto_pack_after_download` (default off) hands a finished download straight to `ConversionService.enqueue_for_candidate`; the task page surfaces the produced CBZ path and the candidate detail page shows a distinct packed/failed status block.
- Auto-approval is now pattern-based: the persisted AST is `{"kind":"regex","field","pattern"}`, the pattern is `re.compile`-validated at save time (an invalid regex is refused, never stored), and evaluation is `re.search` over the field's stored value. `REGEX_FIELDS` excludes the numeric `Rating`/`Pages`.
- `/manual-add` accepts an ExHentai gallery link or a magnet link. A manual candidate is created as `APPROVED` immediately, enqueued through the proper provider (Ex/ qBittorrent), and needs that provider configured; an unrecognised input or an unconfigured client returns a clear 400.
- Approval-enqueued download no longer rejects an already-approved candidate; the enqueue accepts `APPROVED`/`DOWNLOADED`.
- A failed job offers retry. Retry reads the payload path first and succeeds if the file is readable, otherwise restarts the download chain and surfaces any exception; `TORRENT_CONTENT_UNREACHABLE`/`TORRENT_CONTENT_UNEXPECTED` were removed from `PERMANENT_DOWNLOAD_ERRORS` so a path fix is a real recovery path. Settings are re-read per add/retry.

## Phase 15 Assumptions And Boundaries
- Packaging is never automatic implicitly: both `auto_pack_after_download` and `torrent_auto_pack` default to off and remain explicit operator decisions.
- Regex matching is case-sensitive by design; operators use inline flags (`(?i)`) or explicit character classes for case-insensitive rules. `Rating`/`Pages` are numeric and not regex-matchable.
- Manual add is treated as already approved by contract: it is an operator action, so it skips review and heads straight to the download queue.
- Retry reuse of the same job row keeps the `idempotency_key` contract and preserves attempt history; it does not create a second job.

### Deferred After Phase 15
- Still open: **`local_save_path` is unset in the live deployment**, so `torrent_auto_pack` and the autonomous automatic-pack path cannot run until the qBittorrent save directory is mounted and registered.
- Outstanding from earlier phases: the phase 6 low-resource pass, a recorded encrypted RAR fixture, the `BRIDGE` profile protocol, and the `{category}/{artist}/{title}` library layout.

### R21: Runtime Log Level And UTC Daily Files
- [x] Persist one WebUI log level for both runtime output and the viewer default
- [x] Default to `INFO`; enable `uvicorn.access` only at `DEBUG`
- [x] Write `{yyyy-MM-dd}.log` and duplicate `WARNING`+ into `{yyyy-MM-dd}_error.log`
- [x] Roll and retain files by UTC day while preserving the legacy `ehbot.log` reader fallback
- [x] Verify configuration, runtime switching, WebUI behavior, UTC rollover, retention and file contents
- **Status:** complete; 1174 collected, 0 failed, 12 environment-dependent 7-Zip skips.

### R22: Portable Managed 7-Zip On Windows
- [x] Pin official Windows x86, x64 and ARM64 26.02 assets and SHA-256 digests
- [x] Verify the official `7zr.exe` bootstrap separately
- [x] Extract only `7z.exe` and `7z.dll` without running the installer
- [x] Prefer the managed version and remove PATH / registry / Program Files discovery
- [x] Run all 12 real archive integration tests against the portable Windows pair
- **Status:** complete; 1181 collected, all 12 real 7-Zip tests execute on Windows.

### R85: Page Size Input And Archive Metadata Synchronization
- [x] Accept any positive page size; preserve filters and reset to page one
- [x] Read actual ComicInfo and share in-place edits between WebUI and API
- [x] Support custom metadata fields, preserve archive paths/images/generated facts
- [x] Sync documentation and verify 418 distinct related tests; collect 2037 total
- **Status:** complete; no migration, version 0.3.0rc3; full suite and release not run.

### R86: Server Realtime API And WebUI Partial Updates
- [x] Publish authenticated WebSocket commands, invalidations and JSON/HTML snapshots
- [x] Support browser Cookie/Origin/CSRF and mobile Bearer access/API Key
- [x] Replace WebUI polling/SSE with one connection and preserve editing/selection state
- [x] Add list-only Tag left-click filtering and keyboard-accessible right-click menu
- [x] Cover real repack notifications, auth revocation, log snapshot races and HTTP fallback
- [x] Deliver docs/REALTIME_API.md for the later EhBot-Mobile implementation
- [x] Verify 545 distinct tests across 20 files, DOM behavior and 2064 collected total
- **Status:** complete; no migration, version 0.3.0rc3; mobile implementation, real-device testing, full suite and release remain later work.

### R87: Public URL And WebUI Operations / Metadata Fixes
- [x] Implement approved v2 PUBLIC_BASE_URL in both Compose files and validate exact public origins
- [x] Repair rule route collisions, same-origin URLs, submitter handling and specific error feedback
- [x] Move connection state to topbar; reconnect and reconcile automatically; keep 50 plus wide custom input
- [x] Share file-based metadata editor, protect manual/locked fields and explicitly save fetched drafts
- [x] Verify 637 distinct cases across 21 files, real TCP/CBZ and DOM; collect 2101 total
- [x] Synchronize operator docs, realtime contract, environment examples and phase ledger
- **Status:** complete; no migration, version 0.3.0rc3; local commit only. Actual proxy/browser testing, mobile implementation and release remain later work.

### 队列、多账号与统一 API 改造（2026-10-13，已授权）

完整范围与接手状态见 [处理计划](DOWNLOAD_QUEUE_AND_SHARED_API_IMPLEMENTATION_PLAN.md)。旧 Phase 15 重试改旧行不再作为新模型契约。

- [x] P0：批准、详细计划与快速接手入口
- [x] P1 / R88：管理员认证完整切片（rc4/026，12 新合同、370 个不同用例最终通过、2113 collect；本地提交见计划，无发布）
- [x] P2 / R89：任务/尝试/审批/作品槽、原子认领、持久操作/回执、HTTP/Web/WS 任务命令；2026-10-14 复核补漏，rc5/028、28 个合同、19 文件完整 460 条通过、2141 collect；[完成核验](R89_COMPLETION_AUDIT.md)。物理停止与完整屏障在 P3，勿部署中间切片
- [x] P3 / R90：真实停止、目标预检、远端归属/共享拒绝、受管 7-Zip、refile/产物屏障、独立运行移除/清理与恢复、重加等待及六项原触发回归；rc6/029、2213 collect、628 个不同用例最终通过。完整发布崩溃恢复与心跳按 P8/P9
- [x] P4 / R91：实际 startup/provider/账号版本/持久停止与共享 API/WS/Web 接通；掩码查看编辑、独立登录、停用保留与固定结果回执；rc7/030，2280 collect、322 个不同用例最终通过、两个 DOM 脚本通过；每类型先启用一个，完整多账号按 P5–P7 接续，见 R91 audit/progress
- [x] P5 / R92（已验收，以下为验收前调查记录）：031 已接统一摄取/回填、手动原子发布、移除代次、补 gid/来源编辑持久停止、操作版本/共享状态/幂等重试及 Web/WS；来源补 gid 接目标键预留、旧 owner 撤销与全部观察关联。继续审计交叉变更/移除、媒体组/回复与提交后回调恢复，再做阶段验收，接续见处理计划与 progress
- P5 后续证据：已补持久后续意图/自动审批原子提交及摄取代次核对；真实来源停止、交叉变更/移除和媒体组/回复合同通过。2449 collect-only，17 文件 240/0；交错文件扩展为 16/0、followups 含旧库多 alias 扩展为 8/0，按节点最新 251 个不同用例通过。继续迁移/旧写入口与历史半审批恢复边界的完成核验，不重复实现上述已验证机制。
- [x] P6 / R93（已验收，以下为验收前调查记录）：同类多启用、每账号来源/游标、下载 observation/revision/account 绑定与传输前文件核对已接；来源列表/账号 picker、032 来源命令 CAS/幂等/草稿、033 旧共享订阅一次性迁移已验证。下一步监听公平/独立退避、可靠事件持久化后推进游标和停止后接替
- P5/P6 最新接续：2463 collect-only；摄取/来源等 14 文件 180/0，另设置/API/WS 六文件 232/0、此前来源/TG 三文件 33/0，不相加。旧半审批不猜意图、显式入队边界已验证；固定输入 chat_title 首次/重试丢失已复现修复，新增编辑重放/旧库回填合同。阶段状态不变，接手读计划和 progress 最新条目。
- P6 来源命令接续：2481 collect-only（0.93s），source_commands 新合同 18/0、来源/摄取/下载五文件 64/0、此前来源/设置/数据库/迁移五文件 134/0；组间重叠不相加。真实 HTMX/Alpine DOM root/proxy 通过，远端删除仍保留草稿、原版本和幂等键。031/032 未冻结，完整目标 active，见 progress 最新小节。
- P6 旧订阅接续：2488 collect-only（0.95s）；账号/来源/摄取/数据库九文件 121/0，强化第二账号中途故障后的迁移文件 7/0。规则复制只面向原单账号导入槽，保留墓碑、用户游标和专属配置；历史消息不改接收账号。031–033 未冻结，接续完整监听与接替，不据此验收 P5/P6。
- P6 监听接续：034 原子保存用户消息/游标；接收与候选消费分开，每账号消费持有独立 lease。来源预算轮转/频道交错、FloodWait 与会话失效、历史后台恢复、AI 清理和真实写线程停止合同通过。2506 collect-only；11 文件 154/0、后续 8 文件 102/0，最终 3 文件 59/0，重叠不相加。031–034 未冻结，P5/P6 未验收；继续 P5 最终审计与停止后跨账号接替，P9 再接持久冷却和跨功能预算。
- P5 最终审计接续：见 R92_REQUIREMENT_AUDIT.md。已定位全部 Python 候选/观察写者，并复现修复原订阅失效后的固定输入选择，已保存备用观察可恢复；AI 前/提交时均核对来源。2511 collect-only，最终十文件 133/0、随后四文件 70/0，重叠不相加。账号不可用但订阅启用、过期 claim、完整旧入口/要求验收仍待核对；新的手动发布至入队持久意图明确列入 P8，与旧半审批无任务不猜意图区分。
- P5 账号/过期处理接续：2525 collect-only；摄取十一文件 146/0、扩展十三文件 180/0，随后手动/账号恢复两文件 21/0，组间不相加。原账号不可用可让位给已保存合格观察，仍合格的原输入只由原账号处理；TG/手动共用本地活任务记录，实际清理前不会因过期接替。百条恢复扫描已复现修复饥饿并共享账号游标。继续最终旧入口/全合同及升级回退核验；P6 下载接替和 P7–P12 不缩减，详见 progress。
- P6 下载接替接续：2569 collect-only；十二文件 197/0，最后 failover 加强为 11/0（按节点最新 198 个不同用例通过，非最后全组重跑）。035 只为经真实退出确认的内部操作恢复同一 task，新 attempt/独立 staging；有限账号池、取消覆盖、重启/升级和提交回滚通过。账号停用触发接替及显式重试重选仍待核对；P5/P6 未验收，031–035 未冻结。
- P5/P6 本轮接续：旧入口与快照恢复审计已落 R92_REQUIREMENT_AUDIT；生命周期接替、完整旧历史冻结、备用观察停止、停止处理器中断/恢复和停机清理通过。最新 2593 collect-only；十三文件 221/0，快照加强 2/0，P5 十八文件 248/0 与最后三文件 40/0；按节点最新 445 个不同用例通过。P5/P6 仍未阶段验收或提交，031–036 未冻结；不要重做已通过合同，继续阶段收口与多监听/来源/API 完整验收。
- P6 合同接续：完整监听/旧订阅/来源命令/账号 HTTP/Web 八文件 95/0；EH 串行改动后来源/监听六文件 81/0，账号/API 在十七文件 215/0 中通过。接阶段验收/混合工作区提交边界，不用重复测试代替阶段交付。
- [x] P7 / R94（已验收，以下为验收前调查记录）：036 已接匿名 gdata 不借账号、批量不重复公共请求、持久有限认证元数据池/固定次序/成功租约和本进程同账号串行；能力/冷却/共享退避、收费意图/未知结果查回、独立归档 attempt、种子认证及崩溃恢复仍待完成。本轮 2610 collect-only，最新节点共 309 个不同用例通过，非整库；详见 progress。
- [ ] P8：产物依赖、发布、打包取消
- [ ] P9：心跳、恢复、预算、清理
- [ ] P10：全域共享 API、版本/幂等、WS/冲突 UI
- [ ] P11：审批饥饿、ComicInfo 写锁、缩略图竞态
- [ ] P12：合同与本地验收、升级/恢复、最终交接


## R92 / P5 独立交付

- [x] 统一身份、031 历史回填、手动原子发布、持久审批后续。
- [x] 单账号真实监听与共享订阅、旧收件箱恢复兼容。
- [x] 独立验收并本地提交 `2ea49f2`；447 个不同用例通过；2465 collect-only。
- [ ] P6 提取验收与阶段提交；P7–P12 按原计划继续。


## R93 / P6 独立交付

- [x] 多 TG 来源/监听/消息与游标原子接收/有限串行账号接替。
- [x] 独立 rc9/035 树 2596 collected（仅收集）；定向组按当前节点取最新 419 个不同用例通过，0 failure/error/skip，非一次整组或整库运行。
- [x] 本地阶段提交 c61af7e，58 blobs 与验收快照逐 SHA256 一致，暂存为空；接 P7–P12。


## R94 / P7 独立交付

- [x] EH 有限池/健康/收费核实/认证种子及串行队列接替；2661 collect-only / 363 distinct passed。
- [x] 本地阶段提交 `31b5b00`，40 blobs/空暂存区/027–036 已核实；接 P8–P12。

## R95 / P8 交付

- [x] 037 产物版本、精确 ARCHIVE/CBZ 依赖、入队设置与持久打包/手动下载意图。
- [x] CBZ 准备/替换/确认/清理与改名/refile 三断点恢复；真实进程退出、取消/删除/外部改动/输入竞争合同。
- [x] 27 个相关文件按最新节点 590 个不同用例通过；2687 collect-only；README/USAGE/API/交接同步。
- [x] 本地阶段提交 89ef4b9；37 blobs、空暂存区、027–037 已核实；接 P9–P12。

## R96 / P9 已验收并本地提交（rc12/038）

- [x] OS 文件锁先于迁移；worker_session/心跳/真实活动与 dispatch 窗口保护；第二后台启动失败，取消启动释放锁。
- [x] 周期恢复逐项隔离、游标轮转，未知文件失败保留 journal；自动中断恢复最多 3 次，显式重试重新开始。
- [x] 本地下载/打包/远端占用分开，远端未知/STOPPING 保留；账号请求与监听分别有全局/单账号限制，等待后复核停止/版本。
- [x] TG 限流持久化，重启不绕过；到期恢复启动监听；停机先阻止领取/新请求。
- [x] 迁移/进程树/账号/供应商/摄取等 454 个不同相关用例最终通过，2710 collect-only；文档与 R96 audit 同步。Linux 新守护路径尚未实跑，P12 单独验证。
- [x] 本地提交 f60412b，37 blobs/空暂存区/027–038 已核实，038 冻结；TEMP 精确清理剩余 0。models.py 既有 BOM 排除。

## P10 进行中（未验收、未提交）

- [x] 元数据原子编辑/抓取、操作查询、旧表单/JS 前置条件与草稿保留、HTTP↔WS 共享回执。
- [x] 审核原子核心与新 review API；入队失败整体回滚、网络前后版本核对、同键并发重发。
- [x] 旧单项/批量审核 API/Web 接入共享核心，持久固定父/子结果及拒绝；并发/中断恢复/跨通道重发通过。
- [x] 来源下载/打包 API/Web 接原子所见版本/任务/回执，锁外预检后 CAS；旧任务终态后重发不复活，精确产物保持。
- [x] 候选单项/批量删除复用持久 removal，最终 CAS/原受理回执同事务；HTTP↔WS/旧表单重放、真实停止与批量冲突保留通过。主动筛选释放选择，HTMX 显示 409/428。
- [x] 已下载重下/重包/移除、列表改名/详情精确路径与固定去重预览；任务/pin/回执原子提交，真实移动恢复、HTTP↔WS/旧表单与草稿保留通过。
- [x] 手动新增持久回执/取消UNKNOWN/跨通道重发及编号复用检查，35条核心/旧入口合同及10条最终HTTP/核心合同通过（重叠不相加）。
- [x] 七个设置保存区原子CAS/回执、读快照版本一致、旧表单草稿/显式确认及模板预览保留；最终三文件14/0及DOM root/proxy通过。
- [x] settings-legacy-fixed180/0、settings-runtime99/0、settings-draft-final16/0，DOM根路径与前缀通过；2829 collect-only / 57XML当前节点942 distinct passed，0failure/error/skip，非整库。
- [x] 逐条审批/路径规则稳定身份、独立版本、原子回执与HTTP/WS/原表单；冲突/试跑/后续广播保留草稿。规则最终78/0、旧页面等237/0、DOM root/proxy通过，2846 collect-only / 63XML当前节点1015 distinct passed，非整库。
- [x] qBittorrent第八设置区CAS/HMAC安全回执、首次并发主密钥、JSON部分编辑、HTTP/WS/旧表单与浏览器密码草稿；改实例在途推送/恢复原实例停止确认通过。七文件150个不同用例最终通过，旧API2/0、DOM root/proxy；2855 collect-only / 68XML当前节点1116 distinct passed，非整库。
- [x] 工具profile逐条稳定身份/版本、原DB验证与原子回执、API/WS/旧表单及显式冲突确认；最新五文件118/0、加强两条2/0、DOM root/proxy通过。2863 collect-only / 71XML当前节点1193 distinct passed，非整库。
- [x] 手动新增重复失败回执/提示链接固定原作品身份；profile删除原生错误页保留草稿；身份五文件47/0，数据库旧测试两条修复复验2/0。
- [x] 工具安装持久受理/固定回执、独立revision、HTTP/WS/旧表单与显式确认、跨进程文件锁、替换失败/中断恢复、丢响应及停机真实线程收束；重启UNKNOWN阻止自动重装。核心31/0、关联54/0、最终16/0，DOM root/proxy；2875 collect-only / 79XML当前节点1264 distinct passed，非整库。
- [x] AI总开关独立ai-master保存区、内部变化版本、HTTP/WS/原表单固定回执、冲突复选框/版本/key保留与明确确认。相关15/0、加强4/0、DOM root/proxy；2877 collect-only / 81XML当前节点1266 distinct passed，非整库。
- [x] AI稳定UUID/独立编辑版本、迟到验证与Key健康屏障；供应商新增/编辑/删除共享HTTP/WS/原表单CAS/安全回执。删除核对子配置版本，原对象消失保留草稿；最新核心97/0、旧入口22/0、DOM root/proxy通过。2895 collect-only，90XML当前完整节点最新1276 distinct passed，非整库。
- [x] Key/模型八种本地操作共享CAS/HMAC安全回执，新增集合版本/逐项版本/冷却健康版本；原表单及冲突草稿、密钥浏览器DOM保留。三scope模型链CAS、原排序复用、删除触发scope版本、HTTP/WS/原表单与显式确认通过。最终合并86/0、排序及原单元93/0、相邻80/0、旧入口46/0、独立版本加强1/0、DOM root/proxy；2967 collect-only / 103XML当前节点1356 distinct passed，非整库。
- [x] 路径AI参数/prompt/model-source公开命令接HTTP/WS/原表单；面板/重置共用版本，模型来源独立，Series最终事务复查。五文件38/0、旧入口90/0、DOM root/proxy通过；2991 collect-only / 107XML当前节点最新1380 distinct passed，非整库。
- [x] AI单项/供应商/全局验证和模型拉取持久受理，固定原配置/Key信封、互斥claim、断线继续、停机收束、重启UNKNOWN；HTTP/WS/原表单共享202回执，结果页/最近10项恢复。拉取清单添加在保存事务复查配置，拒绝过期选择并保留草稿。六文件134/0、最后三文件29/0、旧页面/合同42/0、DOM root/proxy；3019 collect-only / 120XML当前完整节点最新1408 distinct passed，非整库。
- [x] 密码库/API Key独立版本、原子写入/安全回执、HTTP/WS/原表单及冲突草稿；原生直接200显示首次明文，删除全局pending_api_key，同键重放不旋转/不再回明文，密码登录专属管理Key。迟到解压成功核对原UUID/版本；真实038升级/DDL回滚/重启与取消后查回执通过。旧入口200/0、核心/迁移/归档46/0、相邻43/0、DOM root/proxy；3039 collect-only /126XML当前节点最新1439 distinct passed，非整库。
- [x] v2任务/作品控制、账号别名与能力目录，共享入队同来源复用/异来源先停、HTTP幂等头/改名后审计；移除不可达complete_if_ready/_torrent_verify。维护四入口补共享JSON/WS/原表单、固定范围/逐件CAS/精确输入/回执、只读查询/显式续作与缓存回放；原生mobile/desktop放行api/v2。五文件57/0、归档等七文件214/0、旧维护12/0、DOM root/proxy。
- [ ] 收尾全P10新增合同及旧设置/API/身份/账号/队列集中回归，逐路由和阶段差异最终核验。3071 collect-only/142XML当前最新1579 distinct passed，非整库；P10未验收/暂存/提交，HEAD仍f60412b，不把切片冒充阶段交付。
- [ ] 全阶段验收后立即暂存代码/正式测试/同步文档、本地提交并核实 SHA；rc12 不提前提升，039 不冻结。

## R97 / P10 整阶段验收（2026-10-16，待本地提交）

- [x] 全域共享命令/对象CAS/持久回执，旧HTTP与Web、WS/native和v2任务域共用领域实现。
- [x] 三类手动来源补共享PUT/表单；画廊合并目标版本贯穿真实停止，目标变化不合并也不重试旧意图。
- [x] 真实038升级保留旧reference、失败039 DDL整体回滚及guard重新初始化保留；027–038不改。
- [x] WS取消修前复现/修后收束；集中612/0、相邻76/0、来源组82pass/1时间夹具失败后完整详情39/0，真实DOM root/proxy通过。
- [x] 3088 collect-only；152XML最新当前节点1623 distinct passed、0failure/error/skip；compileall/CodeGraph/diff-check及用户文档同步。
- [ ] 立即整阶段暂存、审查、本地提交并核实记录SHA；039随提交冻结，不拆P10切片。仅排除models.py既有BOM。
- [ ] P11与P12按原范围继续；Linux guardian实跑尚未完成，总目标active。

上文P10“进行中/未验收”和旧计数均为历史快照，本节及P10审计顶部为当前事实。
