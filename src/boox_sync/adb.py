"""Read-only adb listing and pull of exported .note files."""

import hashlib
import os
import shutil
import tempfile
import re
import subprocess
from pathlib import Path

from boox_sync.ksync import NotebookMeta, assemble_note, read_note_tree
from boox_sync.sync import RemoteNotebook
from boox_sync.storage import read_json, write_json

KSYNC_COUCH = "/storage/emulated/0/.ksync/couch"
KSYNC_DOCUMENT = "/storage/emulated/0/.ksync/document"
KSYNC_POINT = "/storage/emulated/0/.ksync/point"
THUMBNAILS = "/storage/emulated/0/.noteCache/thumbnail"
_TREE_MARK = "-NOTE_TREE.cblite2"


class AdbError(RuntimeError):
    """adb could not list or pull a file."""


def list_command(adb: str, serial: str, root: str) -> list[str]:
    """Return the argv that lists .note files under one root."""
    return [
        adb,
        "-s",
        serial,
        "shell",
        "toybox find "
        + _shell_quote(root)
        + " -type f -name "
        + _shell_quote("*.note"),
    ]


def _shell_quote(value: str) -> str:
    """Quote one string for the device shell."""
    return "'" + value.replace("'", "'\\''") + "'"


def stat_command(adb: str, serial: str, path: str) -> list[str]:
    """Return the argv that prints size, epoch mtime, and path.

    adb joins every argument after ``shell`` with spaces and does not add
    quotes. The format and the path travel as one already-quoted command so
    the device shell keeps ``%s %Y %n`` together and keeps spaces inside the path.
    """
    script = "toybox stat -c " + _shell_quote("%s %Y %n") + " " + _shell_quote(path)
    return [adb, "-s", serial, "shell", script]


def pull_command(adb: str, serial: str, remote: str, local: str) -> list[str]:
    """Return the argv that copies one remote file to a local path."""
    return [adb, "-s", serial, "pull", remote, local]


class AdbUsbSource:
    """List and pull manual .note exports. The tablet is never modified."""

    def __init__(
        self,
        adb_path: str,
        serial: str,
        roots: list[str],
        runner=None,
        timeout: int = 120,
    ):
        """Store the adb binary, serial, and roots.

        Args:
            adb_path: Path to the adb binary.
            serial: Device serial passed to adb -s.
            roots: Absolute on-device directories to search.
            runner: Callable with the subprocess.run signature. Defaults to
                subprocess.run.
        """
        import subprocess

        self.adb_path = adb_path
        self.serial = serial
        self.roots = list(roots)
        self._run = _bounded_runner(runner or subprocess.run, timeout)

    def list_notes(self) -> list[tuple[str, int, int]]:
        """Return path, size, and mtime for each .note file under the roots."""
        found: list[tuple[str, int, int]] = []
        for root in self.roots:
            proc = self._run(
                list_command(self.adb_path, self.serial, root),
                capture_output=True,
                text=True,
            )
            if proc.returncode != 0:
                continue
            for line in proc.stdout.splitlines():
                remote = line.strip()
                if not remote:
                    continue
                stat = self._run(
                    stat_command(self.adb_path, self.serial, remote),
                    capture_output=True,
                    text=True,
                )
                if stat.returncode != 0:
                    raise AdbError(stat.stderr.strip() or f"stat failed for {remote}")
                size_text, mtime_text, path = stat.stdout.strip().split(maxsplit=2)
                found.append((path, int(size_text), int(mtime_text)))
        return found

    def pull(self, path: str) -> bytes:
        """Copy one remote file into memory and delete the local temp file."""
        handle = tempfile.NamedTemporaryFile(
            prefix="boox-pull-", suffix=".note", delete=False
        )
        local = handle.name
        handle.close()
        try:
            proc = self._run(
                pull_command(self.adb_path, self.serial, path, local),
                capture_output=True,
                text=True,
            )
            if proc.returncode != 0:
                raise AdbError(proc.stderr.strip() or "adb pull failed")
            return Path(local).read_bytes()
        finally:
            os.unlink(local)


def _bounded_runner(runner, timeout):
    def run(argv, **kwargs):
        try:
            return runner(argv, timeout=timeout, **kwargs)
        except subprocess.TimeoutExpired as exc:
            raise AdbError(f"ADB timed out after {timeout}s") from exc
        except OSError as exc:
            raise AdbError(f"Cannot run ADB: {exc}") from exc

    return run


def listing_command(adb: str, serial: str, roots: list[str]) -> list[str]:
    """NUL-terminate records so spaces and newlines in filenames are unambiguous."""
    script = "toybox find " + " ".join(_shell_quote(root) for root in roots)
    script += " -type f -printf " + _shell_quote("%s %T@ %p\\0")
    return [adb, "-s", serial, "shell", script]


def parse_listing(text: str) -> list[tuple[str, int, str]]:
    """Read complete records and refuse malformed data instead of skipping it."""
    if not text:
        return []
    if "\0" in text:
        if not text.endswith("\0"):
            raise AdbError("truncated file listing")
        records = text[:-1].split("\0")
    else:
        records = text.splitlines()
    rows = []
    seen = set()
    for record in records:
        match = re.fullmatch(r"(\d+) (-?\d+(?:\.\d+)?) (/.*)", record, re.DOTALL)
        if not match:
            raise AdbError("malformed file listing; archive was not changed")
        size, mtime, path = match.groups()
        if re.search(r"\d+ -?\d+(?:\.\d+)? /", path) or path in seen:
            raise AdbError("ambiguous or duplicate file listing")
        seen.add(path)
        rows.append((path, int(size), mtime))
    return rows


SIGNATURE_VERSION = "3"


def signature(rows: list[tuple[str, int, str]]) -> str:
    """Hash one notebook's file rows into a change signature.

    The version prefix changes when the pull starts keeping files an older
    pass dropped, so the next sync reads those notebooks again.
    """
    blob = SIGNATURE_VERSION + "\n"
    blob += "".join(f"{path} {size} {mtime}\n" for path, size, mtime in sorted(rows))
    return hashlib.sha256(blob.encode()).hexdigest()


class AdbKsyncSource:
    """List and pull live notebooks from the ksync staging area.

    Listing costs one adb call. The NOTE_TREE database is pulled only when
    its files change and is cached under cache_dir between runs.
    """

    def __init__(
        self,
        adb_path: str,
        serial: str,
        cache_dir: Path,
        runner=None,
        timeout: int = 120,
    ):
        """Store the adb binary, serial, and cache location.

        Args:
            adb_path: Path to the adb binary.
            serial: Device serial passed to adb -s.
            cache_dir: Writable directory for the NOTE_TREE cache.
            runner: Callable with the subprocess.run signature.
        """
        import subprocess

        self.adb_path = adb_path
        self.serial = serial
        self.cache_dir = Path(cache_dir)
        self._run = _bounded_runner(runner or subprocess.run, timeout)
        self._listing: list[tuple[str, int, str]] | None = None

    def list_notebooks(self) -> list[RemoteNotebook]:
        """Return every Notes-app notebook with its change signature."""
        rows = self._all_rows()
        tree_rows = [row for row in rows if _TREE_MARK in row[0]]
        tree_sig = signature(tree_rows)
        metas = self._note_tree(tree_sig)
        by_note: dict[str, list[tuple[str, int, str]]] = {}
        for path, size, mtime in rows:
            note_id = _owner_of(path)
            if note_id:
                by_note.setdefault(note_id, []).append((path, size, mtime))
        return [
            RemoteNotebook(
                note_id=meta.note_id,
                title=meta.title,
                signature=signature(by_note.get(meta.note_id, [])),
                active=meta.active,
            )
            for meta in metas
        ]

    def pull_notebook(self, notebook: RemoteNotebook) -> tuple[bytes, bytes | None]:
        """Copy and verify a stable inventory before assembling a notebook."""
        if not re.fullmatch(r"[A-Za-z0-9-]+", notebook.note_id):
            raise AdbError("invalid notebook id")
        expected = [r for r in self._all_rows() if _owner_of(r[0]) == notebook.note_id]
        if signature(expected) != notebook.signature:
            raise AdbError("notebook inventory changed; retry on the next sync")
        doc = f"{KSYNC_DOCUMENT}/{notebook.note_id}"
        if not any(p.startswith(doc + "/") for p, _, _ in expected):
            raise AdbError(
                "notebook has no staged document files; keeping the previous archive"
            )
        files: dict[str, bytes] = {}
        actual: dict[str, int] = {}
        roots = []
        for remote_dir, kind in (
            (doc, "document"),
            (f"{KSYNC_POINT}/{notebook.note_id}", "point"),
        ):
            if not any(p.startswith(remote_dir + "/") for p, _, _ in expected):
                continue
            roots.append(remote_dir)
            local = self._pull_dir(remote_dir)
            if local is None:
                raise AdbError(f"failed to pull {kind} files")
            try:
                for path in sorted(local.rglob("*")):
                    if not path.is_file():
                        continue
                    rel = path.relative_to(local).as_posix()
                    payload = path.read_bytes()
                    actual[remote_dir + "/" + rel] = len(payload)
                    # Preserve every source file, including stash and unknown metadata.
                    files[f"source/{kind}/{rel}"] = payload
                    if "stash" in rel.split("/"):
                        continue
                    key = f"point/{rel}" if kind == "point" else _content_key(rel)
                    if key is not None:
                        files[key] = payload
            finally:
                shutil.rmtree(
                    local.parent
                    if local.parent.name.startswith("boox-pull-")
                    else local,
                    ignore_errors=True,
                )
        cover_path = f"{THUMBNAILS}/{notebook.note_id}.png"
        cover = None
        if any(p == cover_path for p, _, _ in expected):
            roots.append(cover_path)
            cover = self._pull_file(cover_path)
            actual[cover_path] = len(cover)
        if actual != {p: size for p, size, _ in expected}:
            raise AdbError("incomplete notebook transfer; keeping the previous archive")
        if signature(self._query_rows(roots)) != notebook.signature:
            raise AdbError("notebook changed during transfer; retry on the next sync")
        return assemble_note(notebook.note_id, notebook.title, files), cover

    def _query_rows(self, roots):
        proc = self._run(
            listing_command(self.adb_path, self.serial, roots),
            capture_output=True,
            text=True,
        )
        if proc.returncode != 0:
            raise AdbError(proc.stderr.strip() or "ADB listing failed")
        return parse_listing(proc.stdout)

    def _all_rows(self) -> list[tuple[str, int, str]]:
        if self._listing is None:
            self._listing = self._query_rows(
                [KSYNC_COUCH, KSYNC_DOCUMENT, KSYNC_POINT, THUMBNAILS]
            )
        return self._listing

    def _note_tree(self, tree_sig: str) -> list[NotebookMeta]:
        cache = self.cache_dir / "note_tree" / "meta.json"
        cached = read_json(cache)
        if (
            cached
            and cached.get("version") == 2
            and cached.get("signature") == tree_sig
        ):
            try:
                rows = cached["notebooks"]
                if not isinstance(rows, list) or not all(
                    isinstance(row, list)
                    and len(row) == 3
                    and isinstance(row[0], str)
                    and isinstance(row[1], str)
                    and type(row[2]) is bool
                    for row in rows
                ):
                    raise ValueError("Invalid cached notebook metadata")
                return [
                    NotebookMeta(note_id=row[0], title=row[1], active=row[2])
                    for row in rows
                ]
            except (KeyError, TypeError, IndexError, ValueError):
                pass
        local = self._pull_dir_tree()
        if local is None:
            raise AdbError("notebook database transfer failed; previous cache retained")
        try:
            databases = [
                p
                for p in sorted(local.rglob("db.sqlite3"))
                if _TREE_MARK in p.parent.name
            ]
            if not databases:
                raise AdbError("no NOTE_TREE database found")
            copied = {}
            for path in local.rglob("*"):
                if not path.is_file():
                    continue
                parts = path.relative_to(local).parts
                for index, part in enumerate(parts):
                    if _TREE_MARK in part:
                        remote = KSYNC_COUCH + "/" + "/".join(parts[index:])
                        copied[remote] = path.stat().st_size
                        break
            after = [r for r in self._query_rows([KSYNC_COUCH]) if _TREE_MARK in r[0]]
            if signature(after) != tree_sig:
                raise AdbError(
                    "notebook database changed during transfer; retry on the next sync"
                )
            if copied != {path: size for path, size, _ in after}:
                raise AdbError(
                    "incomplete notebook database transfer; previous cache retained"
                )
            metas = [meta for db in databases for meta in read_note_tree(db)]
        finally:
            shutil.rmtree(local, ignore_errors=True)
        seen = {}
        for meta in metas:
            if meta.note_id in seen and seen[meta.note_id] != meta:
                raise AdbError(
                    "Conflicting notebook metadata; previous inventory retained"
                )
            seen[meta.note_id] = meta
        metas = list(seen.values())
        write_json(
            cache,
            {
                "version": 2,
                "signature": tree_sig,
                "notebooks": [[m.note_id, m.title, m.active] for m in metas],
            },
        )
        return metas

    def _pull_dir_tree(self) -> Path:
        local = Path(tempfile.mkdtemp(prefix="boox-tree-"))
        try:
            proc = self._run(
                [self.adb_path, "-s", self.serial, "pull", KSYNC_COUCH, str(local)],
                capture_output=True,
                text=True,
            )
            if proc.returncode != 0:
                raise AdbError(proc.stderr.strip() or "notebook database pull failed")
            return local
        except Exception:
            shutil.rmtree(local, ignore_errors=True)
            raise

    def _pull_dir(self, remote_dir: str) -> Path:
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        parent = Path(tempfile.mkdtemp(prefix="boox-pull-", dir=self.cache_dir))
        try:
            proc = self._run(
                [self.adb_path, "-s", self.serial, "pull", remote_dir, str(parent)],
                capture_output=True,
                text=True,
            )
            target = parent / Path(remote_dir).name
            if proc.returncode != 0 or not target.is_dir():
                raise AdbError(
                    proc.stderr.strip() or f"directory pull failed: {remote_dir}"
                )
            return target
        except Exception:
            shutil.rmtree(parent, ignore_errors=True)
            raise

    def _pull_file(self, remote: str) -> bytes:
        with tempfile.TemporaryDirectory(prefix="boox-cover-") as directory:
            path = Path(directory) / "cover.png"
            proc = self._run(
                [self.adb_path, "-s", self.serial, "pull", remote, str(path)],
                capture_output=True,
                text=True,
            )
            if proc.returncode != 0 or not path.is_file():
                raise AdbError(proc.stderr.strip() or "cover transfer failed")
            return path.read_bytes()


def _owner_of(path: str) -> str | None:
    """Return the notebook id that owns one staged file, if any."""
    for root in (KSYNC_DOCUMENT, KSYNC_POINT, THUMBNAILS):
        prefix = root + "/"
        if path.startswith(prefix):
            rest = path[len(prefix) :]
            if root == THUMBNAILS:
                name, dot, _suffix = rest.partition(".")
                if dot and name:
                    return name
                return None
            return rest.split("/", 1)[0]
    return None


def _content_key(rel: str) -> str | None:
    """Map one pulled document-tree path to its assemble_note key."""
    parts = rel.split("/")
    if (
        parts[0] == "virtual"
        and len(parts) == 4
        and parts[2] == "pb"
        and parts[1] in {"page", "doc"}
    ):
        return parts[1] + "/" + parts[3]
    if parts[0] == "pageModel" and len(parts) == 3 and parts[1] == "pb":
        return "pagemodel/" + parts[2]
    if parts[0] in {"shape", "template"} and len(parts) >= 2:
        return "/".join(parts)
    if parts[0] == "resource" and len(parts) >= 3 and parts[1] in {"pb", "data"}:
        return "/".join(parts)
    return None
