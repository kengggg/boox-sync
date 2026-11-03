"""Ksync tests build synthetic staging trees and read them back."""

import sqlite3

import pytest
from fleece_fixture import fleece, notebook

from boox_sync.ksync import (
    NotebookMeta,
    assemble_note,
    build_batch,
    build_page_model,
    build_virtual_page,
    read_note_tree,
)
from boox_sync.note import parse_notes
from boox_sync.render import _rgba, render_note

NOTE_ID = "11111111111141118111111111111111"
PAGE_1 = "22222222222242228222222222222222"
PAGE_2 = "44444444-4444-4444-8444-444444444444"
RECT = {
    "left": 0.0,
    "top": 0.0,
    "right": 1860.0,
    "bottom": 2480.0,
    "empty": False,
    "stability": 0,
}


def _make_tree(tmp_path, rows):
    path = tmp_path / "tree" / "db.sqlite3"
    path.parent.mkdir(parents=True)
    db = sqlite3.connect(path)
    db.execute(
        "CREATE TABLE kv_default (key TEXT PRIMARY KEY, sequence INTEGER,"
        " flags INTEGER, version BLOB, body BLOB, extra BLOB, expiration INTEGER)"
    )
    db.executemany("INSERT INTO kv_default (key, body) VALUES (?, ?)", rows)
    db.commit()
    db.close()
    return path


def _body(title):
    return fleece(notebook(title))


def test_read_note_tree_lists_notebooks_and_skips_companions(tmp_path):
    """Companion sync documents without the Local marker are not notebooks."""
    rows = [
        (NOTE_ID, _body("Sample Project")),
        ("bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb", _body("Notepad1")),
        ("companion0000000000000000000000", fleece({"documentUniqueId": "companion"})),
    ]
    metas = read_note_tree(_make_tree(tmp_path, rows))
    assert metas == [
        NotebookMeta(note_id=NOTE_ID, title="Sample Project"),
        NotebookMeta(note_id="bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb", title="Notepad1"),
    ]


def test_read_note_tree_reads_long_titles(tmp_path):
    """Titles longer than 14 bytes use the long string form."""
    rows = [(NOTE_ID, _body("iRO and Modern Warehouse"))]
    metas = read_note_tree(_make_tree(tmp_path, rows))
    assert metas[0].title == "iRO and Modern Warehouse"


def test_read_note_tree_untitled_notebook(tmp_path):
    """An empty title becomes Untitled instead of being dropped."""
    rows = [(NOTE_ID, _body(""))]
    metas = read_note_tree(_make_tree(tmp_path, rows))
    assert metas[0].title == "Untitled"


def _strokes(x_pos, count, pen=21):
    return [
        {
            "shape_id": f"55555555-5555-5555-{x_pos:04d}-55555555{i:04d}",
            "pen_type": pen,
            "points": [(x_pos + i * 5, 10, 100), (x_pos + i * 5 + 4, 60, 100)],
            "thickness": 2,
        }
        for i in range(count)
    ]


def _two_page_files():
    files = {}
    files.update(
        build_batch(
            PAGE_1,
            "11111111-1111-1111-1111-111111111111",
            _strokes(10, 2),
            1793600000000,
        )
    )
    files.update(
        build_batch(
            PAGE_1,
            "22222222-2222-2222-2222-222222222222",
            _strokes(100, 1),
            1793600900000,
        )
    )
    files.update(
        build_batch(
            PAGE_2,
            "33333333-3333-3333-3333-333333333333",
            _strokes(10, 1, pen=2),
            1793601000000,
        )
    )
    files[f"page/{PAGE_1}"] = build_virtual_page(PAGE_1, 0, 1793599000000, RECT)
    files[f"page/{PAGE_2}"] = build_virtual_page(
        PAGE_2, 1, 1793599001000, RECT, layout="horizontal_line"
    )
    files[f"pagemodel/{PAGE_1}"] = build_page_model(
        PAGE_1, [{"id": 0, "lock": False, "show": True}], 1793599000000, RECT
    )
    files[f"pagemodel/{PAGE_2}"] = build_page_model(
        PAGE_2, [{"id": 0, "lock": False, "show": False}], 1793599001000, RECT
    )
    files[f"template/{PAGE_2}.template_json"] = (
        b'{"properties":{"spacing":38,"resourceAttr":{"resName":"horizontal_line"},"shaderRect":'
        + b'{"left":0,"top":0,"right":1860,"bottom":2480}}}'
    )
    return files


def test_assemble_note_produces_a_readable_note():
    """The assembled zip parses as a modern .note with merged batches."""
    raw = assemble_note(NOTE_ID, "Sample Project", _two_page_files())
    notes = parse_notes(raw)
    assert len(notes) == 1
    note = notes[0]
    assert note.note_id == NOTE_ID
    assert note.title == "Sample Project"
    assert [page.page_id for page in note.pages] == [PAGE_1, PAGE_2]
    assert len(note.pages[0].strokes) == 3
    assert len(note.pages[1].strokes) == 1
    assert note.width == 1860.0
    assert note.height == 2480.0
    assert note.pages[0].template is None
    assert note.pages[1].template["properties"]["spacing"] == 38


def test_assemble_note_renders_in_order():
    """The rendered PDF keeps page order and hides the hidden layer."""
    raw = assemble_note(NOTE_ID, "Sample Project", _two_page_files())
    result = render_note(raw)
    assert result.status == "ok"
    assert result.page_count == 2
    assert result.page_size_source == "metadata"


def test_assemble_note_ignores_stash_and_unknown_keys():
    """stash entries and metadata keys never enter the zip content."""
    files = _two_page_files()
    files["stash/archivedShape/1/x.zip"] = b"PK\x03\x04decoy"
    files["doc/11111111"] = b"doc-pb"
    raw = assemble_note(NOTE_ID, "Sample Project", files)
    assert b"decoy" not in raw
    notes = parse_notes(raw)
    assert len(notes[0].pages) == 2


def test_rgba_masks_wide_ksync_colors():
    """64-bit ksync colors carry the ARGB in the low 32 bits."""
    red, green, blue, alpha = _rgba(0xFFFFFFFFFFFF6163)
    assert (
        round(red * 255),
        round(green * 255),
        round(blue * 255),
        round(alpha * 255),
    ) == (255, 97, 99, 255)
    red, green, blue, alpha = _rgba(0xFF000000)
    assert (red, green, blue, alpha) == (0.0, 0.0, 0.0, 1.0)


def test_assemble_note_keeps_a_template_stored_under_json():
    """A device path template/json/<page> is not nested a second time."""
    files = _two_page_files()
    payload = files.pop(f"template/{PAGE_2}.template_json")
    files[f"template/json/{PAGE_2}.template_json"] = payload
    raw = assemble_note(NOTE_ID, "Sample Project", files)
    assert b"template/json/json/" not in raw
    note = parse_notes(raw)[0]
    assert note.pages[1].template["properties"]["spacing"] == 38


def test_assemble_note_keeps_one_embedded_picture():
    """A single PNG under resource/ is drawn as the page background."""
    files = _two_page_files()
    files["resource/data/pic.png"] = (
        b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
        b"\x08\x02\x00\x00\x00\x90wS\xde\x00\x00\x00\x0cIDATx\x9cc\xf8\xff\xff?"
        b"\x00\x05\xfe\x02\xfe\xa0\xd3\xb5\x00\x00\x00\x00IEND\xaeB`\x82"
    )
    raw = assemble_note(NOTE_ID, "Sample Project", files)
    note = parse_notes(raw)[0]
    assert note.images
    result = render_note(raw)
    assert result.status == "approximate"
    assert "image-placement" in result.reasons
    assert result.pdf_bytes


def test_assemble_note_without_page_records():
    """Missing page metadata is disclosed even when ink fits the fallback size."""
    files = build_batch(
        PAGE_1, "11111111-1111-1111-1111-111111111111", _strokes(10, 1), 1793600000000
    )
    raw = assemble_note(NOTE_ID, "No Pages", files)
    result = render_note(raw)
    assert result.status == "approximate"
    assert result.page_size_source == "default"
    assert "page-size" in result.reasons


def test_tree_uses_lifecycle_status_even_when_source_bytes_remain(tmp_path):
    rows = [
        ("live", fleece(notebook("Current"))),
        ("deleted", fleece(notebook("Deleted", status=0, enabled=True))),
        ("new_deleted", fleece(notebook("New firmware deletion", status=0))),
        ("forced", fleece(notebook("Force deleted", forceDelete=True))),
        (
            "legacy",
            fleece({"source": "Local", "type": 1, "title": "Legacy", "enabled": False}),
        ),
    ]
    assert {
        n.note_id: n.active for n in read_note_tree(_make_tree(tmp_path, rows))
    } == {
        "live": True,
        "deleted": False,
        "new_deleted": False,
        "forced": False,
        "legacy": False,
    }


def test_tree_resolves_shared_keys_and_does_not_read_an_old_title(tmp_path):
    keys = ["title", "source", "type", "status"]
    old = fleece(notebook("Stale title"))
    body = fleece(notebook("Current title", status=0), keys, prefix=old)
    path = _make_tree(tmp_path, [(NOTE_ID, body)])
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE kv_info (key TEXT, body BLOB)")
        db.execute("INSERT INTO kv_info VALUES ('SharedKeys', ?)", (fleece(keys),))
    assert read_note_tree(path) == [NotebookMeta(NOTE_ID, "Current title", False)]


@pytest.mark.parametrize("status", [None, "0", True, 2, -1])
def test_unknown_lifecycle_does_not_produce_an_empty_inventory(tmp_path, status):
    path = _make_tree(tmp_path, [(NOTE_ID, fleece(notebook(status=status)))])
    with pytest.raises(ValueError, match="lifecycle"):
        read_note_tree(path)


def test_deleted_couchbase_tombstone_can_have_no_body(tmp_path):
    path = _make_tree(tmp_path, [(NOTE_ID, _body("Live"))])
    with sqlite3.connect(path) as db:
        db.execute("UPDATE kv_default SET flags=1, body=NULL")
    assert read_note_tree(path) == [NotebookMeta(NOTE_ID, "Untitled", False)]


def test_invalid_body_fails_the_complete_inventory(tmp_path):
    path = _make_tree(tmp_path, [(NOTE_ID, _body("Valid")), ("bad", b"broken")])
    with pytest.raises(ValueError):
        read_note_tree(path)
