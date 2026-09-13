"""PyInstaller entry-point shim for Timelapse Video Processing.

app.spec points Analysis at this file instead of at
``source/etaluma_video/__main__.py`` directly, so packaging does not need to
assume anything about how the UI shell agent writes that module (a bare
``if __name__ == "__main__":`` guard vs. an importable ``main()``, relative
vs. absolute imports, etc.). The only contract this file depends on is the
one recorded in docs/plans/v0.4_plan.md 5.1 and this folder's
docs/INTEGRATION_NOTES.md: ``etaluma_video.__main__`` exposes a ``main()``
callable that runs the app and returns an int exit code (or None, treated as
0).

Not part of the installed application; PyInstaller compiles this into the
bootloader's entry script and it never appears as a separate file in the
frozen build.
"""
from __future__ import annotations

import sys


def _run() -> int:
    from etaluma_video.__main__ import main

    result = main()
    return int(result) if result is not None else 0


if __name__ == "__main__":
    sys.exit(_run())
