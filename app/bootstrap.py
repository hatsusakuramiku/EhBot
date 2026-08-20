from pathlib import Path

from app.private_files import write_private_text


BOOTSTRAP_PASSWORD_FILE = "bootstrap_admin_password"


def write_bootstrap_password(data_path: Path, password: str) -> Path:
    destination = data_path / BOOTSTRAP_PASSWORD_FILE
    write_private_text(destination, password)
    return destination


def remove_bootstrap_password(data_path: Path) -> None:
    (data_path / BOOTSTRAP_PASSWORD_FILE).unlink(missing_ok=True)


def format_bootstrap_banner(password: str, password_file: Path) -> str:
    """Render the first-run credentials as an unmissable console banner.

    The password is printed, not just its file path. On a container the data
    directory is usually a bind mount or a volume, so telling the operator to
    go and open a file is useless: `docker compose logs` is the one place they
    are already looking. The value is single-use, the account is forced to
    change it at the next login, and it is only ever shown on the very first
    boot, so printing it does not weaken the steady-state posture.
    """
    lines = (
        "",
        "=" * 68,
        "  EhBot 首次启动：管理员初始密码",
        "=" * 68,
        "  用户名：admin",
        f"  密码：{password}",
        "",
        "  首次登录后必须修改密码；修改后本密码即失效。",
        f"  备份：{password_file}",
        "=" * 68,
        "",
    )
    return "\n".join(lines)
