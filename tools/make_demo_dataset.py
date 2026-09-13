"""Write a small synthetic Etaluma experiment, to try the app without a microscope or real data.

    python tools/make_demo_dataset.py "<new folder>" [--positions 3] [--timepoints 24] [--size 950]
                                      [--layout subfolders|flat] [--fixed]

The images are generated, not recorded: WHITE, F2 and F3 planes with drifting bright spots on a
textured background, Lumaview-style burned-in timestamp and scale bar ("100 µm, 10x"), a protocol
file (.epf) and AviSynth files, in the same folder shapes Lumaview writes. The test suite uses the
same generator (tests/fixtures/make_synthetic_dataset.py), so what the app does with the demo is
what the tests check. Open the folder with "Open folder" in the app.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "source"))

from tests.fixtures.make_synthetic_dataset import make_dataset  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Write a synthetic Etaluma experiment to try the app.")
    parser.add_argument("folder", type=Path, help="Experiment folder to create (must not exist or be empty)")
    parser.add_argument("--positions", type=int, default=3, help="Stage positions (default 3)")
    parser.add_argument("--timepoints", type=int, default=24, help="Timepoints per position (default 24)")
    parser.add_argument("--size", type=int, default=950, help="Image width and height in pixels (default 950)")
    parser.add_argument("--layout", choices=["subfolders", "flat"], default="subfolders",
                        help="A folder per position (default) or every file in the experiment folder")
    parser.add_argument("--fixed", action="store_true", help="One timepoint only (fixed-image mode)")
    args = parser.parse_args(argv)
    if args.folder.exists() and any(args.folder.iterdir()):
        parser.error(f"{args.folder} already holds files; choose a new folder")
    path = make_dataset(args.folder, positions=args.positions, timepoints=1 if args.fixed else args.timepoints,
                        size=args.size, layout=args.layout, fixed=args.fixed, prefix="Demo")
    print(f"Demo experiment written to {path}")
    print('Open it with "Open folder" in Timelapse Video Processing.')
    return 0


if __name__ == "__main__":
    sys.exit(main())
