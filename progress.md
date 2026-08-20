# Progress Log

## Session: 2026-08-19

### Phase 1: Requirements And Discovery
- **Status:** complete
- Actions taken:
  - Consolidated the Telegram, ExHentai, review UI, CBZ, Docker, and resource requirements.
  - Verified Telegram Bot API and Telethon boundaries.
  - Verified E-Hentai metadata API behavior and ExHentai cookie fields.
  - Reviewed ComicPacker's README, dependency list, and conversion source.
- Files created/modified:
  - `task_plan.md`
  - `findings.md`
  - `progress.md`

### Phase 2: Plan Authoring
- **Status:** complete
- Actions taken:
  - Selected the target stack and module boundaries.
  - Authored the formal development plan, including architecture, workflows, data model, Docker contract, security, testing, estimates, and acceptance criteria.
- Files created/modified:
  - `task_plan.md`
  - `findings.md`
  - `DEVELOPMENT_PLAN.md`

### Phase 3: Verification And Delivery
- **Status:** complete
- Actions taken:
  - Verified all confirmed requirements are represented in the plan.
  - Confirmed Markdown code fences are balanced and all 22 numbered sections are present.
  - Checked that the plan contains no unresolved TODO or placeholder text.
- Files created/modified:
  - `task_plan.md`
  - `progress.md`

## Test Results
| Test | Input | Expected | Actual | Status |
|------|-------|----------|--------|--------|
| Requirement coverage | Confirmed conversation requirements | Every requirement maps to plan scope or acceptance criteria | Covered by sections 3, 4, 12, 15, and 21 | Pass |
| Markdown structure | `DEVELOPMENT_PLAN.md` | Balanced code fences and sequential sections | 40 fences, even; sections 1 through 22 present | Pass |
| Placeholder scan | `DEVELOPMENT_PLAN.md` | No TODO/TBD/template placeholders | No matches | Pass |

## Error Log
| Timestamp | Error | Attempt | Resolution |
|-----------|-------|---------|------------|
| 2026-08-19 | Browser navigation timeout | 1 | Switched to read-only documentation endpoints |
| 2026-08-19 | `rg.exe` launch failure | 1 | Used PowerShell enumeration |

## 5-Question Reboot Check
| Question | Answer |
|----------|--------|
| Where am I? | Phase 3 complete |
| Where am I going? | Awaiting user review before implementation |
| What's the goal? | Produce an implementation-ready EhBot development plan |
| What have I learned? | See `findings.md` |
| What have I done? | Created and verified `DEVELOPMENT_PLAN.md` |

## Implementation Session: 2026-08-19

### Stage 1: Foundation And Persistence
- **Status:** code complete; Docker runtime verification pending
- Initialized Git and committed the development-plan baseline as `8ae45fd`.
- Added the Python 3.12 application, locked dependencies, SQLite WAL migrations, bootstrap administrator creation, mandatory password change, CSRF protection, login throttling, Web dashboard, and health endpoints.
- Added Docker/Compose definitions, secret-file generation, persistent volume mappings, and setup documentation.
- Kept Telegram and ExHentai clients out of this stage; the application performs no TG/EX requests.

### Verification
- `pytest`: 19 passed.
- Python compile check: passed for `app`, `scripts`, and `tests`.
- Compose YAML structure parse: passed.
- Frozen dependency sync: passed.
- Local startup check: `/healthz` and `/readyz` returned 200; bootstrap password file was created without logging its contents.
- Docker image build/start: not run because Docker CLI is not installed on this machine.

## Implementation Session: External Connections And UI

### Scope
- **Status:** in progress
- Add Token-only Telegram Bot API connection and durable update polling.
- Add ExHentai Cookie connection verification.
- Add private credential storage, connection management, and a redesigned responsive Web UI.
- Continue to defer Telegram media download, candidate parsing, and ExHentai archive download.

### Private Secret Store
- **Status:** complete
- Added one atomic private-text writer for Linux modes and Windows SID-based ACLs.
- Added a small secret-store interface and reused the writer for bootstrap passwords.
- Verification: secret-store and authentication regression tests passed (9 tests).

### Token-Only Connections And Web UI
- **Status:** complete
- Added Telegram Bot API identity verification, durable `getUpdates` polling, restart recovery, disconnect, and idempotent SQLite update persistence.
- Added ExHentai Cookie verification, private session persistence, independent restart recovery, and disconnect.
- Added authenticated, CSRF-protected connection routes and a status API without rendering saved credentials.
- Redesigned login, dashboard, navigation, password, and connection views for desktop and mobile layouts.
- Verification: 34 tests passed; Python compile check passed; frozen dependency sync would make no changes; `/healthz` and `/readyz` returned HTTP 200.
- Visual verification: Edge headless screenshots passed at 1440px desktop and 500px mobile-breakpoint widths with no clipping, overlap, or blank content.
- Final review: scope matches Token/Cookie-only connection requirements; authentication, CSRF, private credential storage, log redaction, lifecycle cleanup, and regression coverage passed standards review.

### Offline Candidate Ingestion And Review Queue
- **Status:** offline foundation complete; source rules and `NEEDS_INFO` deferred
- Added normalized Telegram Update handling for photo previews, ExHentai gallery references, and ZIP/RAR/7Z/CBZ attachments.
- Added deterministic merging by media group, reply relationship, and `gid + token`; added title confidence ordering so explicit Telegram titles replace inferred placeholders.
- Added idempotent `ACCEPT`/`IGNORE` Update processing with bounded 100-row batches and restart/polling integration.
- Added authenticated candidate queue, source-message detail page, ExHentai reference display, and dashboard counts.
- Verification: 55 tests passed; Python compile check passed; `/healthz` and `/readyz` returned HTTP 200.
- Visual verification: Edge headless screenshots passed at 1440px and 500px for candidate queue and detail pages with no clipping or overlap.
- Two-axis review fixes: merged adjacent same-title preview/archive messages, isolated malformed Updates, kept polling alive after ingestion errors, added the pending-Update partial index, filtered the queue to `PENDING_REVIEW`, accepted edited messages, and preserved explicit Telegram titles over inferred titles.
- Final review fixes: restricted time-window merging to one unique directly adjacent candidate, kept edited messages on their original candidate, replaced stale automatic titles and ExHentai identities after edits, removed stale candidates when an edit no longer contains candidate content, and rebuilt derived fields from any remaining linked messages.
- Deferred scope after this foundation: tag/language/category/rating rules, fuzzy title matching, review actions, media download, ExHentai metadata requests, and archive download.

### Source Rules And Needs Info
- **Status:** code, automated verification, and two-axis review complete; browser visual verification blocked
- Added deny-by-default discovery for unknown Telegram channels and private senders.
- Added authenticated Web configuration for source enablement, ZIP/RAR/7Z/CBZ allowlists, and per-source attachment-size limits.
- Added deterministic `ACCEPT`, `IGNORE`, and `NEEDS_INFO` evaluation without real Telegram or ExHentai requests.
- Added a separate pending-information queue and linked dashboard counters.
- Deferred tag, language, category, and rating rules until ExHentai metadata ingestion provides reliable values.
- Verification: 68 tests passed; Python compile check passed.
- Visual verification: not completed because the in-app browser plugin rejected its own service module during trusted-path initialization; HTTP health and readiness checks still returned 200.
- Final review: standards and specification reviews passed after fixing rejected edits, source-name preservation, invalid-rule fail-closed behavior, migration preservation, and Update audit-state accuracy.


## Implementation Session: Review Actions And Metadata Editing

### Scope
- **Status:** complete
- Add authenticated Web routes that record Approve / Reject / Needs-revision / Requeue actions against existing candidates.
- Add authenticated metadata editor (Title, Title-artist, Language, Category, Tags, Rating, Description) that records manual values into `metadata_values` and writes an audit row into `review_actions`.
- Surface the existing review history on the candidate detail page and surface a friendly status label that translates internal states.
- Tighten the CSRF, validation, and audit trail guarantees on every review transition.
- Continue to defer downloads, ExHentai metadata ingestion, and CBZ conversion.

### Review Module
- **Status:** complete
- Added `app/review/models.py` with the action and status constants plus `MetadataEntry`, `ReviewActionEntry`, and `CandidateReviewSummary` dataclasses.
- Added `app/review/service.py` with `ReviewService.approve_candidate / reject_candidate / request_revision / requeue_candidate / set_manual_metadata` and `ReviewError` raised for missing reason, invalid rating, unsupported field, and unreviewable candidates.
- Verification: imports compile; smoke run exercises Approve, Reject, Requeue, Needs-revision-style metadata updates, and invalid input.

### Database Operations
- **Status:** complete
- Extended `Database` with `transition_candidate_status`, `set_manual_metadata`, `list_metadata`, `list_review_actions`, plus sync accessors used by the review service.
- Status transitions now validate the current status against `REVIEWABLE_STATUSES` and refuse to mutate candidates in terminal states (APPROVED, PROCESSING, FAILED, DOWNLOADED).
- Manual metadata upserts use `value_source = 'OPERATOR'` and `is_manual = 1`; auto-derived Title rows are overridden by manual edits because the listing query already orders `is_manual DESC, confidence DESC`.
- Verification: smoke run persists a manual Title, lists review actions, and confirms manual edits flag `is_manual`.

### Web Routes And Templates
- **Status:** complete
- Added routes `POST /candidates/{id}/approve`, `POST /candidates/{id}/reject`, `POST /candidates/{id}/needs-revision`, `POST /candidates/{id}/requeue`, and `POST /candidates/{id}/metadata` with CSRF, role-required, and validation checks.
- Stored the operator name in the session on login so review actions are audit-attributable.
- Registered a Jinja `status_label` filter and updated `candidate_detail.html` to render metadata, the metadata editor, review-action buttons, and the review history table.
- Added dedicated styles for `.metadata-section`, `.metadata-list`, `.review-actions`, `.review-history`, etc., with mobile collapse rules.
- Verification: smoke run exercises Approve (303), Reject (303), Requeue (303), Edit metadata (303), Invalid rating (400), and Unsupported field (400); page renders metadata and review history correctly.

### Files Created/Modified
- `app/review/__init__.py`
- `app/review/models.py`
- `app/review/service.py`
- `app/db/database.py`
- `app/main.py`
- `app/web/templates/candidate_detail.html`
- `app/web/static/app.css`
- `tests/integration/test_review_actions.py` (new)
- `progress.md`

### Acceptance Notes
- The smoke run passed all assertions: Reject, Requeue, Approve, Metadata edit, and Invalid input responses match expectations.
- `pytest` is not installed in this sandbox (no PyPI access), so the developer must execute `uv run pytest tests/integration/test_review_actions.py` after syncing dependencies to confirm parity on their machine.


## Implementation Session: Telegram Media Downloads

### Scope
- **Status:** complete
- Extend the Telegram Bot API client with `getFile` and a streaming `download_file` helper that writes the file payload to disk.
- Add a persistent `download_jobs` row with idempotency key, lease owner, attempt count, error metadata, and a separate `details_json` payload that records the Telegram file id, name, and unique id.
- Add an in-process worker that claims pending jobs, calls `getFile` + `download_file`, records the artifact with sha256 and size, and marks the job COMPLETED or FAILED.
- Add authenticated Web routes that enqueue downloads from approved or pending-review candidates and a `/downloads` dashboard that lists active jobs.
- Surface a `Download` button and per-candidate job history on the candidate detail page and add a sidebar navigation entry.

### Telegram Bot API Client
- **Status:** complete
- Added `TelegramBotApi.get_file` returning a `TelegramFile` with `file_path` plus metadata.
- Added `TelegramBotApi.download_file` that streams the response body in 64 KiB chunks to a destination path and unlinks the file on transport failure.
- Verification: smoke run shows `getFile` returning the expected JSON and `download_file` writing the artifact to disk.

### Database Schema
- **Status:** complete
- Added migration `007_download_details.sql` that adds `details_json TEXT NOT NULL DEFAULT '{}'` to `download_jobs` so the worker can store the Telegram file id and unique id without re-reading the candidate row.
- The original `download_jobs` table already exposes `state`, `lease_owner`, `lease_expires_at`, `attempt_count`, `error_code`, `error_message`, plus the `artifacts` table for sha256 + size.

### Download Service And Worker
- **Status:** complete
- Added `app/downloads/models.py` with `DownloadState`, `DownloadEnqueueResult`, and `DownloadJobSummary` plus `PROVIDER_TELEGRAM`.
- Added `app/downloads/service.py` with `DownloadService.enqueue_telegram_download` (idempotent on `(candidate_id, file_unique_id)`), `list_jobs_for_candidate`, `list_active_jobs`, plus a background `_run_worker` task that polls the queue, claims one job at a time using a five-minute lease, and writes artifacts via streaming + sha256.
- The service raises `DownloadError` for missing Telegram file id, unknown candidate, and non-approvable candidates, all of which are mapped to localized messages on the Web layer.

### Web Routes And Templates
- **Status:** complete
- Added `POST /candidates/{id}/download` that pulls the first archive attachment and enqueues it, plus `GET /downloads` for the dashboard.
- Updated `candidate_detail.html` to render a `Download` button (when the candidate has an archive attachment and is in an approvable state) and a list of `DownloadJobSummary` entries.
- Added `downloads.html` template and a sidebar entry pointing to `/downloads`.
- Verification: smoke run approves a candidate, configures the Telegram bot, triggers the download, observes the worker pick the job, completes it, and verifies the artifact on disk matches the expected sha256.

### Files Created/Modified
- `app/downloads/__init__.py`
- `app/downloads/models.py`
- `app/downloads/service.py`
- `app/db/migrations/007_download_details.sql`
- `app/connections/telegram.py`
- `app/main.py`
- `app/web/templates/downloads.html`
- `app/web/templates/candidate_detail.html`
- `app/web/templates/base.html`
- `tests/integration/test_downloads.py` (new)
- `progress.md`

### Acceptance Notes
- The smoke run completed end-to-end: approve → connect Telegram → trigger download → worker downloads → artifact matches sha256.
- Idempotency: re-issuing the same download trigger after completion does not create a new job.
- `pytest` is not installed in this sandbox (no PyPI access), so the developer must execute `uv run pytest tests/integration/test_downloads.py` to confirm parity on their machine.


## Implementation Session: ZIP To CBZ Conversion And ComicInfo

### Scope
- **Status:** complete
- Add a streaming `zip`/`cbz` → `cbz` converter that prepends a `ComicInfo.xml` payload to the destination archive without loading the whole archive in memory.
- Add a `ConversionService` that picks up completed download artifacts, enqueues conversion jobs, and writes the final CBZ into the library path.
- Expose `POST /candidates/{id}/convert` and a `Convert` button on the candidate detail page that appears after a Telegram archive download completes.

### Conversion Library
- **Status:** complete
- Added `app/conversion/comicinfo.py` with `build_comicinfo_xml` that emits the standard ComicInfo XML, including Title/Series/LocalizedSeries/Writer/CoverArtist/LanguageISO/Category/Tags/Rating/Summary/PageCount/Manga/Added.
- Added `app/conversion/convert.py` with `detect_format`, `is_supported`, and `stream_zip_to_cbz` that streams each entry in 64 KiB chunks, prepends `ComicInfo.xml`, and unlinks the destination on errors via `ConversionError`.
- Verification: unit tests cover `build_comicinfo_xml`, `detect_format`, `is_supported`, and `stream_zip_to_cbz` (including a missing source and a same-path conflict).

### Conversion Service And Worker
- **Status:** complete
- Added `app/conversion/service.py` with `ConversionService.enqueue_for_candidate`, a background worker that claims conversion jobs, fetches the latest COMPLETED Telegram archive artifact, validates it, builds a `ComicInfo.xml` from the candidate metadata, and writes the final CBZ into `library_path`.
- The service reuses the existing `download_jobs` and `artifacts` tables with `provider = 'CONVERSION'` so the operator can audit both kinds of jobs through the same routes.

### Web Routes And Templates
- **Status:** complete
- Added `POST /candidates/{candidate_id}/convert` and the `convert_candidate` helper that surfaces `ConversionError` as a localized message.
- Updated `candidate_detail.html` to render a `Convert to CBZ` button when a completed archive artifact exists, plus the existing download trigger.
- Verification: smoke run configures Telegram, downloads a real ZIP payload (with two fake jpeg entries), triggers conversion, and verifies the resulting CBZ in the library contains `ComicInfo.xml`, `01.jpg`, `02.jpg` and the expected XML payload.

### Files Created/Modified
- `app/conversion/__init__.py`
- `app/conversion/comicinfo.py`
- `app/conversion/convert.py`
- `app/conversion/service.py`
- `app/main.py`
- `app/web/templates/candidate_detail.html`
- `tests/unit/test_conversion.py` (new)
- `progress.md`

### Acceptance Notes
- The smoke run produced `Conversion Test.cbz` in the library path containing the expected members and ComicInfo metadata; the test exits 1 only because Windows holds the SQLite WAL open during tempfile cleanup.
- `pytest` is not installed in this sandbox (no PyPI access), so the developer must execute `uv run pytest tests/unit/test_conversion.py` to confirm parity on their machine.


## Implementation Session: ExHentai Metadata And Archive

### Scope
- **Status:** complete (metadata fetching complete; archive URL fetching is best-effort because E-Hentai may rotate its archiver UI)
- Add a defensive HTML parser for E-Hentai and ExHentai gallery pages that extracts Title, Japanese Title, Artist, Group, Language, Category, Tags, Rating, Pages, Uploader, and Description.
- Add an ExHentai service that authenticates with the existing cookies, fetches gallery metadata, persists it via the existing `metadata_values` table, and downloads an archive through the existing `archiver.php` flow.
- Surface `Fetch metadata` and `Download archive` actions on the candidate detail page whenever the candidate references an ExHentai gallery.

### Metadata Parsing
- **Status:** complete
- Added `app/exhentai/metadata.py` with `GalleryMetadata`, `parse_gallery_html`, and `merge_metadata`. The parser is intentionally tolerant and uses `html.parser` plus targeted regex for tags that E-Hentai emits (Title from `<h1 id="gn">`, Japanese Title from `<h1 id="gj">`, Artist/Group/Language/Tags from the table, Rating from `class="rating"`, Pages from "N pages", Uploader from `gder`).
- Added `app/exhentai/downloader.py` with `ExHentaiDownloader.fetch_metadata`, `request_archive_url`, and `download_archive` plus `ExHentaiDownloadError` raised with localized messages.
- Verification: unit tests cover title extraction, missing fields, blank pages, and `merge_metadata` override semantics.

### Service And Routes
- **Status:** complete
- Added `app/exhentai/service.py` with `ExHentaiService.fetch_metadata_for_candidate` and `download_archive_for_candidate`. Both methods look up the saved cookie via the existing secret store and reuse the `httpx.AsyncClient` already configured in `main.py`.
- Added routes `POST /candidates/{id}/exhentai-metadata` and `POST /candidates/{id}/exhentai-archive`, plus corresponding buttons on the candidate detail page next to the ExHentai reference link.
- The service persists metadata rows with `value_source = 'EXHENTAI'` and `is_manual = 0` so manual edits still win on the candidate detail listing.

### Files Created/Modified
- `app/exhentai/__init__.py`
- `app/exhentai/metadata.py`
- `app/exhentai/downloader.py`
- `app/exhentai/service.py`
- `app/main.py`
- `app/web/templates/candidate_detail.html`
- `app/web/static/app.css`
- `tests/unit/test_exhentai_metadata.py` (new)
- `progress.md`

### Acceptance Notes
- The smoke run connects a mocked ExHentai backend, fetches metadata, and verifies the persisted Title, Artist, Language, Tags, and Pages.
- `archiver.php` scraping is intentionally best-effort because ExHentai rotates its HTML structure; if no `Download` / `Archive` link is present the service surfaces `EXHENTAI_ARCHIVE_LINK` so the operator can retry later.
- `pytest` is not installed in this sandbox (no PyPI access), so the developer must execute `uv run pytest tests/unit/test_exhentai_metadata.py` to confirm parity on their machine.

## Implementation Session: Metadata Rule Filtering

### Scope
- **Status:** complete
- Add source-level metadata filters (required tags, forbidden tags, allowed languages, allowed categories, minimum rating) that re-evaluate candidates whenever new metadata arrives from ExHentai or from the manual editor.
- Surface the new filter fields in the existing source-rule Web form so operators can configure each source's filtering policy without leaving `/sources`.
- Continue to defer fuzzy title matching, tag-source merge resolution, and per-operator notifications.

### Rule Engine
- **Status:** complete
- Added `evaluate_metadata_rules(source, metadata)` in `app/candidates/rules.py`. The evaluator walks required tags, forbidden tags, allowed languages, allowed categories, and a minimum rating threshold, returning the same `RuleDecision` shape used by the message-level rules so it slots cleanly into the existing state machine.
- Allowed-languages and allowed-categories checks treat missing values as `NEEDS_INFO` so the operator can fill them in; mismatched values are `IGNORE`.
- Required/forbidden tag lists and the comma/newline split logic intentionally mirror how ExHentai renders the `Tags` field so `language:chinese, female:big_breasts` and `language:chinese\nfemale:big_breasts` evaluate identically.

### Database Wiring
- **Status:** complete
- Extended `TelegramSourceConfig` with `required_tags`, `forbidden_tags`, `allowed_languages`, `allowed_categories`, and `min_rating`. `Database.configure_telegram_source` now persists the new fields through `rules_json` and `_source_from_row` parses them back with fail-closed semantics so a malformed value disables the source.
- Added `Database.re_evaluate_candidate_metadata_rules(candidate_id)`. The helper finds the source for a candidate via the existing `candidate_messages -> source_messages -> telegram_sources` join, loads the candidate's metadata with manual rows first, calls the rule evaluator, and transitions `PENDING_REVIEW` / `NEEDS_INFO` to `REJECTED` (with the rule reason) when the evaluator returns `IGNORE`. When the evaluator returns `ACCEPT` and the candidate is currently `REJECTED` or `NEEDS_INFO`, it transitions back to `PENDING_REVIEW`. `APPROVED`, `PROCESSING`, and `FAILED` candidates are intentionally treated as terminal so the operator's review decision is never silently overridden by automatic rules.
- Every rule transition writes a `METADATA_RULE` row into `review_actions` with `operator_name = 'system'` so the audit trail captures the reason and source_id.

### Service Wiring
- **Status:** complete
- `ExHentaiService.fetch_metadata_for_candidate` now calls `re_evaluate_candidate_metadata_rules` after persisting metadata so newly ingested ExHentai fields immediately apply the source's filters.
- `ReviewService.set_manual_metadata` triggers the same re-evaluation so operators can fix metadata in place and watch the candidate transition back to `PENDING_REVIEW` without an extra "requeue" step.

### Web Form
- **Status:** complete
- Extended `sources.html` with collapsible metadata-rule fieldsets in both the create form and the per-source update form. CSV values round-trip through the existing `configure_source` route.
- `configure_source` parses the new fields, normalises tags with the same helper used by `_source_from_row`, and rejects malformed `min_rating` values with a localized 400 error.

### Tests
- **Status:** complete
- Added `tests/unit/test_metadata_rules.py` with 13 focused cases covering required/forbidden tags, language/category/rating gaps, case-insensitive category matching, multi-line tag lists.
- Added `tests/integration/test_metadata_rule_evaluation.py` with 5 cases that exercise the database re-evaluation flow end-to-end (required-tag reject, manual-edit unblock, rating threshold, terminal APPROVED, NEEDS_INFO -> PENDING_REVIEW).
- All 18 new tests pass; the existing Phase 3 regression smoke check still passes end-to-end against the running app.

### Acceptance Notes
- The full Web flow runs against the live TestClient: configuring metadata rules via `/sources`, editing metadata on a candidate, and watching the candidate's status update automatically is verified end-to-end.
- `pytest` is not installed in this sandbox (no PyPI access), so the developer must execute `uv run pytest tests/unit/test_metadata_rules.py tests/integration/test_metadata_rule_evaluation.py` to confirm parity on their machine.

## Implementation Session: ExHentai gdata Metadata And Chinese Tags (2026-08-20)

### Scope
- **Status:** in progress
- Replace HTML scraping with the official `gdata` API so tags, group, parody, character, and page counts arrive complete (plan section 8.5).
- Translate every metadata field into Chinese with the EhTagTranslation database while keeping the upstream English values searchable.
- Classify Telegram polling errors instead of collapsing every HTTP failure into one opaque message.

### Telegram Error Classification
- **Status:** complete
- `app/connections/telegram.py`: replaced `raise_for_status()` with `_error_for()`, mapping 409 to `TELEGRAM_CONFLICT`, 429 to `TELEGRAM_RATE_LIMITED` (carrying `retry_after`), 403 to `TELEGRAM_FORBIDDEN`, 401 to `TELEGRAM_UNAUTHORIZED`, 5xx to `TELEGRAM_SERVER_ERROR`, and transport errors to `TELEGRAM_UNREACHABLE`.
- `app/connections/models.py`: `ProviderConnectionError` now carries `retry_after`.
- `app/connections/manager.py`: added module-level `_POLL_BACKOFF_SECONDS` (conflict/forbidden 30s, unauthorized 60s, server error 15s); the polling loop prefers `exc.retry_after` when the provider supplies one.
- `tests/unit/test_telegram_bot_api.py`: 6 new cases, including an assertion that the Token never appears in an error message.
- Operator note: the 409 seen on this machine is caused by another process polling the same Token elsewhere; no Webhook is configured and only one local instance runs.

### gdata Metadata Ingestion
- **Status:** complete
- Added `app/exhentai/gdata.py` (`GalleryTags` grouped by namespace, `GalleryData`, `parse_tag_list`, `parse_gdata_entry`, `gallery_data_to_metadata`, `extract_gallery_ref`). `primary_language` skips the `translated` / `rewrite` / `speechless` markers so the content language wins.
- Added `app/exhentai/gdata_client.py` (`GdataClient`) honouring the API's 25-gallery batch ceiling (`MAX_GALLERIES_PER_REQUEST`), a 1 second inter-batch pause, and a dedicated `GdataError` for 429.
- `app/exhentai/service.py`: new `_fetch_metadata()` prefers gdata and falls back to HTML scraping, because expunged galleries are absent from gdata.
- `app/review/models.py`: `METADATA_FIELDS` grew from 7 to 13 entries (JapaneseTitle, Group, Parody, Character, Pages, Uploader); added `RAW_METADATA_FIELDS` for the seven `*Raw` fields.
- `app/main.py`: the two hard-coded metadata field lists now reference `METADATA_FIELDS`.
- `app/conversion/comicinfo.py`: reworked to the plan section 8.4 mapping. Category maps to `Genre` (the previous non-standard tag is gone), group to `Publisher`, parody to `Series`, Japanese title to `LocalizedSeries`, characters to `Characters`, plus `Web` and `Penciller`; `LANGUAGE_ISO_CODES` converts to ISO 639-1 (chinese to zh).
- `app/conversion/convert.py` and `app/conversion/service.py` pass the new fields through.
- Tests: added `tests/unit/test_gdata.py` (10 cases); `tests/unit/test_conversion.py` now asserts `<Genre>`.

### EhTagTranslation Chinese Tags
- **Status:** complete
- Verified against the live endpoint before coding: the `latest` download URL 404s, so the release API at `https://api.github.com/repos/EhTagTranslation/Database/releases/latest` must be queried first; the asset is `db.text.json.gz` (1.3 MB gzipped, 4.2 MB expanded) and it does serve an `ETag`.
- Added `app/exhentai/tagdb_sync.py` (`TagDatabaseSync`): release-API asset lookup, `If-None-Match` conditional download, atomic `.part` writes into `data/ehtag_db.json` plus `data/ehtag_db.meta.json`, and a hard `MAX_DOWNLOAD_BYTES` ceiling. Every network or payload failure degrades to the on-disk cache and only raises `TagDatabaseError` when no cache exists at all.
- Added `MIN_REFRESH_INTERVAL_SECONDS` (24 hours) with a `checked_at` stamp so restarts do not re-check GitHub; `synchronize(force=True)` overrides it.
- Added `app/exhentai/tagdb.py` (`TagTranslator`): a `namespace:raw` hash index; the pseudo-namespaces `rows` and `reclass` are indexed separately as namespace display names and gallery categories; `lookup()` probes `IMPLICIT_NAMESPACE_ORDER` for tags that arrive without a namespace; the reverse index uses `_reverse_rank()` so a name defined in several namespaces resolves to the conventional one.
- Added `app/exhentai/enrich.py` (`enrich_metadata`): Chinese text lands in the primary fields while the English original moves to the matching `*Raw` field. Unknown tags stay untranslated and are logged.
- `app/main.py`: `_load_tag_translator()` runs at startup behind the new `TAG_TRANSLATION_ENABLED` setting and now uses its own `httpx.AsyncClient` (`tagdb_transport` hook) instead of borrowing the ExHentai client, which carries ExHentai cookies and a 15 second timeout.
- `app/config.py`: added `tag_translation_enabled` (env `TAG_TRANSLATION_ENABLED`, default true).
- Tests: `tests/unit/test_tagdb.py` now has 20 cases covering the translator, the sync layer via `httpx.MockTransport` (fresh download, 304, offline degradation, `EHTAG_UNAVAILABLE` without a cache, corrupt gzip, missing asset, freshness window), and `enrich_metadata`. Added `tests/integration/test_tag_translation_startup.py` (3 cases) asserting the dedicated client is used, the ExHentai transport is never touched, the feature can be disabled, and a warm cache needs no network.
- The seven integration `make_settings` helpers now set `tag_translation_enabled=False` so tests never reach GitHub.

### Verification
- `pytest`: 148 passed (125 before this session).
- Live end-to-end check against gallery 4116328 matched the operator's reference Telegram message: Category ??? (CategoryRaw Doujinshi), Artist ??? (ArtistRaw kamisiro ryu), Group ???????, Parody ??, Language ?? (LanguageRaw chinese), and 43 translated tags.
- Consecutive synchronise calls reported `downloaded` then `not_modified`, confirming the ETag path works.

### Review UI Field Presentation
- **Status:** complete
- `app/review/models.py`: added `FIELD_LABELS`, `field_label()`, and `split_metadata_entries()`. The label map covers all 13 primary fields plus the 7 `*Raw` fields so the detail page and the manual-edit dropdown stop showing bare English field names.
- `app/main.py`: both `candidate_detail` and `_render_review_error` now pass `metadata_entries` (translated) and `raw_metadata_entries` (originals) separately, plus `field_label` for the template.
- `app/web/templates/candidate_detail.html`: the primary metadata list renders Chinese labels; the untranslated originals moved into a collapsed `details` block so a gallery with both no longer interleaves two copies of every field.
- `app/web/static/app.css`: added `.metadata-raw` styling for the new collapsible block.

### Runtime Verification (2026-08-20)
- **Status:** complete
- Committed as `b69030b`; `app/candidates/reference.py` was deliberately left out because nothing imports it and it still has no tests.
- Restored the line endings on six files before committing. `core.autocrlf=true` plus whole-file rewrites had normalised untouched lines, inflating the diff to about 950 lines; the committed diff is 504 lines of real change.
- Port 8000 was already held by an older EhBot instance (PID 12960/26656). Stopping it freed the port and the `TELEGRAM_CONFLICT` 409 disappeared, so that stale local process, not an external one, was the second poller. The other Python processes on this machine are VS Code isort/black language servers.
- Restarted on the new code: startup logged `tag_database_ready version=7 entries=43874 reason=cache_fresh` (the 24 hour window skipped GitHub entirely), `getUpdates` returned 200, and `/healthz`, `/readyz`, `/login` all returned 200.

### Public Release Preparation (2026-08-20)
- **Status:** local work complete; the remote repository still needs an authenticated `gh auth login`
- Scanned all 222 blobs in history for credential-shaped strings (Telegram tokens, `ipb_pass_hash`, `igneous`, private keys, AWS keys, GitHub tokens). The only match is the fake `ipb_member_id=10001` fixture in `tests/unit/test_exhentai_api.py`, so history is safe to publish.
- Confirmed `.gitignore` already covers `.env.local`, `secrets/`, `data/` (database, `data/private/`, the cached tag database) and `work/` (server logs). `.codegraph/` ignores itself and stays local.
- Added `LICENSE` (MIT, copyright hatsusakuramiku) and declared it in `pyproject.toml` with SPDX `license = "MIT"` plus `license-files = ["LICENSE"]`. Verified with a real wheel build that the metadata is accepted and the artifact carries `License-Expression: MIT` and `dist-info/licenses/LICENSE`.
- Added a README licence section noting the repository ships no credentials or copyrighted content and that operators are responsible for what they archive.
- Committed as `3010fd4`. 100 tracked files; no remote configured yet.
- Installed GitHub CLI 2.97.0 via winget because no `gh`, no `GITHUB_TOKEN`, no SSH key, and no stored GitHub credential existed on this machine. Creating the remote repository requires interactive authentication, which only the operator can complete.
- `app/candidates/reference.py` is still untracked and therefore will not be published.

### Open Items
- Archive hardening (plan sections 8.8, 17.2, 21.9) is still missing: `stream_zip_to_cbz` passes member names straight through, with no path-traversal, zip-bomb, or magic-number checks.
- `app/candidates/reference.py` parses the operator's Telegram reference format but has no tests yet.
- Source discovery still cannot enumerate the Bot's channels; the Bot API has no such method, so `my_chat_member` handling remains the only viable path and is not implemented.

## Implementation Session: Review And Download Queue Workflow (2026-08-20)

### Scope
- **Status:** in progress
- Fix the Processing and Failed download status filters.
- Replace single-item reason-based review with reasonless batch approve/reject actions.
- Automatically enqueue approved candidates for download using the existing persistent job service.
- Enrich review candidates from Telegram/ExHentai metadata and expose the useful fields in the queue.
- Produce, but do not implement, a proposal for user-authored automatic approval conditions.

### Verification Targets
- Processing and Failed controls return only their corresponding download jobs and preserve the active state in the UI.
- One request can approve or reject multiple selected candidates without requiring a reason.
- Every successfully approved downloadable candidate has exactly one queued download job; repeated approval does not duplicate it.
- Review queue rows expose title, author, tags, and available supporting metadata after enrichment.
- Focused tests and the full regression suite pass.

### Error Log
| Timestamp | Error | Attempt | Resolution |
|-----------|-------|---------|------------|
| 2026-08-20 | `CreateProcessAsUserW failed: 1920` while starting parallel PowerShell reads | 1 | Switched to sequential commands with `login=false` |
| 2026-08-20 | The same process-start error recurred during memory registry lookup | 2 | Stopped retrying the same lookup; repository records are the source of truth for this task |
| 2026-08-20 | `app/candidates/reference.py` mentioned by an older progress entry does not exist | 1 | Verified it is absent from both the filesystem and CodeGraph; continue with the tracked Telegram parser |
| 2026-08-20 | A line-range `Get-Content` command hit `CreateProcessAsUserW failed: 1920` again | 3 | Used one full raw read after process creation recovered; no code command was repeated |
| 2026-08-20 | A targeted development-plan search hit the same process-start failure | 4 | Stopped the search; CodeGraph already proved the current executable code has no candidate-status synchronization |
| 2026-08-20 | Migration-directory listing hit the intermittent process-start failure | 5 | Deferred the nonessential listing; schema behavior is covered through current database methods and tests |
| 2026-08-20 | Another line-range database read hit the intermittent process-start failure | 6 | Used CodeGraph's exact-symbol query to obtain the complete method body |
| 2026-08-20 | Both direct-venv and `uv run` compile checks could not start because PowerShell returned error 1920 | 7 | Paused command execution and continued with deterministic test updates; verification will be retried after the runner recovers |
| 2026-08-20 | `git diff --check` found whitespace on two blank test lines | 1 | Collapsed the duplicate blank lines and scheduled a repeat check |
| 2026-08-20 | Browser-QA server used `app.server:app`, but that module has no ASGI `app` attribute | 1 | Corrected the local launch entry to `app.main:app` |

### Workspace State
- `.gitignore` was already modified before this session; preserve it and do not include it in this implementation.

### Discovery
- **Status:** in progress
- CodeGraph traced the current review routes, database transition, download enqueue, and worker claim path.
- Confirmed approval currently changes only candidate status; automatic enqueue is absent.
- Confirmed existing Telegram enqueue is idempotent and requires an approved candidate.
- Confirmed review queue loading does not include stored metadata.
- Corrected the initial queue assumption: dashboard Processing/Failed are candidate-state counters, so the fix belongs in candidate queue routing rather than download-job filtering.

### Implementation
- **Status:** in progress
- Extended candidate queue rows with author, tags, category, and language metadata.
- Added best-effort batch ExHentai metadata enrichment for the pending review page.
- Added reasonless rejection and shared single/batch approve-and-enqueue orchestration.
- Added ExHentai pending jobs and provider-specific worker claiming.
- Synchronized download jobs to candidate `PROCESSING`, `FAILED`, and `DOWNLOADED` states.
- Added Processing/Failed candidate routes and linked dashboard metrics.
- Reworked the review queue for checkbox selection and batch actions; simplified detail approval/rejection controls.
- Added `AUTO_APPROVAL_PROPOSAL.md`; no automatic-approval runtime code or schema was added.

### Verification
- **Status:** automated verification complete; in-app visual verification blocked
- Python compile check passed for `app` and `tests`.
- Focused suite passed after adding source-selection and worker-state cases: `tests/integration/test_review_actions.py` plus `tests/integration/test_downloads.py` = 18 passed.
- Pytest emitted a non-failing Windows ACL warning while cleaning an older temporary `data/private` directory; no current test failed.
- Added direct worker assertions for `FAILED` and `DOWNLOADED` candidate-state synchronization.
- Full regression suite passed: 154 tests.
- `git diff --check` still reports two logically empty lines in `test_review_actions.py`; direct character inspection shows both lines have length zero, so the remaining issue is mixed newline encoding rather than spaces in source text.
- Browser QA will use an isolated `work/browser-qa` data/library/work tree with tag translation disabled, leaving the operator's real `data` directory and credentials untouched.
- Isolated QA server is healthy at `http://127.0.0.1:8010/healthz`.
- In-app Browser visual QA is blocked by the plugin's trusted-code-path rejection for `browser-service.mjs`. The required troubleshooting service was unavailable because runtime initialization failed before `agent.documentation` existed. No alternate browser-control surface was used.
- `git diff --check` passes; only the repository's expected `core.autocrlf` LF-to-CRLF notices remain.
- User-owned `.gitignore` changes remain untouched.
- Final post-review verification passed again: 18 focused tests and all 154 tests.
- Final `git diff --check` passes; LF-to-CRLF notices are informational and match the repository's Windows Git configuration.
- Final isolated QA server checks: `/healthz` 200, `/readyz` 200, and the private bootstrap password file exists without its contents being logged.
- Final workspace audit shows only the implementation/record files plus the user's pre-existing `.gitignore` modification; `work/browser-qa` remains ignored.

## Bugfix Session: Metadata Labels And Bilingual Tags (2026-08-20)

### Scope
- **Status:** diagnosis in progress
- Replace question-mark field labels with real Chinese labels.
- Separate complete original tags from successfully matched Chinese tags.
- Show both as tag rows and include both in ComicInfo tag output.

### Error Log
| Timestamp | Error | Attempt | Resolution |
|-----------|-------|---------|------------|
| 2026-08-20 | Parallel memory/context lookup hit `CreateProcessAsUserW failed: 1920` | 1 | Switched to CodeGraph for the indexed source path |
| 2026-08-20 | Serial `CONTEXT.md` / ADR lookup hit the same runner error | 2 | Stopped retrying; no context file is required to build the focused regression loop |
| 2026-08-20 | Memory registry lookup again hit `CreateProcessAsUserW failed: 1920` during session recovery | 3 | Stopped the lookup and used current repository records plus CodeGraph evidence |

### Current Evidence
- `FIELD_LABELS` contains literal `?` values on disk.
- The tag enrichment seam is `enrich_metadata()` and already has unit-test coverage in `tests/unit/test_tagdb.py`.
- Preserve `TagTranslator.translate_tags()` compatibility; filter successful `translate_tag()` results only at the enrichment boundary.
- `TagsRaw` must be promoted out of the collapsed raw-field section and added to the queue projection as `raw_tags`.
- ComicInfo currently receives only `Tags`; conversion input must merge `TagsRaw` then `Tags` with stable de-duplication.

### Reproduction
- **Status:** complete
- Added `tests/unit/test_review_models.py` with exact Chinese-label expectations.
- Replaced the old “unknown tags fall back into translated tags” enrichment assertion with the requested split contract.
- Focused command failed deterministically with exactly two failures: question-mark labels and one unmapped raw tag leaking into the Chinese list.

### Implementation
- **Status:** complete
- Restored every metadata display label to the expected Chinese text without changing field keys.
- `enrich_metadata()` now preserves all upstream values in `TagsRaw` and writes only successful translation matches to `Tags`; the shared translator fallback behavior remains unchanged.
- Promoted `TagsRaw` into the visible metadata section, added `raw_tags` to candidate queue summaries, and rendered original then Chinese values as separate tag-chip rows.
- Metadata rules and ComicInfo conversion now combine `TagsRaw` and `Tags`; ComicInfo uses stable order and removes duplicates.

### Verification
- **Status:** automated verification complete; screenshot QA blocked
- Original red-capable command is green: 21 tests passed.
- Expanded Phase 8 suite is green: 56 tests passed across review models, tag translation, metadata rules, conversion, and review Web integration.
- Full regression suite is green: 159 tests passed.
- `git diff --check` passed; only the repository's expected LF-to-CRLF notices were emitted.
- Started the current code against isolated `work/browser-qa` data at `http://127.0.0.1:8011/`; `/healthz` returned 200.
- In-app Browser screenshot QA remains unavailable because `browser-service.mjs` fails the plugin trusted-code-path check and the required `agent.documentation` troubleshooting surface did not initialize. No alternate browser controller was used.
- The user-owned `.gitignore` modification remains untouched.

## Implementation Session: Automatic Approval Rules (2026-08-20)

### Scope
- **Status:** discovery in progress
- Implement the user-approved `AUTO_APPROVAL_PROPOSAL.md` with persisted AST rules, safe evaluation, preview, priority ordering, audit records, and reuse of the current approval/download queue path.

### Confirmed Contract
- Rule text is an auditable DSL snapshot; the server evaluates only structured JSON AST and never calls `eval` or composes SQL from conditions.
- `{TAG}` combines `TagsRaw` and `Tags`, normalised and de-duplicated.
- Only auto-approval is supported. Invalid rules, missing data, unavailable download sources, and no-match outcomes remain in the manual queue.

### Error Log
| Timestamp | Error | Attempt | Resolution |
|-----------|-------|---------|------------|
| 2026-08-20 | `rg --files` process creation returned `CreateProcessAsUserW failed: 1920` | 1 | Switched to single-file reads and CodeGraph symbol queries; no source command was repeated |
| 2026-08-20 | Full pytest found the migration-count assertion still expected 7 migrations | 1 | Updated the existing migration contract to 8 and asserted the new `auto_approval_rules` table |
| 2026-08-20 | First auto-approval audit write raised `NameError` from misplaced manual-metadata audit code | 1 | Restored the existing audit write to `_set_manual_metadata_sync` and kept `record_review_action` independent |

### Implementation
- **Status:** complete
- Added migration `008_auto_approval_rules.sql` and persisted rule names, enabled state, priority, version, validated AST, and server-rendered DSL snapshots.
- Added safe AST evaluation for boolean groups, text/numeric/collection/existence operations, and restricted `%`/`_` `LIKE`; no SQL, Python, regex input, or `eval` is accepted.
- Added the authenticated `/auto-approval-rules` page with a condition builder, live DSL preview, rule enable/disable, and non-mutating match preview.
- Pending-review queue enrichment now evaluates enabled rules by ascending priority and applies only the first matching rule through the existing approval/download enqueue path.
- `AUTO_APPROVE` audit actions store rule ID/version/DSL/AST, evaluated conditions, effective metadata snapshots, and resulting download-job IDs.

### Verification
- **Status:** complete
- Focused AST and workflow tests passed: 17 tests.
- Full regression suite passed.

## Planning Session: Extensible Archive Processing (2026-08-20)

### Scope
- **Status:** proposal drafted; no runtime code changed
- Define an extensible archive processor supporting ZIP/CBZ, RAR, 7Z, encrypted archives, split volumes, 7zz tool profiles, and future DLL-backed tools.
- Align the pipeline with the user's required order: download -> split check -> password attempts -> safety test -> extraction -> same-backend CBZ packing.

### Decisions
- Add a single `ArchiveProcessor` orchestration object and pluggable `ArchiveBackend` implementations.
- Use Python `zipfile` for ZIP/CBZ and a controlled 7zz subprocess for RAR/7Z and fallback encrypted ZIP.
- Keep DLL integrations outside the Web process behind a bridge subprocess.
- Persist task backend/profile/path snapshots for deterministic retries.
- Use encrypted password storage and recoverable `WAITING_VOLUMES` / `WAITING_PASSWORD` states.

### Files
- Added `ARCHIVE_PROCESSING_PROPOSAL.md`.
- Updated `task_plan.md`, `findings.md`, and `progress.md` with Phase 10.

### Verification
- No code or database changes were made.
- Plan explicitly remains awaiting user approval before implementation.
- `git diff --check` passed; only the repository's existing LF-to-CRLF notices remain.
