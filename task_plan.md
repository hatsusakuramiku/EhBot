# Task Plan: EhBot Development Plan

## Goal
Produce an implementation-ready development plan for a Docker-deployed Telegram and ExHentai comic ingestion, review, download, and CBZ conversion service.

## Current Phase
Implementation phase 1 code complete; Docker runtime verification pending

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
- [x] Add administrator login, CSRF protection, short-term login lockout, and logout
- [x] Add the Web dashboard plus liveness and readiness endpoints
- [x] Add Docker, Compose, secret-file bootstrap, and local development documentation
- [x] Pass the complete automated test suite
- [ ] Build and start the Docker image on a host with Docker installed
- **Status:** code complete; runtime container acceptance pending because Docker is not installed on this machine
