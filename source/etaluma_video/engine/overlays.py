"""Text burned into exported videos and images: elapsed time, experiment and position name, scale bar.

Decisions 6-10 of the 0.6 plan:

* time = start offset + serial x capture interval, written ``Day d : hh:mm:ss``; d counts completed
  days (Day 0 at the start, 26 h -> ``Day 1 : 02:00:00``). Days are shown when the video reaches
  24 h (``auto``), always, or never (``26:00:00``). The choice is made once per experiment so the
  label never changes format halfway through a video;
* the time box covers Lumaview's own clock box (bottom left) at a size that stays readable at any
  output width; the name label sits to the right of it at the same size, never on top of either;
* the app scale bar covers Lumaview's bar and label (bottom right) with a readable bar of the same
  length Lumaview used, when its label was read.

The name label and the scale bar are drawn once into small patches, the time once per distinct
text, and the patches are copied into each frame, so a 1900 px frame pays well under a millisecond.
"""
from __future__ import annotations

import math
import re
import threading

import numpy as np
from PIL import Image, ImageDraw

from .models import Box, Overlays

__all__ = ["DAY", "DAYS_MODES", "show_days", "format_elapsed", "parse_elapsed", "OverlayPainter"]

DAY = 86400.0
DAYS_MODES = ("auto", "always", "never")
BOX_RGB = (0, 0, 0)
TEXT_RGB = (255, 255, 255)


def show_days(mode: str, last_seconds: float) -> bool:
    """Whether the day counter is shown for a video whose last label is ``last_seconds``."""
    if mode == "always":
        return True
    if mode == "never":
        return False
    return abs(float(last_seconds)) >= DAY


def format_elapsed(seconds: float, days: bool) -> str:
    """``Day 1 : 02:00:00`` (days=True) or ``26:00:00`` (days=False); negative offsets keep a sign."""
    sign = "-" if seconds < 0 else ""
    total = int(round(abs(float(seconds))))
    if days:
        d, rest = divmod(total, 86400)
        h, rest = divmod(rest, 3600)
        m, s = divmod(rest, 60)
        return f"{sign}Day {d} : {h:02d}:{m:02d}:{s:02d}"
    h, rest = divmod(total, 3600)
    m, s = divmod(rest, 60)
    return f"{sign}{h:02d}:{m:02d}:{s:02d}"


_DAY_PREFIX = re.compile(r"^(?:day\s*(\d+)|(\d+)\s*(?:d|days?))\s*:?\s*(.*)$")
_NUMBER = re.compile(r"\d+(?:\.\d+)?")


def parse_elapsed(text: str | None) -> float:
    """Seconds from ``Day 2 : 06:00:00``, ``2d 06:00``, ``06:00:00``, ``6:30`` or ``36`` (hours).

    Empty text is 0. A leading ``-`` makes the offset negative. Raises ValueError with a sentence.
    """
    s = (text or "").strip().lower()
    if not s:
        return 0.0
    sign = -1.0 if s.startswith("-") else 1.0
    s = s.lstrip("+-").strip()
    days = 0
    prefix = _DAY_PREFIX.match(s)
    if prefix:
        days = int(prefix.group(1) or prefix.group(2))
        s = prefix.group(3).strip()
    if not s:
        return sign * days * DAY
    if _NUMBER.fullmatch(s):
        return sign * (days * DAY + float(s) * 3600.0)
    parts = [p.strip() for p in s.split(":")]
    if len(parts) not in (2, 3) or not all(_NUMBER.fullmatch(p) for p in parts):
        raise ValueError("Write the start time as Day d : hh:mm:ss, hh:mm:ss or hh:mm.")
    hours, minutes = float(parts[0]), float(parts[1])
    seconds = float(parts[2]) if len(parts) == 3 else 0.0
    if minutes >= 60 or seconds >= 60:
        raise ValueError("Minutes and seconds in the start time must be below 60.")
    return sign * (days * DAY + hours * 3600.0 + minutes * 60.0 + seconds)


def _nice_length_um(width_px: int, pixel_size: float) -> float:
    """A 1-2-5 length of at most about 18 % of the frame width."""
    target = width_px * pixel_size * 0.18
    exponent = 10 ** math.floor(math.log10(target))
    return max(v * exponent for v in (1, 2, 5, 10) if v * exponent <= target)


def _scaled(box: Box | None, sx: float, sy: float, pad: int = 2) -> Box | None:
    if box is None:
        return None
    return Box(int(math.floor((box.y0 - pad) * sy)), int(math.ceil((box.y1 + pad) * sy)),
               int(math.floor((box.x0 - pad) * sx)), int(math.ceil((box.x1 + pad) * sx)))


def _paste(rgb: np.ndarray, x: int, y: int, patch: np.ndarray) -> None:
    h, w = rgb.shape[:2]
    ph, pw = min(patch.shape[0], h - y), min(patch.shape[1], w - x)
    if ph > 0 and pw > 0:
        rgb[y : y + ph, x : x + pw] = patch[:ph, :pw]


class OverlayPainter:
    """Draws the app's time, name and scale bar into frames of one output size.

    size, source_size: (width, height) of the output frame and of the capture it came from.
    overlays: Lumaview's burned-in boxes in capture coordinates (covered, never drawn over twice).
    time_template: the longest time text this painter will see (fixes the box width, so the
    box does not jitter as digits change).
    pixel_size: µm per *output* pixel; label_um: Lumaview's bar length when its label was read.
    """

    def __init__(self, size: tuple[int, int], source_size: tuple[int, int], overlays: Overlays | None = None, *,
                 time: bool = False, time_template: str = "Day 88 : 88:88:88", name: str = "",
                 scale_bar: bool = False, pixel_size: float | None = None, label_um: float | None = None) -> None:
        from .reports import font  # local import: reports is heavier and imports parsing

        self.size = (int(size[0]), int(size[1]))
        w, h = self.size
        sw, sh = source_size
        if overlays is not None and overlays.image_shape:
            sh, sw = overlays.image_shape
        sx, sy = w / float(sw), h / float(sh)
        self.font_px = max(13, round(w * 0.0125))
        self.font = font(self.font_px)
        self.pad = max(3, round(self.font_px * 0.4))
        self.box_h = round(self.font_px * 1.55)
        gap = max(4, round(self.font_px * 0.6))
        margin_x, margin_y = round(0.0275 * w), round(0.0145 * h)
        detected = overlays is not None and overlays.detected
        clock = _scaled(overlays.timestamp_box, sx, sy) if detected else None
        bar_region = None
        if detected and overlays.bar_box is not None:
            bar_region = _scaled(overlays.bar_box, sx, sy)
            label = _scaled(overlays.label_box, sx, sy)
            if label is not None:
                bar_region = Box(min(bar_region.y0, label.y0), max(bar_region.y1, label.y1),
                                 min(bar_region.x0, label.x0), max(bar_region.x1, label.x1))
        self.time_box: tuple[int, int, int, int] | None = None  # x0, y0, x1, y1
        self.patches: list[tuple[int, int, np.ndarray]] = []  # static patches: x, y, rgb
        self._time_cache: dict[str, np.ndarray] = {}
        self._key_cache: dict[tuple, np.ndarray] = {}
        self._lock = threading.Lock()
        self._margin = (round(0.0275 * w), round(0.0145 * h))

        # the bottom-left row: the time box covers Lumaview's clock when there is one
        template = re.sub(r"\d", "8", time_template)
        text_w = int(math.ceil(self.font.getlength(template))) + 2 * self.pad
        if clock is not None:
            bw, bh = max(clock.width, text_w), max(clock.height, self.box_h)
            x0, y1 = max(0, clock.x0), min(h, clock.y1)
            row = (x0, max(0, y1 - bh), min(w, x0 + bw), y1)
        else:
            row = (margin_x, h - margin_y - self.box_h, min(w, margin_x + text_w), h - margin_y)
        if time:
            self.time_box = row

        # scale bar: covers Lumaview's bar and label
        scale_left = w - margin_x
        if scale_bar and pixel_size:
            length_um = float(label_um) if label_um else _nice_length_um(w, pixel_size)
            bar_px = round(length_um / pixel_size)
            if not 4 <= bar_px <= 0.6 * w:
                length_um = _nice_length_um(w, pixel_size)
                bar_px = round(length_um / pixel_size)
            text = f"{length_um:g} µm"
            thick = max(2, round(self.font_px * 0.25))
            content_w = max(bar_px, int(math.ceil(self.font.getlength(text)))) + 2 * self.pad
            content_h = self.pad + self.box_h + thick + self.pad
            if bar_region is not None:
                right, bottom = min(w, bar_region.x1), min(h, bar_region.y1)
                bw, bh = max(content_w, bar_region.width), max(content_h, bar_region.height)
            else:
                right, bottom = w - margin_x, h - margin_y
                bw, bh = content_w, content_h
            patch = Image.new("RGB", (bw, bh), BOX_RGB)
            d = ImageDraw.Draw(patch)
            bx1 = bw - self.pad
            d.rectangle((bx1 - bar_px, bh - self.pad - thick, bx1 - 1, bh - self.pad - 1), fill=TEXT_RGB)
            d.text((bx1 - bar_px / 2, bh - self.pad - thick - self.box_h / 2), text, font=self.font,
                   fill=TEXT_RGB, anchor="mm")
            x0, y0 = max(0, right - bw), max(0, bottom - bh)
            self.patches.append((x0, y0, np.asarray(patch)))
            scale_left = x0
        elif bar_region is not None:
            scale_left = bar_region.x0

        # name label: right of the time box (or Lumaview's clock), same size, never overlapping
        if name:
            beside = time or clock is not None
            left = row[2] if time else margin_x
            if clock is not None:
                left = max(left, clock.x1)
            if beside:
                left += gap
            available = scale_left - gap - left
            label = name
            while label and self.font.getlength(label) + 2 * self.pad > available:
                label = label[:-2] + "…" if len(label) > 2 else ""
            if label and available >= 3 * self.font_px:
                lw = int(math.ceil(self.font.getlength(label))) + 2 * self.pad
                patch = Image.new("RGB", (lw, row[3] - row[1]), BOX_RGB)
                ImageDraw.Draw(patch).text((self.pad, (row[3] - row[1]) / 2), label, font=self.font,
                                           fill=TEXT_RGB, anchor="lm")
                self.patches.append((left, row[1], np.asarray(patch)))

    @property
    def active(self) -> bool:
        return self.time_box is not None or bool(self.patches)

    def _time_patch(self, text: str) -> np.ndarray:
        with self._lock:
            patch = self._time_cache.get(text)
            if patch is None:
                x0, y0, x1, y1 = self.time_box
                img = Image.new("RGB", (x1 - x0, y1 - y0), BOX_RGB)
                ImageDraw.Draw(img).text((self.pad, (y1 - y0) / 2), text, font=self.font, fill=TEXT_RGB, anchor="lm")
                patch = np.asarray(img)
                if len(self._time_cache) > 8:
                    self._time_cache.clear()
                self._time_cache[text] = patch
            return patch

    def _key_patch(self, entries: tuple) -> np.ndarray:
        """A colour square and a name per channel, on one black strip (0.7 channel key)."""
        with self._lock:
            patch = self._key_cache.get(entries)
            if patch is None:
                square = max(8, round(self.font_px * 0.75))
                gap = max(4, round(self.font_px * 0.45))
                widths = [square + gap + int(math.ceil(self.font.getlength(name))) for name, _colour in entries]
                width = self.pad * 2 + sum(widths) + gap * 2 * (len(entries) - 1)
                img = Image.new("RGB", (max(1, width), self.box_h), BOX_RGB)
                d = ImageDraw.Draw(img)
                x = self.pad
                for (name, colour), item_w in zip(entries, widths):
                    top = (self.box_h - square) // 2
                    d.rectangle((x, top, x + square - 1, top + square - 1),
                                fill=tuple(int(round(255 * float(c))) for c in colour))
                    d.text((x + square + gap, self.box_h / 2), name, font=self.font, fill=TEXT_RGB, anchor="lm")
                    x += item_w + gap * 2
                patch = np.asarray(img)
                self._key_cache[entries] = patch
            return patch

    def draw(self, rgb: np.ndarray, time_text: str | None = None, key: list | None = None) -> np.ndarray:
        """Copy the patches into ``rgb`` (in place when writable) and return it."""
        if not self.active and not key:
            return rgb
        if not rgb.flags.writeable:
            rgb = rgb.copy()
        for x, y, patch in self.patches:
            _paste(rgb, x, y, patch)
        if self.time_box is not None and time_text:
            _paste(rgb, self.time_box[0], self.time_box[1], self._time_patch(time_text))
        if key:
            _paste(rgb, self._margin[0], self._margin[1], self._key_patch(tuple((n, tuple(c)) for n, c in key)))
        return rgb
