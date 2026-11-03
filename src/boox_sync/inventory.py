"""Last complete tablet inventories, separate from recoverable archive manifests."""

from datetime import datetime

from boox_sync.storage import read_json, write_json


def load_inventories(state_dir):
    raw = read_json(state_dir / "inventory.json") or {}
    devices = raw.get("devices")
    if raw.get("version") != 1 or not isinstance(devices, dict):
        return {}
    valid = {}
    for serial, snapshot in devices.items():
        if not isinstance(snapshot, dict):
            continue
        notes = snapshot.get("notebooks")
        if not isinstance(snapshot.get("checked_at"), str) or not isinstance(
            notes, dict
        ):
            continue
        if all(
            isinstance(key, str)
            and key
            and isinstance(note, dict)
            and isinstance(note.get("title"), str)
            and type(note.get("active")) is bool
            for key, note in notes.items()
        ):
            valid[serial] = snapshot
    return valid


def record_inventory(config, notebooks):
    if any(
        not isinstance(n.note_id, str)
        or not n.note_id
        or not isinstance(n.title, str)
        or type(n.active) is not bool
        for n in notebooks
    ) or len({n.note_id for n in notebooks}) != len(notebooks):
        raise ValueError("Invalid notebook inventory; previous tablet status retained")
    devices = load_inventories(config.state_dir)
    devices[config.device_serial] = {
        "checked_at": datetime.now().astimezone().isoformat(),
        "notebooks": {
            n.note_id: {"title": n.title, "active": n.active} for n in notebooks
        },
    }
    write_json(config.state_dir / "inventory.json", {"version": 1, "devices": devices})
