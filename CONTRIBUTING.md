# Contributing

Small, focused pull requests are welcome. Describe the problem and its expected behavior before proposing a large feature or changing archive formats.

## Development

The Python engine requires Python 3.11 or newer. Native builds require Apple Silicon and Xcode tools with Swift 6 or newer. CI uses Python 3.12 for packaging and macOS 15 for native checks; the app's deployment target is macOS 14.

```sh
uv sync --frozen --python 3.12 --extra dev
uv run pytest -q
uv run ruff check src tests macos/*.py scripts
uv run ruff format --check src tests macos/*.py scripts
uv build
uv run python scripts/check_distribution.py
swift test --package-path macos --scratch-path macos/.build --disable-sandbox
```

Use `uv run ruff format src tests macos/*.py scripts` to format Python. Keep Swift changes consistent with nearby code. Build the complete app using the commands in the README when changing packaging or the bridge. The packaged smoke test exercises the frozen helper with a system-only PATH and a synthetic Thai/Latin note; it does not contact a tablet.

The engine's installed version comes from `pyproject.toml`. Do not hardcode another version in Python or Swift. Run `uv lock` after changing project metadata or dependencies, then commit `uv.lock` with the change. Release tags must match the project version exactly.

## Tests and notebook privacy

Use `build_note`, `build_batch`, and `tests/fleece_fixture.py` to create synthetic examples. The two committed `.note` fixtures contain generated sample strokes/text and repeated dummy UUIDs. Do not add real notebook bytes, tablet-exported PDFs, metadata databases, personal titles, serials, or screenshots containing private content.

Hardware tests are optional and must use your own authorized tablet, read-only commands, and a separate archive and state directory. Create a disposable sample notebook yourself when testing edits or erasures. Never add device writes, install commands, or automatic deletion of retained Mac copies to a sync path.

Changes to parsers, archive commits, recovery, or scheduling need regression coverage for the failure being fixed. Visual PDF checks complement tests; a successful render is not proof of fidelity. State the device model, firmware, and validation limits without including its serial number.

## Pull requests and reports

Explain the user-visible result and the checks performed. Update the relevant guide and `CHANGELOG.md` for behavior changes. Avoid generated build outputs and unrelated formatting. Keep dependency updates separate when possible.

For a vulnerability, use [GitHub private vulnerability reporting](https://github.com/kengggg/boox-sync/security/advisories/new) when enabled. If that page is unavailable, open an issue asking for a private reporting channel without including exploit details or private notebook data. There is no promised response time or bug bounty. Ordinary bugs belong in the issue templates.

By submitting a contribution, you agree that it may be distributed under the project's MIT license. Keep third-party notices intact and identify the source and license of any new bundled asset or dependency.
