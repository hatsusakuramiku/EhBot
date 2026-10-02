# RAR fixtures

Small RAR archives used by `tests/integration/test_seven_zip_real.py` to prove
the RAR half of the archive pipeline. They are **data, not software**: 7-Zip
cannot create RAR, but it reads these, so the suite exercises RAR without a
`rar` binary (and without shipping one — rarlab's archiver is not
redistributable, see `AgentHelp/ENCRYPTED_ARCHIVE_FALLBACK_PROPOSAL.md` §9.2).

| File | Format | Password | Purpose |
|------|--------|----------|---------|
| `rar5-plain.rar` | RAR5 | — | baseline RAR5 round trip |
| `rar5-password.rar` | RAR5, data-encrypted | `S3cret` | vault lookup after a readable listing |
| `rar5-hp.rar` | RAR5, header-encrypted | `S3cret` | vault lookup before listing |
| `rar3-plain.rar` | RAR4 (RAR3 family) | — | older readers' container |
| `rar3-hp.rar` | RAR4, header-encrypted | `S3cret` | RAR3 header encryption |
| `rar5-vol.part1..3.rar` | RAR5 | — | `.partN.rar` volume discovery |
| `rar3-old.rar/.r00/.r01` | RAR4 | — | legacy `.rNN` volume discovery from both ends |
| `rar5-solid.rar` | RAR5, solid | — | high-ratio pages pass the ratio gate |

Each archive holds three pages (`01.jpg`..`03.jpg`); the solid one uses blank
page bodies so the ratio gate is actually exercised.

Regenerate with a `rar` binary you supply yourself (nothing is downloaded):

    python -m scripts.make_rar_fixtures --rar /path/to/rar

The script skips cleanly when no `rar` is available. Archives differ between
runs (rar stamps them with the current time); the tests never depend on exact
bytes.
