"""Regenerate the committed RAR fixtures used by the archive tests.

The RAR format cannot be written by 7-Zip (only read), so the fixtures under
`tests/fixtures/rar/` are produced once on a developer machine with a real
`rar` binary and then committed. The binaries stay out of the repository and
the image: this script only drives an operator-supplied `rar`.

    python -m scripts.make_rar_fixtures --rar /path/to/rar

It exits 0 without touching anything when no `rar` is available, so it can sit
in a maintenance checklist without breaking machines that do not have one.
"""

from __future__ import annotations

import argparse
import os
import random
import shutil
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


JPEG_HEADER = b"\xff\xd8\xff\xe0"
PASSWORD = "S3cret"


def _pages(directory: Path, *, zeros: int) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    # A seeded PRNG keeps the page bytes reproducible. The archives themselves
    # still differ between runs because rar stamps them with the current time.
    noise = random.Random(20261001)
    for index in (1, 2, 3):
        body = b"\x00" * zeros if zeros else noise.randbytes(2048)
        (directory / f"{index:02d}.jpg").write_bytes(JPEG_HEADER + body)


def _run(rar: str, target: Path, *arguments: str, cwd: Path) -> None:
    completed = subprocess.run(
        [rar, "a", "-idq", "-ep1", "-y", *arguments, str(target), "*.jpg"],
        cwd=str(cwd),
        capture_output=True,
    )
    if completed.returncode != 0:
        detail = (completed.stdout + completed.stderr).decode(
            "utf-8", errors="replace"
        ).strip()
        raise SystemExit(f"{target.name}: rar exited {completed.returncode}\n{detail}")


def _clear(directory: Path) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    for path in directory.iterdir():
        if path.is_file():
            path.unlink()


def main() -> int:
    parser = argparse.ArgumentParser(description="Regenerate the RAR fixtures.")
    parser.add_argument(
        "--rar",
        default=os.getenv("EHBOT_RAR") or shutil.which("rar"),
        help="Path to the rar binary (default: $EHBOT_RAR, then PATH).",
    )
    parser.add_argument(
        "--output",
        default="tests/fixtures/rar",
        help="Directory that receives the fixtures.",
    )
    parser.add_argument(
        "--work",
        default=None,
        help="Scratch directory (default: a temporary directory).",
    )
    arguments = parser.parse_args()

    if not arguments.rar:
        print("skipped: no rar binary (set --rar or $EHBOT_RAR)")
        return 0

    output = Path(arguments.output).resolve()
    work = Path(arguments.work).resolve() if arguments.work else Path(
        os.getenv("TMPDIR", "/tmp")
    ) / "ehbot-rar-fixtures"
    shutil.rmtree(work, ignore_errors=True)
    plain = work / "plain"
    squish = work / "squish"
    _pages(plain, zeros=0)
    _pages(squish, zeros=65536)
    _clear(output)

    def make(name: str, *switches: str, cwd: Path = plain) -> None:
        _run(arguments.rar, output / name, *switches, cwd=cwd)
        print(f"wrote {name}")

    make("rar5-plain.rar", "-ma5")
    make("rar5-password.rar", "-ma5", f"-p{PASSWORD}")
    make("rar5-hp.rar", "-ma5", f"-hp{PASSWORD}")
    make("rar3-plain.rar", "-ma4")
    make("rar3-hp.rar", "-ma4", f"-hp{PASSWORD}")
    make("rar5-vol.rar", "-ma5", "-v3k")
    make("rar3-old.rar", "-ma4", "-v3k", "-vn")
    make("rar5-solid.rar", "-ma5", "-s", cwd=squish)
    shutil.rmtree(work, ignore_errors=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
