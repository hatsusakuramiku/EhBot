# EhBot — Agent Notes

Telegram-sourced manga review and CBZ archiving service. Single container,
SQLite in WAL mode, one administrator, FastAPI + Jinja2. Pipeline:
message -> candidate -> review -> download -> pack.

**Scope ends at the archive.** Download, convert to the target archive format,
plus a few operator-convenience management items. Detailed book/library
management belongs to downstream tools.

Where that line now sits: the library domain was deleted on 2026-08-26 and partly
restored on 2026-08-28 as `/downloaded`, which is `EHBot.md` §1.3.1 and nothing
more — list what has been downloaded, batch repack / remove / redownload, rename
or relocate one book. It is an operator console, not a library: there is no
`library_items` table (the domain reads `download_jobs` + `artifacts`), no reader,
no shelves or collections, no import scan of files this service did not download,
and no cover extracted from a CBZ's first page. A work's detail page is still only
`/works/{id}`. Do not grow it past that list.

**Every document named here lives in `AgentHelp/`, beside this file.** The root
holds only `README.md` (operator-facing: what this is and how to deploy it) and a
pointer stub at `AGENTS.md` that the agent convention requires to sit there. A
bare filename in these documents means `AgentHelp/<name>` unless it is `README.md`
or a path under `app/` or `tests/`.

Read `progress.md` bottom-up for current state; it ends with a handoff section
naming the next phase. `EHBot.md` is the requirements spec, `DEVELOPMENT_PLAN.md`
the phased plan, `COMPETITIVE_ANALYSIS.md` the research the UI refactor is
based on. The five `*_PROPOSAL.md` files are per-feature design records, and
`findings.md` / `task_plan.md` hold research notes and the phase ledger.

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

**Baseline: 1068 collected, 0 failed.** Ending below this is a regression.
**Compare `collected`, not `passed`:** the twelve `test_seven_zip_real.py`
cases skip or run depending on whether the host has a real toolchain in
`data/tools/7zip/`, so `passed` is 1068 on a machine that has one and 1056 with
twelve skips on a machine that does not. (An older note gave 927 for the second
case, which was simply wrong. Baseline moves per phase:
R0 439 -> R1 524 -> R2 569 -> R3 592 -> R4 635 -> R5 663 -> R6 708 -> R8 809 ->
R9 820 -> Telegram user account 866 -> R10 939 -> R11 985 -> R12 1018 -> R13 1029
-> R14 1039 -> R15 1068. There is no R7 — that number was the library domain,
deleted on 2026-08-26; its narrow replacement is R10.)

**The suite takes ~19 minutes on a Linux host, not the 150-320 s above.** Almost
all of it is argon2: `PasswordHash.recommended()` costs ~0.7 s per hash and
~0.85 s per verify, and every one of the 179 `create_app(` sites logs in. Run a
single file while iterating; the full suite is a once-per-session cost.

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

**Where code goes.** `app/main.py` (120 lines) builds the app, mounts static
files and includes routers — nothing else belongs in it. `app/wiring.py` owns the
lifespan and constructs every service in dependency order. A page route lives in
`app/web/routes/<domain>.py`, a JSON route in `app/api/`. The two layers have
separate dependency modules on purpose: `app/api/deps.py` raises a 503 `ApiError`
and never redirects, `app/web/deps.py` raises `HTTPException` or returns a 303 to
`/login`. A few accessor names are duplicated between them, and the one
difference — what an unauthenticated caller gets back — is the reason.

**Router include order is load-bearing.** Starlette matches in declaration order,
so a router carrying a typed path parameter (`/settings/{section}`,
`/works/{work_id}`) has to be included after every literal sibling, and inside a
module a typed route has to be declared below its literal ones. `app/wiring.py`
orders the includes; a route that suddenly 404s or lands on the wrong handler is
this.

`/ui-kit` renders every shared component in its real states from fixtures in
`app/web/routes/ui_kit.py` — no database access, so it works on a fresh install.
Open it after touching `ui.css` or `components/ui.html`; it is where a theme,
density or contrast regression shows up first. It is behind the session and is
deliberately absent from `NAV_ITEMS` (a developer tool does not belong in
operator navigation).

- **The Bot API's 20 MB ceiling is a protocol limit, and the MTProto path is
  the answer to it.** `app/connections/telegram_user.py` logs a user account in
  and downloads by `(chat_id, message_id)` — never by `file_id`, because MTProto
  file references are per-account and an id the bot minted cannot be resolved
  there. `PROVIDER_TELEGRAM_USER` is its own provider rather than a mode on
  `TELEGRAM`: the credential, the ceiling, the failure vocabulary and what an
  operator can do about a failure all differ, and one provider with a hidden mode
  would report「Telegram 原档」for a job whose real problem is an expired session.
  A file under 20 MB still goes to the bot — it is already receiving the message
  and needs no second credential. The session string is a full account
  credential: `data/private/telegram_user_session` only, never in a page, a
  payload or a log, and a test asserts it.
- **State vocabulary lives only in `app/api/status.py`**; DTO-to-JSON conversion
  only in `app/api/serializers.py`. Payloads carry resolved `label`/`tone`/`live`
  beside the raw `code` so the browser never translates an enum. A label written
  in a template or in JS is a second source of truth and a bug.
- **`ReviewOrchestrator` (`app/review/orchestration.py`) is the only path that
  may approve a candidate.** Reach it via `deps.review_orchestrator(request)`.
  Never re-implement approve-then-enqueue.
- **`AutoApprovalSweeper` owns the automatic-approval schedule, not a page.**
  Rules used to fire only from `_render_candidates`, which made approval a side
  effect of somebody opening 待审核: an unattended deployment approved nothing,
  and only the visible page was swept. The sweeper (a lifespan task, plus an
  on-ingest callback on `ConnectionManager`) is the unattended path; the page's
  call is a latency optimisation and must stay optional. It re-reads its interval
  every pass, so 「保存即生效」 holds without a restart — do not capture the
  setting at construction.
- **Which statuses an action may act on comes from `statuses_allowing(action)`
  (`app/review/models.py`).** `REVIEWABLE_STATUSES` covers approve / reject /
  needs-revision; `REQUEUEABLE_STATUSES` adds `FAILED`, because a failed download
  has to have a way back to a human without being approvable outright. There was
  a second copy of the reviewable set inside `Database`, and the two states it
  disagreed about were a dead end on the work page — one function, read by the
  page, the JSON layer and the database guard, is what prevents that. A state the
  page offers and the write refuses is a button that can only fail.
- **`_enqueue` revives a `FAILED` / `CANCELLED` job row; it must never touch a
  `COMPLETED` one.** `idempotency_key` is UNIQUE per source, so one book keeps
  one attempt history rather than splitting across rows. Re-fetching a finished
  book is `redownload_work`'s explicit decision (it bumps `attempt_count`), and
  re-pending a row the worker holds would hand the same transfer out twice.
- **A path template resolves `{title}` through the 标题来源 setting, and
  `{japanese_title}` / `{english_title}` never fall back across languages.** An
  ExHentai English title routinely carries `:` and `/`, which is why Japanese is
  the default. `{title}` does fall back, or the setting would become a way to
  lose a book's name; the two explicit placeholders must not, or a template that
  named a language would publish under one it did not ask for with nothing on
  screen saying so.
- **JSON routes return 401, never a redirect** — `fetch` follows a redirect
  silently and hands back a login page as 200. JSON CSRF is header-only
  (`X-CSRF-Token`).
- **Status `tone` is semantic** (`waiting`, `danger`, ...), never a colour. CSS
  decides appearance; a theme change must not touch Python.
- **The event bus drops rather than blocks**, and events carry ids only. A
  browser that stops reading must never stall the download worker; the client
  re-reads authoritative state over REST.
- **Migrations are append-only.** The existing fifteen are frozen; add `016_*`
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
- **`app/web/static/ui.css` is the only stylesheet.** `app.css` was deleted in
  R9 together with the three pre-refactor pages that rendered against it, and
  `data-legacy` no longer exists — there is no such thing as a legacy page now. A
  test scans every page's `<link rel="stylesheet">` set, so a second stylesheet
  fails the suite rather than drifting.
- **Every rule in `ui.css` is class-scoped, and stays that way.** The original
  reason (a frozen `app.css` hardcoding light surfaces) is gone, but the property
  it bought is not: a bare `body`, `p` or `a` rule reaches `/ui-kit`, every email-
  shaped fragment HTMX swaps in, and any page added later, so a contrast
  regression from one unscoped selector lands everywhere at once and shows up
  nowhere in particular.
- **`NAV_ITEMS` in `app/web/routes/shell.py` is the only navigation source.**
  The sidebar, the phone tab bar and its drawer all render from it. Three
  renderings of one list is not three lists — the previous hand-written pair had
  already drifted by one link. A test compares the rendered destination sets.
- **An ARIA state goes only on an element it is defined for.**
  `aria-pressed` is a `button` state and `aria-selected` belongs to `tab` /
  `option` / `row` / `gridcell`, so both announce nothing on an `<a>` — the tab
  strip, the two view switches and the log-level filters each carried one and
  said it to no one. A link that is the one in effect says `aria-current`.
  `aria-sort` goes on the `<th>`, never on the button inside it. A test scans the
  rendered pages for both mistakes.
- **An overlay watches `open`; it does not focus from `x-init`.** `x-init` runs
  when Alpine initialises the teleported markup — page load — so the old
  `x-init="$nextTick(() => $el.focus())"` stole focus on arrival and moved
  nothing when the dialog opened. Watch `open` on the **wrapper** (the panel is
  inside `x-show` and a watcher declared there does not exist to see the close),
  return focus to the trigger, and open a confirmation on 取消 rather than on the
  destructive button.
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
- **HTML does not nest forms, and nothing warns you.** A `<form>` start tag
  inside another form is *ignored* by the parser: the element is never created
  and its children — including its submit button and its `action` — join the
  outer form. Every row on `/activity` and `/candidates` lives inside a batch
  form, so a row action is a `<button formaction="…">`, never a form of its own.
  This shipped broken once: R9 found every per-row action on `/activity`
  submitting the batch endpoint with no action, rendering 200 the whole time.
  `tests/integration/markup.py` parses each page for it. Content inside a
  `<template>` is exempt — a template's contents are a separate fragment, which
  is what makes the teleported dialog forms legal.
- **A destructive or GP-spending action takes two steps; a cheap one takes
  one.** `ui.confirm` in `components/ui.html` is the only dialog. Because
  `x-teleport="body"` moves the dialog out of the form it was written in, the
  confirm button reconnects through HTML's `form="<id>"` attribute (`form=`),
  names its action when the form serves several (`field=`/`value=`), and can
  retarget one submission (`formaction=`). Pass a `key=` whenever the same label
  appears on more than one row, or every `aria-labelledby` on the page points at
  the first dialog's title. Do not gate retry, resume, pause or approve: a
  confirmation on every button is how an operator learns to dismiss them
  unread.
- **`/activity`, `/candidates` and `/works/{id}` are the reference
  implementations for a domain page.** One server-computed snapshot
  (`queue_snapshot` in `app/api/activity.py`, `_render_candidates` in
  `app/web/routes/candidates.py`, `work_snapshot` in `app/api/works.py`) feeding
  both the
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
  `/works/{id}` comes back there; `local_return_to` in `app/web/deps.py` accepts
  only
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
- **Repairing a path and refusing one are two functions, and which you want
  depends on who is waiting.** `safe_library_name` / `render_library_path` run
  inside a packing job for a book that is already downloaded, so they sanitise and
  never refuse — failing a job over a punctuation mark leaves the book unpublished
  for a reason nobody asked about. `strict_library_segment` /
  `check_library_segment` / `plan_library_path` run while an operator is looking at
  a form, or while a batch is about to re-file fifty books, so they refuse and name
  the offending segment and character. Do not "simplify" the strict branch into a
  call to the sanitising one: the path an operator gets would stop being the path
  they typed, with nothing on screen saying so. `check_library_segment` returns
  `(code, message)` rather than raising because a batch collects one reason per
  work.
- **An explicit archive path is refused when taken, never suffixed.**
  `unique_library_target`'s ` (2)` is right when a *template* renders two books
  onto one name — neither name was chosen, so the suffix is the least-bad answer.
  It is wrong for a path an operator typed: the book lands at a name they did not
  ask for beside one they did. `set_archive_path` checks two things, because a name
  can be taken two ways — another work's *pin* (whose file may not exist yet, so
  letting a second book pin it means the two race at pack time) and an existing
  *file* with no pin (a book packed before anything was pinned, which no row can
  find). `mkdir` runs only after every check passes, or a form being iterated on
  litters the library with empty directories.
- **`work_archive_paths.is_manual` is the same guard `metadata_values.is_manual`
  is: a template is a default, and a default must never overwrite a decision.** A
  batch repack recomputes every selected work's path from the current layout
  template, and without this the first batch after a rename would quietly undo it
  fifty books at a time. The guard sits on the *whole* upsert
  (`WHERE NOT (stored.is_manual = 1 AND excluded.is_manual = 0)`), not on the flag:
  keeping the flag while still assigning `relative_path` preserved the label on the
  operator's decision and threw the decision itself away.
- **The pin lives in `work_archive_paths`, keyed by candidate; the 014 column is
  the fallback.** `artifacts.library_relative_path` can only exist once a CBZ does,
  because an artifact row is created *by* a pack — so it cannot answer 「下次打包放
  哪」 before the first pack, and it died with the artifact, which lost the
  operator's decision on every removal. `ConversionService` reads the table first
  and falls back to the column, which is what keeps pre-015 renames working with no
  data migration. The column is still written on *every* pack, not only after a
  rename: without it a freshly packed book cannot prefill the detail page's 目录
  field, and an operator editing only the filename submits an empty directory and
  moves the book to the library root.
- **A pinned path is re-validated on read, not trusted from the write.** The write
  did validate, but the ceiling is on the whole path and the library root is a
  setting — moving the library deeper can push a path that was legal when pinned
  past what the filesystem takes. The refusal parks the job with the reason on it,
  which is the only way the operator finds out at all.
- **`CONVERSION_WAITING_PATH` is a waiting state, not a failure.** The archive is
  intact, nothing was attempted, and the remedy is an edit (shorten the title, or
  pin a path on the work detail page) followed by a requeue — exactly the shape of
  待补分卷 and 待补密码. Marking it FAILED would file an untouched book beside books
  whose packing genuinely broke, and would offer 重试 when retrying re-renders the
  same impossible path. The batch **parks a row** rather than only reporting a
  skip, because a skip is a sentence in a flash message that is gone on the next
  navigation and 「这本书为什么没打包」 has to still be answerable tomorrow. A
  RUNNING row is left alone — the worker holds it.
- **A batch re-file changes nothing about a work whose path it cannot render.**
  Old pin, old file, nothing moved; the work goes to 需干预 with the reason and the
  batch carries on. Refusing forty-nine books over one long title is what per-work
  batching exists to avoid. It also does not move the file itself — the pack
  publishes to the new path because it reads the pin, and moving first would leave
  a book at a path no artifact row names if the pack then failed.
- **`POST /works/{id}/archive-path` is the one write route on the work detail
  page, and a refusal re-renders the page rather than redirecting.** Unlike
  approve, which already had a home under `/candidates/{id}`, no existing endpoint
  sets an archive path, so routing it through one would have invented a second
  meaning for a candidate action. The 400-with-the-page is because the operator has
  a form open and needs to see which value was rejected; a redirect carrying
  `?error=` is right for a list action, which has no form to come back to.
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

- **`/downloaded` is `EHBot.md` §1.3.1 and stops there.** It reads
  `download_jobs` joined to `artifacts` — there is no registry table, because a
  second table describing the same book is a second truth. The five tabs filter on
  the CBZ artifact and the packaging job's state, **never on the candidate's
  status**: packaging does not touch that column, so it cannot answer
  「打好包了吗」. Same rule `work_stage` follows.
- **`/activity` history and `/downloaded` read the same rows and ask different
  questions.** History is ordered by *task* and answers「这次跑得怎么样」; the
  downloaded page is ordered by *work* and answers「这本书现在在哪、要不要再动
  它」. That is why one groups by job state and the other by pack state, and why
  neither is a filter of the other.
- **Deleting the records and deleting the files are two decisions, and the safe one
  is what happens by omission.** `remove_work(delete_files=False)` is the default
  because `EHBot.md` §1.2.3 says so. The page offers two buttons with two dialogs
  rather than one dialog with a checkbox: `ui.confirm` is teleported out of its
  form and can carry exactly one name/value pair, so a checkbox would have to be
  reconnected by hand and could be left in either state. The file-deleting variant
  is therefore its own action name (`remove-files`), which makes it impossible to
  reach by omission. Do not "simplify" it into a flag on one button.
- **A removal writes `removed_works`.** `list_history_jobs` has always treated a
  terminal job row as permanent, so deleting one silently would make the history
  lie about its own completeness. The audit row cannot be a column on
  `download_jobs` — that row is the thing being deleted — and it records *whether
  the files went too*, which is the question an operator asks later.
- **A removal keeps the candidate.** It is the work's identity, it holds the
  metadata, and every `review_actions` row points at it. Removing downloaded
  content means the download is gone, not that the book was never seen.
- **Every path the archived-work service touches is resolved and proved to be
  inside the root it belongs to** — the CBZ under the library, the source archive
  under the work directory, each against its own root and against the *currently
  configured* one. The stored path is not trusted even though this service wrote
  it: an old layout template, a re-pointed library directory or an ExHentai artist
  name can all leave a value outside the tree. A refused file lands in
  `failed_files`, the records still go, and the audit row's `deleted_files` stays 0
  — claiming 1 there would be the trail lying about bytes still on disk.
- **`Database.downloaded_work` (single) deliberately does not filter on
  `state = COMPLETED`; `list_downloaded_works` does.** A work being re-downloaded
  has last run's archive and a job row that is PENDING again. Filtering the single
  read too would make every caller's「已有任务在进行」guard unreachable and report
  `WORK_NOT_DOWNLOADED` — both wrong and unactionable. The state travels on the DTO
  so the caller decides.
- **Re-download resets the original job row; `retry_job` cannot serve it.**
  `retry_job` refuses a COMPLETED job on purpose — inside the queue, a finished
  download is finished. Here the operator is explicitly asking to fetch it again,
  so the same row goes back to PENDING with `attempt_count + 1`. A second row is
  not an option: `idempotency_key` is UNIQUE per source, so a different key would
  split one book's attempt history in two.
- **Re-packing must land on the file it replaces, and on the operator's path if
  they set one.** `_library_target` reads `artifacts.library_relative_path` before
  it renders the layout template, and `_existing_cbz_paths_sync` reserves this
  book's own path. Without the first, a renamed book is moved back to the
  template's location *and* `unique_library_target` grows a ` (2)` beside the
  operator's copy — one action reading as both undone and duplicated. The stored
  relative path is re-validated on the way out (absolute, or containing `..`, is
  ignored and the template renders instead): it was sanitised on the way in, but a
  path read back out of the database and joined onto a root is exactly the shape
  that must not be trusted twice.
- **`downloaded_pack_view` is derived vocabulary, so `is_live` cannot see it.** The
  pack codes are deliberately absent from `_REGISTRIES`, which means
  `is_live("packing")` is False. Anything deciding whether to poll must read
  `pack["live"]` off the resolved payload — `downloaded_snapshot` does — or the page
  freezes on 打包中 and never refreshes. The badge and the polling decision come
  from one object for that reason.
- **`downloaded_tab_view` raises on an unknown name**, like `settings_section_view`
  and unlike `candidate_tab_view`. The difference is where the value comes from: a
  candidate tab is validated by its route before it arrives, while this one reaches
  the page from a query string an operator can type, so `?tab=nonsense` must 404
  rather than render a page titled with the typo.
- **Rename is per-work and there is no batch version.** A filename belongs to one
  book. A batch rename would need a template, and the archive layout setting
  already is that template.

## Business invariants

Several are locked by tests. Do not "simplify" them:

- Nothing downloads before review; only `APPROVED`/`DOWNLOADED` may enqueue.
- ExHentai is the only metadata authority.
- ExHentai Archive Download is never routed automatically (it spends GP).
- A stalled torrent is not a failure — `WAITING_TORRENT` reports the stall,
  files the row under 需干预, keeps every action it had, and waits for an
  operator decision.
- Packaging is explicit; `auto_pack_after_download` and `torrent_auto_pack`
  default off. Re-packaging an already packaged work is allowed and requeues the
  same task row: every state but RUNNING is requeueable, and 重新打包 is why
  COMPLETED is on that list.
- Removing downloaded content never deletes a file unless the request named the
  file-deleting action, and never touches a work whose download or packaging task
  is still in flight.
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


**Logging invariants (added by R12):**

- **Credentials never appear on any output path.** `redact_sensitive_values`
  runs over the message, the exception text and the stack, because a URL
  carrying a token is as likely to surface in a traceback as in a message.
  Uvicorn's access logger must be collected into the same pipeline
  (handlers cleared, `propagate = True`); otherwise redaction is silently
  bypassed on the access log path.
- **`logging.exception(...)` is the only form allowed for unexpected errors.**
  An error without a traceback is a real-world debugging dead end -- every
  defensive worker loop in `app/` uses it for that reason. Do not "simplify"
  any of the four call sites to `.error("...")`.
- **Retention is a preference, not a precondition.** A read-only `data/`
  must not prevent startup; the file handler is dropped with a
  `LOG_FILE_UNAVAILABLE` warning and the service continues with stdout only.
- **Log level is deployment-level, not per-request.** It lives in the
  environment (`LOG_LEVEL`), not in `system_settings` -- a deployment whose
  log level lives in the database cannot raise it to debug the startup that
  failed before the database opened.
- **`configure_logging()` is idempotent.** A test session that builds
  several applications must not reset the root handlers on every call;
  the explicit setup from `app.server.main` runs once with `force=True`.
- **`request_id_var` is a `contextvars.ContextVar`.** Every existing
  `logging.getLogger(__name__)` site inside a request picks the id up
  without being edited; tasks spawned with `asyncio.create_task` inherit
  the context so an enqueued job keeps the id of the request that
  enqueued it.
- **An inbound `X-Request-ID` is honoured only when proxy headers are
  trusted.** For the same reason `X-Forwarded-For` is: a value a client can
  set is a value a client can use to forge or collide. Untrusted input is
  replaced rather than rejected.


**Added by R14 (review pass):**

- **`_CONTEXT_FIELDS` is the log contract, and the formatter drops anything
  not on it.** A field attached through `extra=` that is not listed is
  discarded silently -- which is how `error_message` went missing for two
  releases while every unit test passed, because `caplog` sees the
  `LogRecord` and not the formatted line. **Assert on
  `JsonFormatter().format(...)`, not on record attributes**, whenever the
  point of a test is that a field reaches the log.
- **Context fields are redacted like the message.** A provider's
  `error_message` routinely quotes the URL it tried, and that URL may carry
  a token. String fields go through `redact_sensitive_values` in the
  formatter's field loop; do not move redaction back to the call sites.
- **Never pass a value through `extra=` for a field that is not
  whitelisted.** Put it in the message text -- that is the path redaction
  runs on -- or add the field deliberately.
- **Security headers come from one middleware.** `app/web/security_headers.py`
  sets CSP, `X-Content-Type-Options`, `X-Frame-Options` and `Referrer-Policy`
  on every response. `script-src` must keep `'unsafe-inline'` and
  `'unsafe-eval'`: Alpine evaluates `x-data` with `new Function`, and the
  pre-paint theme bootstrap in `base.html` / `login.html` is inline. The
  enforced part is the rest -- do not loosen `connect-src`, `form-action`,
  `object-src` or `frame-ancestors`.
- **Use `Database.connection()`, never `Database._connect()`.**
  `with sqlite3.connect(...)` ends the transaction but does **not** close the
  connection; the wrapper closes in a `finally`. An unclosed handle on a WAL
  database holds its read snapshot, which stops `-wal` from being
  checkpointed.
- **The login throttle prunes on write.** `app.state.login_attempts` is
  bounded by `MAX_TRACKED_CLIENTS`, and a lockout logs `login_locked_out`.
  Behind an untrusted proxy every caller shares one bucket, and that is the
  deliberate choice: `request.client` is only trustworthy when
  `TRUST_PROXY_HEADERS` is set, and a forgeable key is worse than a coarse
  one.
