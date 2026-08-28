# EhBot — Agent Notes

Telegram-sourced manga review and CBZ archiving service. Single container,
SQLite in WAL mode, one administrator, FastAPI + Jinja2. Pipeline:
message -> candidate -> review -> download -> pack.

**Scope ends at the archive.** Download, convert to the target archive format,
plus a few operator-convenience management items. Detailed book/library
management belongs to downstream tools — the library domain was deleted from the
plan on 2026-08-26 by operator instruction. Do not reintroduce it.

Read `progress.md` bottom-up for current state; it ends with a handoff section
naming the next phase. `EHBot.md` is the requirements spec, `DEVELOPMENT_PLAN.md`
the phased plan, `COMPETITIVE_ANALYSIS.md` the research the UI refactor is
based on.

## Environment (Windows / PowerShell 5.1)

These are not preferences; each one silently corrupts output or wastes a cycle.

**Never write Chinese text directly through the shell.** The console input
encoding is GB2312, so redirection and here-strings mangle it. Write files as
UTF-8 without BOM:

```powershell
$enc = New-Object System.Text.UTF8Encoding($false)
[System.IO.File]::WriteAllText($path, $text, $enc)
```

Read with `[System.IO.File]::ReadAllText($path, [System.Text.Encoding]::UTF8)`,
or `Get-Content -Encoding UTF8`.

**Editing Python/CSS reliably:** write a patch script to `$env:TEMP\x.py` using
the UTF8Encoding call above, then run it with `.\.venv\Scripts\python.exe`.
Inside the script read via `io.open(p, encoding="utf-8")`, write via
`io.open(p, "w", encoding="utf-8", newline="\n")`, and `assert old in src`
before replacing so a failed match is loud rather than silent.

**Python:** always `.\.venv\Scripts\python.exe`. There is no `uv` module.
Printing non-ASCII from `python -c` raises `UnicodeEncodeError` unless you set
`$env:PYTHONIOENCODING="utf-8"` — easier to write output to a temp file and
`Get-Content -Encoding UTF8` it.

**`rg.exe` is not usable** (access denied). Use `Select-String`. Note it has no
`-Recurse`; pipe from `Get-ChildItem -Recurse -Include *.py`.

**`.git` is read-only to the sandbox.** `git add`/`commit` need escalated
permissions. `git status --short` works but prints a harmless
`.config/git/ignore: Permission denied` warning.

**`data/private/` is unreadable**, so bare `Get-ChildItem -Recurse` from the
repo root fails partway. Scope recursive listings to `app`, `tests`, etc.

## Tests

Full suite is ~150-320 s. There is **no `pytest-timeout` plugin**, and a PTY
swallows pytest's summary line, so run it as a job and read the JUnit XML:

```powershell
$job = Start-Job -ScriptBlock { Set-Location 'E:\Workshop\VSCode\EhBot'
  & .\.venv\Scripts\python.exe -m pytest --no-header -q --junitxml="$env:TEMP\pt.xml" 2>&1 | Select-Object -Last 20 }
if (Wait-Job $job -Timeout 500) { Receive-Job $job } else { "TIMEOUT"; Stop-Job $job }
Remove-Job $job -Force
$s = ([xml](Get-Content "$env:TEMP\pt.xml")).testsuites.testsuite
"tests={0} failures={1} errors={2}" -f $s.tests, $s.failures, $s.errors
```

**Baseline: 809 passed / 12 skipped / 0 failed.** Ending below this is a
regression. The 12 skips are expected and pre-existing — the real-7-Zip tests
need a toolchain this dev machine cannot host. (Baseline moves per phase:
R0 439 -> R1 524 -> R2 569 -> R3 592 -> R4 635 -> R5 663 -> R6 708 -> R8 809.
There is no R7 — the library domain was deleted from the plan. An older note
claiming "0 skipped" was wrong.)

**Do not seed a `PENDING` job in a test that then asserts on it.** `create_app`'s
lifespan always starts the download worker and there is no toggle, so the worker
claims the row and fails it mid-test. Seed `PAUSED`, `FAILED`, `WAITING_TORRENT`
or `CONVERSION_WAITING_PASSWORD` — which are also the states the queue exists to
show. `tests/integration/test_activity_web.py` has the fixture.

**Do not stream an SSE endpoint through `TestClient`.** It drains the response
body when the context exits and the stream is endless, so it deadlocks. Call the
endpoint function directly and pull frames off `response.body_iterator` with
`anext()`, then `aclose()`.

**Do not verify routing by inspecting `app.routes`.** This FastAPI version defers
`include_router`, showing one `_IncludedRouter` with `path=None`. Issue a real
request instead.

**`response.context` exists only when the final response is a template.** It is
attached through the `http.response.debug` ASGI extension, so a client with
`follow_redirects=False` that stops on a 307 gets a plain `Response` and
`AttributeError`. Forty-eight tests broke this way when the old settings paths
became redirects — the fix is to request the tab that owns what the test asserts,
not to follow the redirect.

**`zoneinfo` has no tz database on this Windows host.** A test that asserts a
zone name round-trips is fine; one that asks `ZoneInfo` to resolve it is not.
Timezone validation is by shape for the same reason a slim container has the same
gap.

## Architecture rules

`/ui-kit` renders every shared component in its real states from fixtures in
`app/web/routes/ui_kit.py` — no database access, so it works on a fresh install.
Open it after touching `ui.css` or `components/ui.html`; it is where a theme,
density or contrast regression shows up first. It is behind the session and is
deliberately absent from `NAV_ITEMS` (a developer tool does not belong in
operator navigation).

- **State vocabulary lives only in `app/api/status.py`**; DTO-to-JSON conversion
  only in `app/api/serializers.py`. Payloads carry resolved `label`/`tone`/`live`
  beside the raw `code` so the browser never translates an enum. A label written
  in a template or in JS is a second source of truth and a bug.
- **`ReviewOrchestrator` (`app/review/orchestration.py`) is the only path that
  may approve a candidate.** Reach it via `deps.review_orchestrator(request)`.
  Never re-implement approve-then-enqueue.
- **JSON routes return 401, never a redirect** — `fetch` follows a redirect
  silently and hands back a login page as 200. JSON CSRF is header-only
  (`X-CSRF-Token`).
- **Status `tone` is semantic** (`waiting`, `danger`, ...), never a colour. CSS
  decides appearance; a theme change must not touch Python.
- **The event bus drops rather than blocks**, and events carry ids only. A
  browser that stops reading must never stall the download worker; the client
  re-reads authoritative state over REST.
- **Migrations are append-only.** The existing twelve are frozen; add `013_*`
  onward.
- **`GET /api/v1/thumbnails/{hash}` accepts a hash and nothing else.** A URL
  parameter would make it an open proxy for anyone holding a session. The only
  admission point is the scrape path, which writes `candidates.thumb_url` and a
  `PENDING` `thumbnails` row in one transaction — so the service can only fetch
  a URL something upstream already vouched for. Never add a URL parameter.
- **The thumbnail hash names the source, not the bytes**:
  `sha256(f"{variant}\0{source_url}")`, in `app/thumbnails/identity.py`. Because
  it is derivable before any fetch, a serializer can emit a cover URL
  synchronously; because the variant is inside it, `Cache-Control: immutable` is
  honest. A second variant sharing a URL would permanently serve the wrong size
  out of the browser cache.
- **Import `app/thumbnails/identity.py`, not `service.py`, when you only need to
  name a thumbnail.** The split exists so the scrape and serialization paths do
  not drag httpx and Pillow in.
- **A failed thumbnail is a 200 placeholder, not a 404.** An `<img>` whose `src`
  404s renders as a broken-image icon with no way to style around it; the state
  travels in the `X-Thumbnail-State` / `X-Thumbnail-Error` headers instead.
- **`looks_like_image` lives in `app/archive/safety.py`** and is shared by the
  telegraph fetcher, the conversion path and the thumbnail renderer. Do not add
  a fourth magic-number table.
- **`is_locked` is not a duplicate of `is_manual`.** `is_manual` protects text
  the operator typed; `is_locked` pins a value ExHentai supplied that the
  operator judged correct. Both guards are needed in the metadata upsert, and a
  lock covers **every** row for the field, not just the winning one.
- **Job claim is `ORDER BY priority, id`, never `ORDER BY priority` alone.**
  Every pre-column job is priority 100, so within one priority the queue must
  stay FIFO — promoting one job reorders that job and nothing else.
- **`app/web/static/ui.css` is the authoritative stylesheet; `app.css` is
  frozen.** New rules go in `ui.css`. `app.css` holds only pre-refactor page CSS
  and is deleted outright in R9 — the split is a file boundary rather than two
  halves of one file precisely so that deletion is `rm` and not a careful cut.
- **Every rule in `ui.css` is class-scoped, deliberately.** `app.css` still
  hardcodes a light `body` background and a light `--ink`, so a bare `body` or
  `p` rule in `ui.css` would put muted grey on near-black across the pages not
  yet rewritten — about 3.2:1, under the 4.5:1 the project is held to. Pages
  awaiting rewrite carry `data-legacy="true"` on `.ui-main`, which pins them to
  light with `color-scheme` included; drop that attribute in the same commit that
  rewrites the page, never before. `base.html` supplies the attribute by default,
  so a page is legacy unless it overrides `main_attrs` — after R8 that leaves
  `dashboard.html`, `manual_add.html` and `login.html`, and **`app.css` cannot be
  deleted while any of the three still renders against it.**
- **`NAV_ITEMS` in `app/web/routes/shell.py` is the only navigation source.**
  The sidebar, the phone tab bar and its drawer all render from it. Three
  renderings of one list is not three lists — the previous hand-written pair had
  already drifted by one link. A test compares the rendered destination sets.
- **Only a leaf claims `aria-current="page"` — ask `is_current()`, not
  `matches()`.** A parent's prefix is by construction a prefix of its children's
  paths, so `matches()` is true for both and two elements would announce
  themselves as the current page. A section whose child is open gets the
  `is-active` class instead. An "index" child whose path equals its parent's
  (`/candidates`, `/activity`, `/connections`) must be declared `exact=True`,
  or it prefix-matches its own siblings. A converted page's own tab strip marks
  the current tab as well, so a page may legitimately render the marker several
  times — the invariant is one *destination*, not one occurrence.
- **A state's Chinese label is never written in a template or in JS.** The badge
  macro takes the whole `StatusView` from `app/api/status.py`, so a page and a
  JSON response cannot disagree, and a template can never pair one state's label
  with another state's colour. This covers attributes nobody sees on screen: the
  raw code rides in `data-code`, never in `title`, because `title` reaches the
  hover tooltip and the accessibility tree. `data-theme="auto"` likewise never
  reaches the DOM: neither theme selector matches it, so `applyTheme("auto")`
  *removes* the attribute. This extends past job/candidate states to anything the
  vocabulary owns: tab names come from `candidate_tab_view`, metadata provenance
  from `metadata_source_view`, a work's stage from `work_stage_view`, an audit
  verb from `review_action_view`, who performed it from `actor_view`, and an
  attachment's kind from `attachment_kind_view`. Page copy that is not a state —
  a heading's subtitle, an empty state's two lines, a disabled button's reason —
  stays with the page.
- **`/activity`, `/candidates` and `/works/{id}` are the reference
  implementations for a domain page.** One server-computed snapshot
  (`queue_snapshot` in `app/api/activity.py`, `_render_candidates` in
  `app/main.py`, `work_snapshot` in `app/api/works.py`) feeding both the
  JSON endpoint and the template, one template for its tabs, macros for
  repeated markup, a `data-field` contract with its script, and real forms
  underneath so the page works with JavaScript off. Read them before writing the
  next domain — `/candidates` in particular for how a page keeps its whole state
  in the query string, `/works/{id}` for the smallest complete page-and-endpoint
  pair, with a test asserting the two cannot disagree.
- **`/works/{id}` is the one detail page for a work at every stage, and it added
  no write routes.** Actions still POST to `/candidates/{id}/…`; R6 changed only
  where they redirect. A second approve path is the thing to avoid, so do not add
  `/works/{id}/approve`. `/candidates/{id}` 307s to the work page and keeps its
  route *function* name `candidate_detail`, because orphaned templates still call
  `url_for('candidate_detail')`.
- **A work's stage comes from facts, not from the status column.** `work_stage`
  checks for a packaged CBZ artifact, then whether the review flow can still act,
  then falls through to 下载期. Packaging does not change the candidate's status,
  so the artifact is the only honest evidence of 「已入库」 — and a packaged work
  with a new job in flight stays 入库期.
- **Any redirect target a page hands the server is an open-redirect surface.**
  The job-action forms carry a hidden `return_to` so an action taken on
  `/works/{id}` comes back there; `local_return_to` in `app/main.py` accepts only
  a rooted path — no scheme, no `//host` (browsers treat a protocol-relative
  target as another origin), no backslash (some parsers normalise it to one), no
  control characters — and a refused target falls back to a known-good redirect
  while the action still runs.
- **A timeline node per job, never one per transition.** `download_jobs` keeps no
  transition history, so a node holds the job's *current* state plus its own
  retry/pause/resume/cancel. Inventing nodes for states a job passed through
  would be a timeline claiming to know more than the database does.
- **`review_actions.operator_name` holds a login or one of two reserved names,
  and the actor is derived from it.** `AUTO_OPERATOR` / `SYSTEM_OPERATOR` live in
  `app/review/models.py`; `actor_kind` resolves them to 自动规则 / 系统 and
  everything else to 操作员. Deriving rather than storing is what gives rows
  written before this vocabulary existed the right actor.
- **A route with a typed path parameter must be declared below every literal
  sibling.** Starlette matches in declaration order and `/candidates/{candidate_id}`
  types its parameter as `int`, so a tab path declared after it is answered by the
  detail route and refused as an unparsable id. The six tab paths are above it.
- **A page's whole state lives in the query string, read in one place.**
  `_render_candidates` reads search / sort / view / facets / page off
  `request.query_params` rather than declaring eight parameters on each of six
  routes, and `_query_href` merges one parameter into the current URL so no call
  site re-lists the others and none can drop `search`. That is also what makes a
  filtered list a link an operator can send themselves.
- **An unknown facet name raises; it is never ignored.** A silently dropped
  filter shows more rows than the operator asked for and looks like the filter
  working. `CANDIDATE_FACETS` in `app/db/database.py` is the whitelist, and
  `MAX_FACET_VALUES = 8` keeps a hand-edited or looping URL from building fifty
  `EXISTS` subqueries.
- **A batch is idempotent because it acts one candidate at a time.**
  `approve_and_enqueue` is all-or-nothing over the ids it gets, so
  `apply_review_batch` hands it one per call: each is still validated and routed
  atomically, but a selection containing one already-approved item no longer
  refuses the other forty-nine. A replay approves only what is still pending and
  reports the rest under `skipped`. The orchestrator writes one `review_actions`
  row per candidate it actually acts on, so a skip leaves no audit trace of an
  action that did not happen.
- **A metadata write sends only what changed.** Each field and lock in the
  drawer carries `data-original` and `changes()` diffs against it. A PATCH of all
  twenty-one fields would re-stamp every one as operator-edited and quietly
  outrank the next scrape on all of them.
- **`activity.js` patches fields; it never builds a row.** Row markup
  constructed in JS is a second copy of the `job_row` macro and the two drift. A
  poll that finds an unknown job shows a change notice instead.
- **A row note is a second badge, not a state.** `NOTE_SEEDING` says「正在做种」
  beside a `COMPLETED` torrent whose payload the client is still sharing.
  Inventing a `SEEDING` state would have changed grouping, history and the JSON
  contract to say one extra thing about one provider.
- **When two callers share a coroutine, the argument checking goes inside it.**
  `apply_job_batch` serves the JSON endpoint and the no-JS form; while the checks
  lived in the callers, one of them had none and a `priority` batch with no
  number returned a 500.
- **`PROVIDER_CONVERSION` is deliberately absent from `SUPPORTED_PROVIDERS`.** A
  packaging job shares the `download_jobs` table but the download worker must
  never claim one, and the conversion worker claims nothing else. The two queues
  stay separate in the view and in the API.
- **The theme/density script in `<head>` is inline and blocking on purpose.**
  Deferring it flashes the light theme for one frame on every navigation.
- **`/settings/{section}` is one page with seven bodies, and a section code is
  vocabulary.** `SETTINGS_SECTION_STATUS` in `app/api/status.py` names each tab,
  its URL segment, its nav entry and its JSON payload in one place; every section
  is `neutral`, because a section is a place, not a state. `settings_section_view`
  is the one resolver there that *raises* on an unknown code rather than falling
  back: the others describe stored history, where a row written by an older
  version must still render, but a section code arrives in a URL an operator
  typed, so `/settings/nonsense` must 404 rather than render a page titled with
  the typo.
- **A settings page and its endpoint read one builder.** `settings_snapshot` in
  `app/api/settings.py` dispatches through `_SECTION_BUILDERS`; the render and
  `GET /api/v1/settings/{section}` call the same function, and a test asserts the
  page context is a superset of the JSON body for all seven — the page adds
  `csrf_token`, `error`, `notice` and nothing else. There is deliberately no
  `PUT`: every write is a form POST with its own validation and refusal message,
  and a JSON writer would be a second gate per section.
- **The retired settings paths 307; they do not 404.** `/connections`,
  `/sources`, `/auto-approval-rules`, `/archive-settings` and `/change-password`
  redirect to the tab that replaced them, and each keeps its route *function*
  name because `url_for` still calls several of them. 307 rather than 301 so a
  browser does not cache a tab layout that can still change. `settings_redirect`
  is the shared helper for saves and hardcodes 303, which is why the retirements
  build their 307 inline.
- **The archive layout template is validated as a path, because it is one.**
  `validate_library_template` refuses an absolute template, a `..` anywhere, an
  unknown placeholder and a template with no `{title}`; `render_library_path`
  sanitises each rendered value, collapses slashes inside a value so a volume
  name cannot invent a directory level, truncates each segment and falls back to
  未分类 / 未知作者 / `candidate-{id}`. The operator's template and an
  ExHentai artist name are both untrusted input to a path join. Preview is a
  convenience and never the gate — `save_library_template` validates again —
  while `_library_target` *falls back* on a stored template that no longer
  validates, because by then the book is downloaded and refusing to publish it
  over a settings mistake is the worse outcome.
- **An empty settings submission restores the default.** For the layout template
  that is the difference between 「恢复默认」 and putting every book in the
  library root; the three system preferences behave the same way, which is what
  makes the hint on the form true. Do not "fix" a handler to store the empty
  value.
- **A rule trial run reads and only reads.** `AutomaticApprovalService.dry_run`
  takes a condition rather than a stored rule — the point is to find out before
  saving — scans a bounded window of recent candidates in every status, and
  reports a count plus up to five titles. `evaluate_rule` is pure and every
  database call on that path is a read; a test asserts the candidate's status,
  `review_actions` and `auto_approval_rules` are all untouched afterwards.
- **`settings.js` previews; `validate_rule_ast` decides.** The browser renders
  the DSL and compiles the regex for immediate feedback, and the server compiles
  every pattern again at save time. A disagreement between the two can cost one
  refused save and never an unchecked one.
- **Live settings go through a provider callable; the timezone is the one
  exception.** The conversion service reads the layout template per job,
  `TelegraphService` takes a `concurrency_provider`, `/api/v1/meta` reads the
  cadence per request. `shell_context` runs for every page and is synchronous, so
  it reads `app.state.display_timezone`, which `refresh_display_timezone()`
  re-caches at startup and after the system form saves — the only two moments it
  can change. The idle cadence is *derived* from the active one, so no operator
  can make a background tab poll faster than a foreground one.
- **Timestamps are localised in the browser, and `datetime` is never rewritten.**
  `ui.js` reads the zone from `<meta name="display-timezone">` and replaces each
  `<time>` element's text through `Intl.DateTimeFormat`; the attribute keeps the
  machine-readable UTC value, which the server also renders as the text, so a
  browser with JavaScript off shows a complete timestamp. Validation of a zone is
  by shape because a slim container may ship no tz database, while the browser
  always has the full list.
- **Theme and density are not stored on the server.** They answer how one screen
  looks, not how the deployment runs; a stored theme follows an operator onto a
  screen where it is wrong. The 系统 tab points at the topbar controls rather
  than rendering a second pair that could disagree.

## Business invariants

Several are locked by tests. Do not "simplify" them:

- Nothing downloads before review; only `APPROVED`/`DOWNLOADED` may enqueue.
- ExHentai is the only metadata authority.
- ExHentai Archive Download is never routed automatically (it spends GP).
- A stalled torrent is not a failure — `WAITING_TORRENT` reports the stall,
  files the row under 需干预, keeps every action it had, and waits for an
  operator decision.
- Packaging is explicit; `auto_pack_after_download` and `torrent_auto_pack`
  default off.
- Credentials: never plaintext, never echoed to a page, never logged.
- Security gates (path traversal, decompression bombs, SSRF, image magic
  numbers) must never be loosened. The thumbnail proxy is inside this rule: it
  reuses the telegraph SSRF guard, gates on `looks_like_image`, bounds decoded
  pixel count, and re-encodes everything it serves to WebP so the outbound bytes
  are ours.
- Retries reuse the same job row and increment `attempt_count`. A replayed bulk
  action changes nothing the first one did: `apply_job_batch` reports a job that
  cannot take the action under `skipped`, with its reason, and runs the rest.
- A packaging job is not a download job: `PROVIDER_CONVERSION` stays out of
  `SUPPORTED_PROVIDERS`, and the two queues stay separate in view and API.