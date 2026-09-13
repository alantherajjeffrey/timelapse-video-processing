"""Screenshots for the README, made from synthetic demo experiments (never from real data).

    python tools/make_screenshots.py [output folder]          (default: docs/images)

Runs offscreen with the development environment. The demo data, settings and outputs live in a
scratch folder at the root of the system drive (``C:\\TimelapseDemo``), so no path in a screenshot
shows a Windows user name; the folder is removed at the end.
"""
from __future__ import annotations

import os
import shutil
import sys
import time
from pathlib import Path

APP = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(APP / "source"))
sys.path.insert(0, str(APP))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
if sys.platform == "win32":
    os.environ.setdefault("QT_QPA_FONTDIR", r"C:\Windows\Fonts")
WORK = Path(os.environ.get("SystemDrive", "C:") + "\\") / "TimelapseDemo"
os.environ["ETALUMA_DATA_DIR"] = str(WORK / "settings")


def wait(app, condition, seconds: float = 180.0) -> None:
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        app.processEvents()
        if condition():
            return
        time.sleep(0.02)
    raise TimeoutError("the window did not get there in time")


def settle(app, seconds: float = 1.0) -> None:
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        app.processEvents()
        time.sleep(0.02)


def main(out: Path) -> None:
    from PySide6.QtWidgets import QApplication

    from etaluma_video.ui import theme
    from etaluma_video.ui.main_window import MainWindow
    from etaluma_video.ui.settings import Settings
    from etaluma_video.ui.state import AppContext
    from etaluma_video.ui.workers import wait_for_background
    from tests.fixtures.make_synthetic_dataset import make_dataset

    out.mkdir(parents=True, exist_ok=True)
    if WORK.exists():
        shutil.rmtree(WORK)
    app = QApplication.instance() or QApplication(sys.argv)
    theme.apply_theme(app, theme.DEFAULT_THEME)
    window = MainWindow(AppContext(), Settings())
    window.resize(1500, 930)
    window.show()
    settle(app, 0.5)
    window.grab().save(str(out / "welcome.png"))

    first = make_dataset(WORK / "20260101_120000_demo", positions=3, timepoints=12, size=600)
    second = make_dataset(WORK / "20260102_090000_demo_b", positions=2, timepoints=12, size=600, seed=1)
    window.actions_api.open_folder(str(first))
    wait(app, lambda: window.ctx.active is not None)
    wait(app, lambda: window.viewer.last_image() is not None and bool(window.ctx.auto_bounds))
    wait(app, lambda: not window.runner.busy)
    settle(app, 2.0)
    window.grab().save(str(out / "main_window.png"))

    window.queue.add_folders([str(first)], display="auto", preset="builtin:quick")
    window.queue.start()
    wait(app, lambda: not window.queue.running, 600)
    window.queue.add_folders([str(second)], display="remembered", preset="builtin:presentation")
    window.queue.add_folders([str(first)], display="auto", preset="builtin:standard")
    window.show_queue()
    settle(app, 1.0)
    window.grab().save(str(out / "queue.png"))

    window.viewer.controller.cancel_all()
    wait_for_background(20000)
    window.close()
    settle(app, 0.5)
    shutil.rmtree(WORK, ignore_errors=True)
    print(f"Screenshots written to {out}")


if __name__ == "__main__":
    main(Path(sys.argv[1]) if len(sys.argv) > 1 else APP / "docs" / "images")
