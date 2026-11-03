"""Menu tests drive BooxMenu and PresenceRunner, which the status item uses."""

import inspect

from boox_sync.ksync import assemble_note, build_batch
from boox_sync.menu import BooxMenu, PresenceRunner, run_menu, status_menu_rows
from boox_sync.sync import RemoteNotebook, sync_once

ROWS = [
    ("absent", 0, None, "Not connected", False, False),
    ("syncing", 0, None, "Syncing", False, False),
    ("up_to_date", 0, None, "Up to date", False, True),
    ("approximate", 2, None, "Up to date, 2 approximate", False, True),
    (
        "unauthorized",
        0,
        "unauthorized",
        "Unlock the tablet and accept this computer",
        True,
        True,
    ),
    ("adb_missing", 0, "adb_missing", "Android platform tools not found", True, True),
    ("no_notes", 0, "no_notes", "No notebooks found on the tablet.", True, True),
    ("notebook_failed", 0, "failed:note", "Last sync failed", True, True),
]


def test_status_line_and_sync_now_for_each_spec_row():
    """Each spec situation sets the status line and Sync now enabled flag."""
    for kind, count, key, line, notifies, enabled in ROWS:
        menu = BooxMenu()
        menu.update(kind, approximate_count=count, error_key=key)
        assert menu.status_line == line
        assert menu.sync_enabled is enabled
        assert (menu.notifications == [key]) is notifies


def test_repeated_error_key_notifies_once_per_attach_session():
    """The same error key notifies once until the tablet disconnects."""
    menu = BooxMenu()
    menu.update("notebook_failed", error_key="failed:abc")
    menu.update("notebook_failed", error_key="failed:abc")
    assert menu.notifications == ["failed:abc"]
    menu.update("notebook_failed", error_key="failed:def")
    assert menu.notifications == ["failed:abc", "failed:def"]
    menu.update("absent")
    assert menu.sync_enabled is False
    menu.update("notebook_failed", error_key="failed:abc")
    assert menu.notifications == ["failed:abc", "failed:def", "failed:abc"]


def test_menu_entrypoint_uses_the_boox_title_and_items():
    """The menu process title, items, notebook list, and view action are wired."""
    assert BooxMenu.TITLE == "BOOX"
    assert BooxMenu.ITEMS == ("Sync now", "Open archive", "Quit")
    source = inspect.getsource(run_menu)
    assert "BooxMenu.TITLE" in source
    assert "BooxMenu.ITEMS" in source
    assert "BackgroundRunner" in source
    assert "Notebooks" in source
    assert "view_notebook" in source
    assert "status_menu_rows" in source


def test_notebook_rows_list_titles_between_status_and_items():
    """A menu with notebooks lists their titles above the fixed items."""
    menu = BooxMenu()
    menu.update("up_to_date")
    menu.approximate_titles = ["Sample Project", "Sample Budget"]
    assert status_menu_rows(menu) == [
        "Up to date",
        "Sample Project",
        "Sample Budget",
        "Sync now",
        "Open archive",
        "Quit",
    ]


def test_notebook_label_uses_the_folder_when_titles_collide():
    """Duplicate titles stay distinct in the menu by using the folder name."""
    from boox_sync.menu import notebook_label

    assert (
        notebook_label({"title": "Notepad1", "archive_folder": "Notepad1 025a27ca"})
        == "Notepad1 025a27ca"
    )
    assert (
        notebook_label({"title": "Sample Project", "archive_folder": "Sample Project"})
        == "Sample Project"
    )


def test_sync_report_marks_approximate_renders(tmp_path):
    """A rendered charcoal notebook is listed as approximate and has a PDF."""
    from boox_sync.config import Config
    from boox_sync.menu import _apply_report

    files = build_batch(
        "22222222222242228222222222222222",
        "11111111-1111-1111-1111-111111111111",
        [{"pen_type": 22, "points": [(10, 20, 1000), (40, 60, 2000)], "thickness": 3}],
        1793600000000,
    )
    raw = assemble_note("11111111111141118111111111111111", "Charcoal notebook", files)

    class Source:
        def list_notebooks(self):
            return [
                RemoteNotebook(
                    "11111111111141118111111111111111", "Charcoal notebook", "s1"
                )
            ]

        def pull_notebook(self, notebook):
            return raw, None

    config = Config(
        device_serial="TEST1234",
        adb_path="/opt/homebrew/bin/adb",
        archive_dir=tmp_path / "BOOX",
        state_dir=tmp_path / "state",
        remote_roots=["/storage/emulated/0/note"],
        poll_seconds=30,
    )
    report = sync_once(config, Source())
    menu = BooxMenu()
    _apply_report(menu, report)
    assert menu.status_line == "Up to date, 1 approximate"
    assert menu.last_error == ""
    assert (tmp_path / "BOOX" / "Charcoal notebook" / "Charcoal notebook.pdf").is_file()


def test_presence_runner_syncs_once_on_attach_and_stops_on_detach(tmp_path):
    """Absent ticks do not pull. A tick during sync is skipped. Detach stops."""
    files = build_batch(
        "22222222222242228222222222222222",
        "11111111-1111-1111-1111-111111111111",
        [{"pen_type": 2, "points": [(10, 10, 50), (40, 40, 50)], "thickness": 2}],
        1793600000000,
    )
    raw = assemble_note("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa", "Journal", files)
    pulls: list[str] = []
    lists = {"count": 0}
    attached = {"value": False}

    class Source:
        def list_notebooks(self):
            lists["count"] += 1
            return [
                RemoteNotebook("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa", "Journal", "s1")
            ]

        def pull_notebook(self, notebook):
            runner.tick()
            pulls.append(notebook.note_id)
            return raw, None

    from boox_sync.config import Config

    config = Config(
        device_serial="TEST1234",
        adb_path="/opt/homebrew/bin/adb",
        archive_dir=tmp_path / "BOOX",
        state_dir=tmp_path / "state",
        remote_roots=["/storage/emulated/0/note"],
        poll_seconds=30,
    )
    source = Source()
    menu = BooxMenu()
    runner = PresenceRunner(
        config,
        menu,
        present=lambda: attached["value"],
        sync=lambda cfg: sync_once(cfg, source),
    )
    runner.tick()
    assert lists["count"] == 0
    assert pulls == []
    assert menu.status_line == "Not connected"
    assert menu.sync_enabled is False
    attached["value"] = True
    runner.tick()
    assert lists["count"] == 1
    assert pulls == ["aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"]
    assert menu.status_line == "Up to date, 1 approximate"
    attached["value"] = False
    runner.tick()
    runner.tick()
    assert lists["count"] == 1
    assert menu.status_line == "Not connected"
    assert menu.sync_enabled is False
