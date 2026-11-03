"""One serialized sync pass, with failures isolated per notebook."""

import hashlib
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path

from boox_sync.archive import archive_notebook, export_pdf
from boox_sync.config import Config, validate_config
from boox_sync.inventory import record_inventory
from boox_sync.render import RENDERER_VERSION
from boox_sync.state import load_state, save_state
from boox_sync.storage import archive_lock, notebook_folder, read_json, write_json


@dataclass
class Outcome:
    note_id: str | None
    status: str
    error: str | None = None
    source_path: str | None = None
    title: str | None = None
    skipped: bool = False
    reasons: list[str] = field(default_factory=list)


@dataclass
class SyncReport:
    status: str
    outcomes: list[Outcome] = field(default_factory=list)
    error: str | None = None


@dataclass
class RemoteNotebook:
    note_id: str
    title: str
    signature: str
    active: bool = True


class NotebookSource:
    def list_notebooks(self) -> list[RemoteNotebook]:
        raise NotImplementedError

    def pull_notebook(self, notebook: RemoteNotebook) -> tuple[bytes, bytes | None]:
        raise NotImplementedError


def sync_once(
    config: Config, source: NotebookSource, clock=None, progress=None
) -> SyncReport:
    """Commit only complete reads, keep recovery history and render automatically."""
    validate_config(config)
    try:
        with archive_lock(config.archive_dir), archive_lock(config.state_dir):
            notebooks = list(source.list_notebooks())
            record_inventory(config, notebooks)
            if progress:
                progress({"phase": "listed", "total": len(notebooks), "completed": 0})
            if not notebooks:
                report = SyncReport(status="no_notes")
                _record_report(config, report)
                return report
            state = load_state(config.state_dir, config.archive_dir)
            outcomes = []
            for index, notebook in enumerate(notebooks):
                if progress:
                    progress(
                        {
                            "phase": "notebook",
                            "title": notebook.title,
                            "total": len(notebooks),
                            "completed": index,
                        }
                    )
                try:
                    if notebook.active:
                        outcomes.extend(
                            _sync_notebook(config, source, notebook, state, clock)
                        )
                    else:
                        outcomes.append(
                            Outcome(
                                notebook.note_id,
                                "inactive",
                                title=notebook.title,
                                skipped=True,
                            )
                        )
                except Exception as exc:
                    outcomes.append(
                        Outcome(
                            notebook.note_id,
                            "failed",
                            str(exc),
                            notebook.note_id,
                            notebook.title,
                        )
                    )
                if progress:
                    progress(
                        {
                            "phase": "completed",
                            "total": len(notebooks),
                            "completed": index + 1,
                        }
                    )
            report = SyncReport(
                "failed" if any(o.status == "failed" for o in outcomes) else "ok",
                outcomes,
            )
            _record_report(config, report)
            return report
    except Exception as exc:
        report = SyncReport("failed", error=str(exc))
        _record_report(config, report)
        return report


def _record_report(config, report):
    try:
        write_json(
            config.state_dir / "last-report.json",
            {
                **asdict(report),
                "finished_at": datetime.now().astimezone().isoformat(),
            },
        )
    except OSError:
        pass  # The operation error remains visible in the returned report.


def _sync_notebook(config, source, notebook, state, clock):
    key = f"{config.device_serial}:{notebook.note_id}"
    entry = state.get("notes", {}).get(key)
    unchanged = (
        entry
        and entry.get("signature") == notebook.signature
        and entry.get("title") == notebook.title
        and _archived_note_ok(config.archive_dir, entry)
    )
    if unchanged and (
        not config.render_after_sync or _pdf_current(config.archive_dir, entry)
    ):
        folder = notebook_folder(config.archive_dir, entry["archive_folder"])
        manifest = read_json(folder / "manifest.json") or {}
        status = (
            "approximate"
            if manifest.get("render_status") == "approximate"
            else "skipped"
        )
        save_state(config.state_dir, state)  # also persists a recovered index
        return [
            Outcome(
                notebook.note_id,
                status,
                source_path=notebook.note_id,
                title=folder.name,
                skipped=True,
                reasons=manifest.get("approximate_reasons", []),
            )
        ]
    if not unchanged:
        raw, cover = source.pull_notebook(notebook)
        written = archive_notebook(
            config.archive_dir,
            raw,
            device_serial=config.device_serial,
            source_path=f"ksync:{notebook.note_id}",
            state=state,
            cover=cover,
            signature=notebook.signature,
            title=notebook.title,
            clock=clock,
            keep_revisions=config.keep_revisions,
            expected_note_id=notebook.note_id,
        )
        save_state(config.state_dir, state)
        folders = [item.folder_name for item in written]
    else:
        folders = [entry["archive_folder"]]
    outcomes = []
    for folder in folders:
        if config.render_after_sync:
            result = export_pdf(config.archive_dir, folder, clock=clock)
            outcomes.append(
                Outcome(
                    notebook.note_id,
                    result.status,
                    result.error,
                    notebook.note_id,
                    folder,
                    reasons=result.reasons,
                )
            )
        else:
            outcomes.append(
                Outcome(
                    notebook.note_id,
                    "pending",
                    source_path=notebook.note_id,
                    title=folder,
                )
            )
    return outcomes


def _pdf_current(archive_root: Path, entry: dict) -> bool:
    try:
        folder = notebook_folder(archive_root, entry["archive_folder"])
        manifest = read_json(folder / "manifest.json") or {}
        return (
            manifest.get("render_status") in {"ok", "approximate"}
            and int(manifest.get("renderer_version") or 0) == RENDERER_VERSION
            and (folder / f"{folder.name}.pdf").is_file()
            and manifest.get("rendered_sha256")
            == manifest.get("content_sha256")
            == entry.get("sha256")
        )
    except (ValueError, KeyError, TypeError):
        return False


def _archived_note_ok(archive_root: Path, entry: dict) -> bool:
    try:
        folder = notebook_folder(archive_root, entry["archive_folder"])
        manifest = read_json(folder / "manifest.json") or {}
        digest = hashlib.sha256(
            (folder / f"{folder.name}.note").read_bytes()
        ).hexdigest()
        return manifest.get("note_id") == entry.get(
            "note_id"
        ) and digest == manifest.get("content_sha256") == entry.get("sha256")
    except (OSError, ValueError, KeyError):
        return False
