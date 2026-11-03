"""CLI tests call the boox-sync entry point."""

import json
from pathlib import Path

import pytest

from boox_sync.cli import main
from boox_sync.ksync import assemble_note, build_batch
from boox_sync.note import build_note


def test_first_run_requires_device_selection(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    assert main(["sync"]) == 1
    assert "init --serial" in capsys.readouterr().out
    assert not (tmp_path / "Documents").exists()


def test_init_requires_serial_and_preserves_existing_config(tmp_path):
    path = tmp_path / "config.json"
    with pytest.raises(SystemExit) as error:
        main(["init", "--config", str(path)])
    assert error.value.code == 2 and not path.exists()
    assert main(["init", "--config", str(path), "--serial", "TEST1234"]) == 0
    before = path.read_bytes()
    assert main(["init", "--config", str(path), "--serial", "DIFFERENT"]) == 1
    assert path.read_bytes() == before


def _note(text=None):
    strokes = [
        {"pen_type": 2, "points": [(10, 20, 100), (60, 70, 100)], "thickness": 2}
    ]
    if text is not None:
        strokes = [
            {
                "pen_type": 6,
                "text": text,
                "bbox": {"left": 40, "top": 50, "right": 240, "bottom": 90},
            }
        ]
    return build_note(
        note_id="aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
        title="Journal",
        width=500,
        height=800,
        strokes=strokes,
    )


def test_render_command_prints_status_and_does_not_touch_sync_state(tmp_path):
    """boox-sync render writes the PDF and leaves the sync state file alone."""
    note = tmp_path / "ball.note"
    note.write_bytes(_note())
    output = tmp_path / "ball.pdf"
    state = Path.home() / "Library" / "Application Support" / "BOOX Sync" / "state.json"
    before = state.read_bytes() if state.exists() else None
    code = main(["render", str(note), "-o", str(output)])
    after = state.read_bytes() if state.exists() else None
    assert code == 0
    assert output.is_file()
    assert before == after


def test_committed_fixtures_render_the_same_twice(tmp_path, capsys):
    """The shipped fixtures render twice with the same status, box, and text."""
    from pypdf import PdfReader

    state = Path.home() / "Library" / "Application Support" / "BOOX Sync" / "state.json"
    before = state.read_bytes() if state.exists() else None
    expectations = {
        "ballpoint.note": None,
        "textbox.note": "Hello note",
    }
    for name, expected_text in expectations.items():
        snapshots = []
        printed = []
        for index in (1, 2):
            dest = tmp_path / f"{name}{index}.pdf"
            code = main(["render", f"tests/fixtures/{name}", "-o", str(dest)])
            printed.append(capsys.readouterr().out)
            assert code == 0
            reader = PdfReader(dest)
            snapshots.append(
                (
                    len(reader.pages),
                    float(reader.pages[0].mediabox.width),
                    float(reader.pages[0].mediabox.height),
                    reader.pages[0].extract_text(),
                )
            )
        assert printed[0] == printed[1]
        assert "render_status=ok" in printed[0]
        assert snapshots[0] == snapshots[1]
        assert snapshots[0][0] == 1
        assert snapshots[0][1:][:2] == (500.0, 800.0)
        if expected_text is not None:
            assert snapshots[0][3].removesuffix("\n") == expected_text
    after = state.read_bytes() if state.exists() else None
    assert before == after


def test_render_text_command(tmp_path, capsys):
    """boox-sync render prints render_status for a text-box note."""
    note = tmp_path / "text.note"
    note.write_bytes(_note("Hello note"))
    output = tmp_path / "text.pdf"
    code = main(["render", str(note), "-o", str(output)])
    printed = capsys.readouterr().out
    assert code == 0
    assert "render_status=ok" in printed
    assert output.is_file()


@pytest.mark.parametrize("path_lookup", [False, True])
def test_probe_accepts_a_long_device_line_with_spaces(
    tmp_path, monkeypatch, capsys, path_lookup
):
    """adb devices -l pads the serial with spaces. That line is still authorized."""
    from boox_sync.adb import AdbKsyncSource, AdbUsbSource
    from boox_sync.sync import RemoteNotebook

    listing = "List of devices attached\nTEST1234               device usb:1 product:NoteAir3C model:NoteAir3C device:NoteAir3C transport_id:1\n"

    class Completed:
        def __init__(self, stdout="", returncode=0):
            self.stdout = stdout
            self.stderr = ""
            self.returncode = returncode

    def fake_run(argv, *args, **kwargs):
        if list(argv[-2:]) == ["devices", "-l"]:
            return Completed(listing)
        if "getprop" in argv:
            return Completed("NoteAir3C\n")
        raise AssertionError(argv)

    monkeypatch.setattr("boox_sync.cli.subprocess.run", fake_run)
    monkeypatch.setattr("boox_sync.cli.usb_present", lambda serial: True)
    monkeypatch.setattr(
        AdbKsyncSource,
        "list_notebooks",
        lambda self: [
            RemoteNotebook(
                "11111111111141118111111111111111", "Sample Project", "sig1234567890"
            )
        ],
    )
    monkeypatch.setattr(
        AdbUsbSource,
        "list_notes",
        lambda self: [("/storage/emulated/0/note/Journal.note", 4, 9)],
    )
    fake_adb = tmp_path / "adb"
    fake_adb.touch()
    fake_adb.chmod(0o755)
    monkeypatch.setenv("PATH", str(tmp_path))
    config = tmp_path / "config.json"
    config.write_text(
        json.dumps(
            {
                "device_serial": "TEST1234",
                "archive_dir": str(tmp_path / "BOOX"),
                "state_dir": str(tmp_path / "state"),
                "remote_roots": ["/storage/emulated/0/note"],
                "adb_path": "adb" if path_lookup else str(fake_adb),
            }
        )
    )
    code = main(["probe", "--config", str(config)])
    printed = capsys.readouterr().out
    assert code == 0
    assert "adb does not list an authorized device" not in printed
    assert "notebook 'Sample Project'" in printed
    assert "/storage/emulated/0/note/Journal.note" in printed
    assert not (tmp_path / "BOOX").exists()


def test_sync_invalid_serial_exits_1_and_writes_nothing(tmp_path, capsys):
    """An invalid serial refuses the sync before creating the archive."""
    archive = tmp_path / "BOOX"
    config = tmp_path / "config.json"
    config.write_text(
        json.dumps(
            {
                "device_serial": "bad serial",
                "archive_dir": str(archive),
                "state_dir": str(tmp_path / "state"),
                "remote_roots": ["/storage/emulated/0/note"],
                "adb_path": "/opt/homebrew/bin/adb",
            }
        )
    )
    code = main(["sync", "--config", str(config)])
    assert code == 1
    assert "invalid device serial" in capsys.readouterr().out
    assert not archive.exists()
    assert not (tmp_path / "state").exists()


def test_sync_absent_device_does_not_call_adb(tmp_path, capsys, monkeypatch):
    """A serial that is not on USB exits 1 without invoking adb."""
    calls = []

    def spy(argv, *args, **kwargs):
        calls.append(list(argv))
        raise AssertionError(f"An absent device must not launch a process: {argv}")

    monkeypatch.setattr("boox_sync.cli.usb_present", lambda serial: False)
    monkeypatch.setattr("boox_sync.cli.subprocess.run", spy)
    archive = tmp_path / "BOOX"
    config = tmp_path / "config.json"
    config.write_text(
        json.dumps(
            {
                "device_serial": "ABSENT1",
                "archive_dir": str(archive),
                "state_dir": str(tmp_path / "state"),
                "remote_roots": ["/storage/emulated/0/note"],
                "adb_path": "/opt/homebrew/bin/adb",
            }
        )
    )
    code = main(["sync", "--config", str(config)])
    printed = capsys.readouterr().out
    assert code == 1
    assert "adb not invoked" in printed
    assert "Not connected" in printed
    assert calls == []
    assert not archive.exists()


def _synced_archive(tmp_path, monkeypatch, path_lookup=False):
    """Sync one notebook into a throwaway archive and return the config path."""
    from boox_sync.adb import AdbKsyncSource
    from boox_sync.ksync import build_virtual_page
    from boox_sync.sync import RemoteNotebook

    page = "22222222222242228222222222222222"
    files = build_batch(
        page,
        "11111111-1111-1111-1111-111111111111",
        [{"pen_type": 2, "points": [(10, 20, 100), (60, 70, 100)], "thickness": 2}],
        1793600000000,
    )
    files[f"page/{page}"] = build_virtual_page(
        page,
        0,
        1793599000000,
        {"left": 0.0, "top": 0.0, "right": 1860.0, "bottom": 2480.0},
    )
    raw = assemble_note("11111111111141118111111111111111", "Sample Project", files)
    listing = "List of devices attached\nTEST1234               device\n"
    archive = tmp_path / "BOOX"

    class Completed:
        def __init__(self, stdout="", returncode=0):
            self.stdout = stdout
            self.stderr = ""
            self.returncode = returncode

    monkeypatch.setattr("boox_sync.cli.usb_present", lambda serial: True)

    def fake_run(argv, *args, **kwargs):
        if "devices" in argv:
            return Completed(listing)
        raise AssertionError(argv)

    monkeypatch.setattr("boox_sync.cli.subprocess.run", fake_run)
    monkeypatch.setattr(AdbKsyncSource, "pull_notebook", lambda self, nb: (raw, b"png"))

    def fake_list(self):
        return [
            RemoteNotebook("11111111111141118111111111111111", "Sample Project", "s1")
        ]

    monkeypatch.setattr(AdbKsyncSource, "list_notebooks", fake_list)
    fake_adb = tmp_path / "adb"
    fake_adb.touch()
    fake_adb.chmod(0o755)
    monkeypatch.setenv("PATH", str(tmp_path))
    config = tmp_path / "config.json"
    config.write_text(
        json.dumps(
            {
                "device_serial": "TEST1234",
                "archive_dir": str(archive),
                "state_dir": str(tmp_path / "state"),
                "remote_roots": ["/storage/emulated/0/note"],
                "adb_path": "adb" if path_lookup else str(fake_adb),
            }
        )
    )
    return config, archive


@pytest.mark.parametrize("path_lookup", [False, True])
def test_notebooks_view_and_export_commands(tmp_path, monkeypatch, capsys, path_lookup):
    """sync writes the PDF, notebooks lists it, and view opens that PDF."""
    config, archive = _synced_archive(tmp_path, monkeypatch, path_lookup)
    assert main(["sync", "--config", str(config)]) == 0
    folder = archive / "Sample Project"
    assert (folder / "Sample Project.note").is_file()
    assert (folder / "cover.png").read_bytes() == b"png"
    assert (folder / "Sample Project.pdf").is_file()

    code = main(["notebooks", "--config", str(config)])
    printed = capsys.readouterr().out
    assert code == 0
    assert "Sample Project" in printed

    opened = []
    monkeypatch.setattr(
        "boox_sync.cli.default_opener", lambda path: opened.append(Path(path))
    )
    code = main(["view", "sample project", "--config", str(config)])
    printed = capsys.readouterr().out
    assert code == 0
    assert opened == [folder / "Sample Project.pdf"]
    assert (folder / "Sample Project.pdf").is_file()
    manifest = json.loads((folder / "manifest.json").read_text())
    assert manifest["render_status"] == "ok"

    code = main(
        ["export", "11111111", "-o", str(tmp_path / "out.pdf"), "--config", str(config)]
    )
    printed = capsys.readouterr().out
    assert code == 0
    assert "render_status=ok" in printed
    assert (tmp_path / "out.pdf").is_file()


def test_view_unknown_notebook_prints_error(tmp_path, monkeypatch, capsys):
    """view with no match exits 1 without touching anything."""
    config, _archive = _synced_archive(tmp_path, monkeypatch)
    assert main(["sync", "--config", str(config)]) == 0
    code = main(["view", "No Such Notebook", "--config", str(config)])
    printed = capsys.readouterr().out
    assert code == 1
    assert "no notebook matches" in printed
