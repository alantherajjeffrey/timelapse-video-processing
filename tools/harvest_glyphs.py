"""Harvest scale-bar glyph templates from the real Etaluma captures.

Lumaview burns the label "<N> µm, <M>x" into every frame with the same bitmap
font at the same size, so the reader in ``engine/calibration.py`` can match
exact crops of real captures instead of a rendered font.

Four experiments carry a label that has been read visually; between them they
supply the characters ``0 1 2 4 5 µ m , x``. Digits 3, 6, 7, 8 and 9 never
appear in the sample set (see assets/glyphs/README.md).

Each template is a *full label-height* canvas (15 rows on 1900 px captures)
trimmed left and right to the glyph, saved as a binary PNG. Keeping the empty
rows above and below encodes the baseline, which is what separates the comma
from the descender of "µ".

Usage
-----
    .venv\\Scripts\\python.exe tools\\harvest_glyphs.py "..\\private\\Sample set" \\
        --out source\\etaluma_video\\assets\\glyphs

Read-only with respect to the samples root.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np
import tifffile
from PIL import Image

HERE = Path(__file__).resolve().parent
V04 = HERE.parent
if str(V04 / "source") not in sys.path:
    sys.path.insert(0, str(V04 / "source"))

from etaluma_video.engine.calibration import GLYPH_CHARS, detect_overlays, yellow_mask  # noqa: E402

sys.path.insert(0, str(HERE))
from probe_scale_bars import pick_frame, raw_frames  # noqa: E402

#: Experiment folder prefix -> the label a human read off the corner crop.
#:
#: The first four are the labels confirmed while planning (plan section 2.4). 20250317 was
#: added during M1: it is the only *brightfield* capture in the sample set (WHITE median 171
#: against 5-99 for the rest), and Lumaview anti-aliases the label against the image, so its
#: glyphs binarise to different shapes under the strict yellow rule. Without it the reader
#: cannot reach the 0.85 threshold on that experiment. Its label is verified ground truth
#: (plan section 2.4 and the 2x corner crop from tools/probe_scale_bars.py).
#:
#: 20230213, 20260202, 20260204 and 20260209 are deliberately NOT harvested: they are the
#: held-out experiments that show the templates generalise to unseen captures.
LABELLED: dict[str, str] = {
    "20260313_195541_CD14": "100 µm, 10x",
    "20250708_192541_Insphero": "200 µm, 10x",
    "20250522_200426_HepaRG": "200 µm, 4x",
    "20260707_221520": "500 µm, 10x",
    "20250317_115335": "200 µm, 10x",
}
#: Character -> template file stem (the inverse of calibration.GLYPH_CHARS).
CHAR_NAMES: dict[str, str] = {c: n for n, c in GLYPH_CHARS.items()}
#: Digits that no sample label contains.
MISSING_DIGITS = ("3", "6", "7", "8", "9")


#: Largest horizontal gap (px) across which two components still belong to one glyph.
#: 0 means "touching or overlapping". The label font is anti-aliased, so the strict
#: yellow rule splits every "0" at its 1-px left stroke and the comma into two
#: isolated pixels; both re-join at gap 0. The narrowest real inter-glyph gap in
#: the sample labels is 2 px, so this cannot merge two different characters.
MERGE_GAP_PX = 0
#: Columns of background kept on each side of a glyph in its template (see harvest_experiment).
GLYPH_MARGIN_PX = 1


def merge_overlapping(boxes: list[tuple[int, int, int, int]],
                      gap: int = MERGE_GAP_PX) -> list[tuple[int, int, int, int]]:
    """Merge boxes (y0, y1, x0, x1) whose x-ranges overlap or touch.

    Keeps "µ" together with its descender and the two surviving comma pixels together.
    """
    out: list[list[int]] = []
    for y0, y1, x0, x1 in sorted(boxes, key=lambda b: b[2]):
        if out and x0 <= out[-1][3] + gap:  # overlaps or touches the running group in x
            g = out[-1]
            g[0], g[1] = min(g[0], y0), max(g[1], y1)
            g[2], g[3] = min(g[2], x0), max(g[3], x1)
        else:
            out.append([y0, y1, x0, x1])
    return [tuple(g) for g in out]


def glyph_boxes(mask: np.ndarray, label_box) -> list[tuple[int, int, int, int]]:
    """Glyph boxes inside label_box, in absolute image coordinates, left to right."""
    import cv2

    sub = np.where(mask[label_box.y0 : label_box.y1, label_box.x0 : label_box.x1], 255, 0).astype(np.uint8)
    n, _, stats, _ = cv2.connectedComponentsWithStats(sub, connectivity=8)
    boxes = []
    for i in range(1, n):  # 0 is the background
        x, y, w, h, _area = stats[i]
        boxes.append((label_box.y0 + int(y), label_box.y0 + int(y + h),
                      label_box.x0 + int(x), label_box.x0 + int(x + w)))
    return merge_overlapping(boxes)


def harvest_experiment(experiment: Path, label: str, root: Path) -> dict:
    """Crop one template per character of ``label`` from the first frame of ``experiment``."""
    chosen = pick_frame(raw_frames(experiment))
    if chosen is None:
        raise SystemExit(f"{experiment.name}: no raw frames")
    channel, _, path = chosen
    rgb = np.ascontiguousarray(tifffile.imread(path)[..., :3])
    mask = yellow_mask(rgb)
    ov = detect_overlays(rgb)
    if ov.label_box is None:
        raise SystemExit(f"{experiment.name}: no label box detected")

    chars = [c for c in label if not c.isspace()]
    boxes = glyph_boxes(mask, ov.label_box)
    if len(boxes) != len(chars):
        raise SystemExit(
            f"{experiment.name}: expected {len(chars)} glyphs for {label!r} but split into {len(boxes)}: {boxes}")

    instances: list[tuple[str, np.ndarray]] = []
    for ch, (_, _, x0, x1) in zip(chars, boxes):
        # Full label height, trimmed left/right to the glyph plus GLYPH_MARGIN_PX columns of
        # background: the baseline *and* the empty flanks are part of the match. Without the
        # flanks the 1-px-wide comma matches single columns inside "µ" with score 1.0.
        a = max(0, x0 - GLYPH_MARGIN_PX)
        b = min(mask.shape[1], x1 + GLYPH_MARGIN_PX)
        instances.append((ch, np.where(mask[ov.label_box.y0 : ov.label_box.y1, a:b], 255, 0).astype(np.uint8)))
    return {"experiment": experiment.name, "frame": str(path.relative_to(root)).replace("\\", "/"),
            "channel": channel, "label": label, "label_box": ov.label_box.to_list(),
            "bar_px": ov.bar_len_px, "glyph_height": int(ov.label_box.height),
            "instances": instances, "boxes": boxes}


def correlation(a: np.ndarray, b: np.ndarray) -> float | None:
    """Normalised cross-correlation of two binary templates; None when the widths differ."""
    if a.shape != b.shape:
        return None
    x, y = a.astype(np.float64).ravel(), b.astype(np.float64).ravel()
    x, y = x - x.mean(), y - y.mean()
    d = float(np.sqrt((x * x).sum() * (y * y).sum()))
    return 1.0 if d == 0 else float((x * y).sum() / d)


def agreement_note(distinct: list[np.ndarray], cors: list[float | None]) -> str:
    """Human-readable summary of how far the variants of one character differ."""
    if len(distinct) == 1:
        return "one rendering only"
    same = [c for c in cors if c is not None]
    parts = []
    if same:
        parts.append(f"min r to canonical = {min(same):.4f}")
    if len(same) < len(cors):
        widths = sorted({d.shape[1] for d in distinct})
        parts.append(f"{len(cors) - len(same)} variant(s) of a different width "
                     f"({', '.join(str(w) for w in widths)} px)")
    return "; ".join(parts)


def main(argv: list[str] | None = None) -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, OSError):  # pragma: no cover
        pass
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("samples", nargs="?", default=str(V04.parent / "private" / "Sample set"))
    ap.add_argument("--out", default=str(V04 / "source" / "etaluma_video" / "assets" / "glyphs"))
    ap.add_argument("--report", default=str(V04.parent / "private" / "probe" / "harvest.json"),
                    help="where the machine-readable harvest summary goes (scratch, not a shipped asset)")
    args = ap.parse_args(argv)

    root = Path(args.samples).expanduser().resolve()
    out_dir = Path(args.out).expanduser().resolve()
    if not root.is_dir():
        print(f"samples root not found: {root}")
        return 2
    if out_dir.is_relative_to(root):
        print(f"refusing to write inside the samples root: {out_dir}")
        return 2

    harvests = []
    for prefix, label in LABELLED.items():
        matches = [p for p in sorted(root.iterdir()) if p.is_dir() and p.name.startswith(prefix)]
        if not matches:
            print(f"skipping {prefix}: not present under {root}")
            continue
        harvests.append(harvest_experiment(matches[0], label, root))
    if not harvests:
        print("no labelled experiments found")
        return 2

    # ---- group every instance by character, keep the distinct renderings -- #
    per_char: dict[str, list[tuple[str, np.ndarray]]] = {}
    for h in harvests:
        for ch, t in h["instances"]:
            per_char.setdefault(ch, []).append((h["experiment"], t))

    print(f"{'glyph':<7} {'seen':>5} {'variants':>9} {'sizes (w x h)':<18} agreement of the distinct renderings")
    print("-" * 96)
    agreement: dict[str, dict] = {}
    variants: dict[str, list[np.ndarray]] = {}
    for ch in sorted(per_char, key=lambda c: list(GLYPH_CHARS).index(CHAR_NAMES[c])):
        items = per_char[ch]
        distinct: list[np.ndarray] = []
        counts: list[int] = []
        for _, t in items:
            for i, d in enumerate(distinct):
                if d.shape == t.shape and np.array_equal(d, t):
                    counts[i] += 1
                    break
            else:
                distinct.append(t)
                counts.append(1)
        # Most frequent rendering first: it becomes the canonical <name>.png.
        order = sorted(range(len(distinct)), key=lambda i: -counts[i])
        distinct = [distinct[i] for i in order]
        variants[ch] = distinct
        ref = distinct[0]
        cors = [correlation(ref, d) for d in distinct[1:]]
        same = [c for c in cors if c is not None]
        note = agreement_note(distinct, cors)
        sizes = ", ".join(f"{d.shape[1]}x{d.shape[0]}" for d in distinct)
        agreement[CHAR_NAMES[ch]] = {
            "char": ch, "instances": len(items), "experiments": sorted({n for n, _ in items}),
            "variants": len(distinct), "sizes": [[int(d.shape[0]), int(d.shape[1])] for d in distinct],
            "pixel_identical": len(distinct) == 1,
            "min_correlation_to_canonical": round(min(same), 6) if same else None,
            "variants_of_other_width": len(cors) - len(same), "agreement": note}
        print(f"{CHAR_NAMES[ch]:<7} {len(items):>5} {len(distinct):>9} {sizes:<18} {note}")

    # ---- write the templates -------------------------------------------- #
    out_dir.mkdir(parents=True, exist_ok=True)
    for old in out_dir.glob("*.png"):
        old.unlink()
    for ch, distinct in variants.items():
        for i, t in enumerate(distinct):
            suffix = "" if i == 0 else f"_v{i + 1}"
            Image.fromarray(t).save(out_dir / f"{CHAR_NAMES[ch]}{suffix}.png")

    heights = sorted({h["glyph_height"] for h in harvests})
    rows = sorted({tuple(h["label_box"][:2]) for h in harvests})
    readme = [
        "# Scale-bar glyph templates",
        "",
        "Harvested from real LS720 captures by `tools/harvest_glyphs.py`; do not hand-edit.",
        "`engine/calibration.read_scale_label` matches them against the binary yellow mask",
        "(`R>170 & G>170 & B<90`) with `cv2.matchTemplate(TM_CCOEFF_NORMED)`.",
        "",
        "## Geometry",
        "",
        f"- Label row span at 1900 px: rows {rows[0][0]}-{rows[0][1] - 1} "
        f"(half-open {rows[0][0]}-{rows[0][1]}) in every source experiment.",
        f"- Glyph (template canvas) height: {', '.join(str(h) for h in heights)} px.",
        "- Each PNG is the **full label height**, trimmed only left and right to the glyph, so the",
        "  baseline offset is part of the match. This is what keeps `,` from matching the descender of `µ`.",
        "- Binary: 0 = background, 255 = glyph. Templates are **never rescaled**; Lumaview draws the",
        "  label at native pixel size regardless of frame size, so only the search regions scale.",
        "",
        "## Variants",
        "",
        "Lumaview anti-aliases the label against the image underneath, and the strict yellow rule",
        "(`R>170 & G>170 & B<90`) therefore yields a small family of binary shapes per character:",
        "the same `1` binarises 3 px wide in one position and 4 px in another, and every `0` splits",
        "at its 1-px left stroke. `<name>.png` is the most frequent rendering; `<name>_v2.png`,",
        "`_v3.png` … are the other renderings actually observed. The reader matches all of them and",
        "keeps the best score, so no threshold had to be relaxed. In the agreement column below,",
        "`min r to canonical` compares variants of equal width; variants that binarise to a",
        "*different* width cannot be correlated pixel-for-pixel and are reported as such.",
        "",
        "Every template also carries one column of background on each side. Those empty flanks are",
        "what stop the 3-px comma - two surviving pixels of an anti-aliased glyph - from matching",
        "single columns inside `µ` and `m` with a perfect score.",
        "",
        "## Sources",
        "",
        "| Experiment | Label | Frame | Bar px |",
        "|---|---|---|---|",
    ]
    for h in harvests:
        readme.append(f"| {h['experiment']} | {h['label']} | {h['frame']} | {h['bar_px']} |")
    readme += [
        "",
        "## Templates",
        "",
        "| Files | Character | Instances | Experiments | Sizes (w x h) | Agreement |",
        "|---|---|---|---|---|---|",
    ]
    for name in sorted(agreement, key=lambda n: list(GLYPH_CHARS).index(n)):
        a = agreement[name]
        files = ", ".join(f"`{name}{'' if i == 0 else f'_v{i + 1}'}.png`" for i in range(a["variants"]))
        sizes = ", ".join(f"{s[1]} x {s[0]}" for s in a["sizes"])
        readme.append(f"| {files} | `{a['char']}` | {a['instances']} | {len(a['experiments'])} | "
                      f"{sizes} | {a['agreement']} |")
    readme += [
        "",
        "## Missing digits",
        "",
        f"No sample label contains {', '.join(MISSING_DIGITS)}, so `"
        + "`, `".join(f"d{d}" for d in MISSING_DIGITS) + "` are **absent**.",
        "A label using one of them (for example `300 µm`) reads back as unreadable with low confidence,",
        "and the user picks the objective manually.",
        "",
        "PIL fallbacks were **tested and rejected**. Arial, Segoe UI, Tahoma, Verdana, Microsoft Sans",
        "Serif, Calibri and Consolas were rendered at 9-18 px, binarised at 128 and correlated against",
        "the harvested `0 1 2 4 5` over every vertical placement: the best font/size combination did not",
        "reach r = 0.5 on all five, far below the 0.85 the reader requires. Lumaview's label is a",
        "hinted bitmap rendering anti-aliased against the image, which no plain glyph render reproduces.",
        "Adding the missing digits needs a real capture that contains them - drop such a frame into the",
        "samples root, add its label to `LABELLED` in `tools/harvest_glyphs.py` and re-run this tool.",
        "",
        f"Generated for {len(harvests)} labelled experiments.",
        "",
    ]
    (out_dir / "README.md").write_text("\n".join(readme), encoding="utf-8")

    # The machine-readable summary is scratch output, not a shipped asset: assets/glyphs/
    # holds only the templates and their README.
    summary = {"glyphs": agreement, "label_rows": [list(r) for r in rows], "glyph_height": heights,
               "sources": [{k: h[k] for k in ("experiment", "frame", "label", "label_box", "bar_px")} for h in harvests],
               "missing_digits": list(MISSING_DIGITS)}
    scratch = Path(args.report).expanduser().resolve()
    scratch.parent.mkdir(parents=True, exist_ok=True)
    scratch.write_text(json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8")
    n_files = sum(a["variants"] for a in agreement.values())
    print(f"\n{len(agreement)} characters, {n_files} template files -> {out_dir}")
    print(f"harvest report -> {scratch}")
    print(f"missing digits (no sample provides them): {', '.join(MISSING_DIGITS)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
