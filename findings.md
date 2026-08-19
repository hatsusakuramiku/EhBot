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
