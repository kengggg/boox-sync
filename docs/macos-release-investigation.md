# macOS release investigation

Status: investigation and agreed direction, 2026-11-03. Baseline: `v0.2.3` / `1c75735`.

## Agreed direction

Ship a polished **macOS-only application**, keeping the bundled Python engine where it is useful. Continue with SwiftUI/AppKit, Apple Silicon initially, and direct app/DMG distribution. The current deployment target is macOS 14; minimum-version installation still needs validation. A complete Swift engine rewrite is outside this release effort.

End users should need only the app and an authorized USB connection. Python, ADB, and fonts are already bundled. The Python CLI remains a development/support interface; Linux engine tests do not imply a Linux product or release commitment.

## What is already native

| Area | Current implementation |
| --- | --- |
| Main window, sidebar, settings, recovery, menu bar | SwiftUI with AppKit integration in `macos/Sources/BooxApp` |
| Schedule and settings model | Swift/Foundation in `macos/Sources/BooxCore` |
| Login, notifications, file dialogs, folder bookmarks | ServiceManagement, UserNotifications, and AppKit/Foundation |
| Background sync and PDFs | Bundled Python helper, launched through Foundation `Process`; versioned JSON progress/results |
| Tablet transport | Bundled official arm64 ADB; read-only operations |
| Distribution | Apple Silicon app/DMG, CI packaging and smoke tests, tag-triggered draft prereleases |

The existing UI does not need to be replaced to provide a native Mac experience. The release investigation should measure user-visible behavior before deciding to replace any engine component.

## Priorities and evidence required

### 1. Establish the installed-app baseline

Install the exact CI-built DMG on a clean Apple Silicon Mac without developer tools. Validate first launch, tablet authorization, folder selection, Save Settings, one successful sync, an unchanged repeat, and opening the resulting PDF. Use a synthetic notebook and separate archive.

The existing packaged smoke test verifies the helper and PDF output, not the complete interactive setup. The folder chooser's disabled confirmation was observed during automated interaction; reproduce it manually before deciding whether it is an app defect, automation limitation, or both. Direct path entry is a verified workaround, not the intended completion criterion for a polished setup flow.

Evidence: an installation checklist with app version, macOS version, synthetic steps, outcomes, and sanitized captures. Exercise the minimum supported macOS version and a current release; a deployment target alone is insufficient.

### 2. Make background work recover predictably

`EngineClient.perform` launches work off the main thread and limits response size, but its pipe read/process wait has no overall deadline. Cancelling its Swift task does not currently cancel the underlying process. Per-ADB timeouts do not cover a stalled helper or malformed response stream.

Investigate operation-specific deadlines and cancellation, a typed request/result boundary, and actionable errors when the helper exits or stops responding. Preserve notebook identity, atomic archive commits, five previous good revisions, and read-only tablet access.

Evidence: tests for missing/malformed final results, helper crashes and stalls, cancellation during each archive phase, and quitting during a sync. Verify that previous good files remain usable and the next operation can start. Measure startup, idle activity, and helper launch overhead before changing connection probing or process architecture.

### 3. Complete native integration validation

Exercise folder selection, Copy & Switch, recovery/save dialogs, menu-bar-only operation, close/reopen behavior, notifications when allowed or denied, launch-at-login approval, wake/reconnect, and removable archive disks. Validate keyboard navigation, narrow windows, and clear connection versus sync-error states.

Existing Swift tests cover schedule/settings/library behavior. They do not establish these installed-app integrations. Fix reproduced issues with focused regression coverage and document the remaining manual checks.

### 4. Rehearse signed distribution

The current draft is ad-hoc signed. Developer ID signing and notarization remain an unverified release path. Apple's notarization requirements include valid signatures for distributed executables, Developer ID, hardened runtime, and secure timestamps. Exercise the entire bundled app after signing, including Python extensions and ADB; do not infer success from an ad-hoc build. See [Apple's notarization guidance](https://developer.apple.com/documentation/security/notarizing-macos-software-before-distribution).

Use the existing signing hooks in `macos/build_app.py`, then verify notarization logs, the stapled ticket, and Gatekeeper behavior on a clean Mac. After a successful rehearsal, add restricted CI signing credentials and fail the release on signing/notarization errors. Credentials must stay outside the repository. The chosen direct-download route does not require App Sandbox; Apple's [distribution guidance](https://developer.apple.com/documentation/xcode/preparing-your-app-for-distribution) distinguishes its requirements from App Store distribution.

Publishing remains separate from producing a draft. An in-app updater is a later product decision; the first release can use manual GitHub downloads.

## Architecture changes to consider only with evidence

A typed Swift engine interface can improve error handling while retaining the current parser/renderer. If process supervision proves limiting, evaluate an XPC service; Apple provides [XPC](https://developer.apple.com/documentation/xpc) for managed helper-process communication, but moving to it still requires cancellation and archive-safety tests.

If a specific engine component later justifies a native replacement, compare both implementations on the same synthetic fixtures before switching. Core Graphics can [create PDF contexts](https://developer.apple.com/documentation/coregraphics/cgcontext), and [Core Text](https://developer.apple.com/documentation/coretext) provides text layout. Neither establishes equivalent BOOX rendering: page ordering, erasures, pen widths, Thai text, extractable text, and approximation reporting must be verified. Retain ADB unless a separate transport investigation establishes a reliable replacement.

## Release completion criteria

- Users install and complete setup without Python, Homebrew, or Terminal commands.
- Signed/notarized app launches under normal Gatekeeper behavior on the tested Macs.
- Native dialogs, background lifecycle, permissions, and recovery checks pass.
- Sync preserves current data and recovery history across disconnects, failures, and cancellation.
- CI verifies the actual macOS app artifact; compatibility claims match observed evidence.
- The release notes clearly describe supported devices, rendering limits, and the manual update process.

This document changes the release focus only. It does not claim that the remaining native checks, signing rehearsal, or proposed engine improvements have been completed.
