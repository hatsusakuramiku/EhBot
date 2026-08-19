from pathlib import Path

import pytest

from scripts.create_secrets import create_secret_files


def test_secret_creation_refuses_to_replace_existing_credentials(
    tmp_path: Path,
) -> None:
    output_dir = tmp_path / "secrets"
    output_dir.mkdir()
    existing_file = output_dir / "app_secret_key"
    existing_file.write_text("keep-this-value", encoding="utf-8")

    with pytest.raises(FileExistsError):
        create_secret_files(output_dir)

    assert existing_file.read_text(encoding="utf-8") == "keep-this-value"
