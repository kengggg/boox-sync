#!/usr/bin/env python3
"""Fetch a pinned Google archive and install only ADB and its original notices."""

import argparse
import hashlib
import json
import shutil
import tempfile
import urllib.request
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FILES = ("adb", "NOTICE.txt", "source.properties")


def install(archive: Path, output: Path, expected_sha256: str) -> None:
    """Verify before extracting; never overwrite tools or extract arbitrary paths."""
    if output.exists():
        raise ValueError(f"Output already exists: {output}")
    with archive.open("rb") as handle:
        actual = hashlib.file_digest(handle, "sha256").hexdigest()
    if actual != expected_sha256:
        raise ValueError("Platform Tools SHA-256 mismatch")
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=output.parent) as temporary:
        stage = Path(temporary) / "platform-tools"
        stage.mkdir()
        with zipfile.ZipFile(archive) as package:
            for name in FILES:
                member = package.getinfo(f"platform-tools/{name}")
                if member.file_size > 64 * 1024 * 1024:
                    raise ValueError(f"Unexpectedly large Platform Tools file: {name}")
                (stage / name).write_bytes(package.read(member))
        (stage / "adb").chmod(0o755)
        stage.rename(output)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=ROOT / ".tools/platform-tools")
    parser.add_argument(
        "--archive", type=Path, help="Verify a previously downloaded ZIP"
    )
    args = parser.parse_args()
    manifest = json.loads((ROOT / "scripts/platform-tools.json").read_text())
    with tempfile.TemporaryDirectory(prefix="boox-platform-tools-") as temporary:
        archive = args.archive or Path(temporary) / "platform-tools.zip"
        if args.archive is None:
            with urllib.request.urlopen(manifest["url"], timeout=60) as response:
                with archive.open("wb") as output:
                    shutil.copyfileobj(response, output)
        install(archive, args.output.resolve(), manifest["sha256"])
    print(f"Verified Platform Tools {manifest['version']}: {args.output}")


if __name__ == "__main__":
    main()
