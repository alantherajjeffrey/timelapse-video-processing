"""Application entry point: ``python -m etaluma_video``.

Sets up the ``etaluma`` logger hierarchy (rotating session file in the user data
folder), creates the QApplication, applies the saved theme and shows the main window.
"""
from __future__ import annotations

import datetime as _dt
import logging
import logging.handlers
import sys
from pathlib import Path

FILE_FORMAT = "%(asctime)s %(levelname)-7s %(name)s: %(message)s"
CONSOLE_FORMAT = "%(levelname)-7s %(name)s: %(message)s"
MAX_BYTES = 5 * 1024 * 1024
BACKUP_COUNT = 5
ROOT_LOGGER = "etaluma"


def setup_logging(console: bool = True) -> Path | None:
    """Attach the rotating session file (DEBUG) and an optional console handler (INFO).

    Idempotent: calling it twice does not duplicate handlers.
    """
    from .ui.settings import logs_dir  # local import: keeps module import side-effect free

    logger = logging.getLogger(ROOT_LOGGER)
    logger.setLevel(logging.DEBUG)
    logger.propagate = False
    existing = {getattr(h, "_etaluma_role", None) for h in logger.handlers}

    path: Path | None = None
    if "file" not in existing:
        try:
            path = logs_dir() / f"session_{_dt.date.today():%Y%m%d}.log"
            handler = logging.handlers.RotatingFileHandler(path, maxBytes=MAX_BYTES, backupCount=BACKUP_COUNT, encoding="utf-8")
            handler.setLevel(logging.DEBUG)
            handler.setFormatter(logging.Formatter(FILE_FORMAT))
            handler._etaluma_role = "file"  # noqa: SLF001 - marker for idempotence
            logger.addHandler(handler)
        except Exception as exc:  # pragma: no cover - read-only user folder
            if sys.stderr is not None:  # a frozen windowed build has no streams
                print(f"Session log could not be opened: {exc}", file=sys.stderr)
            path = None
    else:
        for h in logger.handlers:
            if getattr(h, "_etaluma_role", None) == "file":
                path = Path(getattr(h, "baseFilename", "")) or None

    # A frozen build is console=False: sys.stderr is None and a StreamHandler on it would raise.
    if console and sys.stderr is not None and "console" not in existing:
        stream = logging.StreamHandler(sys.stderr)
        stream.setLevel(logging.INFO)
        stream.setFormatter(logging.Formatter(CONSOLE_FORMAT))
        stream._etaluma_role = "console"  # noqa: SLF001
        logger.addHandler(stream)
    return path


def install_excepthook() -> None:
    """Unhandled exceptions go to the session log instead of a silent crash."""
    log = logging.getLogger(ROOT_LOGGER)
    previous = sys.excepthook

    def hook(exc_type, exc_value, exc_tb):
        log.error("Unhandled exception", exc_info=(exc_type, exc_value, exc_tb))
        try:  # 0.7: the crash window, once the main window exists
            from .ui.crash_dialog import report_exception

            report_exception(exc_type, exc_value, exc_tb)
        except Exception:  # pragma: no cover - reporting must never raise
            pass
        if sys.stderr is not None:  # windowed frozen build: nowhere to print
            previous(exc_type, exc_value, exc_tb)

    sys.excepthook = hook
    import threading

    threading.excepthook = lambda args: hook(args.exc_type, args.exc_value, args.exc_traceback)


def diagnostic(path: str | Path) -> int:
    """Write a small environment report and exit (used by packaging/test-installer.ps1).

    ``python -m etaluma_video --diagnostic report.json`` opens no window.
    """
    import json
    import platform

    from . import APP_NAME, VERSION
    from .ui.settings import user_data_dir

    def version_of(module: str) -> str | None:
        try:
            mod = __import__(module)
        except Exception as exc:
            return f"missing ({type(exc).__name__})"
        return str(getattr(mod, "__version__", getattr(mod, "version", "unknown")))

    ffmpeg = None
    ffmpeg_exists = False
    try:
        import imageio_ffmpeg

        ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
        ffmpeg_exists = Path(ffmpeg).is_file()
    except Exception as exc:  # pragma: no cover - depends on the bundle
        ffmpeg = f"unavailable ({type(exc).__name__})"

    try:
        from PySide6.QtCore import qVersion

        qt = qVersion()
    except Exception:  # pragma: no cover
        qt = "unknown"

    report = {
        "app": APP_NAME,
        "app_version": VERSION,
        "frozen": bool(getattr(sys, "frozen", False)),
        "executable": sys.executable,
        "python": platform.python_version(),
        "platform": platform.platform(),
        "qt": qt,
        "modules": {name: version_of(name) for name in
                    ("PySide6", "numpy", "cv2", "tifffile", "imagecodecs", "imageio_ffmpeg", "PIL")},
        "ffmpeg_exe": ffmpeg,
        "ffmpeg_exists": ffmpeg_exists,
        "user_data_dir": str(user_data_dir()),
        "session_log": str(setup_logging(console=False) or ""),
    }
    target = Path(path).expanduser()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(report, indent=2), encoding="utf-8")
    logging.getLogger("etaluma.ui").info("Diagnostic report written to %s", target)
    return 0


def main(argv: list[str] | None = None) -> int:
    """Create the application and run it. Returns the Qt exit code (0 = success)."""
    argv = list(sys.argv if argv is None else argv)
    if "--diagnostic" in argv:
        index = argv.index("--diagnostic")
        target = argv[index + 1] if index + 1 < len(argv) else "diagnostic.json"
        return diagnostic(target)
    if "--version" in argv:
        from . import APP_NAME, VERSION

        logging.getLogger("etaluma.ui").info("%s %s", APP_NAME, VERSION)
        if sys.stdout is not None:
            print(f"{APP_NAME} {VERSION}")
        return 0
    from PySide6.QtCore import Qt, QTimer
    from PySide6.QtGui import QGuiApplication
    from PySide6.QtWidgets import QApplication

    from . import APP_NAME, VERSION
    from .ui.main_window import MainWindow, app_icon
    from .ui.settings import Settings, user_data_dir
    from .ui.state import AppContext
    from .ui.theme import apply_theme

    log_path = setup_logging()
    install_excepthook()
    log = logging.getLogger("etaluma.ui")

    QGuiApplication.setHighDpiScaleFactorRoundingPolicy(Qt.HighDpiScaleFactorRoundingPolicy.PassThrough)
    app = QApplication.instance() or QApplication(argv)
    app.setApplicationName(APP_NAME)
    app.setApplicationDisplayName(APP_NAME)
    app.setApplicationVersion(VERSION)
    app.setOrganizationName(APP_NAME)
    app.setWindowIcon(app_icon())

    settings = Settings.load()
    theme = apply_theme(app, settings.theme)
    log.info("%s %s starting (Python %s, Qt %s)", APP_NAME, VERSION, sys.version.split()[0], _qt_version())
    log.info("User data folder: %s", user_data_dir())
    log.debug("Session log: %s · theme %s", log_path, theme)

    ctx = AppContext()
    window = MainWindow(ctx, settings)
    from .ui.crash_dialog import install as install_crash_window

    install_crash_window(window)
    window.show()

    folders = [a for a in argv[1:] if not a.startswith("-")]
    if folders and Path(folders[0]).is_dir():
        QTimer.singleShot(0, lambda: window.actions_api.open_folder(folders[0]))

    return int(app.exec())


def _qt_version() -> str:
    try:
        from PySide6.QtCore import qVersion

        return qVersion()
    except Exception:  # pragma: no cover
        return "unknown"


if __name__ == "__main__":
    raise SystemExit(main())
