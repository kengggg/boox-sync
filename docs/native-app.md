# Native macOS app

BOOX Sync is a SwiftUI application with a Dock window and menu bar item. A bundled Python helper runs the existing sync and rendering engine in a separate process. The local preview targets Apple Silicon and macOS 14 or newer; development validation was performed on macOS 27.0.1.

## First use

1. Open `BOOX Sync.app`, or copy it from the local DMG into Applications.
2. Connect the BOOX using a USB data cable. Enable USB debugging on the tablet and accept this Mac there.
3. Open Settings, choose the tablet under Device, and use Choose Folder under Archive. Save Settings. You can also enter an absolute folder path.
4. Choose Sync Now, then review Notebooks and Activity. Connection status and sync results are separate: a connected tablet can still have unavailable notebook content.

No device or archive is selected by default. The app does not automatically import the older CLI configuration. Selecting an existing BOOX Sync archive adopts its manifests without moving notebook files. Keep only one scheduler active when using the app and CLI with the same archive; the archive lock serializes writes, but separate schedulers produce confusing reports and unnecessary work.

The app only reads tablet data. It does not install software on the tablet or modify its notebooks. Firmware compatibility is currently verified on Note Air 3C firmware 4.2. The notebook list defaults to **On tablet**. **Archived** retains inactive, no-longer-listed, and other-tablet copies; **All** also shows copies whose tablet status is still unknown. A complete metadata scan updates these groups; disconnections and failed scans retain the previous list and its last-checked time. Inactive records are skipped. Active notebooks without readable staged files remain visible and report a transfer failure. No Mac copies are deleted.

## Settings

| Tab | Controls and behavior |
| --- | --- |
| Archive | Enter, choose, or reveal the sync folder. Copy Current Archive & Switch copies to a separate empty folder, hashes every file, and retains the original. Simply choosing another folder switches without copying. |
| Schedule | Automatic sync, interval, allowed weekdays and hours, and time zone. The default is every 30 seconds while connected, with no restricted hours. |
| Device | Explicit tablet selection, connection state, and Refresh Devices. Unauthorized and offline states are shown separately. |
| PDF & Recovery | Automatic background PDFs, five previous good revisions by default, and the timeout for each connection operation. |
| General | Optional launch at login, menu bar visibility, notifications, and privacy-limited diagnostics export. Login behavior changes immediately; other changes require Save Settings. |

Allowed hours are start-inclusive and end-exclusive. Overnight windows belong to their starting day: Friday 22:00–06:00 includes Saturday before 06:00. The interval starts when the preceding sync finishes; missed intervals do not queue a burst. Reconnecting within allowed hours starts a fresh sync. Sync Now bypasses automatic hours and pause, but still requires the selected tablet and prevents overlapping operations.

Automatic syncing requires the app to be running and the Mac to be awake. Closing the main window leaves the app running. Use Stop in the progress area or menu bar to cancel the current operation. Quit offers Finish and Quit, Keep Running, or Stop and Quit while work is active. Completed notebooks and recovery history remain available; sync again to finish interrupted work. A stopped sync is recorded separately from a failure and does not send a failure notification. The app checks again after wake and disk mount changes. A fixed schedule time zone remains fixed when traveling; Follow this Mac follows the system zone. Next-run timestamps are displayed in the Mac's time zone.

The selected folder has a UUID marker and a macOS bookmark. The engine checks the marker before using the folder; it will not silently recreate a missing disk path or use a replacement folder. Re-select a folder intentionally if its identity changes. Recovery History exports a verified previous `.note` to a new file; it does not replace the current notebook or write to the tablet.

Folder and save dialogs return focus to their originating window. Copy & Switch first chooses the destination, then asks for confirmation with the selected path. Native folder selection, saving its bookmark, Copy & Switch, and diagnostics export were checked with an isolated archive on the development Mac. Recovery export also passed with two synthetic notebook revisions. Clean-Mac setup and broader lifecycle/accessibility checks remain in [native validation](macos-native-validation.md).

General shows the actual macOS notification permission separately from the app's notification choices. If macOS blocks notifications, enable BOOX Sync in System Settings → Notifications. Saving unrelated settings does not repeatedly request permission.

Launch at login uses macOS Service Management. Install the app in Applications before enabling it. Registration may require approval in System Settings. This build does not enable login items automatically.

## Storage and process boundary

- App preferences: `~/Library/Application Support/BOOX Sync/native-settings.json`.
- Latest app status: `native-status.json` beside preferences.
- Engine state, reports, and per-device `inventory.json`: `archives/<archive UUID>/` under that application support folder. Inventory is separate from archive manifests, so index recovery does not lose tablet status.
- Notebook sources, PDFs, manifests, `.history`, and `.rejected`: in the chosen archive.
- `BOOX_SYNC_HOME=/absolute/path` overrides application support for isolated development tests.

The UI and helper exchange protocol-versioned JSON lines over stdin/stdout. Progress events update the window; the final result carries a structured outcome. No notebook bytes cross this control channel. The UI owns helper lifetimes and prevents concurrent modifying operations. Its typed protocol envelope rejects malformed versions/events, incomplete or duplicate results, and oversized messages. Task cancellation stops the owned helper process group, escalating after a two-second termination grace period. Nonblocking pipes prevent stuck input or inherited output handles from holding an operation indefinitely. Connection probes are independent of long syncs. Helpers use argument arrays, not shell command strings.

Helper deadlines use a monotonic clock. Device/health calls allow 20 seconds without an event and 30 seconds overall. Archive metadata operations allow 60 seconds without an event and 120 seconds overall. Sync, rendering, copying, and recovery allow at least 15 minutes without an event (or three times the configured connection timeout, whichever is longer), with a four-hour overall limit. These generous limits accommodate large notebooks; Stop provides immediate user cancellation. Valid progress refreshes the idle deadline but never the overall deadline.

Stopping between a source-file replacement and its manifest commit can leave a detectable incomplete update. The prior verified revision is retained, and a subsequent sync repairs it. Source/PDF/state interruption tests exercise this recovery; cancellation is not an all-notebook rollback.

The application is not App Sandbox enabled. ADB and the helper are bundled inside `Contents/Helpers`; dependency licenses and the original Android Platform Tools notice are under `Contents/Resources`. Diagnostics export versions and status counts, omitting notebook content, titles, device serials, and local paths. No telemetry or automatic upload is included.

## Build and check

Build on Apple Silicon with Xcode, Python 3.12, uv, and official Android Platform Tools installed. ADB must be alongside its original `NOTICE.txt`. Build prerequisites are not required on the eventual user's Mac.

```sh
uv sync --frozen --python 3.12 --extra app --extra dev
swift test --package-path macos --scratch-path macos/.build --disable-sandbox
uv run python scripts/fetch_platform_tools.py
uv run python macos/build_app.py --adb .tools/platform-tools/adb
uv run python macos/smoke_test.py 'dist/BOOX Sync.app'
```

Outputs are `dist/BOOX Sync.app` and `dist/BOOX-Sync-VERSION-arm64-local.dmg`. The default uses ad-hoc local signing. `--reuse-engine` reuses the frozen helper only when its source, fonts, and lockfile fingerprint matches. `--no-dmg` builds just the app.

The smoke test verifies signatures, bundled ADB, the JSON protocol, and a synthetic mixed Thai/Latin PDF while the helper's PATH contains only system directories. It never contacts a tablet. CI also runs `EngineClientTests/testPackagedEngineThroughNativeClient` after packaging, with `BOOX_TEST_ENGINE` pointing to the bundled helper. That verifies health, folder preparation, and PDF generation through the same Swift client used by the app. Hardware checks must use a separate archive and explicit read-only authorization.

For UI validation with an isolated configuration:

```sh
BOOX_SYNC_HOME=/tmp/boox-local-preview 'dist/BOOX Sync.app/Contents/MacOS/BOOXSync'
```

## Releases

CI builds local previews; version tags create draft prereleases for maintainer review. These builds are not Developer ID signed or notarized. See the [release guide](releases.md) for the exact workflow and future signing setup, and [compatibility](compatibility.md) for remaining native validation.
