"""Command line entry for boox-sync."""

import argparse
import subprocess
import sys
import json
import shutil
import hashlib
from pathlib import Path

from boox_sync.adb import AdbKsyncSource, AdbUsbSource
from boox_sync.config import (
    Config,
    ConfigError,
    load_config,
    validate_config,
    save_config,
    default_config_path,
)
from boox_sync.menu import run_menu, status_line
from boox_sync.render import render_note
from boox_sync.sync import SyncReport, sync_once
from boox_sync.usb import usb_present
from boox_sync.storage import atomic_write, archive_lock, read_json
from boox_sync.viewer import default_opener, find_entry, load_state, view_notebook


def main(argv: list[str] | None = None) -> int:
    try:
        return _main(argv)
    except (
        OSError,
        ValueError,
        RuntimeError,
        LookupError,
        subprocess.SubprocessError,
    ) as exc:
        print(f"error: {exc}")
        return 1


def _main(argv: list[str] | None = None) -> int:
    """Run a boox-sync subcommand.

    Args:
        argv: Arguments after the program name. Defaults to sys.argv.

    Returns:
        Process exit code.
    """
    parser = argparse.ArgumentParser(prog="boox-sync")
    commands = parser.add_subparsers(dest="command", required=True)

    render = commands.add_parser("render")
    render.add_argument("path")
    render.add_argument("-o", dest="output")

    sync = commands.add_parser("sync")
    sync.add_argument("--config", type=Path)

    probe = commands.add_parser("probe")
    probe.add_argument("--config", type=Path)

    notebooks = commands.add_parser("notebooks")
    notebooks.add_argument("--config", type=Path)

    view = commands.add_parser("view")
    view.add_argument("notebook")
    view.add_argument("--config", type=Path)
    view.add_argument("--no-open", action="store_true")

    export = commands.add_parser("export")
    export.add_argument("notebook")
    export.add_argument("--config", type=Path)
    export.add_argument("-o", dest="output", type=Path)

    menu = commands.add_parser("menu")
    menu.add_argument("--config", type=Path)

    init = commands.add_parser(
        "init", help="Create a config without changing the tablet"
    )
    init.add_argument("--config", type=Path)
    init.add_argument("--serial", required=True)
    init.add_argument("--archive", type=Path)
    status = commands.add_parser("status", help="Show the last sync report")
    status.add_argument("--config", type=Path)
    history = commands.add_parser("history", help="List recoverable notebook revisions")
    history.add_argument("notebook")
    history.add_argument("--config", type=Path)
    restore = commands.add_parser(
        "restore", help="Copy a revision to a separate .note file"
    )
    restore.add_argument("notebook")
    restore.add_argument("revision")
    restore.add_argument("-o", "--output", type=Path, required=True)
    restore.add_argument("--config", type=Path)

    args = parser.parse_args(argv)
    if args.command == "render":
        return render_command(
            Path(args.path), Path(args.output) if args.output else None
        )
    if args.command == "init":
        target = args.config or default_config_path()
        if target.exists():
            raise ValueError(f"config already exists: {target}")
        config = Config(
            device_serial=args.serial, adb_path=shutil.which("adb") or Config().adb_path
        )
        if args.archive:
            config.archive_dir = args.archive.expanduser()
        print(save_config(config, target))
        return 0
    config = load_config(args.config)
    validate_config(config)
    if args.command == "status":
        report = read_json(config.state_dir / "last-report.json")
        print(
            json.dumps(report, indent=2, ensure_ascii=False)
            if report
            else "No sync report yet"
        )
        return 0 if report else 1
    if args.command in {"history", "restore"}:
        return history_command(
            config,
            args.notebook,
            getattr(args, "revision", None),
            getattr(args, "output", None),
        )
    if args.command == "sync":
        return sync_command(config)
    if args.command == "probe":
        return probe_command(config)
    if args.command == "notebooks":
        return notebooks_command(config)
    if args.command == "view":
        return view_command(config, args.notebook, no_open=args.no_open)
    if args.command == "export":
        return export_command(config, args.notebook, args.output)
    run_menu(config)
    return 0


def render_command(path: Path, output: Path | None) -> int:
    """Render one local .note file and print render_status.

    Args:
        path: Local .note file.
        output: PDF path. Defaults to the note path with a .pdf suffix.

    Returns:
        0 for ok or approximate, 1 when rendering fails.
    """
    result = render_note(path.read_bytes())
    if result.pdf_bytes is not None:
        target = output if output is not None else path.with_suffix(".pdf")
        if target.resolve() == path.resolve():
            raise ValueError("PDF output must not replace the source notebook")
        atomic_write(target, result.pdf_bytes)
    if result.error:
        print(f"error={result.error}")
    print(f"render_status={result.status}")
    print("reasons=" + ",".join(result.reasons))
    print(f"page_count={result.page_count}")
    print(f"page_size_source={result.page_size_source}")
    return 0 if result.status != "failed" else 1


def sync_command(config: Config) -> int:
    """Run one sync if the tablet is on USB and adb is authorized.

    Args:
        config: Loaded config.

    Returns:
        0 when every notebook is archived or skipped, otherwise 1.
    """
    try:
        validate_config(config)
    except ConfigError as exc:
        print(exc)
        return 1
    report = sync_attached(config)
    print(status_line(_kind_for(report), _approximate_count(report)))
    if report.error:
        print(f"error={report.error}")
    for item in report.outcomes:
        details = item.error or ", ".join(item.reasons)
        print(
            f"{item.title or item.note_id}: {item.status}"
            + (f" — {details}" if details else "")
        )
    if report.status == "absent":
        print("adb not invoked")
    return 0 if report.status == "ok" else 1


def sync_attached(config: Config, source_factory=None) -> SyncReport:
    """Sync only while the configured USB device is present.

    Args:
        config: Loaded config.
        source_factory: Builds the notebook source from the config. Called
            only after adb is authorized, so an absent tablet never
            reaches it.

    Returns:
        Sync report, or a report whose status is absent, adb_missing,
        unauthorized, or no_notes.
    """
    try:
        validate_config(config)
        if not usb_present(config.device_serial):
            return SyncReport(status="absent")
        if not Path(config.adb_path).is_file():
            return SyncReport(status="adb_missing")
        if not _adb_authorized(config.adb_path, config.device_serial):
            return SyncReport(status="unauthorized")
        factory = source_factory or ksync_source
        source = factory(config)
        return sync_once(config, source)
    except Exception as exc:
        return SyncReport("failed", error=str(exc))


def ksync_source(config: Config) -> AdbKsyncSource:
    """Build the live notebook source over the ksync staging area."""
    return AdbKsyncSource(
        config.adb_path,
        config.device_serial,
        config.state_dir / "tmp" / config.device_serial,
        timeout=config.adb_timeout_seconds,
    )


def notebooks_command(config: Config) -> int:
    """Print every synced notebook with its render status."""
    state = load_state(config.state_dir, config.archive_dir)
    entries = sorted(
        state.get("notes", {}).values(), key=lambda entry: str(entry.get("title"))
    )
    if not entries:
        print("no notebooks synced yet")
        return 1
    for entry in entries:
        manifest = (
            read_json(config.archive_dir / entry["archive_folder"] / "manifest.json")
            or {}
        )
        print(
            f"{entry.get('title')}  {entry.get('note_id')}  {manifest.get('render_status', 'unknown')}"
        )
    return 0


def view_command(config: Config, needle: str, no_open: bool = False) -> int:
    """Render one notebook on demand and open its PDF."""
    try:
        status, error, pdf = view_notebook(
            config.state_dir,
            config.archive_dir,
            needle,
            opener=None if no_open else default_opener,
        )
    except LookupError as exc:
        print(exc)
        return 1
    if pdf is None:
        print(error or status)
        return 1
    if no_open:
        print(pdf)
    return 0


def export_command(config: Config, needle: str, output: Path | None) -> int:
    """Render one archived notebook to a PDF without opening it."""
    from boox_sync.archive import export_pdf

    state = load_state(config.state_dir, config.archive_dir)
    try:
        found = find_entry(state, needle)
    except LookupError as exc:
        print(exc)
        return 1
    if found is None:
        print(f"no notebook matches {needle!r}")
        return 1
    _key, entry = found
    result = export_pdf(config.archive_dir, entry["archive_folder"], output=output)
    if result.error:
        print(f"error={result.error}")
    print(f"render_status={result.status}")
    print("reasons=" + ",".join(result.reasons))
    print(f"page_count={result.page_count}")
    print(f"page_size_source={result.page_size_source}")
    if output is not None and result.pdf_bytes is not None:
        print(output)
    return 0 if result.status != "failed" else 1


def probe_command(config: Config) -> int:
    """Print device and staging info. Write no archive files.

    Args:
        config: Loaded config.

    Returns:
        0 when a listing was printed, 1 when the tablet or adb is unavailable.
    """
    import tempfile

    print(f"serial={config.device_serial}")
    if not usb_present(config.device_serial):
        print("Not connected")
        print("adb not invoked")
        return 1
    if not Path(config.adb_path).is_file():
        print("Android platform tools not found")
        return 1
    devices = subprocess.run(
        [config.adb_path, "devices", "-l"], capture_output=True, text=True, timeout=10
    )
    sys.stdout.write(devices.stdout)
    if not any(
        adb_line_state(line, config.device_serial) == "device"
        for line in devices.stdout.splitlines()
    ):
        print("adb does not list an authorized device")
        return 1
    for prop in ("ro.product.model", "ro.build.display.id"):
        proc = subprocess.run(
            [config.adb_path, "-s", config.device_serial, "shell", "getprop", prop],
            capture_output=True,
            text=True,
            timeout=10,
        )
        print(f"{prop}={proc.stdout.strip()}")
    with tempfile.TemporaryDirectory(prefix="boox-probe-") as temporary:
        source = AdbKsyncSource(
            config.adb_path,
            config.device_serial,
            Path(temporary),
            timeout=config.adb_timeout_seconds,
        )
        try:
            notebooks = source.list_notebooks()
        except Exception as exc:
            print(f"list_error={exc}")
            return 1
    for notebook in notebooks:
        print(
            f"notebook {notebook.title!r} id={notebook.note_id} sig={notebook.signature[:12]}"
        )
    exports = AdbUsbSource(config.adb_path, config.device_serial, config.remote_roots)
    try:
        notes = exports.list_notes()
    except Exception as exc:
        print(f"list_error={exc}")
        return 1
    for path, size, mtime in notes:
        print(f"{size} {mtime} {path}")
    if notes:
        print(f"stat_sample={notes[0][1]} {notes[0][2]} {notes[0][0]}")
    return 0


def history_command(config, needle, revision=None, output=None):
    from boox_sync.archive import list_revisions

    with archive_lock(config.archive_dir):
        found = find_entry(load_state(config.state_dir, config.archive_dir), needle)
        if not found:
            raise LookupError(f"no notebook matches {needle!r}")
        key, entry = found
        serial = key.split(":", 1)[0]
        revisions = list_revisions(config.archive_dir, serial, entry["note_id"])
        if revision is None:
            for path in revisions:
                manifest = read_json(path / "manifest.json") or {}
                print(
                    f"{path.name}  {manifest.get('synced_at')}  {manifest.get('title')}"
                )
            if not revisions:
                print("No previous revisions yet")
            return 0
        matches = [p for p in revisions if p.name == revision]
        if not matches:
            raise LookupError(
                "revision not found; use boox-sync history to list revisions"
            )
        raw = (matches[0] / "notebook.note").read_bytes()
        manifest = read_json(matches[0] / "manifest.json") or {}
        if hashlib.sha256(raw).hexdigest() != manifest.get("content_sha256"):
            raise ValueError("revision integrity check failed")
        if output.exists():
            raise ValueError("restore output already exists; choose a new path")
        atomic_write(output, raw)
        print(output)
        return 0


def adb_line_state(line: str, serial: str) -> str | None:
    """Return the adb state for this serial, or None when the line is something else.

    ``adb devices`` separates the serial and state with a tab. ``adb devices -l``
    pads the serial with spaces. In both forms the state is the second word.
    """
    parts = line.split()
    if len(parts) >= 2 and parts[0] == serial:
        return parts[1]
    return None


def _adb_authorized(adb_path: str, serial: str) -> bool:
    proc = subprocess.run(
        [adb_path, "devices"], capture_output=True, text=True, timeout=10
    )
    return any(
        adb_line_state(line, serial) == "device" for line in proc.stdout.splitlines()
    )


def _kind_for(report: SyncReport) -> str:
    if report.status == "ok":
        if any(item.status == "approximate" for item in report.outcomes):
            return "approximate"
        return "up_to_date"
    mapping = {
        "absent": "absent",
        "unauthorized": "unauthorized",
        "adb_missing": "adb_missing",
        "no_notes": "no_notes",
        "failed": "notebook_failed",
    }
    return mapping.get(report.status, "notebook_failed")


def _approximate_count(report: SyncReport) -> int:
    return sum(1 for item in report.outcomes if item.status == "approximate")


if __name__ == "__main__":
    raise SystemExit(main())
