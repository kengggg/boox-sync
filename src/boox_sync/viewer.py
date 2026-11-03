"""Open one archived notebook as a PDF, rendering it first when needed."""

import hashlib
import subprocess
from pathlib import Path

from boox_sync.archive import export_pdf
from boox_sync.render import RENDERER_VERSION
from boox_sync.state import load_state
from boox_sync.storage import archive_lock, notebook_folder, read_json


def find_entry(state: dict, needle: str) -> tuple[str, dict] | None:
    """Find one notebook by exact title, note id, or id prefix.

    Args:
        state: The sync state document.
        needle: Title (case-insensitive) or note id prefix.

    Returns:
        (key, entry) or None. Raises LookupError when several match.
    """
    hay = str(needle).strip().casefold()
    if not hay:
        return None
    exact: list[tuple[str, dict]] = []
    prefix: list[tuple[str, dict]] = []
    for key, entry in state.get("notes", {}).items():
        title = str(entry.get("title") or "").casefold()
        note_id = str(entry.get("note_id") or "")
        if title == hay or note_id.casefold() == hay:
            exact.append((key, entry))
        elif note_id.casefold().startswith(hay):
            prefix.append((key, entry))
    matches = exact or prefix
    if not matches:
        return None
    if len(matches) > 1:
        names = ", ".join(
            sorted(
                f"{entry.get('title') or key} ({entry.get('note_id')})"
                for _key, entry in matches
            )
        )
        raise LookupError(f"several notebooks match {needle!r}: {names}")
    return matches[0]


def needs_render(folder: Path) -> bool:
    """Return whether the archived PDF is missing or older than the .note."""
    manifest = read_json(folder / "manifest.json") or {}
    try:
        digest = hashlib.sha256(
            (folder / f"{folder.name}.note").read_bytes()
        ).hexdigest()
        return not (
            manifest.get("render_status") in {"ok", "approximate"}
            and int(manifest.get("renderer_version") or 0) == RENDERER_VERSION
            and (folder / f"{folder.name}.pdf").is_file()
            and manifest.get("rendered_sha256")
            == manifest.get("content_sha256")
            == digest
        )
    except (OSError, ValueError, TypeError):
        return True


def view_notebook(
    state_dir: Path,
    archive_dir: Path,
    needle: str,
    opener=None,
) -> tuple[str, str | None, Path | None]:
    """Render one notebook on demand and open its PDF.

    Args:
        state_dir: Directory holding state.json.
        archive_dir: Directory holding the notebook folders.
        needle: Notebook title or note id, resolved by find_entry.
        opener: Callable receiving the PDF path. Defaults to `open`.

    Returns:
        (render_status, error, pdf_path). The pdf path is None when the
        notebook was not found or rendering failed.
    """
    with archive_lock(archive_dir):
        found = find_entry(load_state(state_dir, archive_dir), needle)
        if found is None:
            return "missing", f"no notebook matches {needle!r}", None
        _key, entry = found
        folder = notebook_folder(Path(archive_dir), entry["archive_folder"])
        if needs_render(folder):
            result = export_pdf(Path(archive_dir), folder.name)
            if result.pdf_bytes is None:
                return result.status, result.error, None
            status = result.status
        else:
            status = (read_json(folder / "manifest.json") or {}).get(
                "render_status", "ok"
            )
        pdf = folder / f"{folder.name}.pdf"
        if opener is not None:
            opener(pdf)
        return status, None, pdf


def default_opener(pdf: Path) -> None:
    """Open the PDF in the user's viewer."""
    subprocess.run(["open", str(pdf)], check=True, timeout=10)
