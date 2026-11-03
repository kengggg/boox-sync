# Command-line guide

Install Python 3.11 or newer, [uv](https://docs.astral.sh/uv/getting-started/installation/), and [Android Platform Tools](https://developer.android.com/tools/releases/platform-tools). Enable USB debugging on the BOOX, connect it by USB, and accept the computer on the tablet.

From this repository:

```sh
uv sync --frozen --extra dev
adb devices -l
uv run boox-sync init --serial YOUR_TABLET_SERIAL
uv run boox-sync probe
uv run boox-sync sync
uv run boox-sync menu
```

`init` explicitly creates the configuration and refuses to overwrite an existing file. `probe` reads the device without updating your archive. The menu command stays in the foreground; Quit stops its timer. A running worker finishes its bounded operation before Python exits. The menu and USB presence detection require macOS. Local parsing, rendering, and archive tests also run on Linux.

Default locations:

- Archive: `~/Documents/BOOX/`
- Configuration: `~/Library/Application Support/BOOX Sync/config.json`
- State and last report: `~/Library/Application Support/BOOX Sync/`

There is no built-in device serial. Initialize the CLI with `init --serial YOUR_SERIAL` before syncing. An explicitly named missing configuration fails rather than silently selecting another archive.

## Configuration

```json
{
  "device_serial": "YOUR_TABLET_SERIAL",
  "adb_path": "adb",
  "archive_dir": "~/Documents/BOOX",
  "state_dir": "~/Library/Application Support/BOOX Sync",
  "poll_seconds": 30,
  "adb_timeout_seconds": 120,
  "keep_revisions": 5,
  "render_after_sync": true,
  "remote_roots": ["/storage/emulated/0/note", "/sdcard/note"]
}
```

`adb_timeout_seconds` bounds each ADB operation. `keep_revisions` counts previous good source revisions in addition to the current copy. `render_after_sync: false` defers PDFs until View or Export. `remote_roots` applies only to manual-export discovery in `probe`.

For an isolated archive, create a separate config with its own `archive_dir` **and** `state_dir`, and pass `--config PATH` to each command.

## Use and recovery

```sh
uv run boox-sync notebooks
uv run boox-sync status
uv run boox-sync view 'Notebook title'
uv run boox-sync export 'Notebook title' -o /tmp/notebook.pdf
uv run boox-sync render /path/to/export.note -o /tmp/export.pdf
uv run boox-sync history 'Notebook title'
uv run boox-sync restore 'Notebook title' REVISION_ID -o /tmp/recovered.note
```

Notebook commands accept an exact title or a unique notebook ID prefix. Ambiguous titles list matching IDs. `restore` verifies the revision and copies it to a separate, new `.note` file; it does not rewind sync state or change the tablet. Render that recovered file with `render`.

Each notebook folder contains its `.note`, `manifest.json`, optional `cover.png`, and generated `.pdf`. Verified previous revisions are under `.history/`; unreadable assembled inputs are quarantined under `.rejected/`. Neither directory is a substitute for backing up the archive to another disk.

Transfers must match their complete file inventory before and after copying. Failed, partial, or changing reads leave the current archive alone. Source hashes are checked before skipping or viewing. Writes use temporary files and atomic replacement; a process lock serializes sync and render operations. A damaged state index is rebuilt from manifests. An interrupted source/manifest update is detected on the next pass, with the preceding verified revision retained.

Renaming creates a new folder and marks the old one as superseded. Renamed folders remain as additional historical copies. Title collisions are resolved repeatedly until an owned or unused folder is found. Tablet deletions never delete Mac files. The native notebook list defaults to **On tablet**, using the last complete, verified tablet inventory. **Archived** holds retained copies marked inactive or no longer listed, and copies from other tablets. **All** also includes older copies whose tablet status has not been checked yet. The last-checked time stays visible when offline. Inactive records are skipped during sync, even if BOOX retains their source files; active notebooks with transfer failures remain visible on the tablet list. Unknown metadata or incomplete reads leave the previous inventory unchanged. A failed PDF render retains the previous PDF, but View refuses to present it as current.

## Rendering quality

`ok` means the renderer encountered no known approximation; it is not a pixel-perfect fidelity guarantee. `approximate` carries reasons in the manifest, command output, and last sync report, and remains visible across unchanged syncs. The menu marks approximate, failed, and pending notebooks.

- Per-page dimensions and explicit page order are used when available. Missing dimensions use a fallback with `page-size`. Missing order uses creation time with `page-order`; manually reordered legacy pages may differ from the tablet.
- Newer records replace earlier records of the same shape. Point batches are joined by their recorded identity. Deletion markers were verified with a before/after erasure on the connected Note Air 3C. Unknown states or missing required points fail explicitly.
- Thai typed text uses bundled Noto Sans Thai and HarfBuzz shaping. Latin text and Thai can be mixed; PDF ActualText preserves the original text for readers that support it. Unsupported scripts fail instead of producing missing-glyph squares. Handwriting remains ink; there is no OCR.
- Charcoal, calligraphy, unsupported geometry, inferred image placement, and unsupported rich text styling are marked approximate. Template artwork, arbitrary fonts, and editable round-trip back to BOOX are not implemented.
- Original staged metadata, points, shapes, and superseded source records are retained inside the assembled `.note` for recovery and future parser improvements.

