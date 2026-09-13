"""Threshold measurements on raw capture planes. Display settings never touch these numbers.

The standing rules, unchanged since Codex 0.1:

* a pixel is positive when its **raw 8-bit value is strictly greater** than the threshold,
* areas are ``pixels x pixel_size**2`` µm²,
* objects are 8-connected components of the positive mask; they are not validated biology,
* the measured region may exclude bottom rows (``exclude_bottom_px``) and, new in 0.4,
  any detected Lumaview overlay (``exclude_mask``).

With ``exclude_mask=None`` and no bottom crop the result is byte-for-byte the Codex 0.3 result.
"""
from __future__ import annotations

import cv2
import numpy as np

__all__ = ["quantify"]


def quantify(a: np.ndarray, threshold: float, pixel_size: float, exclude_bottom_px: int = 0,
             exclude_mask: np.ndarray | None = None) -> dict:
    """Measure one raw plane.

    a: uint8 HxW plane (never normalized).
    threshold: absolute 8-bit value; pixels strictly greater than it are positive.
    pixel_size: µm per pixel, for the µm² columns.
    exclude_bottom_px: rows cropped from the bottom before measuring.
    exclude_mask: optional boolean HxW mask, True on pixels to ignore (detected overlays).
    """
    if not 0 <= exclude_bottom_px < a.shape[0]:
        raise ValueError("Excluded bottom rows must leave some image area.")
    region = a[: a.shape[0] - exclude_bottom_px]
    if exclude_mask is not None:
        if exclude_mask.shape != a.shape:
            raise ValueError("The exclusion mask must match the image dimensions.")
        keep = ~np.asarray(exclude_mask, dtype=bool)[: a.shape[0] - exclude_bottom_px]
    else:
        keep = None
    if keep is not None and not keep.any():
        raise ValueError("The exclusion mask removes the whole measured area.")
    values = region[keep] if keep is not None else region.ravel()
    mask = (region > threshold)
    if keep is not None:
        mask &= keep
    mask = mask.astype(np.uint8)
    count, _, stats, _ = cv2.connectedComponentsWithStats(mask, connectivity=8)
    positive = int(mask.sum())
    measured = int(values.size)
    largest = int(stats[1:, cv2.CC_STAT_AREA].max()) if count > 1 else 0
    return {"mean_intensity": float(values.mean()), "integrated_density": int(values.sum(dtype=np.uint64)),
            "positive_area_percent": 100 * positive / measured, "positive_area_px": positive,
            "positive_area_um2": positive * pixel_size ** 2,
            "largest_positive_object_px": largest,
            "largest_positive_object_um2": largest * pixel_size ** 2,
            "positive_object_count": count - 1, "measured_area_px": measured,
            "excluded_bottom_px": exclude_bottom_px,
            "excluded_overlay_px": int(region.size - measured),
            "threshold": threshold, "threshold_rule": "intensity > threshold",
            "area_method": "threshold-positive pixels; 8-connected objects; no biological segmentation"}
