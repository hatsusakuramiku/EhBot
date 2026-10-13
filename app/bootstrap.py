from pathlib import Path

from app.private_files import write_private_text


BOOTSTRAP_PASSWORD_FILE = "bootstrap_admin_password"


def write_bootstrap_password(data_path: Path, password: str) -> Path:
    destination = data_path / BOOTSTRAP_PASSWORD_FILE
    write_private_text(destination, password)
    return destination


def remove_bootstrap_password(data_path: Path) -> None:
    (data_path / BOOTSTRAP_PASSWORD_FILE).unlink(missing_ok=True)


def format_bootstrap_banner(password_file: Path, username: str = "admin") -> str:
    """Show the administrator name and private password file, without its secret."""
    lines = (
        "",
        "=" * 68,
        "  EhBot 首次启动：管理员初始密码",
        "=" * 68,
        f"  用户名：{username}",
        "",
        "  首次登录后必须修改密码；修改后本密码即失效。",
        f"  请从私有文件读取密码：{password_file}",
        "=" * 68,
        "",
    )
    return "\n".join(lines)
