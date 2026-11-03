"""Build and read a modern BOOX .note zip.

The zip layout, point records, and protobuf field numbers follow the public
descriptions of device exports. This module does not copy those projects.
"""

import io
import json
import struct
import zipfile
from dataclasses import dataclass, field

from boox_sync.proto import (
    as_float,
    as_text,
    encode_bytes_field,
    encode_float_field,
    encode_varint_field,
    first,
    grouped,
    parse_message,
)

PAGE_ID = "22222222-2222-2222-2222-222222222222"
POINTS_DOC = "33333333-3333-3333-3333-333333333333"
SHAPE_DOC = "44444444-4444-4444-4444-444444444444"


@dataclass
class Stroke:
    """One shape plus the points that draw it."""

    pen_type: int
    points: list[tuple[float, float, int]] = field(default_factory=list)
    thickness: float = 2.0
    color: int = 0xFF000000
    text: str = ""
    html: str = ""
    style: dict = field(default_factory=dict)
    extra: dict | None = None
    layer_id: int = 1
    created: int = 1
    transform: tuple[float, float, float, float, float, float] | None = None
    bbox: dict | None = None
    shape_id: str = ""
    points_doc: str = ""
    modified: int = 0
    state_flag: int = 0


@dataclass
class ParsedPage:
    """One notebook page."""

    page_id: str
    strokes: list[Stroke]
    template: dict | None
    layers: list[dict]
    rect: dict | None = None
    reasons: list[str] = field(default_factory=list)


@dataclass
class ParsedNote:
    """One notebook inside a .note zip."""

    note_id: str
    title: str
    pages: list[ParsedPage]
    width: float | None
    height: float | None
    background: tuple[str, bytes] | None
    prefix: str
    images: list[bytes] = field(default_factory=list)


@dataclass
class NoteHeader:
    """Identity stored in note_info, without page geometry."""

    note_id: str
    title: str
    prefix: str


def build_note(
    *,
    note_id: str,
    title: str,
    strokes: list[dict] | None = None,
    width: float | None = 500.0,
    height: float | None = 800.0,
    template: dict | None = None,
    stash_text: str | None = None,
    corrupt_points: bool = False,
    page_id: str = PAGE_ID,
    background: tuple[str, bytes] | None = None,
) -> bytes:
    """Return a modern single-note .note zip.

    Args:
        note_id: Note UUID stored in note_info.
        title: Notebook title stored in note_info.
        strokes: Stroke dicts. Keys are pen_type, points, thickness, color,
            text, html, style, extra, layer_id, and created.
        width: Canvas width in PDF points. Omit the field when None.
        height: Canvas height in PDF points. Omit the field when None.
        template: Template JSON written for the page.
        stash_text: When set, write a decoy text shape under stash/.
        corrupt_points: Write a points blob that is too short to parse.
        page_id: Page UUID.
        background: Resource id and bytes referenced as the page background.

    Returns:
        Zip bytes.
    """
    stroke_rows = [
        _stroke_from_dict(row, index) for index, row in enumerate(strokes or [])
    ]
    info = _note_info(
        note_id=note_id,
        title=title,
        width=width,
        height=height,
        page_id=page_id,
        background_id=None if background is None else background[0],
    )
    shape_blob = _shape_message(stroke_rows)
    points_blob = b"garbage" if corrupt_points else _points_blob(page_id, stroke_rows)
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        root = f"{note_id}/"
        archive.writestr(f"{root}note/pb/note_info", info)
        archive.writestr(
            f"{root}point/{page_id}/{page_id}#{POINTS_DOC}#points",
            points_blob,
        )
        archive.writestr(
            f"{root}shape/{page_id}#{SHAPE_DOC}#1.zip",
            _zip_bytes({"shape.pb": shape_blob}),
        )
        if template is not None:
            archive.writestr(
                f"{root}template/json/{page_id}.template_json",
                json.dumps(template).encode(),
            )
        if background is not None:
            res_id, payload = background
            archive.writestr(f"{root}resource/pb/{res_id}#1", payload)
        if stash_text is not None:
            decoy = _stroke_from_dict(
                {"pen_type": 6, "text": stash_text, "points": [(1, 1, 1), (20, 20, 1)]},
                0,
            )
            archive.writestr(
                f"{root}stash/shape/{page_id}#{SHAPE_DOC}#1.zip",
                _zip_bytes({"shape.pb": _shape_message([decoy])}),
            )
    return buffer.getvalue()


def read_headers(raw: bytes) -> list[NoteHeader]:
    """Return one header per modern note_info entry.

    Raises:
        ValueError: The bytes are not a zip, or note_info does not parse.
    """
    archive = _open_zip(raw)
    headers: list[NoteHeader] = []
    for name in archive.namelist():
        if _ignored(name) or not name.endswith("note/pb/note_info"):
            continue
        prefix = name[: -len("note/pb/note_info")]
        buckets = grouped(parse_message(archive.read(name)))
        note_id = as_text(first(buckets, 1, b"")) or prefix.strip("/")
        title = as_text(first(buckets, 6, b""))
        headers.append(NoteHeader(note_id=note_id, title=title, prefix=prefix))
    return headers


def parse_notes(raw: bytes) -> list[ParsedNote]:
    """Parse every modern notebook in a .note zip.

    Raises:
        ValueError: The zip is not a modern .note archive or a points blob is corrupt.
    """
    archive = _open_zip(raw)
    headers = read_headers(raw)
    if not headers:
        raise ValueError("not a modern .note zip")
    notes: list[ParsedNote] = []
    for header in headers:
        info_name = f"{header.prefix}note/pb/note_info"
        buckets = grouped(parse_message(archive.read(info_name)))
        width = as_float(first(buckets, 22))
        height = as_float(first(buckets, 23))
        canvas = _json_obj(as_text(first(buckets, 12, b"")))
        page_info = canvas.get("pageInfoMap", {}) if isinstance(canvas, dict) else {}
        page_ids = _page_ids(buckets, archive, header.prefix)
        background = _background(archive, header.prefix, buckets)
        pages: list[ParsedPage] = []
        for page_id in page_ids:
            layers = []
            info = page_info.get(page_id, {})
            if isinstance(info, dict):
                layers = list(info.get("layerList") or [])
            template = _template(archive, header.prefix, page_id)
            page_reasons = (
                ["page-order"] if canvas.get("booxSyncPageOrderInferred") else []
            )
            strokes = _page_strokes(archive, header.prefix, page_id, page_reasons)
            pages.append(
                ParsedPage(
                    page_id=page_id,
                    strokes=strokes,
                    template=template,
                    layers=layers,
                    rect=info.get("booxSyncRect") if isinstance(info, dict) else None,
                    reasons=page_reasons,
                )
            )
        notes.append(
            ParsedNote(
                note_id=header.note_id,
                title=header.title,
                pages=pages,
                width=width,
                height=height,
                background=background,
                prefix=header.prefix,
                images=_embedded_images(archive, header.prefix),
            )
        )
    return notes


def _open_zip(raw: bytes) -> zipfile.ZipFile:
    if not raw.startswith(b"PK"):
        raise ValueError("not a modern .note zip")
    try:
        return zipfile.ZipFile(io.BytesIO(raw))
    except zipfile.BadZipFile as exc:
        raise ValueError("not a modern .note zip") from exc


def _ignored(name: str) -> bool:
    normalized = name.replace("\\", "/")
    return any(f"/{part}/" in f"/{normalized}" for part in ("stash", "source"))


def _stroke_from_dict(row: dict, index: int) -> Stroke:
    shape_id = row.get("shape_id") or f"55555555-5555-5555-5555-55555555{index:04d}"
    points = [tuple(point) for point in row.get("points") or []]
    return Stroke(
        pen_type=int(row.get("pen_type", 2)),
        points=[(float(x), float(y), int(pressure)) for x, y, pressure in points],
        thickness=float(row.get("thickness", 2.0)),
        color=int(row.get("color", 0xFF000000)),
        text=row.get("text") or "",
        html=row.get("html") or "",
        style=dict(row.get("style") or {}),
        extra=row.get("extra"),
        layer_id=int(row.get("layer_id", 1)),
        created=int(row.get("created", index + 1)),
        bbox=row.get("bbox"),
        shape_id=shape_id,
    )


def _note_info(
    *,
    note_id: str,
    title: str,
    width: float | None,
    height: float | None,
    page_id: str,
    background_id: str | None,
) -> bytes:
    canvas = {
        "pageInfoMap": {
            page_id: {"layerList": [{"id": 1, "lock": False, "show": True}]}
        }
    }
    parts = [
        encode_bytes_field(1, note_id.encode()),
        encode_bytes_field(6, title.encode()),
        encode_bytes_field(12, json.dumps(canvas).encode()),
        encode_bytes_field(20, json.dumps({"pageNameList": [page_id]}).encode()),
    ]
    if width is not None:
        parts.append(encode_float_field(22, width))
    if height is not None:
        parts.append(encode_float_field(23, height))
    if background_id is not None:
        background = {
            "useDocBKGround": True,
            "docBKGround": {"type": 1, "resId": background_id},
        }
        parts.append(encode_bytes_field(13, json.dumps(background).encode()))
    return b"".join(parts)


def _shape_message(strokes: list[Stroke], points_doc: str = POINTS_DOC) -> bytes:
    parts: list[bytes] = []
    for stroke in strokes:
        body = _one_shape(stroke, points_doc)
        parts.append(encode_bytes_field(1, body))
    return b"".join(parts)


def _one_shape(stroke: Stroke, points_doc: str = POINTS_DOC) -> bytes:
    bbox = stroke.bbox or {"left": 0, "top": 0, "right": 0, "bottom": 0, "empty": False}
    parts = [
        encode_bytes_field(1, stroke.shape_id.encode()),
        encode_varint_field(2, stroke.created),
        encode_varint_field(4, stroke.color),
        encode_float_field(5, stroke.thickness),
        encode_varint_field(6, stroke.layer_id),
        encode_bytes_field(7, json.dumps(bbox).encode()),
        encode_varint_field(12, stroke.pen_type),
        encode_bytes_field(16, points_doc.encode()),
        encode_bytes_field(18, SHAPE_DOC.encode()),
    ]
    if stroke.transform is not None:
        values = list(stroke.transform) + [0, 0, 1]
        parts.append(encode_bytes_field(8, json.dumps({"values": values}).encode()))
    if stroke.style:
        parts.append(encode_bytes_field(9, json.dumps(stroke.style).encode()))
    if stroke.text:
        parts.append(encode_bytes_field(10, stroke.text.encode()))
    if stroke.extra is not None:
        parts.append(encode_bytes_field(20, json.dumps(stroke.extra).encode()))
    if stroke.html:
        parts.append(encode_bytes_field(22, stroke.html.encode()))
    return b"".join(parts)


def _points_blob(
    page_id: str, strokes: list[Stroke], points_doc: str = POINTS_DOC
) -> bytes:
    header = struct.pack(">I", 1) + _pad36(page_id) + _pad36(points_doc)
    body = bytearray()
    index = bytearray()
    for stroke in strokes:
        if not stroke.points:
            continue
        offset = len(header) + len(body)
        chunk = bytearray(4)
        for x_pos, y_pos, pressure in stroke.points:
            chunk += struct.pack(">ffBBHI", x_pos, y_pos, 0, 0, pressure, 0)
        body += chunk
        index += _pad36(stroke.shape_id)
        index += struct.pack(">II", offset, len(chunk))
    index_at = len(header) + len(body)
    return header + bytes(body) + bytes(index) + struct.pack(">I", index_at)


def _pad36(value: str) -> bytes:
    raw = value.encode("ascii")
    if len(raw) > 36:
        raise ValueError(f"{value} is longer than 36 bytes")
    return raw.ljust(36, b" ")


def _zip_bytes(files: dict[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, payload in files.items():
            archive.writestr(name, payload)
    return buffer.getvalue()


def _json_obj(text: str) -> dict:
    if not text:
        return {}
    value = json.loads(text)
    return value if isinstance(value, dict) else {}


def _page_ids(
    buckets: dict[int, list[object]], archive: zipfile.ZipFile, prefix: str
) -> list[str]:
    active = _json_obj(as_text(first(buckets, 20, b"")))
    names = active.get("pageNameList") if isinstance(active, dict) else None
    if isinstance(names, list) and names:
        return [str(name) for name in names]
    found: list[str] = []
    marker = f"{prefix}point/"
    for name in archive.namelist():
        if _ignored(name) or not name.startswith(marker):
            continue
        page_id = name[len(marker) :].split("/", 1)[0]
        if page_id and page_id not in found:
            found.append(page_id)
    # A text-only page may contain shapes but no point records.
    for name in archive.namelist():
        marker = f"{prefix}shape/"
        if (
            name.startswith(marker)
            and not _ignored(name)
            and "#" in name[len(marker) :]
        ):
            page_id = name[len(marker) :].split("#", 1)[0]
            if page_id not in found:
                found.append(page_id)
    return found


def _template(archive: zipfile.ZipFile, prefix: str, page_id: str) -> dict | None:
    name = f"{prefix}template/json/{page_id}.template_json"
    if name not in archive.namelist():
        return None
    return _json_obj(archive.read(name).decode())


def _embedded_images(archive: zipfile.ZipFile, prefix: str) -> list[bytes]:
    """Return PNG and JPEG payloads stored under resource/."""
    folder = f"{prefix}resource/"
    images: list[bytes] = []
    for name in archive.namelist():
        if _ignored(name) or not name.startswith(folder):
            continue
        payload = archive.read(name)
        if payload.startswith(b"\x89PNG") or payload.startswith(b"\xff\xd8"):
            images.append(payload)
    return images


def _background(
    archive: zipfile.ZipFile,
    prefix: str,
    buckets: dict[int, list[object]],
) -> tuple[str, bytes] | None:
    payload = _json_obj(as_text(first(buckets, 13, b"")))
    ground = payload.get("docBKGround") if isinstance(payload, dict) else None
    if not isinstance(ground, dict):
        return None
    res_id = str(ground.get("resId") or "")
    if not res_id:
        return None
    folder = f"{prefix}resource/pb/"
    for name in archive.namelist():
        if (
            name.startswith(folder)
            and res_id in name.split("/")[-1]
            and not _ignored(name)
        ):
            return res_id, archive.read(name)
    return None


def _page_strokes(
    archive: zipfile.ZipFile, prefix: str, page_id: str, reasons=None
) -> list[Stroke]:
    by_id: dict[str, Stroke] = {}

    def batch_order(name):
        stamp = name.rsplit("#", 1)[-1].removesuffix(".zip")
        return (int(stamp) if stamp.isdigit() else 0, name)

    for name in sorted(archive.namelist(), key=batch_order):
        if (
            _ignored(name)
            or not name.startswith(f"{prefix}shape/{page_id}#")
            or not name.endswith(".zip")
        ):
            continue
        inner = zipfile.ZipFile(io.BytesIO(archive.read(name)))
        for inner_name in inner.namelist():
            if inner_name.endswith("/"):
                continue
            message = grouped(parse_message(inner.read(inner_name)))
            for raw_shape in message.get(1, []):
                if not isinstance(raw_shape, bytes):
                    continue
                stroke = _stroke_from_proto(raw_shape)
                if not stroke.shape_id:
                    raise ValueError("shape has no identity")
                previous = by_id.get(stroke.shape_id)
                if previous is None or stroke.modified >= previous.modified:
                    by_id[stroke.shape_id] = stroke
    points_by_batch = {}
    for name in archive.namelist():
        if (
            _ignored(name)
            or not name.startswith(f"{prefix}point/{page_id}/")
            or not name.endswith("#points")
        ):
            continue
        parts = name.rsplit("/", 1)[-1].split("#")
        batch = parts[-2] if len(parts) >= 3 else ""
        for shape_id, points in _parse_points(archive.read(name)):
            points_by_batch[(batch, shape_id)] = points
    for stroke in by_id.values():
        if stroke.state_flag not in {0, 1}:
            raise ValueError(
                f"unsupported shape state {stroke.state_flag}; source is preserved"
            )
        if stroke.state_flag:
            continue
        stroke.points = points_by_batch.get((stroke.points_doc, stroke.shape_id), [])
        if not stroke.points and stroke.pen_type not in {6, 16, 40}:
            raise ValueError(
                f"missing points for shape {stroke.shape_id} in batch {stroke.points_doc}"
            )
    # Preserve orphan ink as visibly approximate, never silently discard it.
    for (batch, shape_id), points in points_by_batch.items():
        if shape_id not in by_id:
            by_id[shape_id] = Stroke(
                pen_type=0, shape_id=shape_id, points=points, points_doc=batch
            )
    # Field 15=1 is a deletion marker, verified by a before/after erasure on
    # Note Air 3C firmware 4.2. Retain the records in the raw recovery archive.
    return [stroke for stroke in by_id.values() if not stroke.state_flag]


def _stroke_from_proto(raw: bytes) -> Stroke:
    buckets = grouped(parse_message(raw))
    transform = None
    transform_text = as_text(first(buckets, 8, b""))
    if transform_text:
        values = _json_obj(transform_text).get("values")
        if isinstance(values, list) and len(values) >= 6:
            transform = tuple(float(part) for part in values[:6])  # type: ignore[assignment]
    extra_text = as_text(first(buckets, 20, b""))
    style_text = as_text(first(buckets, 9, b""))
    bbox_text = as_text(first(buckets, 7, b""))
    thickness = as_float(first(buckets, 5))
    return Stroke(
        pen_type=int(first(buckets, 12, 0) or 0),
        thickness=2.0 if thickness is None else thickness,
        color=int(first(buckets, 4, 0xFF000000) or 0),
        text=as_text(first(buckets, 10, b"")),
        html=as_text(first(buckets, 22, b"")),
        style=_json_obj(style_text),
        extra=_json_obj(extra_text) if extra_text else None,
        layer_id=int(first(buckets, 6, 0) or 0),
        created=int(first(buckets, 2, 0) or 0),
        transform=transform,
        bbox=_json_obj(bbox_text) if bbox_text else None,
        shape_id=as_text(first(buckets, 1, b"")).strip(),
        points_doc=as_text(first(buckets, 16, b"")).strip(),
        modified=int(first(buckets, 3, first(buckets, 2, 0)) or 0),
        state_flag=int(first(buckets, 15, 0) or 0),
    )


def _parse_points(data: bytes) -> list[tuple[str, list[tuple[float, float, int]]]]:
    if len(data) < 80:
        raise ValueError("points blob is too short")
    index_at = struct.unpack_from(">I", data, len(data) - 4)[0]
    if index_at < 76 or index_at > len(data) - 4:
        raise ValueError("invalid points index")
    index = data[index_at : len(data) - 4]
    if len(index) % 44 != 0:
        raise ValueError("invalid points index")
    strokes: list[tuple[str, list[tuple[float, float, int]]]] = []
    for cursor in range(0, len(index), 44):
        shape_id = index[cursor : cursor + 36].decode("ascii", "replace").strip()
        offset, size = struct.unpack_from(">II", index, cursor + 36)
        end = offset + size
        if offset < 76 or end > index_at or size < 4 or (size - 4) % 16 != 0:
            raise ValueError("invalid points index")
        points: list[tuple[float, float, int]] = []
        record = offset + 4
        while record < end:
            x_pos, y_pos, _tilt_x, _tilt_y, pressure, _delta = struct.unpack_from(
                ">ffBBHI", data, record
            )
            points.append((x_pos, y_pos, pressure))
            record += 16
        strokes.append((shape_id, points))
    return strokes
