"""Adb source tests check the commands the tablet actually receives."""

from boox_sync.adb import AdbUsbSource, list_command, pull_command, stat_command


def posix_words(command: str) -> list[str]:
    """Split a device-shell command the way a POSIX shell splits quotes."""
    words: list[str] = []
    buf: list[str] = []
    quoted = False
    index = 0
    while index < len(command):
        char = command[index]
        if quoted:
            if char == "'":
                quoted = False
            else:
                buf.append(char)
            index += 1
            continue
        if char == "'":
            quoted = True
            index += 1
            continue
        if char == "\\":
            index += 1
            if index < len(command):
                buf.append(command[index])
                index += 1
            continue
        if char.isspace():
            if buf:
                words.append("".join(buf))
                buf = []
            index += 1
            continue
        buf.append(char)
        index += 1
    if buf:
        words.append("".join(buf))
    return words


def joined_shell(argv: list[str]) -> str:
    """Join arguments after shell the way adb sends them to the device."""
    return " ".join(argv[argv.index("shell") + 1 :])


class Completed:
    def __init__(self, stdout="", returncode=0, stderr=""):
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode


def test_adb_commands_only_list_stat_and_pull():
    """The tablet is only searched and copied. Nothing is installed or deleted."""
    commands = [
        list_command("adb", "TEST1234", "/storage/emulated/0/note"),
        stat_command("adb", "TEST1234", "/storage/emulated/0/note/a.note"),
        pull_command(
            "adb", "TEST1234", "/storage/emulated/0/note/a.note", "local.note"
        ),
    ]
    for command in commands:
        assert "install" not in command
        assert "push" not in command
        assert "rm" not in command
        assert "uninstall" not in command
    assert posix_words(joined_shell(commands[0])) == [
        "toybox",
        "find",
        "/storage/emulated/0/note",
        "-type",
        "f",
        "-name",
        "*.note",
    ]
    stat_words = posix_words(joined_shell(commands[1]))
    assert stat_words == [
        "toybox",
        "stat",
        "-c",
        "%s %Y %n",
        "/storage/emulated/0/note/a.note",
    ]
    spaced = "/storage/emulated/0/note/My Journal.note"
    quoted = posix_words(joined_shell(stat_command("adb", "TEST1234", spaced)))
    assert quoted == ["toybox", "stat", "-c", "%s %Y %n", spaced]
    named = "/storage/emulated/0/note/it's mine.note"
    assert (
        posix_words(joined_shell(stat_command("adb", "TEST1234", named)))[-1] == named
    )
    assert commands[2][1:4] == ["-s", "TEST1234", "pull"]


def test_adb_source_lists_and_pulls_with_those_commands(tmp_path):
    """AdbUsbSource issues find, stat, and pull, then returns the file bytes."""
    calls = []
    local_holder = {}
    remote = "/storage/emulated/0/note/My Journal.note"

    def runner(argv, **kwargs):
        calls.append(list(argv))
        if "shell" in argv:
            words = posix_words(joined_shell(argv))
            if words[:2] == ["toybox", "find"]:
                if "/missing" in words:
                    return Completed(stdout="", returncode=1)
                return Completed(stdout=remote + "\n")
            if words[:3] == ["toybox", "stat", "-c"] and words[3] == "%s %Y %n":
                return Completed(stdout=f"4 9 {words[4]}\n")
            raise AssertionError(words)
        if "pull" in argv:
            local_holder["path"] = argv[-1]
            open(argv[-1], "wb").write(b"note")
            return Completed(stdout="")
        raise AssertionError(argv)

    source = AdbUsbSource(
        "adb", "TEST1234", ["/storage/emulated/0/note", "/missing"], runner=runner
    )
    listed = source.list_notes()
    assert listed == [(remote, 4, 9)]
    assert source.pull(remote) == b"note"
    kinds = []
    for argv in calls:
        words = posix_words(joined_shell(argv)) if "shell" in argv else argv
        if "find" in words:
            kinds.append("find")
        elif "stat" in words:
            kinds.append("stat")
        elif "pull" in words:
            kinds.append("pull")
        else:
            kinds.append("other")
    assert "other" not in kinds
    assert kinds.count("pull") == 1
    assert not __import__("os").path.exists(local_holder["path"])


class _Proc:
    def __init__(self, stdout="", returncode=0):
        self.stdout = stdout
        self.stderr = ""
        self.returncode = returncode


def test_ksync_listing_command_quotes_and_covers_all_roots():
    """One find call prints size, mtime, and path for every staging root."""
    from boox_sync.adb import listing_command

    command = listing_command(
        "adb",
        "TEST1234",
        ["/storage/emulated/0/.ksync/couch", "/storage/emulated/0/.ksync/document"],
    )
    script = command[-1]
    assert command[:4] == ["adb", "-s", "TEST1234", "shell"]
    assert "'/storage/emulated/0/.ksync/couch'" in script
    assert "-type f -printf" in script
    assert "'%s %T@ %p\\0'" in script


def test_parse_listing_and_signature():
    """Listing rows parse, and identical rows produce identical signatures."""
    from boox_sync.adb import parse_listing, signature

    text = "830 1793599647.514 /a/b.txt\n45 1.0 /a/c\n999 2 /d\n"
    rows = parse_listing(text)
    assert rows == [
        ("/a/b.txt", 830, "1793599647.514"),
        ("/a/c", 45, "1.0"),
        ("/d", 999, "2"),
    ]
    assert signature(rows) == signature(list(reversed(rows)))
    assert signature(rows) != signature(rows + [("/e", 1, "1")])


def test_ksync_source_lists_and_skips_unchanged(tmp_path, monkeypatch):
    """list_notebooks reads the cached tree when the signature is stable."""
    import sqlite3

    from boox_sync.adb import AdbKsyncSource
    from fleece_fixture import fleece, notebook

    def seed_tree(_cache):
        tree = tmp_path / "couch"
        db_dir = tree / "user-NOTE_TREE.cblite2"
        db_dir.mkdir(parents=True, exist_ok=True)
        db_path = db_dir / "db.sqlite3"
        db = sqlite3.connect(db_path)
        db.execute(
            "CREATE TABLE kv_default (key TEXT PRIMARY KEY, sequence INTEGER,"
            " flags INTEGER, version BLOB, body BLOB, extra BLOB, expiration INTEGER)"
        )
        db.execute(
            "INSERT INTO kv_default (key, body) VALUES (?, ?)",
            (
                "11111111111141118111111111111111",
                fleece(notebook("Sample Project")),
            ),
        )
        db.commit()
        db.close()
        return tmp_path / "couch"

    tree = seed_tree(tmp_path / "cache")
    db_size = (tree / "user-NOTE_TREE.cblite2" / "db.sqlite3").stat().st_size
    listing = "\n".join(
        [
            f"{db_size} 1793600000.0 /storage/emulated/0/.ksync/couch/user-NOTE_TREE.cblite2/db.sqlite3",
            "10 1793600001.0 /storage/emulated/0/.ksync/document/11111111111141118111111111111111/shape/page#batch#1.zip",
            "20 1793600002.0 /storage/emulated/0/.ksync/point/11111111111141118111111111111111/page/x#points",
            "30 1793600003.0 /storage/emulated/0/.noteCache/thumbnail/11111111111141118111111111111111.png",
        ]
    )
    pulls: list[str] = []
    runner_calls = {"count": 0}

    def runner(argv, *args, **kwargs):
        runner_calls["count"] += 1
        if "find" in argv[-1]:
            return _Proc(listing + "\n")
        if "pull" in argv:
            pulls.append(argv[-2])
            return _Proc("", 0)
        raise AssertionError(argv)

    source = AdbKsyncSource("adb", "TEST1234", tmp_path / "cache", runner=runner)
    monkeypatch.setattr(source, "_pull_dir_tree", lambda: tree)

    from boox_sync.adb import parse_listing, signature
    from boox_sync.storage import write_json

    write_json(
        source.cache_dir / "note_tree/meta.json",
        {
            "signature": signature(
                [row for row in parse_listing(listing) if "NOTE_TREE" in row[0]]
            ),
            "notebooks": [["stale", "Old heuristic title"]],
        },
    )
    notebooks = source.list_notebooks()
    assert notebooks[0].active is True
    assert [(nb.note_id, nb.title) for nb in notebooks] == [
        ("11111111111141118111111111111111", "Sample Project")
    ]
    assert all(nb.signature == notebooks[0].signature for nb in notebooks)

    again = source.list_notebooks()
    assert again[0].signature == notebooks[0].signature
    assert (
        runner_calls["count"] == 2
    )  # Initial inventory and post-copy consistency check.


def test_pull_notebook_maps_dirs_to_content_keys(tmp_path, monkeypatch):
    """pull_notebook converts pulled trees to assemble keys and a note zip."""
    from pathlib import Path

    from boox_sync.adb import AdbKsyncSource
    from boox_sync.ksync import build_batch, build_virtual_page
    from boox_sync.note import parse_notes
    from boox_sync.sync import RemoteNotebook

    page = "22222222222242228222222222222222"
    files = build_batch(
        page,
        "11111111-1111-1111-1111-111111111111",
        [{"pen_type": 2, "points": [(10, 20, 100), (60, 70, 100)], "thickness": 2}],
        1793600000000,
    )
    files[f"page/{page}"] = build_virtual_page(
        page,
        0,
        1793599000000,
        {"left": 0.0, "top": 0.0, "right": 1860.0, "bottom": 2480.0},
    )

    def write_tree(base: Path):
        for key, data in files.items():
            target = base / key
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)

    def fake_pull_dir(remote_dir):
        if remote_dir.endswith("/document/11111111111141118111111111111111"):
            base = tmp_path / "fake-doc"
            base.mkdir()
            doc = base / "11111111111141118111111111111111"
            (doc / "virtual/page/pb").mkdir(parents=True)
            (doc / "shape").mkdir(parents=True)
            (doc / "resource/data").mkdir(parents=True)
            (doc / "template/json").mkdir(parents=True)
            (doc / "virtual/page/pb/x").write_bytes(files[f"page/{page}"])
            (
                doc
                / f"shape/{page}#11111111-1111-1111-1111-111111111111#1793600000000.zip"
            ).write_bytes(
                files[
                    f"shape/{page}#11111111-1111-1111-1111-111111111111#1793600000000.zip"
                ]
            )
            (doc / "resource/data/pic.png").write_bytes(b"\x89PNG\r\n\x1a\n")
            (doc / "template/json" / f"{page}.template_json").write_bytes(b"{}")
            return doc
        page_dir = tmp_path / "fake-point" / page
        page_dir.mkdir(parents=True, exist_ok=True)
        (page_dir / f"{page}#11111111-1111-1111-1111-111111111111#points").write_bytes(
            files[f"point/{page}/{page}#11111111-1111-1111-1111-111111111111#points"]
        )
        return tmp_path / "fake-point"

    from boox_sync.adb import KSYNC_DOCUMENT, KSYNC_POINT, THUMBNAILS, signature

    nid = "11111111111141118111111111111111"
    doc = f"{KSYNC_DOCUMENT}/{nid}"
    pt = f"{KSYNC_POINT}/{nid}"
    rows = [
        (doc + "/virtual/page/pb/x", len(files[f"page/{page}"]), "1"),
        (
            doc
            + f"/shape/{page}#11111111-1111-1111-1111-111111111111#1793600000000.zip",
            len(
                files[
                    f"shape/{page}#11111111-1111-1111-1111-111111111111#1793600000000.zip"
                ]
            ),
            "1",
        ),
        (doc + "/resource/data/pic.png", 8, "1"),
        (doc + f"/template/json/{page}.template_json", 2, "1"),
        (
            pt + f"/{page}/{page}#11111111-1111-1111-1111-111111111111#points",
            len(
                files[
                    f"point/{page}/{page}#11111111-1111-1111-1111-111111111111#points"
                ]
            ),
            "1",
        ),
        (f"{THUMBNAILS}/{nid}.png", 9, "1"),
    ]
    source = AdbKsyncSource(
        "adb",
        "TEST1234",
        tmp_path / "cache",
        runner=lambda *a, **k: (_ for _ in ()).throw(
            AssertionError("unexpected subprocess")
        ),
    )
    source._listing = rows
    monkeypatch.setattr(source, "_query_rows", lambda roots: rows)
    monkeypatch.setattr(source, "_pull_dir", fake_pull_dir)
    monkeypatch.setattr(source, "_pull_file", lambda remote: b"png-bytes")

    notebook = RemoteNotebook(
        "11111111111141118111111111111111", "Sample Project", signature(rows)
    )
    raw, cover = source.pull_notebook(notebook)
    assert cover == b"png-bytes"
    note = parse_notes(raw)[0]
    assert note.title == "Sample Project"
    assert note.width == 1860.0
    assert len(note.pages[0].strokes) == 1
    assert note.images == [b"\x89PNG\r\n\x1a\n"]
    assert (
        f"{note.note_id}/template/json/{page}.template_json"
        in __import__("zipfile").ZipFile(__import__("io").BytesIO(raw)).namelist()
    )
