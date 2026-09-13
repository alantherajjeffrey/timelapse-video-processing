"""Live viewer: the central preview of the main window.

Renders the current timepoint of the current position from the preview cache through
``engine.display`` (the same functions the export uses), with view chips (composite,
fluorescence only, each channel), a timeline with play/pause, zoom and pan, and a readout
of the raw channel values under the cursor. Re-renders on every profile change, so the
display panel's handles update the image live.
"""
from __future__ import annotations

import logging
import time

import numpy as np
from PySide6.QtCore import QRectF, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QImage, QPainter, QPixmap
from PySide6.QtWidgets import (
    QButtonGroup,
    QGraphicsPixmapItem,
    QGraphicsScene,
    QGraphicsView,
    QHBoxLayout,
    QLabel,
    QSizePolicy,
    QSlider,
    QStyle,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from ..engine import display
from ..engine.models import CHANNEL_ORDER, FLUOR_CHANNELS
from ..engine.overlays import format_elapsed as format_time, show_days
from .preview_cache import PreviewController, PreviewStack

log = logging.getLogger("etaluma.ui")

VIEW_COMPOSITE = "composite"
VIEW_FLUOR = "fluorescence_only"
VIEW_LABELS = {VIEW_COMPOSITE: "Composite", VIEW_FLUOR: "Fluorescence only"}

#: Checkable chips (view selector, Auto | Manual): the checked one must be unmistakable in both themes.
CHIP_QSS = (
    "QToolButton { padding: 3px 10px; border: 1px solid palette(mid); border-radius: 10px; }"
    "QToolButton:checked { background: palette(highlight); border-color: palette(highlight); color: palette(highlighted-text); }"
    "QToolButton:hover:!checked { border-color: palette(highlight); }"
)


def rgb_to_qimage(rgb: np.ndarray) -> QImage:
    """HxWx3 uint8 RGB -> QImage that owns its pixels."""
    arr = np.ascontiguousarray(rgb, dtype=np.uint8)
    if arr.ndim == 2:
        arr = np.ascontiguousarray(np.repeat(arr[..., None], 3, axis=2))
    h, w = arr.shape[:2]
    return QImage(arr.data, w, h, 3 * w, QImage.Format.Format_RGB888).copy()


def format_elapsed(seconds: float) -> str:
    if seconds >= 3600:
        return f"{seconds / 3600:.1f} h"
    if seconds >= 60:
        return f"{seconds / 60:.0f} min"
    return f"{seconds:.0f} s"


class ImageView(QGraphicsView):
    """Zoomable, pannable image. Double-click fits; the wheel zooms under the cursor."""

    hovered = Signal(int, int)  # image pixel under the cursor, (-1, -1) outside

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._scene = QGraphicsScene(self)
        self.setScene(self._scene)
        self._item = QGraphicsPixmapItem()
        self._item.setTransformationMode(Qt.TransformationMode.SmoothTransformation)
        self._scene.addItem(self._item)
        self._message = self._scene.addSimpleText("")
        self._message.setBrush(QColor(170, 170, 170))
        self._fit = True
        self._size = (0, 0)
        self.setBackgroundBrush(QColor(14, 14, 16))
        self.setDragMode(QGraphicsView.DragMode.ScrollHandDrag)
        self.setTransformationAnchor(QGraphicsView.ViewportAnchor.AnchorUnderMouse)
        self.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.setMouseTracking(True)
        self.setMinimumSize(320, 240)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)

    def has_image(self) -> bool:
        return not self._item.pixmap().isNull()

    def set_image(self, image: QImage) -> None:
        self._message.setText("")
        self._item.setPixmap(QPixmap.fromImage(image))
        size = (image.width(), image.height())
        if size != self._size:
            self._size = size
            self._scene.setSceneRect(QRectF(0, 0, image.width(), image.height()))
            if self._fit:
                self.fit()

    def show_message(self, text: str) -> None:
        self._item.setPixmap(QPixmap())
        self._size = (0, 0)
        self._message.setText(text)
        self._scene.setSceneRect(self._message.boundingRect())
        self.resetTransform()
        self.centerOn(self._message)

    def fit(self) -> None:
        self._fit = True
        if self.has_image():
            self.fitInView(self._item, Qt.AspectRatioMode.KeepAspectRatio)

    def one_to_one(self) -> None:
        self._fit = False
        self.resetTransform()

    def wheelEvent(self, event) -> None:  # noqa: N802 - Qt API
        if not self.has_image():
            return
        factor = 1.25 ** (event.angleDelta().y() / 120.0)
        self._fit = False
        self.scale(factor, factor)

    def mouseDoubleClickEvent(self, event) -> None:  # noqa: N802 - Qt API
        self.fit()
        super().mouseDoubleClickEvent(event)

    def resizeEvent(self, event) -> None:  # noqa: N802 - Qt API
        super().resizeEvent(event)
        if self._fit:
            self.fit()

    def mouseMoveEvent(self, event) -> None:  # noqa: N802 - Qt API
        super().mouseMoveEvent(event)
        if not self.has_image():
            return
        pt = self._item.mapFromScene(self.mapToScene(event.position().toPoint()))
        x, y = int(pt.x()), int(pt.y())
        w, h = self._size
        self.hovered.emit(x, y) if 0 <= x < w and 0 <= y < h else self.hovered.emit(-1, -1)

    def leaveEvent(self, event) -> None:  # noqa: N802 - Qt API
        self.hovered.emit(-1, -1)
        super().leaveEvent(event)


class ViewerWidget(QWidget):
    """Central live preview: ``ViewerWidget(ctx, parent=None)``."""

    rendered = Signal(float)  # milliseconds of the last render (tests, diagnostics)

    def __init__(self, ctx, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.ctx = ctx
        self._view_mode = VIEW_COMPOSITE
        self._stack: PreviewStack | None = None
        self._last_rgb: np.ndarray | None = None
        self._last_log = 0.0
        self._log_next_render = False
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)

        # ---- top bar ---------------------------------------------------------- #
        self.title = QLabel("No experiment open")
        self.title.setProperty("role", "heading")
        self.view_bar = QWidget()
        self.view_layout = QHBoxLayout(self.view_bar)
        self.view_layout.setContentsMargins(0, 0, 0, 0)
        self.view_layout.setSpacing(4)
        self.view_group = QButtonGroup(self)
        self.view_group.setExclusive(True)
        self.view_group.idClicked.connect(self._on_view_clicked)
        self._view_ids: dict[int, str] = {}
        self.fit_button = QToolButton(text="Fit")
        self.fit_button.setToolTip("Fit the image to the window (double-click the image)")
        self.one_button = QToolButton(text="1:1")
        self.one_button.setToolTip("Show preview pixels at 100 %")
        top = QHBoxLayout()
        top.setContentsMargins(0, 0, 0, 0)
        top.addWidget(self.title)
        top.addSpacing(12)
        top.addWidget(self.view_bar)
        top.addStretch(1)
        top.addWidget(self.fit_button)
        top.addWidget(self.one_button)

        # ---- image ---------------------------------------------------------------- #
        self.view = ImageView(self)
        self.view.show_message("Open an experiment folder to see a live preview")
        self.fit_button.clicked.connect(self.view.fit)
        self.one_button.clicked.connect(self.view.one_to_one)
        self.view.hovered.connect(self._on_hover)

        # ---- timeline --------------------------------------------------------------- #
        self.play_button = QToolButton()
        self.play_button.setIcon(self.style().standardIcon(QStyle.StandardPixmap.SP_MediaPlay))
        self.play_button.setToolTip("Play or pause the timeline (space)")
        self.play_button.setCheckable(True)
        self.play_button.toggled.connect(self._on_play_toggled)
        self.slider = QSlider(Qt.Orientation.Horizontal)
        self.slider.setRange(0, 0)
        self.slider.setEnabled(False)
        self.slider.valueChanged.connect(self._on_slider)
        self.time_label = QLabel("")
        self.time_label.setProperty("role", "mono")
        self.time_label.setMinimumWidth(170)
        self.status_label = QLabel("")
        self.status_label.setProperty("role", "muted")
        self.hover_label = QLabel("")
        self.hover_label.setProperty("role", "mono")
        bottom = QHBoxLayout()
        bottom.setContentsMargins(0, 0, 0, 0)
        bottom.addWidget(self.play_button)
        bottom.addWidget(self.slider, 1)
        bottom.addWidget(self.time_label)
        info = QHBoxLayout()
        info.setContentsMargins(0, 0, 0, 0)
        info.addWidget(self.status_label, 1)
        info.addWidget(self.hover_label)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 6)
        layout.setSpacing(6)
        layout.addLayout(top)
        layout.addWidget(self.view, 1)
        layout.addLayout(bottom)
        layout.addLayout(info)

        # ---- timers and controller ---------------------------------------------------- #
        self._render_timer = QTimer(self)
        self._render_timer.setSingleShot(True)
        self._render_timer.setInterval(0)
        self._render_timer.timeout.connect(self.render_now)
        self._play_timer = QTimer(self)
        self._play_timer.timeout.connect(self._advance)
        self.controller = PreviewController(ctx, self, width_getter=self._preview_width)
        self.controller.stack_started.connect(self._on_stack_started)
        self.controller.stack_progress.connect(self._on_stack_progress)
        self.controller.stack_finished.connect(self._on_stack_finished)
        self.controller.status.connect(self.status_label.setText)
        if ctx is not None:
            ctx.active_dataset_changed.connect(self._on_dataset)
            ctx.position_changed.connect(self._on_position)
            ctx.profile_changed.connect(self._on_profile)
            ctx.histograms_changed.connect(lambda _h: self.schedule_render())
            ctx.time_format_changed.connect(lambda *_a: self._update_time_label())
            ctx.channel_names_changed.connect(self._on_channel_names)

    # ---- helpers ------------------------------------------------------------------------ #
    def _settings(self):
        return getattr(self.window(), "settings", None)

    def _preview_width(self) -> int:
        s = self._settings()
        return int(getattr(s, "preview_cache_width", 640) or 640)

    def schedule_render(self) -> None:
        self._render_timer.start()

    def set_image(self, rgb) -> None:
        """Show an RGB array (kept for callers that already have pixels)."""
        if rgb is None:
            self.view.show_message("")
            return
        self._last_rgb = np.asarray(rgb)
        self.view.set_image(rgb_to_qimage(self._last_rgb))

    def current_view(self) -> str:
        return self._view_mode

    def set_view(self, view: str) -> None:
        self._view_mode = view
        for button_id, name in self._view_ids.items():
            button = self.view_group.button(button_id)
            if button is not None:
                button.setChecked(name == view)
        self.schedule_render()

    def last_image(self) -> np.ndarray | None:
        return self._last_rgb

    # ---- context reactions -------------------------------------------------------------------- #
    def _on_dataset(self, dataset) -> None:
        self.play_button.setChecked(False)
        self._stack = None
        self._last_rgb = None
        self.slider.blockSignals(True)
        self.slider.setRange(0, 0)
        self.slider.blockSignals(False)
        self.slider.setEnabled(False)
        self.time_label.setText("")
        if dataset is None:
            self.title.setText("No experiment open")
            self._rebuild_views([])
            self.view.show_message("Open an experiment folder to see a live preview")
            return
        self.title.setText(getattr(dataset, "name", ""))
        self._rebuild_views(list(getattr(dataset, "channels", []) or []))
        self.view.show_message("Loading preview…")

    def _on_position(self, roi: str) -> None:
        ds = getattr(self.ctx, "active", None)
        if ds is not None and roi:
            self.title.setText(f"{ds.name} · {roi}")

    def _on_profile(self, _profile) -> None:
        self._log_next_render = True
        self.schedule_render()

    def _view_label(self, name: str) -> str:
        if name in VIEW_LABELS:
            return VIEW_LABELS[name]
        custom = (getattr(self.ctx, "channel_names", None) or {}).get(name) if self.ctx is not None else None
        return f"{name} · {custom}" if custom else name

    def _on_channel_names(self, _names) -> None:
        for button_id, name in self._view_ids.items():
            button = self.view_group.button(button_id)
            if button is not None:
                button.setText(self._view_label(name))

    def _rebuild_views(self, channels: list[str]) -> None:
        for button in list(self.view_group.buttons()):
            self.view_group.removeButton(button)
            button.deleteLater()
        self._view_ids.clear()
        fluor = [c for c in CHANNEL_ORDER if c in channels and c in FLUOR_CHANNELS]
        views: list[str] = []
        if fluor:
            views.append(VIEW_COMPOSITE)
            if "WHITE" in channels:
                views.append(VIEW_FLUOR)
        views += [c for c in CHANNEL_ORDER if c in channels]
        if self._view_mode not in views:
            self._view_mode = views[0] if views else VIEW_COMPOSITE
        for i, name in enumerate(views):
            button = QToolButton(text=self._view_label(name))
            button.setStyleSheet(CHIP_QSS)
            button.setCheckable(True)
            button.setChecked(name == self._view_mode)
            button.setToolTip(f"Show the {VIEW_LABELS.get(name, name + ' channel').lower()}")
            self.view_group.addButton(button, i)
            self.view_layout.addWidget(button)
            self._view_ids[i] = name

    def _on_view_clicked(self, button_id: int) -> None:
        view = self._view_ids.get(button_id)
        if view and view != self._view_mode:
            self._view_mode = view
            log.debug("viewer.set_view(%s)", view)
            self.schedule_render()

    # ---- stack events ------------------------------------------------------------------------- #
    def _on_stack_started(self, stack: PreviewStack) -> None:
        same = self._stack is not None and self._stack.key == stack.key
        self._stack = stack
        self.slider.blockSignals(True)
        self.slider.setRange(0, max(0, stack.n - 1))
        if not same:
            self.slider.setValue(0)
        self.slider.blockSignals(False)
        self.slider.setEnabled(stack.n > 1)
        self.play_button.setEnabled(stack.n > 1)
        self._update_time_label()
        self.schedule_render()

    def _on_stack_progress(self, stack: PreviewStack, loaded: int) -> None:
        if self._stack is None or self._stack.key != stack.key:
            self._on_stack_started(stack)
            return
        if self._last_rgb is None or getattr(self, "_shown_index", None) != int(self.slider.value()):
            self.schedule_render()

    def _on_stack_finished(self, stack: PreviewStack) -> None:
        if self._stack is None or self._stack.key != stack.key:
            self._on_stack_started(stack)
        self.schedule_render()

    # ---- rendering ------------------------------------------------------------------------------ #
    def render_now(self) -> None:
        stack = self._stack
        if stack is None or self.ctx is None:
            return
        index = int(self.slider.value())
        shown = index if stack.is_ready(index) else stack.nearest_ready(index)  # frames load coarse to fine
        if shown is None:
            self._update_time_label(loading=True)
            return
        planes = stack.planes_at(shown)
        if not planes:
            return
        t0 = time.perf_counter()
        profile = self.ctx.profile
        if profile.rolling_ball.enabled:
            profile = profile.copy()
            profile.rolling_ball.radius_px = max(1, int(round(profile.rolling_ball.radius_px * stack.scale)))
        bounds = display.effective_bounds(profile, self.ctx.auto_bounds or {})
        view = self._view_mode
        try:
            if view == VIEW_COMPOSITE:
                rgb = display.render_frame_fast(planes, profile, bounds, include_white=True, background_mask=stack.mask)
            elif view == VIEW_FLUOR:
                rgb = display.render_frame_fast(planes, profile, bounds, include_white=False, background_mask=stack.mask)
            elif view in planes:
                rgb = display.render_single_channel_fast(planes[view], view, profile, bounds, background_mask=stack.mask)
            else:
                rgb = np.zeros((*stack.shape, 3), dtype=np.uint8)
        except ValueError as exc:  # every channel disabled, for instance
            self.view.show_message(str(exc))
            return
        ms = (time.perf_counter() - t0) * 1000.0
        self._last_rgb = rgb
        self.view.set_image(rgb_to_qimage(rgb))
        self._shown_index = shown
        self._update_time_label(loading=shown != index)
        self.rendered.emit(ms)
        self._publish_frame_histograms(stack, planes, profile)
        now = time.perf_counter()
        if self._log_next_render and not self._play_timer.isActive() and now - self._last_log > 0.5:
            self._last_log = now
            log.debug("Preview render %s t %d in %.1f ms", view, index + 1, ms)
        self._log_next_render = False

    def _publish_frame_histograms(self, stack: PreviewStack, planes: dict, profile) -> None:
        """Histograms of the frame on screen (overlay pixels excluded) for the display panel."""
        if self.ctx is None:
            return
        mask = stack.mask
        radius = profile.rolling_ball.radius_px if profile.rolling_ball.enabled else 0
        counts = {}
        for ch, plane in planes.items():
            if radius and ch in FLUOR_CHANNELS:
                plane = display.subtract_background(plane, radius, mask if mask is not None and mask.shape == plane.shape else None)
            values = plane[~mask] if mask is not None and mask.shape == plane.shape else plane.ravel()
            counts[ch] = np.bincount(values.ravel(), minlength=256)[:256]
        self.ctx.frame_histograms_changed.emit(counts)

    def _update_time_label(self, loading: bool = False) -> None:
        stack = self._stack
        if stack is None:
            self.time_label.setText("")
            return
        index = int(self.slider.value())
        text = f"t {index + 1}/{stack.n}"
        ds = getattr(self.ctx, "active", None)
        interval = getattr(ds, "interval", None) if ds is not None else None
        if interval and index < stack.n:  # the videos' format and start time (0.6)
            offset = float(getattr(self.ctx, "time_offset", 0.0) or 0.0)
            days = show_days(getattr(self.ctx, "time_days", "auto"), offset + stack.serials[-1] * float(interval))
            text += f" · {format_time(offset + stack.serials[index] * float(interval), days)}"
        if loading:
            text += " · loading"
        self.time_label.setText(text)

    def _on_slider(self, _value: int) -> None:
        self.schedule_render()

    def _on_hover(self, x: int, y: int) -> None:
        stack = self._stack
        if stack is None or x < 0:
            self.hover_label.setText("")
            return
        values = stack.values_at(int(self.slider.value()), x, y)
        if not values:
            self.hover_label.setText("")
            return
        fx, fy = int(x / stack.scale), int(y / stack.scale)
        self.hover_label.setText(f"x {fx} y {fy} · " + " · ".join(f"{ch} {v}" for ch, v in values.items()))

    # ---- playback ------------------------------------------------------------------------------------ #
    def _fps(self) -> float:
        stack = self._stack
        settings = self._settings()
        duration = float(getattr(settings, "duration_seconds", 10.0) or 10.0)
        n = stack.n if stack is not None else 1
        return float(np.clip(n / max(duration, 0.1), 1.0, 30.0))

    def _on_play_toggled(self, playing: bool) -> None:
        icon = QStyle.StandardPixmap.SP_MediaPause if playing else QStyle.StandardPixmap.SP_MediaPlay
        self.play_button.setIcon(self.style().standardIcon(icon))
        if playing and self._stack is not None and self._stack.n > 1:
            self._play_timer.start(int(1000.0 / self._fps()))
        else:
            self._play_timer.stop()

    def _advance(self) -> None:
        stack = self._stack
        if stack is None or stack.n <= 1:
            self.play_button.setChecked(False)
            return
        nxt = int(self.slider.value()) + 1
        if nxt >= stack.n:
            nxt = 0
        if not stack.is_ready(nxt):  # still loading: jump to the next loaded timepoint
            nxt = stack.next_ready(nxt)
            if nxt is None:
                return
        self.slider.setValue(nxt)

    def keyPressEvent(self, event) -> None:  # noqa: N802 - Qt API
        key = event.key()
        if key == Qt.Key.Key_Space:
            self.play_button.toggle()
        elif key == Qt.Key.Key_Right:
            self.slider.setValue(min(self.slider.maximum(), self.slider.value() + 1))
        elif key == Qt.Key.Key_Left:
            self.slider.setValue(max(0, self.slider.value() - 1))
        else:
            super().keyPressEvent(event)
