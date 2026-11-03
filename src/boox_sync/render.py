"""Render a modern BOOX .note zip to PDF bytes."""

import io
import json
import re
import math
import html
from pathlib import Path
from dataclasses import dataclass, field

from reportlab.lib.colors import Color
from reportlab.lib.utils import ImageReader
from reportlab.pdfgen import canvas
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont, shapeStr

from boox_sync.note import ParsedNote, Stroke, parse_notes

RENDERER_VERSION = 3
DEFAULT_PAGE = (1860.0, 2480.0)
_OK_PENS = {2, 5, 15, 21, 37, 6, 16}
_HTML_TAGS = re.compile(r"<[^>]+>")
_APPROX_PENS = {22: "charcoal", 60: "calligraphy", 61: "calligraphy"}
_PRECISE_GEOMETRY = {"", "Oval"}
_PRECISE_TYPES = {
    "LineString",
    "DirectionLine",
    "BidirectionalLine",
    "Polygon",
    "MultiPoint",
}


@dataclass
class RenderResult:
    """Outcome of rendering one notebook."""

    status: str
    reasons: list[str] = field(default_factory=list)
    pdf_bytes: bytes | None = None
    page_count: int = 0
    page_size_source: str | None = None
    error: str | None = None


def render_note(raw: bytes, note_id: str | None = None) -> RenderResult:
    """Render a .note zip.

    Args:
        raw: Exported .note bytes.
        note_id: When set, render only that notebook. Otherwise render every
            notebook in the zip into one PDF.

    Returns:
        Status ok, approximate, or failed. Failed results have no PDF bytes.
    """
    try:
        notes = parse_notes(raw)
        chosen = [note for note in notes if note_id is None or note.note_id == note_id]
        if note_id is not None and not chosen:
            raise ValueError(f"note {note_id} is not in the zip")
        return _draw(chosen)
    except Exception as exc:
        return RenderResult(status="failed", error=str(exc))


def pen_width(pen_type: int, thickness: float, pressure: int | None) -> float:
    """Return the stroke width in PDF points for one pen sample.

    Args:
        pen_type: BOOX pen type number.
        thickness: Base thickness from the shape record.
        pressure: Stylus pressure from 0 to 4095, or None when the point has none.

    Returns:
        Width in PDF points.
    """
    if pen_type == 5:
        return _pressure_width(thickness, pressure, 1.37, 0.59)
    if pen_type == 21:
        return _pressure_width(thickness, pressure, 2.35, 0.43)
    if pen_type in {22, 60, 61}:
        return _pressure_width(thickness, pressure, 1.37, 0.59)
    return thickness if thickness else 1.0


def _pressure_width(
    thickness: float, pressure: int | None, scale: float, exponent: float
) -> float:
    if pressure is None:
        return thickness if thickness else 1.0
    sample = max(0.0, min(4095.0, float(pressure))) / 4095.0
    return max(0.5, thickness * scale * (sample**exponent))


def _draw(notes: list[ParsedNote]) -> RenderResult:
    reasons: list[str] = []
    size_source = "metadata"
    buffer = io.BytesIO()
    pdf = canvas.Canvas(buffer)
    page_count = 0
    for note in notes:
        background, image_reasons = _note_background(note)
        _remember(reasons, image_reasons)
        if not note.pages:
            width, height, source, page_reasons = _page_size(note, None, [])
            _remember(reasons, page_reasons)
            size_source = source
            pdf.setPageSize((width, height))
            _paint_background(pdf, background, width, height, reasons)
            pdf.showPage()
            page_count += 1
            continue
        for page in note.pages:
            width, height, source, page_reasons = _page_size(
                note, page.template, page.strokes, page.rect
            )
            _remember(reasons, page_reasons)
            _remember(reasons, page.reasons)
            size_source = source
            pdf.setPageSize((width, height))
            _paint_background(pdf, background, width, height, reasons)
            _paint_template(pdf, page.template, width, height)
            pdf.saveState()
            if page.rect:
                pdf.translate(
                    -float(page.rect.get("left", 0)), float(page.rect.get("top", 0))
                )
            for stroke in _visible_strokes(page.strokes, page.layers):
                _remember(reasons, [_reason_for(stroke)])
                _paint_stroke(pdf, stroke, height)
            pdf.restoreState()
            pdf.showPage()
            page_count += 1
    pdf.save()
    status = "approximate" if reasons else "ok"
    return RenderResult(
        status=status,
        reasons=reasons,
        pdf_bytes=buffer.getvalue(),
        page_count=page_count,
        page_size_source=size_source,
    )


def _note_background(note: ParsedNote) -> tuple[tuple[str, bytes] | None, list[str]]:
    """Use one embedded picture as the page background.

    Several pictures have no page position in the staged files, so they stay
    in the .note and the render is marked approximate.
    """
    if note.background is not None:
        return note.background, []
    if len(note.images) == 1:
        return ("embedded", note.images[0]), ["image-placement"]
    if len(note.images) > 1:
        return None, ["image"]
    return None, []


def _page_size(
    note: ParsedNote,
    template: dict | None,
    strokes: list[Stroke],
    page_rect: dict | None = None,
) -> tuple[float, float, str, list[str]]:
    if page_rect:
        width = float(page_rect.get("right", 0)) - float(page_rect.get("left", 0))
        height = float(page_rect.get("bottom", 0)) - float(page_rect.get("top", 0))
        if not all(math.isfinite(n) and n > 0 for n in (width, height)):
            raise ValueError("invalid page dimensions")
        return width, height, "metadata", []
    if note.width and note.height:
        if not all(math.isfinite(n) and n > 0 for n in (note.width, note.height)):
            raise ValueError("invalid page dimensions")
        return float(note.width), float(note.height), "metadata", []
    rect = None
    if template:
        rect = (template.get("properties") or {}).get("shaderRect")
    if isinstance(rect, dict) and "right" in rect and "bottom" in rect:
        width = float(rect["right"]) - float(rect.get("left", 0))
        height = float(rect["bottom"]) - float(rect.get("top", 0))
        if width > 0 and height > 0:
            return width, height, "template", []
    width, height = DEFAULT_PAGE
    max_x, max_y = _extent(strokes)
    if max_x > width or max_y > height:
        return max(width, max_x), max(height, max_y), "default", ["page-size"]
    return width, height, "default", ["page-size"]


def _extent(strokes: list[Stroke]) -> tuple[float, float]:
    """Return the furthest point or box edge on the page."""
    max_x = 0.0
    max_y = 0.0
    for stroke in strokes:
        for point in stroke.points:
            x_pos, y_pos, _pressure = _apply(point, stroke.transform)
            max_x = max(max_x, x_pos)
            max_y = max(max_y, y_pos)
        box = stroke.bbox or {}
        right = box.get("right")
        bottom = box.get("bottom")
        if isinstance(right, (int, float)):
            max_x = max(max_x, float(right))
        if isinstance(bottom, (int, float)):
            max_y = max(max_y, float(bottom))
    return max_x, max_y


def _remember(reasons: list[str], extra: list[str | None]) -> None:
    for reason in extra:
        if reason and reason not in reasons:
            reasons.append(reason)


def _visible_strokes(strokes: list[Stroke], layers: list[dict]) -> list[Stroke]:
    hidden = {layer.get("id") for layer in layers if layer.get("show") is False}
    order = {layer.get("id"): index for index, layer in enumerate(layers)}
    visible = [stroke for stroke in strokes if stroke.layer_id not in hidden]
    return sorted(
        visible, key=lambda stroke: (order.get(stroke.layer_id, 10**9), stroke.created)
    )


def _reason_for(stroke: Stroke) -> str | None:
    if stroke.pen_type in {6, 16}:
        if stroke.html and not stroke.text:
            return "rich-text"
        if stroke.transform and (
            stroke.transform[0],
            stroke.transform[1],
            stroke.transform[3],
            stroke.transform[4],
        ) != (1, 0, 0, 1):
            return "text-transform"
        if stroke.style.get("textItalic") and any(
            "\u0e00" <= c <= "\u0e7f" for c in stroke.text
        ):
            return "text-style"
    if stroke.pen_type in _OK_PENS:
        return None
    if stroke.pen_type == 40:
        return "geometry" if _geometry_is_approximate(stroke) else None
    if stroke.pen_type in _APPROX_PENS:
        return _APPROX_PENS[stroke.pen_type]
    return f"pen-{stroke.pen_type}"


def _geometry_is_approximate(stroke: Stroke) -> bool:
    for feature in _features(stroke):
        geometry = feature.get("geometry") or {}
        subtype = str((feature.get("properties") or {}).get("subType") or "")
        kind = str(geometry.get("type") or "")
        if kind == "FeatureCollection" or subtype in {
            "WaveLine",
            "Curve",
            "Arc",
            "Bracket",
            "Surface",
        }:
            return True
        if kind not in _PRECISE_TYPES or (
            kind == "MultiPoint" and subtype not in _PRECISE_GEOMETRY
        ):
            return True
    return not _features(stroke)


def _features(stroke: Stroke) -> list[dict]:
    if not stroke.extra:
        return []
    collection = stroke.extra.get("featureCollection")
    if isinstance(collection, str):
        collection = json.loads(collection)
    if not isinstance(collection, dict):
        return []
    features = collection.get("features") or []
    return [feature for feature in features if isinstance(feature, dict)]


def _paint_background(
    pdf: canvas.Canvas, background, width: float, height: float, reasons: list[str]
) -> None:
    if background is None:
        return
    _res_id, payload = background
    if payload.startswith(b"\x89PNG") or payload.startswith(b"\xff\xd8"):
        pdf.drawImage(
            ImageReader(io.BytesIO(payload)),
            0,
            0,
            width=width,
            height=height,
            mask="auto",
        )
        return
    _remember(reasons, ["background"])


def _paint_template(
    pdf: canvas.Canvas, template: dict | None, width: float, height: float
) -> None:
    if not template:
        return
    props = template.get("properties") or {}
    name = str((props.get("resourceAttr") or {}).get("resName") or "")
    spacing = float(props.get("spacing") or 0)
    if spacing <= 0:
        return
    rect = props.get("shaderRect") or {}
    left = float(rect.get("left", 0))
    right = float(rect.get("right", width))
    top = float(rect.get("top", 0))
    bottom = float(rect.get("bottom", height))
    pdf.setStrokeColor(Color(0.75, 0.75, 0.75, 1))
    pdf.setLineWidth(0.4)
    if "point" in name or "dot" in name:
        y_pos = top
        while y_pos <= bottom:
            x_pos = left
            while x_pos <= right:
                pdf.circle(x_pos, height - y_pos, 0.8, stroke=0, fill=1)
                x_pos += spacing
            y_pos += spacing
        return
    if "horizontal_line" in name or name.endswith("line"):
        y_pos = top + spacing
        while y_pos < bottom:
            pdf.line(left, height - y_pos, right, height - y_pos)
            y_pos += spacing


def _paint_stroke(pdf: canvas.Canvas, stroke: Stroke, page_height: float) -> None:
    red, green, blue, alpha = _rgba(stroke.color)
    if stroke.pen_type == 15:
        alpha = 0.5
        pdf.setBlendMode("Multiply")
    else:
        pdf.setBlendMode("Normal")
    color = Color(red, green, blue, alpha=alpha)
    pdf.setStrokeColor(color)
    pdf.setFillColor(color)
    pdf.setLineCap(1)
    pdf.setLineJoin(1)
    points = [_apply(point, stroke.transform) for point in stroke.points]
    if stroke.pen_type in {6, 16}:
        _paint_text(pdf, stroke, page_height, color)
        return
    if stroke.pen_type == 37:
        _paint_fill(pdf, points, page_height)
        return
    if stroke.pen_type == 40 and not _geometry_is_approximate(stroke):
        _paint_geometry(pdf, stroke, page_height)
        return
    if stroke.pen_type == 22:
        _paint_variable(pdf, points, page_height, stroke.thickness, 22)
        return
    if stroke.pen_type in {5, 21, 60, 61}:
        _paint_variable(pdf, points, page_height, stroke.thickness, stroke.pen_type)
        return
    width = stroke.thickness if stroke.thickness else 1.0
    if stroke.pen_type not in {2, 15, 40}:
        width = stroke.thickness if stroke.thickness else 1.0
    pdf.setLineWidth(width)
    _paint_polyline(pdf, points, page_height)


def _paint_text(
    pdf: canvas.Canvas, stroke: Stroke, page_height: float, color: Color
) -> None:
    markup = re.sub(r"<br\s*/?>|</p>", "\n", stroke.html, flags=re.IGNORECASE)
    text = stroke.text or html.unescape(_HTML_TAGS.sub("", markup))
    if not text:
        return
    style = stroke.style or {}
    size = float(style.get("textSize") or 12)
    font = "Helvetica"
    bold = bool(style.get("textBold"))
    italic = bool(style.get("textItalic"))
    if bold and italic:
        font = "Helvetica-BoldOblique"
    elif bold:
        font = "Helvetica-Bold"
    elif italic:
        font = "Helvetica-Oblique"
    if not math.isfinite(size) or size <= 0:
        raise ValueError("invalid text size")
    pdf.setFillColor(color)
    left, top, right = _text_edges(stroke)
    for index, line in enumerate(text.splitlines()):
        runs = _text_runs(line, font, size, bold)
        width = sum(run[2] for run in runs)
        align = int(style.get("alignType") or 0)
        x = min(left, right)
        if align == 1:
            x += (abs(right - left) - width) / 2
        elif align == 2:
            x += abs(right - left) - width
        # ActualText preserves the original Unicode even when shaping substitutes
        # glyphs or decomposes Thai vowels. It is used by Preview/Poppler search.
        actual = (b"\xfe\xff" + line.encode("utf-16-be")).hex().upper()
        pdf.addLiteral(f"/Span << /ActualText <{actual}> >> BDC")
        for run_font, shaped, run_width in runs:
            pdf.setFont(run_font, size)
            pdf.drawString(x, page_height - top - size - index * size * 1.25, shaped)
            x += run_width
        pdf.addLiteral("EMC")


def _text_runs(text: str, latin_font: str, size: float, bold: bool):
    """Shape Thai runs with a bundled font; fail explicitly for missing glyphs."""
    groups = []
    for char in text:
        thai = "\u0e00" <= char <= "\u0e7f"
        if not thai:
            try:
                char.encode("cp1252")
            except UnicodeEncodeError as exc:
                raise ValueError(
                    f"unsupported text character U+{ord(char):04X}; source is preserved"
                ) from exc
        font = "BooxThaiBold" if thai and bold else "BooxThai" if thai else latin_font
        if groups and groups[-1][0] == font:
            groups[-1][1] += char
        else:
            groups.append([font, char])
    result = []
    for font, value in groups:
        if font.startswith("BooxThai"):
            if font not in pdfmetrics.getRegisteredFontNames():
                filename = (
                    "NotoSansThai-Bold.ttf" if bold else "NotoSansThai-Regular.ttf"
                )
                pdfmetrics.registerFont(
                    TTFont(font, str(Path(__file__).parent / "fonts" / filename))
                )
            face = pdfmetrics.getFont(font)
            if not face.shapable:
                raise ValueError(
                    "Thai rendering requires HarfBuzz; reinstall boox-sync dependencies"
                )
            shaped = shapeStr(value, font, size, force=True)
            width = sum(item.x_advance for item in shaped.__shapeData__) * size / 1000
        else:
            shaped = value
            width = pdfmetrics.stringWidth(value, font, size)
        result.append((font, shaped, width))
    return result


def _aligned_x(
    pdf: canvas.Canvas,
    text: str,
    font: str,
    size: float,
    left: float,
    right: float,
    style: dict,
) -> float:
    """Place text in the box. alignType 0 is left, 1 is center, 2 is right."""
    align = int(style.get("alignType") or 0)
    span = max(right, left) - min(right, left)
    origin = min(left, right)
    text_width = pdf.stringWidth(text, font, size)
    if align == 1:
        return origin + (span - text_width) / 2
    if align == 2:
        return origin + span - text_width
    return origin


def _text_edges(stroke: Stroke) -> tuple[float, float, float]:
    bbox = stroke.bbox or {}
    if "left" in bbox and "top" in bbox:
        left = float(bbox["left"])
        top = float(bbox["top"])
        right = float(bbox["right"]) if "right" in bbox else left
    elif stroke.points:
        left, top, _pressure = stroke.points[0]
        right = left
    else:
        left, top, right = 0.0, 0.0, 0.0
    if stroke.transform:
        mapped_left, mapped_top = _apply((left, top, 0), stroke.transform)[:2]
        mapped_right = _apply((right, top, 0), stroke.transform)[0]
        return mapped_left, mapped_top, mapped_right
    return left, top, right


def _paint_fill(
    pdf: canvas.Canvas, points: list[tuple[float, float, int]], page_height: float
) -> None:
    pairs = zip(points[0::2], points[1::2])
    for (x0, y0, _p0), (x1, _y1, _p1) in pairs:
        pdf.rect(min(x0, x1), page_height - y0 - 0.5, abs(x1 - x0), 1, stroke=0, fill=1)


def _paint_variable(
    pdf: canvas.Canvas,
    points: list[tuple[float, float, int]],
    page_height: float,
    thickness: float,
    pen_type: int,
) -> None:
    if len(points) == 1:
        x, y, pressure = points[0]
        pdf.circle(
            x,
            page_height - y,
            pen_width(pen_type, thickness, pressure) / 2,
            stroke=0,
            fill=1,
        )
        return
    if len(points) < 2:
        return
    for start, end in zip(points, points[1:]):
        pressure = int((start[2] + end[2]) / 2)
        pdf.setLineWidth(pen_width(pen_type, thickness, pressure))
        pdf.line(start[0], page_height - start[1], end[0], page_height - end[1])


def _paint_polyline(
    pdf: canvas.Canvas, points: list[tuple[float, float, int]], page_height: float
) -> None:
    if len(points) == 1:
        pdf.circle(
            points[0][0],
            page_height - points[0][1],
            pdf._lineWidth / 2,
            stroke=0,
            fill=1,
        )
        return
    if len(points) < 2:
        return
    path = pdf.beginPath()
    path.moveTo(points[0][0], page_height - points[0][1])
    for x_pos, y_pos, _pressure in points[1:]:
        path.lineTo(x_pos, page_height - y_pos)
    pdf.drawPath(path, stroke=1, fill=0)


def _paint_geometry(pdf: canvas.Canvas, stroke: Stroke, page_height: float) -> None:
    pdf.setLineWidth(stroke.thickness if stroke.thickness else 1.0)
    for feature in _features(stroke):
        geometry = feature.get("geometry") or {}
        kind = geometry.get("type")
        subtype = str((feature.get("properties") or {}).get("subType") or "")
        coords = _coords(geometry)
        if kind == "MultiPoint" and subtype == "Oval" and len(coords) >= 2:
            (x0, y0), (x1, y1) = coords[0], coords[1]
            mapped = [
                _apply((x0, y0, 0), stroke.transform),
                _apply((x1, y1, 0), stroke.transform),
            ]
            pdf.ellipse(
                min(mapped[0][0], mapped[1][0]),
                page_height - max(mapped[0][1], mapped[1][1]),
                max(mapped[0][0], mapped[1][0]),
                page_height - min(mapped[0][1], mapped[1][1]),
                stroke=1,
                fill=0,
            )
            continue
        mapped = [
            _apply((x_pos, y_pos, 0), stroke.transform)
            for x_pos, y_pos in _ring(kind, coords)
        ]
        _paint_polyline(pdf, mapped, page_height)


def _coords(geometry: dict) -> list[tuple[float, float]]:
    raw = geometry.get("coordinates", geometry.get("coords")) or []
    return _pairs(raw)


def _pairs(raw: object) -> list[tuple[float, float]]:
    pairs: list[tuple[float, float]] = []
    if isinstance(raw, list) and raw and isinstance(raw[0], (int, float)):
        if len(raw) >= 2:
            pairs.append((float(raw[0]), float(raw[1])))
        return pairs
    if isinstance(raw, list):
        for item in raw:
            pairs.extend(_pairs(item))
    return pairs


def _ring(
    kind: str | None, coords: list[tuple[float, float]]
) -> list[tuple[float, float]]:
    if kind == "Polygon":
        return [coords[index] for index in range(0, len(coords), 2)]
    return coords


def _apply(
    point: tuple[float, float, int],
    transform: tuple[float, float, float, float, float, float] | None,
) -> tuple[float, float, int]:
    x_pos, y_pos, pressure = point
    if transform is None:
        return point
    scale_x, skew_y, translate_x, skew_x, scale_y, translate_y = transform
    return (
        scale_x * x_pos + skew_y * y_pos + translate_x,
        skew_x * x_pos + scale_y * y_pos + translate_y,
        pressure,
    )


def _rgba(color: int) -> tuple[float, float, float, float]:
    # ksync-staged shapes widen the value; the real ARGB sits in the low 32 bits.
    if color > 0xFFFFFFFF:
        color &= 0xFFFFFFFF
    alpha = (color >> 24) & 0xFF
    red = ((color >> 16) & 0xFF) / 255
    green = ((color >> 8) & 0xFF) / 255
    blue = (color & 0xFF) / 255
    return red, green, blue, (alpha / 255 if alpha else 1)
