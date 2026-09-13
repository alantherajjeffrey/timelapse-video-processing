"""Print the estimated size of a run before starting it.

    python tools/estimate_output_size.py "<experiment folder>" [--width 950] [--no-avi] [--no-mp4]

Thin wrapper over ``engine.process.estimate_output_bytes`` (the same numbers the app logs before
Process starts): MJPG is about 0.5 bytes per rendered pixel, H.264 about a tenth of that.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "source"))

from etaluma_video.engine import Options, estimate_output_bytes, scan_dataset  # noqa: E402


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Estimate the output size of a run")
    parser.add_argument("folder", type=Path)
    parser.add_argument("--width", type=int, default=950)
    parser.add_argument("--no-avi", action="store_true")
    parser.add_argument("--no-mp4", action="store_true")
    parser.add_argument("--json", action="store_true", help="Print the raw estimate dictionary")
    args = parser.parse_args(argv)
    ds = scan_dataset(args.folder)
    estimate = estimate_output_bytes(ds, Options(width=args.width, avi=not args.no_avi, mp4=not args.no_mp4))
    print(json.dumps(estimate, indent=2) if args.json else f"{ds.name}: {estimate['text']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
