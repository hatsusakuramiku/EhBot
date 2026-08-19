from __future__ import annotations

from getpass import getpass
from pathlib import Path
import secrets

from pwdlib import PasswordHash


def write_private_file(path: Path, value: str) -> None:
    path.write_text(value, encoding="utf-8")
    path.chmod(0o600)


def main() -> None:
    output_dir = Path("secrets")
    output_dir.mkdir(mode=0o700, exist_ok=True)

    password = getpass("管理员密码: ")
    confirmation = getpass("再次输入管理员密码: ")
    if password != confirmation:
        raise SystemExit("两次输入的密码不一致")
    if len(password) < 12:
        raise SystemExit("管理员密码至少需要 12 个字符")

    write_private_file(output_dir / "app_secret_key", secrets.token_urlsafe(48))
    write_private_file(
        output_dir / "admin_password_hash",
        PasswordHash.recommended().hash(password),
    )
    print("秘密文件已写入 secrets 目录；其内容不会显示。")


if __name__ == "__main__":
    main()
