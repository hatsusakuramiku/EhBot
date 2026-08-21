"""Create the runtime directories a local checkout needs.

This script used to generate `secrets/app_secret_key` as well. It no longer
does: the session signing key is generated and persisted by the application on
first start (see `app/session_secret.py`), because requiring a hand-created
random file bought nothing -- the value is random either way -- and turned a
missing file into an obstacle between an operator and a running service.

Set `APP_SECRET_KEY` yourself only when several replicas must share one key.
"""

from __future__ import annotations

from pathlib import Path


RUNTIME_DIRECTORIES: tuple[str, ...] = ("data", "library", "work")


def create_runtime_directories(root: Path = Path(".")) -> tuple[Path, ...]:
    created: list[Path] = []
    for name in RUNTIME_DIRECTORIES:
        directory = root / name
        directory.mkdir(parents=True, exist_ok=True)
        created.append(directory)
    return tuple(created)


def main() -> None:
    create_runtime_directories()
    print(
        "\u8fd0\u884c\u76ee\u5f55\u5df2\u5c31\u7eea\u3002"
        "\u4f1a\u8bdd\u5bc6\u94a5\u4e0e\u7ba1\u7406\u5458\u521d\u59cb"
        "\u5bc6\u7801\u5747\u5728\u9996\u6b21\u542f\u52a8\u65f6"
        "\u81ea\u52a8\u751f\u6210\u3002"
    )


if __name__ == "__main__":
    main()
