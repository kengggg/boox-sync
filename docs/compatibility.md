# Compatibility and validation

BOOX Sync is experimental. The reader uses BOOX's undocumented `.ksync` staging area. It can preserve only content that the tablet exposes there; it is not a full-device backup.

## Current evidence

| Surface | Evidence and limit |
| --- | --- |
| BOOX Note Air 3C, firmware 4.2 | Read-only transfers, active/inactive inventory, rename/retention, and before/after erasure checked locally. Other devices and firmware remain unverified. |
| Native app | Apple Silicon; macOS 14 deployment target. Interactive development checks were on macOS 27.0.1. Minimum-version and clean-Mac installation need a manual pass. |
| Python engine | CI matrix covers Python 3.11–3.13 on Linux and macOS. Native UI and USB presence detection require macOS. |
| Packaging | CI builds an arm64 app/DMG and runs the frozen helper with synthetic data. This does not prove signing/notarization or real-device compatibility. |

A controlled comparison with four pages from tablet-exported PDFs found matching content and order on the tested samples. This is limited evidence, not a general fidelity guarantee. Private notebook files and detailed local test captures are not part of the public repository.

## Known limitations

- An active notebook may lack staged document files. Sync reports that failure and retains the prior good copy.
- `ok` means no known approximation was encountered, not pixel-perfect equivalence. `approximate` includes reasons that persist across unchanged syncs.
- Missing page order uses a creation-time fallback; arbitrary manual reordering cannot always be reconstructed. Missing dimensions use a marked fallback.
- Thai/Latin typed text uses Noto Sans Thai and HarfBuzz. Unsupported scripts fail explicitly. Handwriting remains ink; OCR is not included.
- Charcoal, calligraphy, some geometry, inferred image placement, and rich text styles can be approximate. Arbitrary fonts, template artwork, and editing back to BOOX are not implemented.
- Folder selection/save, Copy & Switch with synthetic files, diagnostics/recovery export, and blocked-notification status passed native checks on the development Mac. Clean-Mac dialog behavior remains unverified; see [native validation](macos-native-validation.md).
- Stop, retry after stopping, and Stop and Quit passed with the real bundled engine and a synthetic stalled ADB transport. Menu-bar interaction, login registration, notification delivery when allowed, wake/reconnect, and installation on another Mac still need broader end-to-end validation.
- Preview builds are ad-hoc signed, not Developer ID signed or notarized. No automatic updater is implemented.

## Manual release check

Use an isolated `BOOX_SYNC_HOME` and an empty test archive. On a clean Apple Silicon Mac, check install/launch, tablet authorization, folder selection, one sync and an unchanged repeat, an edit/erasure, deletion retention, current PDF opening, recovery export, schedule restrictions/manual override, reconnect/wake, notifications, optional login startup, and quitting during a sync.

Record the app version, macOS version, BOOX model/firmware, checks performed, and failures. Keep private captures outside the repo. Add a sanitized compatibility report through the bug template; passing one device does not imply support for all BOOX devices.
