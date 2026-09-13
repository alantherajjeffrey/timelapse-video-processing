"""Histogram strip with two draggable handles: the per-channel LUT control of the display panel.

Shows the channel's histogram over every loaded timepoint (filled) and the current frame's
histogram (outline), on a log or linear count scale, with intensity values along the x axis.
The shaded parts lie outside the display window. Drag the black point (low) or the white point
(high); dragging inside the window moves both. In Auto mode the widget is read-only and simply
shows the automatic bounds.
"""
from __future__ import annotations

import numpy as np
from PySide6.QtCore import QPointF, QRectF, QSize, Qt, Signal
from PySide6.QtGui import QColor, QFont, QPainter, QPainterPath, QPen, QPolygonF
from PySide6.QtWidgets import QSizePolicy, QWidget

HANDLE_GRAB_PX = 8
MIN_GAP = 1.0
AXIS_PX = 15
TICKS = (0, 50, 100, 150, 200, 255)
VIEWS = ("both", "stack", "frame")


def _curve(counts, log: bool) -> np.ndarray | None:
    if counts is None:
        return None
    c = np.asarray(counts, dtype=np.float64)[:256]
    if log:
        c = np.log1p(c)
    peak = float(c.max()) if c.size else 0.0
    return c / peak if peak > 0 else c


class HistogramWidget(QWidget):
    bounds_changed = Signal(float, float)  # live while dragging
    editing_finished = Signal(float, float)  # mouse released

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._stack_counts = None
        self._frame_counts = None
        self._log = True
        self._view = "both"
        self._low, self._high = 0.0, 255.0
        self._editable = True
        self._colour = QColor(210, 210, 210)
        self._markers: list[float] = []
        self._drag: str | None = None
        self._origin = (0.0, 0.0, 0.0)
        self.setMinimumHeight(40 + AXIS_PX)
        self.setMouseTracking(True)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.setToolTip("Filled: every loaded timepoint. Outline: the current frame.\n"
                        "Drag the handles to set the black and white points; drag inside to move both.")

    def sizeHint(self) -> QSize:  # noqa: N802 - Qt API
        return QSize(220, 46 + AXIS_PX)

    # ---- state ------------------------------------------------------------ #
    def set_histogram(self, counts) -> None:
        """Histogram over every loaded timepoint (the Auto-normalised bounds are computed from it)."""
        self._stack_counts = None if counts is None else np.asarray(counts)
        self.update()

    def set_frame_histogram(self, counts) -> None:
        """Histogram of the frame shown in the preview."""
        self._frame_counts = None if counts is None else np.asarray(counts)
        self.update()

    def set_log(self, log: bool) -> None:
        self._log = bool(log)
        self.update()

    def is_log(self) -> bool:
        return self._log

    def set_view(self, view: str) -> None:
        self._view = view if view in VIEWS else "both"
        self.update()

    def view(self) -> str:
        return self._view

    def set_bounds(self, low: float, high: float) -> None:
        low, high = float(low), float(high)
        low = min(max(low, 0.0), 255.0 - MIN_GAP)
        high = max(min(high, 255.0), low + MIN_GAP)
        if (low, high) != (self._low, self._high):
            self._low, self._high = low, high
            self.update()

    def bounds(self) -> tuple[float, float]:
        return self._low, self._high

    def set_editable(self, editable: bool) -> None:
        self._editable = bool(editable)
        self.setCursor(Qt.CursorShape.ArrowCursor)
        self.update()

    def set_colour(self, colour) -> None:
        self._colour = QColor(colour)
        self.update()

    def set_markers(self, values) -> None:
        self._markers = [float(v) for v in (values or [])]
        self.update()

    # ---- geometry ------------------------------------------------------------ #
    def _plot(self) -> QRectF:
        return QRectF(self.rect()).adjusted(6, 6, -6, -(3 + AXIS_PX))

    def _x(self, value: float) -> float:
        r = self._plot()
        return r.left() + value / 255.0 * r.width()

    def _value(self, x: float) -> float:
        r = self._plot()
        return float(np.clip((x - r.left()) / max(r.width(), 1.0) * 255.0, 0.0, 255.0))

    # ---- painting ----------------------------------------------------------------- #
    def _path(self, curve: np.ndarray, r: QRectF, closed: bool) -> QPainterPath:
        path = QPainterPath(QPointF(r.left(), r.bottom()) if closed else QPointF(self._x(0), r.bottom() - float(curve[0]) * r.height()))
        for i, v in enumerate(curve):
            path.lineTo(QPointF(self._x(i), r.bottom() - float(v) * r.height()))
        if closed:
            path.lineTo(QPointF(r.right(), r.bottom()))
            path.closeSubpath()
        return path

    def paintEvent(self, _event) -> None:  # noqa: N802 - Qt API
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        r = self._plot()
        palette = self.palette()
        p.fillRect(self.rect(), palette.color(palette.ColorRole.Base))
        stack = _curve(self._stack_counts, self._log) if self._view in ("both", "stack") else None
        frame = _curve(self._frame_counts, self._log) if self._view in ("both", "frame") else None
        if stack is not None:
            muted = QColor(self._colour)
            muted.setAlpha(140)
            p.fillPath(self._path(stack, r, True), muted)
        if frame is not None:
            line = QColor(self._colour).lighter(140)
            line.setAlpha(255)
            if stack is None:
                fill = QColor(self._colour)
                fill.setAlpha(110)
                p.fillPath(self._path(frame, r, True), fill)
            p.setPen(QPen(line, 1.3))
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawPath(self._path(frame, r, False))
        if stack is None and frame is None:
            p.setPen(palette.color(palette.ColorRole.PlaceholderText))
            p.drawText(r, Qt.AlignmentFlag.AlignCenter, "histogram appears when the preview is loaded")
        xl, xh = self._x(self._low), self._x(self._high)
        shade = QColor(0, 0, 0, 120 if palette.color(palette.ColorRole.Base).lightness() < 128 else 55)
        p.fillRect(QRectF(r.left(), r.top(), max(0.0, xl - r.left()), r.height()), shade)
        p.fillRect(QRectF(xh, r.top(), max(0.0, r.right() - xh), r.height()), shade)
        for v in self._markers:
            x = self._x(v)
            p.setPen(QPen(QColor(239, 159, 39), 1.2))
            p.drawLine(QPointF(x, r.bottom() - 5), QPointF(x, r.bottom()))
        handle = palette.color(palette.ColorRole.Text) if self._editable else palette.color(palette.ColorRole.PlaceholderText)
        pen = QPen(handle, 1.6)
        if not self._editable:
            pen.setStyle(Qt.PenStyle.DashLine)
        p.setPen(pen)
        for x in (xl, xh):
            p.drawLine(QPointF(x, r.top()), QPointF(x, r.bottom()))
        if self._editable:
            p.setBrush(handle)
            p.setPen(Qt.PenStyle.NoPen)
            for x in (xl, xh):
                p.drawPolygon(QPolygonF([QPointF(x - 4, r.top() - 5), QPointF(x + 4, r.top() - 5), QPointF(x, r.top())]))
        p.setPen(QPen(palette.color(palette.ColorRole.Mid), 1))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawRect(r)
        # x axis: intensity values
        font = QFont(self.font())
        font.setPointSizeF(max(6.5, font.pointSizeF() * 0.78))
        p.setFont(font)
        text = palette.color(palette.ColorRole.PlaceholderText)
        for v in TICKS:
            x = self._x(v)
            p.setPen(QPen(palette.color(palette.ColorRole.Mid), 1))
            p.drawLine(QPointF(x, r.bottom()), QPointF(x, r.bottom() + 3))
            p.setPen(text)
            box = QRectF(x - 16, r.bottom() + 3, 32, AXIS_PX - 2)
            align = Qt.AlignmentFlag.AlignHCenter
            if v == TICKS[0]:
                box.moveLeft(r.left() - 2)
                align = Qt.AlignmentFlag.AlignLeft
            elif v == TICKS[-1]:
                box.moveRight(r.right() + 2)
                align = Qt.AlignmentFlag.AlignRight
            p.drawText(box, align | Qt.AlignmentFlag.AlignTop, str(v))
        p.setPen(text)
        p.drawText(QRectF(r.left() + 3, r.top() + 1, 40, 12), Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop,
                   "log" if self._log else "lin")
        p.end()

    # ---- mouse ------------------------------------------------------------------------ #
    def mousePressEvent(self, event) -> None:  # noqa: N802 - Qt API
        if not self._editable or event.button() != Qt.MouseButton.LeftButton:
            return super().mousePressEvent(event)
        x = event.position().x()
        xl, xh = self._x(self._low), self._x(self._high)
        dl, dh = abs(x - xl), abs(x - xh)
        if min(dl, dh) <= HANDLE_GRAB_PX:
            self._drag = "low" if dl <= dh else "high"
        elif xl < x < xh:
            self._drag = "window"
        else:
            self._drag = "low" if x < xl else "high"
            self._move_handle(self._drag, self._value(x))
        self._origin = (x, self._low, self._high)

    def mouseMoveEvent(self, event) -> None:  # noqa: N802 - Qt API
        x = event.position().x()
        if self._drag is None:
            if self._editable:
                near = min(abs(x - self._x(self._low)), abs(x - self._x(self._high))) <= HANDLE_GRAB_PX
                self.setCursor(Qt.CursorShape.SizeHorCursor if near else Qt.CursorShape.ArrowCursor)
            return
        if self._drag == "window":
            x0, low0, high0 = self._origin
            delta = self._value(x) - self._value(x0)
            delta = float(np.clip(delta, -low0, 255.0 - high0))
            self._set_and_emit(low0 + delta, high0 + delta)
        else:
            self._move_handle(self._drag, self._value(x))

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802 - Qt API
        if self._drag is not None:
            self._drag = None
            self.editing_finished.emit(self._low, self._high)
        super().mouseReleaseEvent(event)

    def _move_handle(self, which: str, value: float) -> None:
        if which == "low":
            self._set_and_emit(min(value, self._high - MIN_GAP), self._high)
        else:
            self._set_and_emit(self._low, max(value, self._low + MIN_GAP))

    def _set_and_emit(self, low: float, high: float) -> None:
        low, high = round(float(low), 1), round(float(high), 1)
        if (low, high) == (self._low, self._high):
            return
        self._low, self._high = low, high
        self.update()
        self.bounds_changed.emit(low, high)
