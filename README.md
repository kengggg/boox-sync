# BOOX Sync

[![CI](https://github.com/kengggg/boox-sync/actions/workflows/tests.yml/badge.svg)](https://github.com/kengggg/boox-sync/actions/workflows/tests.yml)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)

A native macOS app that archives ONYX BOOX notebooks over USB and generates PDFs locally.

- Dock window and menu bar controls, with connection and sync status.
- Choose an archive folder, sync interval, allowed days, hours, and time zone.
- Automatic background PDFs and five previous verified revisions per notebook by default.
- Keep deleted notebooks in the Mac archive; distinguish **On tablet**, **Archived**, and **All**.
- Read-only tablet access. No account, telemetry, or cloud service.

**Experimental preview.** The app targets Apple Silicon and macOS 14 or newer. Tablet validation currently covers Note Air 3C firmware 4.2. BOOX's notebook format is undocumented; some notebooks have unavailable staged content and some PDF features are approximate. Keep an independent backup. See [compatibility and known limitations](docs/compatibility.md).

## Install and use

Preview downloads, when published, appear on [GitHub Releases](https://github.com/kengggg/boox-sync/releases). Current builds use local ad-hoc signing: **they are not Developer ID signed or notarized**, and macOS may block opening them. Signed distribution is planned. No automatic updater is included.

1. Copy **BOOX Sync.app** from the DMG to Applications.
2. Connect your BOOX with a USB data cable, enable USB debugging, and accept the Mac on the tablet.
3. Open Settings, select the tablet, and choose an existing archive folder.
4. Click **Sync Now**, then review Notebooks and Activity.

Python, ADB, and fonts are bundled; end users do not need Homebrew or Python. Closing the window keeps the app running. **Quit BOOX Sync** stops it. **Sync Now** overrides the automatic schedule. Launch at login is optional.

See the [app guide](docs/native-app.md) for settings, storage, and recovery. The [CLI guide](docs/cli.md) covers source installation, configuration, and local rendering.

## Build from source

On Apple Silicon, install Xcode command-line tools and [uv](https://docs.astral.sh/uv/getting-started/installation/), then:

```sh
git clone https://github.com/kengggg/boox-sync.git
cd boox-sync
uv sync --frozen --python 3.12 --extra app --extra dev
uv run python scripts/fetch_platform_tools.py
uv run python macos/build_app.py --adb .tools/platform-tools/adb
uv run python macos/smoke_test.py 'dist/BOOX Sync.app'
```

The download script fetches a pinned official Android Platform Tools archive, verifies SHA-256, and retains ADB's original notices. Outputs are in `dist/`. Build dependencies and local tools are excluded from Git.

## Contribute

Read [CONTRIBUTING.md](CONTRIBUTING.md) for setup, tests, and safe synthetic reproductions. Start with [architecture](docs/architecture.md) for the Swift/Python boundary and archive guarantees.

CI checks Python 3.11–3.13 on Linux and macOS, Swift tests, source packages, and an Apple Silicon app/DMG build. Pushing a matching version tag runs the same checks and creates a **draft prerelease** with checksums. See the [release guide](docs/releases.md). Publishing remains a maintainer decision.

## License and credits

Project code is [MIT licensed](LICENSE). Bundled fonts and tools retain their own licenses; see [NOTICE](NOTICE) and [third-party components](docs/third-party.md). BOOX Sync is an independent project, not affiliated with or endorsed by ONYX.
