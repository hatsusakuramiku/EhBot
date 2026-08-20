"""Install the pinned official 7-Zip binary into the data directory.

Intended for image builds and provisioning, so the first request does not pay
the download cost:

    python -m scripts.install_seven_zip --data-path /app/data
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.archive.toolchain import (  # noqa: E402
    SEVEN_ZIP_VERSION,
    ToolchainError,
    asset_for_platform,
    install,
)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Download and verify the official 7-Zip binary."
    )
    parser.add_argument(
        "--data-path",
        default=os.getenv("DATA_PATH", "data"),
        help="Data directory; the binary is installed under <data>/tools.",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Reinstall even when this version is already present.",
    )
    arguments = parser.parse_args()

    tools_path = Path(arguments.data_path) / "tools"
    try:
        asset = asset_for_platform()
        print(f"7-Zip {SEVEN_ZIP_VERSION}: {asset.file_name}")
        executable = install(tools_path, force=arguments.force)
    except ToolchainError as exc:
        print(f"error [{exc.code}]: {exc.public_message}", file=sys.stderr)
        return 1
    print(f"installed: {executable}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())