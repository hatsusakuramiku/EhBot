# Findings And Decisions

## Requirements
- Build a new project from scratch.
- Subscribe to Telegram channels and accept direct private shares.
- Support channels where the Bot can be an administrator and channels where it cannot.
- Recognize preview-only, preview-plus-archive, and archive-only message patterns.
- Put matching items into a manual Web review queue.
- Download only after approval, primarily from Telegram.
- Optionally obtain an archive automatically from ExHentai when only a gallery link exists.
- Parse metadata from Telegram first and fall back to ExHentai.
- Convert downloaded content into CBZ and embed metadata.
- Delete original archives by default, with an option to retain them.
- Support Docker and expose a configurable HTTP port; TLS and reverse proxy are user-managed.
- Run on 1 CPU and 512 MB in low-resource mode; allow higher memory profiles.

## Research Findings
- Telegram Bot API exposes `channel_post`, but hosted `getFile` downloads are currently limited to 20 MB.
- A local Telegram Bot API server removes the file-size limit but adds unnecessary resource overhead for this target.
- Telethon uses MTProto and can run Bot and user sessions in one asyncio process.
- Telethon session files contain reusable authorization material and must be protected as secrets.
- E-Hentai `gdata` accepts gallery ID and token, returns structured metadata, supports up to 25 galleries per request, and requires throttling after bursts.
- ExHentai private access commonly uses `ipb_member_id`, `ipb_pass_hash`, and `igneous` cookies.
- ExHentai archive acquisition is HTML-workflow based rather than covered by the stable metadata API; it needs an isolated adapter.
- ComicPacker is MIT-licensed and models useful ComicInfo fields.
- ComicPacker currently creates the complete CBZ as bytes before writing, which is unsuitable for large files under tight memory limits.
- ComicPacker's current compressed-file implementation handles ZIP only despite README claims for RAR and 7Z.
- Channel preview links are `telegra.ph` pages, and the URL is carried in a caption `text_link` entity rather than in the message text.
- `api.telegra.ph/getPage/<path>?return_content=true` returns an ordered node tree for any published page, so images can be read from the official API instead of scraped HTML. gallery-dl's `telegraph.py` scrapes `<figure>`/`<img>` from HTML and is a usable fallback shape.
- The preview images are not hosted on `telegra.ph/file/`. Each channel runs its own Telegram file proxy — `image.dangernsfw.win` serves `<sha256>.webp`, `pic.850123.xyz` uses the base64 `file_id` as the path — both behind Cloudflare, and both answer 403 without a browser `User-Agent` and a `Referer`.
- Preview pages carry the complete page set: 22/22, 15/15 and 78/78 measured against the ExHentai gdata `filecount` on 2026-08-21.
- Preview images are re-encoded, not originals. Gallery 1655718 is 145,185,851 B over 15 pages at the source versus 7,895,214 B over the same 15 pages on the preview page, normalized to 1280 px wide. That is roughly 5–10 % of the original bytes.
- A `file_id` taken from a third-party proxy URL cannot be handed to this project's own bot: `getFile` only serves files the calling bot has access to.
- The ExHentai gdata response already returns `torrentcount` and `torrents[{hash, added, name, tsize, fsize}]`, so a torrent can be discovered with no extra request and no cookie.
- Torrent availability is not guaranteed: gallery 1655718 reports `torrentcount=0` while galleries 4108964 and 4076223 each report 1 (measured 2026-08-21).
- A torrent's `fsize` differs from the gallery `filesize` — 126,838,245 B versus 139,262,241 B on gallery 4108964 — because the torrent holds the uploader's original `.zip` rather than the re-derived gallery pages.
- `torrents[].hash` is an infohash, not a link. The `.torrent` file has to be resolved through `gallerytorrents.php?gid=&t=` with a logged-in session, and the file embeds the account passkey in its announce URL, which makes it a credential.
- EH trackers require that passkey and are typically flagged private, so a bare magnet link built from the infohash cannot announce.
- `POST /api/v2/torrents/add` accepts the `.torrent` body as a multipart file field, so no shared directory or watch folder is needed to enqueue one. It answers `Ok.` without returning a hash, so the locally computed infohash is the only way to look the torrent back up.
- `autoTMM` must be sent as `false` on add, otherwise qBittorrent's category rules override the `savepath` passed in the same request.

## Technical Decisions
| Decision | Rationale |
|----------|-----------|
| Use `httpx` and `selectolax` for ExHentai | Supports streamed HTTP and low-overhead HTML parsing without Chromium |
| Use standard `zipfile` for ZIP/CBZ | Enables bounded-memory member-by-member copying |
| Use `7zz` as a subprocess for RAR/7Z | More predictable format coverage than Python-only archive packages |
| Store source and job identity keys | Prevents duplicate candidates and duplicate downloads after restarts |
| Keep one download and one conversion active in low-resource mode | Preserves Web UI responsiveness and limits memory peaks |
| Store metadata provenance per field | Allows manual values to override Telegram and Ex-derived values safely |
| Treat the Telegraph preview page as a fallback source, never the default | It clears the Bot API 20 MB ceiling with complete page coverage, but delivers a 1280 px re-encode at 5–10 % of the original bytes |
| Route oversized books to the EH torrent before anything else | It is the only free original-quality source; Archive Download costs GP and the preview page costs quality |
| Demote ExHentai Archive Download to a manual button | Spending a limited GP balance is an operator decision, not a routing default |
| Delegate BitTorrent to an external qBittorrent over its WebAPI | Keeps P2P code and unbounded memory out of the main process and preserves the 1C/512 MB target |
| Verify the `.torrent` infohash locally against the gdata `hash` before pushing it | Catches a mis-parsed or stale `gallerytorrents.php` row before it becomes a wrong download |
| Hard-link or copy out of the torrent save directory, never move | Moving the payload breaks seeding, and the client keeps seeding by default |
| Leave a stalled torrent in `WAITING_TORRENT` instead of auto-falling back | Silent degradation would hand the operator a 1280 px book that looks like an original; the stall is surfaced with the seed count and the choice stays manual |
| Apply the page-count consistency gate to the preview route only | Torrent `fsize` legitimately differs from gallery `filesize`, so the same check would reject valid originals |
| Have the Telegraph provider emit a `ZIP_STORED` archive rather than a CBZ | `ConversionService` already consumes the `ARCHIVE` artifact, so safety checks, ComicInfo injection and atomic publication are reused unchanged |
| Park a Telegraph page-count mismatch in `NEEDS_INFO` | A split preview page or a lossy image host would otherwise publish a silently incomplete book |
| Record preview provenance in ComicInfo `ScanInformation`, not in the file name | Keeps the door open for replacing a preview-grade CBZ with the original later without breaking library indexes |
| Write a `ScanInformation` line for every source, not just the preview route | Makes preview-grade books queryable so they can be back-filled once a torrent appears |

## Issues Encountered
| Issue | Resolution |
|-------|------------|
| Exact ExHentai archive page flow can change | Define it as an adapter with integration fixtures and explicit failure states |
| RAR/7Z memory use depends on archive dictionary | Keep ZIP as the guaranteed 512 MB path and recommend 1 GB for general archive support |

## Resources
- https://core.telegram.org/bots/api
- https://github.com/tdlib/telegram-bot-api
- https://docs.telethon.dev/en/stable/concepts/sessions.html
- https://ehwiki.org/wiki/API
- https://github.com/hatsusakuramiku/ComicPacker
- https://github.com/nonpricklycactus/Ehentai_metadata

## Security Notes
- Never commit or log Telegram tokens, API hashes, sessions, ExHentai cookies, or administrator credentials.
- Validate archive paths, extracted size, file count, compression ratio, and file signatures.
- Only fetch URLs from configured Telegram and E-Hentai/ExHentai sources; revalidate redirect targets.

## Implementation Findings: Token-Only Connections
- Telegram Bot Token alone can authenticate only against the Bot API. Telethon/MTProto still requires an API ID and API Hash.
- The Token-only connector therefore supports `getMe` verification and `getUpdates` polling, while large-file MTProto download remains explicitly deferred.
- Telegram Bot API embeds the Token in the request path, so log redaction must cover `/bot<TOKEN>/...` paths in addition to query strings and headers.
- ExHentai connection settings are the `ipb_member_id`, `ipb_pass_hash`, and `igneous` Cookie values; they should be saved as one atomically replaced private secret document.
- Connection forms must never render saved credential values. Only configured identity and health status are visible.
- The existing `main.py` already owns authentication and lifecycle wiring; connection routes should be isolated in a router and call one connection manager instead of adding provider details to `main.py`.
- The existing Windows SID/Linux mode hardening in `private_files.py` should back a generic atomic private-text writer shared by bootstrap passwords and external credentials.
- The existing database class uses short `asyncio.to_thread` SQLite operations, so Telegram update persistence should follow that established boundary without introducing an ORM.

## 2026-08-20 Extensible Archive Processing Proposal
- The requested canonical pipeline is: download archive, check split volumes, try passwords, run safety tests, extract with a format-specific tool, then pack CBZ with the same backend.
- `app/conversion/convert.py` currently supports only ZIP/CBZ through `zipfile`; RAR and 7Z are detected but not supported. This is the primary conversion seam for the new backend interface.
- `DownloadService` and `ConversionService` already provide durable worker boundaries. The archive backend selection should be stored in the task details snapshot so retries use the same tool profile.
- 7zz should be invoked as a controlled subprocess with argument arrays and timeouts. Direct `ctypes` loading of arbitrary DLLs in the Web process is unsafe; DLL-capable formats should use a dedicated bridge subprocess implementing the same backend contract.
- Common split names include `.partN.rar`, `.rar/.r00`, `.7z.NNN`, and `.zip.NNN`. Missing volumes should be recoverable (`WAITING_VOLUMES`) rather than terminal failure.
- Passwords must be decryptable for retry, so hashes alone are insufficient. Store encrypted password values in a private vault and keep only password-entry IDs and attempt outcomes in task/audit data.
- Safety checks must happen after archive listing/password validation but before full extraction: traversal, magic number, file count, expanded size, compression ratio, timeout, temporary-directory isolation, and atomic `.part` publication.
- The detailed design and phase checklist are in `ARCHIVE_PROCESSING_PROPOSAL.md`; no implementation code was changed in this planning session.

## 2026-08-20 Review And Queue Workflow Intake
- The current project log says review transitions require reasons and downloads are triggered manually after approval; both contracts must change for this request.
- The current project already has persistent `download_jobs`, idempotent Telegram enqueueing, ExHentai metadata fetching, Chinese tag enrichment, and source metadata rules. The implementation should compose these services instead of adding a second queue or metadata store.
- The user asked to review an automatic-approval design before implementation. Keep that work as a documented proposal, separate from the executable changes.
- Memory registry lookup could not start because PowerShell intermittently returned `CreateProcessAsUserW failed: 1920`; the supplied memory summary contained no EhBot-specific implementation history beyond the repository files.

## 2026-08-20 CodeGraph Findings
- `GET /candidates` currently passes only `Database.list_candidates()` output to `candidates.html`; it does not load `metadata_values`, so queue rows cannot show enriched author/tags fields.
- `POST /candidates/{id}/approve` reads an optional `note`; `POST /candidates/{id}/reject` requires a `reason` form field at the route/service boundary. Neither route enqueues a download.
- `DownloadService.enqueue_telegram_download()` already enforces `APPROVED` status and uses `telegram:{candidate_id}:{file_unique_id or file_id}` as an idempotency key. Reuse this method after approval.
- `DownloadService.list_active_jobs()` hard-codes the active-state set. A general allowlisted state filter is the smallest change needed for separate Processing and Failed views.
- Candidate status and download-job state use different vocabularies. CodeGraph initially exposed both; inspection of `dashboard.html` and `Database.candidate_counts()` confirmed the reported controls use candidate statuses.
- `downloads.html` renders only one `active_jobs` collection and has no state selector or active-filter indicator. Its empty text also assumes every view is the active queue.
- `candidates.html` renders each candidate as one full-row anchor. It has no form, checkboxes, batch action controls, or metadata beyond title and ExHentai gallery id, so batch review requires a small structural rewrite of this template.
- `dashboard.html` renders Pending Review and Needs Info as links, but Processing and Failed as inert `<div>` elements even though their counts come from `candidates.status`. Add filtered candidate queue routes and convert those two metrics to links.
- `ExHentaiService.fetch_metadata_for_candidate()` is single-candidate, but `GdataClient.fetch_many()` already supports batches of 25 with throttling. Automatic review enrichment should add one service-level batch method, skip candidates that already have ExHentai metadata, and degrade to the existing queue if the external request fails.
- `ReviewService.reject_candidate()` alone enforces a non-empty reason. Approval already accepts an optional note. The requested contract change is therefore a narrow removal of rejection reason validation plus template/route fields; keep revision reasons unchanged because revision is a different operation.
- ExHentai archive retrieval currently downloads synchronously and records a completed job; the durable download worker supports only Telegram. Approval of Ex-only candidates needs an ExHentai pending job path or it would not satisfy “纳入下载队列”.
- `candidate_detail.html` exposes four separate manual concepts: metadata fetch, immediate ExHentai archive download, Telegram download trigger, and reason-based review. The requested workflow should make approval the normal enqueue action while preserving manual retry/debug actions only where they remain useful.
- Existing review tests explicitly assert that an empty rejection reason returns 400 and approve a photo-only candidate. Those tests encode the old contract and must be replaced with reasonless rejection plus approval/enqueue cases using a real archive or ExHentai reference.
- Current Telegram ingestion extracts only an explicit first-line `Title` (or an inferred filename/gallery title). It does not parse author or tags from arbitrary message text. Without a documented Telegram message format, do not invent an author/tag grammar; show Telegram-derived title and enrich the remaining fields from ExHentai when a gallery reference exists.
- `progress.md` referenced an untracked `app/candidates/reference.py`, but the file is no longer present in the current workspace and CodeGraph has no such node. Treat the current tracked parser as the source of truth.
- No current code path updates `candidates.status` to `PROCESSING` or `FAILED` when a download job is claimed or fails. The dashboard counters/routes are otherwise disconnected from worker reality; the worker must synchronize candidate status at claim/failure (and use the existing `DOWNLOADED` terminal status on completion).
- Download integration tests already cover Telegram enqueue idempotency and artifact completion. Extend this file for candidate-status synchronization and keep those existing contracts intact.
- Candidate status synchronization must preserve conversion: after archive completion the candidate should become `DOWNLOADED`, and `ConversionService` must accept `DOWNLOADED` in addition to the old `APPROVED` state.
- Both download and conversion workers currently claim any row whose state is `PENDING`, without filtering by provider. ExHentai queue support would expose this race more often, so constrain each worker to its owned providers as part of the queue fix.

## 2026-08-20 Implementation Decisions
- The review queue performs best-effort metadata enrichment only for visible candidates that have an ExHentai reference and no stored ExHentai metadata. It then reloads the candidate list because metadata rules may have moved a candidate out of `PENDING_REVIEW`.
- Approval source priority is Telegram archive first, ExHentai reference second. Candidates with neither remain pending and surface a validation error.
- ExHentai approval creates an idempotent `exhentai:{candidate_id}` pending job. The existing ExHentai downloader attaches the artifact to that job; the download worker owns state transitions.
- Candidate download lifecycle is `APPROVED -> PROCESSING -> DOWNLOADED` or `APPROVED -> PROCESSING -> FAILED`. Conversion now accepts `DOWNLOADED` so the existing manual CBZ action remains usable.
- Rejection and approval no longer accept a user reason. `NEEDS_REVISION` retains its required change request because it is a separate corrective workflow.
- Candidate detail reconstructs attachments from `source_messages.attachment_json`; focused tests can safely replace an archive fixture with a photo fixture to verify the no-download-source and Ex-only branches.

## 2026-08-20 Metadata Label And Tag Bug Intake
- CodeGraph shows every value in `app/review/models.py::FIELD_LABELS` is literally stored as question marks, so the UI is rendering the current source faithfully.
- `enrich_metadata()` stores the complete upstream set in `TagsRaw`, then calls `TagTranslator.translate_tags()` for `Tags`. The current behavior must be tested because the reported Chinese row contains fallback originals.
- Required contract: `TagsRaw` contains all upstream tags; `Tags` contains only successful Chinese matches. Both contribute to tag filtering and ComicInfo export.
- No `CONTEXT.md` or ADR result was obtained because the Windows runner again returned `CreateProcessAsUserW failed: 1920`; CodeGraph and repository planning files remain the current evidence sources.

## 2026-08-20 Metadata Bug Reproduction
- Red-capable command: `.\.venv\Scripts\python.exe -m pytest tests\unit\test_review_models.py tests\unit\test_tagdb.py -q`.
- Exact failures: `FIELD_LABELS` differs on every known field because its values are literal question marks; enriched `Tags` contains one extra unmapped raw tag (`female:unmapped tag`).
- `TagsRaw` already preserves the complete upstream sequence correctly, so the storage split exists and only the Chinese-selection/display/export contracts need correction.
- Ranked hypotheses: source-label corruption; fallback behavior leaking through `translate_tags()`; `TagsRaw` hidden by generic raw-field rendering; ComicInfo reading only `Tags`.
- Keep `TagTranslator.translate_tags()` unchanged because its documented fallback contract may have other callers; use successful `translate_tag()` results only inside `enrich_metadata()`.
- Promote `TagsRaw` out of the collapsed generic-original section and add it to candidate queue summaries as `raw_tags`, so the two rows are both visibly tag data.
- Merge `TagsRaw` followed by `Tags` at the conversion boundary with stable de-duplication; the ComicInfo XML builder can retain its existing single `tags` input.
- The completed implementation also combines both fields for source metadata rules, so required/forbidden Tag conditions can use either upstream names or matched Chinese names.
- Automated Web integration confirms original tags precede matched Chinese tags and both render as tag chips in queue/detail HTML. Screenshot verification is blocked by the local in-app Browser plugin trust-path initialization error, not by the application server.

## 2026-08-20 Automatic Approval Rule Implementation Intake
- The user approved the proposal in `AUTO_APPROVAL_PROPOSAL.md` for implementation.
- The executable rule representation must be a persisted, allowlisted JSON AST. The readable DSL is a snapshot for display, copying, and audit only.
- Evaluation order is enabled rules by ascending priority; the first match performs only the existing approval-and-enqueue action. Any uncertainty leaves a candidate in manual review.
- Rules are stored in `auto_approval_rules` migration 008. The server validates and normalizes the AST before storage and derives the DSL snapshot itself, so submitted display text cannot change execution.
- The executor runs after pending-review queue enrichment. It verifies the candidate is still reviewable and delegates source validation, approval, and idempotent job creation to the existing orchestration path.
- `AUTO_APPROVE` is an additional review action after normal approval; its payload contains rule identity/version, AST and DSL, evaluated conditions, effective metadata precedence snapshot, and download job IDs.

## 2026-08-20 Extensible Archive Processing Implementation
- The proposal in `ARCHIVE_PROCESSING_PROPOSAL.md` is now implemented as Phase 10 runtime code; the document remains the design record.
- `ArchiveProcessor` owns the fixed order: resolve volumes, inspect, resolve password, validate safety, extract or stream, pack CBZ with the same backend, then atomically rename `.part` into the library.
- The registered profile decides the backend. `zipfile-default` is preferred for ZIP because it streams members into the CBZ without a full extraction, which keeps the 512 MB profile's guaranteed path intact. `7zz-default` handles RAR/7Z, split volumes, and encrypted ZIP that `zipfile` cannot open.
- `SevenZipBackend` spawns the registered executable with a fixed argument vector (`shell=False`, per-profile timeout) and parses `l -slt` output. The main process still loads no third-party DLL; a DLL-backed tool would be registered as a `BRIDGE` profile pointing at a bridge executable.
- Format detection prefers the magic number and falls back to the file name, because later volumes of a split archive carry no signature of their own. A ZIP renamed to `.rar` is therefore handled by the streaming ZIP path.
- Safety validation runs entirely on the listing before any bytes are written: absolute paths, `..`, symlinks, nested archives, member/total size, compression ratio, directory depth, and image magic-number cross-checks. Pages are renamed to `0001.jpg`-style stable names, so duplicate or non-ASCII member names cannot collide.
- Passwords are attempted as `last successful -> priority -> empty`, matching the proposal. Vault entries use scrypt key derivation plus an HMAC-SHA256 keystream and tag, so no new third-party crypto dependency was added. Plaintext never enters logs, audits, or task details; only the entry ID is recorded.
- `CONVERSION_WAITING_VOLUMES` and `CONVERSION_WAITING_PASSWORD` are recoverable: re-enqueueing the same candidate resets the existing job to `CONVERSION_PENDING` instead of creating a duplicate, so the idempotency key contract is unchanged.
- `KEEP_ORIGINAL` is now an operator setting rather than an environment variable. The original archive is only deleted after the CBZ artifact row is committed.
- `app/archive/__init__.py` deliberately does not re-export `ArchiveSettingsService`, because that module imports `Database`, which imports the archive models; re-exporting it creates a circular import.

## 2026-08-20 Managed 7-Zip Toolchain
- Requirement correction from the user, twice: do not rely on a pre-installed host 7-Zip, fetch the newest official binary automatically at install time, and design for Linux/Docker rather than adding Windows bootstrap complexity.
- Upstream releases are published in `ip7z/7zip` on GitHub with per-asset SHA-256 digests. `26.02` is the pinned version.
- Linux and macOS assets are `.tar.xz`, unpackable by the standard-library `tarfile`, so no external archiver is needed to install the archiver. The Windows assets are self-extracting executables, which is exactly the bootstrap problem avoided by not targeting Windows here.
- Linux assets contain `7zz` and `7zzs`. `7zzs` is statically linked and is preferred because `python:3.12-slim` has no `libstdc++`.
- PyPI offers no usable official binary package: `7zip-bin`, `py-7zip`, and `sevenzip` are absent, and `py7zr` is pure Python with no RAR support. Pinned upstream assets are the only route that covers RAR.
- Trust is pinned in source, not discovered at runtime: the version plus one digest per platform. A mismatch raises `TOOLCHAIN_DIGEST_MISMATCH` and leaves any existing install intact, so a compromised mirror or truncated transfer cannot install an executable.
- Extraction matches member names against a fixed allowlist instead of joining archive-controlled names onto the destination, which removes tar path traversal from the threat model entirely.
- Installs are version-isolated (`<data>/tools/7zip/<version>/`) and idempotent, staged in `.incoming`, then promoted with `replace()`. Upgrading the pin never overwrites the binary a running worker is using.
- Resolution order is managed install, then `PATH`, then the Windows default directory. This keeps a developer host with its own 7-Zip working while a container always gets the pinned build.
- Startup provisioning is best effort: `ensure_toolchain()` warns instead of raising, because the built-in streaming ZIP path must keep working when the archiver is unavailable.
- Real-binary QA was worth the effort: it exposed that header-encrypted archives fail at `inspect` rather than at extraction, and that `7zz` stores full staging paths unless it is invoked with `cwd` set.
- Tests stay offline by construction. `tests/unit/test_toolchain.py` builds a real `tar.xz` in memory and injects the download; `tests/conftest.py` disables auto-install and blocks `toolchain._download` for every test.

## 2026-08-20 Docker Linux Verification
- Windows structurally cannot verify the managed 7-Zip install: the pinned assets are Linux `.tar.xz` builds and `asset_for_platform()` refuses Windows on purpose. Docker is not a convenience here, it is the only place the contract can be proven.
- Running on Linux immediately exposed a defect Windows had been hiding. `ensure_toolchain()` caught only `ArchiveSettingsError`, so any other exception escaped the FastAPI lifespan and killed startup. Windows never reached that code because the platform check returns early. The lesson is that a "best effort" startup step must contain every exception, not just the one it expects.
- A test guard that cannot trigger in production is a weak guard. The download blocker raised `AssertionError`, which the application would never see, so it masked the real degraded branch. Raising the production error type (`TOOLCHAIN_DOWNLOAD_FAILED`) made the startup bug visible.
- `os.environ.setdefault` is the wrong tool for test isolation: an operator shell exporting the variable silently re-enabled network access for the whole suite. Overwriting with `MonkeyPatch.setenv` is the correct behavior.
- Never hand-write a dependency list for a container check. The first attempt missed `pwdlib` and produced 12 collection errors that looked like code failures; `uv sync --frozen --all-groups` keeps the check tied to `pyproject.toml`.
- Escaping matters when a Python script is embedded in another Python script: `\xff` in a non-raw triple-quoted string is resolved by the outer interpreter and reaches the container as a non-ASCII byte. `bytes([0xFF, ...])` is escape-proof.
- Installing the toolchain's one dependency into a small base image, rather than with `pip install` at run time, is what makes the `--network none` offline stage possible.
- Confirmed on real Linux: `7zzs` is the extracted binary, it runs on `python:3.12-slim-bookworm` with no extra runtime library, it reports both `Rar` and `Rar5`, the install lands in `/tmp/ehbot-tools/7zip/26.02/`, and a tampered payload is rejected with `TOOLCHAIN_DIGEST_MISMATCH` without installing anything.
- Host note for the next agent: Docker Desktop needed the WSL2 backend (the Hyper-V backend had no Linux VM), and `daemon.json` still lists a misspelled mirror `doocker.1ms.run`, so pulls depend on the proxy.

## 2026-08-20 Download Queue Controls And Configurable Paths
- The reported "download failed" was not a network or proxy problem. Telegram's Bot API refuses `getFile` for anything over **20 MB** with `Bad Request: file is too big`, and that ceiling belongs to the API, not to the deployment. No retry policy, proxy or timeout change can move it; only MTProto can.
- **A fallback error branch that discards the upstream description is worse than no error handling.** `_error_for` turned a precise `file is too big` into "Telegram 拒绝了连接请求", which pointed the investigation straight at the proxy. A generic branch must always carry the original text through.
- Distinguishing permanent from transient failures is what makes a retry button honest. `PERMANENT_DOWNLOAD_ERRORS` hides retry for causes that cannot change, so the operator is not invited to repeat a guaranteed failure.
- A failed job that disappears from the queue cannot be retried. The listing query moved from `ACTIVE_DOWNLOAD_STATES` to `OPEN_DOWNLOAD_STATES` because "what the worker will pick up" and "what the operator can act on" are two different sets.
- `PAUSED` is intentionally neither active nor terminal. Because the worker claims only `PENDING`, pause needs no worker-side logic at all; the state itself is the mechanism. This is also why pause is refused for `DOWNLOADING`: suspending a live HTTP stream and resuming it correctly is a much larger contract than cancelling it.
- Retry reuses the existing job row instead of inserting a new one. Inserting would either collide with the `idempotency_key` or require weakening it, and it would scatter the attempt history across rows.
- Cancel has to think about the candidate, not just the job. Returning it to `PENDING_REVIEW` keeps it in the review queue; leaving it `APPROVED` would strand it with no job and no way back.
- Configurable directories reused the existing `archive_settings` key/value table rather than adding a table or new environment variables, which kept the change to two keys plus validation.
- **Resolve a configurable path per task, not at construction time.** The first instinct was to pass the path into each service's `__init__`, which would have required a restart for every change. A provider callable (`work_path_provider`) makes the override effective immediately and keeps the environment value as the fallback.
- Validating a directory means proving it is writable, not just parsing it. `save_paths` requires an absolute path and calls `ensure_writable_directory`, so a typo fails at save time instead of at the next download.
- The data directory is deliberately not editable from the page that stores the setting: the setting lives in the database inside that directory.
- Verifying against the operator's running container was the wrong move once they had rotated the bootstrap password. A throwaway database inside the same container proved the same transitions without touching their data, and is the pattern to reuse.


## 2026-08-22 Operator-Facing Bugfix And Workflow Completion
- The auto-approval rule surface the operator asked for is one pattern form, `Regex({Title}, 'regexstring')`, not the "可选内容" condition builder. The persisted AST is `{"kind": "regex", "field", "pattern"}`; the DSL snapshot renders as `Regex({Field}, "...")` and is derived, never executed.
- Regex syntax must fail at save, never at run: `validate_rule_ast` calls `re.compile` and raises `RuleValidationError` on a `re.error`, and the invalid rule is not persisted. `_compile_regex` is `lru_cache`d because a regex is read on every candidate evaluation.
- Matching is case-sensitive by default, and deliberately so: operators get `(?i)` inline flags and `^`/`$` for control, while `Rating`/`Pages` are excluded from `REGEX_FIELDS` because they are numeric values, not text.
- A completed download with no packaging path shown is an invisible pipeline. Two changes surfaced it: `DownloadJobSummary.artifact_cbz_path` (the produced CBZ on the task page) and a `pack-status` block on the candidate detail page with distinct `.pack-done` / `.pack-failed` states.
- Auto-pack is a decision, not a side effect: `auto_pack_after_download` defaults off exactly like the existing `torrent_auto_pack`, and a live failure during the work proved a default-on would silently convert in tests and fail the worker. Enabling packaging is an explicit operator action.
- A manual-add entry point is an operator-declared intent, so the created candidate is `APPROVED` immediately (contract), not routed through review. It still needs the corresponding source: an EH link needs ExHentai configured, a magnet needs qBittorrent; an unconfigured client is a clear 400, not a silent queue.
- The magnet path reuses qBittorrent's `/torrents/add` (`add_magnet`, same 409-absorbing add semantics as `add_torrent`) and branches inside `TorrentService.push_for_candidate`, so a magnet candidate never needs an ExHentai gid.
- The false 「必须要审批后才能进入下载队列」 surfaced because the download enqueue rejected a candidate it had already approved and enqueued; the enqueue now accepts `APPROVED` and `DOWNLOADED`, which is the contract phase 7 established.
- Retry-as-recovery for a torrent means reading the payload before restarting the chain: if the file is readable the job succeeds, otherwise the chain restarts, and an exception stops with the reason shown. Removing `TORRENT_CONTENT_UNREACHABLE` / `TORRENT_CONTENT_UNEXPECTED` from `PERMANENT_DOWNLOAD_ERRORS` is what lets a path fix ever be retried to success.
- Settings are re-read per add/retry through the per-action provider callables established in phase 13, so a setting change is effective on the next attempt without a restart.
- A dataclass field with a default must come after all non-default fields; placing `artifact_cbz_path` before `created_at`/`updated_at`/`details` raised `TypeError` at import, which reads as a confusing ordering error rather than a value problem.
- The magnet no-client guard cannot be `app.state.<service> is None`, because services are always constructed; the real gate is `torrent_client().is_configured`. The first instinct was a guard that never failed, which the test immediately exposed.
- `TestClient` follows redirects by default, so an assertion on a 303 had to pass `follow_redirects=False`; otherwise the response was the rendered target and the intended status was invisible.

## 2026-10-13 批准方案转实施

- 方案已获批，当前 P1/R88，接手状态见 DOWNLOAD_QUEUE_AND_SHARED_API_IMPLEMENTATION_PLAN.md。
- 管理员有稳定 id，但启动/登录/改密码按 admin；用户名与身份需分离。refresh 和改密码撤销目前分步写，要数据库事务而非 endpoint 锁。
- Cookie 是签名 session，Web 仅验 authenticated；新设计必须核对认证代次并覆盖 WS，独立设备会话不能用全局当前 session 替换。
- P1 已新增 026 与固定身份/revision/认证代次；Web/API 共用认证和配置核心，原审阅复现及首次封面失败记录保留。
- 单靠改密后另调用撤销函数会留下窗口；改密、旧浏览器/密码凭据撤销与当前浏览器续接必须同事务。提交前同时核对发起凭据仍有效，避免注销时已通过密码校验的旧请求继续提交。
- 旧签名 Cookie 缺乏可撤销的设备身份，不能反复“升级”为新 session，否则注销后可重放旧 Cookie；026 强制旧 Cookie 重新登录。
- P1 新合同 12 条通过；核心/设置首次旧契约 6 条失败已修复并定向复验；27 文件共 370 个不同用例最终通过，2113 collect。版本 rc4/026，仅源码/临时库验证，无镜像发布。
- P2 接口调查：DownloadService 旧 retry 在状态裁决前执行 torrent verify/push/abandon；新控制核不能只包一层 API revision，应先事务受理意图再启动副作用，详见已批准方案。

## 2026-10-13 P2 / R89 队列核心实施证据

- DownloadService 入队仅按 provider 的 key 去重，worker SELECT/UPDATE 无短事务；conversion 同样有双领取窗口。027 的 queue_tasks 下载槽约束 + 既有 worker BEGIN IMMEDIATE 收口，不新增调度器。
- EH/Telegraph 在完成时自行 INSERT/UPDATE download_jobs，torrent poller、归档移除也直接写表；因此仅外层 API 包装无法覆盖旧路径。本阶段以 SQL trigger 作兼容投影 adapter，任务/尝试历史与约束覆盖所有旧写者；generation 的实际 provider 提交资格仍需 P3 显式传递。
- 取消前先 abandon、重试前先 verify/push 均改为先持久受理。STOPPING 不释放槽、不伪报远端已停；queue_operations 不带 candidate/task 外键，可在记录移除后继续收尾。停止处理器明确在 P3，未部署。
- 用户提出运行记录不能移除：public remove/purge 在文件动作前检查所有来源；SQL 最终守卫禁止活动 job 删除。较早来源运行而最新来源完成的合同已覆盖；文件删除与新入队间的完整协调在 P3。
- 21 个新合同通过；新旧审批证据/迁移回滚、跨凭据回执、SQL 注入失败时状态/操作/回执同回滚、下载和打包双 worker、旧整数 ID 复用、STOPPING/陈旧提交、旧来源移除保护均有临时库证据。最终回归汇总见 progress/R89。

## 2026-10-14 R89 完成核验补漏

- 实际新复现的 4 条失败：FAILED/COMPLETED 种子尝试在后续 STOPPING 中被改写；内部 retry 槽冲突泄露 SQLite 异常；打包旧完成被拒后又写 FAILED 触发同一 guard。均不是 P3 的物理停止测试。
- 追加 028 修正 adapter 对 finished_at 非空历史的更新，027 冻结；内部命令统一槽冲突业务反馈，下载/打包复用陈旧提交拒绝集合。028 不凭空恢复此前已丢失的历史。
- 新增 7 用例，核心合同 28；同键并发、八天未结束操作回执、HTTP/Web/WS 同结果与混合批量裁决已验。19 文件完整 460 passed / 0 failed / 0 skipped，2141 collect-only。要求映射见 R89_COMPLETION_AUDIT，实际命令与证据见 progress/R89。

## 2026-10-14 P3 实施调查与实际复现（尚未验收）

- 原始 preview sibling 触发条件已转正式 HTTP 回归：第一张图片失败时另一请求继续运行，先红 1/3；改成显式取消/等待所有子任务，失败、超时、外部取消连同既有 Telegraph 单元共 40/0。
- asyncio.to_thread 的 await 取消不结束线程；run_sync 在取消（含重复取消）时等待真实线程退出，再传播 CancelledError。TG/EH/Telegraph/torrent/conversion 的线程操作已接入，真实打包线程取消不晚替换旧 CBZ 有正式证据。
- 使用 execution_scope 的 ContextVar 将真实 task/attempt/generation 传至供应商和线程；Database.connection 在写事务提交前核对身份。辅助元数据写入也回滚，避免只屏障主产物而让旧来源污染元数据。失败并保留远端收尾槽使用 failure_transaction，在 BEGIN IMMEDIATE 内先核对身份，再原子保存 FAILED 历史和 STOPPING 意图。
- 种子旧 abandon 吞异常、delete 不校验 HTTP、status 将无效 JSON 结构解释为“不存在”，都可能伪造停止。现行实现校验请求与查回，未知归属/实例变化/推送结果未知均保槽；SHARED 只解除本项目关联。029 保存 hash/实例/attempt tag/归属/推送意图，不存远端密码或 EH passkey。
- 推送中取消不能先让 HTTP add 继续无人管理：受管 add 等待响应并保存资源事实后再传播取消；响应丢失时保留 INTENDED，暂时查不到不证明停止。失败尝试先保存 FAILED，再同事务建立 cancel operation 和 STOPPING，避免先释放槽。
- 本条记录时 P3 尚缺持久运行移除、用户列表即时移除与独立资源清理、移除后重加身份/槽协调、完整断点恢复/迁移和六项原始复现核验；运行移除后续进展见本文末尾。P8 继续可恢复文件发布与精确 artifact 依赖，P9 继续 worker_session/心跳预算；不能冒充已完成。

## 2026-10-14 P3 运行移除、重加预留与暂存清理

- 旧 active-delete guard 拒绝运行移除；原 queue_job_delete 释放旧 task 槽。采用独立 queue_removals 预留：业务行即时消失，旧任务记录显示 REMOVED，但真实停止/清理继续持久化，匹配的新候选可排队而不可领取。画廊匹配不含 token，纯磁链 hash 不分大小写；P5 后再统一到其 unique_key，当前不新增另一套摄取机制。
- 远端停止按 task_id 筛选资源，不能仅按可复用整数 job_id 查；旧 details/hash 也不能因缺新资源行就放弃停止。失败历史和停止意图分开保存。
- 文件清单必须在删除业务行的同一事务捕获；目录必须在实际输出之前登记，否则取消线程晚产出的孤立 CBZ 无从清理。默认保留已登记产物，旧尝试未发布残片仍清理；复用整数候选 ID 后不能清掉新候选的 pin。路径漂移/其他 artifact 引用/链接均拒绝并保留失败信息。
- 去重不需要预先 cancel：直接共享持久 purge 才不会人为制造停止操作与文件完成审计的竞争。Web 提示需只针对 remove 的 dict 结果，repack 返回整数。
- 累计预览预算在单张图片完成时即判定，超限会取消并 drain sibling；相关 53 条通过。当前不是逐网络块流式预算，不扩张性能结论。
- 最终核心 12 文件 204/0；Web/单元 5 文件 134/0、API 后验 3/0；2174 仅 collected，P3 新合同累计 33。正式证据与首次失败见 progress 最末。P3 尚待切源目标预检、refile/7-Zip 全路径文件提交、完整恢复和六项原触发验收；P4–P12 范围不变。

## 2026-10-14 P3 / R90 完成核验

- 目标预检先查回执，在写锁外执行真实只读 EH/Telegraph 检查，受理再核对目标引用与任务版本；失败不撤销 A。
- refile 真入口为 `_relocate_sync`，不是旧接手说明里的 `_refile_sync`；文件移动与 artifact/pin 同短事务，普通 SQL 失败在释放写锁前补偿。发布崩溃恢复仍接 P8。
- 7-Zip 普通执行与成员头读取均受管；Windows 暂停创建后加入 Job，再恢复主线程；停止等待整个 Job 清空。真实 26.02 工具的 27 条合同已运行，无 skip。
- 原“共享种子也可附着”的行为违反已批准互斥方案，已改为新共享拒绝；历史关联只解除。加种成功不等于归属已确认，轮询在原实例/本 attempt 标签核实后才能写进度或产物；归属或实例变化保槽，禁止 B 接替。
- 切源、refile、进程树、provider 晚返回、推送至 park 断点、029 冲突回滚及六项原触发全部有正式回归。最终 29 文件 625/0 与最后三文件 51/0 按节点覆盖为 628 个不同用例最终通过；2213 collect-only、rc6/029；首次失败与修正记录见 R90 audit/progress。P4–P12 仍待实施。

## 2026-10-14 P4 存储与旧配置导入切片

- 现有 SecretStore 名称仅允许小写字母/数字/下划线，write_private_text 已用独立临时文件原子替换。无需重造文件权限/原子写模块；新版本以 account UUID、credential_revision、独立随机后缀命名，否则同版本并发写文件会在数据库 CAS 之前覆盖赢家。
- 030 的 external_accounts 只保存公开身份、配置/凭据版本及引用；external_account_credentials 保留精确版本引用。enabled 平台身份唯一且不能为空；配置值只在私有文件。新增账号默认停用/未验证；存储层不允许直接修改 active 账号凭据，等待后续协调器确认旧执行停止，纯别名变化不改凭据版本。
- AccountRegistry.reveal 只允许 Bot Token、EH 三字段、TG API ID/Hash/手机号，不返回 session；公共快照默认固定掩码，AccountContext repr 不含秘密。未提交字段保持原值，空字符串/掩码/不明字段拒绝，明确 clear_fields 才清空。
- 旧 Bot/Cookie/TG API/session 可重复/并发复制至一份独立账号；legacy_slot 保留导入标记，删除后不会因旧私有文件还在而重新导入。原文件先保留，待运行适配切换后处理；旧手机号原实现未持久化，不能凭空补出。import_legacy 尚未接 startup。
- 18 条正式合同覆盖上述文件/DB/CAS/导入与 29→30 冲突回滚；相关四文件完整 56/0，2231 仅 collected。未接 runtime、停止、账号生命周期 HTTP/WS、掩码编辑界面，不将存储切片当作 P4 已完成或产品已支持多账号。

## 2026-10-14 P4 持久停止与账号请求边界

- 仅 remote_identity 不能证明新凭据已经验证；030 增 verified_credential_revision，凭据修改后保留来源身份但启用必须重新验证。stop_operation_id 期间编辑、启用、迟到验证和新借用均拒绝。
- AccountLifecycle 在短事务内受理停止、停用新选择、P3 队列撤销、停止依赖与管理员回执；凭据待修改值只写独立私有文件，数据库/回执不含明文。未知停止保持 PENDING，最终成功才绑定新文件/软删除；重复命令先读回执，业务行删除后仍可重放。
- 按 attempt/account/credential_revision 绑定，停止查询不能仅 inner join download_jobs：已移除业务行的 queue_operations、已有 switch/retry 意图与已完成受管做种仍需等待。复用 QueueControl._apply 和 DownloadService.process_operations，不另造物理停止逻辑。
- AccountRuntime.use 覆盖完整请求生命周期，按账号/版本跟踪 lease，停止 await lease 退出；HTTP 替身和真实阻塞线程证明等待实际清理。重复处理不能再次 cancel 正在 finally 的任务。非队列元数据/摄取也通过 AccountExecution 与 Database.connection 短事务检查拒绝迟到写入；该 ContextVar 随 to_thread 传播。
- AccountService 已成为生产 wiring；旧 ConnectionManager 仅由账号 listener 复用摄取逻辑，旧配置/断开入口无账号身份或版本时拒绝。已接 startup/provider/API/UI，但 P4 仍需阶段最终回归和提交；不包含真实供应商或正式部署验收。

## 2026-10-14 P5 统一身份与处理权调查

- P4 实际已验收并本地提交 15dd3bb；上一节末行的“仍需最终回归”是历史调查状态，以 R91 audit/progress 为当前事实。
- source_messages 当前唯一 (account_id, chat_id, message_id) 可继续保存完整观察，telegram_accounts 的新增可空 external_account_id 将新账号 UUID 映射到旧整数外键，无需另建观察实体表；历史固定 Bot 槽无法证明真实接收账号，不能回填为某个 Bot UUID。
- 031 将引用增量写在 source_messages，extra_json 只记录固定键版本，避免并发读旧 JSON 整体覆盖导致丢来源/无界增长。登记与领取必须同短写事务，AI/HTTP 不占写锁，候选创建与 finish_claim 守卫必须同事务。
- 真实 Bot/Telethon 完整转发坐标可以统一；普通群、私聊、隐藏发件人与缺原帖 id 都不足以跨账号认定相同消息。传输 parser 单元合同核对了这些边界。
- 主摄取仍未接存储核心，历史回填/唯一 gid 与来源编辑/移除代次未完成；下一动作在实施计划快速接手表。031 可修改直到 P5 阶段验收，不改冻结 027–030。

## 2026-10-15 P5 身份升级、来源停止与手动原子发布

- 上节末行是历史调查快照；主摄取/回填/代次已接，当前实现与验收状态以 PHASES/处理计划/progress 最新条目为准。
- `set_candidate_eh_ref` 的旧直接 UPDATE 会留下旧统一键；`publish_candidate` 通过回复/媒体关系补 gid 也有同类旁路。现共用 references 的短事务意图与 P3 停止循环，停止确认后再关联目标键/合并历史，旧 gallery 键不作为新 gallery 的别名。目标处理中 owner/revision 在最终关联时撤销，迟到结果不得发布。
- `_has_persistent_work` 包含任何审批/审核/历史下载，不能当作“仍在执行”的判断。已停止候选复用 merging 合并历史与人工/锁定字段，活动来源则先以 changes 保存最新编辑和 task 集合、撤销 observation revision，等真实 finally/远端停止完成。只有补全未知作品身份才停止全部源任务，普通编辑只停止明确选中该观察的 TG 任务。
- 旧 switch/retry 意图可能已在 STOPPING，身份变更必须等它完成物理停止但抑制其旧接替，不创建第二个来源。持久来源变更保留任务集合，进程重启可从同一 P3 loop 继续；停止期间再次编辑保存最新输入，不执行中间版本。
- 手动添加先建候选再分别写元数据会暴露半件作品。`insert_manual_candidate` 允许 EH publisher 在同一 BEGIN IMMEDIATE 内完成候选、claim、元数据、种子、封面；公开写接口继续调用相同 helper。续租与发布使用同一局部锁，避免 LINKED 提交成功后并发 renew 误判 stale。故障注入与并发读取合同已转正式测试。

## 2026-10-15 P5 操作版本与结果、目标键审计

- observation 的 input revision 不能替代操作 revision。失败处理先回滚再开连接写 FAILED，两事务之间新编辑可提交；失败写回必须带捕获的操作版本，不能只凭 source_message_id/status。实际注入该交错，确认新输入保持 PENDING 并最终应用。
- 复用现有两张变更表增加 revision/result/original candidate；不另造 scheduler。`candidates.operations` 提供白名单查询和同事务 CAS retry/receipt；同管理员 Cookie/Bearer 重放先读回执，旧版本和身份失效拒绝。
- `changes` 为无 gid 作品补 gid 时，和 `references` 一样需要在受理事务预留 target_key 并撤销目标 PROCESSING owner；否则旧源尚未停止，目标可独立发布。claim_entry 同时检查两种持久意图，最终关联将目标已有合格观察一并挂载。新增测试先复现只挂一条观察，再以实际 SQL 修复。
- 完成/失败都需要通知。合并通知覆盖原/目标作品，保留 original_candidate_id 支持原详情跳转与操作查询；页面和共享 API 用相同状态投影，不能只显示「已受理」。仍需核实交叉 reference/observation 命令和提交后 callback 丢失时的后续恢复，P5 未验收。

## 2026-10-15 P5 后续意图、自动审批事务及真实来源字段

- `wiring._on_candidates_ingested` 原本仅靠即时 callback；自动审批 interval=0 时丢 callback 会让入库候选长期没有后续。031 用 LINKED 发布 trigger 同事务登记 followup revision；现有审批 worker 启动与每轮恢复、持久退避，callback/event 只降低延迟，不另造调度器。同键追加观察不重复登记。
- 原自动审批分开提交 approve、enqueue、AUTO_* 审计，任一步故障都可能留下半件结果。现在预检在事务外，最终 BEGIN IMMEDIATE 内重算规则与元数据、核对摄取身份，审批/任务/规则证据一起提交。真实 job/普通审计/规则审计故障注入确认全部回滚，提交后 ack 丢失也不重复执行。
- 真实入队保存的是 `details_json.observation_id`；changes/operations/031 guard 和旧测试却使用 `source_observation_id`。把原停止合同改成实际 enqueue 接口后 4 条中 3 条复现未停止，统一真实字段后通过；不能用手工构造的错误字段证明生产路径安全。方案的逻辑 source_observation_id 在当前兼容投影对应 observation_id。
- 仅比较 gid/规则/字段不足以防候选整数 ID 复用。真实 purge 后按同 gid 重加，旧自动驳回会误作用到新候选；匹配结果现包含 ingest entry id/generation/revision，写锁内重新对比，复现转为正式回归。
- 两种 reference/observation 先后顺序、已知/未知初始 gid、来源改画廊/停用及运行 purge 共 12 条通过；媒体组/回复补全与已知冲突隔离另 4 条通过。真实下载 finally 退出前保留停止/移除句柄。运行 purge 的业务 job/candidate 立即删除，必须检查独立 queue_operations/queue_removals，而非误要求业务行仍为 STOPPING。
- 本轮最终证据与失败记录见 progress；P5 尚待迁移/旧入口逐项核验，不能用这些合同替代 P6 来源管理和监听接替，或 P8/P9/P10 的完整发布/恢复/API 验收。
- 补充旧 030 库的多 alias 场景：候选先有无 gid 的频道观察，后有带 gid 的关联观察，031 回填同时保留消息键与 gallery 键，各自登记意图。真实 worker 只作一次决策并确认两个 alias；再次 initialize/重启不重复决定，已纳入 followups 第 8 条合同。

## 2026-10-15 P5 固定输入与 P6 来源管理接续

- 历史 APPROVED 无任务不能区分旧入队失败和管理员主动移除。真实旧 030 库合同验证：重复 initialize 保留审批、不自动重下，显式 enqueue 可创建任务且不重复审批/审计；已关闭这项迁移调查，不再反复猜测历史意图。
- `load_message` 把 chat_title 重建为 chat_id，首次 AI 输入已经丢失来源名称。把既有恢复合同扩展到比较 `(message_text, chat_title)` 后直接复现；未冻结 031 增加消息的接收时 chat_title，新增/编辑保存，重载读取固定值。历史记录只从已有来源别名回填、缺失则用 Chat ID，不猜账号。另测仅修改频道名后的重新判定/重放与历史回填幂等。
- `_list_telegram_sources_sync` 仍只筛 legacy Bot 槽，账号专属来源虽已保存却在 API/UI 隐身。现列出所有未 dismiss 的来源并返回账号 UUID/别名/类型，账号快照仅提供选择所需公开字段。新建、预填、批量添加与逐行更新携带账号，避免同 Chat ID 写回历史槽。
- 会话读取的共享服务现支持显式 TG 用户 UUID；多可用账号的旧省略调用拒绝歧义，不取第一项；新增 JSON/WS 命令与 Web picker 共用该服务。正式合同覆盖 HTTP/Cookie、Bearer、WS、账号缓存隔离、只读与后续添加归属、无效/停用账号、CSRF。
- 来源写入仍为既有 upsert/bulk 接口，尚未全域接 revision/CAS/幂等；不得把此次账号选择切片当作已完成并发冲突防护。下一切片先收口这些共享写入口，再处理 legacy_slot 旧订阅复制（历史观察账号仍未知）、多监听公平/限流与停止后接替。P5 最终完成审计与 P6–P12 均不据局部通过自动验收。

## 2026-10-15 P6 来源写入并发与 Web 草稿

- 来源是独立配置记录，不使用全局配置版本。032 为 telegram_sources 增 revision 和只监听配置字段的 trigger，兼容内部旧写者；last_message_id 等运行游标不触发。027–030 不改，031 仍负责摄取身份，032 尚未冻结。
- SourceCommands 的 BEGIN IMMEDIATE 事务先查同管理员回执，再校验来源版本、写配置和整批结果。旧创建版本 0 不得复活墓碑；明确读到墓碑版本后才恢复。批量逐项业务冲突保留其他成功项，但回执插入等系统故障整笔回滚。HTTP/API、原 Web 表单、WS 桥均调用同一核心，没有路由直接 upsert/bulk 的旁路。
- 新增 source_versions 供会话 picker 记录看到的版本；创建 0、编辑正版本及幂等键均为必需。批量添加保留已有配置并返回 SKIPPED，不覆盖规则；API 可单独查询断线命令回执。重放只保证 command 恒定，旁边的当前来源快照可以更新。
- 原 realtime.js 使用全部 hidden 字段拼表单身份，新增版本和请求键后每次刷新都匹配不到旧草稿。来源改用稳定表单身份；后台刷新遇来源草稿时保留页面并提示变化，其他表单提交时也保留已被远端删除的来源草稿。真实 HTMX/Alpine DOM root/proxy 验证输入节点、版本和请求键不被替换。
- 来源冲突页面保存完整草稿和当前配置。只有管理员点击明确确认按钮才采用冲突快照版本；后台不更新旧版本。原始 rules_json 作为冲突信息，历史规则 JSON 已损坏时仍可按读取版本修复，不先 json.loads 阻断管理。
- 正式测试为 source_commands（18 条）、既有来源/设置/迁移回归与扩展 DOM 脚本；结果见 progress，不能据此宣布 P5/P6 或全域 API 验收。

## 2026-10-15 P6 旧共享订阅的一次性迁移

- 旧共享来源放在 bot-api://configured 的 telegram_sources，旧 MTProto 的 set_source_cursor 写这里；这不能证明历史 source_messages 的真实接收账号。033 只复制配置给 external_accounts.legacy_slot 明确为 TG_BOT/TG_USER 的原导入账号，原消息行/接收账号/来源引用保持不变。
- AccountRegistry.import_legacy 在配置导入后、runtime 启动前，以同一短事务复制各账号规则/墓碑、写一次性完成标记并退休原共享配置。专属来源已存在时不覆盖，后来新增账号不继承；旧 TG 用户保留其游标，Bot 不借用该轮询游标。旧共享配置保留原值，支持缺失的另一个原账号稍后导入，完成过的账号不再复制。
- 只做 INSERT OR IGNORE 而不记完成标记，会在管理员删行或改规则后反复导入；只把原规则清空则后到原账号丢配置。现保留历史原值/退休位与每账号完成标记，管理 API 拒绝陈旧共享写入。退休变更推进原配置 revision，旧批量请求不能悄悄更新不可见配置。
- 七个正式合同验证两类导入、用户游标、未知消息身份不变、重启/编辑/删除、已有专属配置、后到旧账号、并发导入和中途失败整体回滚；实际 AccountService.start 合同也验证迁移规则在账号启动后可读且重启保持。仅临时库/受控供应商，结果见 progress。

## 2026-10-15 P6 多监听下一切片的源码边界（仅调查，未实现）

- 每个 AccountListener 已拥有自己的任务、实体缓存和 runtime lease；不能为“公平”重新造一组重复监听器。Bot `_poll_telegram` 先持久化 raw updates，但随后 await 全部业务处理才拉下一批；`process_pending_updates` 的 while 会把当前账号待处理事件全部排空，慢 AI 或历史积压会阻塞该账号的继续接收与启动。
- MTProto `_ingest_source` 每个来源先跑 process_pending_entries，再顺序 process_message（包含 AI），最后推进最高消息游标。当前顺序不因普通失败提前跨过未持久消息，但没有将接收持久化与昂贵判定分开；单个慢来源会拖延同账号其他来源。下一步须先定义有界每轮预算/轮转和可恢复接收边界，再接处理，不能用简单取消 AI 来伪装公平。
- TelegramUserClient 已将 FloodWaitError.seconds 转为 ProviderConnectionError.retry_after；Bot 轮询会使用这个值，用户来源循环目前只记录错误然后继续其他来源并固定十秒重试，尚未实现账号独立的 FloodWait 冷却。P9 接完整持久冷却/预算，但 P6 不能在同账号已限流时继续发一轮其他来源请求。

## 2026-10-15 P6 可靠监听切片（覆盖上面的未实现快照）

- 两个原触发通过正式 test_listener_delivery 复现：AI 已进入后第二来源不再被访问；限流后仍访问第二来源。首次夹具用了不符合 TelegramUserCredentials 格式的短 api_hash，造成请求根本未开始；修正为合法测试格式后才取得上述有效失败证据，不能把首轮失败当产品复现。
- 沿用 AccountListener 的接收任务与 runtime，另设每账号受管消费任务。Bot 原始事件持久化后即可拉下一批/推进 offset；MTProto 使用 034 与 TelegramUserInbox，在单个 BEGIN IMMEDIATE 中保存归一化输入和来源游标，再交原 CandidateIngestor.process_message 处理。没有第二套去重键/处理所有权；ACK 失败重放已发布输入不重复 AI。考虑过接收直接领取并转交 claim，会将接收进度绑住处理租约，现用独立持久投递隔离两者。
- 用户每轮最多十个来源、每来源百条并轮转起点；Bot/MTProto 待处理事件和到期摄取输入按频道交错、保留同频道顺序，各自最多百条。未知旧事件改为持久后台恢复循环，不在 start 同步排空，也不会因限制每轮而留下无人处理的剩余事件。
- FloodWait/retry_after 中断该用户的剩余来源，检查 dialogs 也生效；保存来源不清等待。网络故障也进行账号退避，会话失效/应用凭据无效则停止后续请求并保留配置。当前限流保存在运行时；P9 仍需持久冷却、全局/账号及跨监听/下载/会话读取预算，不能把本切片当完整预算交付。
- 消费持有独立账号 lease，取消后沿用 ingestor 的实际 AI 清理等待；Bot raw 写入及首次游标写入改用已有 run_sync，取消时等待真实写线程退出。正式测试控制 AI finally 与真实线程的 release，确认 stop 尚未结束、lease 未先释放；关闭后输入可恢复。候选消费失败仍通过已有连接快照/账号运行状态报告，网络接收继续。
- 最终 test_listener_delivery 十八个合同；十一文件 154/0、后续八文件 102/0、最终 listener/manager/database 三文件 59/0，分组重叠/时点不同不叠加。2506 collect-only；031–034 未冻结，P5/P6 未验收。下一步 P5 §8.G 最终/旧写入口审计及 TG 来源账号优先和实际停止后串行接替；不要重做本次已验证的接收分离。

## 2026-10-15 P5 要求核对与固定输入恢复

- 要求/入口映射见 R92_REQUIREMENT_AUDIT.md（进行中，非验收结论）。当前 Python 候选插入仅 publish_candidate 与 insert_manual_candidate；观察插入集中 register_observation，画廊更新集中 persistence/merging/references/migration。兼容 save_candidate_message 转同一核心，app 内没有产品调用者；不能仅凭搜索把这些函数的每个调用约束判定已验收。
- 原固定输入失败后，订阅停用或允许格式收紧，另一个合格观察补入仍会拿旧输入跑 AI，之后把整键忽略。正式 pipeline 复现并修复：reselect_pending_input 只换待处理且原输入已不合格的记录，原输入仍合格则稳定；登记和后台恢复共用，备用观察已存也可跨重启恢复，不需要新事件。
- AI 前先检查本地来源，发布事务再次重查。中途来源变更撤回 claim、保持可恢复 PENDING 并择合格输入，不把其他观察判忽略；不替换 PROCESSING owner。五条正式合同覆盖原订阅停用/格式变化/原输入不变、失败后已有备用恢复、AI 中途变化恢复。最新十文件 133/0，最后四文件 70/0，2511 collect-only，组间重叠不相加；具体失败/修正和时点见 progress。
- 未完成审查：账号本身失效但订阅仍启用、过期 claim 的完整恢复；P6 旧请求真正结束后的合格备用账号/新 attempt；P8 新手动发布为 APPROVED 后 enqueue 前崩溃的持久流水线意图。后一项当前自动审批 followup 不覆盖，必须和历史 APPROVED/no-job 不猜意图区分。P5/P6 未验收，031–034 仍未冻结。

## 2026-10-15 P5 账号状态、过期处理与扫描窗口（覆盖上述账号/claim 待核对项）

- 真实 AccountRuntime 合同复现：A 首次失败，B 观察已保存；A 账号正在停止、停用、删除或完成换凭据而尚未验证，订阅仍启用时，B 的恢复均返回 0。另复现 A 仍合格时 B 新观察竟领取 A 的固定输入并在 B lease 内跑 AI。source_still_eligible 已在事务内核对注册账号状态，prepare_observation 限定固定输入的领取账号；原输入合格则只补 B 观察，不自动换输入。
- 过期遗留 claim 在新/已保存 B 观察下都无法接替；取消清理完成但 claim 已过期时也无法恢复。reselect_pending_input 现先撤销遗留 owner/revision，再用原本的输入选择和有界 claim，不发明第二条处理链。旧结果仍由 revision 拒绝。
- 仅加过期恢复会使仍在运行或清理的请求与新处理重叠，手动验证也必须纳入。Database 持有同一 IngestProcessors，原 ingestor 的 task 表/单次取消等待移入此处；TG 及手动短锁覆盖 DB 领取至本地任务登记的间隙。后台恢复和新投递均跳过活任务；不同 CandidateIngestor 实例仍共享该记录。锁不覆盖网络请求，只覆盖领取/登记和有界恢复读取。
- 首个“百条挡路”用例只有一个 A 频道，原频道交错能让 B 自有输入提前，所以 1/0，不能算复现；改成 100 个有效 A 频道、每件均有 B 观察后，连续三轮都恢复不到 B 第 101 件，取得 1/1 的有效失败。现按原频道交错顺序生成扫描位置，沿账号上次位置轮转；游标在数据库运行实例共享，替换消费者不重置，进程重启仍从持久输入重新扫描。每轮仍限 100，不为“公平”无限排空。
- 正式新增 account_recovery 十一条、manual 三条；pipeline 夹具改成真实已验证启用账号，不把产品账号约束放宽为未启用也可用。十一文件 146/0，十三扩展文件 180/0，最后手动/账号恢复两文件 21/0（含重复取消且真实 cleanup 等待），2525 collect-only；时点/失败明细见 progress。P5/P6 仍未验收，P6 下载 attempt 接替、P9 worker_session/持久预算和 P7–P12 不缩减。

## 2026-10-15 P6 下载接替的下一入口（仅源码核对）

- AccountService.resolve_telegram_user_source：来源本身为 TG_USER 时直接 use_account，账号不可用就抛错；其他账号循环只覆盖 Bot 来源转用户读取，且只检查文件名/大小与非空 MTProto 标识。后续需核对来源原账号优先、同一附件身份以及可证明的备用观察/共享消息，不能把相同画廊或名称/大小单独视为字节相同。
- DownloadService._handle_job：TG 任务严格使用 details.account_id；绑定账号不可用时直接 _mark_job_failed_sync，没有串行换账号的新 attempt。_process_claimed_job 只结算/记录结果，目前未见 TG 自动备用接替入口。接续应先加真实失败/清理阻塞的下载合同，再沿 queue task/attempt/作品槽接入持久、有界接替；不在 provider_scope 内直接循环多个账号并冒充一个 attempt。
- 上两项只是当前源码调查，不是新产品实现/已复现下载缺陷；上一节通过的是摄取恢复及本地排他，不能代替这些下载合同。P6 仍未验收。

## 2026-10-15 P6 精确附件与持久下载接替（覆盖上节源码快照）

- 官方依据：TDLib [FileManager.cpp](https://github.com/tdlib/td/blob/master/td/telegram/files/FileManager.cpp) 的 FileNode::get_unique_id 使用 base64url(zero_encode(serialize(location.as_unique())))；[FileLocation.hpp](https://github.com/tdlib/td/blob/master/td/telegram/files/FileLocation.hpp) 的 AsUnique::store 写入 int32 文件类别加一及 AsKey 的 int64 document id；[FileType.h](https://github.com/tdlib/td/blob/master/td/telegram/files/FileType.h) 定义 Photo=0、Document=1，所以文档标记为 2。app/connections/telegram_files.py 严格解析这 12 字节与现有 mtproto:<id>；不从文件名/大小、file_id/access_hash 猜身份。未知类别/照片/畸形/越界标识拒绝跨账号匹配，正式单元合同 19 条。
- 用户来源仍合格时保持原账号；不可用时先取同作品同文档的有效 TG_USER 已保存观察，再验证共享频道原坐标。私聊/普通群必须使用目标账号自己的观察坐标。selected download_* 字段独立于 origin_account_id 与原 observation；两者在短提交事务和传输提交均复核。Bot 备用只能用自身 observation/file_id，getFile 返回的身份也要匹配，不把 A 的 file_id 交给 B。
- 原始两条有效失败已修复；新增来源在预检/传输中被编辑、移除、换附件的六个交错合同。下载失败曾直接 FAILED 释放槽；三条正式合同复现。现借现有 queue_operations 保存 replace-account，冻结首次失败时的有限合格账号池，保存已失败账号集合；旧执行自然退出后才预检/恢复 PENDING，同一 task 下一次领取产生独立 attempt 和 staging。035 守卫只认可当前 attempt/generation 对应且已完成的内部操作，显式重试保留原来的新任务语义。
- 取消等待接替复用原操作并更新版本；finish_operation 读取数据库最新动作，预检期间取消不会启动后继。失败 attempt 的原始错误冻结，提交回滚仍保持 STOPPING/槽/待处理意图；重启可恢复。直接旧 generation 晚完成被拒绝。
- 十二相关文件 197/0，最终 failover 扩为 11/0，2569 collect-only。具体首次错误/失败/修复计数见 progress，不叠加重叠组。剩余：运行中账号停用/删除/凭据修改目前仍由生命周期发 cancel，需接入合格备用；显式 retry 需要重新选择原账号并重置本轮池，不能沿用旧 attempted_accounts；P5 最终审计及 P7–P12 继续。以上没有完成 P6 阶段验收。

## 2026-10-15 P5 审计与 P6 生命周期收束（覆盖上节待修项）

- 失败任务 retry/restart 的细节必须与状态恢复放在同一 UPDATE；单独 UPDATE details 会先触发 adapter 改写旧终态 task。两条恢复断言已复现修复，最终检查整个旧任务快照不变。
- 生命周期停止按真实 TG 观察创建持久接替，管理员 cancel 优先；原观察在备用预检前校验。不能用缩窄所有 provider 状态来保护 pending B，应只排除已结束 A 的旧绑定；非 TG queued/paused 合同保留。
- worker 关闭与操作取消可以对同一 producer 重复 cancel，实际打断 finally 并提前释放槽，已直接复现修复。账号/queue coordinator 的 shield 不能替代 worker 自己等待真实清理。
- 来源停止匹配需要原始 observation_id 和实际备用 download_observation_id；原实现只挡备用晚提交而不停止流。真实编辑/移除及失败操作重试三条复现已修复，原观察不连带修改。
- R92_REQUIREMENT_AUDIT 已补实际旧写入口及 030 升级/快照回退边界；最新节点 445 个不同用例通过，2593 collect-only，详见 progress。P5/P6/P7 尚未验收，031–036 未冻结。
- P7 036 的 persistent sequence 轮询已有五条合同；selected_account 查询不推进，use_kind 无嵌套账号时才选择。当前 ExHentaiService.account_request 在公共 gdata 前已借账号，尚不满足“匿名一次成功不消耗池”的完整方案，须在认证 fallback/调用者发布上下文中一起改，不能只给同一匿名请求换账号标签。
- request_archive_url 含 POST，通用 EXHENTAI_UNREACHABLE 无法判定申请是否被接受。下一步需持久操作意图和只读结果核实，未知返回 EH_ARCHIVE_RESULT_UNKNOWN；不能机械跨账号重复 POST。P7 尚无元数据持久有限尝试、能力/冷却管理或共享预算。
- P10 另需核对 AccountService.command 对敏感 params 的裸 SHA256，按已批准方案改成服务端私有密钥 HMAC；此处记录真实源码偏差，不在本轮宣称解决。

## 2026-10-15 P7 匿名请求与认证元数据池（覆盖上节元数据待实现项）

- 原 account_request 在公共 gdata 前借用 EH，空 enrichment 也推进轮询；批量未返回画廊后又调用 _fetch_metadata，逐条重复同一个匿名请求。新正式七例先复现六个失败；现以任务内惰性 AsyncExitStack 保留认证成功后的租约，公共请求不借账号，批量未命中直接 HTML。显式空 Cookie 加拒绝重定向，避免共享 HTTP 客户端 Cookie 自动附回匿名请求。
- MetadataAccountPool 只负责只读认证元数据，036 保存候选账号/凭据版本、操作和各次结果；共享 selector 支持 captured candidate_versions，不纳入途中新增或换凭据的账号。正常起点公平，剩余顺序固定，不被其他操作推进全局轮询改变。失败 scope 退出后才试下一账号，成功 scope 用同步 push_async_exit 转交外层写入 scope；手动发布继续受该凭据版本屏障保护。
- AccountService.use_account/use_kind 为每个 EH 账号增加本进程锁，锁涵盖 runtime lease 的实际退出，TG 不使用此锁；等待期间仍需在借用时重查账号状态。不是持久预算，也没有替代 P9 worker_session。直接 verification 的请求预算仍须随统一预算核对。
- 操作注册写线程被取消时，先排空线程，再原子记录取消；只对诊断表使用独立关闭连接，不绕过生产元数据/任务提交屏障。进程被杀产生的 RUNNING/STARTED 仍待 worker-session reconcile，不把正常取消合同冒充崩溃恢复。
- 所有账号实际失败返回 EH_ALL_ACCOUNTS_FAILED；全停用/冷却/版本变化等不可借用返回 EH_NO_ACCOUNT_AVAILABLE，持久记录区分 FAILED/UNAVAILABLE。仅保存非敏感别名、账号/凭据版本、阶段和错误码，不存链接 token、Cookie 或供应商异常原文。
- 仍须完成：HTML HTTP 200 错误页/授权/限流 fixture、Retry-After 与站点共享退避、凭据/能力分级健康及冷却。当前 HTTP 429 仍按通用 HTML HTTP 错误轮换，公共单条请求限流仍可进入认证回退，不能视为完整限流实现。批量 gdata 网络错误仍保留原有 best-effort 返回 0 行为；下一步需与共享退避一起核对。
- 队列已绑定 EH 时，元数据仍沿用该账号，不在同一个 queue attempt 内私下轮换；下载接替必须由队列创建独立 attempt/staging。收费 POST 的持久意图/只读查回、未知费用、种子认证与跨进程预算按原 P7/P9 范围继续。
- 本轮 P6 95/0、最终监听/来源 81/0，EH 等十七文件 215/0、后续六文件 82/0、最终租约两文件 21/0；最新节点 309 个通过、2610 collect-only，非整库。P5/P6/P7 未阶段验收，031–036 未冻结，详见 progress 最新记录。

## 2026-10-15 P7 官方归档协议与收费意图（未验收）

- 上轮只读研究确认 JHenTai 的 `lib/src/network/eh_request.dart:843` 及 EhViewer-NekoInverter 的 `EhEngine.kt:460` 使用 `dltype=org`、`dlcheck=Download Original Archive`；两者解析 `#continue > a`，JHenTai 再取下载页 `#db > p > a`。对应解析文件为 JHenTai `eh_spider_parser.dart`（blob a849c8cc73d74b910d9e6ed17997ccd8d80022f7）及 EhViewer `ArchiveParser.kt`（blob 34c5301191640be8051e2def494d78971feeb602）。这些公开客户端证明表单/DOM，不证明重复 POST 不重扣，也不证明只读 GET 总能查回。
- 当前工作区 `archive.py` 在正常执行屏障内预存收费意图，一个 gid 仅允许一个 REQUESTED/UNKNOWN；仅初次 POST，异常/取消后只读核实，不能确认保留 UNKNOWN。记录固定账号/凭据版本；签名链接和画廊 token 保存在私有 SecretStore，不进入诊断表。外部下载节点显式不附 EH Cookie。
- 已接实际 ExHentaiService 与 wiring，12 条新合同包含真队列领取/归档发布、并发同 gid、资金不足分级健康、POST 不明 503、取消及实际 finally 退出、取消撤销发布屏障后仍保存费用未知。下述 173/0 证据是受控 HTTP，不代表真实供应商联调。
- 尚无 EH 下载有限账号池/串行独立 queue attempt、认证种子路径、费用未知人工核实 API 与崩溃 reconcile。P7 未完成；P5/P6 独立提交前不继续堆积 P7。032–036 与未验收 031 均未冻结，已提交基线仍 rc7/030。


## 2026-10-15 P5 独立交付边界

P4 MTProto 原来先 admission 后 save，提取 P5 时必须改走 process_message，确保付费判定前先取得持久身份。旧共享白名单在 P6 迁移前只对同类型唯一启用账号保留兼容，新观察保留接收账号，历史不重标。rule_source_id 固定订阅引用并在发布前重查；低层旧输入为空时仍按其账号/Chat 查回。来源变更还须压过已受理的 retry/switch，否则停止后会错误启动旧后继。


P5 本地提交 2ea49f2，只含 031；共 447 个不同用例最新通过。新增 205 条旧收件箱后台恢复合同；AccountService 的原有单次启动处理不足以覆盖全部旧数据，现采用现有有界恢复循环。P6 接续移除共享规则适配，使用其 033 显式迁移后的账号专属规则。

**P5 下载合同补验与同阶段修正。** 初次本地提交后补跑真实 create_app→worker→Telegram 产物链路，download-first 为 9/1/0/0（5.394 秒），复现 AccountService.use_kind 不接受来源 account_id。为单账号 P5 补显式账号类型/可用性校验与整个 scope 的凭据租约；Bot 来源转用户下载时不把 Bot ID 当用户账号。不引入 P6 备用账号池。download-fixed 为 66/1/0/0（44.915 秒），产品已修，剩余断言误把异常 code 当显示消息；改为检查 exc.code 后，最终五文件 66/0/0/0（44.229 秒）：ingest_legacy_runtime、downloads、account_service、account_runtime、review_actions。与此前 final/cross/backlog-fixed 按节点取最新，共 447 个不同用例通过，0 failure/error/skip；2465 collected，仅收集。同一 P5 本地提交使用 amend 修正，未推送；首次 299d54a 已由最终 2ea49f2 替代。提交快照共 74 文件，精确暂存并逐 blob SHA256 核验；最终 SHA 记录在工作区接手入口。

**清理。** 所有测试会话已终态，结果在本日志留痕；提交哈希核验后删除本轮确切 TEMP 根目录 ehbot-p5-split-20261015-a 及前述四个 P7 archive 报告目录，不重建 .scratch。

### R93 / P6 — 多 TG 监听、来源命令与串行下载账号接替（0.3.0rc9 / 035，2026-10-15）

**范围。** Bot/TG 用户各自启用，独立监听、游标、session、实体缓存和候选消费；共享 P5 统一键，不建第二套队列。032 来源配置版本/同事务幂等回执，JSON/Web/WS 共用核心并保留冲突草稿；033 将旧共享规则一次性复制到已证明的历史导入账号，保留墓碑、用户游标和未知历史观察。034 将 MTProto 消息与游标原子持久化，慢 AI 不阻塞接收，来源/频道有界轮转，FloodWait/失效仅影响该账号。

**传输。** 用户来源优先原账号，Bot 使用自身 Token/file_id；备用必须证明同一个 Telegram document，私聊使用目标账号自己的观察。原始/实际观察版本在入队与发布重查。失败或账号生命周期变更登记持久 replace-account，旧请求及清理真实退出后才恢复同一 task、新 attempt/独立 staging。035 只为当前代次、已完成的内部接替开放 STOPPING→PENDING/FAILED；取消优先，有限账号池跨重启不扩展，显式重试开启新任务并保留旧历史。

**独立提取与失败。** 从已提交 P5 `2ea49f2` 导出命名 TEMP 树，逐文件/方法提取 032–035，排除 036 与 EH health/轮询/metadata/archive。初次十二文件 222/2/0/0（83.135 秒）：两项是 P7 测试预期（EH 多启用、P7 私有凭据 helper）误带入，恢复 P5 单 EH 合同；P6 另加实际 Bot/用户的直接/命令多启用与独立监听合同。静态核对发现最后一个消费错误可掩盖健康账号，改为先读取实际监听状态再汇总；各账号错误仍可见。

**补验过程。** 一次五文件从主混合树 cwd 执行，89/2/0/0（36.449 秒），不作为独立提交证据；新测试没注入 CandidateIngestor 导致 TG 用户无监听，已补真实摄取服务。独立树五文件初验 89/3/0/0（39.168 秒），除上述两项外，旧停止合同在接收完第二来源前就取消却断言已存两条，改为明确等待两来源持久保存后施加停止。最终独立五文件 89/0/0/0（32.992 秒），含 account_service、telegram_account_runtime、listener_delivery、source_account_downloads、telegram_download_failover。完整 HTTP/Web/WS/摄取/队列/下载/账号十一文件 193/0/0/0（207.123 秒）。独立 rc9/035 树 2596 collected（仅收集）；定向组按当前节点取最新 419 个不同用例通过，0 failure/error/skip，非一次整组或整库运行。 原始十二文件中被 P6 阶段边界替换掉的 EH 多启用节点不计入当前集合；重复组不累加。

**UI/静态。** 真实 HTMX/Alpine + jsdom 的来源草稿/远端删除、实时与日志场景在根路径和 /ehbot 前缀均通过。离线 npm 缓存未命中后，仅在指定 TEMP 安装 39 个临时依赖，测试后清理；未改依赖清单。compileall 与 collect 通过，阶段提交前检查 diff --check 和逐 blob SHA256。版本仅是本地源码标识，不代表发布。

**升级/回退与边界。** 027–031 不改写；032–035 随阶段提交冻结。033 拷贝和导入标记同事务，失败回滚；034 升级保留既有游标，035 升级/重启/旧完成屏障见正式回归。沿 R92 离线完整快照备份/恢复步骤，不能删迁移记录或只降源码。仅临时库，未读真实 private/请求供应商/升级真实库。EH 仍单启用，完整 EH 池由 P7 交付；持久冷却/共享预算属 P9，最终全域协议属 P10。

**交接。** 代码、正式测试与同步文档验收后立即本地阶段提交，核实 SHA 再进 P7。P7 现有 036/health/metadata/archive 保留主工作区，未验收；仍需独立归档 attempt、认证种子、未知费用核实/崩溃恢复。P8–P12 原范围保持，总目标 active。无推送、发布、部署。

**清理留痕。** P5 提交 `2ea49f2` 的 74 blobs 已逐 SHA256 核验，暂存为空、031 相同；P5 TEMP 3227 文件及四个 P7 archive 目录 1/99/100/365 文件均已核对绝对路径与无重解析点后删除，剩余 0，.scratch 不存在。本轮 ehbot-p6-split-20261015-a 留至本地提交逐 blob 核验后删除，所有测试进程终态后才清理。

**P6 提交核实。** 本地提交 `c61af7e`（单行标题、无正文）；58 个提交文件与独立 rc9/035 验收快照逐 blob SHA256 相同，暂存为空、027–035 工作区与提交相同。P7 未夹带。所有测试和 Node/npm 进程已退出；结果留痕后清理确切 TEMP 目录。无推送、发布或部署。

### R94 / P7 — EH 有限账号池、能力退避与收费核实（0.3.0rc10 / 036，2026-10-15）

**实现。** 036 的公平序号、只读元数据操作/尝试、版本化能力健康、SITE/GDATA 共享 Retry-After 与独立归档收费意图接入真实 AccountService/wiring。匿名 gdata 不借账号、不带 Cookie、不推进轮询；认证 HTML 固定有限账号/凭据集合，失败请求先退出，同账号操作/连接测试串行，成功租约保留到写入。明确认证失效只标实际版本 AUTH，重验成功恢复；额度不足只冷却 ARCHIVE，不影响元数据/种子；站点限流不逐账号重试放大请求。

**队列与费用。** DownloadAccountPool 只为现有队列选账号，QueueControl 沿 P6 的 replace-account/真实退出屏障推进同任务的新 attempt；归档/认证种子的账号、版本和有限池在供应商请求前保存到当前任务与 attempt。GP/认证明确拒绝才自动接替，各账号自己读取申请页与链接。收费 POST 前保存意图和私有定位符；超时/取消/结果未知只用原账号 GET 核实，不盲目重复 POST 或换 B。原账号版本不可用时保持 EH_ARCHIVE_RESULT_UNKNOWN；原版本恢复可显式重试只读核实。READY 私有定位符随任务重试保留，下载失败后不重复收费。新账号不扩池，取消优先于预检，旧历史不被新结果改写。付款意图跨取消/删除保留，不伪称远端费用被撤销。

**种子。** .torrent 请求使用确切账号 Cookie，登录失败更新 AUTH 并经新 attempt 接替；种子文件在 infohash 验证后才推送 qBittorrent，认证失败阶段没有远端资源。EH Cookie 不发给 ehtracker 等外部地址，显式逐跳处理有限重定向；网络错误不把签名 URL/供应商异常拼进公开消息。磁链不借 EH 账号、不受 EH 站点冷却阻塞。账号池字段不是远端 hash/推送意图，种子页限流不能凭非空 details 伪造 STOPPING/远端停止。预检改走认证只读池，不请求收费归档。

**失败留痕。** 初次四文件 68/1/0/0（7.055 秒），新用例错写 enqueue_eh_torrent_download，核对现有接口改用 enqueue_torrent_download。随后十文件 152/1/0/0（34.943 秒），唯一剩余是 FakeClient.added 保存 multipart 请求却按纯 torrent bytes 断言；改为仅一次推送且 multipart 包含准确 payload。七文件 135/0/0/0（41.086 秒），增加账号停用/真实清理等待、READY 重试无二次 POST。静态核对补限流时无远端资源与磁链无需 EH 的合同。

**最终验证。** 完整二十四个相关文件 362/0/0/0（94.528 秒）；最后调整磁链冷却边界后，download_accounts/rotation/metadata_accounts/archive_intents 四文件 42/0/0/0（16.943 秒）。按 classname/name 取最新 363 个不同相关用例通过，0 failure/error/skip，非一次整组或整库运行；2661 collected，仅收集。涵盖实际 MockTransport 的归档 form→POST→只读查回→landing→ZIP、队列 attempt/产物、认证 .torrent→qBittorrent、有限池/重启/取消/停用以及 API/数据库/旧 provider 屏障。没有真实供应商请求或真实账号测试。P6 既有 DOM 未改，不重复运行；compileall/CodeGraph 与标准 diff --check 通过。

**交付。** rc10/036 仅本地源码，027–035 保持不变，036 随阶段提交冻结。README/USAGE/REALTIME_API、PHASES/计划/task_plan/findings/本日志与 R94 audit 同步；无环境变量/配置字段变化，.env.example 无需改。独立元数据操作崩溃诊断与收费意图已有持久记录/只读恢复，进程会话、快速重启调度、持续心跳和完整预算按 P9；原档→打包流水线/文件发布崩溃恢复接 P8。P10–P12 原范围保持，总目标 active。立即暂存本阶段代码、正式测试和文档并本地提交，核实 SHA 后才进 P8；不推送、发布或部署。

**升级/回退与清理。** 036 未交付前仍有实验性结构变更，最终验证全部用新临时库；旧实验 036 不能重复 initialize 自动补表。正式升级沿 R92 完整离线快照/隔离副本步骤，036 冻结后不改写；回退恢复旧完整库/私有配置/配套源码，不能删版本记录。P6 TEMP 已核对无重解析点后删除 3865 文件，剩余 0，.scratch 不存在。本轮仅 ehbot-p7-download-20261015-a，报告留痕后、提交核验后删除；正式用例在 tests/。

### P8 / R95 产物和发布调查裁决（2026-10-15）

原有“完成后 callback enqueue”无法跨入库/下载提交与回调崩溃恢复，也会把 B 重下的明确 repack 请求提前交给 A。037 保留当前 artifacts 投影，另存不可变 artifact_versions；队列在下载入队捕获设置，并保存明确后续意图，实际原档/完成结果原子提交后由既有 conversion worker 有界 materialize。手动新增同事务保存下载意图，失败保留重试。无需第二调度器或通用 DAG。

单个 rename 不等于数据库/文件原子提交。CBZ 替换保存准备记录、旧副本、新摘要与精确 attempt；启动/退出/停止后先核实 journal，合格新文件与产物/完成结果同事务确认，取消先赢则恢复旧文件。改名/refile 用独立移动 journal，未提交移动逆向恢复旧路径；路径钉、产物与 refile 完成一起提交，且固定 CBZ 输入。普通移动跨挂载点明确失败并保旧；打包在目标卷 staging。未知外部字节不覆盖，保留 RECOVERY_REQUIRED。

实际证据：artifact_pipeline 四个打包真实退出断点，relocation_recovery 的 manual/worker 各 prepare/replace/commit 三断点；另覆盖取消旧线程、未决移动拒绝移除、目标被抢占、已修复文件重试、两个活动输入冲突、API 非法 ID/跨作品误用，以及实际 036 升级。最终 27 个相关文件按节点最新 590 passed、2687 collect-only，非整库；详 R95 audit/progress。P9 会话/心跳/预算与 P10–P12 不在此阶段完成范围。

### P9 / R96 会话与预算裁决（2026-10-15）

租约超时不能证明实际请求停止；单后台 OS 文件锁与 session UUID 确认进程所有权，活动集合保护长请求/取消清理，短 lease 保护领取到派发的窗口。锁在迁移前取得，重复实例整个拒绝启动。恢复使用既有 operations loop，每批 100 条轮转和逐项隔离，不建立第二调度器；未知文件保留 journal，恢复次数有上限。

全局请求额度须在账号等待之后取得，且取得后重查版本/停止/session；真实等待合同复现了旧请求越过停用的竞态并已修正。监听与普通/传输分池，TG FloodWait/429 是账号级持久事实，取消不能丢掉服务端限流。远端下载槽与本地槽分开，未知推送和未确认停止保留额度，完成后做种单独计算。初值保守，未声称性能最优。

最终 454 个不同相关用例通过，2710 collect-only，未运行整库；实际 037 升级、快速重启、长请求、未知文件隔离和 Windows 硬退出有合同。Linux guardian 仅静态检查，实跑是 P12 未完成项。见 R96_COMPLETION_AUDIT 与 progress；无真实供应商/库升级。
