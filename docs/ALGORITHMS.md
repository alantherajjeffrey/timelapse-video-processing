# How the display works

*What the Display panel actually does to your images, in plain language.*

Everything on this page affects **how your movies and montages look**, never what the numbers say.
Intensity measurements and the quantification CSV are computed from the raw camera pixels. You can
push the contrast as far as you like; the measurements will not move.

---

## 1. The one idea behind all of it

A camera plane is 8-bit: every pixel is a number from 0 to 255. Your cells might live between,
say, 6 and 110 of those 255 levels, and the rest of the range is empty. If we showed the plane
as it is, you would see a nearly black movie.

So for each channel we choose two numbers — a **black point** and a **white point** — and stretch
everything between them across the full range:

- anything at or below the black point becomes black,
- anything at or above the white point becomes fully bright,
- everything in between is spread out smoothly.

Choosing those two numbers well is the whole game. **Auto-normalised** picks them for you from the
histogram of your data; **Manual** lets you drag them yourself.

The bounds are **absolute intensities**, not percentages. Once chosen, the same pair is used for
every position and every timepoint in the experiment, so a cell that gets brighter over time
*looks* brighter over time. This is why the app builds the histogram over the whole series
before exporting rather than auto-scaling each frame — per-frame auto-scaling is what makes a
timelapse flicker and makes a dimming signal look constant.

---

## 2. The three Auto-normalised methods

(Called "Auto" before 0.8.) All three are a linear stretch between a black and a white point, that is a normalisation; none of them is histogram equalisation, which would reshape the histogram non-linearly.

All three read the same thing: a histogram of every pixel of the channel, with the burned-in
Lumaview overlays (timestamp box, scale bar) excluded so the bright yellow bar cannot drag the white
point up.

**Which frames (0.6).** The histogram is built from the full-size images of every position, using
every 8th timepoint of each position (at least 24 per position; all of them when there are fewer).
On CD14 the bounds from every 16th frame were the same as from every frame, to within one gray
level, for all three methods, while reading only every 8th image cuts the measurement from 74 s to
about 8 s. The measurement starts in the background as soon as a folder is opened, the current
position first, and is kept on disk: the numbers in the Display panel are the numbers Process and
Quick video use, and they do not measure again. (0.5 measured the preview's 640 px copies of the
positions you had looked at, and its upper bounds came out lower than the videos': F3 32 in the
preview against 62 in the video on CD14.)

### Adaptive — the default

> Black point = the **brightest background level** plus one noise width. White point = the 99.9th percentile.

A fluorescence image usually has one background — the buffer, the empty gel, the dark space
between cells — and it shows up as a tall spike in the histogram. Adaptive finds that spike,
measures how wide it is (the width at half its height, converted to a standard deviation), and
puts the black point one width above it. That clips the background and its noise to black and
keeps everything above it.

Some images have **more than one background**, and that is the case Adaptive is built to handle.
A spheroid sitting in an autofluorescent well has two: the dark medium outside the well, and the
glowing well itself — which is dimmer than your cells but much brighter than the medium, and can
easily cover a fifth of the frame. Adaptive finds every level that accounts for at least **5 %**
of the pixels, treats all of them as background, and sets the black point above the **brightest**
one. Your actual signal is almost never that abundant — a spheroid covering 1 % of the frame
cannot reach the 5 % bar — so it is never mistaken for background and never clipped.

The white point is the 99.9th percentile — the brightest 0.1 % of pixels are allowed to saturate,
which stops one hot speck of debris from dimming the whole movie.

**Use it when** your signal is discrete objects on a background: puncta, labelled cells, beads,
nuclei, spheroids — the usual case, glowing well or not.

**It still fails when:**

- *Signal covers more than half the frame.* Then your signal is itself the most abundant level,
  it is counted as background, the black point lands inside your cells, and the dimmer ones
  vanish. Switch to Classic.
- *The unwanted glow has a strong gradient across it.* If a well is much brighter on one side
  than the other, its pixels spread out instead of piling up into one level, and each part may
  fall below the 5 % bar. Adaptive then clips above whichever part it did find, leaving the
  brightest corner of the glow visible. Background cut, or the rolling ball (section 7), handles
  that better.

If Adaptive proposes a window narrower than 8 gray levels, what it does depends on what it found.
With a single background, a narrow window means there is no real signal above the noise, so it
quietly uses Classic for that channel. With several backgrounds it means your signal only just
clears a bright glow — so it keeps the clipping and simply widens the window to 8 levels, because
switching to Classic there would put the black point back at 0 and restore the very glow it had
just identified. Either way the log says which happened.

### Classic

> Black point = 0. White point = the brightest per-frame 99.5th percentile seen anywhere in the series.

This is what the original 2026 processing script did, kept so old movies can be reproduced. It
never clips anything at the bottom, so the background comes along for the ride.

**Use it when** the "background" is something you want to see — a faintly labelled monolayer, a
diffuse stain, a signal that fills the field — or when you want the look of the earlier versions.

**It looks bad when** the background is bright, because the whole frame lights up. On the CD14
chip, where the green channel sits on a background around intensity 82, Classic turns 99.7 % of
the frame green and buries the phase-contrast structure completely.

### Background cut

> Black point = the 90th percentile. White point = the 99.9th percentile.

The bluntest of the three: throw away the dimmest 90 % of pixels, whatever they are. It makes no
assumption about the shape of the histogram, which is exactly why it survives the awkward cases.

**Use it when** your objects are small and bright and everything else is in the way, and Adaptive
has not managed it — typically a glow with a strong gradient across it, where Adaptive cannot
pin the background down to one level.

**It is wrong when** your signal covers more than 10 % of the frame, because then it clips your
own cells.

### Choosing quickly

| What you are looking at | Start with |
|---|---|
| Puncta, labelled cells, beads on a dark background | **Adaptive** |
| Small bright objects inside a glowing well or on a glowing gel | **Adaptive** |
| The glow is much brighter on one side of the frame | **Background cut**, or Adaptive + rolling ball |
| Something bright covering most of the frame | **Classic** |
| Reproducing a movie made by an older version | **Classic** |

The Auto-normalised bounds and the method that was actually used are printed in the log for every run, so
you can always see what the app decided.

---

## 3. Manual bounds and gamma

Switch the Display panel to **Manual** and the two handles become draggable on the real
histogram (or type the numbers). "Reset to Auto-normalised" copies whatever Auto-normalised currently proposes, which
is the easiest way to start: let Auto-normalised get close, then nudge.

**Gamma** is a third control, and it is not the same as moving the handles. The handles decide
*where* the visible range starts and stops; gamma decides how brightness is distributed *inside*
that range.

- **gamma 1.0** (default) — a straight line, faithful to the data.
- **gamma below 1** (e.g. 0.5) — lifts the dim mid-tones. Faint structures become visible
  without raising the black point. Noise becomes more visible too.
- **gamma above 1** (e.g. 2.0) — pushes the mid-tones down, so only the brightest things stay
  bright. Useful for calming a busy background.

Because gamma is non-linear it changes the *apparent* relative brightness of two objects. Keep
it at 1.0 whenever a reader might compare brightness by eye, and mention it in the figure legend
if you change it. It never touches the measurements.

Manual bounds apply to every position and timepoint. A profile can be saved and loaded on
another experiment, which is how you make two conditions genuinely comparable — same black
point, same white point, same gamma, both movies.

---

## 4. Colours

Two presets:

- **CGM** (default) — WHITE gray, F1 **c**yan, F2 **g**reen, F3 **m**agenta.
- **RGB** — WHITE gray, F1 blue, F2 green, F3 red.

CGM is the default on purpose. Cyan/green/magenta are all easy to tell apart for the common
forms of colour blindness, and — more usefully — where two of them overlap you get a colour that
is obviously neither parent. Red and green overlapping just give yellow, which looks like a
fourth dye. Any channel colour can be changed individually if you need to match a figure.

---

## 5. Combining channels: screen, additive, max

When two labels sit in the same place, the two coloured layers have to be combined into one
pixel. Three rules are available; the default is screen.

**Screen** — `result = 1 − (1 − a)(1 − b)`. Physically this is two lights shining on the same
spot: two half-bright layers make something brighter than either, but never more than fully
bright. Overlaps stay *coloured*, so you can still see that two things overlap. This is the
default because it is the only one of the three that handles overlap honestly.

**Additive** — the two layers are added and anything over full brightness is clipped. Faithful
at low intensities, but any real overlap saturates to white and you lose the information that
there were two channels there. This was the behaviour of the earliest version of this tool.

**Maximum** — whichever channel is brighter wins that pixel, outright. No blending at all. Use
it when you want to be sure a colour you see comes from one channel only and is not a mixture.

A concrete case: two channels at 90 % brightness in the same pixel. Additive shows pure white.
Screen shows a bright but still clearly tinted colour. Maximum shows the brighter channel's
colour, unchanged.

> **Note on overlap.** All three of these are *display* operations on spatially overlapping
> structures. None of them corrects spectral bleed-through — a dye leaking into a neighbouring
> channel's filter. That needs crosstalk subtraction, which this version does not do.

---

## 6. The WHITE channel

WHITE is the transmitted-light image (brightfield or phase contrast). It plays two different
roles and gets different treatment in each.

**As its own movie**, it is stretched from its 0.5th to its 99.5th percentile — a straightforward
contrast stretch that uses the full range without letting a few dead pixels set the limits.

**As an underlay in the composite**, it is dimmed and laid *under* the fluorescence, so it gives
you context (where the cells are, where the channel walls are) without competing with the signal.
Two presets, chosen automatically from the median brightness of the WHITE plane:

| Preset | Chosen when | Gamma | Weight |
|---|---|---|---|
| **Phase contrast** | median ≤ 128 (a dark image with bright edges) | 1.0 | 0.6 |
| **Brightfield** | median > 128 (a bright, evenly lit image) | 2.0 | 0.5 |

Brightfield gets gamma 2.0 because a brightfield image is mostly bright: without it, the
background would flood the composite. Phase contrast is already mostly dark with bright outlines,
so it needs no gamma — just dimming.

The **weight slider** is yours to move, and it is the control you will reach for most:

- **0.0** — no underlay at all; identical to the fluorescence-only composite.
- **0.3** — a hint of context; fluorescence dominates completely.
- **0.6** (phase default) — structure clearly readable, fluorescence still obviously on top.
- **0.9** — the transmitted image competes with the fluorescence; usually too much.

The automatic preset can always be overridden, and the weight you set is saved in the profile.

Two things WHITE never does: it is never included in the fluorescence-only composite, and it is
never mixed into the montage panels or the measurements.

---

## 7. Rolling-ball background subtraction

Off by default. Turn it on (Display panel, radius in pixels, default 50) and each **fluorescence**
channel gets a smoothly varying background estimated and subtracted before the contrast stretch.
WHITE is never touched.

The idea, which comes from ImageJ: imagine rolling a ball of the given radius along the
underside of the image, treating intensity as height. The surface the ball traces out is the
background. Anything the ball cannot reach into — anything smaller than the ball — is signal and
survives.

**What it removes**

- uneven illumination across the field,
- a general haze or fog from out-of-focus fluorescence,
- a slowly varying autofluorescent background, such as a glowing well or gel.

**What it does not remove**

- spectral bleed-through from another channel,
- camera read noise or shot noise (it removes the *slope*, not the *grain*),
- anything about the same size as the ball or larger — which is the trap below.

**Choosing the radius.** The rule is simple: *the radius must be clearly larger than the objects
you want to keep*. Set it too small and the ball climbs into your own structures and hollows them
out. On the Insphero spheroid, radius 50 does remove the autofluorescent well completely — but it
also eats the spheroid, leaving only a speckled shell, because the spheroid is about 200 px
across and the ball is 100 px. At radius 150 the well is still gone and the spheroid survives
intact. If your objects look hollow or ring-shaped after switching it on, the radius is too small.

**Rolling ball versus Background cut.** They fix the same symptom differently. Background cut
raises the black point globally, so a background that is bright *in one corner only* is either
still visible there or clipped everywhere else. Rolling ball removes the background wherever it
is, corner or centre, and leaves the black point free. Rolling ball costs a little processing
time and can distort large objects; Background cut is free and cannot. Try Background cut first,
and reach for the rolling ball when the background is uneven across the field.

**Lumaview's boxes are filled first (0.6).** Lumaview's clock box is exact black. Left in the
image, it pulls the estimated background down around itself, and subtracting that lower background
left a bright band beside the box in 0.5. The overlay pixels are now filled with the median
background before the ball is rolled, and the band is gone.

**Measurements are unaffected.** Rolling ball is a display setting like everything else on this
page. The quantification is computed from the raw planes.

---

## 8. Preview and export are equivalent, not identical

The live preview renders downscaled copies of your frames (640 px) so that dragging a slider
updates instantly; the export renders at the video width. The bounds, the contrast stretch, the
gamma, the colours and the blending are *identical* in both — what you see is what you get. Very
small bright specks look a little dimmer in the 640 px preview than in a 1900 px video, because
shrinking an image averages them with their surroundings; the mapping itself is the same. Since 0.6
both are computed in 8-bit arithmetic (one lookup table per channel and colour, then the blend),
which matches the exact floating-point formulas to within a gray level or two and is several
times faster at 1900 px.

The one thing that cannot be identical is the rolling-ball background, because the ball is
rolled on a smaller grid in the preview (the radius is scaled down by the same factor). The
estimated background therefore differs by a fraction of a gray level in smooth areas and a little
more at hard edges. It is a visual match, not a pixel-for-pixel match. If you are tuning a
radius close to the size of your objects, confirm it on the exported movie.

---

## 9. What never changes

| Output | Display settings applied? |
|---|---|
| Per-channel videos | yes |
| Composite with WHITE | yes |
| Fluorescence-only composite | yes (WHITE excluded) |
| Montages and fixed-image panels | yes (0.6; they were raw before) |
| Quantification CSV, areas, intensities | **no — raw** |
| Dashboard numbers | **no — raw** |

Every run folder also contains `display_profile.json`, the exact settings used, so any movie can
be reproduced or the same look applied to another experiment.
