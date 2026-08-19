from pathlib import Path
from tempfile import NamedTemporaryFile


def ensure_writable_directory(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    with NamedTemporaryFile(dir=path, prefix=".ehbot-ready-", delete=True):
        pass
