"""Recover the index from archive manifests when state is missing or damaged."""

import time
from pathlib import Path

from boox_sync.archive import empty_state, state_entry
from boox_sync.storage import read_json, write_json


def load_state(state_dir: Path, archive_root: Path | None = None) -> dict:
    path = Path(state_dir) / "state.json"
    state = read_json(path)
    if not state or not isinstance(state.get("notes"), dict):
        state = empty_state()
    state["notes"] = {
        key: entry
        for key, entry in state["notes"].items()
        if isinstance(entry, dict)
        and isinstance(entry.get("archive_folder"), str)
        and isinstance(entry.get("note_id"), str)
    }
    if archive_root and archive_root.is_dir():
        for folder in sorted(archive_root.iterdir()):
            if (
                not folder.is_dir()
                or folder.is_symlink()
                or folder.name.startswith(".")
            ):
                continue
            manifest = read_json(folder / "manifest.json")
            if (
                not manifest
                or manifest.get("later_copy")
                or not manifest.get("note_id")
                or not manifest.get("device_serial")
            ):
                continue
            key = f"{manifest['device_serial']}:{manifest['note_id']}"
            entry = state_entry(manifest, folder.name)
            prior = state["notes"].get(key)
            if not prior or str(entry.get("synced_at", "")) >= str(
                prior.get("synced_at", "")
            ):
                # Older manifests did not record the remote signature.
                if (
                    prior
                    and entry.get("signature") is None
                    and prior.get("sha256") == entry.get("sha256")
                ):
                    entry["signature"] = prior.get("signature")
                state["notes"][key] = entry
    return state


def save_state(state_dir: Path, state: dict) -> None:
    path = Path(state_dir) / "state.json"
    if read_json(path) == state:
        return
    if path.exists() and read_json(path) is None:
        path.rename(path.with_name(f"state.corrupt-{time.time_ns()}.json"))
    write_json(path, state)
