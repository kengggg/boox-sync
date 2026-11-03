#!/usr/bin/env python3
"""Build an Apple Silicon .app and DMG with a bundled Python engine and ADB.

Run with the project's `app` extra. Local builds use ad-hoc signatures. A
Developer ID identity and optional notarytool keychain profile enable release
signing; this script never downloads tools or changes login items.
"""

import argparse
import hashlib
import importlib.metadata
import json
import os
import platform
import plistlib
import shutil
import subprocess
import sys
import sysconfig
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BUILD = ROOT / "macos" / ".build"
ASSETS = ROOT / "macos" / "Assets"


def run(*args, **kwargs):
    print("+", " ".join(str(a) for a in args), flush=True)
    return subprocess.run([str(a) for a in args], check=True, **kwargs)


def source_digest():
    digest = hashlib.sha256()
    for path in (
        sorted((ROOT / "src/boox_sync").rglob("*.py"))
        + sorted((ROOT / "src/boox_sync/fonts").glob("*"))
        + [
            ROOT / "uv.lock",
            ROOT / "pyproject.toml",
            ROOT / "macos/build_app.py",
            ROOT / "macos/engine_entry.py",
        ]
    ):
        digest.update(path.read_bytes())
    return digest.hexdigest()


def write_icons(contents: Path) -> None:
    """Scale the master artwork into AppIcon.icns and copy the menu bar template."""
    iconset = BUILD / "AppIcon.iconset"
    if iconset.exists():
        shutil.rmtree(iconset)
    iconset.mkdir(parents=True)
    master = ASSETS / "AppIcon.png"
    for base in (16, 32, 128, 256, 512):
        for scale in (1, 2):
            pixels = base * scale
            suffix = "@2x" if scale == 2 else ""
            run(
                "sips",
                "-z",
                str(pixels),
                str(pixels),
                master,
                "--out",
                iconset / f"icon_{base}x{base}{suffix}.png",
            )
    run("iconutil", "-c", "icns", iconset, "-o", contents / "Resources/AppIcon.icns")
    shutil.copy2(ASSETS / "MenuBarIcon.png", contents / "Resources/MenuBarIcon.png")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=ROOT / "dist")
    parser.add_argument(
        "--adb", type=Path, default=Path(shutil.which("adb") or "/opt/homebrew/bin/adb")
    )
    parser.add_argument(
        "--identity",
        default="-",
        help="Developer ID Application identity; '-' means local ad-hoc",
    )
    parser.add_argument(
        "--notary-profile",
        help="Existing notarytool keychain profile; requires Developer ID signing",
    )
    parser.add_argument(
        "--reuse-engine",
        action="store_true",
        help="Reuse only if source and lock fingerprints match",
    )
    parser.add_argument("--no-dmg", action="store_true")
    args = parser.parse_args()
    if sys.platform != "darwin" or platform.machine() != "arm64":
        parser.error("Build on macOS with Xcode and Apple Silicon Python.")
    if args.notary_profile and args.identity == "-":
        parser.error(
            "Notarization requires a Developer ID Application signing identity."
        )
    adb = args.adb.resolve()
    notices = adb.parent / "NOTICE.txt"
    if not adb.is_file() or not notices.is_file():
        parser.error("Provide an official Platform Tools adb alongside its NOTICE.txt.")
    BUILD.mkdir(parents=True, exist_ok=True)
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    environment = {
        **os.environ,
        "MACOSX_DEPLOYMENT_TARGET": "14.0",
        "PYINSTALLER_CONFIG_DIR": str(BUILD / "pyinstaller-cache"),
    }
    engine_dir = BUILD / "engine-dist" / "boox-engine.app"
    fingerprint = BUILD / "engine-fingerprint"
    reuse = (
        args.reuse_engine
        and engine_dir.is_dir()
        and fingerprint.exists()
        and fingerprint.read_text() == source_digest()
    )
    if not reuse:
        run(
            sys.executable,
            "-m",
            "PyInstaller",
            "--noconfirm",
            "--clean",
            "--onedir",
            "--windowed",
            "--osx-bundle-identifier",
            "app.booxsync.engine",
            "--target-arch",
            "arm64",
            "--name",
            "boox-engine",
            "--distpath",
            engine_dir.parent,
            "--workpath",
            BUILD / "pyinstaller",
            "--specpath",
            BUILD,
            "--paths",
            ROOT / "src",
            "--collect-data",
            "boox_sync",
            "--copy-metadata",
            "boox-sync",
            "--collect-data",
            "reportlab",
            "--hidden-import",
            "uharfbuzz",
            "--exclude-module",
            "rumps",
            "--exclude-module",
            "AppKit",
            "--exclude-module",
            "Foundation",
            ROOT / "macos/engine_entry.py",
            env=environment,
            cwd=ROOT,
        )
        fingerprint.write_text(source_digest())
    run(
        "swift",
        "build",
        "--package-path",
        ROOT / "macos",
        "--scratch-path",
        BUILD,
        "-c",
        "release",
        "--arch",
        "arm64",
        "--disable-sandbox",
        env=environment,
    )
    bin_path = subprocess.check_output(
        [
            "swift",
            "build",
            "--package-path",
            str(ROOT / "macos"),
            "--scratch-path",
            str(BUILD),
            "-c",
            "release",
            "--arch",
            "arm64",
            "--show-bin-path",
        ],
        text=True,
    ).strip()
    stage = output / ".BOOX-Sync-build.app"
    if stage.exists():
        shutil.rmtree(stage)
    contents = stage / "Contents"
    for directory in ("MacOS", "Helpers", "Resources"):
        (contents / directory).mkdir(parents=True)
    shutil.copy2(Path(bin_path) / "BOOXSync", contents / "MacOS/BOOXSync")
    helper = contents / "Helpers/BOOXEngine.app"
    shutil.copytree(engine_dir, helper, symlinks=True)
    helper_info = helper / "Contents/Info.plist"
    helper_values = plistlib.loads(helper_info.read_bytes())
    helper_values["LSBackgroundOnly"] = True
    helper_info.write_bytes(plistlib.dumps(helper_values))
    architectures = subprocess.check_output(
        ["lipo", "-archs", str(adb)], text=True
    ).split()
    if architectures == ["arm64"]:
        shutil.copy2(adb, contents / "Helpers/adb")
    elif "arm64" in architectures:
        run("lipo", adb, "-thin", "arm64", "-output", contents / "Helpers/adb")
    else:
        raise RuntimeError("ADB must contain an Apple Silicon executable")
    (contents / "Helpers/adb").chmod(0o755)
    shutil.copy2(notices, contents / "Resources/ADB-NOTICE.txt")
    shutil.copy2(ROOT / "NOTICE", contents / "Resources/NOTICE.txt")
    shutil.copy2(ROOT / "LICENSE", contents / "Resources/LICENSE.txt")
    version = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]["version"]
    info = {
        "CFBundleIdentifier": "app.booxsync.mac",
        "CFBundleName": "BOOX Sync",
        "CFBundleDisplayName": "BOOX Sync",
        "CFBundleExecutable": "BOOXSync",
        "CFBundlePackageType": "APPL",
        "CFBundleSupportedPlatforms": ["MacOSX"],
        "NSPrincipalClass": "NSApplication",
        "CFBundleShortVersionString": version,
        "CFBundleVersion": version,
        "LSMinimumSystemVersion": "14.0",
        "NSHighResolutionCapable": True,
        "CFBundleIconFile": "AppIcon",
        "LSApplicationCategoryType": "public.app-category.productivity",
        "NSHumanReadableCopyright": "BOOX Sync contributors. Not affiliated with ONYX.",
    }
    (contents / "Info.plist").write_bytes(plistlib.dumps(info))
    write_icons(contents)
    licenses = contents / "Resources/Licenses"
    licenses.mkdir()
    shutil.copy2(
        ROOT / "src/boox_sync/fonts/OFL.txt", licenses / "NotoSansThai-OFL.txt"
    )
    for package in (
        "reportlab",
        "uharfbuzz",
        "pillow",
        "charset-normalizer",
        "pyinstaller",
    ):
        dist = importlib.metadata.distribution(package)
        for relative in dist.files or []:
            path = Path(str(relative))
            if (
                path.name.lower().startswith(("license", "copying", "notice"))
                and dist.locate_file(relative).is_file()
            ):
                destination = licenses / package / path
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(dist.locate_file(relative), destination)
    shutil.copy2(
        Path(sysconfig.get_path("stdlib")) / "LICENSE.txt", licenses / "Python.txt"
    )
    build_info = {
        "version": version,
        "source_commit": os.environ.get("GITHUB_SHA", "local-working-tree"),
        "architecture": "arm64",
        "minimum_macos": "14.0",
        "signing": "local-ad-hoc" if args.identity == "-" else "Developer ID",
        "engine_source_sha256": source_digest(),
        "adb_original_sha256": hashlib.sha256(adb.read_bytes()).hexdigest(),
        "adb_version": subprocess.check_output(
            [str(adb), "version"], text=True
        ).splitlines()[:2],
    }
    (contents / "Resources/build-info.json").write_text(
        json.dumps(build_info, indent=2)
    )
    sign_options = ["--force", "--sign", args.identity]
    if args.identity != "-":
        sign_options += ["--timestamp", "--options", "runtime"]
    # Sign nested Mach-O files, then framework bundles, and the containing app last.
    macho = {
        b"\xcf\xfa\xed\xfe",
        b"\xce\xfa\xed\xfe",
        b"\xca\xfe\xba\xbe",
        b"\xbe\xba\xfe\xca",
    }
    for path in sorted(contents.rglob("*"), key=lambda p: len(p.parts), reverse=True):
        if path.is_symlink() or not path.is_file() or path.parent.name == "MacOS":
            continue
        with path.open("rb") as handle:
            magic = handle.read(4)
        if magic in macho:
            run("codesign", *sign_options, path)
    for path in sorted(
        contents.rglob("*.framework"), key=lambda p: len(p.parts), reverse=True
    ):
        if not path.is_symlink():
            run("codesign", *sign_options, path)
    run("codesign", *sign_options, helper)
    run("codesign", *sign_options, stage)
    run("codesign", "--verify", "--deep", "--strict", "--verbose=2", stage)
    # Exercise the actual frozen interpreter with no developer Python variables.
    smoke_env = {
        k: v
        for k, v in os.environ.items()
        if k not in {"PYTHONPATH", "PYTHONHOME", "VIRTUAL_ENV"}
    }
    result = subprocess.run(
        [str(helper / "Contents/MacOS/boox-engine")],
        input='{"protocol":1,"command":"health"}\n',
        text=True,
        capture_output=True,
        env=smoke_env,
        check=True,
        timeout=20,
    )
    if not json.loads(result.stdout)["ok"]:
        raise RuntimeError("Bundled engine failed its health check")
    app = output / "BOOX Sync.app"
    if app.exists():
        shutil.rmtree(app)
    stage.rename(app)
    print("Built:", app)
    if not args.no_dmg:
        dmg_stage = BUILD / "dmg-stage"
        if dmg_stage.exists():
            shutil.rmtree(dmg_stage)
        dmg_stage.mkdir()
        shutil.copytree(app, dmg_stage / app.name, symlinks=True)
        (dmg_stage / "Applications").symlink_to("/Applications")
        (dmg_stage / "Read Me.txt").write_text(
            "BOOX Sync\n\nDrag BOOX Sync to Applications, then open it.\nUse Settings to select your BOOX and an archive folder.\n\n"
            + (
                "Experimental preview: locally signed, not Developer ID signed or notarized. macOS may block opening this app.\n"
                if args.identity == "-"
                else ""
            )
        )
        dmg = (
            output
            / f"BOOX-Sync-{version}-arm64{'-local' if args.identity == '-' else ''}.dmg"
        )
        run(
            "hdiutil",
            "create",
            "-volname",
            "BOOX Sync",
            "-srcfolder",
            dmg_stage,
            "-ov",
            "-format",
            "UDZO",
            dmg,
        )
        if args.notary_profile:
            run("codesign", *sign_options, dmg)
            run(
                "xcrun",
                "notarytool",
                "submit",
                dmg,
                "--keychain-profile",
                args.notary_profile,
                "--wait",
            )
            run("xcrun", "stapler", "staple", dmg)
            run("xcrun", "stapler", "validate", dmg)
        print("Built:", dmg)


if __name__ == "__main__":
    main()
