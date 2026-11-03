"""Durable writes and a shared lock for archive readers and writers."""

import fcntl
import json
import os
import tempfile
import threading
from contextlib import contextmanager
from pathlib import Path


class ArchiveBusy(RuntimeError):
    """Another process or worker owns the archive."""


_threads = threading.local()


@contextmanager
def archive_lock(root: Path):
    """Serialize archive operations across processes; allow nested same-thread use."""
    root = Path(root).resolve()
    held = getattr(_threads, "held", None)
    if held is None:
        held = _threads.held = set()
    if root in held:
        yield
        return
    root.mkdir(parents=True, exist_ok=True)
    with (root / ".boox-sync.lock").open("a+b") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise ArchiveBusy(
                "BOOX archive is busy; try again when the current operation finishes"
            ) from exc
        held.add(root)
        try:
            yield
        finally:
            held.remove(root)
            fcntl.flock(handle, fcntl.LOCK_UN)


def atomic_write(path: Path, data: bytes) -> None:
    """Commit a flushed sibling temporary file with an atomic rename."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".partial", dir=path.parent
    )
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        Path(temporary).unlink(missing_ok=True)


def read_json(path: Path) -> dict | None:
    """Read a JSON object. Damaged or missing metadata is recoverable."""
    try:
        result = json.loads(path.read_text())
        return result if isinstance(result, dict) else None
    except (OSError, ValueError):
        return None


def write_json(path: Path, value: dict) -> None:
    atomic_write(
        path, (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode()
    )


def notebook_folder(root: Path, name: str) -> Path:
    """Resolve one direct archive child without following an external symlink."""
    if not name or name in {".", ".."} or Path(name).name != name or "\\" in name:
        raise ValueError("invalid archive folder")
    path = root / name
    if path.is_symlink() or path.resolve().parent != root.resolve():
        raise ValueError("archive folder must be a direct directory, not a symlink")
    return path
