# Scale-bar glyph templates

Harvested from real LS720 captures by `tools/harvest_glyphs.py`; do not hand-edit.
`engine/calibration.read_scale_label` matches them against the binary yellow mask
(`R>170 & G>170 & B<90`) with `cv2.matchTemplate(TM_CCOEFF_NORMED)`.

## Geometry

- Label row span at 1900 px: rows 1816-1830 (half-open 1816-1831) in every source experiment.
- Glyph (template canvas) height: 15 px.
- Each PNG is the **full label height**, trimmed only left and right to the glyph, so the
  baseline offset is part of the match. This is what keeps `,` from matching the descender of `µ`.
- Binary: 0 = background, 255 = glyph. Templates are **never rescaled**; Lumaview draws the
  label at native pixel size regardless of frame size, so only the search regions scale.

## Variants

Lumaview anti-aliases the label against the image underneath, and the strict yellow rule
(`R>170 & G>170 & B<90`) therefore yields a small family of binary shapes per character:
the same `1` binarises 3 px wide in one position and 4 px in another, and every `0` splits
at its 1-px left stroke. `<name>.png` is the most frequent rendering; `<name>_v2.png`,
`_v3.png` … are the other renderings actually observed. The reader matches all of them and
keeps the best score, so no threshold had to be relaxed. In the agreement column below,
`min r to canonical` compares variants of equal width; variants that binarise to a
*different* width cannot be correlated pixel-for-pixel and are reported as such.

Every template also carries one column of background on each side. Those empty flanks are
what stop the 3-px comma - two surviving pixels of an anti-aliased glyph - from matching
single columns inside `µ` and `m` with a perfect score.

## Sources

| Experiment | Label | Frame | Bar px |
|---|---|---|---|
| 20260313_195541_CD14 | 100 µm, 10x | 20260313_195541_CD14/ROI-1s1a/<prefix>_ROI-1s1a_WHITE_000000.tif | 120 |
| 20250708_192541_Insphero_after CF double wash | 200 µm, 10x | 20250708_192541_Insphero_after CF double wash/Timelapse_ROI-1a_AZR_cf_WHITE_000000.tif | 241 |
| 20250522_200426_HepaRG_CFDA working | 200 µm, 4x | 20250522_200426_HepaRG_CFDA working/ROI-1b/WHITE/Timelapse_ROI-1b_WHITE_000000.tif | 96 |
| 20260707_221520-verify if power | 500 µm, 10x | 20260707_221520-verify if power/ROI-1a/<prefix>_ROI-1a_WHITE_000000.tif | 603 |
| 20250317_115335 | 200 µm, 10x | 20250317_115335/Timelapse_ROI-10j_WHITE_000000.tif | 240 |

## Templates

| Files | Character | Instances | Experiments | Sizes (w x h) | Agreement |
|---|---|---|---|---|---|
| `d0.png`, `d0_v2.png`, `d0_v3.png`, `d0_v4.png` | `0` | 14 | 5 | 9 x 15, 9 x 15, 9 x 15, 9 x 15 | min r to canonical = 0.8538 |
| `d1.png`, `d1_v2.png`, `d1_v3.png` | `1` | 5 | 4 | 5 x 15, 6 x 15, 5 x 15 | min r to canonical = 0.9158; 1 variant(s) of a different width (5, 6 px) |
| `d2.png`, `d2_v2.png`, `d2_v3.png` | `2` | 3 | 3 | 9 x 15, 9 x 15, 9 x 15 | min r to canonical = 0.8517 |
| `d4.png` | `4` | 1 | 1 | 9 x 15 | one rendering only |
| `d5.png` | `5` | 1 | 1 | 9 x 15 | one rendering only |
| `mu.png`, `mu_v2.png`, `mu_v3.png` | `µ` | 5 | 5 | 8 x 15, 9 x 15, 8 x 15 | min r to canonical = 0.8225; 1 variant(s) of a different width (8, 9 px) |
| `m.png`, `m_v2.png`, `m_v3.png` | `m` | 5 | 5 | 13 x 15, 13 x 15, 13 x 15 | min r to canonical = 0.8793 |
| `comma.png`, `comma_v2.png`, `comma_v3.png` | `,` | 5 | 5 | 3 x 15, 3 x 15, 3 x 15 | min r to canonical = 0.4767 |
| `x.png`, `x_v2.png`, `x_v3.png` | `x` | 5 | 5 | 9 x 15, 9 x 15, 8 x 15 | min r to canonical = 0.9255; 1 variant(s) of a different width (8, 9 px) |

## Missing digits

No sample label contains 3, 6, 7, 8, 9, so `d3`, `d6`, `d7`, `d8`, `d9` are **absent**.
A label using one of them (for example `300 µm`) reads back as unreadable with low confidence,
and the user picks the objective manually.

PIL fallbacks were **tested and rejected**. Arial, Segoe UI, Tahoma, Verdana, Microsoft Sans
Serif, Calibri and Consolas were rendered at 9-18 px, binarised at 128 and correlated against
the harvested `0 1 2 4 5` over every vertical placement: the best font/size combination did not
reach r = 0.5 on all five, far below the 0.85 the reader requires. Lumaview's label is a
hinted bitmap rendering anti-aliased against the image, which no plain glyph render reproduces.
Adding the missing digits needs a real capture that contains them - drop such a frame into the
samples root, add its label to `LABELLED` in `tools/harvest_glyphs.py` and re-run this tool.

Generated for 5 labelled experiments.
