# Third-party components

The project's MIT license does not replace licenses on bundled dependencies.

| Component | Source / notices |
| --- | --- |
| Noto Sans Thai Regular and Bold | [Noto fonts](https://github.com/notofonts/noto-fonts/tree/main/hinted/ttf/NotoSansThai), SIL OFL 1.1; `src/boox_sync/fonts/OFL.txt` |
| ADB | [Android Platform Tools](https://developer.android.com/tools/releases/platform-tools); original `NOTICE.txt` shipped as `Contents/Resources/ADB-NOTICE.txt` |
| CPython | Python license copied from the build runtime into `Contents/Resources/Licenses/Python.txt` |
| ReportLab, uharfbuzz, Pillow, charset-normalizer | License/notice files copied from installed distributions into `Contents/Resources/Licenses/` |
| PyInstaller bootloader | PyInstaller notices copied into the app; its distribution exception is retained with the upstream license |

The app also includes the project `LICENSE` and `NOTICE` under `Contents/Resources`. Python wheels include the project license/notices and font license. The CLI's optional-on-macOS rumps/PyObjC UI is not bundled into the native app.

The ADB archive version, URL, and SHA-256 are in `scripts/platform-tools.json`. The initial checksum was cross-checked against Homebrew's Android Platform Tools cask. When updating, download the official versioned archive, verify its checksum independently, inspect its notices and architecture, and run a full package build/smoke test. Never replace the original tool notices with this summary.

Format references are credited in the root NOTICE. Format readers were independently implemented; upstream parser code is not vendored here. The Fleece reader handles only the subset used by the supported notebook metadata. App/menu artwork is maintained in `macos/Assets` as project assets under MIT.
