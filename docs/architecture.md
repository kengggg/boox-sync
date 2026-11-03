# Architecture

BOOX Sync has a SwiftUI macOS interface and a Python sync/rendering engine. The app bundles the engine, Python runtime, ADB, and fonts. A separate Python CLI uses the same archive implementation.

```text
SwiftUI / CLI → Python engine → ADB read operations → BOOX .ksync
                     ↓
         verified source → local archive → PDF
                     ↓
             previous good revisions
```

## Source map

| Path | Responsibility |
| --- | --- |
| `macos/Sources/BooxApp/` | Native windows, menu bar, settings, and helper lifecycle |
| `macos/Sources/BooxCore/` | Settings and automatic scheduling rules |
| `src/boox_sync/engine.py` | Protocol-versioned JSON-lines bridge and archive identity |
| `adb.py`, `usb.py` | Device discovery, authorization, bounded read-only transfers |
| `fleece.py`, `ksync.py`, `inventory.py` | Current metadata, lifecycle status, complete tablet inventory, deterministic notebook assembly |
| `note.py`, `proto.py`, `render.py` | Binary records, active shapes, page ordering, PDF rendering |
| `archive.py`, `storage.py`, `state.py` | Locks, atomic writes, history, and manifest/index recovery |
| `sync.py`, `viewer.py` | Per-notebook sync and verified current-PDF access |
| `cli.py`, `menu.py` | CLI and the older Python menu interface |
| `macos/build_app.py`, `scripts/` | Packaging, pinned ADB download, and release checks |

## Data and failure boundaries

The tablet is a read-only source. The engine reads the NOTE_TREE database and its companions, staged document/point records, and optional thumbnails. It decodes the current Fleece root and lifecycle status rather than inferring active notebooks from leftover files. `.ksync` is an undocumented staging area, not a guaranteed full-device backup API.

Inventory and source signatures are checked before and after transfer. A changed, incomplete, unknown, or damaged read fails explicitly. A failed inventory scan preserves the last complete inventory. An active notebook with missing content stays visible with a failure; it never replaces a good source with an empty notebook.

Each archive notebook contains a source `.note`, manifest, optional thumbnail, and PDF. Identity includes the device and notebook ID. Titles are presentation, not identity. Atomic replacement and a process lock protect local updates. Previous verified sources live in `.history`; invalid assembled inputs are retained in `.rejected`. The default history limit is five previous revisions in addition to the current source.

Tablet deletion does not delete Mac files. Renames can retain superseded folders. A damaged index is rebuilt from manifests. Restore exports a verified revision to a new path; it does not rewind state or write to the tablet. A failed PDF render keeps the earlier file but View refuses to call it current.

## App/engine boundary

The UI sends one versioned JSON request to a helper process and receives progress and a final structured result. Notebook bytes stay in files. Connection probes are independent of long syncs; archive operations are serialized. A selected archive's UUID marker prevents silently switching to a replacement disk or recreating a missing mount.

The app owns the schedule and must be running while the Mac is awake. Intervals start after a completed sync, missed runs do not accumulate, and manual sync bypasses allowed hours. See the [app guide](native-app.md) for overnight windows and time zones.

## Compatibility and changes

Synthetic tests exercise parser errors, lifecycle changes, transfer failure, history, recovery, text shaping, and scheduling. Native packaging tests verify the actual frozen interpreter. They do not establish compatibility with every BOOX firmware or macOS version; see [compatibility](compatibility.md).

Keep persisted formats backward compatible or provide an explicit, recoverable migration. Never infer a successful sync solely from a connected USB device, successful process exit, or existing PDF.
