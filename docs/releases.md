# Releases

The initial release policy is **version tag → checks → Apple Silicon build → draft prerelease**. Drafts require a maintainer to inspect and publish them. Regular pushes and pull requests run the same reusable checks without creating releases. CI artifacts expire after 14 days; published release assets are separate.

## Prepare a version

1. Update `project.version` in `pyproject.toml` and the changelog. Use a numeric `MAJOR.MINOR.PATCH` version. The Python metadata, helper health response, and app bundle use that version.
2. Run `uv lock`, local checks from CONTRIBUTING, and the full app smoke test.
3. Merge the reviewed change to `main` and wait for CI.
4. Tag that commit and push the tag. For the current version:

   ```sh
   git tag -a v0.2.3 -m 'BOOX Sync 0.2.3 preview'
   git push origin v0.2.3
   ```

Do not reuse a version for changed code or move a published tag. GitHub Actions rejects a tag that does not match `pyproject.toml`.

## What automation does

The release workflow runs Python tests on Linux/macOS, lint and distribution checks, Swift tests, and a complete arm64 app build on macOS 15. The build fetches ADB 37.0.1 from a versioned Google URL and verifies its pinned SHA-256. Runtime dependencies use `uv.lock`; Actions use full commit hashes. Runner images and Xcode still receive upstream updates, so this is a pinned-input build process, not a bit-for-bit reproducibility claim.

The frozen-helper smoke test verifies signatures, ADB, JSON protocol/version, and a synthetic Thai/Latin PDF. The workflow verifies the DMG, then assembles only:

- `BOOX-Sync-VERSION-arm64-local.dmg`
- `build-info.json` with source commit, versions, architecture, signing mode, and source/tool hashes
- `SHA256SUMS`

All checks must pass before a separate job receives `contents: write` and creates the draft. Fork PR jobs receive no signing secrets. The workflow never publishes automatically and never replaces an existing release or its assets. If a draft exists from a previous attempt, inspect it manually; do not delete a published release to rerun a tag.

The draft is marked **prerelease** and clearly identifies ad-hoc signing. Review its changelog and clean-Mac install checks before publishing. Verify downloaded assets with `shasum -a 256 -c SHA256SUMS`. Users should get both files from the same release; checksums detect corruption, not a compromised publisher.

## GitHub repository setup

Use `kengggg/boox-sync`, default branch `main`. Enable Issues, Actions, and private vulnerability reporting. Give Actions read-only default permissions; the draft job explicitly requests release write access. The workflow needs only the repository's automatic `GITHUB_TOKEN`, not a personal access token.

Protect `main` against force-push/deletion and require CI before merging contributions. Restrict creation/update/deletion of `v*` tags to maintainers using a tag ruleset. Review the currently reported check names before making them required; this repository does not claim those server-side rules are configured merely because these files exist.

Old local history may contain personal test identifiers. Before the first public push, review history as well as the working tree. A clean initial public commit can preserve the earlier Git history in the private local repository without publishing it.

## Developer ID signing later

The current workflow intentionally builds local previews and requires no Apple credentials. The builder supports an installed Developer ID Application identity and an existing `notarytool` keychain profile:

```sh
uv run python macos/build_app.py --adb .tools/platform-tools/adb \
  --identity 'Developer ID Application: YOUR NAME (TEAMID)' \
  --notary-profile YOUR_PROFILE
```

This path still needs a real signed rehearsal. It signs nested code with hardened runtime, submits the DMG, and staples/validates the ticket. Before switching automated releases to signed builds, add an isolated CI keychain, restricted signing secrets/environment, cleanup, notarization failure checks, and clean-Mac Gatekeeper validation. Do not silently fall back to ad-hoc signing on a failed signed release. Never commit or paste signing credentials into an issue.

No PyPI publication, App Store distribution, or in-app updater is configured.
