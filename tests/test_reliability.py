"""Regression checks for data loss, real transport errors and rendering fidelity."""

import io
import json
import shlex
import subprocess
import zipfile
from concurrent.futures import Future, ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

import pytest
from pypdf import PdfReader

from boox_sync.adb import (
    AdbError,
    AdbKsyncSource,
    KSYNC_DOCUMENT,
    KSYNC_POINT,
    listing_command,
    parse_listing,
    signature,
)
from boox_sync.archive import archive_notebook, empty_state, export_pdf, list_revisions
from boox_sync.cli import main
from boox_sync.config import Config, ConfigError, load_config
from boox_sync.ksync import assemble_note, build_batch, build_virtual_page
from boox_sync.menu import BackgroundRunner, BooxMenu, _apply_report
from boox_sync.note import build_note, parse_notes
from boox_sync.proto import encode_bytes_field, encode_varint_field
from boox_sync.render import render_note
from boox_sync.state import load_state
from boox_sync.storage import ArchiveBusy, archive_lock, atomic_write
from boox_sync.sync import RemoteNotebook, sync_once
from boox_sync.viewer import needs_render, view_notebook

N = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
P = "22222222-2222-2222-2222-222222222222"
B = "33333333-3333-3333-3333-333333333333"
S = "55555555-5555-5555-5555-555555555555"


def config(root):
    return Config(
        device_serial="TEST1234", archive_dir=root / "archive", state_dir=root / "state"
    )


def raw(title="Journal", x=10, pen=2):
    return build_note(
        note_id=N,
        title=title,
        strokes=[{"pen_type": pen, "points": [(x, 10, 100), (x + 20, 50, 100)]}],
    )


class Source:
    def __init__(self, title="Journal", pen=2):
        self.title, self.pen, self.sig, self.x, self.pulls = title, pen, "one", 10, 0

    def list_notebooks(self):
        return [RemoteNotebook(N, self.title, self.sig)]

    def pull_notebook(self, notebook):
        self.pulls += 1
        return raw(self.title, self.x, self.pen), None


def test_listing_format_roundtrips_real_record_delimiters():
    words = shlex.split(listing_command("adb", "S", ["/root"])[-1])
    fmt = words[words.index("-printf") + 1]
    # Execute the format's escapes, rather than independently fabricating newline output.
    fmt = fmt.replace("%s", "10").replace("%T@", "1.0").replace("%p", "/a")
    output = bytes(fmt, "ascii").decode("unicode_escape")
    assert parse_listing(output + output.replace("/a", "/b")) == [
        ("/a", 10, "1.0"),
        ("/b", 10, "1.0"),
    ]


@pytest.mark.parametrize(
    "listing",
    ["noise", "10 1 /a20 2 /b", "10 1 /a\0truncated", "10 1 /a\0" + "10 1 /a\0"],
)
def test_malformed_listing_is_not_silently_accepted(listing):
    with pytest.raises(AdbError):
        parse_listing(listing)


def test_listing_supports_spaces_and_newlines_in_paths():
    assert parse_listing("3 1.5 /a/a file\nnext\0") == [("/a/a file\nnext", 3, "1.5")]


class Transport:
    """A device filesystem simulator exercising the actual ADB return-code path."""

    def __init__(self):
        files = build_batch(
            P,
            B,
            [{"shape_id": S, "pen_type": 2, "points": [(10, 10, 100), (30, 30, 100)]}],
            1,
        )
        self.files = {}
        for key, value in files.items():
            remote = (
                f"{KSYNC_POINT}/{N}/" + key[6:]
                if key.startswith("point/")
                else f"{KSYNC_DOCUMENT}/{N}/" + key
            )
            self.files[remote] = value
        self.fail = self.partial = self.change = False
        self.calls = []

    def rows(self):
        return [(path, len(value), "1") for path, value in sorted(self.files.items())]

    def run(self, argv, **kwargs):
        assert kwargs["timeout"] > 0
        self.calls.append(argv)
        if "shell" in argv:
            out = "".join(
                f"{size} {mtime} {path}\0" for path, size, mtime in self.rows()
            )
            return SimpleNamespace(returncode=0, stdout=out, stderr="")
        remote, target = argv[-2:]
        if self.fail:
            return SimpleNamespace(returncode=1, stdout="", stderr="device offline")
        dest = Path(target) / Path(remote).name
        dest.mkdir()
        for path, value in list(self.files.items()):
            if path.startswith(remote + "/"):
                p = dest / path[len(remote) + 1 :]
                p.parent.mkdir(parents=True, exist_ok=True)
                if not self.partial:
                    p.write_bytes(value)
        if self.change:
            self.files[remote + "/extra"] = b"changed during transfer"
            self.change = False
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    def source(self, root):
        source = AdbKsyncSource("adb", "TEST1234", root, runner=self.run)
        source._listing = self.rows()
        source.list_notebooks = lambda: [
            RemoteNotebook(N, "Journal", signature(source._listing))
        ]
        return source


@pytest.mark.parametrize("failure", ["fail", "partial", "change"])
def test_real_transport_failures_cannot_replace_good_archive(tmp_path, failure):
    cfg, initial = config(tmp_path), Source()
    assert sync_once(cfg, initial).status == "ok"
    folder = cfg.archive_dir / "Journal"
    before = {p.name: p.read_bytes() for p in folder.iterdir()}
    state = (cfg.state_dir / "state.json").read_bytes()
    transport = Transport()
    setattr(transport, failure, True)
    result = sync_once(cfg, transport.source(tmp_path / "cache"))
    assert result.status == "failed"
    assert {p.name: p.read_bytes() for p in folder.iterdir()} == before
    assert (cfg.state_dir / "state.json").read_bytes() == state


def test_transport_copies_complete_source_and_cleans_temporary_trees(tmp_path):
    transport = Transport()
    source = transport.source(tmp_path / "cache")
    blob, cover = source.pull_notebook(source.list_notebooks()[0])
    assert cover is None
    assert len(parse_notes(blob)[0].pages[0].strokes) == 1
    names = zipfile.ZipFile(io.BytesIO(blob)).namelist()
    assert any("/source/document/shape/" in n for n in names)
    assert not list((tmp_path / "cache").glob("boox-pull-*"))


def test_adb_timeout_is_bounded_and_descriptive(tmp_path):
    def timed_out(argv, **kwargs):
        raise subprocess.TimeoutExpired(argv, kwargs["timeout"])

    source = AdbKsyncSource("adb", "S", tmp_path, runner=timed_out, timeout=7)
    with pytest.raises(AdbError, match="7s"):
        source.list_notebooks()


def test_failed_tree_refresh_preserves_the_cache(tmp_path, monkeypatch):
    path = tmp_path / "note_tree" / "meta.json"
    path.parent.mkdir()
    original = json.dumps({"signature": "old", "notebooks": [[N, "Journal"]]})
    path.write_text(original)
    source = AdbKsyncSource("adb", "S", tmp_path)
    monkeypatch.setattr(source, "_pull_dir_tree", lambda: None)
    with pytest.raises(AdbError):
        source._note_tree("new")
    assert path.read_text() == original


def test_missing_database_wal_cannot_commit_an_incomplete_notebook_list(
    tmp_path, monkeypatch
):
    from boox_sync.adb import KSYNC_COUCH

    source = AdbKsyncSource("adb", "S", tmp_path / "cache")
    tree = tmp_path / "pull"
    db = tree / "user-NOTE_TREE.cblite2" / "db.sqlite3"
    db.parent.mkdir(parents=True)
    db.write_bytes(b"db")
    remote = KSYNC_COUCH + "/user-NOTE_TREE.cblite2/db.sqlite3"
    rows = [(remote, 2, "1"), (remote + "-wal", 4, "1")]
    monkeypatch.setattr(source, "_pull_dir_tree", lambda: tree)
    monkeypatch.setattr(source, "_query_rows", lambda roots: rows)
    with pytest.raises(AdbError, match="incomplete notebook database"):
        source._note_tree(signature(rows))
    assert not (source.cache_dir / "note_tree/meta.json").exists()


def test_five_previous_revisions_are_verified_and_restorable(tmp_path, capsys):
    cfg, source = config(tmp_path), Source()
    versions = []
    for i in range(8):
        source.x, source.sig = i * 10, str(i)
        versions.append(raw(x=source.x))
        # Use the exact transferred ZIP; regenerating it can cross a ZIP timestamp boundary.
        source.pull_notebook = lambda notebook, payload=versions[-1]: (payload, None)
        assert sync_once(cfg, source).status == "ok"
    revisions = list_revisions(cfg.archive_dir, cfg.device_serial, N)
    assert len(revisions) == 5
    assert [p.joinpath("notebook.note").read_bytes() for p in revisions] == list(
        reversed(versions[2:7])
    )
    config_file = tmp_path / "config.json"
    config_file.write_text(
        json.dumps(
            {
                "device_serial": cfg.device_serial,
                "archive_dir": str(cfg.archive_dir),
                "state_dir": str(cfg.state_dir),
            }
        )
    )
    output = tmp_path / "recovered.note"
    assert (
        main(
            [
                "restore",
                N,
                revisions[0].name,
                "-o",
                str(output),
                "--config",
                str(config_file),
            ]
        )
        == 0
    )
    assert output.read_bytes() == versions[6]
    assert (cfg.archive_dir / "Journal/Journal.note").read_bytes() == versions[7]


def test_suffix_collision_and_unowned_folders_are_preserved(tmp_path):
    state = empty_state()
    ids = [
        N,
        "cccccccc-cccc-cccc-cccc-cccccccccccc",
        "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb",
    ]
    for id, title in zip(ids, ["Journal", "Journal bbbbbbbb", "Journal"]):
        archive_notebook(
            tmp_path,
            build_note(note_id=id, title=title),
            device_serial="S",
            source_path="test",
            state=state,
        )
    entries = state["notes"].values()
    assert len({entry["archive_folder"] for entry in entries}) == 3
    for entry in entries:
        folder = tmp_path / entry["archive_folder"]
        assert (
            parse_notes((folder / f"{folder.name}.note").read_bytes())[0].note_id
            == entry["note_id"]
        )
    user = tmp_path / "Private"
    user.mkdir()
    (user / "Private.note").write_bytes(b"unowned")
    archive_notebook(
        tmp_path, raw("Private"), device_serial="S", source_path="test", state=state
    )
    assert (user / "Private.note").read_bytes() == b"unowned"


@pytest.mark.parametrize("title", ["Work/Notes", " A:B ", "e\u0301", ".", "ก" * 200])
def test_sanitized_titles_skip_identical_second_sync(tmp_path, title):
    cfg, source = config(tmp_path), Source(title)
    assert sync_once(cfg, source).status == "ok"
    assert sync_once(cfg, source).outcomes[0].skipped
    assert source.pulls == 1


@pytest.mark.parametrize("damage", ["note", "manifest", "state", "missing-state"])
def test_sync_recovers_damaged_files(tmp_path, damage):
    cfg, source = config(tmp_path), Source()
    expected = raw()
    # ZIP timestamps vary; recovery must preserve the exact source payload.
    source.pull_notebook = lambda notebook: (expected, None)
    assert sync_once(cfg, source).status == "ok"
    folder = cfg.archive_dir / "Journal"
    paths = {
        "note": folder / "Journal.note",
        "manifest": folder / "manifest.json",
        "state": cfg.state_dir / "state.json",
        "missing-state": cfg.state_dir / "state.json",
    }
    if damage == "missing-state":
        paths[damage].unlink()
    else:
        paths[damage].write_text("{")
    result = sync_once(cfg, source)
    assert result.status == "ok"
    assert (folder / "Journal.note").read_bytes() == expected
    assert load_state(cfg.state_dir)["notes"]
    assert not needs_render(folder)


def test_view_refuses_stale_pdf_when_source_is_corrupt(tmp_path):
    cfg, source = config(tmp_path), Source()
    sync_once(cfg, source)
    folder = cfg.archive_dir / "Journal"
    previous = (folder / "Journal.pdf").read_bytes()
    (folder / "Journal.note").write_bytes(b"broken")
    opened = []
    assert needs_render(folder)
    status, error, path = view_notebook(
        cfg.state_dir, cfg.archive_dir, N, opened.append
    )
    assert status == "failed" and "integrity" in error and path is None
    assert not opened
    assert (folder / "Journal.pdf").read_bytes() == previous


def test_explicit_export_failure_leaves_manifest_untouched(tmp_path):
    cfg, source = config(tmp_path), Source()
    sync_once(cfg, source)
    folder = cfg.archive_dir / "Journal"
    before = (folder / "manifest.json").read_bytes()
    (folder / "Journal.note").write_bytes(b"bad")
    assert (
        export_pdf(cfg.archive_dir, "Journal", output=tmp_path / "elsewhere.pdf").status
        == "failed"
    )
    assert (folder / "manifest.json").read_bytes() == before


def test_approximate_warning_survives_skipped_sync(tmp_path):
    cfg, source, menu = config(tmp_path), Source(pen=22), BooxMenu()
    for _ in range(2):
        report = sync_once(cfg, source)
        _apply_report(menu, report)
        assert menu.status_line == "Up to date, 1 approximate"
    assert source.pulls == 1 and report.outcomes[0].skipped


def test_atomic_replace_failure_preserves_old_bytes(tmp_path, monkeypatch):
    target = tmp_path / "state.json"
    target.write_bytes(b"old")

    def fail(*args):
        raise OSError("simulated interrupted commit")

    monkeypatch.setattr("boox_sync.storage.os.replace", fail)
    with pytest.raises(OSError):
        atomic_write(target, b"new")
    assert target.read_bytes() == b"old"
    assert list(tmp_path.iterdir()) == [target]


def test_lock_is_reentrant_but_excludes_another_worker(tmp_path):
    with archive_lock(tmp_path):
        with archive_lock(tmp_path):
            pass

        def competing():
            with pytest.raises(ArchiveBusy):
                with archive_lock(tmp_path):
                    pass

        with ThreadPoolExecutor(1) as worker:
            worker.submit(competing).result(timeout=2)


def test_lock_excludes_another_process(tmp_path):
    import sys

    with archive_lock(tmp_path):
        program = "from pathlib import Path; from boox_sync.storage import archive_lock; import sys\nwith archive_lock(Path(sys.argv[1])): pass"
        proc = subprocess.run(
            [sys.executable, "-c", program, str(tmp_path)],
            capture_output=True,
            text=True,
            timeout=10,
        )
    assert proc.returncode != 0 and "archive is busy" in proc.stderr


def test_two_batches_of_same_shape_draw_latest_once():
    files = {}
    for batch, stamp, x in [
        (B, 1, 10),
        ("99999999-9999-9999-9999-999999999999", 2, 90),
    ]:
        files.update(
            build_batch(
                P,
                batch,
                [
                    {
                        "shape_id": S,
                        "pen_type": 2,
                        "points": [(x, 10, 100), (x + 20, 20, 100)],
                    }
                ],
                stamp,
            )
        )
    strokes = parse_notes(assemble_note(N, "Edits", files))[0].pages[0].strokes
    assert len(strokes) == 1 and strokes[0].points[0][0] == 90


def test_missing_point_chunk_fails_instead_of_drawing_blank():
    files = build_batch(
        P,
        B,
        [{"shape_id": S, "pen_type": 2, "points": [(10, 10, 100), (30, 30, 100)]}],
        1,
    )
    files = {k: v for k, v in files.items() if not k.startswith("point/")}
    result = render_note(assemble_note(N, "Partial", files))
    assert result.status == "failed" and "missing points" in result.error


def test_tombstone_does_not_resurrect_old_points():
    files = build_batch(
        P,
        B,
        [{"shape_id": S, "pen_type": 2, "points": [(10, 10, 100), (30, 30, 100)]}],
        1,
    )
    # Explicit wire fields from the observed device structure: id, modified,
    # pen, state marker; no point reference in the replacement record.
    shape = (
        encode_bytes_field(1, S.encode())
        + encode_varint_field(3, 10)
        + encode_varint_field(12, 2)
        + b"\x78\x01"
    )
    inner = io.BytesIO()
    with zipfile.ZipFile(inner, "w") as z:
        z.writestr("record", encode_bytes_field(1, shape))
    files[f"shape/{P}#later#10.zip"] = inner.getvalue()
    note = parse_notes(assemble_note(N, "Erased", files))[0]
    assert note.pages[0].strokes == []
    assert not note.pages[0].reasons


def test_per_page_dimensions_and_source_metadata_survive_assembly():
    files = {"doc/original": b"metadata"}
    for index, (p, w, h) in enumerate([(P, 500, 800), ("other", 1000, 1400)]):
        files[f"page/{p}"] = build_virtual_page(
            p, index, index + 1, {"left": 0, "top": 0, "right": w, "bottom": h}
        )
    blob = assemble_note(N, "Mixed pages", files)
    assert blob == assemble_note(N, "Mixed pages", files)
    archive = zipfile.ZipFile(io.BytesIO(blob))
    assert archive.read(f"{N}/source/doc/original") == b"metadata"
    assert archive.read(f"{N}/source/page/{P}") == files[f"page/{P}"]
    result = render_note(blob)
    pages = PdfReader(io.BytesIO(result.pdf_bytes)).pages
    assert [(float(p.mediabox.width), float(p.mediabox.height)) for p in pages] == [
        (500, 800),
        (1000, 1400),
    ]


def test_source_backup_cannot_be_parsed_as_an_extra_notebook():
    blob = raw()
    buffer = io.BytesIO(blob)
    with zipfile.ZipFile(buffer, "a") as archive:
        header = archive.read(f"{N}/note/pb/note_info")
        archive.writestr(f"{N}/source/document/note/pb/note_info", header)
    assert len(parse_notes(buffer.getvalue())) == 1


def test_legacy_pages_use_creation_order_with_warning():
    files = {}
    for page, created in [("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa", 20), (P, 10)]:
        files.update(
            build_batch(
                page,
                B,
                [
                    {
                        "pen_type": 2,
                        "created": created,
                        "points": [(10, 10, 100), (20, 20, 100)],
                    }
                ],
                30,
            )
        )
    note = parse_notes(assemble_note(N, "Legacy order", files))[0]
    assert note.pages[0].page_id == P
    assert all("page-order" in p.reasons for p in note.pages)


def test_partial_page_metadata_does_not_drop_ink_or_reuse_other_page_size():
    files = {
        f"page/{P}": build_virtual_page(
            P, 0, 1, {"left": 0, "top": 0, "right": 500, "bottom": 800}
        )
    }
    other = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
    files.update(
        build_batch(
            other, B, [{"pen_type": 2, "points": [(10, 10, 100), (20, 20, 100)]}], 10
        )
    )
    result = render_note(assemble_note(N, "Partial metadata", files))
    pages = PdfReader(io.BytesIO(result.pdf_bytes)).pages
    assert [(float(p.mediabox.width), float(p.mediabox.height)) for p in pages] == [
        (500, 800),
        (1860, 2480),
    ]
    assert set(result.reasons) == {"page-size", "page-order"}


def test_explicit_missing_config_does_not_use_default_archive(tmp_path):
    with pytest.raises(ConfigError, match="does not exist"):
        load_config(tmp_path / "missing.json")


def test_thai_and_latin_text_use_embedded_shaped_fonts():
    text = "Hello สวัสดี ภาษาไทย 123\nน้ำ กิ กี่ กี้ กิ๊ กิ๋"
    result = render_note(
        build_note(
            note_id=N,
            title="Thai",
            strokes=[
                {
                    "pen_type": 6,
                    "text": text,
                    "style": {"textSize": 20},
                    "bbox": {"left": 10, "top": 20, "right": 480},
                }
            ],
        )
    )
    assert result.status == "ok"
    reader = PdfReader(io.BytesIO(result.pdf_bytes))
    content = reader.pages[0].get_contents().get_data()
    assert b"/ActualText" in content
    extracted = reader.pages[0].extract_text()
    assert "■" not in extracted and "สวัสดี" in extracted
    fonts = reader.pages[0]["/Resources"]["/Font"].values()
    assert any("NotoSansThai" in str(f.get_object().get("/BaseFont")) for f in fonts)


def test_unsupported_script_fails_explicitly():
    result = render_note(
        build_note(
            note_id=N, title="Unsupported", strokes=[{"pen_type": 6, "text": "漢字"}]
        )
    )
    assert result.status == "failed" and "unsupported text character" in result.error


def test_long_fleece_string_uses_varint_length():
    # Independent known varint for 300 bytes, beyond the old single-byte path.
    from boox_sync.fleece import decode_fleece

    assert decode_fleece(b"\x4f\xac\x02" + b"a" * 300 + b"\x00\x80\x98") == "a" * 300


class ManualExecutor:
    def __init__(self):
        self.jobs = []

    def submit(self, fn, *args):
        future = Future()
        self.jobs.append((future, fn, args))
        return future

    def complete(self):
        future, fn, args = self.jobs.pop(0)
        try:
            future.set_result(fn(*args))
        except Exception as exc:
            future.set_exception(exc)


def test_background_runner_never_blocks_or_overlaps_and_recovers(tmp_path):
    executor, menu, calls = ManualExecutor(), BooxMenu(), []
    attached = [True]

    def sync(cfg):
        calls.append("sync")
        raise RuntimeError("listing failed")

    runner = BackgroundRunner(
        config(tmp_path), menu, lambda: attached[0], sync, executor, clock=lambda: 10
    )
    runner.tick()
    assert len(executor.jobs) == 1 and not calls  # only schedules presence work
    executor.complete()
    runner.tick()
    assert menu.status_line == "Syncing" and not menu.sync_enabled
    runner.tick()
    runner.sync_now()
    assert len(executor.jobs) == 1
    executor.complete()
    runner.tick()
    assert (
        menu.status_line == "Last sync failed" and "listing failed" in menu.last_error
    )
    assert calls == ["sync"]


@pytest.mark.parametrize(
    "name,value",
    [
        ("poll_seconds", 0),
        ("keep_revisions", -1),
        ("adb_timeout_seconds", "10"),
        ("render_after_sync", "yes"),
        ("remote_roots", "/wrong-type"),
    ],
)
def test_invalid_config_is_rejected_before_writes(tmp_path, name, value):
    cfg = config(tmp_path)
    setattr(cfg, name, value)
    with pytest.raises(ConfigError):
        sync_once(cfg, Source())
    assert not cfg.archive_dir.exists()


def test_bad_config_cli_has_actionable_error_without_traceback(tmp_path, capsys):
    path = tmp_path / "bad.json"
    path.write_text("{")
    assert main(["sync", "--config", str(path)]) == 1
    assert "Cannot read config" in capsys.readouterr().out
