"""Where the app keeps settings, named display profiles, the preview cache and session logs.

``%LOCALAPPDATA%\\Timelapse Video Processing\\`` on Windows, overridable with ``ETALUMA_DATA_DIR``
(tests always set it to a temp folder). JSON is written atomically, with a short retry loop
because Windows readers and antivirus can briefly hold the destination open.

Ported from Codex 0.3 ``desktop_support.py``; the Streamlit/WebView request bridge is dropped.
"""
from __future__ import annotations

import json
import os
import shutil
import time
import uuid
from pathlib import Path

from .. import USER_DATA_DIRNAME, OLD_USER_DATA_DIRNAMES

__all__ = ["user_data_dir", "profiles_dir", "preview_cache_dir", "logs_dir", "settings_path",
           "atomic_write_json", "read_json", "load_settings", "save_settings",
           "output_folder_to_open", "format_duration"]


def user_data_dir() -> Path:
    """Root of the per-user data folder; created on first use."""
    override = os.environ.get("ETALUMA_DATA_DIR")
    root = Path(override) if override else Path(os.environ.get("LOCALAPPDATA", Path.home())) / USER_DATA_DIRNAME
    if not override and not root.exists():
        _copy_from_old_name(root)
    root.mkdir(parents=True, exist_ok=True)
    return root


#: What comes over from the folder of the app's earlier name (0.8); caches and logs are rebuilt.
_CARRIED_OVER = ("settings.json", "profiles", "output_presets", "experiments", "queue.json")


def _copy_from_old_name(root: Path) -> None:
    """First start under the new name: copy settings, profiles, presets, memory and queue (the old folder stays)."""
    for old_name in OLD_USER_DATA_DIRNAMES:
        old = root.parent / old_name
        if not old.is_dir():
            continue
        root.mkdir(parents=True, exist_ok=True)
        for name in _CARRIED_OVER:
            source = old / name
            try:
                if source.is_dir():
                    shutil.copytree(source, root / name, dirs_exist_ok=True)
                elif source.is_file():
                    shutil.copy2(source, root / name)
            except OSError:
                pass
        return


def _sub(name: str) -> Path:
    path = user_data_dir() / name
    path.mkdir(parents=True, exist_ok=True)
    return path


def profiles_dir() -> Path:
    """Named display profiles the user saved (``<name>.json``)."""
    return _sub("profiles")


def preview_cache_dir() -> Path:
    """Cached preview stacks and histogram files; safe to delete at any time."""
    return _sub("preview_cache")


def logs_dir() -> Path:
    """Rotating session logs written by the UI log dock."""
    return _sub("logs")


def settings_path() -> Path:
    return user_data_dir() / "settings.json"


def atomic_write_json(path: Path | str, data) -> None:
    """Write JSON through a temporary file and one atomic replace."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        temporary.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
        # Windows readers and antivirus may briefly hold the destination open.
        for attempt in range(10):
            try:
                os.replace(temporary, path)
                break
            except PermissionError:
                if attempt == 9:
                    raise
                time.sleep(min(0.02 * 2 ** attempt, 0.2))
    finally:
        temporary.unlink(missing_ok=True)


def read_json(path: Path | str, default=None):
    """Parsed JSON, or ``default`` when the file is missing or corrupt."""
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def load_settings() -> dict:
    data = read_json(settings_path(), {})
    return data if isinstance(data, dict) else {}


def save_settings(settings: dict) -> None:
    if not isinstance(settings, dict):
        raise TypeError("Settings must be a dictionary")
    atomic_write_json(settings_path(), settings)


def output_folder_to_open(reviewed=None, recent=(), configured=None) -> Path | None:
    """First folder that actually exists: the reviewed run, then recent runs, then the default."""
    for value in [reviewed, *recent, configured]:
        if value and Path(value).is_dir():
            return Path(value)
    return None


def format_duration(seconds: float) -> str:
    if seconds < 60:
        return f"{seconds:.1f} seconds"
    minutes, seconds = divmod(int(seconds), 60)
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h {minutes}m {seconds}s" if hours else f"{minutes}m {seconds}s"
