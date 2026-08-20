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

## Technical Decisions
| Decision | Rationale |
|----------|-----------|
| Use `httpx` and `selectolax` for ExHentai | Supports streamed HTTP and low-overhead HTML parsing without Chromium |
| Use standard `zipfile` for ZIP/CBZ | Enables bounded-memory member-by-member copying |
| Use `7zz` as a subprocess for RAR/7Z | More predictable format coverage than Python-only archive packages |
| Store source and job identity keys | Prevents duplicate candidates and duplicate downloads after restarts |
| Keep one download and one conversion active in low-resource mode | Preserves Web UI responsiveness and limits memory peaks |
| Store metadata provenance per field | Allows manual values to override Telegram and Ex-derived values safely |

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
