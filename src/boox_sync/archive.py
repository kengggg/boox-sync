"""Archive notebooks without losing the previous verified revision."""

import hashlib
import shutil
import tempfile
import time
import unicodedata
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from boox_sync.note import parse_notes, read_headers
from boox_sync.render import RENDERER_VERSION, RenderResult, render_note
from boox_sync.storage import (
    archive_lock,
    atomic_write,
    notebook_folder,
    read_json,
    write_json,
)

MANIFEST_KEYS = (
    "note_id",
    "title",
    "device_serial",
    "content_sha256",
    "synced_at",
    "source_path",
    "render_status",
    "approximate_reasons",
    "render_error",
    "page_count",
    "page_size_source",
    "later_copy",
    "renderer_version",
    "rendered_sha256",
    "signature",
)


@dataclass
class ArchivedNote:
    note_id: str
    folder_name: str
    status: str
    error: str | None


def sanitize_title(title: str) -> str:
    cleaned = unicodedata.normalize("NFC", title or "")
    cleaned = "".join(c for c in cleaned if c not in "/:\\\x00" and ord(c) >= 32).strip(
        " ."
    )
    # Leave room for an identity suffix, extension and temporary filename.
    return (
        cleaned.encode("utf-8")[:160].decode("utf-8", "ignore").strip(" .")
        or "Untitled"
    )


def empty_state() -> dict:
    return {"notes": {}}


def archive_notebook(
    archive_root: Path,
    raw: bytes,
    *,
    device_serial: str,
    source_path: str,
    state: dict,
    cover: bytes | None = None,
    signature: str | None = None,
    title: str | None = None,
    clock=None,
    keep_revisions: int = 5,
    expected_note_id: str | None = None,
) -> list[ArchivedNote]:
    """Validate, preserve the previous revision, then commit raw bytes and metadata.

    Unreadable input is kept under .rejected for diagnosis and never replaces a
    notebook. Revisions contain verified source bytes; render quality is separate.
    """
    with archive_lock(archive_root):
        try:
            parse_notes(raw)
            headers = read_headers(raw)
            if expected_note_id is not None and (
                len(headers) != 1 or headers[0].note_id != expected_note_id
            ):
                raise ValueError(
                    "source notebook identity does not match the requested notebook"
                )
        except Exception as exc:
            digest = hashlib.sha256(raw).hexdigest()
            atomic_write(archive_root / ".rejected" / f"{digest}.note", raw)
            raise ValueError(
                f"incomplete or unreadable notebook (saved under .rejected): {exc}"
            ) from exc
        stamped = (clock() if clock else datetime.now().astimezone()).isoformat(
            timespec="microseconds"
        )
        digest = hashlib.sha256(raw).hexdigest()
        written = []
        for header in headers:
            source_title = title if title is not None else header.title
            clean_title = sanitize_title(source_title or Path(source_path).stem)
            folder, previous = _choose_folder(
                archive_root, header.note_id, clean_title, state, device_serial
            )
            existing = _read_manifest(folder) or {}
            note_path = folder / f"{folder.name}.note"
            if (
                note_path.is_file()
                and hashlib.sha256(note_path.read_bytes()).hexdigest() != digest
            ):
                _snapshot(archive_root, folder, device_serial, header.note_id)
            elif previous is not None:
                _snapshot(archive_root, previous, device_serial, header.note_id)
            same = existing.get("content_sha256") == digest
            manifest = {
                "note_id": header.note_id,
                "title": source_title,
                "device_serial": device_serial,
                "content_sha256": digest,
                "synced_at": stamped,
                "source_path": source_path,
                "signature": signature,
                "later_copy": None,
                "render_status": existing.get("render_status", "pending")
                if same
                else "pending",
                "approximate_reasons": existing.get("approximate_reasons", [])
                if same
                else [],
                "render_error": existing.get("render_error") if same else None,
                "page_count": existing.get("page_count") if same else None,
                "page_size_source": existing.get("page_size_source") if same else None,
                "renderer_version": existing.get("renderer_version", RENDERER_VERSION)
                if same
                else RENDERER_VERSION,
                "rendered_sha256": existing.get("rendered_sha256") if same else None,
            }
            atomic_write(note_path, raw)
            if cover is not None:
                atomic_write(folder / "cover.png", cover)
            else:
                (folder / "cover.png").unlink(missing_ok=True)
            # The manifest is the commit record. A crash before this point is
            # detected by the content hash and the previous revision is retained.
            write_json(folder / "manifest.json", manifest)
            if previous is not None and previous != folder:
                old = _read_manifest(previous)
                if old:
                    old["later_copy"] = folder.name
                    write_json(previous / "manifest.json", old)
            state.setdefault("notes", {})[f"{device_serial}:{header.note_id}"] = (
                state_entry(manifest, folder.name)
            )
            _prune_history(archive_root, device_serial, header.note_id, keep_revisions)
            written.append(ArchivedNote(header.note_id, folder.name, "ok", None))
        return written


def state_entry(manifest: dict, folder: str) -> dict:
    return {
        "note_id": manifest["note_id"],
        "title": manifest.get("title") or "Untitled",
        "source_path": manifest.get("source_path"),
        "sha256": manifest.get("content_sha256"),
        "archive_folder": folder,
        "signature": manifest.get("signature"),
        "synced_at": manifest.get("synced_at", ""),
    }


def export_pdf(
    archive_root: Path, folder_name: str, output: Path | None = None, clock=None
) -> RenderResult:
    """Render a verified source and keep the previous PDF on any failure."""
    with archive_lock(archive_root):
        folder = notebook_folder(archive_root, folder_name)
        note_path = folder / f"{folder_name}.note"
        manifest = _read_manifest(folder)
        try:
            raw = note_path.read_bytes()
            digest = hashlib.sha256(raw).hexdigest()
            if not manifest or manifest.get("content_sha256") != digest:
                raise ValueError(
                    "archive integrity check failed; sync again or restore a revision"
                )
            result = render_note(raw, note_id=manifest["note_id"])
        except (OSError, ValueError, KeyError) as exc:
            result = RenderResult(status="failed", error=str(exc))
        if result.pdf_bytes is not None:
            target = output if output is not None else folder / f"{folder_name}.pdf"
            if target.resolve() == note_path.resolve():
                raise ValueError("PDF output must not replace the source notebook")
            atomic_write(target, result.pdf_bytes)
        if output is None and manifest:
            manifest.update(
                {
                    "render_status": result.status,
                    "approximate_reasons": list(result.reasons),
                    "render_error": result.error,
                    "page_count": result.page_count,
                    "page_size_source": result.page_size_source,
                    "renderer_version": RENDERER_VERSION,
                    "rendered_sha256": digest if result.pdf_bytes is not None else None,
                }
            )
            write_json(folder / "manifest.json", manifest)
        return result


def _choose_folder(
    root: Path, note_id: str, title: str, state: dict, device_serial: str
):
    entry = state.get("notes", {}).get(f"{device_serial}:{note_id}")
    previous = None
    if entry and entry.get("archive_folder"):
        previous = notebook_folder(root, entry["archive_folder"])
        if sanitize_title(entry.get("title", "")) == title and _belongs_to(
            previous, note_id, device_serial, entry
        ):
            return previous, None
    # Never assume a suffix is free; another notebook may use it as its title.
    suffix = "".join(c for c in note_id.lower() if c.isascii() and c.isalnum())[:8]
    suffix = suffix or hashlib.sha256(note_id.encode()).hexdigest()[:8]
    index = 0
    while True:
        name = (
            title
            if index == 0
            else f"{title} {suffix}" + (f"-{index}" if index > 1 else "")
        )
        candidate = root / name
        if not candidate.exists() and not candidate.is_symlink():
            break
        if _belongs_to(candidate, note_id, device_serial, entry):
            break
        index += 1
    return (
        candidate,
        previous if previous and previous.is_dir() and previous != candidate else None,
    )


def _belongs_to(folder: Path, note_id: str, serial: str, entry: dict | None) -> bool:
    if folder.is_symlink() or not folder.is_dir():
        return False
    manifest = _read_manifest(folder)
    if manifest:
        return (
            manifest.get("note_id") == note_id
            and manifest.get("device_serial") == serial
        )
    # Recover a damaged manifest only when state identifies this very folder and
    # the independently read notebook header agrees with that identity.
    if entry and entry.get("archive_folder") == folder.name:
        try:
            headers = read_headers((folder / f"{folder.name}.note").read_bytes())
            return len(headers) == 1 and headers[0].note_id == note_id
        except (OSError, ValueError):
            return False
    return False


def _read_manifest(folder: Path) -> dict | None:
    return read_json(folder / "manifest.json")


def history_root(root: Path, serial: str, note_id: str) -> Path:
    identity = hashlib.sha256(f"{serial}:{note_id}".encode()).hexdigest()
    return root / ".history" / identity


def list_revisions(root: Path, serial: str, note_id: str) -> list[Path]:
    history = history_root(root, serial, note_id)
    if not history.is_dir():
        return []
    return sorted(
        (
            p
            for p in history.iterdir()
            if p.is_dir()
            and not p.name.startswith(".")
            and (p / "manifest.json").is_file()
        ),
        reverse=True,
    )


def _snapshot(root: Path, folder: Path, serial: str, note_id: str) -> None:
    manifest = _read_manifest(folder)
    if (
        not manifest
        or manifest.get("note_id") != note_id
        or manifest.get("device_serial") != serial
    ):
        return
    path = folder / f"{folder.name}.note"
    if not path.is_file():
        return
    raw = path.read_bytes()
    digest = hashlib.sha256(raw).hexdigest()
    if manifest.get("content_sha256") != digest:
        return
    try:
        parse_notes(raw)
    except Exception:
        return
    latest = list_revisions(root, serial, note_id)
    if (
        latest
        and (read_json(latest[0] / "manifest.json") or {}).get("content_sha256")
        == digest
    ):
        return
    history = history_root(root, serial, note_id)
    history.mkdir(parents=True, exist_ok=True)
    stage = Path(tempfile.mkdtemp(prefix=".revision-", dir=history))
    try:
        atomic_write(stage / "notebook.note", raw)
        for source, target in [
            (folder / "cover.png", "cover.png"),
            (folder / f"{folder.name}.pdf", "notebook.pdf"),
        ]:
            if source.is_file() and (
                target != "notebook.pdf" or manifest.get("rendered_sha256") == digest
            ):
                atomic_write(stage / target, source.read_bytes())
        write_json(stage / "manifest.json", manifest)
        stage.rename(history / f"{time.time_ns()}-{digest[:12]}")
    finally:
        if stage.exists():
            shutil.rmtree(stage)


def _prune_history(root: Path, serial: str, note_id: str, keep: int) -> None:
    for revision in list_revisions(root, serial, note_id)[keep:]:
        shutil.rmtree(revision)
