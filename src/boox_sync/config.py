"""Load and check sync configuration."""

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_SERIAL = ""
DEFAULT_ADB = "adb"
DEFAULT_ROOTS = ["/storage/emulated/0/note", "/sdcard/note"]
_SERIAL = re.compile(r"^[A-Za-z0-9]+$")
_SHELL_META = re.compile(r"[;&|$`()<>\\'\"*?\[\]\s]")


class ConfigError(ValueError):
    """The config refuses a sync before any file is written."""


@dataclass
class Config:
    """Paths and device identity for one sync pass."""

    device_serial: str = DEFAULT_SERIAL
    adb_path: str = DEFAULT_ADB
    archive_dir: Path = field(
        default_factory=lambda: Path.home() / "Documents" / "BOOX"
    )
    poll_seconds: int = 30
    adb_timeout_seconds: int = 120
    keep_revisions: int = 5
    render_after_sync: bool = True
    remote_roots: list[str] = field(default_factory=lambda: list(DEFAULT_ROOTS))
    state_dir: Path = field(
        default_factory=lambda: (
            Path.home() / "Library" / "Application Support" / "BOOX Sync"
        )
    )


def default_config_path() -> Path:
    """Return the on-disk config path."""
    return Path.home() / "Library" / "Application Support" / "BOOX Sync" / "config.json"


def load_config(path: Path | None = None) -> Config:
    """Load config JSON, or the built-in defaults when the file is missing.

    Args:
        path: Config file. The default path is used when this is omitted.

    Returns:
        A config with user paths expanded.
    """
    config_path = default_config_path() if path is None else path
    if not config_path.exists():
        if path is not None:
            raise ConfigError(
                f"Config file does not exist: {path}; use boox-sync init --config PATH"
            )
        return Config()
    try:
        data = json.loads(config_path.read_text())
        if not isinstance(data, dict):
            raise ValueError("expected a JSON object")
        archive = data.get("archive_dir", "~/Documents/BOOX")
        state = data.get("state_dir", "~/Library/Application Support/BOOX Sync")
        config = Config(
            device_serial=str(data.get("device_serial", DEFAULT_SERIAL)),
            adb_path=str(data.get("adb_path", DEFAULT_ADB)),
            archive_dir=Path(archive).expanduser(),
            poll_seconds=data.get("poll_seconds", 30),
            adb_timeout_seconds=data.get("adb_timeout_seconds", 120),
            keep_revisions=data.get("keep_revisions", 5),
            render_after_sync=data.get("render_after_sync", True),
            remote_roots=data.get("remote_roots", list(DEFAULT_ROOTS)),
            state_dir=Path(state).expanduser(),
        )
        validate_config(config)
        return config
    except (OSError, ValueError, TypeError) as exc:
        raise ConfigError(f"Cannot read config {config_path}: {exc}") from exc


def validate_config(config: Config) -> None:
    """Reject a serial or remote root that must not be sent to a shell.

    Args:
        config: Config about to be used.

    Raises:
        ConfigError: The serial or a remote root is not usable.
    """
    if not config.device_serial:
        raise ConfigError("No device selected; run boox-sync init --serial YOUR_SERIAL")
    if _SERIAL.fullmatch(config.device_serial) is None:
        raise ConfigError("invalid device serial")
    for name in ("poll_seconds", "adb_timeout_seconds", "keep_revisions"):
        value = getattr(config, name)
        if type(value) is not int or value < 1:
            raise ConfigError(f"{name} must be a positive integer")
    if not isinstance(config.render_after_sync, bool):
        raise ConfigError("render_after_sync must be true or false")
    if not isinstance(config.remote_roots, list) or not config.remote_roots:
        raise ConfigError("invalid remote root")
    for root in config.remote_roots:
        if (
            not isinstance(root, str)
            or not root.startswith("/")
            or _SHELL_META.search(root)
            or ".." in root.split("/")
        ):
            raise ConfigError("invalid remote root")


def save_config(config: Config, path: Path | None = None) -> Path:
    """Save an explicitly initialized configuration."""
    from boox_sync.storage import write_json

    validate_config(config)
    target = path or default_config_path()
    write_json(
        target,
        {
            "device_serial": config.device_serial,
            "adb_path": config.adb_path,
            "archive_dir": str(config.archive_dir),
            "state_dir": str(config.state_dir),
            "poll_seconds": config.poll_seconds,
            "adb_timeout_seconds": config.adb_timeout_seconds,
            "keep_revisions": config.keep_revisions,
            "render_after_sync": config.render_after_sync,
            "remote_roots": config.remote_roots,
        },
    )
    return target
