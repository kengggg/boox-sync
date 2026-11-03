"""Versioned JSON-lines bridge for the native macOS app. No GUI dependencies.

One request on stdin performs one operation. Progress and a final result are
emitted on stdout; notebook bytes are never sent over the control channel.
"""

import hashlib
import json
import os
import platform
import shutil
import subprocess
import sys
import tempfile
import uuid
from collections import Counter
from dataclasses import asdict
from pathlib import Path

from boox_sync import __version__
from boox_sync.adb import AdbKsyncSource
from boox_sync.archive import list_revisions
from boox_sync.config import Config, validate_config
from boox_sync.inventory import load_inventories
from boox_sync.render import RENDERER_VERSION, render_note
from boox_sync.state import load_state
from boox_sync.storage import (
    archive_lock,
    atomic_write,
    notebook_folder,
    read_json,
    write_json,
)
from boox_sync.sync import sync_once

PROTOCOL_VERSION = 1
MARKER = ".boox-archive.json"


def emit(event):
    print(
        json.dumps({"protocol": PROTOCOL_VERSION, **event}, ensure_ascii=False),
        flush=True,
    )


def archive_identity(root):
    """A selected folder must still exist and match its persisted identity."""
    if not root.is_dir():
        raise ValueError(
            "Archive folder is unavailable. Reconnect its disk or choose a folder in Settings."
        )
    marker = read_json(root / MARKER) or {}
    value = marker.get("archive_id")
    try:
        return str(uuid.UUID(value))
    except (ValueError, TypeError, AttributeError) as exc:
        raise ValueError(
            "Archive identity is missing. Choose this folder again in Settings."
        ) from exc


def prepare_archive(root):
    if not root.is_dir():
        raise ValueError("Choose an existing archive folder.")
    with archive_lock(root):
        if (root / MARKER).exists():
            identity = archive_identity(root)
        else:
            identity = str(uuid.uuid4())
            write_json(root / MARKER, {"version": 1, "archive_id": identity})
        # Verify write access without leaving a test file behind.
        with tempfile.TemporaryFile(dir=root):
            pass
    return {
        "archive_id": identity,
        "archive_dir": str(root.resolve()),
        "free_bytes": shutil.disk_usage(root).free,
    }


def request_config(request):
    raw = request.get("config") or {}
    root = Path(raw.get("archive_dir", "")).expanduser()
    if not raw.get("archive_dir") or not root.is_absolute():
        raise ValueError("Choose an archive folder in Settings.")
    identity = archive_identity(root)
    if identity != raw.get("archive_id"):
        raise ValueError("The archive folder has changed. Select it again in Settings.")
    data_dir = Path(request["data_dir"]).expanduser()
    cfg = Config(
        device_serial=raw.get("device_serial") or "UNSELECTED",
        adb_path=request.get("adb_path", ""),
        archive_dir=root,
        state_dir=data_dir / "archives" / identity,
        poll_seconds=raw.get("poll_seconds", 30),
        adb_timeout_seconds=raw.get("adb_timeout_seconds", 120),
        keep_revisions=raw.get("keep_revisions", 5),
        render_after_sync=raw.get("render_after_sync", True),
    )
    validate_config(cfg)
    return cfg


def devices(adb_path, runner=subprocess.run):
    if not Path(adb_path).is_file():
        raise ValueError("The bundled connection tool is missing. Reinstall BOOX Sync.")
    proc = runner(
        [adb_path, "devices", "-l"], capture_output=True, text=True, timeout=10
    )
    if proc.returncode:
        raise RuntimeError(proc.stderr.strip() or "Unable to read connected devices.")
    found = []
    for line in proc.stdout.splitlines():
        parts = line.split()
        if len(parts) < 2 or parts[0] == "List" or parts[0].startswith("*"):
            continue
        serial, state = parts[:2]
        if state not in {
            "device",
            "unauthorized",
            "offline",
            "recovery",
            "bootloader",
            "sideload",
        }:
            continue
        properties = dict(part.split(":", 1) for part in parts[2:] if ":" in part)
        found.append(
            {
                "serial": serial,
                "state": state,
                "model": properties.get("model", "Android device").replace("_", " "),
            }
        )
    return {"devices": found}


def library(cfg):
    state = load_state(cfg.state_dir, cfg.archive_dir)
    snapshot = load_inventories(cfg.state_dir).get(cfg.device_serial)
    current = snapshot["notebooks"] if snapshot else {}
    report = read_json(cfg.state_dir / "last-report.json")
    notes = []
    for key, entry in state.get("notes", {}).items():
        serial = key.split(":", 1)[0]
        remote = current.get(entry["note_id"])
        presence = "unknown"
        if serial != cfg.device_serial:
            presence = "other_device"
        elif snapshot is not None:
            presence = (
                "absent"
                if remote is None
                else ("active" if remote["active"] else "inactive")
            )
        folder = notebook_folder(cfg.archive_dir, entry["archive_folder"])
        manifest = read_json(folder / "manifest.json") or {}
        notes.append(
            {
                "id": key,
                "note_id": entry["note_id"],
                "serial": serial,
                "title": remote["title"]
                if presence == "active"
                else entry.get("title", "Untitled"),
                "presence": presence,
                "has_archive": True,
                "folder": folder.name,
                "status": manifest.get("render_status", "pending"),
                "reasons": manifest.get("approximate_reasons", []),
                "error": manifest.get("render_error"),
                "synced_at": entry.get("synced_at"),
                "pages": manifest.get("page_count"),
            }
        )
    saved = {note["id"] for note in notes}
    for note_id, remote in current.items():
        key = f"{cfg.device_serial}:{note_id}"
        if not remote["active"] or key in saved:
            continue
        # Failed first transfers must still appear in the current tablet list.
        notes.append(
            {
                "id": key,
                "note_id": note_id,
                "serial": cfg.device_serial,
                "title": remote["title"],
                "folder": "",
                "presence": "active",
                "has_archive": False,
                "status": "not saved",
                "reasons": [],
                "error": "No local copy yet. Sync again; see Activity if the transfer fails.",
                "synced_at": None,
                "pages": None,
            }
        )
    notes.sort(key=lambda item: (item["title"].casefold(), item["id"]))
    return {
        "notebooks": notes,
        "last_report": report,
        "inventory_checked_at": snapshot["checked_at"] if snapshot else None,
        "free_bytes": shutil.disk_usage(cfg.archive_dir).free,
    }


def notebook_entry(cfg, key):
    state = load_state(cfg.state_dir, cfg.archive_dir)
    entry = state["notes"].get(key)
    if not entry:
        raise ValueError("This notebook is no longer in the selected archive.")
    return entry


def history(cfg, key):
    entry = notebook_entry(cfg, key)
    serial = key.split(":", 1)[0]
    revisions = []
    for path in list_revisions(cfg.archive_dir, serial, entry["note_id"]):
        manifest = read_json(path / "manifest.json") or {}
        revisions.append(
            {
                "id": path.name,
                "date": manifest.get("synced_at", ""),
                "status": manifest.get("render_status", "pending"),
                "bytes": (path / "notebook.note").stat().st_size,
            }
        )
    return {"revisions": revisions}


def restore(cfg, request):
    with archive_lock(cfg.archive_dir):
        entry = notebook_entry(cfg, request["notebook"])
        serial = request["notebook"].split(":", 1)[0]
        paths = list_revisions(cfg.archive_dir, serial, entry["note_id"])
        path = next((p for p in paths if p.name == request["revision"]), None)
        if path is None:
            raise ValueError("Revision not found.")
        raw = (path / "notebook.note").read_bytes()
        manifest = read_json(path / "manifest.json") or {}
        if hashlib.sha256(raw).hexdigest() != manifest.get("content_sha256"):
            raise ValueError("The recovery copy failed its integrity check.")
        target = Path(request["output"])
        if target.exists() or target.is_symlink():
            raise ValueError(
                "Choose a new filename; recovery does not replace existing files."
            )
        atomic_write(target, raw)
        return {"path": str(target)}


def copy_archive(cfg, target, progress):
    """Copy, verify, and switch; preserve the original as a recovery copy."""
    source = cfg.archive_dir.resolve()
    target = target.resolve()
    if source == target or source in target.parents or target in source.parents:
        raise ValueError("Choose a separate folder outside the current archive.")
    if not target.is_dir():
        raise ValueError("Choose an existing empty destination folder.")
    with archive_lock(source), archive_lock(target):
        if any(
            p.name not in {".boox-sync.lock", ".DS_Store"} for p in target.iterdir()
        ):
            raise ValueError("The destination must be empty to avoid mixing archives.")
        paths = []
        for path in source.rglob("*"):
            if path.is_symlink():
                raise ValueError(
                    "Archive contains a symbolic link. Resolve it before copying."
                )
            if path.is_file() and path.name != ".boox-sync.lock":
                paths.append(path)
        required = sum(p.stat().st_size for p in paths)
        if shutil.disk_usage(target).free < required + 16 * 1024 * 1024:
            raise ValueError("The destination does not have enough free space.")
        with tempfile.TemporaryDirectory(prefix=".boox-copy-", dir=target) as temporary:
            stage = Path(temporary)
            for index, path in enumerate(paths):
                rel = path.relative_to(source)
                dest = stage / rel
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(path, dest)
                if _digest(path) != _digest(dest):
                    raise ValueError(
                        "Archive copy verification failed; the original is unchanged."
                    )
                progress({"phase": "copy", "completed": index + 1, "total": len(paths)})
            for path in stage.iterdir():
                if path.name != MARKER:
                    path.rename(target / path.name)
            (stage / MARKER).rename(target / MARKER)
    return {
        "archive_id": archive_identity(target),
        "archive_dir": str(target),
        "free_bytes": shutil.disk_usage(target).free,
    }


def _digest(path):
    with path.open("rb") as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest()


def dispatch(request, progress=lambda event: None):
    if request.get("protocol") != PROTOCOL_VERSION:
        raise ValueError(
            "App and sync engine versions do not match. Reinstall BOOX Sync."
        )
    command = request.get("command")
    if command == "health":
        return {
            "version": __version__,
            "renderer_version": RENDERER_VERSION,
            "python": platform.python_version(),
        }
    if command == "devices":
        return devices(request["adb_path"])
    if command == "prepare_archive":
        return prepare_archive(Path(request["path"]))
    if command == "render_file":
        # Used by packaging validation and user-selected standalone exports.
        source, target = Path(request["path"]), Path(request["output"])
        if source.resolve() == target.resolve():
            raise ValueError("PDF output cannot replace the source notebook.")
        result = render_note(source.read_bytes())
        if not result.pdf_bytes:
            raise ValueError(result.error or "PDF generation failed.")
        atomic_write(target, result.pdf_bytes)
        return {"path": str(target), "status": result.status, "reasons": result.reasons}
    cfg = request_config(request)
    if command == "library":
        return library(cfg)
    if command == "sync":
        connected = devices(cfg.adb_path)["devices"]
        device = next((d for d in connected if d["serial"] == cfg.device_serial), None)
        if device is None:
            raise ValueError("The selected tablet is not connected.")
        if device["state"] != "device":
            raise ValueError("Unlock the tablet and accept USB debugging for this Mac.")
        source = AdbKsyncSource(
            cfg.adb_path,
            cfg.device_serial,
            cfg.state_dir / "tmp" / cfg.device_serial,
            timeout=cfg.adb_timeout_seconds,
        )
        report = sync_once(cfg, source, progress=progress)
        return {"report": asdict(report), **library(cfg)}
    if command == "view":
        entry = notebook_entry(cfg, request["notebook"])
        # Resolve by identity, and use only this entry to avoid cross-device ambiguity.
        from boox_sync.archive import export_pdf
        from boox_sync.viewer import needs_render

        folder = notebook_folder(cfg.archive_dir, entry["archive_folder"])
        with archive_lock(cfg.archive_dir):
            if needs_render(folder):
                result = export_pdf(cfg.archive_dir, folder.name)
                if result.pdf_bytes is None:
                    raise ValueError(result.error or "PDF generation failed.")
            return {"path": str(folder / f"{folder.name}.pdf")}
    if command == "history":
        with archive_lock(cfg.archive_dir):
            return history(cfg, request["notebook"])
    if command == "restore":
        return restore(cfg, request)
    if command == "copy_archive":
        return copy_archive(cfg, Path(request["path"]), progress)
    if command == "diagnostics":
        data = library(cfg)
        report = data.get("last_report") or {}
        return {
            "protocol": PROTOCOL_VERSION,
            "renderer_version": RENDERER_VERSION,
            "python": platform.python_version(),
            "system": platform.system(),
            "notebook_status_counts": dict(
                Counter(n["status"] for n in data["notebooks"])
            ),
            "last_report_status": report.get("status"),
            "last_report_at": report.get("finished_at"),
            "outcome_status_counts": dict(
                Counter(o["status"] for o in report.get("outcomes", []))
            ),
        }
    raise ValueError("Unknown engine command.")


def main():
    # Allow the native parent to stop only this operation and its ADB clients.
    if hasattr(os, "setsid"):
        try:
            os.setsid()
        except OSError:
            pass
    try:
        line = sys.stdin.buffer.readline(1024 * 1024 + 1)
        if len(line) > 1024 * 1024:
            raise ValueError("Request exceeds the maximum size.")
        request = json.loads(line)
        if not isinstance(request, dict):
            raise ValueError("Expected a request object.")
        result = dispatch(
            request, progress=lambda data: emit({"event": "progress", **data})
        )
        emit({"event": "result", "ok": True, "data": result})
        return 0
    except Exception as exc:
        emit({"event": "result", "ok": False, "error": str(exc)})
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
