"""Kill a real archive writer at commit boundaries, then retry the same sync."""

import hashlib
import json
import os
import select
import signal
import subprocess
import sys
from pathlib import Path

import pytest

from boox_sync import storage
from boox_sync.archive import list_revisions
from boox_sync.config import Config
from boox_sync.note import build_note, parse_notes
from boox_sync.sync import RemoteNotebook, sync_once
from boox_sync.viewer import needs_render

NOTE_ID = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"


class Source:
    def __init__(self, raw, signature):
        self.raw, self.signature = raw, signature

    def list_notebooks(self):
        return [RemoteNotebook(NOTE_ID, "Interruption Test", self.signature)]

    def pull_notebook(self, notebook):
        return self.raw, None


def config(root):
    return Config(
        device_serial="SYNTHETIC",
        archive_dir=root / "archive",
        state_dir=root / "state",
    )


def payload(x):
    return build_note(
        note_id=NOTE_ID,
        title="Interruption Test",
        strokes=[{"pen_type": 2, "points": [(x, 10, 100), (x + 10, 30, 100)]}],
    )


def interrupted_worker(root, phase):
    original_replace = os.replace

    def replace(source, target):
        target = Path(target)
        live = target.parent.name == "Interruption Test"
        match = (
            (phase == "snapshot" and target.name == "notebook.note")
            or (
                phase in {"source-before", "source-after"}
                and live
                and target.suffix == ".note"
            )
            or (phase == "manifest" and live and target.name == "manifest.json")
            or (
                phase in {"pdf-before", "pdf-after"}
                and live
                and target.suffix == ".pdf"
            )
            or (phase == "state" and target.name == "state.json")
        )
        if match and phase.endswith("-before"):
            print("ready", flush=True)
            sys.stdin.readline()
        original_replace(source, target)
        if match and not phase.endswith("-before"):
            print("ready", flush=True)
            sys.stdin.readline()

    storage.os.replace = replace
    sync_once(config(root), Source((root / "next.note").read_bytes(), "new"))


@pytest.mark.parametrize(
    "phase",
    [
        "snapshot",
        "source-before",
        "source-after",
        "manifest",
        "pdf-before",
        "pdf-after",
        "state",
    ],
)
@pytest.mark.parametrize("stop_signal", [signal.SIGTERM, signal.SIGKILL])
def test_interrupted_sync_retains_verified_source_and_can_retry(
    tmp_path, phase, stop_signal
):
    cfg = config(tmp_path)
    old, new = payload(10), payload(50)
    assert sync_once(cfg, Source(old, "old")).status == "ok"
    (tmp_path / "next.note").write_bytes(new)
    process = subprocess.Popen(
        [sys.executable, __file__, str(tmp_path), phase],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        assert select.select([process.stdout], [], [], 10)[0], (
            "Writer never reached commit boundary"
        )
        assert process.stdout.readline() == b"ready\n"
        process.send_signal(stop_signal)
        process.wait(timeout=10)
        assert process.returncode == -stop_signal
    finally:
        if process.poll() is None:
            process.kill()
        process.communicate(timeout=10)

    folder = cfg.archive_dir / "Interruption Test"
    current = folder / "Interruption Test.note"
    revisions = list_revisions(cfg.archive_dir, cfg.device_serial, NOTE_ID)
    verified = []
    for revision in revisions:
        raw = (revision / "notebook.note").read_bytes()
        manifest = json.loads((revision / "manifest.json").read_text())
        assert hashlib.sha256(raw).hexdigest() == manifest["content_sha256"]
        assert parse_notes(raw)[0].note_id == NOTE_ID
        verified.append(raw)
    assert current.read_bytes() == old or old in verified
    if phase == "source-after":
        assert needs_render(
            folder
        )  # Mixed commit is detected, not treated as up to date.
    assert (
        sync_once(cfg, Source(new, "new")).status == "ok"
    )  # Lock released by process exit.
    assert current.read_bytes() == new
    assert not needs_render(folder)
    assert sync_once(cfg, Source(new, "new")).outcomes[0].status == "skipped"


if __name__ == "__main__":
    interrupted_worker(Path(sys.argv[1]), sys.argv[2])
