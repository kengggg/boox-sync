"""Release inputs must be verified before they can become downloadable assets."""

import hashlib
import importlib.util
import json
import zipfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def script(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


release = script("release")
tools = script("fetch_platform_tools")


@pytest.mark.parametrize(
    "tag", ["v0.2.4", "0.2.3", "v0.2.3-rc1", "v00.2.3", "v0.2.3\n"]
)
def test_invalid_or_mismatched_release_tag_is_rejected(tag):
    with pytest.raises(ValueError):
        release.check_tag(tag, "0.2.3")


def test_engine_health_uses_project_metadata():
    from boox_sync import __version__
    from boox_sync.engine import dispatch

    assert __version__ == release.project_version()
    release.check_tag(f"v{__version__}", __version__)
    assert dispatch({"protocol": 1, "command": "health"})["version"] == __version__


def test_tools_checksum_and_required_files_precede_install(tmp_path):
    archive, output = tmp_path / "tools.zip", tmp_path / "tools"
    with zipfile.ZipFile(archive, "w") as package:
        package.writestr("platform-tools/adb", b"synthetic executable")
    with pytest.raises(ValueError, match="SHA-256"):
        tools.install(archive, output, "0" * 64)
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    with pytest.raises(KeyError):
        tools.install(archive, output, digest)
    assert not output.exists()


def test_tools_extract_only_allowlisted_files_and_do_not_overwrite(tmp_path):
    archive, output = tmp_path / "tools.zip", tmp_path / "tools"
    with zipfile.ZipFile(archive, "w") as package:
        for name in tools.FILES:
            package.writestr(f"platform-tools/{name}", name.encode())
        package.writestr("../outside.txt", b"must not extract")
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    tools.install(archive, output, digest)
    assert sorted(p.name for p in output.iterdir()) == sorted(tools.FILES)
    assert not (tmp_path / "outside.txt").exists()
    assert (output / "adb").stat().st_mode & 0o111
    with pytest.raises(ValueError, match="already exists"):
        tools.install(archive, output, digest)


def test_release_assets_fail_on_stale_metadata_then_include_only_verified_files(
    tmp_path,
):
    dist, output = tmp_path / "dist", tmp_path / "assets"
    resources = dist / "BOOX Sync.app/Contents/Resources"
    resources.mkdir(parents=True)
    metadata = resources / "build-info.json"
    info = {"version": "0.2.2", "architecture": "arm64", "signing": "local-ad-hoc"}
    metadata.write_text(json.dumps(info))
    dmg = dist / "BOOX-Sync-0.2.3-arm64-local.dmg"
    dmg.write_bytes(b"synthetic dmg")
    (dist / "private.note").write_bytes(b"not a release asset")
    with pytest.raises(ValueError, match="metadata"):
        release.prepare_assets(dist, output, "0.2.3")
    assert not output.exists()
    info["version"] = "0.2.3"
    metadata.write_text(json.dumps(info))
    release.prepare_assets(dist, output, "0.2.3")
    assert {p.name for p in output.iterdir()} == {
        dmg.name,
        "build-info.json",
        "SHA256SUMS",
    }
    for line in (output / "SHA256SUMS").read_text().splitlines():
        digest, name = line.split("  ")
        assert hashlib.sha256((output / name).read_bytes()).hexdigest() == digest
