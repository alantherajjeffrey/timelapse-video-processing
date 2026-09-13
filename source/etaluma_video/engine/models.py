"""Shared data contracts for the v0.4 engine and UI.

Everything the UI and the engine exchange is defined here so that the engine
modules (parsing, histograms, display, calibration, export) and the Qt widgets
can be built in parallel against one vocabulary. Keep this module free of Qt,
cv2 and file I/O beyond JSON.

Decisions recorded in docs/plans/v0.4_plan.md section 1 are encoded as defaults.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Iterable

import numpy as np

# --------------------------------------------------------------------------- #
# Channels, colours, objectives
# --------------------------------------------------------------------------- #

CHANNEL_ORDER: tuple[str, ...] = ("WHITE", "F1", "F2", "F3")
FLUOR_CHANNELS: tuple[str, ...] = ("F1", "F2", "F3")

#: Index of the RGB plane that carries each channel's signal in an LS720 TIFF.
CHANNEL_PLANE: dict[str, int] = {"WHITE": 0, "F1": 2, "F2": 1, "F3": 0}

#: Colour presets (decision 13). Values are RGB multipliers in [0, 1].
COLOUR_PRESETS: dict[str, dict[str, tuple[float, float, float]]] = {
    "CGM": {"WHITE": (1.0, 1.0, 1.0), "F1": (0.0, 1.0, 1.0), "F2": (0.0, 1.0, 0.0), "F3": (1.0, 0.0, 1.0)},
    "RGB": {"WHITE": (1.0, 1.0, 1.0), "F1": (0.0, 0.0, 1.0), "F2": (0.0, 1.0, 0.0), "F3": (1.0, 0.0, 0.0)},
}
DEFAULT_COLOUR_PRESET = "CGM"

#: LS720 objective table, µm per pixel at 1900 px frames (docs/objective_lens_selection_values.md).
OBJECTIVES: dict[str, float] = {"4x": 2.068, "10x": 0.826, "20x": 0.411, "40x": 0.205}

#: Relative disagreement between the burned-in bar and the table above which the UI warns (decision 21).
CALIBRATION_DISAGREEMENT = 0.05

AUTO_METHODS: tuple[str, ...] = ("adaptive", "classic", "cut")
BLEND_MODES: tuple[str, ...] = ("screen", "additive", "max")
WHITE_PRESETS: dict[str, dict[str, float]] = {
    "brightfield": {"gamma": 2.0, "weight": 0.5},
    "phase": {"gamma": 1.0, "weight": 0.6},
}
#: Median of the WHITE plane above which the brightfield preset is chosen (decision 15).
WHITE_BRIGHTFIELD_MEDIAN = 128


# --------------------------------------------------------------------------- #
# Display profile (decision 12, section 4.2 of the plan)
# --------------------------------------------------------------------------- #


@dataclass
class ChannelDisplay:
    """Manual display settings of one fluorescence channel."""

    enabled: bool = True
    colour: tuple[float, float, float] = (1.0, 1.0, 1.0)
    low: float = 0.0
    high: float = 255.0
    gamma: float = 1.0


@dataclass
class WhiteDisplay:
    """Display settings of the WHITE (transmitted light) channel."""

    enabled: bool = True
    preset: str = "phase"  # "brightfield" | "phase"
    preset_auto: bool = True  # True: re-detected from the median for each experiment
    weight: float = 0.6  # underlay weight in the composite with WHITE
    gamma: float = 1.0  # underlay gamma
    low: float = 0.0  # standalone WHITE video stretch bounds (absolute 8-bit values)
    high: float = 255.0


@dataclass
class RollingBall:
    """Rolling-ball background subtraction for fluorescence channels (decision 16)."""

    enabled: bool = False
    radius_px: int = 50


@dataclass
class DisplayProfile:
    """Everything that decides how planes become RGB frames. Saved as display_profile.json."""

    schema: int = 1
    mode: str = "auto"  # "auto" | "manual"
    auto_method: str = "adaptive"  # see AUTO_METHODS
    colour_preset: str = DEFAULT_COLOUR_PRESET  # "CGM" | "RGB" | "custom"
    channels: dict[str, ChannelDisplay] = field(default_factory=dict)
    white: WhiteDisplay = field(default_factory=WhiteDisplay)
    blend: str = "screen"  # see BLEND_MODES
    rolling_ball: RollingBall = field(default_factory=RollingBall)

    # ---- construction -------------------------------------------------- #
    @classmethod
    def default_for(cls, channels: Iterable[str], colour_preset: str = DEFAULT_COLOUR_PRESET) -> "DisplayProfile":
        """Profile with one ChannelDisplay per fluorescence channel present, colours from the preset."""
        preset = COLOUR_PRESETS[colour_preset]
        p = cls(colour_preset=colour_preset)
        for ch in channels:
            if ch in FLUOR_CHANNELS:
                p.channels[ch] = ChannelDisplay(colour=preset[ch])
        return p

    def apply_colour_preset(self, name: str) -> None:
        preset = COLOUR_PRESETS[name]
        self.colour_preset = name
        for ch, cd in self.channels.items():
            cd.colour = preset[ch]

    def fluorescence_channels(self) -> list[str]:
        return [ch for ch in FLUOR_CHANNELS if ch in self.channels]

    def enabled_fluorescence(self) -> list[str]:
        return [ch for ch in self.fluorescence_channels() if self.channels[ch].enabled]

    # ---- serialisation -------------------------------------------------- #
    def to_dict(self) -> dict:
        d = asdict(self)
        for cd in d["channels"].values():
            cd["colour"] = [float(x) for x in cd["colour"]]
            for k in ("low", "high", "gamma"):
                cd[k] = float(cd[k])
        for k in ("weight", "gamma", "low", "high"):
            d["white"][k] = float(d["white"][k])
        d["rolling_ball"]["radius_px"] = int(d["rolling_ball"]["radius_px"])
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "DisplayProfile":
        p = cls(
            schema=int(d.get("schema", 1)),
            mode=d.get("mode", "auto"),
            auto_method=d.get("auto_method", "adaptive"),
            colour_preset=d.get("colour_preset", DEFAULT_COLOUR_PRESET),
            blend=d.get("blend", "screen"),
        )
        for ch, cd in (d.get("channels") or {}).items():
            p.channels[ch] = ChannelDisplay(
                enabled=bool(cd.get("enabled", True)),
                colour=tuple(float(x) for x in cd.get("colour", (1.0, 1.0, 1.0))),
                low=float(cd.get("low", 0.0)),
                high=float(cd.get("high", 255.0)),
                gamma=float(cd.get("gamma", 1.0)),
            )
        w = d.get("white") or {}
        p.white = WhiteDisplay(
            enabled=bool(w.get("enabled", True)),
            preset=w.get("preset", "phase"),
            preset_auto=bool(w.get("preset_auto", True)),
            weight=float(w.get("weight", 0.6)),
            gamma=float(w.get("gamma", 1.0)),
            low=float(w.get("low", 0.0)),
            high=float(w.get("high", 255.0)),
        )
        rb = d.get("rolling_ball") or {}
        p.rolling_ball = RollingBall(enabled=bool(rb.get("enabled", False)), radius_px=int(rb.get("radius_px", 50)))
        return p

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), indent=2)

    @classmethod
    def from_json(cls, text: str) -> "DisplayProfile":
        return cls.from_dict(json.loads(text))

    def save(self, path: Path | str) -> None:
        Path(path).write_text(self.to_json(), encoding="utf-8")

    @classmethod
    def load(cls, path: Path | str) -> "DisplayProfile":
        return cls.from_json(Path(path).read_text(encoding="utf-8"))

    def signature(self) -> str:
        """Stable hash of every rendering-relevant field (cache keys, metadata)."""
        return hashlib.sha256(json.dumps(self.to_dict(), sort_keys=True).encode("utf-8")).hexdigest()[:16]

    def copy(self) -> "DisplayProfile":
        return DisplayProfile.from_dict(self.to_dict())


#: Effective display bounds per channel: {channel: (low, high)} in absolute 8-bit units.
Bounds = dict[str, tuple[float, float]]


# --------------------------------------------------------------------------- #
# Overlays and calibration (section 4.4 of the plan)
# --------------------------------------------------------------------------- #


@dataclass
class Box:
    """Half-open pixel box [y0, y1) x [x0, x1)."""

    y0: int
    y1: int
    x0: int
    x1: int

    def slices(self) -> tuple[slice, slice]:
        return slice(self.y0, self.y1), slice(self.x0, self.x1)

    @property
    def height(self) -> int:
        return self.y1 - self.y0

    @property
    def width(self) -> int:
        return self.x1 - self.x0

    def to_list(self) -> list[int]:
        return [self.y0, self.y1, self.x0, self.x1]

    @classmethod
    def from_list(cls, v) -> "Box | None":
        return None if v is None else cls(*[int(x) for x in v])


@dataclass
class Overlays:
    """Lumaview overlays burned into a capture (timestamp box bottom-left, scale bar bottom-right)."""

    image_shape: tuple[int, int] | None = None  # (height, width)
    timestamp_box: Box | None = None
    bar_box: Box | None = None
    label_box: Box | None = None
    bar_len_px: int | None = None
    pad_px: int = 2

    @property
    def detected(self) -> bool:
        return self.timestamp_box is not None or self.bar_box is not None

    def boxes(self) -> list[Box]:
        return [b for b in (self.timestamp_box, self.bar_box, self.label_box) if b is not None]

    def mask(self, shape: tuple[int, int] | None = None) -> np.ndarray:
        """Boolean mask, True on overlay pixels (each box padded by pad_px). Excluded from histograms."""
        shape = shape or self.image_shape
        m = np.zeros(shape, dtype=bool)
        h, w = shape
        for b in self.boxes():
            m[max(0, b.y0 - self.pad_px) : min(h, b.y1 + self.pad_px), max(0, b.x0 - self.pad_px) : min(w, b.x1 + self.pad_px)] = True
        return m

    def exclude_bottom_px(self) -> int:
        """Rows from the bottom that contain any overlay (measurement exclusion)."""
        if not self.detected or self.image_shape is None:
            return 0
        top = min(b.y0 for b in self.boxes())
        return max(0, self.image_shape[0] - top + self.pad_px)

    def to_dict(self) -> dict:
        return {
            "image_shape": list(self.image_shape) if self.image_shape else None,
            "timestamp_box": self.timestamp_box.to_list() if self.timestamp_box else None,
            "bar_box": self.bar_box.to_list() if self.bar_box else None,
            "label_box": self.label_box.to_list() if self.label_box else None,
            "bar_len_px": self.bar_len_px,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "Overlays":
        return cls(
            image_shape=tuple(d["image_shape"]) if d.get("image_shape") else None,
            timestamp_box=Box.from_list(d.get("timestamp_box")),
            bar_box=Box.from_list(d.get("bar_box")),
            label_box=Box.from_list(d.get("label_box")),
            bar_len_px=d.get("bar_len_px"),
        )


@dataclass
class Calibration:
    """Pixel size and objective for one experiment, and where they came from. Saved as calibration.json."""

    pixel_size_um: float | None = None
    objective: str | None = None  # "4x" | "10x" | "20x" | "40x" | None
    source: str = "none"  # "burned-in scale bar" | "manual" | "table" | "none"
    bar_px: int | None = None
    label_um: float | None = None
    label_objective: str | None = None
    table_pixel_size_um: float | None = None
    disagreement: bool = False  # |bar / table - 1| > CALIBRATION_DISAGREEMENT
    confidence: float = 0.0  # glyph-reader confidence in [0, 1]
    message: str = ""  # human-readable summary for the calibration card and the log
    source_frame: str | None = None  # path of the frame the bar was read from

    @property
    def usable(self) -> bool:
        return self.pixel_size_um is not None and self.pixel_size_um > 0

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "Calibration":
        return cls(**{k: d.get(k) for k in cls.__dataclass_fields__ if k in d})

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), indent=2)

    @classmethod
    def manual(cls, objective: str) -> "Calibration":
        return cls(pixel_size_um=OBJECTIVES[objective], objective=objective, source="manual",
                   table_pixel_size_um=OBJECTIVES[objective], confidence=1.0,
                   message=f"{objective} chosen manually ({OBJECTIVES[objective]:.3f} µm/px)")


# --------------------------------------------------------------------------- #
# Histograms (section 4.5 of the plan)
# --------------------------------------------------------------------------- #


def percentile_from_counts(counts: np.ndarray, p: float) -> float:
    """Interpolated percentile of an 8-bit histogram, identical to Codex 0.3's rule."""
    counts = np.asarray(counts, dtype=np.int64)
    total = int(counts.sum())
    if total <= 0:
        return 0.0
    cumulative = np.cumsum(counts)
    rank = p / 100.0 * (total - 1)
    lower, upper = int(np.floor(rank)), int(np.ceil(rank))
    a = int(np.searchsorted(cumulative, lower + 1))
    b = int(np.searchsorted(cumulative, upper + 1))
    return float(a + (b - a) * (rank - lower))


@dataclass
class ChannelHistogram:
    """256-bin histogram of one channel over every covered frame, overlay pixels excluded."""

    counts: np.ndarray = field(default_factory=lambda: np.zeros(256, dtype=np.int64))
    frame_p995_max: float = 0.0  # running max over frames of the per-frame 99.5th percentile (Classic)
    frames: int = 0

    def add_frame(self, plane: np.ndarray, mask: np.ndarray | None = None) -> None:
        values = plane[~mask] if mask is not None else plane.ravel()
        if values.size == 0:
            return
        self.counts += np.bincount(values.ravel(), minlength=256)[:256]
        self.frame_p995_max = max(self.frame_p995_max, float(np.percentile(values, 99.5)))
        self.frames += 1

    def merge(self, other: "ChannelHistogram") -> "ChannelHistogram":
        return ChannelHistogram(self.counts + other.counts, max(self.frame_p995_max, other.frame_p995_max), self.frames + other.frames)

    def percentile(self, p: float) -> float:
        return percentile_from_counts(self.counts, p)

    @property
    def pixels(self) -> int:
        return int(self.counts.sum())

    def mode(self) -> int:
        return int(np.argmax(self.counts))


@dataclass
class HistogramSet:
    """Histograms of every channel of one experiment, possibly covering only some positions yet."""

    channels: dict[str, ChannelHistogram] = field(default_factory=dict)
    positions: list[str] = field(default_factory=list)  # positions covered so far
    total_positions: int = 0
    rolling_ball_radius: int | None = None  # None: computed on raw planes
    fingerprint: str = ""  # dataset source fingerprint the histograms belong to

    @property
    def complete(self) -> bool:
        return self.total_positions > 0 and len(self.positions) >= self.total_positions

    def merge(self, other: "HistogramSet") -> "HistogramSet":
        out = HistogramSet(total_positions=max(self.total_positions, other.total_positions),
                           rolling_ball_radius=self.rolling_ball_radius, fingerprint=self.fingerprint)
        for ch in set(self.channels) | set(other.channels):
            a, b = self.channels.get(ch), other.channels.get(ch)
            out.channels[ch] = a.merge(b) if a and b else (a or b)
        out.positions = sorted(set(self.positions) | set(other.positions))
        return out

    def save(self, path: Path | str) -> None:
        arrays = {f"counts_{ch}": h.counts for ch, h in self.channels.items()}
        meta = {ch: {"frame_p995_max": h.frame_p995_max, "frames": h.frames} for ch, h in self.channels.items()}
        np.savez(path, meta=json.dumps({"channels": meta, "positions": self.positions, "total_positions": self.total_positions,
                                        "rolling_ball_radius": self.rolling_ball_radius, "fingerprint": self.fingerprint}), **arrays)

    @classmethod
    def load(cls, path: Path | str) -> "HistogramSet":
        with np.load(path) as z:
            meta = json.loads(str(z["meta"]))
            hs = cls(positions=list(meta["positions"]), total_positions=int(meta["total_positions"]),
                     rolling_ball_radius=meta.get("rolling_ball_radius"), fingerprint=meta.get("fingerprint", ""))
            for ch, m in meta["channels"].items():
                hs.channels[ch] = ChannelHistogram(np.array(z[f"counts_{ch}"], dtype=np.int64), float(m["frame_p995_max"]), int(m["frames"]))
        return hs
