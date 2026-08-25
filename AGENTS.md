# EhBot — Agent Notes

Telegram-sourced manga review and CBZ archiving service. Single container,
SQLite in WAL mode, one administrator, FastAPI + Jinja2. Pipeline:
message -> candidate -> review -> download -> pack -> library.

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

Full suite is ~110-150 s. There is **no `pytest-timeout` plugin**, and a PTY
swallows pytest's summary line, so run it as a job and read the JUnit XML:

```powershell
$job = Start-Job -ScriptBlock { Set-Location 'E:\Github\EhBot'
  & .\.venv\Scripts\python.exe -m pytest --no-header -q --junitxml="$env:TEMP\pt.xml" 2>&1 | Select-Object -Last 20 }
if (Wait-Job $job -Timeout 500) { Receive-Job $job } else { "TIMEOUT"; Stop-Job $job }
Remove-Job $job -Force
$s = ([xml](Get-Content "$env:TEMP\pt.xml")).testsuites.testsuite
"tests={0} failures={1} errors={2}" -f $s.tests, $s.failures, $s.errors
```

**Baseline: 524 passed / 0 failed / 0 skipped.** Ending below this is a
regression. (An older doc claimed "427 passed / 12 skipped" — that was wrong.)

**Do not stream an SSE endpoint through `TestClient`.** It drains the response
body when the context exits and the stream is endless, so it deadlocks. Call the
endpoint function directly and pull frames off `response.body_iterator` with
`anext()`, then `aclose()`.

**Do not verify routing by inspecting `app.routes`.** This FastAPI version defers
`include_router`, showing one `_IncludedRouter` with `path=None`. Issue a real
request instead.

## Architecture rules

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
- **Migrations are append-only.** The existing eleven are frozen; add `012_*`
  onward.

## Business invariants

Several are locked by tests. Do not "simplify" them:

- Nothing downloads before review; only `APPROVED`/`DOWNLOADED` may enqueue.
- ExHentai is the only metadata authority.
- ExHentai Archive Download is never routed automatically (it spends GP).
- A stalled torrent is not a failure — `WAITING_TORRENT` reports the stall and
  waits for an operator decision.
- Packaging is explicit; `auto_pack_after_download` and `torrent_auto_pack`
  default off.
- Credentials: never plaintext, never echoed to a page, never logged.
- Security gates (path traversal, decompression bombs, SSRF, image magic
  numbers) must never be loosened.
- Retries reuse the same job row and increment `attempt_count`.