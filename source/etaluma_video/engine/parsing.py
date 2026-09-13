"""Capture discovery and metadata parsing: folders and filenames become a Dataset.

This module owns everything that turns files on disk into the in-memory model:

* the filename grammar for all five Etaluma folder layouts,
* ``.epf`` (protocol), ``.avs`` (AviSynth) and ``.roi`` (stage map) XML/text parsing,
* :func:`scan_dataset` and the batch discovery helpers,
* the source fingerprint (paths + sizes + mtimes) used to invalidate caches and runs,
* pixel access: :func:`read_plane` (one channel plane) and :func:`read_rgb` (the whole frame).

Ported from Codex 0.3 ``etaluma_common.py`` with the plane map and the objective table taken
from :mod:`etaluma_video.engine.models` so there is a single source of truth. Nothing here
renders pixels; nothing here imports Qt.

Exclusions that must never change: ``thumbnail`` folders (63x64 PNG data with a ``.tif``
extension) and ``analysis_output`` folders (this app's own results) are invisible to frame
discovery, exactly as in Codex 0.3.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
import xml.etree.ElementTree as ET
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import tifffile

from .models import CHANNEL_ORDER, CHANNEL_PLANE, OBJECTIVES

__all__ = [
    "summarize_warnings",
    "CHANNELS",
    "OBJECTIVES",
    "Frame",
    "Dataset",
    "excluded",
    "parse_filename",
    "parse_epf",
    "parse_avs",
    "parse_roi",
    "scan_dataset",
    "discover_experiments",
    "find_experiments",
    "position_for",
    "inventory",
    "summary",
    "read_plane",
    "read_rgb",
    "thumbnail_path",
    "current_inventory_fingerprint",
]

#: Per-channel acquisition metadata. ``plane`` mirrors models.CHANNEL_PLANE; ``rgb`` is the raw
#: montage colour Codex 0.3 used (F3 is red here, while the CGM display preset makes it magenta).
CHANNELS: dict[str, dict] = {
    "WHITE": {"plane": CHANNEL_PLANE["WHITE"], "rgb": [1, 1, 1], "led": "Transmitted", "lut": "gray",
              "fluorophores": "Phase / brightfield"},
    "F1": {"plane": CHANNEL_PLANE["F1"], "rgb": [0, 1, 1], "led": "405 nm", "lut": "cyan",
           "fluorophores": "DAPI, Hoechst, BFP"},
    "F2": {"plane": CHANNEL_PLANE["F2"], "rgb": [0, 1, 0], "led": "488 nm", "lut": "green",
           "fluorophores": "GFP, FITC, Fluo-4"},
    "F3": {"plane": CHANNEL_PLANE["F3"], "rgb": [1, 0, 0], "led": "594 nm", "lut": "red",
           "fluorophores": "RFP, mCherry"},
}

ROI_RE = re.compile(r"(?:^|_)ROI-(\d+[a-z0-9]*)(?=_|$)", re.I)
#: Lumaview labware well names, "Well" + row letters + column number: Wella1, Welli11, Wellk22 (0.7).
WELL_RE = re.compile(r"(?:^|_)(Well[a-z]{1,2}\d{1,3})(?=_|$)", re.I)
END_RE = re.compile(r"_(WHITE|F1|F2|F3)_(\d{6})\.(?:tif|tiff)$", re.I)


@dataclass
class Frame:
    """One capture file: a position, a channel and a serial number."""

    path: str
    roi: str
    order: int
    id: str
    descriptor: str
    channel: str
    serial: int


@dataclass
class Dataset:
    """One experiment folder, fully inventoried and cross-checked against its metadata."""

    root: str
    layout: str
    mode: str
    frames: list[Frame]
    protocols: list[dict]
    avs: list[dict]
    positions: list[dict]
    warnings: list[str] = field(default_factory=list)
    source_fingerprint: str = ""

    @property
    def name(self) -> str:
        return Path(self.root).name

    @property
    def groups(self) -> dict[str, dict[str, list[Frame]]]:
        """{roi: {channel: [Frame, ...]}} in scan order."""
        result: dict[str, dict[str, list[Frame]]] = {}
        for f in self.frames:
            result.setdefault(f.roi, {}).setdefault(f.channel, []).append(f)
        return result

    @property
    def channels(self) -> list[str]:
        return [c for c in CHANNEL_ORDER if any(f.channel == c for f in self.frames)]

    @property
    def protocol(self) -> dict:
        return self.protocols[0] if self.protocols else {}

    @property
    def interval(self) -> float | None:
        return self.protocol.get("interval_seconds")

    @property
    def objective_metadata(self) -> dict:
        """Objective evidence found in the protocol/AVS text (still a source for the calibration card)."""
        evidence = [item for source in [*self.protocols, *self.avs] for item in source.get("objective_metadata", [])]
        values = {item["objective"] for item in evidence}
        value = next(iter(values)) if len(values) == 1 and None not in values else None
        status = "detected" if value else "missing" if not evidence else "unsupported or conflicting"
        return {"objective": value, "status": status, "evidence": evidence}

    @property
    def n_timepoints(self) -> int:
        return (max(f.serial for f in self.frames) + 1) if self.frames else 0


# --------------------------------------------------------------------------- #
# Source fingerprint
# --------------------------------------------------------------------------- #


_PLANNED = re.compile(r"^(?P<roi>[^/]+)/(?P<ch>[^:]+): (?P<got>\d+) captured vs (?P<want>\d+) planned; "
                      r"early stop or partial copy\.$")


def _order_by_capture_time(frames: list[Frame]) -> None:
    """Number labware-named positions 1, 2, ... in the order they were captured.

    Their names (Wella1, Welli11) carry no capture order, and the protocol of such runs lists a
    single placeholder stage position. Lumaview captures positions one after another within a
    timepoint, so the file times of each position's earliest capture give the order; names sort
    naturally when times tie or are missing.
    """
    first: dict[str, tuple[float, int]] = {}
    for f in frames:
        try:
            stamp = os.path.getmtime(f.path)
        except OSError:
            stamp = float("inf")
        key = (f.serial, stamp)
        if f.roi not in first or key < first[f.roi]:
            first[f.roi] = key
    natural = lambda s: [int(t) if t.isdigit() else t.lower() for t in re.split(r"(\d+)", s)]  # noqa: E731
    ranked = sorted(first, key=lambda roi: (first[roi][0], first[roi][1], natural(roi)))
    order = {roi: i + 1 for i, roi in enumerate(ranked)}
    for f in frames:
        f.order = order[f.roi]


_NO_STAGE = re.compile(r"^(?P<roi>.+): no unambiguous stage position\.$")


def summarize_warnings(warnings: list[str]) -> list[str]:
    """The scan warnings with the per-position, per-channel "captured vs planned" lines collapsed.

    CD14 printed 18 of them (6 positions x 3 channels, all "196 captured vs 300 planned"); they
    become one line per distinct count, in the place of the first one. Other warnings are kept.
    """
    out: list[str | tuple[int, int]] = []
    groups: dict[tuple[int, int], dict] = {}
    for w in warnings:
        m = _PLANNED.match(w)
        if not m:
            out.append(w)
            continue
        key = (int(m["got"]), int(m["want"]))
        if key not in groups:
            groups[key] = {"rois": [], "channels": [], "lines": []}
            out.append(key)
        g = groups[key]
        g["lines"].append(w)
        if m["roi"] not in g["rois"]:
            g["rois"].append(m["roi"])
        if m["ch"] not in g["channels"]:
            g["channels"].append(m["ch"])
    no_stage = [w for w in out if isinstance(w, str) and _NO_STAGE.match(w)]
    if len(no_stage) > 1:  # labware-named runs: one line instead of one per position
        first = out.index(no_stage[0])
        out = [w for w in out if w not in no_stage]
        names = ", ".join(_NO_STAGE.match(w)["roi"] for w in no_stage[:6]) + (", …" if len(no_stage) > 6 else "")
        out.insert(first, f"{len(no_stage)} positions ({names}) have no stage coordinates in the protocol; "
                          "the stage map leaves them out.")
    lines: list[str] = []
    for item in out:
        if isinstance(item, str):
            lines.append(item)
            continue
        g = groups[item]
        if len(g["lines"]) == 1:
            lines.append(g["lines"][0])
            continue
        where = (f"{len(g['rois'])} positions" if len(g["rois"]) > 1 else g["rois"][0]) + " × " + \
            (f"{len(g['channels'])} channels ({', '.join(g['channels'])})" if len(g["channels"]) > 1 else g["channels"][0])
        lines.append(f"{where}: {item[0]} of {item[1]} planned timepoints captured; early stop or partial copy.")
    return lines


def _excluded_name(name: str) -> bool:
    return "thumbnail" in name.lower() or name.lower().startswith("analysis_output")


def excluded(path: Path | str, root: Path | str | None = None) -> bool:
    """True for thumbnail caches and this app's own outputs; they are never capture sources.

    ``root`` restricts the check to the parts below the experiment folder, so an experiment that
    happens to live under a folder called "thumbnails" is still readable.
    """
    parts = Path(path).parts
    if root is not None:
        try:
            parts = Path(path).resolve().relative_to(Path(root).resolve()).parts
        except (ValueError, OSError):
            pass
    return any(_excluded_name(part) for part in parts)


def _source_inventory(ds: Dataset) -> list[tuple[str, int, int]]:
    paths = set()
    for directory, dirs, filenames in os.walk(ds.root, followlinks=False):
        dirs[:] = [d for d in dirs if not _excluded_name(d) and not (Path(directory) / d).is_symlink()]
        for name in filenames:
            p = Path(directory) / name
            if p.suffix.lower() in (".tif", ".tiff", ".epf", ".avs", ".roi") and not excluded(p, ds.root) and not p.is_symlink():
                paths.add(p.resolve())
    # A manually selected ROI file may live outside the experiment.
    for item in [*ds.protocols, *ds.avs, *ds.positions]:
        source = item.get("path") or item.get("source")
        if source:
            paths.add(Path(source).resolve())
    rows = []
    for p in sorted(paths):
        try:
            stat = p.stat()
            rows.append((str(p), stat.st_size, stat.st_mtime_ns))
        except FileNotFoundError:
            # Keep missing referenced metadata in the signature so deletion invalidates normally.
            rows.append((str(p), -1, -1))
    return rows


def current_inventory_fingerprint(ds: Dataset) -> str:
    """SHA-256 over (path, size, mtime) of every capture and metadata file of the experiment."""
    return hashlib.sha256(json.dumps(_source_inventory(ds), separators=(",", ":")).encode()).hexdigest()


# --------------------------------------------------------------------------- #
# Filename grammar
# --------------------------------------------------------------------------- #


def parse_filename(path: Path | str, root: Path | str | None = None) -> Frame | None:
    """Decompose a capture path into a Frame, or None when it is not an Etaluma capture.

    Lumaview writes ``<prefix>_<position>[_<descriptor>]_<channel>_<serial>.tif``. The position is
    ``ROI-<order><id>`` for stage positions (order and id come from the name), a labware well
    such as ``Wella1`` or ``Welli11``, or any other name (0.7). With ``root`` given, a file inside a
    position folder (``<root>/<position>/...`` or ``<root>/<position>/<channel>/...``) takes the
    folder's name, which also covers names with underscores. Labware positions get their order
    from the capture times in :func:`scan_dataset`.
    """
    # PureWindowsPath-like inputs also work when tests run on Linux.
    parts = re.split(r"[\\/]", str(path))
    name = parts[-1]
    if not re.search(r"\.tiff?$", name, re.I):
        return None
    tail = END_RE.search(name)
    serial_match = re.search(r"(?:^|_)(\d{6})\.tiff?$", name, re.I)
    if not serial_match:
        return None
    channel = tail.group(1).upper() if tail else next((x.upper() for x in reversed(parts[:-1]) if x.upper() in CHANNELS), None)
    if channel is None:
        return None
    stem = Path(name).stem
    match = ROI_RE.search(stem)
    if match is None:
        match = next((m for p in reversed(parts[:-1]) if (m := ROI_RE.search(p))), None)
    if match is not None:
        token = match.group(1)
        order_match = re.match(r"(\d+)(.*)", token)
        descriptor_match = re.search(r"ROI-[a-z0-9]+_(.*?)_(?:WHITE|F1|F2|F3)_\d{6}\.tiff?$", name, re.I)
        return Frame(str(path), "ROI-" + token, int(order_match[1]), order_match[2],
                     descriptor_match[1] if descriptor_match else "", channel, int(serial_match[1]))

    # 0.7: labware wells and any other position name
    head = stem[: tail.start()] if tail else stem[: serial_match.start()]
    position, descriptor = "", ""
    if root is not None:
        root_parts = [p for p in re.split(r"[\\/]", str(root)) if p]
        dirs = [p for p in parts[:-1] if p]
        if [p.casefold() for p in dirs[: len(root_parts)]] == [p.casefold() for p in root_parts]:
            inner = [d for d in dirs[len(root_parts):] if d.upper() not in CHANNELS]
            if inner:
                position = inner[0]
                if f"_{position}_" in f"_{head}_":
                    descriptor = f"_{head}_".split(f"_{position}_", 1)[1].strip("_")
    if not position:
        well = WELL_RE.search(head)
        tokens = [t for t in head.split("_") if t]
        if well is not None:
            position = well.group(1)
            descriptor = head[well.end():].strip("_")
        elif len(tokens) >= 2:  # <prefix>_<position>[_<descriptor>]
            position, descriptor = tokens[1], "_".join(tokens[2:])
    if not position:
        return None
    well = WELL_RE.fullmatch(position) if position.lower().startswith("well") else None
    ident = position[4:] if well is not None else position
    return Frame(str(path), position, 0, ident, descriptor, channel, int(serial_match[1]))


def thumbnail_path(frame: Frame | str | Path) -> Path | None:
    """The Lumaview thumbnail beside a capture (``thumbnail/<name>.thumbnail.tif``), if present.

    Note the file holds PNG data under a ``.tif`` extension; read it with PIL, not tifffile.
    """
    p = Path(frame.path if isinstance(frame, Frame) else frame)
    candidate = p.parent / "thumbnail" / f"{p.stem}.thumbnail.tif"
    return candidate if candidate.is_file() else None


# --------------------------------------------------------------------------- #
# Metadata parsers
# --------------------------------------------------------------------------- #


def xml_tree(path: Path | str):
    root = ET.parse(path).getroot()
    for el in root.iter():
        el.tag = el.tag.split("}")[-1]
    return root


def positions_from_xml(root, source) -> list[dict]:
    positions = []
    for el in root.iter():
        values = {c.tag.lower(): (c.text or "").strip() for c in el}
        if {"order", "id", "x", "y", "z"} <= values.keys():
            p = {"order": int(values["order"]), "id": values["id"], "source": str(source)}
            p.update({k: float(values[k]) for k in ("x", "y", "z")})
            if not all(math.isfinite(p[k]) for k in ("x", "y", "z")):
                raise ValueError(f"Non-finite position in {source}")
            positions.append(p)
    return positions


def parse_roi(path: Path | str) -> list[dict]:
    return positions_from_xml(xml_tree(path), path)


def objective_evidence(value, path, field_name) -> dict:
    text = str(value).strip()
    numeric = re.fullmatch(r"(\d+(?:\.\d+)?)\s*[x×]?", text, re.I)
    labels = [float(numeric[1])] if numeric else [float(n) for n in re.findall(r"(?<![\d.])(\d+(?:\.\d+)?)\s*[x×](?![a-z])", text, re.I)]
    candidates = {f"{n:g}x" for n in labels}
    objective = next(iter(candidates)) if len(candidates) == 1 else None
    return {"path": str(path), "field": field_name, "value": text, "objective": objective if objective in OBJECTIVES else None}


def epf_objective_evidence(root, path) -> list[dict]:
    evidence: list[dict] = []

    def visit(el, within_objective=False):
        key = re.sub(r"[^a-z]", "", el.tag.lower())
        explicit = key in {"objective", "objectivemagnification", "objectivename", "objectivelens"}
        if not len(el) and (explicit or within_objective and key in {"magnification", "name"}):
            evidence.append(objective_evidence(el.text or "", path, el.tag))
        for name, value in el.attrib.items():
            normalized = re.sub(r"[^a-z]", "", name.lower())
            if normalized in {"objective", "objectivemagnification"} or explicit and normalized in {"magnification", "name"}:
                evidence.append(objective_evidence(value, path, f"{el.tag}@{name}"))
        for child in el:
            visit(child, within_objective or explicit)

    visit(root)
    return evidence


def parse_epf(path: Path | str) -> dict:
    """Lumaview protocol file: timing, LED settings, stage positions and the raw XML tree."""
    root = xml_tree(path)

    # Keep the original hierarchy as well as interpreted fields, including ordered savedImages.
    def unpack(el):
        if not len(el):
            return (el.text or "").strip()
        out: dict[str, list] = {}
        for child in el:
            out.setdefault(child.tag, []).append(unpack(child))
        return {k: v[0] if len(v) == 1 else v for k, v in out.items()}

    raw = unpack(root)

    def seconds(prefix):
        value = sum(float(raw.get(prefix + suffix, 0) or 0) * multiplier
                    for suffix, multiplier in [("Hours", 3600), ("Minutes", 60), ("Seconds", 1)])
        if not math.isfinite(value) or value < 0:
            raise ValueError(f"Invalid timing in {path}")
        return value or None

    interval, period = seconds("captureEvery"), seconds("totalPeriod")
    return {"path": str(path), "raw": raw, "protocol_name": raw.get("protocolName"),
            "objective_metadata": epf_objective_evidence(root, path),
            "interval_seconds": interval, "planned_duration_seconds": period,
            "expected_frames": math.ceil(period / interval) if interval and period else None,
            "saved_images": [(e.text or "") for e in root.findall(".//savedImages/string")],
            "positions": positions_from_xml(root, path)}


def parse_avs(path: Path | str) -> dict:
    """AviSynth sidecar: ``ImageSource(..., start=, end=, fps=)`` is a frame-count cross-check."""
    content = Path(path).read_text(encoding="utf-8-sig", errors="replace")
    call = re.search(r'ImageSource\s*\(\s*"([^"]+)"(.*?)\)', content, re.I | re.S)
    if not call:
        raise ValueError(f"No ImageSource in {path}")
    args = dict((k.lower(), float(v)) for k, v in re.findall(r"(start|end|fps)\s*=\s*([\d.]+)", call[2], re.I))
    start, end = int(args.get("start", 0)), int(args["end"])
    if end < start:
        raise ValueError(f"Invalid frame range in {path}")
    frame = parse_filename(str(Path(path).parent / call[1].replace("%06d", f"{start:06d}")))
    return {"path": str(path), "pattern": call[1], "start": start, "end": end,
            "objective_metadata": [objective_evidence(value.strip().strip('"'), path, key) for key, value in
                                   re.findall(r"(?im)^\s*#?\s*(objective(?:[_ ]?magnification|[_ ]?name)?)\s*[:=]\s*([^\r\n]+)", content)],
            "n_frames": end - start + 1, "fps": args.get("fps"),
            "roi": frame.roi if frame else None, "channel": frame.channel if frame else None}


# --------------------------------------------------------------------------- #
# Discovery
# --------------------------------------------------------------------------- #


def discover_experiments(folder: Path | str, batch: bool = False) -> list[Path]:
    """[folder] unless batch, in which case every experiment folder found underneath it."""
    root = Path(folder).expanduser().resolve()
    if not root.is_dir():
        raise ValueError(f"Folder does not exist: {root}")
    if not batch:
        return [root]
    found: list[Path] = []

    def visit(p: Path) -> None:
        children = [x for x in sorted(p.iterdir()) if not _excluded_name(x.name)]
        if (any(x.suffix.lower() == ".epf" for x in children if x.is_file())
                or any(parse_filename(x) for x in children if x.is_file())
                or any(x.is_dir() and x.name.upper().startswith("ROI-") for x in children)
                or any(x.is_dir() and WELL_RE.fullmatch(x.name) for x in children)):
            found.append(p)
        else:
            for child in children:
                if child.is_dir() and not child.is_symlink():
                    visit(child)

    visit(root)
    if not found:
        raise ValueError("No experiment folders found.")
    return found


def find_experiments(parent: Path | str) -> list[Path]:
    """Every experiment folder under a batch parent (the explorer's batch node)."""
    return discover_experiments(parent, batch=True)


def scan_dataset(folder: Path | str, roi_file: str | None = None) -> Dataset:
    """Inventory one experiment folder: frames, protocol, AVS, stage positions and warnings."""
    root = Path(folder).expanduser().resolve()
    if not root.is_dir() or _excluded_name(root.name) or _excluded_name(root.parent.name):
        raise ValueError("Choose an experiment folder outside analysis_output or thumbnails.")
    files = sorted(p for p in root.rglob("*") if p.is_file() and not excluded(p, root) and not p.is_symlink())
    # Reject a batch parent: the same ROI/channel/serial often recurs in different experiments.
    nested_protocols = {p.parent for p in files if p.suffix.lower() == ".epf" and p.parent != root}
    if nested_protocols:
        raise ValueError("This folder contains nested experiments. Select batch mode or one experiment.")
    frames, warnings, seen = [], [], {}
    for p in files:
        if p.suffix.lower() not in (".tif", ".tiff"):
            continue
        f = parse_filename(p, root)
        if not f:
            warnings.append(f"Unrecognized TIFF skipped: {p.relative_to(root)}")
            continue
        key = (f.roi, f.channel, f.serial)
        if key in seen:
            raise ValueError(f"Duplicate ROI/channel/frame: {p} and {seen[key]}. Select a single experiment or remove ambiguity.")
        seen[key] = str(p)
        frames.append(f)
    if not frames:
        raise ValueError("No supported Etaluma TIFFs found.")
    protocols, avs, standalone_positions = [], [], []
    for p in files:
        try:
            if p.suffix.lower() == ".epf":
                protocols.append(parse_epf(p))
            elif p.suffix.lower() == ".avs":
                avs.append(parse_avs(p))
            elif p.suffix.lower() == ".roi":
                standalone_positions.extend(parse_roi(p))
        except (ET.ParseError, ValueError, KeyError, OSError) as exc:
            warnings.append(f"Metadata could not be read: {p.name}: {exc}")
    if len(protocols) > 1:
        warnings.append("Multiple protocols: first alphabetically supplies timing; all retained in metadata.")
    if roi_file:
        standalone_positions = parse_roi(roi_file)
    positions = standalone_positions or (protocols[0]["positions"] if protocols else [])
    by_order = defaultdict(list)
    for pos in positions:
        by_order[pos["order"]].append(pos)
    for order, ps in by_order.items():
        if len(ps) > 1:
            warnings.append(f"Multiple stage definitions for order {order}; only an exact ID match can resolve it.")
    for f in frames:
        exact = [p for p in positions if f.roi.casefold() == f"ROI-{p['order']}{p['id']}".casefold()]
        if len(exact) == 1:
            f.order, f.id = exact[0]["order"], exact[0]["id"]
    _order_by_capture_time([f for f in frames if not f.roi.upper().startswith("ROI-")])
    frames.sort(key=lambda f: (f.order, f.roi, list(CHANNELS).index(f.channel), f.serial))
    mode = "fixed" if max(f.serial for f in frames) == 0 else "timelapse"
    if any(Path(f.path).parent.name.upper() in CHANNELS for f in frames):
        layout = "nested channel folders"
    elif all(Path(f.path).parent == root for f in frames):
        layout = "flat + descriptor" if any(f.descriptor for f in frames) else "flat"
    elif all(f.roi.upper().startswith("ROI-") for f in frames):
        layout = "ROI subfolders"
    else:
        layout = "position subfolders"
    ds = Dataset(str(root), layout, mode, frames, protocols, avs, positions, warnings)
    for roi, channels in ds.groups.items():
        first = next(iter(channels.values()))[0]
        pos = position_for(ds, first)
        if pos and pos["id"] != first.id:
            warnings.append(f"{roi}: filename ID '{first.id}' differs from protocol ID '{pos['id']}' at order {first.order}; stage assigned by order.")
        elif not pos:
            warnings.append(f"{roi}: no unambiguous stage position.")
        missing = set(ds.channels) - channels.keys()
        if missing:
            warnings.append(f"{roi}: missing channels {', '.join(sorted(missing))}.")
        for ch, fs in channels.items():
            serials = [f.serial for f in fs]
            if serials != list(range(0, max(serials) + 1)):
                warnings.append(f"{roi}/{ch}: non-contiguous serials or nonzero start; videos contain available captures only.")
            av = next((a for a in avs if a["roi"] == roi and a["channel"] == ch), None)
            if av and av["n_frames"] != len(fs):
                warnings.append(f"{roi}/{ch}: {len(fs)} TIFFs; AVS describes {av['n_frames']}.")
            expected = ds.protocol.get("expected_frames")
            if mode == "timelapse" and expected and len(fs) < expected:
                warnings.append(f"{roi}/{ch}: {len(fs)} captured vs {expected} planned; early stop or partial copy.")
    captured_orders = {f.order for f in frames}
    for p in positions:
        if p["order"] not in captured_orders:
            warnings.append(f"Defined position {p['order']}/{p['id']} has no captured TIFFs.")
    if not ds.interval:
        warnings.append("Capture interval unavailable; elapsed times are unknown until an interval is supplied.")
    if mode == "fixed" and ds.protocol.get("expected_frames", 0):
        warnings.append("Single-image routing follows actual TIFFs; protocol timing may be inherited from another run.")
    ds.warnings = list(dict.fromkeys(warnings))
    ds.source_fingerprint = current_inventory_fingerprint(ds)
    return ds


def position_for(ds: Dataset, f: Frame) -> dict | None:
    exact = [p for p in ds.positions if p["order"] == f.order and p["id"] == f.id]
    if not f.roi.upper().startswith("ROI-"):  # labware names: the protocol's order says nothing about them
        return exact[0] if len(exact) == 1 else None
    candidates = exact or [p for p in ds.positions if p["order"] == f.order]
    return candidates[0] if len(candidates) == 1 else None


def inventory(ds: Dataset) -> list[dict]:
    """One row per position x channel: counts, serial range, AVS cross-check, stage coordinates."""
    rows = []
    for roi, channels in ds.groups.items():
        for ch, fs in channels.items():
            f, p = fs[0], position_for(ds, fs[0]) or {}
            av = next((a for a in ds.avs if a["roi"] == roi and a["channel"] == ch), {})
            rows.append({"ROI": roi, "ID": f.id, "order": f.order, "protocol_ID": p.get("id"),
                         "descriptor": f.descriptor, "channel": ch, "n_frames": len(fs),
                         "first_serial": fs[0].serial, "last_serial": fs[-1].serial,
                         "avs_frames": av.get("n_frames"), "planned_frames": ds.protocol.get("expected_frames"),
                         **{k: p.get(k) for k in ("x", "y", "z")}})
    return rows


def summary(ds: Dataset) -> dict:
    """JSON-serialisable description of the experiment (the CLI ``--inspect`` payload)."""
    return {"root": ds.root, "name": ds.name, "layout": ds.layout, "mode": ds.mode, "n_rois": len(ds.groups),
            "n_images": len(ds.frames), "channels": ds.channels, "interval_seconds": ds.interval,
            "n_timepoints": ds.n_timepoints, "inventory": inventory(ds), "warnings": ds.warnings,
            "objective_metadata": ds.objective_metadata}


# --------------------------------------------------------------------------- #
# Pixels
# --------------------------------------------------------------------------- #


def _read_array(path: Path | str) -> np.ndarray:
    with tifffile.TiffFile(path) as tif:
        if len(tif.pages) != 1:
            raise ValueError(f"Multi-page TIFF needs an explicit stack interpretation: {path}")
        a = tif.asarray()
    if a.ndim == 3 and a.shape[0] in (3, 4) and a.shape[-1] not in (3, 4):
        a = np.moveaxis(a, 0, -1)
    return a


def read_plane(path: Path | str, channel: str) -> np.ndarray:
    """The uint8 HxW plane that carries ``channel``'s signal (models.CHANNEL_PLANE)."""
    a = _read_array(path)
    if a.ndim == 3 and a.shape[-1] in (3, 4):
        a = a[..., CHANNELS[channel]["plane"]]
    if a.ndim != 2 or a.dtype != np.uint8:
        raise ValueError(f"Expected an 8-bit grayscale or RGB capture; got {a.shape}/{a.dtype}: {path}. "
                         "Convert higher-bit images with an explicit calibrated policy first.")
    return np.ascontiguousarray(a)


def read_rgb(path: Path | str) -> np.ndarray:
    """The whole capture as uint8 HxWx3 (grayscale is stacked; RGBA loses alpha).

    Used by the calibration reader, which needs the burned-in yellow overlay across all planes.
    """
    a = _read_array(path)
    if a.dtype != np.uint8:
        raise ValueError(f"Expected an 8-bit capture; got {a.shape}/{a.dtype}: {path}. "
                         "Convert higher-bit images with an explicit calibrated policy first.")
    if a.ndim == 2:
        a = np.repeat(a[..., None], 3, axis=2)
    elif a.ndim == 3 and a.shape[-1] >= 3:
        a = a[..., :3]
    else:
        raise ValueError(f"Expected a grayscale or RGB capture; got {a.shape}: {path}")
    return np.ascontiguousarray(a)
