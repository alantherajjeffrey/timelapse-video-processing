"""Saved output presets, and the display and output choices of Quick video and the queue (0.8).

Output preset keys: ``builtin:<id>`` (Quick, Standard, Presentation), ``user:<name>`` (saved as
JSON in ``<user data>/output_presets``) and ``current`` (the Output panel as it is, passed in as a
snapshot). Display choice keys: ``auto`` (Auto-normalised, measured on each folder with the
default colours), ``remembered`` (the experiment's own display from the per-experiment memory,
else Auto-normalised), ``current`` (a snapshot of the Display panel) and ``profile:<name>`` (a
display profile saved in ``<user data>/profiles``). Nothing here touches widgets, so the queue's
worker thread uses the same functions.
"""
from __future__ import annotations

import json
import logging
import re
from pathlib import Path

from ..engine import presets as engine_presets
from ..engine.models import DisplayProfile
from ..engine.userdata import atomic_write_json, profiles_dir, user_data_dir

log = logging.getLogger("etaluma.ui")

AUTO, REMEMBERED, CURRENT = "auto", "remembered", "current"
QUICK = "builtin:quick"
DISPLAY_LABELS = {AUTO: "Auto-normalised", REMEMBERED: "Remembered for this experiment",
                  CURRENT: "Current display settings"}
CURRENT_PRESET_LABEL = "Current output settings"
_UNSAFE = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


# ---- output presets ---------------------------------------------------------------- #
def presets_dir() -> Path:
    path = user_data_dir() / "output_presets"
    path.mkdir(parents=True, exist_ok=True)
    return path


def clean_name(name) -> str:
    text = " ".join(str(name or "").split())
    if not text or len(text) > 60 or _UNSAFE.search(text) or text.endswith(".") or text.startswith("."):
        raise ValueError('A preset name needs 1 to 60 characters, without < > : " / \\ | ? * '
                         "and without a dot at either end.")
    return text


def user_presets() -> dict[str, dict]:
    found: dict[str, dict] = {}
    for path in sorted(presets_dir().glob("*.json"), key=lambda p: p.stem.casefold()):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            found[str(data.get("name") or path.stem)] = engine_presets.normalise(data.get("values"))
        except (OSError, ValueError, AttributeError) as exc:
            log.debug("Output preset %s skipped: %s", path.name, exc)
    return found


def save_user_preset(name, values: dict) -> str:
    name = clean_name(name)
    builtin = {v["label"].casefold() for v in engine_presets.BUILTIN_PRESETS.values()}
    if name.casefold() in builtin:
        raise ValueError(f"{name} is a built-in preset; choose another name.")
    atomic_write_json(presets_dir() / f"{name}.json",
                      {"schema": 1, "name": name, "values": engine_presets.normalise(values)})
    return f"user:{name}"


def delete_user_preset(name) -> None:
    (presets_dir() / f"{clean_name(name)}.json").unlink(missing_ok=True)


def rename_user_preset(old, new) -> str:
    values = user_presets().get(old)
    if values is None:
        raise ValueError(f"There is no saved preset called {old}.")
    key = save_user_preset(new, values)
    if clean_name(old).casefold() != clean_name(new).casefold():
        delete_user_preset(old)
    return key


def preset_choices() -> list[tuple[str, str]]:
    choices = [(f"builtin:{k}", engine_presets.BUILTIN_PRESETS[k]["label"]) for k in engine_presets.BUILTIN_ORDER]
    return choices + [(f"user:{name}", name) for name in user_presets()]


def preset_label(key: str) -> str:
    if key == CURRENT:
        return CURRENT_PRESET_LABEL
    kind, _, name = str(key).partition(":")
    if kind == "builtin" and name in engine_presets.BUILTIN_PRESETS:
        return engine_presets.BUILTIN_PRESETS[name]["label"]
    return name or str(key)


def preset_description(key: str) -> str:
    kind, _, name = str(key).partition(":")
    if kind == "builtin" and name in engine_presets.BUILTIN_PRESETS:
        return engine_presets.BUILTIN_PRESETS[name]["description"]
    if key == CURRENT:
        return "The choices in the Output panel as they are now"
    return "Saved output preset"


def preset_values(key: str, snapshot: dict | None = None) -> dict:
    """The preset's values; ``current`` returns the snapshot. Raises ValueError for a missing preset."""
    if key == CURRENT:
        return engine_presets.normalise(snapshot)
    kind, _, name = str(key).partition(":")
    if kind == "builtin" and name in engine_presets.BUILTIN_PRESETS:
        return engine_presets.builtin_values(name)
    if kind == "user":
        values = user_presets().get(name)
        if values is not None:
            return values
    raise ValueError(f"The output preset {preset_label(key)} no longer exists.")


def is_quick(key: str) -> bool:
    return key == QUICK


# ---- display choices ---------------------------------------------------------------- #
def saved_profiles() -> list[str]:
    try:
        return sorted((p.stem for p in profiles_dir().glob("*.json")), key=str.casefold)
    except OSError:
        return []


def display_choices() -> list[tuple[str, str]]:
    return [(k, DISPLAY_LABELS[k]) for k in (AUTO, REMEMBERED, CURRENT)] + \
           [(f"profile:{name}", f"Profile: {name}") for name in saved_profiles()]


def display_label(key: str) -> str:
    if key in DISPLAY_LABELS:
        return DISPLAY_LABELS[key]
    return f"Profile: {str(key).partition(':')[2]}" if str(key).startswith("profile:") else str(key)


def resolve_display(key: str, dataset, snapshot: dict | None = None) -> tuple[DisplayProfile, str]:
    """(profile, what it is) for one experiment. Raises ValueError when a saved profile cannot be read."""
    from .display_panel import adapt_profile  # noqa: WPS433 - shared with the Display panel

    channels = list(getattr(dataset, "channels", []) or [])
    profile = note = None
    if key == REMEMBERED:
        from .experiment_memory import load_record  # noqa: WPS433
        from .state import dataset_key  # noqa: WPS433

        record = load_record(dataset_key(dataset)) or {}
        if isinstance(record.get("profile"), dict):
            profile, note = DisplayProfile.from_dict(record["profile"]), "the display remembered for this experiment"
        else:
            note = "Auto-normalised (nothing remembered for this experiment yet)"
    elif key == CURRENT and snapshot:
        profile, note = DisplayProfile.from_dict(snapshot), "the display settings as they were when added"
    elif str(key).startswith("profile:"):
        name = str(key).partition(":")[2]
        try:
            profile = DisplayProfile.load(profiles_dir() / f"{name}.json")
        except Exception as exc:
            raise ValueError(f"The display profile {name} could not be read: {exc}") from exc
        note = f"display profile {name}"
    if profile is None:
        profile, note = DisplayProfile.default_for(channels), note or "Auto-normalised"
    return adapt_profile(profile, channels), note


# ---- engine options ----------------------------------------------------------------- #
def options_for(dataset, preset: dict, profile: DisplayProfile, experiment: dict | None = None, *,
                quick: bool = False, calibration=None):
    """Engine ``Options`` from a preset, a display and what the experiment remembers.

    ``experiment`` holds the per-experiment choices (channel names, start time as typed,
    timepoint range); the preset decides everything else it covers.
    """
    from ..engine.overlays import parse_elapsed  # noqa: WPS433
    from ..engine.process import Options  # noqa: WPS433

    opts = engine_presets.apply_preset(Options(profile=profile.copy(), quick=bool(quick)), preset)
    record = experiment or {}
    opts.channel_names = {str(k): str(v).strip() for k, v in (record.get("channel_names") or {}).items()
                          if str(v).strip()}
    try:
        opts.time_offset_seconds = float(parse_elapsed(record.get("time_offset") or ""))
    except ValueError as exc:
        log.warning("Start time ignored: %s", exc)
    span = record.get("timepoint_range")
    if isinstance(span, (list, tuple)) and len(span) == 2:
        try:
            first, last = int(span[0]), int(span[1])
        except (TypeError, ValueError):
            first = last = 0
        if 1 <= first <= last:
            opts.timepoint_range = [first, last]
    if calibration is not None and getattr(calibration, "usable", False):
        opts.calibration = calibration
    return opts
