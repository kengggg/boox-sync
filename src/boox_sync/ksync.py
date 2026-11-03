"""Read the ONYX ksync staging area and assemble modern .note zips.

The ksync agent writes live Notes data to shared storage while the tablet
is used:

- ``couch/<user>-NOTE_TREE.cblite2``  the notebook list (id, title, section)
- ``document/<id>/virtual/page/pb/*``  per-page protobuf: id, times, page
  rect, layout name, template path, page index
- ``document/<id>/pageModel/pb/*``  per-page layer list
- ``document/<id>/shape/<page>#<batch>#<ms>.zip``  stroke batches
- ``document/<id>/template/json/<page>.template_json``  page templates
- ``point/<id>/<page>/<page>#<batch>#points``  point-record chunks
- ``document/<id>/stash/``  superseded strokes, ignored here
- ``.noteCache/thumbnail/<id>.png``  notebook cover thumbnails

One stroke batch joins its point chunk on the batch id stored in each
shape's field 16. ``assemble_note`` never re-encodes shapes, points, or
templates: it writes them verbatim into the layout ``boox_sync.note``
already reads, so one notebook becomes one standard modern ``.note`` zip.

The couch bodies are Fleece-encoded. Decode the current root dictionary
and shared keys to read notebook titles and lifecycle status. Deleted
notebooks can remain in this database and retain staged source files.
"""

import io
import json
import sqlite3
import zipfile
from dataclasses import dataclass
from pathlib import Path

from boox_sync.fleece import decode_fleece

from boox_sync.note import (
    _points_blob,
    _shape_message,
    _stroke_from_proto,
    _stroke_from_dict,
    _zip_bytes,
)
from boox_sync.proto import (
    encode_bytes_field,
    encode_float_field,
    encode_varint_field,
    parse_message,
    grouped,
)


@dataclass
class NotebookMeta:
    """One notebook listed by the NOTE_TREE database."""

    note_id: str
    title: str
    active: bool = True


@dataclass
class PageMeta:
    """One page of one notebook, merged from page and pageModel protobufs."""

    page_id: str
    index: int | None = None
    created: int = 0
    rect: dict | None = None
    layers: list | None = None
    layout: str = ""
    template_ref: str = ""


def read_note_tree(path) -> list[NotebookMeta]:
    """Read complete notebook identities and lifecycle states, including inactive ones.

    Args:
        path: The db.sqlite3 file. Its -wal and -shm siblings are used when
            present.

    Returns:
        One entry per Local notebook. Unknown or damaged metadata fails the
        whole inventory so it cannot falsely retire previously active notebooks.
    """
    db = sqlite3.connect(f"file:{Path(path).resolve()}?mode=ro", uri=True)
    try:
        tables = {
            r[0]
            for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        shared = (
            db.execute("SELECT body FROM kv_info WHERE key='SharedKeys'").fetchone()
            if "kv_info" in tables
            else None
        )
        keys = decode_fleece(shared[0]) if shared else None
        if keys is not None and (
            not isinstance(keys, list) or not all(isinstance(k, str) for k in keys)
        ):
            raise ValueError("Invalid notebook metadata shared keys")
        rows = db.execute("SELECT key, flags, body FROM kv_default").fetchall()
        if "kv_del_default" in tables:
            rows += db.execute("SELECT key, flags, body FROM kv_del_default").fetchall()
    finally:
        db.close()
    metas: list[NotebookMeta] = []
    for key, flags, body in rows:
        if (flags or 0) & 1:
            # A database tombstone may no longer retain its body or title.
            metas.append(NotebookMeta(str(key), "Untitled", active=False))
            continue
        if not isinstance(body, bytes):
            raise ValueError("Notebook metadata is missing a document body")
        doc = decode_fleece(body, keys)
        if not isinstance(doc, dict):
            raise ValueError("Notebook metadata root is not a dictionary")
        if doc.get("source") != "Local" or doc.get("type") != 1:
            continue
        title = doc.get("title")
        if not isinstance(title, str):
            raise ValueError("Notebook metadata is missing a title")
        status = doc.get("status")
        if type(status) is int and status in (0, 1):
            active = status == 1
        elif "status" not in doc and type(doc.get("enabled")) is bool:
            active = doc["enabled"]
        else:
            raise ValueError(
                "Unsupported notebook lifecycle status; previous inventory retained"
            )
        if doc.get("forceDelete") is True:
            active = False
        metas.append(NotebookMeta(str(key), title.strip() or "Untitled", active))
    return metas


def _json_obj(raw: bytes) -> dict | None:
    try:
        value = json.loads(raw.decode("utf-8", "replace"))
    except ValueError:
        return None
    return value if isinstance(value, dict) else None


def _nested(data: bytes) -> list[tuple[int, object]]:
    """Return the fields inside the first length-delimited field."""
    for number, value in parse_message(data):
        if number == 1 and isinstance(value, bytes):
            return parse_message(value)
    return []


def _page_from_virtual(data: bytes) -> PageMeta | None:
    """Read one virtual/page/pb file."""
    meta = PageMeta(page_id="")
    for number, value in _nested(data):
        if number == 1 and isinstance(value, bytes):
            meta.page_id = value.decode("utf-8", "replace")
        elif number in (2, 3) and isinstance(value, int) and value > meta.created:
            meta.created = value
        elif number in (6, 7, 8) and isinstance(value, bytes) and meta.rect is None:
            meta.rect = _json_obj(value)
        elif number == 9 and isinstance(value, bytes):
            meta.layout = value.decode("utf-8", "replace")
        elif number == 10 and isinstance(value, bytes):
            meta.template_ref = value.decode("utf-8", "replace")
        elif number == 12 and isinstance(value, bytes):
            text = value.decode("utf-8", "replace")
            if text.lstrip("-").isdigit():
                meta.index = int(text)
    return meta if meta.page_id else None


def _page_from_model(data: bytes) -> PageMeta | None:
    """Read one pageModel/pb file."""
    meta = PageMeta(page_id="")
    for number, value in _nested(data):
        if number == 1 and isinstance(value, bytes):
            meta.page_id = value.decode("utf-8", "replace")
        elif number in (5, 6) and isinstance(value, int) and value > meta.created:
            meta.created = value
        elif number == 2 and isinstance(value, bytes) and meta.layers is None:
            layers = _json_obj(value) or {}
            if isinstance(layers.get("layerList"), list):
                meta.layers = layers["layerList"]
        elif number == 7 and isinstance(value, bytes) and meta.rect is None:
            meta.rect = _json_obj(value)
    return meta if meta.page_id else None


def collect_pages(files: dict[str, bytes]) -> list[PageMeta]:
    """Return the notebook's pages in page order.

    Args:
        files: The notebook's file map, keys as described in assemble_note.

    Returns:
        Pages sorted by index, then creation time. The virtual/page record
        wins over pageModel for rect and index; pageModel supplies layers.
    """
    by_id: dict[str, PageMeta] = {}
    for name, data in files.items():
        kind, _, _rest = name.partition("/")
        if kind == "page":
            meta = _page_from_virtual(data)
            source = "virtual"
        elif kind == "pagemodel":
            meta = _page_from_model(data)
            source = "model"
        else:
            continue
        if meta is None:
            continue
        if meta.page_id not in by_id:
            by_id[meta.page_id] = meta
            continue
        merged = by_id[meta.page_id]
        for field in ("created", "layout", "template_ref"):
            if getattr(meta, field):
                setattr(merged, field, getattr(meta, field))
        if source == "virtual":
            if meta.rect:
                merged.rect = meta.rect
            if meta.index is not None:
                merged.index = meta.index
        else:
            if meta.layers is not None:
                merged.layers = meta.layers
            if merged.rect is None:
                merged.rect = meta.rect
    # Legacy staging may omit some or all page metadata. Keep every page with
    # ink, and use its earliest stroke only as an explicitly approximate order.
    for name, data in files.items():
        if name.startswith("point/"):
            page_id = name.split("/")[1]
            by_id.setdefault(page_id, PageMeta(page_id))
        elif name.startswith("shape/") and name.endswith(".zip"):
            page_id = name.split("/", 1)[1].split("#", 1)[0]
            page = by_id.setdefault(page_id, PageMeta(page_id))
            if page.index is None:
                with zipfile.ZipFile(io.BytesIO(data)) as batch:
                    for member in batch.namelist():
                        if member.endswith("/"):
                            continue
                        for record in grouped(parse_message(batch.read(member))).get(
                            1, []
                        ):
                            created = _stroke_from_proto(record).created
                            if created and (not page.created or created < page.created):
                                page.created = created
    pages = list(by_id.values())
    pages.sort(
        key=lambda page: (
            page.index is None,
            page.index or 0,
            page.created,
            page.page_id,
        )
    )
    return pages


def _rect_size(rect: dict | None) -> tuple[float, float] | None:
    if not isinstance(rect, dict):
        return None
    width = float(rect.get("right", 0)) - float(rect.get("left", 0))
    height = float(rect.get("bottom", 0)) - float(rect.get("top", 0))
    if width > 0 and height > 0:
        return width, height
    return None


def _note_info(
    note_id: str,
    title: str,
    page_ids: list[str],
    layer_map: dict[str, list],
    width: float | None,
    height: float | None,
    page_map: dict | None = None,
    order_inferred: bool = False,
) -> bytes:
    canvas = {
        "pageInfoMap": {
            page_id: {"layerList": layer_map[page_id]} for page_id in page_ids
        }
    }
    if order_inferred:
        canvas["booxSyncPageOrderInferred"] = True
    for page_id, rect in (page_map or {}).items():
        canvas["pageInfoMap"][page_id]["booxSyncRect"] = rect
    parts = [
        encode_bytes_field(1, note_id.encode()),
        encode_bytes_field(6, title.encode()),
        encode_bytes_field(12, json.dumps(canvas).encode()),
        encode_bytes_field(20, json.dumps({"pageNameList": page_ids}).encode()),
    ]
    if width is not None:
        parts.append(encode_float_field(22, width))
    if height is not None:
        parts.append(encode_float_field(23, height))
    return b"".join(parts)


def assemble_note(note_id: str, title: str, files: dict[str, bytes]) -> bytes:
    """Build one modern .note zip from a notebook's ksync file map.

    Args:
        note_id: The ksync document id, used as the note id.
        title: Notebook title from the NOTE_TREE database.
        files: Maps relative keys to bytes:

            - ``page/<uuid>``  virtual/page/pb contents
            - ``pagemodel/<uuid>``  pageModel/pb contents
            - ``shape/<name>.zip``  stroke batches, stored verbatim
            - ``point/<pageId>/<name>``  point chunks, stored verbatim
            - ``template/<name>``  template JSON, stored verbatim
            - ``doc/<name>``  doc metadata, kept but unused

            ``stash/`` entries must not be included.

    Returns:
        Zip bytes that boox_sync.note parses as a modern .note file.
    """
    pages = collect_pages(files)
    ordered = [page.page_id for page in pages]
    layer_map = {
        page.page_id: page.layers if page.layers is not None else [] for page in pages
    }
    size = None
    for page in pages:
        size = _rect_size(page.rect)
        if size is not None:
            break
    if any(page.rect is None for page in pages):
        size = None  # A known page's dimensions must not be applied to an unknown page.
    width, height = size if size is not None else (None, None)
    info = _note_info(
        note_id,
        title,
        ordered,
        layer_map,
        width,
        height,
        {page.page_id: page.rect for page in pages if page.rect},
        order_inferred=len(pages) > 1 and any(page.index is None for page in pages),
    )
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        root = f"{note_id}/"

        def write(name, data):
            # Fixed ZIP metadata makes equal source content produce equal hashes.
            archive.writestr(zipfile.ZipInfo(name), data)

        write(f"{root}note/pb/note_info", info)
        for name, data in sorted(files.items()):
            kind, _, rest = name.partition("/")
            if kind == "shape":
                write(f"{root}shape/{rest}", data)
            elif kind == "point":
                write(f"{root}point/{rest}", data)
            elif kind == "template":
                template_name = (
                    rest[len("json/") :] if rest.startswith("json/") else rest
                )
                template_name = template_name.rsplit("/", 1)[-1]
                if not template_name.endswith(".template_json"):
                    template_name = template_name.split(".")[0] + ".template_json"
                write(f"{root}template/json/{template_name}", data)
            elif kind == "resource":
                write(f"{root}resource/{rest}", data)
            elif kind in {"page", "pagemodel", "doc", "source"}:
                write(
                    f"{root}source/{name}" if kind != "source" else f"{root}{name}",
                    data,
                )
    return buffer.getvalue()


def build_batch(
    page_id: str,
    batch_id: str,
    strokes: list[dict],
    timestamp: int,
) -> dict[str, bytes]:
    """Build one stroke batch and its point chunk.

    Args:
        page_id: Page id the strokes belong to.
        batch_id: Batch doc id, written into each shape's field 16.
        strokes: Stroke dicts accepted by note.build_note.
        timestamp: Batch millisecond stamp used in the shape zip name.

    Returns:
        ``shape/<name>.zip`` and ``point/<pageId>/<name>`` entries.
    """
    rows = [_stroke_from_dict(row, number) for number, row in enumerate(strokes)]
    shape_name = f"{page_id}#{batch_id}#{timestamp}"
    body = _shape_message(rows, points_doc=batch_id)
    points = _points_blob(page_id, rows, points_doc=batch_id)
    return {
        f"shape/{shape_name}.zip": _zip_bytes({f"{shape_name}": body}),
        f"point/{page_id}/{page_id}#{batch_id}#points": points,
    }


def build_virtual_page(
    page_id: str,
    index: int,
    created: int,
    rect: dict,
    layout: str = "Blank",
    template_ref: str = "",
) -> bytes:
    """Build one virtual/page/pb file."""
    nested = [
        encode_bytes_field(1, page_id.encode()),
        encode_varint_field(2, created),
        encode_varint_field(3, created + 1),
        encode_bytes_field(6, json.dumps(rect).encode()),
        encode_bytes_field(9, layout.encode()),
        encode_bytes_field(10, template_ref.encode()),
        encode_bytes_field(12, str(index).encode()),
    ]
    return encode_bytes_field(1, b"".join(nested))


def build_page_model(
    page_id: str, layers: list[dict], created: int, rect: dict
) -> bytes:
    """Build one pageModel/pb file."""
    nested = [
        encode_bytes_field(1, page_id.encode()),
        encode_bytes_field(2, json.dumps({"layerList": layers}).encode()),
        encode_varint_field(5, created),
        encode_varint_field(6, created),
        encode_bytes_field(7, json.dumps(rect).encode()),
    ]
    return encode_bytes_field(1, b"".join(nested))
