from __future__ import annotations

from pathlib import Path
import shutil
import secrets
import tempfile

def write_private_file(path: Path, value: str) -> None:
    path.write_text(value, encoding="utf-8")
    path.chmod(0o600)


def create_secret_files(output_dir: Path) -> None:
    if output_dir.exists():
        raise FileExistsError(
            f"{output_dir} already exists; refusing to replace credentials"
        )
    output_dir.parent.mkdir(parents=True, exist_ok=True)
    temporary_dir = Path(
        tempfile.mkdtemp(prefix=f".{output_dir.name}-", dir=output_dir.parent)
    )
    try:
        temporary_dir.chmod(0o700)
        write_private_file(
            temporary_dir / "app_secret_key", secrets.token_urlsafe(48)
        )
        temporary_dir.rename(output_dir)
    except BaseException:
        shutil.rmtree(temporary_dir, ignore_errors=True)
        raise


def main() -> None:
    output_dir = Path("secrets")
    try:
        create_secret_files(output_dir)
    except FileExistsError as exc:
        raise SystemExit("secrets 目录已存在；为避免轮换凭据，已停止。") from exc
    for runtime_dir in (Path("data"), Path("library"), Path("work")):
        runtime_dir.mkdir(exist_ok=True)
    print("应用秘密及运行目录已创建；秘密内容不会显示。")


if __name__ == "__main__":
    main()
