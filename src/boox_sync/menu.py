"""Menu bar status lines and once-per-attach notifications."""

import time
from concurrent.futures import ThreadPoolExecutor

from boox_sync.viewer import load_state, view_notebook, default_opener
from boox_sync.sync import SyncReport
from boox_sync.storage import read_json

NOTIFY_KINDS = {"unauthorized", "adb_missing", "no_notes", "notebook_failed"}


def status_line(kind: str, approximate_count: int = 0) -> str:
    """Return the spec status line for a menu situation.

    Args:
        kind: One of the situation names in the design spec.
        approximate_count: Notebooks rendered as approximate.

    Returns:
        The status line shown in the menu.
    """
    if kind == "approximate":
        return f"Up to date, {approximate_count} approximate"
    lines = {
        "absent": "Not connected",
        "syncing": "Syncing",
        "up_to_date": "Up to date",
        "unauthorized": "Unlock the tablet and accept this computer",
        "adb_missing": "Android platform tools not found",
        "no_notes": "No notebooks found on the tablet.",
        "notebook_failed": "Last sync failed",
    }
    return lines[kind]


class BooxMenu:
    """Status, Sync now, and notification state for one menu bar."""

    TITLE = "BOOX"
    ITEMS = ("Sync now", "Open archive", "Quit")

    def __init__(self) -> None:
        """Start disconnected, with an empty notification session."""
        self.status_line = status_line("absent")
        self.last_error = ""
        self.approximate_titles: list[str] = []
        self.sync_enabled = False
        self.notifications: list[str] = []
        self._seen: set[str] = set()

    def update(
        self,
        kind: str,
        *,
        approximate_count: int = 0,
        error_key: str | None = None,
        titles: list[str] | None = None,
        error: str | None = None,
    ) -> None:
        """Apply one situation.

        Args:
            kind: Situation name.
            approximate_count: Used when kind is approximate.
            error_key: Notification identity. A repeated key in one attach
                session does not notify again. Disconnecting clears the session.
            titles: Notebook titles listed while kind is approximate.
        """
        self.status_line = status_line(kind, approximate_count)
        self.sync_enabled = kind not in {"absent", "syncing"}
        if kind == "approximate":
            self.approximate_titles = list(titles or [])
        else:
            self.approximate_titles = []
        if kind in {"up_to_date", "approximate", "absent"}:
            self.last_error = ""
        elif kind in NOTIFY_KINDS:
            self.last_error = error or self.status_line
        if kind == "absent":
            self._seen.clear()
            return
        if kind not in NOTIFY_KINDS or not error_key or error_key in self._seen:
            return
        self._seen.add(error_key)
        self.notifications.append(error_key)


def notebook_label(entry: dict) -> str:
    """Return the menu label for one synced notebook.

    The folder name carries the suffix that keeps duplicate titles apart.
    The bare title collides in the menu, which keys items by their label.
    """
    return str(entry.get("archive_folder") or entry.get("title") or "Untitled")


def status_menu_rows(menu: BooxMenu) -> list[str]:
    """Return the menu rows for one status, including approximate titles."""
    rows = [menu.status_line]
    if menu.last_error:
        rows.append(menu.last_error)
    rows.extend(menu.approximate_titles)
    rows.extend(BooxMenu.ITEMS)
    return rows


class PresenceRunner:
    """Poll sync while a presence signal says the tablet is attached.

    A tick that arrives while a sync is already running does nothing.
    A tick while the tablet is absent does not call the sync function.
    """

    def __init__(self, config, menu: BooxMenu, present, sync) -> None:
        """Store the config, menu, presence probe, and sync callable.

        Args:
            config: Sync config passed to sync.
            menu: Status object updated before and after each pass.
            present: Callable returning True while the tablet is attached.
            sync: Callable taking config and returning a sync report. Called
                only when present() is true and no pass is in progress.
        """
        self.config = config
        self.menu = menu
        self.present = present
        self.sync = sync
        self._busy = False

    def tick(self, on_change=None) -> None:
        """Run one poll.

        Args:
            on_change: Called after the menu status changes, including once
                when a sync starts, so the status item can show Syncing.
        """
        if self._busy:
            return
        if not self.present():
            self.menu.update("absent")
            if on_change is not None:
                on_change()
            return
        self._busy = True
        try:
            self.menu.update("syncing")
            if on_change is not None:
                on_change()
            try:
                report = self.sync(self.config)
            except Exception as exc:
                report = SyncReport("failed", error=str(exc))
            _apply_report(self.menu, report)
            if on_change is not None:
                on_change()
        finally:
            self._busy = False


class BackgroundRunner:
    """Run USB probes and sync in one worker; apply results on the UI thread."""

    def __init__(self, config, menu, present, sync, executor, clock=time.monotonic):
        self.config, self.menu = config, menu
        self.present, self.sync, self.executor, self.clock = (
            present,
            sync,
            executor,
            clock,
        )
        self.future = None
        self.kind = None
        self.connected = False
        self.next_probe = self.next_sync = 0.0

    def tick(self):
        now = self.clock()
        if self.future is not None:
            if not self.future.done():
                return
            try:
                result = self.future.result()
                if self.kind == "sync":
                    _apply_report(self.menu, result)
                    if result.status == "absent":
                        self.connected = False
                    self.next_sync = now + self.config.poll_seconds
                elif not result:
                    self.connected = False
                    self.next_sync = 0.0
                    self.menu.update("absent")
                else:
                    if not self.connected:
                        self.next_sync = 0.0
                    self.connected = True
            except Exception as exc:
                _apply_report(self.menu, SyncReport("failed", error=str(exc)))
                self.next_sync = now + self.config.poll_seconds
            self.future = None
        if self.connected and now >= self.next_sync:
            self.menu.update("syncing")
            self.kind = "sync"
            self.future = self.executor.submit(self.sync, self.config)
        elif now >= self.next_probe:
            self.next_probe = now + 1.0
            self.kind = "probe"
            self.future = self.executor.submit(self.present)

    def sync_now(self):
        self.next_sync = 0.0
        self.tick()


def run_menu(config) -> None:
    """Run the BOOX status item until Quit.

    Args:
        config: Sync config. adb is used only while the USB serial is present.
    """
    import rumps

    from boox_sync.cli import ksync_source, sync_attached
    from boox_sync.usb import usb_present

    menu = BooxMenu()
    executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="boox-worker")
    runner = BackgroundRunner(
        config,
        menu,
        present=lambda: usb_present(config.device_serial),
        sync=lambda cfg: sync_attached(cfg, source_factory=ksync_source),
        executor=executor,
    )

    class App(rumps.App):
        def __init__(self) -> None:
            super().__init__(BooxMenu.TITLE, quit_button=None)
            self._status = rumps.MenuItem(menu.status_line)
            self._error = rumps.MenuItem(menu.last_error or " ")
            self._error_key = self._error.title
            self._notes = rumps.MenuItem("Notebooks")
            self._sync = rumps.MenuItem(BooxMenu.ITEMS[0], callback=self.sync_now)
            self._open = rumps.MenuItem(BooxMenu.ITEMS[1], callback=self.open_archive)
            self._quit = rumps.MenuItem(
                BooxMenu.ITEMS[2], callback=rumps.quit_application
            )
            self.menu = [
                self._status,
                self._error,
                self._notes,
                self._sync,
                self._open,
                self._quit,
            ]
            self._note_keys: list[tuple[str, str]] = []
            self._views = {}
            self.refresh(None)

        @rumps.timer(0.5)
        def refresh(self, _sender) -> None:
            runner.tick()
            self._apply()
            for key in menu.notifications:
                rumps.notification(
                    BooxMenu.TITLE, "", menu.last_error or menu.status_line, sound=False
                )
            menu.notifications.clear()
            for note_id, future in list(self._views.items()):
                if not future.done():
                    continue
                del self._views[note_id]
                try:
                    status, error, _pdf = future.result()
                except Exception as exc:
                    status, error = "failed", str(exc)
                if status not in {"ok", "approximate"}:
                    key = "view:" + note_id
                    if key not in menu._seen:
                        menu._seen.add(key)
                        rumps.notification(
                            BooxMenu.TITLE, "", error or status, sound=False
                        )
            self._rebuild_notebooks()

        def sync_now(self, _sender) -> None:
            if not menu.sync_enabled:
                return
            runner.sync_now()
            self._apply()

        def _apply(self) -> None:
            rows = status_menu_rows(menu)
            self._status.title = rows[0]
            self._error.title = (
                menu.last_error[:160] + ("…" if len(menu.last_error) > 160 else "")
            ) or " "
            self._sync.set_callback(self.sync_now if menu.sync_enabled else None)

        def _rebuild_notebooks(self) -> None:
            state = load_state(config.state_dir, config.archive_dir)
            entries = sorted(
                state.get("notes", {}).values(),
                key=lambda entry: str(entry.get("title")),
            )
            wanted = []
            for entry in entries:
                manifest = (
                    read_json(
                        config.archive_dir / entry["archive_folder"] / "manifest.json"
                    )
                    or {}
                )
                quality = manifest.get("render_status")
                label = notebook_label(entry)
                if quality in {"approximate", "failed", "pending"}:
                    label += f" ({quality})"
                wanted.append((str(entry.get("note_id")), label))
            if self._note_keys == wanted:
                return
            for _note_id, label in self._note_keys:
                if label in self._notes:
                    del self._notes[label]
            self._note_keys = []
            for note_id, label in wanted:
                self._notes.add(
                    rumps.MenuItem(label, callback=lambda _s, n=note_id: self.view(n))
                )
                self._note_keys.append((note_id, label))

        def view(self, note_id: str) -> None:
            if note_id not in self._views:
                self._views[note_id] = executor.submit(
                    view_notebook,
                    config.state_dir,
                    config.archive_dir,
                    note_id,
                    default_opener,
                )

        def open_archive(self, _sender) -> None:

            executor.submit(default_opener, config.archive_dir)

    try:
        App().run()
    finally:
        executor.shutdown(wait=False, cancel_futures=True)


def _apply_report(menu: BooxMenu, report) -> None:
    if report.error:
        menu.update("notebook_failed", error_key="sync", error=report.error)
        return
    if report.status == "absent":
        menu.update("absent")
        return
    if report.status == "unauthorized":
        menu.update("unauthorized", error_key="unauthorized")
        return
    if report.status == "adb_missing":
        menu.update("adb_missing", error_key="adb_missing")
        return
    if report.status == "no_notes":
        menu.update("no_notes", error_key="no_notes")
        return
    failures = [item for item in report.outcomes if item.status == "failed"]
    if report.status == "failed" and not failures:
        menu.update("notebook_failed", error_key="sync")
        return
    if failures:
        for item in failures:
            key = "failed:" + (item.note_id or item.source_path or "notebook")
            menu.update(
                "notebook_failed",
                error_key=key,
                error=f"{item.title or item.note_id}: {item.error or 'sync failed'}",
            )
        return
    approximate = [item for item in report.outcomes if item.status == "approximate"]
    if approximate:
        menu.update(
            "approximate",
            approximate_count=len(approximate),
            titles=[item.title for item in approximate if item.title],
        )
        return
    menu.update("up_to_date")
