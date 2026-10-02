"""Put the pre-R50 ingestion behaviour back for tests that are not about policy.

R50 made 「only a message with an ExHentai/e-hentai gallery link becomes a
candidate」 the shipped default. Tests about source rules, metadata
re-evaluation, the download pipeline or the connection manager predate that
policy: they seed archive and photo messages with no link and assert on what
happens *after* admission. This helper stores a scheme that accepts every
message the parser can produce, so those tests keep testing the thing they were
written to test.

Tests *about* the gate do not call it, or store their own scheme -- which is
exactly how 「the default rejects a photo-only message」 is asserted.
"""

from __future__ import annotations

import json

from app.candidates.parse_rules import ARCHIVE_FORMATS, PARSE_RULES_KEY
from app.db.database import Database


async def permit_all_message_types(database: Database) -> None:
    await database.save_system_settings(
        {
            PARSE_RULES_KEY: json.dumps(
                {
                    "require_gallery_link": False,
                    "accept_photo": True,
                    "accept_archive": True,
                    "archive_formats": list(ARCHIVE_FORMATS),
                    "accept_preview": True,
                    "title_required": False,
                },
                separators=(",", ":"),
            )
        }
    )


__all__ = ["permit_all_message_types"]
