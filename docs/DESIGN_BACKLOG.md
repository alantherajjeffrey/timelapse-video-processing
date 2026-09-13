# Design backlog after 0.7

Deferred on purpose; each item needs the maintainer's decision before work starts.

- **Visual threshold and mask preview (fixed images).** Show the raw image beside or under its positive mask, responding live to the threshold and the overlay exclusion, before results are written. Carried over from Codex 0.3's backlog (2026-09-08). Keep raw measurements; distinguish threshold-positive area from a validated spheroid outline.
- **Crosstalk subtraction.** `F3' = F3 − α·F2` with a user-set α, previewed live, for spectral bleed-through between channels (decision 10 treated 0.4's overlap problem as spatial only).
- **Adaptive with a strong background gradient.** A glow whose brightness varies across the well spreads into several small histogram shoulders below the 5 % background rule; Background cut or the rolling ball handles it today.
- **16-bit or multi-page TIFFs.** No calibrated policy yet; such files fail with a clear message.
- **Tracking and segmentation.** Out of scope; the frames CSV and manifest are the hand-off for the separate tracking workflow.
- **Code signing, automatic updates, a resumable job queue.**
- **Public GitHub release.** Prepared in 0.7 (`docs/PUBLISHING.md`, `tools/export_public_repo.py`); it goes public after the maintainer's check.
- **Preview snapshot.** Save the frame on screen as a PNG with its overlays (suggested for 0.7, deferred).
- Done in 0.8: output presets, the unattended queue, Help → Check for updates.
- **Stage map for labware wells.** Read the Lumaview labware file (`.elf`) so well-named positions get stage coordinates.
