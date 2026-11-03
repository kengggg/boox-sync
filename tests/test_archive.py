"""Archive tests drive archive_notebook with synthetic notes."""

import hashlib
import io
import json
import unicodedata
import zipfile
import pytest
from boox_sync.render import RENDERER_VERSION
from datetime import datetime, timezone

from boox_sync.archive import (
    MANIFEST_KEYS,
    archive_notebook,
    empty_state,
    export_pdf,
    sanitize_title,
)
from boox_sync.note import build_note

NOTE_A = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
NOTE_B = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
WHEN = datetime(2026, 11, 2, 12, 0, 0, tzinfo=timezone.utc)
SERIAL = "TEST1234"


def _clock():
    return WHEN


def _note(note_id, title, x_pos=10, corrupt=False):
    return build_note(
        note_id=note_id,
        title=title,
        width=500,
        height=800,
        corrupt_points=corrupt,
        strokes=[
            {
                "pen_type": 2,
                "points": [(x_pos, 10, 100), (x_pos + 40, 70, 100)],
                "thickness": 2,
            }
        ],
    )


def _archive(root, raw, source_path, state, cover=None, signature=None, title=None):
    return archive_notebook(
        root,
        raw,
        device_serial=SERIAL,
        source_path=source_path,
        state=state,
        cover=cover,
        signature=signature,
        title=title,
        clock=_clock,
    )


def _manifest(folder):
    return json.loads((folder / "manifest.json").read_text())


def test_archive_writes_note_manifest_and_cover_without_pdf(tmp_path):
    """One pulled notebook lands as .note + manifest + cover, no PDF yet."""
    root = tmp_path / "BOOX"
    state = empty_state()
    raw = _note(NOTE_A, "Journal")
    written = _archive(root, raw, "ksync:aaa", state, cover=b"png", signature="sig1")
    folder = root / "Journal"
    assert written[0].folder_name == "Journal"
    assert written[0].status == "ok"
    assert (folder / "Journal.note").read_bytes() == raw
    assert (folder / "cover.png").read_bytes() == b"png"
    assert not (folder / "Journal.pdf").exists()
    manifest = _manifest(folder)
    assert set(manifest) == set(MANIFEST_KEYS)
    assert manifest["note_id"] == NOTE_A
    assert manifest["title"] == "Journal"
    assert manifest["device_serial"] == SERIAL
    assert manifest["content_sha256"] == hashlib.sha256(raw).hexdigest()
    assert manifest["synced_at"] == "2026-11-02T12:00:00.000000+00:00"
    assert manifest["source_path"] == "ksync:aaa"
    assert manifest["render_status"] == "pending"
    assert manifest["approximate_reasons"] == []
    assert manifest["render_error"] is None
    assert manifest["page_count"] is None
    assert manifest["page_size_source"] is None
    assert manifest["later_copy"] is None
    assert manifest["renderer_version"] == RENDERER_VERSION


def test_export_pdf_renders_and_updates_the_manifest(tmp_path):
    """Rendering on demand writes the PDF and fills the render fields."""
    root = tmp_path / "BOOX"
    state = empty_state()
    raw = _note(NOTE_A, "Journal")
    _archive(root, raw, "ksync:aaa", state)
    folder = root / "Journal"
    result = export_pdf(root, "Journal", clock=_clock)
    assert result.status == "ok"
    assert result.pdf_bytes is not None
    assert (folder / "Journal.pdf").read_bytes() == result.pdf_bytes
    manifest = _manifest(folder)
    assert manifest["render_status"] == "ok"
    assert manifest["page_count"] == 1
    assert manifest["page_size_source"] == "metadata"
    assert manifest["renderer_version"] == RENDERER_VERSION


def test_export_pdf_to_output_leaves_the_archive_alone(tmp_path):
    """An explicit output path does not write into the archive folder."""
    root = tmp_path / "BOOX"
    _archive(root, _note(NOTE_A, "Journal"), "ksync:aaa", empty_state())
    target = tmp_path / "elsewhere" / "Journal.pdf"
    result = export_pdf(root, "Journal", output=target)
    assert target.read_bytes() == result.pdf_bytes
    assert not (root / "Journal" / "Journal.pdf").exists()
    assert _manifest(root / "Journal")["render_status"] == "pending"


def test_export_pdf_failure_keeps_the_previous_pdf(tmp_path):
    """A broken .note fails the export and keeps the last good PDF."""
    root = tmp_path / "BOOX"
    state = empty_state()
    _archive(root, _note(NOTE_A, "Journal"), "ksync:aaa", state)
    export_pdf(root, "Journal", clock=_clock)
    good_pdf = (root / "Journal" / "Journal.pdf").read_bytes()
    (root / "Journal" / "Journal.note").write_bytes(b"not a zip")
    result = export_pdf(root, "Journal", clock=_clock)
    assert result.status == "failed"
    assert result.pdf_bytes is None
    assert (root / "Journal" / "Journal.pdf").read_bytes() == good_pdf
    manifest = _manifest(root / "Journal")
    assert manifest["render_status"] == "failed"
    assert manifest["page_count"] == 0
    assert manifest["render_error"]


def test_resync_keeps_the_pdf_and_marks_new_bytes_pending(tmp_path):
    """New note bytes keep the last PDF and mark it stale until the next render."""
    root = tmp_path / "BOOX"
    state = empty_state()
    _archive(root, _note(NOTE_A, "Journal"), "ksync:aaa", state, signature="s1")
    export_pdf(root, "Journal", clock=_clock)
    previous = (root / "Journal" / "Journal.pdf").read_bytes()
    _archive(
        root, _note(NOTE_A, "Journal", x_pos=80), "ksync:aaa", state, signature="s2"
    )
    manifest = _manifest(root / "Journal")
    assert manifest["render_status"] == "pending"
    assert manifest["rendered_sha256"] is None
    assert (root / "Journal" / "Journal.pdf").read_bytes() == previous


def test_same_title_replaces_raw(tmp_path):
    """The same note id and title replaces the raw file."""
    root = tmp_path / "BOOX"
    state = empty_state()
    first = _note(NOTE_A, "Journal", x_pos=10)
    second = _note(NOTE_A, "Journal", x_pos=80)
    _archive(root, first, "ksync:aaa", state)
    _archive(root, second, "ksync:aaa", state)
    assert (root / "Journal" / "Journal.note").read_bytes() == second
    assert [p for p in root.iterdir() if not p.name.startswith(".")] == [
        root / "Journal"
    ]


def test_rename_leaves_the_old_folder_and_sets_later_copy(tmp_path):
    """A new title creates a folder and records later_copy on the old one."""
    root = tmp_path / "BOOX"
    state = empty_state()
    first = _note(NOTE_A, "Journal", x_pos=10)
    renamed = _note(NOTE_A, "Journal 2026", x_pos=10)
    _archive(root, first, "ksync:aaa", state)
    _archive(root, renamed, "ksync:aaa", state)
    assert (root / "Journal" / "Journal.note").read_bytes() == first
    assert _manifest(root / "Journal")["later_copy"] == "Journal 2026"
    assert (root / "Journal 2026" / "Journal 2026.note").read_bytes() == renamed
    assert _manifest(root / "Journal 2026")["later_copy"] is None


def test_different_note_id_gets_a_suffixed_folder(tmp_path):
    """A second notebook with the same title does not change the first folder."""
    root = tmp_path / "BOOX"
    state = empty_state()
    first = _note(NOTE_A, "Shared", x_pos=10)
    second = _note(NOTE_B, "Shared", x_pos=30)
    _archive(root, first, "ksync:aaa", state)
    before_note = (root / "Shared" / "Shared.note").read_bytes()
    before_manifest = (root / "Shared" / "manifest.json").read_bytes()
    _archive(root, second, "ksync:bbb", state)
    suffix = NOTE_B.replace("-", "")[:8]
    other = root / f"Shared {suffix}"
    assert (other / f"Shared {suffix}.note").read_bytes() == second
    assert (root / "Shared" / "Shared.note").read_bytes() == before_note
    assert (root / "Shared" / "manifest.json").read_bytes() == before_manifest


def test_title_sanitization_and_multi_note_zip(tmp_path):
    """Titles are NFC folder names, and one zip becomes one folder per note id."""
    assert sanitize_title(" /A:B\x00 ") == "AB"
    assert sanitize_title(" ..Hello.. ") == "Hello"
    assert sanitize_title("...") == "Untitled"
    nfd = "e\u0301"
    assert sanitize_title(nfd) == unicodedata.normalize("NFC", nfd)
    raw = _note(NOTE_A, nfd)
    state = empty_state()
    root = tmp_path / "BOOX"
    _archive(root, raw, "ksync:aaa", state)
    assert (root / "é" / "é.note").read_bytes() == raw
    alpha = build_note(
        note_id=NOTE_A,
        title="Alpha",
        width=500,
        height=800,
        strokes=[
            {
                "pen_type": 6,
                "text": "Alpha page",
                "bbox": {"left": 10, "top": 10, "right": 200, "bottom": 40},
            }
        ],
    )
    beta = build_note(
        note_id=NOTE_B,
        title="Beta",
        width=500,
        height=800,
        strokes=[
            {
                "pen_type": 6,
                "text": "Beta page",
                "bbox": {"left": 10, "top": 10, "right": 200, "bottom": 40},
            }
        ],
    )
    merged = io.BytesIO()
    with zipfile.ZipFile(merged, "w") as dest:
        for blob in (alpha, beta):
            with zipfile.ZipFile(io.BytesIO(blob)) as src:
                for name in src.namelist():
                    dest.writestr(name, src.read(name))
    combined = merged.getvalue()
    written = _archive(root, combined, "ksync:ccc", empty_state())
    assert {item.folder_name for item in written} == {"Alpha", "Beta"}
    for name in ("Alpha", "Beta"):
        assert (root / name / f"{name}.note").read_bytes() == combined


def test_broken_zip_is_quarantined_without_creating_a_notebook(tmp_path):
    root = tmp_path / "BOOX"
    state = empty_state()
    with pytest.raises(ValueError, match="incomplete or unreadable"):
        _archive(root, b"garbage", "ksync:zzz", state, signature="s", title="Broken")
    rejected = list((root / ".rejected").glob("*.note"))
    assert len(rejected) == 1 and rejected[0].read_bytes() == b"garbage"
    assert not (root / "Broken").exists()
    assert state == empty_state()
