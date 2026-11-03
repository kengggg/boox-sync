"""Render tests drive render_note on synthetic modern .note zips."""

import io
import re
import zipfile
import pytest

from pypdf import PdfReader

from boox_sync.archive import archive_notebook, empty_state, export_pdf
from boox_sync.note import build_note
from boox_sync.render import DEFAULT_PAGE, pen_width, render_note

NOTE_ID = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"


def _pdf(data: bytes) -> PdfReader:
    return PdfReader(io.BytesIO(data))


def _content(data: bytes) -> bytes:
    chunks = []
    for page in _pdf(data).pages:
        contents = page.get_contents()
        if contents is None:
            continue
        pieces = contents if isinstance(contents, list) else [contents]
        chunks.extend(piece.get_data() for piece in pieces)
    return b"".join(chunks)


def _ballpoint(**kwargs) -> bytes:
    strokes = kwargs.pop(
        "strokes",
        [{"pen_type": 2, "points": [(10, 20, 1000), (80, 90, 1000)], "thickness": 2}],
    )
    return build_note(
        note_id=NOTE_ID,
        title="Journal",
        strokes=strokes,
        width=500,
        height=800,
        **kwargs,
    )


def test_ballpoint_page_matches_fixture_size_and_draws_a_path():
    """A one-page ballpoint note renders one page with a drawn path."""
    raw = _ballpoint()
    result = render_note(raw)
    assert result.status == "ok"
    assert result.reasons == []
    assert result.page_count == 1
    assert result.page_size_source == "metadata"
    reader = _pdf(result.pdf_bytes)
    assert len(reader.pages) == 1
    box = reader.pages[0].mediabox
    assert float(box.width) == 500
    assert float(box.height) == 800
    content = _content(result.pdf_bytes)
    assert b" m" in content and b" l" in content


def test_text_alignment_uses_the_bounding_box():
    """alignType 0, 1, and 2 place the text on the left, center, and right of the box."""
    from reportlab.pdfbase.pdfmetrics import stringWidth

    text = "Hello note"
    size = 18
    left, right, top = 100, 400, 50
    text_width = stringWidth(text, "Helvetica", size)
    expected_x = {
        0: left,
        1: left + ((right - left) - text_width) / 2,
        2: right - text_width,
    }
    expected_y = 800 - top - size
    for align, origin in expected_x.items():
        raw = build_note(
            note_id=NOTE_ID,
            title="Journal",
            width=500,
            height=800,
            strokes=[
                {
                    "pen_type": 6,
                    "text": text,
                    "style": {"alignType": align, "textSize": size},
                    "bbox": {"left": left, "top": top, "right": right, "bottom": 90},
                }
            ],
        )
        result = render_note(raw)
        assert result.status == "ok"
        positions = [
            (float(x_pos), float(y_pos))
            for x_pos, y_pos in re.findall(
                rb"([0-9.]+) ([0-9.]+) Tm", _content(result.pdf_bytes)
            )
        ]
        assert positions
        assert abs(positions[-1][0] - origin) < 0.01
        assert abs(positions[-1][1] - expected_y) < 0.01


def test_text_box_extracts_the_fixture_string():
    """Typed text in the note is the PDF text."""
    raw = build_note(
        note_id=NOTE_ID,
        title="Journal",
        width=500,
        height=800,
        strokes=[
            {
                "pen_type": 6,
                "text": "Hello note",
                "bbox": {"left": 40, "top": 50, "right": 240, "bottom": 90},
            }
        ],
    )
    result = render_note(raw)
    assert result.status == "ok"
    assert _pdf(result.pdf_bytes).pages[0].extract_text().strip() == "Hello note"


def test_charcoal_and_unknown_pen_are_approximate():
    """Charcoal and an unknown pen still produce a PDF and name the reason."""
    charcoal = _ballpoint(
        strokes=[
            {"pen_type": 22, "points": [(10, 20, 2000), (40, 60, 3000)], "thickness": 3}
        ]
    )
    unknown = _ballpoint(
        strokes=[
            {"pen_type": 99, "points": [(10, 20, 100), (40, 60, 100)], "thickness": 2}
        ]
    )
    charcoal_result = render_note(charcoal)
    unknown_result = render_note(unknown)
    assert charcoal_result.pdf_bytes
    assert charcoal_result.status == "approximate"
    assert charcoal_result.reasons == ["charcoal"]
    assert unknown_result.pdf_bytes
    assert unknown_result.status == "approximate"
    assert unknown_result.reasons == ["pen-99"]


def test_non_modern_zip_fails():
    """Bytes that are not a modern .note zip report failed and have no PDF."""
    plain = render_note(b"this is not a zip")
    assert plain.status == "failed"
    assert plain.pdf_bytes is None
    assert plain.page_count == 0
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("readme.txt", b"hello")
    wrapped = render_note(buffer.getvalue())
    assert wrapped.status == "failed"
    assert wrapped.pdf_bytes is None


def test_page_size_prefers_metadata_then_template_then_default():
    """Page size comes from the canvas, then the template rect, then 1860 by 2480."""
    template = {
        "properties": {
            "resourceAttr": {"resName": "template/ic_horizontal_line_24"},
            "spacing": 40,
            "shaderRect": {"left": 0, "right": 400, "top": 0, "bottom": 600},
        }
    }
    metadata = _ballpoint(template=template)
    from_metadata = render_note(metadata)
    assert from_metadata.page_size_source == "metadata"
    assert float(_pdf(from_metadata.pdf_bytes).pages[0].mediabox.width) == 500
    template_only = build_note(
        note_id=NOTE_ID,
        title="Journal",
        width=None,
        height=None,
        template=template,
        strokes=[{"pen_type": 2, "points": [(10, 10, 10), (20, 20, 10)]}],
    )
    from_template = render_note(template_only)
    assert from_template.status == "ok"
    assert from_template.page_size_source == "template"
    box = _pdf(from_template.pdf_bytes).pages[0].mediabox
    assert float(box.width) == 400
    assert float(box.height) == 600
    fallback = build_note(
        note_id=NOTE_ID,
        title="Journal",
        width=None,
        height=None,
        strokes=[{"pen_type": 2, "points": [(10, 10, 10), (20, 20, 10)]}],
    )
    from_default = render_note(fallback)
    assert from_default.status == "approximate"
    assert from_default.page_size_source == "default"
    assert from_default.reasons == ["page-size"]
    box = _pdf(from_default.pdf_bytes).pages[0].mediabox
    assert float(box.width) == DEFAULT_PAGE[0]
    assert float(box.height) == DEFAULT_PAGE[1]
    overflow = build_note(
        note_id=NOTE_ID,
        title="Journal",
        width=None,
        height=None,
        strokes=[{"pen_type": 2, "points": [(10, 10, 10), (2000, 3000, 10)]}],
    )
    grown = render_note(overflow)
    assert grown.status == "approximate"
    assert "page-size" in grown.reasons
    grown_box = _pdf(grown.pdf_bytes).pages[0].mediabox
    assert float(grown_box.width) >= 2000
    assert float(grown_box.height) >= 3000


def test_stash_text_is_not_rendered():
    """Undo history under stash/ is not drawn into the PDF."""
    raw = build_note(
        note_id=NOTE_ID,
        title="Journal",
        width=500,
        height=800,
        stash_text="STASH_SECRET",
        strokes=[
            {
                "pen_type": 6,
                "text": "Hello note",
                "bbox": {"left": 40, "top": 50, "right": 240, "bottom": 90},
            }
        ],
    )
    result = render_note(raw)
    text = _pdf(result.pdf_bytes).pages[0].extract_text()
    assert "Hello note" in text
    assert "STASH_SECRET" not in text


def _line_widths(data: bytes) -> list[float]:
    content = _content(data)
    return [float(match) for match in re.findall(rb"([0-9.]+) w", content)]


def _geometry(kind: str, subtype: str, coords: list) -> dict:
    return {
        "featureCollection": {
            "features": [
                {
                    "type": "Feature",
                    "geometry": {"type": kind, "coordinates": coords},
                    "properties": {"subType": subtype},
                }
            ]
        }
    }


def _png_bytes() -> bytes:
    import struct
    import zlib

    def chunk(tag: bytes, data: bytes) -> bytes:
        return (
            struct.pack(">I", len(data))
            + tag
            + data
            + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
        )

    ihdr = struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", ihdr)
        + chunk(b"IDAT", zlib.compress(b"\x00\xff\x00\x00"))
        + chunk(b"IEND", b"")
    )


def _jpeg_bytes() -> bytes:
    from PIL import Image

    buffer = io.BytesIO()
    Image.new("RGB", (2, 2), (20, 40, 60)).save(buffer, format="JPEG")
    payload = buffer.getvalue()
    assert payload.startswith(b"\xff\xd8")
    return payload


def test_exact_pens_templates_and_images_render_ok():
    """Pens, geometry, templates, and images listed as exact stay ok."""
    stroke = {"points": [(10, 20, 2000), (80, 90, 1800)], "thickness": 3}
    cases = [
        {"pen_type": 5, **stroke},
        {"pen_type": 15, **stroke},
        {"pen_type": 21, **stroke},
        {"pen_type": 37, "points": [(10, 40, 0), (90, 40, 0)], "thickness": 2},
        {
            "pen_type": 40,
            "thickness": 2,
            "extra": _geometry("LineString", "", [[10, 20], [80, 90]]),
        },
        {
            "pen_type": 40,
            "thickness": 2,
            "extra": _geometry("DirectionLine", "", [[10, 20], [80, 90]]),
        },
        {
            "pen_type": 40,
            "thickness": 2,
            "extra": _geometry("Polygon", "", [[10, 20], [80, 20], [80, 90], [10, 90]]),
        },
        {
            "pen_type": 40,
            "thickness": 2,
            "extra": _geometry("MultiPoint", "Oval", [[10, 20], [80, 90]]),
        },
    ]
    for row in cases:
        result = render_note(_ballpoint(strokes=[row]))
        assert result.status == "ok", row["pen_type"]
        assert result.reasons == []
        assert result.page_count == 1
    ruled = {
        "properties": {
            "resourceAttr": {"resName": "template/ic_horizontal_line_24"},
            "spacing": 40,
            "shaderRect": {"left": 0, "right": 500, "top": 0, "bottom": 800},
        }
    }
    dotted = {
        "properties": {
            "resourceAttr": {"resName": "template/ic_dot_grid"},
            "spacing": 40,
            "shaderRect": {"left": 0, "right": 500, "top": 0, "bottom": 800},
        }
    }
    for template, mark in ((ruled, b" l"), (dotted, b" c")):
        result = render_note(_ballpoint(strokes=[], template=template))
        assert result.status == "ok"
        assert mark in _content(result.pdf_bytes)
    for payload in (_png_bytes(), _jpeg_bytes()):
        result = render_note(_ballpoint(background=("bg1", payload)))
        assert result.status == "ok"
        assert result.reasons == []
        assert result.pdf_bytes


def test_fountain_and_marker_segment_widths_follow_pressure():
    """Fountain and marker widths match the spec formula within 0.01 pt."""
    thickness = 4.0
    pressure = 1000
    sample = pressure / 4095
    expected = {
        5: max(0.5, thickness * 1.37 * (sample**0.59)),
        21: max(0.5, thickness * 2.35 * (sample**0.43)),
    }
    for pen_type, width in expected.items():
        result = render_note(
            _ballpoint(
                strokes=[
                    {
                        "pen_type": pen_type,
                        "points": [(10, 20, pressure), (80, 90, pressure)],
                        "thickness": thickness,
                    }
                ]
            )
        )
        assert result.status == "ok"
        assert any(
            abs(found - width) < 0.01 for found in _line_widths(result.pdf_bytes)
        )
    zero = render_note(
        _ballpoint(
            strokes=[
                {
                    "pen_type": 5,
                    "points": [(10, 20, 0), (80, 90, 0)],
                    "thickness": thickness,
                }
            ]
        )
    )
    assert any(abs(found - 0.5) < 0.01 for found in _line_widths(zero.pdf_bytes))
    assert pen_width(5, 3.5, None) == 3.5
    assert pen_width(21, 2.25, None) == 2.25


def test_calligraphy_and_other_geometry_are_approximate():
    """Calligraphy and wave geometry keep a PDF and name the reason."""
    for pen_type in (60, 61):
        result = render_note(
            _ballpoint(
                strokes=[
                    {
                        "pen_type": pen_type,
                        "points": [(10, 20, 1000), (40, 60, 2000)],
                        "thickness": 3,
                    }
                ]
            )
        )
        assert result.status == "approximate"
        assert result.reasons == ["calligraphy"]
        assert result.pdf_bytes
    wavy = _ballpoint(
        strokes=[
            {
                "pen_type": 40,
                "thickness": 2,
                "extra": _geometry("LineString", "WaveLine", [[10, 20], [80, 90]]),
            }
        ]
    )
    result = render_note(wavy)
    assert result.status == "approximate"
    assert result.reasons == ["geometry"]
    assert result.pdf_bytes


def test_failed_render_leaves_the_previous_pdf(tmp_path):
    """A render that cannot read the points keeps the previous PDF bytes."""
    root = tmp_path / "BOOX"
    state = empty_state()
    good = _ballpoint()
    bad = _ballpoint(corrupt_points=True)
    archive_notebook(
        root,
        good,
        device_serial="TEST1234",
        source_path="ksync:aaa",
        state=state,
    )
    export_pdf(root, "Journal")
    pdf_path = root / "Journal" / "Journal.pdf"
    previous = pdf_path.read_bytes()
    with pytest.raises(ValueError, match="incomplete or unreadable"):
        archive_notebook(
            root,
            bad,
            device_serial="TEST1234",
            source_path="ksync:aaa",
            state=state,
        )
    failed = render_note(bad)
    assert failed.status == "failed"
    assert failed.page_count == 0
    assert failed.pdf_bytes is None
    assert (root / "Journal" / "Journal.note").read_bytes() == good
    assert pdf_path.read_bytes() == previous


def test_non_image_background_is_approximate():
    """A background that is not PNG or JPEG is skipped and marked approximate."""
    raw = build_note(
        note_id=NOTE_ID,
        title="Journal",
        width=500,
        height=800,
        background=("bg1", b"GIF89a-not-png"),
        strokes=[{"pen_type": 2, "points": [(10, 10, 10), (30, 30, 10)]}],
    )
    result = render_note(raw)
    assert result.status == "approximate"
    assert "background" in result.reasons
    assert result.pdf_bytes
