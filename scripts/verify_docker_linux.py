"""Verify the archive toolchain on real Linux using Docker.

Windows cannot exercise the managed 7-Zip install: upstream publishes no
Windows binary for the pinned assets, which are Linux `.tar.xz` builds. This
script runs that verification where it actually matters, inside a Linux
container that starts with no 7-Zip installed at all.

Usage (needs a running Docker engine):

    python scripts/verify_docker_linux.py             # toolchain checks
    python scripts/verify_docker_linux.py --offline   # + fails-closed check
    python scripts/verify_docker_linux.py --suite     # + full pytest on Linux
    python scripts/verify_docker_linux.py --build     # + application image

Each check prints `PASS` or `FAIL`; the exit code is non-zero on any failure.
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parent.parent

PYTHON_IMAGE = "python:3.12-slim-bookworm"

#: Base image with only the toolchain's own dependency preinstalled, so the
#: offline check can run with no network while still importing the module.
BASE_IMAGE = "ehbot-verify-base:latest"

APPLICATION_IMAGE = "ehbot:docker-verify"

BASE_DOCKERFILE = f"""FROM {PYTHON_IMAGE}
RUN pip install --no-cache-dir --disable-pip-version-check 'httpx>=0.28,<1'
"""

#: Proves the managed-install contract in one Linux process: no 7-Zip present
#: at the start, a digest-verified download, an idempotent second install, a
#: statically linked binary that runs on slim, real RAR support, a real 7z
#: round trip, and a tampered payload that installs nothing.
TOOLCHAIN_SCRIPT = """
import shutil, subprocess, sys
from pathlib import Path

sys.path.insert(0, "/repo")

from app.archive.backends.seven_zip import resolve_seven_zip_executable
from app.archive.toolchain import (
    SEVEN_ZIP_VERSION,
    asset_for_platform,
    install,
    install_root,
    installed_executable,
)

failures = []

def check(name, condition, detail=""):
    print(("PASS " if condition else "FAIL ") + name + ((" :: " + str(detail)) if detail else ""))
    if not condition:
        failures.append(name)

check("host has no 7-Zip on PATH", shutil.which("7zz") is None and shutil.which("7z") is None)

tools = Path("/tmp/ehbot-tools")
check("nothing is installed before the first call", installed_executable(tools) is None)

asset = asset_for_platform()
print("asset: " + asset.file_name)
print("url:   " + asset.url)

executable = install(tools)
check("install returns an existing file", executable.is_file(), executable)
check("install is version-isolated", str(executable).startswith(str(install_root(tools))), executable)
check("the static binary is preferred", executable.name == "7zzs", executable.name)

again = install(tools)
check("a second install is idempotent", again == executable)

resolved = resolve_seven_zip_executable("7zz", tools)
check("resolution prefers the managed install", resolved == str(executable), resolved)

result = subprocess.run([str(executable), "i"], capture_output=True, text=True, timeout=60)
check("the managed binary runs on slim", result.returncode == 0, result.returncode)
check("the version matches the pin", SEVEN_ZIP_VERSION in result.stdout, SEVEN_ZIP_VERSION)
check("RAR and RAR5 are supported", "Rar5" in result.stdout and "Rar" in result.stdout)

pages = Path("/tmp/pages")
pages.mkdir(parents=True, exist_ok=True)
jpeg_header = bytes([0xFF, 0xD8, 0xFF, 0xE0])
(pages / "0001.jpg").write_bytes(jpeg_header + b"0" * 256)
(pages / "0002.jpg").write_bytes(jpeg_header + b"1" * 256)
archive = Path("/tmp/book.7z")
subprocess.run([str(executable), "a", str(archive), "*"], cwd=pages, check=True, capture_output=True, timeout=120)
listing = subprocess.run([str(executable), "l", "-slt", str(archive)], capture_output=True, text=True, timeout=60)
check("a real 7z round trip lists both pages", listing.stdout.count("Path = 000") == 2, listing.returncode)
check("member names carry no host path", "/tmp/pages" not in listing.stdout)

tampered = Path("/tmp/tampered-tools")
try:
    install(tampered, download=lambda url: b"not a tar.xz payload")
except Exception as exc:
    code = getattr(exc, "code", type(exc).__name__)
    check("a tampered download is rejected", code == "TOOLCHAIN_DIGEST_MISMATCH", code)
else:
    check("a tampered download is rejected", False, "no error was raised")
check("a rejected download installs nothing", installed_executable(tampered) is None)

print("")
print("FAILURES: " + str(len(failures)))
sys.exit(1 if failures else 0)
"""

#: With no network the install must fail closed on a stable error code rather
#: than raising a bare transport error or leaving a partial install behind.
OFFLINE_SCRIPT = """
import sys
from pathlib import Path

sys.path.insert(0, "/repo")

from app.archive.toolchain import ToolchainError, install, installed_executable

tools = Path("/tmp/offline-tools")
try:
    install(tools)
except ToolchainError as exc:
    ok = exc.code == "TOOLCHAIN_DOWNLOAD_FAILED" and installed_executable(tools) is None
    print(("PASS " if ok else "FAIL ") + "the offline install fails closed :: " + exc.code)
    sys.exit(0 if ok else 1)
print("FAIL the offline install unexpectedly succeeded")
sys.exit(1)
"""

SUITE_COMMAND = (
    "set -e; cp -r /repo /tmp/checkout; cd /tmp/checkout; "
    "pip install --quiet --disable-pip-version-check uv; "
    "uv sync --frozen --all-groups; "
    "uv run --no-sync python -m pytest"
)


def _run(arguments: list[str], *, title: str) -> bool:
    print("")
    print("=" * 72)
    print(title)
    print("=" * 72)
    completed = subprocess.run(arguments, cwd=REPOSITORY_ROOT)
    ok = completed.returncode == 0
    print(("PASS " if ok else "FAIL ") + title)
    return ok


def _container(
    script: str, *, image: str = BASE_IMAGE, network: str | None = None
) -> list[str]:
    """Run a Python snippet in a container without quoting it into a shell."""
    arguments = [
        "docker",
        "run",
        "--rm",
        "--volume",
        f"{REPOSITORY_ROOT}:/repo:ro",
        "--workdir",
        "/repo",
    ]
    if network is not None:
        arguments += ["--network", network]
    arguments += [image, "python", "-c", script]
    return arguments


def _build_base_image() -> bool:
    with tempfile.TemporaryDirectory() as directory:
        dockerfile = Path(directory) / "Dockerfile"
        dockerfile.write_text(BASE_DOCKERFILE, encoding="utf-8", newline="\n")
        return _run(
            [
                "docker",
                "build",
                "--tag",
                BASE_IMAGE,
                "--file",
                str(dockerfile),
                directory,
            ],
            title="verification base image",
        )


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Verify the archive toolchain inside a Linux container."
    )
    parser.add_argument(
        "--offline",
        action="store_true",
        help="Also prove the install fails closed with no network.",
    )
    parser.add_argument(
        "--suite",
        action="store_true",
        help="Also run the full pytest suite on Linux.",
    )
    parser.add_argument(
        "--build",
        action="store_true",
        help="Also build the application image and pre-seed 7-Zip in it.",
    )
    arguments = parser.parse_args()

    if shutil.which("docker") is None:
        print("docker command not found", file=sys.stderr)
        return 2
    probe = subprocess.run(
        ["docker", "version", "--format", "{{.Server.Version}}"],
        capture_output=True,
        text=True,
    )
    if probe.returncode != 0:
        print("docker engine is not reachable:", file=sys.stderr)
        print((probe.stderr or probe.stdout).strip(), file=sys.stderr)
        print("start Docker Desktop and re-run this script", file=sys.stderr)
        return 2
    print(f"docker engine: {probe.stdout.strip()}")

    results: list[tuple[str, bool]] = []

    if not _build_base_image():
        print("cannot continue without the verification base image", file=sys.stderr)
        return 1

    title = "managed 7-Zip install on real Linux"
    results.append((title, _run(_container(TOOLCHAIN_SCRIPT), title=title)))

    if arguments.offline:
        title = "install fails closed with no network"
        results.append(
            (title, _run(_container(OFFLINE_SCRIPT, network="none"), title=title))
        )

    if arguments.suite:
        title = "full pytest suite on Linux"
        results.append(
            (
                title,
                _run(
                    [
                        "docker",
                        "run",
                        "--rm",
                        "--volume",
                        f"{REPOSITORY_ROOT}:/repo:ro",
                        PYTHON_IMAGE,
                        "bash",
                        "-lc",
                        SUITE_COMMAND,
                    ],
                    title=title,
                ),
            )
        )

    if arguments.build:
        title = "application image build"
        built = _run(
            ["docker", "build", "--tag", APPLICATION_IMAGE, "."], title=title
        )
        results.append((title, built))
        if built:
            title = "application image installs 7-Zip"
            results.append(
                (
                    title,
                    _run(
                        [
                            "docker",
                            "run",
                            "--rm",
                            APPLICATION_IMAGE,
                            "python",
                            "-m",
                            "scripts.install_seven_zip",
                            "--data-path",
                            "/tmp/data",
                        ],
                        title=title,
                    ),
                )
            )

    print("")
    print("=" * 72)
    print("SUMMARY")
    print("=" * 72)
    for title, ok in results:
        print(("PASS " if ok else "FAIL ") + title)
    failed = [title for title, ok in results if not ok]
    print("")
    print(f"{len(results) - len(failed)}/{len(results)} stages passed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
