#!/usr/bin/env python3
"""Validate version tags and assemble the exact files allowed in a draft release."""

import argparse
import hashlib
import json
import re
import shutil
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def project_version(root: Path = ROOT) -> str:
    return tomllib.loads((root / "pyproject.toml").read_text())["project"]["version"]


def check_tag(tag: str, version: str) -> None:
    if not re.fullmatch(r"v(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)", tag):
        raise ValueError("Release tags must have the form vMAJOR.MINOR.PATCH")
    if tag != f"v{version}":
        raise ValueError(f"Tag {tag} does not match project version {version}")


def prepare_assets(dist: Path, output: Path, version: str) -> None:
    """Release the tested local-preview DMG plus build metadata and checksums."""
    dmg = dist / f"BOOX-Sync-{version}-arm64-local.dmg"
    metadata = dist / "BOOX Sync.app/Contents/Resources/build-info.json"
    info = json.loads(metadata.read_text())
    if (
        info.get("version") != version
        or info.get("architecture") != "arm64"
        or info.get("signing") != "local-ad-hoc"
    ):
        raise ValueError("App metadata does not match the requested preview release")
    if not dmg.is_file():
        raise ValueError(f"Missing DMG: {dmg.name}")
    output.mkdir(parents=True, exist_ok=False)
    for path in (dmg, metadata):
        shutil.copy2(path, output / path.name)
    rows = []
    for path in sorted(output.iterdir()):
        with path.open("rb") as handle:
            digest = hashlib.file_digest(handle, "sha256").hexdigest()
        rows.append(f"{digest}  {path.name}\n")
    (output / "SHA256SUMS").write_text("".join(rows))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    check = commands.add_parser("check-tag")
    check.add_argument("tag")
    prepare = commands.add_parser("prepare")
    prepare.add_argument("--dist", type=Path, default=ROOT / "dist")
    prepare.add_argument("--output", type=Path, default=ROOT / "dist/release-assets")
    args = parser.parse_args()
    version = project_version()
    if args.command == "check-tag":
        check_tag(args.tag, version)
        print(version)
    else:
        prepare_assets(args.dist, args.output, version)
        print(args.output)


if __name__ == "__main__":
    main()
