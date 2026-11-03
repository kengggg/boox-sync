# macOS native validation

Local implementation pass on `investigate/macos-native`, 2026-11-03. The baseline was `v0.2.3`; the branch build retains that version until the next release is prepared. Checks ran on the Apple Silicon development Mac, macOS 27.0.1. This is not a clean-Mac or macOS 14 certification.

## Implemented and checked

| Area | Evidence | Boundary |
| --- | --- | --- |
| Helper supervision | Swift tests exercise split progress/results, malformed versions/events, incomplete/duplicate results, crashes, oversized lines, silent helpers, blocked stdin, closed/inherited stdout, final-result-without-exit, overall timeout despite progress, cancellation, SIGTERM-resistant children, and reuse after stopping concurrent calls. | Synthetic helper processes; production limits are longer than test deadlines. |
| Archive interruption | 14 Python cases kill a real writer with SIGTERM/SIGKILL at snapshot, before/after source replacement, manifest, before/after PDF replacement, and state commits. A verified previous source remains available; retry produces a valid current source/PDF and an unchanged repeat skips. | Synthetic notebooks, not every possible filesystem/hardware failure. |
| Packaged app | Ad-hoc signature verification, bundled ADB version, synthetic mixed Thai/Latin PDF, and DMG checksum verification passed. Swift calls the packaged helper for health, folder preparation, and PDF rendering. | Developer ID/notarization and Gatekeeper on a clean Mac are untested. |
| Folder selection | Choose Folder → native picker → Use folder → Save Settings passed. The saved UUID matched the archive marker and a bookmark was persisted. Settings remained visible after the dialog fix. | Isolated local folder on the development Mac. |
| Copy & Switch | Destination picker → confirmation showing the path → copy completed. Destination preference/identity persisted; the synthetic file matched the original, which remained present. | Small synthetic archive, no removable disk. |
| Recovery export | Native Recovery History → Recover → save dialog completed. Exported bytes exactly matched the earlier synthetic revision; the current notebook still matched the newer revision. | The row action required a coordinate click in automation; a full keyboard/accessibility pass remains open. |
| Diagnostics export | Native save dialog completed; exported JSON contained only the expected version/status fields. | Synthetic archive without tablet content. |
| Notifications | General displayed the actual denied macOS status and directions to System Settings. No redundant permission button was shown when denied. | Permission was not changed; allowed delivery and first authorization remain untested. |
| Stop / retry / quit | A separate copy of the app used the real frozen engine with only bundled ADB replaced by a synthetic transport. The transport listed a fake tablet and stalled reads. Stop cleared busy state, showed a neutral stopped notice, and re-enabled Sync Now. Another sync started. Stop and Quit exited the app/helper and persisted `cancelled` status. | This validates the native control/process path; archive interruption safety is covered separately above. No real notebook was read for this test. |

## Reproduced dialog issues

The baseline standalone panel returned focus to the main window, leaving Settings behind it. Panels now attach to the originating window and bring it forward on completion. Copy & Switch also opened a picker while its SwiftUI confirmation sheet was closing; its picker confirmation remained disabled in the reproduced flow. Choosing the destination first and then presenting a native confirmation removed that overlap, and the full copy flow passed.

The earlier general folder-picker failure could not be attributed solely to this overlap. Ordinary folder selection subsequently passed. Avoid treating one automation observation as a universal macOS defect or claiming compatibility beyond the tested host.

## Repeatable automated checks

```sh
uv run --frozen pytest -q
swift test --package-path macos --scratch-path macos/.build --disable-sandbox
uv run --frozen python macos/build_app.py --adb .tools/platform-tools/adb
uv run --frozen python macos/smoke_test.py 'dist/BOOX Sync.app'
BOOX_TEST_ENGINE="$PWD/dist/BOOX Sync.app/Contents/Helpers/BOOXEngine.app/Contents/MacOS/boox-engine" \
  swift test --package-path macos --scratch-path macos/.build --disable-sandbox \
  --filter EngineClientTests/testPackagedEngineThroughNativeClient
hdiutil verify dist/*.dmg
```

GitHub [CI run 41613802874](https://github.com/kengggg/boox-sync/actions/runs/41613802874) passed all eight jobs for implementation commit `8c92417`, including app/DMG packaging and the native-client integration test.

The full local suites passed: 195 Python tests and 26 Swift tests with the packaged-helper environment supplied. Without that environment, the packaged-helper Swift test is explicitly skipped and CI runs it after building the app. CI also checks lint, formatting, workflow syntax, source packages, and its Python/OS matrix.

## Remaining release gates

- Clean Apple Silicon Mac without developer tools, including macOS 14 and a current macOS release.
- Fresh tablet authorization and end-to-end sync/open/recovery export using a synthetic notebook on supported hardware.
- Menu-bar-only close/reopen behavior, keyboard/narrow-window checks, wake/reconnect, and removable-disk loss/reappearance.
- Notification authorization and delivery when allowed; optional launch-at-login registration/approval and startup.
- Startup/idle/helper-launch measurements before changing the probe cadence or process architecture.
- Developer ID signing, hardened runtime, notarization, stapling, and normal Gatekeeper installation. Credentials are not configured in this pass.

The public draft release remains unpublished. None of these local results authorize or establish a signed public release.
