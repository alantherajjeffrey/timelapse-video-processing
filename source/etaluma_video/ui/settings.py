"""Persistent user settings and the user data folder.

Settings live in ``<user data dir>/settings.json``. The user data folder is
``%LOCALAPPDATA%\\Timelapse Video Processing`` unless ``ETALUMA_DATA_DIR`` overrides
it (tests always override it). The engine will grow a ``user_data_dir`` helper;
this module uses it as soon as it exists and falls back to the same rule.
"""
from __future__ import annotations

import json
import logging
import os
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path

from .. import USER_DATA_DIRNAME

log = logging.getLogger("etaluma.ui")

WIDTH_CHOICES: tuple[int, ...] = (640, 950, 1900)
PREVIEW_WIDTH_CHOICES: tuple[int, ...] = (640, 950)


def _local_user_data_dir() -> Path:
    """The documented rule, implemented here until ``engine.user_data_dir`` exists."""
    override = os.environ.get("ETALUMA_DATA_DIR")
    if override:
        return Path(override).expanduser()
    base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~/.local/share")
    return Path(base) / USER_DATA_DIRNAME


def user_data_dir() -> Path:
    """User data folder, created on demand. Prefers the engine helper when available."""
    path: Path | None = None
    try:  # engine facade may not be written yet
        from ..engine import user_data_dir as engine_user_data_dir  # type: ignore

        path = Path(engine_user_data_dir())
    except Exception:
        path = None
    if path is None:
        path = _local_user_data_dir()
    path.mkdir(parents=True, exist_ok=True)
    return path


def profiles_dir() -> Path:
    p = user_data_dir() / "profiles"
    p.mkdir(parents=True, exist_ok=True)
    return p


def preview_cache_dir() -> Path:
    p = user_data_dir() / "preview_cache"
    p.mkdir(parents=True, exist_ok=True)
    return p


def logs_dir() -> Path:
    p = user_data_dir() / "logs"
    p.mkdir(parents=True, exist_ok=True)
    return p


def friendly_path(path: str | Path) -> str:
    """``path`` as it reads on any PC: %LOCALAPPDATA%, %APPDATA% or %USERPROFILE% instead of this user's folder."""
    text = str(path)
    for variable in ("LOCALAPPDATA", "APPDATA", "USERPROFILE"):
        base = os.environ.get(variable)
        if base and text.lower().startswith(base.rstrip("\\/").lower()):
            return f"%{variable}%" + text[len(base.rstrip("\\/")):]
    return text


def friendly_text(text: str) -> str:
    """Every occurrence of this user's profile folders in ``text`` replaced by %LOCALAPPDATA% etc."""
    for variable in ("LOCALAPPDATA", "APPDATA", "USERPROFILE"):
        base = os.environ.get(variable)
        if base:
            base = base.rstrip("\\/")
            for spelling in {base, base.replace("\\", "/")}:
                start = text.lower().find(spelling.lower())
                while start >= 0:
                    text = text[:start] + f"%{variable}%" + text[start + len(spelling):]
                    start = text.lower().find(spelling.lower(), start + len(variable) + 2)
    return text


def settings_path() -> Path:
    return user_data_dir() / "settings.json"


@dataclass
class Settings:
    """Everything the app remembers between sessions."""

    schema: int = 3
    theme: str = "terracotta"  # "terracotta" | "dark" | "light"
    # output defaults
    default_width: int = 950
    avi: bool = True
    mp4: bool = True
    duration_seconds: float = 10.0
    composite: bool = True
    fluorescence_only: bool = True
    per_channel: bool = True
    montages: bool = True
    dashboard: bool = True
    app_scale_bar: bool = False
    app_timestamp: bool = True  # 0.6: on by default (0.5 greyed the checkbox out, so its False meant nothing)
    name_label: bool = False  # "<experiment> / <position>" beside the time
    time_offset: str = ""  # start time as typed in the Output panel (Day d : hh:mm:ss)
    time_days: str = "auto"  # auto | always | never
    video_quality: str = "standard"  # MP4: high | standard | small
    channel_key: bool = False  # colour key of the channels on the videos (0.7)
    output_preset: str = "builtin:standard"  # the Output panel's preset (0.8)
    quick_display: str = "auto"  # Quick video: display choice (0.8)
    quick_preset: str = "builtin:quick"  # Quick video: output preset (0.8)
    queue_display: str = "auto"  # queue: display choice of new items (0.8)
    queue_preset: str = "builtin:standard"  # queue: output preset of new items (0.8)
    queue_output_mode: str = "experiment"  # experiment (its analysis_output) | folder (one common folder)
    queue_output_folder: str = ""
    exclude_overlays: bool = True
    # preview and display defaults
    preview_cache_width: int = 640
    rolling_ball_radius: int = 50
    last_profile_json: str = ""  # last manual DisplayProfile, restored on start
    # paths
    last_folder: str = ""
    last_output_folder: str = ""
    last_profile_folder: str = ""
    # window
    window_geometry: str = ""  # base64 of QMainWindow.saveGeometry()
    window_state: str = ""  # base64 of QMainWindow.saveState()
    log_debug: bool = False
    recent_folders: list = field(default_factory=list)  # newest first (0.7)
    extra: dict = field(default_factory=dict)  # forward compatibility

    # ---- persistence ---------------------------------------------------- #
    @classmethod
    def load(cls, path: Path | str | None = None) -> "Settings":
        p = Path(path) if path is not None else settings_path()
        s = cls()
        if not p.is_file():
            return s
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except Exception as exc:  # corrupt file must never stop the app
            log.warning("Settings file could not be read (%s); defaults are used.", exc)
            return s
        known = {f.name for f in fields(cls)}
        for key, value in (data or {}).items():
            if key in known and key != "extra":
                try:
                    setattr(s, key, type(getattr(s, key))(value) if not isinstance(value, type(getattr(s, key))) else value)
                except Exception:
                    setattr(s, key, value)
            else:
                s.extra[key] = value
        if int((data or {}).get("schema", 1) or 1) < 2:
            s.app_timestamp = True  # see the field comment
        if int((data or {}).get("schema", 1) or 1) < 3 and s.theme == "dark":
            s.theme = "terracotta"  # 0.7's default; "dark" was only ever the old default
        s.schema = 3
        return s

    def save(self, path: Path | str | None = None) -> Path:
        p = Path(path) if path is not None else settings_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(p.suffix + ".tmp")
        tmp.write_text(json.dumps(asdict(self), indent=2), encoding="utf-8")
        tmp.replace(p)
        return p

    # ---- helpers -------------------------------------------------------- #
    def as_dict(self) -> dict:
        return asdict(self)

    def update(self, **values) -> None:
        known = {f.name for f in fields(self)}
        for key, value in values.items():
            if key in known:
                setattr(self, key, value)

    def remember_folder(self, folder: str) -> None:
        """Put ``folder`` first in the recent list (at most eight)."""
        folder = str(folder)
        self.recent_folders = [folder] + [f for f in list(self.recent_folders or []) if f != folder][:7]
