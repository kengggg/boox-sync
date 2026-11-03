"""Sync tests drive sync_once with a fake notebook source."""

import json

import pytest

from boox_sync.config import Config, ConfigError
from boox_sync.ksync import assemble_note, build_batch, build_virtual_page
from boox_sync.sync import RemoteNotebook, sync_once

NOTE_A = "11111111111141118111111111111111"
NOTE_B = "33333333333343338333333333333333"
PAGE = "22222222222242228222222222222222"


def _raw(note_id, title, x_pos=10):
    files = build_batch(
        PAGE,
        "11111111-1111-1111-1111-111111111111",
        [
            {
                "pen_type": 2,
                "points": [(x_pos, 10, 50), (x_pos + 30, 40, 50)],
                "thickness": 2,
            }
        ],
        1793600000000,
    )
    files[f"page/{PAGE}"] = build_virtual_page(
        PAGE, 0, 1, {"left": 0, "top": 0, "right": 500, "bottom": 800}
    )
    return assemble_note(note_id, title, files)


class Listing:
    """In-memory notebook source. pull can raise per notebook."""

    def __init__(self, archive):
        self.archive = archive
        self.notebooks: list[RemoteNotebook] = []
        self.failure: dict[str, Exception] = {}
        self.pulled: list[str] = []
        self.cover = b"png-bytes"

    def list_notebooks(self):
        return list(self.notebooks)

    def pull_notebook(self, notebook):
        if notebook.note_id in self.failure:
            raise self.failure[notebook.note_id]
        self.pulled.append(notebook.note_id)
        return _raw(notebook.note_id, notebook.title), self.cover


def _config(tmp_path, serial="TEST1234"):
    return Config(
        device_serial=serial,
        adb_path="/opt/homebrew/bin/adb",
        archive_dir=tmp_path / "BOOX",
        state_dir=tmp_path / "state",
    )


def _nb(note_id, title, sig="s1"):
    return RemoteNotebook(note_id=note_id, title=title, signature=sig)


def test_first_sync_archives_every_notebook_and_writes_pdfs(tmp_path):
    """Every notebook lands as .note + cover + a PDF rendered from those bytes."""
    source = Listing(tmp_path)
    source.notebooks = [_nb(NOTE_A, "Sample Project"), _nb(NOTE_B, "Sample Budget")]
    report = sync_once(_config(tmp_path), source)
    assert report.status == "ok"
    assert [item.status for item in report.outcomes] == ["ok", "ok"]
    for folder in ("Sample Project", "Sample Budget"):
        root = tmp_path / "BOOX" / folder
        assert (root / f"{folder}.note").is_file()
        assert (root / "cover.png").read_bytes() == b"png-bytes"
        assert (root / f"{folder}.pdf").is_file()
        manifest = json.loads((root / "manifest.json").read_text())
        assert manifest["render_status"] == "ok"
        assert manifest["rendered_sha256"] == manifest["content_sha256"]
        assert manifest["source_path"] == f"ksync:{NOTE_A}" or manifest["note_id"]


def test_second_sync_skips_unchanged_notebooks(tmp_path):
    """A matching signature never pulls again."""
    source = Listing(tmp_path)
    source.notebooks = [_nb(NOTE_A, "Sample Project")]
    config = _config(tmp_path)
    sync_once(config, source)
    source.notebooks = [_nb(NOTE_A, "Sample Project")]
    report = sync_once(config, source)
    assert report.status == "ok"
    assert report.outcomes[0].status == "skipped"
    assert source.pulled == [NOTE_A]


def test_changed_signature_repulls_and_replaces(tmp_path):
    """A new signature pulls and replaces the archived .note."""
    source = Listing(tmp_path)
    config = _config(tmp_path)
    source.notebooks = [_nb(NOTE_A, "Sample Project", sig="one")]
    sync_once(config, source)
    source.notebooks = [_nb(NOTE_A, "Sample Project", sig="two")]
    report = sync_once(config, source)
    assert [item.status for item in report.outcomes] == ["ok"]
    assert source.pulled == [NOTE_A, NOTE_A]
    manifest = json.loads(
        (tmp_path / "BOOX" / "Sample Project" / "manifest.json").read_text()
    )
    assert manifest["rendered_sha256"] == manifest["content_sha256"]


def test_missing_archived_note_repulls(tmp_path):
    """A deleted archive folder heals on the next pass."""
    source = Listing(tmp_path)
    config = _config(tmp_path)
    source.notebooks = [_nb(NOTE_A, "Sample Project")]
    sync_once(config, source)
    (tmp_path / "BOOX" / "Sample Project" / "Sample Project.note").unlink()
    source.notebooks = [_nb(NOTE_A, "Sample Project")]
    report = sync_once(config, source)
    assert report.outcomes[0].status == "ok"
    assert (tmp_path / "BOOX" / "Sample Project" / "Sample Project.note").is_file()


def test_pull_failure_keeps_previous_notebook_and_reports_failed(tmp_path):
    """A failed pull leaves the previous archive untouched."""
    source = Listing(tmp_path)
    config = _config(tmp_path)
    source.notebooks = [_nb(NOTE_A, "Sample Project", sig="one")]
    sync_once(config, source)
    before = (tmp_path / "BOOX" / "Sample Project" / "Sample Project.note").read_bytes()
    source.notebooks = [_nb(NOTE_A, "Sample Project", sig="two")]
    source.failure[NOTE_A] = RuntimeError("adb pull failed")
    report = sync_once(config, source)
    assert report.status == "failed"
    assert report.outcomes[0].status == "failed"
    assert "adb pull failed" in report.outcomes[0].error
    after = (tmp_path / "BOOX" / "Sample Project" / "Sample Project.note").read_bytes()
    assert before == after


def test_rename_creates_new_folder_and_marks_later_copy(tmp_path):
    """A title change archives into a new folder and sets later_copy."""
    source = Listing(tmp_path)
    config = _config(tmp_path)
    source.notebooks = [_nb(NOTE_A, "Old Name")]
    sync_once(config, source)
    source.notebooks = [_nb(NOTE_A, "New Name")]
    sync_once(config, source)
    old = tmp_path / "BOOX" / "Old Name"
    new = tmp_path / "BOOX" / "New Name"
    assert (old / "Old Name.note").is_file()
    assert (new / "New Name.note").is_file()
    assert json.loads((old / "manifest.json").read_text())["later_copy"] == "New Name"


def test_same_title_different_note_ids_gets_suffixed_folder(tmp_path):
    """Three notebooks named Notepad1 get distinct folders."""
    source = Listing(tmp_path)
    config = _config(tmp_path)
    source.notebooks = [_nb(NOTE_A, "Notepad1"), _nb(NOTE_B, "Notepad1")]
    report = sync_once(config, source)
    assert report.status == "ok"
    root = tmp_path / "BOOX"
    assert (root / "Notepad1" / "Notepad1.note").is_file()
    suffix = NOTE_B.replace("-", "").lower()[:8]
    assert (root / f"Notepad1 {suffix}" / f"Notepad1 {suffix}.note").is_file()


def test_failed_notebook_does_not_stop_the_run(tmp_path):
    """One failure marks the pass failed but the rest still archive."""
    source = Listing(tmp_path)
    source.notebooks = [_nb(NOTE_A, "Sample Project"), _nb(NOTE_B, "Sample Budget")]
    source.failure[NOTE_A] = RuntimeError("boom")
    report = sync_once(_config(tmp_path), source)
    assert report.status == "failed"
    statuses = {item.note_id: item.status for item in report.outcomes}
    assert statuses[NOTE_A] == "failed"
    assert statuses[NOTE_B] == "ok"
    assert (tmp_path / "BOOX" / "Sample Budget" / "Sample Budget.note").is_file()


def test_empty_listing_reports_no_notes(tmp_path):
    """No notebooks at all stops with the no_notes status."""
    source = Listing(tmp_path)
    report = sync_once(_config(tmp_path), source)
    assert report.status == "no_notes"
    assert not list((tmp_path / "BOOX").glob("*/*.note"))


def test_invalid_serial_refuses_and_writes_nothing(tmp_path):
    """An invalid serial raises before any file is written."""
    source = Listing(tmp_path)
    source.notebooks = [_nb(NOTE_A, "Sample Project")]
    config = _config(tmp_path, serial="bad;serial")
    with pytest.raises(ConfigError):
        sync_once(config, source)
    assert not (tmp_path / "BOOX").exists()
    assert not (tmp_path / "state" / "state.json").exists()


def test_state_records_signature_and_folder(tmp_path):
    """State rows carry the signature used for skipping."""
    source = Listing(tmp_path)
    config = _config(tmp_path)
    source.notebooks = [_nb(NOTE_A, "Sample Project", sig="sig-abc")]
    sync_once(config, source)
    state = json.loads((config.state_dir / "state.json").read_text())
    entry = state["notes"][f"TEST1234:{NOTE_A}"]
    assert entry["signature"] == "sig-abc"
    assert entry["archive_folder"] == "Sample Project"
    assert entry["title"] == "Sample Project"
    assert entry["note_id"] == NOTE_A


def test_inactive_notebooks_keep_every_saved_byte_and_can_reappear(tmp_path):
    from boox_sync.engine import library

    source = Listing(tmp_path)
    config = _config(tmp_path)
    source.notebooks = [_nb(NOTE_A, "Notebook")]
    assert sync_once(config, source).status == "ok"
    before = {
        str(p.relative_to(config.archive_dir)): p.read_bytes()
        for p in config.archive_dir.rglob("*")
        if p.is_file()
    }
    source.notebooks = [
        RemoteNotebook(NOTE_A, "Notebook", "deleted-files", active=False)
    ]
    source.failure[NOTE_A] = RuntimeError("deleted source must never be pulled")
    report = sync_once(config, source)
    assert report.status == "ok"
    assert report.outcomes[0].status == "inactive"
    assert library(config)["notebooks"][0]["presence"] == "inactive"
    assert before == {
        str(p.relative_to(config.archive_dir)): p.read_bytes()
        for p in config.archive_dir.rglob("*")
        if p.is_file()
    }
    source.notebooks = [_nb(NOTE_A, "Notebook")]
    assert sync_once(config, source).status == "ok"
    assert library(config)["notebooks"][0]["presence"] == "active"
    assert source.pulled == [NOTE_A]


def test_verified_empty_inventory_keeps_local_copies(tmp_path):
    from boox_sync.engine import library

    source = Listing(tmp_path)
    config = _config(tmp_path)
    source.notebooks = [_nb(NOTE_A, "Notebook")]
    sync_once(config, source)
    source.notebooks = []
    assert sync_once(config, source).status == "no_notes"
    data = library(config)
    assert data["inventory_checked_at"]
    assert data["notebooks"][0]["presence"] == "absent"
    assert data["notebooks"][0]["has_archive"]


@pytest.mark.parametrize("bad_listing", ["exception", "duplicate", "unknown"])
def test_bad_inventory_does_not_change_previous_tablet_status(tmp_path, bad_listing):
    source = Listing(tmp_path)
    config = _config(tmp_path)
    source.notebooks = [_nb(NOTE_A, "Notebook")]
    sync_once(config, source)
    before = (config.state_dir / "inventory.json").read_bytes()
    if bad_listing == "exception":

        def failure():
            raise RuntimeError("partial database transfer")

        source.list_notebooks = failure
    elif bad_listing == "duplicate":
        source.notebooks *= 2
    else:
        source.notebooks[0].active = None
    assert sync_once(config, source).status == "failed"
    assert (config.state_dir / "inventory.json").read_bytes() == before


def test_active_notebook_without_files_is_visible_as_failed_not_deleted(tmp_path):
    from boox_sync.engine import library

    source = Listing(tmp_path)
    config = _config(tmp_path)
    source.notebooks = [_nb(NOTE_A, "Missing")]
    source.failure[NOTE_A] = RuntimeError("no staged document files")
    assert sync_once(config, source).status == "failed"
    note = library(config)["notebooks"][0]
    assert note["presence"] == "active"
    assert not note["has_archive"]
    assert note["folder"] == ""


def test_inventory_survives_manifest_rebuild_and_is_scoped_per_tablet(tmp_path):
    from boox_sync.engine import library

    source = Listing(tmp_path)
    first, second = _config(tmp_path), _config(tmp_path, serial="SECOND")
    source.notebooks = [_nb(NOTE_A, "Notebook")]
    sync_once(first, source)
    sync_once(second, source)
    source.notebooks = []
    sync_once(first, source)
    (first.state_dir / "state.json").write_text("broken")
    notes = {n["serial"]: n for n in library(first)["notebooks"]}
    assert notes[first.device_serial]["presence"] == "absent"
    assert notes["SECOND"]["presence"] == "other_device"
    notes = {n["serial"]: n for n in library(second)["notebooks"]}
    assert notes["SECOND"]["presence"] == "active"
