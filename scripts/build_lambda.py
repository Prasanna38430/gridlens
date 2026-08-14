#!/usr/bin/env python
"""Build a deterministic Lambda zip for linux, from any host."""

from __future__ import annotations

import argparse
import hashlib
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
STAGE = ROOT / "build" / "lambda"
ARTIFACT = ROOT / "build" / "gridlens-ingest.zip"

# lambda runs linux on x86_64. pydantic-core is a compiled wheel, so installing
# on windows would stage a .pyd that the runtime cannot load, and the failure
# arrives as an ImportError at cold start rather than at build time.
PLATFORM = "x86_64-manylinux2014"
PYTHON = "3.12"

# a fixed timestamp so the same inputs produce the same bytes. zip stores mtime
# per entry, so without this every build has a new hash and terraform redeploys
# a function that did not change.
EPOCH = (1980, 1, 1, 0, 0, 0)


def run(*args: str) -> None:
    subprocess.run(args, check=True, cwd=ROOT)


def stage_dependencies() -> None:
    requirements = ROOT / "build" / "requirements.txt"
    requirements.parent.mkdir(parents=True, exist_ok=True)

    # locked versions only, and without the project itself, which is copied in
    # as source rather than installed
    export = subprocess.run(
        [
            "uv",
            "export",
            "--no-dev",
            "--no-emit-project",
            "--no-hashes",
            "--format",
            "requirements-txt",
        ],
        check=True,
        capture_output=True,
        text=True,
        cwd=ROOT,
    ).stdout
    requirements.write_text(export, encoding="utf-8")

    run(
        "uv",
        "pip",
        "install",
        "--target",
        str(STAGE),
        "--python-platform",
        PLATFORM,
        "--python-version",
        PYTHON,
        "--only-binary",
        ":all:",
        "--no-installer-metadata",
        "--no-compile-bytecode",
        "-r",
        str(requirements),
    )


def stage_source() -> None:
    shutil.copytree(
        ROOT / "src" / "gridlens",
        STAGE / "gridlens",
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
    )


def write_zip() -> Path:
    files = sorted(p for p in STAGE.rglob("*") if p.is_file())
    ARTIFACT.parent.mkdir(parents=True, exist_ok=True)

    with zipfile.ZipFile(
        ARTIFACT, "w", zipfile.ZIP_DEFLATED, compresslevel=9
    ) as bundle:
        for path in files:
            info = zipfile.ZipInfo(
                str(path.relative_to(STAGE)).replace("\\", "/"), date_time=EPOCH
            )
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o644 << 16
            bundle.writestr(info, path.read_bytes())
    return ARTIFACT


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check-only", action="store_true")
    args = parser.parse_args()

    if STAGE.exists():
        shutil.rmtree(STAGE)
    STAGE.mkdir(parents=True)

    stage_dependencies()
    stage_source()
    artifact = write_zip()

    digest = hashlib.sha256(artifact.read_bytes()).hexdigest()
    size = artifact.stat().st_size
    print(
        f"{artifact.relative_to(ROOT)}  {size / 1_048_576:.1f} MiB"
        f"  sha256 {digest[:16]}"
    )

    if args.check_only:
        return 0

    # a package that cannot be imported is worth catching here rather than at
    # a cold start six hours from now
    linux_only = [p.name for p in STAGE.rglob("*.pyd")]
    if linux_only:
        print(
            f"windows binaries staged, this will not run: {linux_only}", file=sys.stderr
        )
        return 1
    if not (STAGE / "pydantic_core").exists():
        print("pydantic_core missing from the bundle", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
