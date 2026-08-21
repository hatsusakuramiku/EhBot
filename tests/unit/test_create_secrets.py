from pathlib import Path

from scripts.create_secrets import RUNTIME_DIRECTORIES, create_runtime_directories


def test_runtime_directories_are_created(tmp_path: Path) -> None:
    created = create_runtime_directories(tmp_path)

    assert {path.name for path in created} == set(RUNTIME_DIRECTORIES)
    assert all(path.is_dir() for path in created)


def test_running_twice_leaves_existing_content_alone(tmp_path: Path) -> None:
    """The script is setup, not a reset; it must never clear a live data directory."""
    existing = tmp_path / "data" / "ehbot.db"
    existing.parent.mkdir(parents=True)
    existing.write_text("database", encoding="utf-8")

    create_runtime_directories(tmp_path)
    create_runtime_directories(tmp_path)

    assert existing.read_text(encoding="utf-8") == "database"
