"""Native bridge contracts and archive-location safety, using synthetic notebooks."""

import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from boox_sync.archive import archive_notebook, empty_state
from boox_sync.engine import (
    MARKER,
    archive_identity,
    devices,
    dispatch,
    prepare_archive,
)
from boox_sync.note import build_note
from boox_sync.state import load_state, save_state


def setup_request(tmp_path):
    archive = tmp_path / "archive"
    archive.mkdir()
    identity = prepare_archive(archive)["archive_id"]
    request = {
        "protocol": 1,
        "command": "library",
        "data_dir": str(tmp_path / "state"),
        "adb_path": "unused",
        "config": {
            "archive_dir": str(archive),
            "archive_id": identity,
            "device_serial": "TEST",
        },
    }
    return archive, request


def seed(archive, request):
    state = empty_state()
    raw = build_note(
        note_id="abc",
        title="Private title",
        strokes=[{"pen_type": 2, "points": [(1, 1, 200), (10, 20, 200)]}],
    )
    archive_notebook(
        archive, raw, device_serial="TEST", source_path="fixture", state=state
    )
    save_state(
        Path(request["data_dir"]) / "archives" / request["config"]["archive_id"], state
    )


def test_missing_archive_is_not_recreated(tmp_path):
    archive, request = setup_request(tmp_path)
    archive.rename(tmp_path / "unmounted")
    with pytest.raises(ValueError, match="unavailable"):
        dispatch(request)
    assert not archive.exists()


def test_replaced_folder_is_not_accepted(tmp_path):
    archive, request = setup_request(tmp_path)
    (archive / MARKER).unlink()
    prepare_archive(archive)
    with pytest.raises(ValueError, match="folder has changed"):
        dispatch(request)


def test_archive_selection_preserves_existing_notebooks(tmp_path):
    archive, request = setup_request(tmp_path)
    seed(archive, request)
    original = next(archive.glob("*/*.note")).read_bytes()
    assert prepare_archive(archive)["archive_id"] == request["config"]["archive_id"]
    assert next(archive.glob("*/*.note")).read_bytes() == original
    data = dispatch(request)
    assert data["notebooks"][0]["title"] == "Private title"


def test_diagnostics_exclude_private_data(tmp_path):
    archive, request = setup_request(tmp_path)
    seed(archive, request)
    request["command"] = "diagnostics"
    result = dispatch(request)
    text = json.dumps(result)
    assert (
        "Private title" not in text and "TEST" not in text and str(tmp_path) not in text
    )
    assert result["notebook_status_counts"] == {"pending": 1}


def test_archive_copy_verifies_and_preserves_original(tmp_path):
    archive, request = setup_request(tmp_path)
    seed(archive, request)
    target = tmp_path / "new"
    target.mkdir()
    request.update(command="copy_archive", path=str(target))
    progress = []
    result = dispatch(request, progress.append)
    assert result["archive_id"] == archive_identity(archive) == archive_identity(target)
    assert (
        next(target.glob("*/*.note")).read_bytes()
        == next(archive.glob("*/*.note")).read_bytes()
    )
    assert progress[-1]["completed"] == progress[-1]["total"]


def test_archive_copy_refuses_nonempty_and_nested_targets(tmp_path):
    archive, request = setup_request(tmp_path)
    nested = archive / "nested"
    nested.mkdir()
    request.update(command="copy_archive", path=str(nested))
    with pytest.raises(ValueError, match="separate folder"):
        dispatch(request)
    target = tmp_path / "other"
    target.mkdir()
    (target / "keep.txt").write_text("unrelated")
    request["path"] = str(target)
    with pytest.raises(ValueError, match="must be empty"):
        dispatch(request)
    assert (target / "keep.txt").read_text() == "unrelated"


def test_protocol_version_is_checked_before_access(tmp_path):
    with pytest.raises(ValueError, match="versions do not match"):
        dispatch({"protocol": 99, "command": "prepare_archive", "path": str(tmp_path)})
    assert not (tmp_path / MARKER).exists()


def test_bridge_outputs_one_structured_result_without_python_environment(tmp_path):
    process = subprocess.run(
        [sys.executable, "-m", "boox_sync.engine"],
        input=json.dumps({"protocol": 1, "command": "health"}) + "\n",
        text=True,
        capture_output=True,
        timeout=10,
    )
    assert process.returncode == 0
    result = json.loads(process.stdout)
    assert result["protocol"] == 1 and result["event"] == "result" and result["ok"]


def test_device_discovery_preserves_authorization_states(tmp_path):
    adb = tmp_path / "adb"
    adb.touch()
    output = "List of devices attached\nBOOX1 device product:NoteAir3C model:Note_Air3C transport_id:1\nBOOX2 unauthorized usb:1-2\nBOOX3 offline\n"
    result = devices(
        str(adb),
        runner=lambda *a, **k: SimpleNamespace(stdout=output, stderr="", returncode=0),
    )
    assert [d["state"] for d in result["devices"]] == [
        "device",
        "unauthorized",
        "offline",
    ]
    assert result["devices"][0]["model"] == "Note Air3C"


def test_view_uses_exact_cross_device_identity(tmp_path):
    archive, request = setup_request(tmp_path)
    seed(archive, request)
    data = dispatch({**request, "command": "view", "notebook": "TEST:abc"})
    assert Path(data["path"]).read_bytes().startswith(b"%PDF")


@pytest.mark.parametrize("corrupt", [False, True])
def test_native_recovery_verifies_history_and_preserves_current(tmp_path, corrupt):
    archive, request = setup_request(tmp_path)
    seed(archive, request)
    current = next(archive.glob("*/*.note"))
    original = current.read_bytes()
    state_dir = Path(request["data_dir"]) / "archives" / request["config"]["archive_id"]
    state = load_state(state_dir, archive)
    updated = build_note(
        note_id="abc",
        title="Private title",
        strokes=[{"pen_type": 2, "points": [(50, 60, 200), (70, 80, 200)]}],
    )
    archive_notebook(
        archive, updated, device_serial="TEST", source_path="fixture", state=state
    )
    save_state(state_dir, state)
    revisions = dispatch({**request, "command": "history", "notebook": "TEST:abc"})[
        "revisions"
    ]
    assert len(revisions) == 1
    output = tmp_path / "recovered.note"
    recovery = {
        **request,
        "command": "restore",
        "notebook": "TEST:abc",
        "revision": revisions[0]["id"],
        "output": str(output),
    }
    if corrupt:
        next((archive / ".history").rglob("notebook.note")).write_bytes(b"corrupt")
        with pytest.raises(ValueError, match="integrity"):
            dispatch(recovery)
        assert not output.exists()
    else:
        dispatch(recovery)
        assert output.read_bytes() == original
        with pytest.raises(ValueError, match="new filename"):
            dispatch(recovery)
    assert current.read_bytes() == updated


def test_archive_before_first_inventory_is_unknown_not_deleted(tmp_path):
    archive, request = setup_request(tmp_path)
    seed(archive, request)
    data = dispatch(request)
    assert data["inventory_checked_at"] is None
    assert data["notebooks"][0]["presence"] == "unknown"
    assert data["notebooks"][0]["has_archive"]


@pytest.mark.parametrize(
    "snapshot",
    [
        None,
        {},
        {"notebooks": {}, "checked_at": None},
        {
            "notebooks": {"abc": {"title": "Notebook", "active": "false"}},
            "checked_at": "2026-11-03T10:00:00Z",
        },
    ],
)
def test_damaged_inventory_does_not_label_archive_deleted(tmp_path, snapshot):
    from boox_sync.storage import write_json

    archive, request = setup_request(tmp_path)
    seed(archive, request)
    state_dir = Path(request["data_dir"]) / "archives" / request["config"]["archive_id"]
    write_json(
        state_dir / "inventory.json", {"version": 1, "devices": {"TEST": snapshot}}
    )
    data = dispatch(request)
    assert data["inventory_checked_at"] is None
    assert data["notebooks"][0]["presence"] == "unknown"
