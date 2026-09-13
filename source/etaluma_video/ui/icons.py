"""Line icons drawn from inline SVG in the theme's colours (0.7).

One stroke style for every toolbar button (0.6 mixed Qt's stock pixmaps with one SVG gear). The
frozen build ships Qt's SVG image plugin; without it every icon comes back empty and the buttons
keep their text.
"""
from __future__ import annotations

from PySide6.QtCore import QByteArray, Qt
from PySide6.QtGui import QIcon, QImage, QPixmap

_STROKE = 'fill="none" stroke="{c}" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"'

SHAPES = {
    "open": '<path d="M3 7.5a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2V17a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z"/>'
            '<path d="M3 10h18"/>',
    "quick": '<path d="M13 2.5 4.5 13.5h6.5l-1 8 8.5-11h-6.5z"/>',
    "process": '<circle cx="12" cy="12" r="9"/><path d="M10 8.3v7.4l6-3.7z" fill="{c}"/>',
    "cancel": '<circle cx="12" cy="12" r="9"/><rect x="9" y="9" width="6" height="6" rx="1" fill="{c}"/>',
    "results": '<rect x="3.5" y="3.5" width="7" height="7" rx="1.5"/><rect x="13.5" y="3.5" width="7" height="7" rx="1.5"/>'
               '<rect x="3.5" y="13.5" width="7" height="7" rx="1.5"/><rect x="13.5" y="13.5" width="7" height="7" rx="1.5"/>',
    "settings": '<circle cx="12" cy="12" r="3.2"/><path d="M19.2 13.4a7.6 7.6 0 0 0 0-2.8l2-1.5-2-3.4-2.4 1a7.6 7.6 0 0 '
                '0-2.4-1.4L14 2.6h-4l-.4 2.7a7.6 7.6 0 0 0-2.4 1.4l-2.4-1-2 3.4 2 1.5a7.6 7.6 0 0 0 0 2.8l-2 1.5 2 3.4 '
                '2.4-1a7.6 7.6 0 0 0 2.4 1.4l.4 2.7h4l.4-2.7a7.6 7.6 0 0 0 2.4-1.4l2.4 1 2-3.4z"/>',
    "timelapse": '<circle cx="12" cy="12" r="9"/><path d="M12 7v5l3.2 2"/>',
    "fixed": '<rect x="3" y="5" width="18" height="14" rx="2"/><circle cx="9" cy="10" r="1.6"/><path d="m21 16-5-5-8 8"/>',
    "about": '<circle cx="12" cy="12" r="9"/><path d="M12 11v5.5"/><circle cx="12" cy="7.8" r="0.9" fill="{c}"/>',
    "queue": '<path d="M4 6h12M4 12h12M4 18h7"/><path d="M15.5 15v6l5-3z" fill="{c}"/>',
}


def svg(name: str, colour: str) -> bytes:
    body = SHAPES[name].replace("{c}", colour)
    return (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24"><g {_STROKE.replace("{c}", colour)}>'
            f"{body}</g></svg>").encode("utf-8")


def icon(name: str, colour: str, size: int = 24) -> QIcon:
    """The named icon in ``colour``, rendered at twice ``size`` for sharp high-DPI display."""
    image = QImage()
    if not image.loadFromData(QByteArray(svg(name, colour)), "SVG"):
        return QIcon()
    pixel = size * 2
    pixmap = QPixmap.fromImage(image.scaled(pixel, pixel, Qt.AspectRatioMode.KeepAspectRatio,
                                            Qt.TransformationMode.SmoothTransformation))
    pixmap.setDevicePixelRatio(2.0)
    return QIcon(pixmap)
