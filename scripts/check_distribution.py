#!/usr/bin/env python3
"""Check the public wheel and source archive for required assets and local files."""

import tarfile
import tomllib
import zipfile
from pathlib import Path


def main():
    version = tomllib.loads(Path("pyproject.toml").read_text())["project"]["version"]
    wheels = list(Path("dist").glob(f"boox_sync-{version}-*.whl"))
    assert len(wheels) == 1, "Expected one wheel for the project version"
    with zipfile.ZipFile(wheels[0]) as wheel:
        names = wheel.namelist()
        for name in ("NotoSansThai-Regular.ttf", "NotoSansThai-Bold.ttf", "OFL.txt"):
            assert f"boox_sync/fonts/{name}" in names, name
        for name in ("LICENSE", "NOTICE"):
            assert any(p.endswith(f"/licenses/{name}") for p in names), name
    with tarfile.open(Path("dist") / f"boox_sync-{version}.tar.gz") as source:
        names = [p.partition("/")[2] for p in source.getnames()]
        for name in (
            "LICENSE",
            "NOTICE",
            "README.md",
            "CONTRIBUTING.md",
            "uv.lock",
            "macos/Package.swift",
            "macos/build_app.py",
            "scripts/platform-tools.json",
            "tests/fixtures/ballpoint.note",
            "tests/fixtures/textbox.note",
        ):
            assert name in names, name
        forbidden = {".local", ".tools", ".venv", ".venv-app", ".build", "__pycache__"}
        assert not any(forbidden.intersection(Path(p).parts) for p in names)
        assert not any(p.endswith((".pdf", ".p12", ".p8")) for p in names)
    print("PASS: source distribution, fonts, notices, and project license")


if __name__ == "__main__":
    main()
