# Analysis parity: what Swift computes, and what the rest costs

[Phase 4](migration-plan.md) wired three analysis libraries in and left
one item open: *port Swift-specific analyses not already covered
upstream, as small adapter functions.* This page is that audit. It is
deliberately not the port — nothing here is implemented, and the point of
writing it first was to find out whether "small adapter functions" is
even the right shape for the answer.

**It mostly is not.** The single largest finding is a licence fact rather
than a capability one: the library that implements almost all of Swift's
core processing menu is **Apache-2.0, not GPL-3.0**, and this project
already installs it. Nine tenths of what looked like a porting backlog is
a dependency declaration.

**Scope note**, the same one [vendor support](vendor-support.md) opens
with: nothing here is a commitment to build any of it. This is the map.

## The licence position, stated first because it changes the answer

Everything in Nion Swift proper is GPL-3.0, and this project is MIT. The
rule this document works under is the one
[pre-hardware work](pre-hardware-work.md) already established and
[migration plan §6](migration-plan.md) draws: **read their code, port
their behaviour, take none of their text.** Every operation below is
named and described from the outside — what it does, what it takes, what
comes out — and no Swift source, docstring, or algorithm body is
reproduced here or anywhere in this repository.

That boundary is why this page names operations rather than quoting them.
It is also why the next paragraph matters so much.

### niondata is Apache-2.0, and it is the whole core menu

Swift's processing menu is wiring. The computation behind almost every
item in it lives in `niondata` — `nion.data.xdata_1_0`, the `xd.*`
namespace Swift's own computation expressions are written against — and
`niondata` is **Apache-2.0**, as is its only Nion dependency `nionutils`.
Checked on the exact version this project already pins in its `device`
extra, not just on the repository's current master:

```
$ pip install niondata            # into a bare 2-package venv
niondata==15.9.1   License: Apache-2.0
nionutils==4.14.1  License: Apache-2.0
numpy==2.4.6
scipy==1.17.1
```

**Four packages.** Measured the same way [Phase 4](migration-plan.md)
measured the others — HyperSpy ~35, py4DSTEM 65, LiberTEM ~102 — which
makes it by a wide margin the lightest analysis dependency on the table.
It also runs standalone, verified rather than assumed: in exactly that
four-package venv — no Swift, no GUI, no HyperSpy, nothing GPL-3.0 —
wrapping a plain NumPy array in `DataAndMetadata` with `nm` axes and
calling `xd.fft` returns an array whose axes are calibrated `1/nm`, and
`xd.radial_profile` returns a profile that keeps its length unit. That
last detail is the one that matters for costing the adapter below: the
axis bookkeeping this project models as `AxisKind.REAL_SPACE` and
`RECIPROCAL_SPACE` is bookkeeping `niondata` already does, so the
conversion is a mapping rather than a reimplementation.

So for the core menu the honest recommendation is not "port these as
small adapter functions". It is **depend on the Apache-2.0 library that
already implements them**, on the MIT side of the process boundary, and
write adapters only where this project's file format and axis model have
to meet it. That is a smaller and much less risky job than reimplementing
fifty operations, and it does not fork a maintained library.

### The awkward part: the upstream analysis stack is mostly GPL-3.0 too

This audit turned up something adjacent that belongs on the record even
though it is not what it was scoped to find. Checked on PyPI:

| Package | Licence |
|---|---|
| `niondata`, `nionutils` | Apache-2.0 |
| `libertem` 0.16.0 | MIT |
| `hyperspy` 2.4.0 | GPL-3.0 |
| `py4dstem` 0.14.18 | GPL-3.0 |
| `exspy` 0.3.2 (EELS/EDS, see below) | GPL-3.0 |

Two of the three analysis extras this project already ships are GPL-3.0,
and `viewer/live.py` imports them **in the application's own process** —
inside the button handlers, but in-process. That is the same shape §6
went to considerable trouble to avoid for the device layer.

This document does not resolve that, and should not: it is a pre-existing
decision, it predates this audit, and the reasoning that would settle it
(an optional extra the *user* installs is arguably not something this
project distributes as a combined work) is a legal judgement rather than
a technical one. What the audit contributes is that the question now has
teeth, because the recommendations below would add a fourth GPL-3.0
optional import (`exspy`) unless someone decides otherwise. **What would
settle it**: §6 growing a paragraph that says explicitly whether the
process boundary applies to analysis extras or only to the device layer,
with the same directness it applies to `nion.*`. Until then, note that
the ordering of preference falls out for free — Apache-2.0 `niondata`
first, MIT LiberTEM second, GPL-3.0 everything else — and that ordering
happens to match the engineering ordering too.

## How this inventory was taken

Enumerated by reading the actual sources, not the marketing pages.
Swift's own documentation site was unreachable from this environment
(the egress proxy blocks `readthedocs.io`), so the doc sources were read
from the repositories instead — which is the better source anyway, since
the menu is registered in code and the published page describes only a
handful of items.

| Source | Version read | Licence |
|---|---|---|
| [`nion-software/nionswift`](https://github.com/nion-software/nionswift) | 16.18.1 (master) | GPL-3.0 |
| [`nion-software/niondata`](https://github.com/nion-software/niondata) | 15.10.0 (master), 15.9.1 (installed) | Apache-2.0 |
| [`nion-software/eels-analysis`](https://github.com/nion-software/eels-analysis) | 0.6.17 | GPL-3.0 |
| [`nion-software/experimental`](https://github.com/nion-software/experimental) | 0.7.21 | GPL-3.0 |
| [`nion-software/nionswift-instrumentation-kit`](https://github.com/nion-software/nionswift-instrumentation-kit) | 23.8.0 | GPL-3.0 |
| [HyperSpy](https://github.com/hyperspy/hyperspy) | 2.4.0, installed and introspected | GPL-3.0 |
| [eXSpy](https://github.com/hyperspy/exspy) | master; 0.3.2 released | GPL-3.0 |
| [LiberTEM](https://github.com/LiberTEM/LiberTEM) | 0.17.0.dev0 (master) | MIT |
| [py4DSTEM](https://github.com/py4dstem/py4DSTEM) | 0.14.19 (master) | GPL-3.0 |

Where a claim about upstream could be checked by running code rather than
reading it, it was: HyperSpy 2.4.0 is installed in this environment and
its method surface was enumerated directly, which is how the EELS finding
below came out the way it did.

**"True" versus "adjacent" below is load-bearing.** True means an
operator would get the same answer from the same inputs. Adjacent means
it is in the neighbourhood and someone will be disappointed — a different
input shape, a different algorithm, or a missing option. An approximate
match asserted as an equivalent is worse than an admitted gap, so
adjacent is used freely.

## What Swift actually offers

Roughly **ninety operator-reachable operations** across four packages,
which sounds worse than it is because the groups differ enormously in how
much is behind them.

### 1. The core Processing menu — 56 operations

Forty-nine named processing actions in `nionswift` itself, five more
registered as processing components (three Fourier windows, mapped sum,
mapped average), plus Redimension and Squeeze on the data menu. Every one
of them is a thin computation expression over one `xd.*` call.

- **Arithmetic and pointwise** — Add, Subtract, Multiply, Divide, Negate,
  Convert to Scalar, Mask, Masked.
- **Spatial filters** — Gaussian, Median, Uniform, Sobel, Laplace.
- **Fourier** — FFT, Inverse FFT, Auto Correlate, Cross Correlate,
  Fourier Filter, Radial Power Spectrum, and Gaussian/Hamming/Hann
  windows.
- **Geometry and resampling** — Crop, Rebin, Resample, Resize, Transpose
  and Flip, Flip Horizontal/Vertical, Rotate Left/Right, Redimension,
  Squeeze.
- **Reductions and spectrum-image navigation** — Projection (Sum), Slice
  Sum, Pick (Point/Sum/Average), Subtract Region Average, Mapped Sum,
  Mapped Average, Line Profile, Radial Profile, Histogram.
- **Sequence and collection** — Measure Shifts, Align Sequence/Collection
  (Fourier), Align Sequence/Collection (Spline 1st Order), Integrate,
  Trim, Extract.
- **RGB** — Make RGB, Extract Red/Green/Blue/Alpha/Luminance.

### 2. EELS — 10 menu items plus a panel

`nionswift-eels-analysis`, a separate GPL-3.0 package: Fit Background,
Map Signal, Map Thickness, Align ZLP by maximum / centre-of-mass / peak
fit, live thickness and live ZLP readouts, Calibrate Spectrum, and
Measure Temperature. The Elemental Mapping panel is its own dock — add an
edge from a periodic table, map the background-subtracted signal for it,
explore, pick, and overlay several element maps as a multi-profile.
Behind them sit a background-model registry (polynomial, two-area, fitted
power law, exponential), a zero-loss peak model, a periodic table with an
edge-onset table, and a hydrogenic cross-section calculation for
quantification.

### 3. Experimental and 4D tools — about 21 operations

`nionswift-experimental`, also GPL-3.0, and the place where most of what
an operator would call "the interesting analyses" live:

- **4D** — Center of Mass 4D, Map 4D, Map 4D RGB, 4D Dark Correction,
  Framewise Dark Correction.
- **Vector-field and phase** — Make iDPC from DPC, Make color COM image.
- **Multi-dimensional processing** — Measure Shifts, Apply Shifts, Crop,
  Integrate Along Axis, Make Tableau, Align Image Sequence, Align
  sequence of multi-dimensional data, Align SI sequence.
- **Sequences** — Split Sequence, Join Sequence(s).
- **Images and spectra** — Double Gaussian, Find Local Maxima, Affine
  Transform Image, Copy Affine Transformation, I·E² Plot.

Worth knowing before costing any of these: the Multi-Dimensional
Processing group's actual computation — measuring and applying shifts
across arbitrary axes, integrating along an axis, building a tableau — is
**in `niondata`**, Apache-2.0, in its `MultiDimensionalProcessing`
module. The GPL-3.0 part is the Swift wizard around it.

### 4. Acquisition-time processing — 5

In `nionswift-instrumentation-kit`, and different in kind because it runs
*during* acquisition rather than on a file afterwards: `sum_project`
(collapse a camera frame to a spectrum on the way in), `sum_masked` (a
virtual detector evaluated per scan position as the scan runs), the drift
tracker (register each frame against the first and predict the drift
rate), MultiAcquire, and the multiple-shift EELS acquisition this project
already has an equivalent of (`energy_offset_series`,
[pre-hardware work §6](pre-hardware-work.md)).

## What upstream already covers

### The core menu

| Swift | Upstream | True or adjacent |
|---|---|---|
| Add, Subtract, Multiply, Divide, Negate | NumPy operators; HyperSpy signal arithmetic | True |
| Gaussian / Median / Uniform / Sobel / Laplace filters | `scipy.ndimage` — what both Swift and HyperSpy call | True |
| FFT, Inverse FFT | `BaseSignal.fft` / `.ifft` | True |
| Hamming / Hann windows | `BaseSignal.apply_apodization(window=...)` — hann, hamming, tukey | True |
| Gaussian window | `scipy.signal.windows.gaussian` | True, but not offered by HyperSpy |
| Auto / Cross Correlate | `scipy.signal.correlate`; `niondata`'s `xd.autocorrelate` | True |
| Crop | HyperSpy `crop` / `crop_signal`, `hs.roi.*` | True |
| Rebin | HyperSpy `rebin` (scale or new shape) | True |
| Resample, Resize | `scipy.ndimage.zoom`, padding | Adjacent — no single upstream call |
| Transpose and Flip | HyperSpy `transpose`, NumPy | True |
| Flip / Rotate | napari layer affine — see "not worth porting" | n/a |
| Redimension, Squeeze | HyperSpy `transpose`, `squeeze` | True |
| Histogram | `get_histogram` | True |
| Projection (Sum), Slice Sum | `sum(axis=...)`, `isig[a:b].sum()` | True |
| Pick (Point/Sum/Average) | `inav[x, y]`, ROI plus `sum`/`mean` | True |
| Subtract Region Average | `s - s.inav[roi].mean()` | True |
| Mapped Sum, Mapped Average | LiberTEM `ApplyMasksUDF`; py4DSTEM `get_virtual_image` | True |
| Line Profile | `hs.roi.Line2DROI(..., linewidth=w)` — including transverse integration | True |
| Radial Profile | py4DSTEM `radial_integral`; `niondata` `xd.radial_profile` | Absent in HyperSpy, true in the other two |
| Radial Power Spectrum | FFT magnitude then a radial profile | Adjacent — a composition, not a call |
| Measure Shifts, Align Sequence (Fourier) | HyperSpy `estimate_shift2D` / `align2D`, `estimate_shift1D` / `align1D` | True |
| Align Sequence (Spline 1st Order) | HyperSpy's sub-pixel interpolation options | Adjacent — different interpolation |
| Integrate, Trim, Extract | `sum` over the navigation axis; `inav[a:b]` | True |
| Mask, Masked | `hs.roi.mask_from_rois` (**PolygonROI only** today); LiberTEM `masks.circular` / `ring` / `rectangular` | Adjacent — see the Fourier-filter gap |
| Fourier Filter | LiberTEM `ApplyFFTMask`; `xd.fourier_mask` | Adjacent — the *mask shapes* are the gap |
| RGB extraction and composition | HyperSpy RGB dtypes; not needed here | n/a — see "not worth porting" |

Read the whole table at once and the shape is clear: **the core menu is
substantially covered, and where it is not, `niondata` covers it under a
permissive licence.** There is no serious argument for reimplementing any
of it.

### EELS

This is where the audit found something that matters more than any
individual gap.

**HyperSpy 2.x no longer contains EELS.** Verified by introspection, not
inferred from release notes: on the installed HyperSpy 2.4.0,
`hs.signals` offers `Signal1D`, `Signal2D`, `BaseSignal` and their
complex and lazy variants — and no `EELSSpectrum`. EELS and EDS moved out
to **eXSpy** at the HyperSpy 2.0 split. Any claim that HyperSpy covers
Swift's EELS menu is a claim about HyperSpy 1.x.

**Since this audit was written, the adapter half of that gap is
closed.** `exspy` is in the `analysis` extra (it arrived with the EDX
work), and `analysis/hyperspy_bridge.py` now has `load_as_eels_signal`:
an EELS camera recording reaches an `exspy.signals.EELSSpectrum` with
its own energy axis, flattened across the spectrometer slit by the same
shared loader an EDX recording uses. What that leaves open is the
*menu-action* half of the estimate below, and the instrument geometry
eXSpy needs for quantification — see "What the adapter can and cannot
tell eXSpy" after the table.

With eXSpy installed the coverage is not merely adequate — it is better
than Swift's:

| Swift (eels-analysis) | Upstream | True or adjacent |
|---|---|---|
| Align ZLP (max / com / peak fit) | eXSpy `align_zero_loss_peak`, `estimate_zero_loss_peak_centre` | True, and it can calibrate the energy origin at the same time |
| Fit Zero Loss Peak, ZLP model | eXSpy ZLP tooling plus HyperSpy model fitting | True |
| Fit Background, Subtract Background (power law, polynomial, exponential) | HyperSpy `Signal1D.remove_background` — PowerLaw, Polynomial, Exponential, Offset, Doniach, Voigt, … | True |
| Two-area background method | HyperSpy fits over one signal range | **Adjacent** — the two-window variant is not a named option |
| Map Thickness, live thickness | eXSpy `estimate_thickness` (log-ratio, and absolute given a mean free path) | True |
| Elemental mapping and quantification | eXSpy `EELSModel` plus `EELSCLEdge` | **True, and strictly better** — see below |
| Calibrate Spectrum | this project already reads dispersion and offset from the device ([pre-hardware work §1](pre-hardware-work.md)) | Subsumed |
| Measure Temperature | nothing found | **Gap** |
| AREELS High Contrast colour map | a napari colormap | Display only |

#### What the adapter can and cannot tell eXSpy

Read from `exspy/signals/eels.py` rather than from memory. eXSpy's EELS
model reads three instrument items, and checks for exactly them in
`_are_microscope_parameters_missing`:

| eXSpy item | unit | from this project's recordings |
|---|---|---|
| `Acquisition_instrument.TEM.beam_energy` | **keV** | `high_tension_v`, ÷1000 |
| `…TEM.beam_current` | **nA** | `beam_current_a`, ×1e9 |
| `…TEM.Detector.EELS.exposure` | s | `exposure_ms`, ÷1000 |
| `…TEM.convergence_angle` | mrad | **nothing records one** |
| `…TEM.Detector.EELS.collection_angle` | mrad | **nothing records one** |

The two semi-angles are the gap, and they are left unset rather than
guessed. The collection angle is set by the spectrometer entrance
aperture and the camera length, which no device here reports. The only
convergence angle anywhere in this stack is usim's `ConvergenceAngle`
control, which appears in **no** Nion package outside the simulator
(checked across the installed `nion.*` tree) — recording it would be
dressing a simulator detail as an instrument convention, the mistake
`nion_server`'s control-name list is careful not to make.

Leaving them absent is also the *safe* state, and that is not a
consolation: eXSpy refuses the operations that need them
(`estimate_thickness(density=…)` raises rather than applying an angular
correction from someone else's geometry), where a plausible wrong angle
would have produced a number that looks like a result. An operator
supplies them per session with eXSpy's own
`signal.set_microscope_parameters(...)`.

**The energy axis is normalized to eV on the way in**, which the EDS
side does not need to do. eXSpy's EDS code validates its axis unit
(`_get_line_energy` takes eV or keV and raises otherwise); its EELS code
checks nowhere while assuming eV everywhere — the ionisation-edge onsets
it matches against the axis are tabulated in eV, `align_zero_loss_peak`'s
subpixel window defaults to ±3 eV, `kramers_kronig_analysis` works in
eV. This project's energy vocabulary also admits meV and keV, so the
exact within-kind conversion happens in the adapter.

The quantification comparison is worth stating plainly because it inverts
the usual direction of these audits. Swift's cross-section code
implements a **K-shell hydrogenic** generalised oscillator strength and
says so in its own control flow — there is no routine for L, M, N or O
shells. eXSpy ships GOSH DFT and Dirac tabulated databases plus
Hartree-Slater and hydrogenic options. Porting Swift's version would be
porting the weaker implementation.

### 4D, DPC, and the experimental tools

| Swift (experimental) | Upstream | True or adjacent |
|---|---|---|
| Center of Mass 4D | LiberTEM `CoMUDF` | True, and better — it also does descan/regression correction and reports divergence and curl |
| Map 4D, Map 4D RGB | LiberTEM `ApplyMasksUDF`; py4DSTEM `get_virtual_image`, `make_detector` | True for the computation; the *interactive* mask placement is not |
| 4D Dark Correction | LiberTEM `corrections`; py4DSTEM `preprocess.darkreference` | True |
| Framewise Dark Correction | as above, per frame | **Adjacent** — the per-frame drifting-offset variant was not found upstream |
| Make iDPC from DPC | py4DSTEM `DPC(...).preprocess(force_com_measured=(x, y))` then `reconstruct()` | **True** — checked specifically: py4DSTEM's DPC accepts pre-computed centre-of-mass images and needs no 4D datacube, which is exactly the segmented-detector case |
| Make color COM image | vector-field colouring | Display only |
| Measure Shifts / Apply Shifts / Integrate Along Axis / Make Tableau | `niondata`'s own `MultiDimensionalProcessing`, Apache-2.0 | True — same code, permissive licence |
| Align Image Sequence, Align sequence of multi-dimensional data | HyperSpy `align2D`; `niondata`'s shift measurement | True |
| Align SI sequence | as above, applied to a spectrum image | Adjacent |
| Split / Join Sequence | HyperSpy `split`, `hs.stack` | True |
| Find Local Maxima | HyperSpy `Signal2D.find_peaks` (scikit-image backends) | True |
| Affine Transform Image, Copy Affine Transformation | `scipy.ndimage.affine_transform`; napari layer affine | True |
| Double Gaussian | nothing found upstream | **Gap** |
| I·E² Plot | one array expression | Not a port |

### Acquisition-time processing

Nothing upstream covers this group, and that is not a gap in HyperSpy,
LiberTEM or py4DSTEM — none of them acquires data. `sum_project` and
`sum_masked` belong to this project's device and acquisition layers, not
to its analysis adapters, and the drift tracker is an instrument-control
feature that happens to use a registration algorithm (`niondata`'s
`xd.register_template`, Apache-2.0). They are listed here for
completeness and then handed to the phase that owns them.

## The genuine gaps, costed

Sizes are engineering days, in the same units and with the same
assumptions [vendor support](vendor-support.md) uses: someone who knows
this codebase, excluding review and the time to find a real dataset to
check against.

### Groundwork — 3–5 days

Not a gap, but everything below is cheaper after it, and it is the
recommendation this whole audit converges on.

| Task | Size |
|---|---|
| `niondata` as an MIT-side optional extra, with `DataAndMetadata` ↔ `FrameCalibration` conversion both ways | 1.5–2 d |
| Route the existing NeXus reader into it, so `xd.*` operates on a recording without a second reader | 0.5–1 d |
| One menu action proving the path, in the shape the other three already have | 1 d |
| Decide and record §6's position on in-process analysis imports | 0.5 d, mostly not engineering |

The calibration conversion is the only real work. `DataAndMetadata`
carries per-axis `Calibration(offset, scale, units)`, and
`storage/calibration.py` already models exactly that with a closed
vocabulary of axis kinds — so the mapping is well defined in both
directions, including the honest uncalibrated case, and it is the same
mapping the HyperSpy adapter already does for `AxesManager`.

### EELS — 4–7 days, of which the first item is now done

| Task | Size | State |
|---|---|---|
| `exspy` as an extra, and an adapter from a recording to `EELSSpectrum` with a real energy axis | 2–3 d | **done** — `load_as_eels_signal` |
| Wire background subtraction and thickness as menu actions | 1–2 d | open, and see below |
| Zero-loss alignment over a series, against `energy_offset_series` output | 1–2 d | the *test* exists; the workflow does not |

This was the largest genuine capability gap in the project, and it was
not a Swift-specific one: it is that the `analysis` extra was chosen
before the HyperSpy 2.0 split and nobody had needed EELS since. The
adapter was indeed small; the decision it forces is the licence one
above, which is still open.

`tests/integration/test_eels_round_trip.py` is the third row's evidence
rather than its workflow: it sweeps the spectrometer with
`energy_offset_series`, loads each step as an `EELSSpectrum`, and shows
eXSpy finding the zero-loss peak at 0 eV in every one while the peak
itself moves 160 channels across the detector. That pins the axis
end to end. What it is *not* is the operator-facing thing the row
describes — assembling a sweep into one wider spectrum, or aligning a
drifting ZLP across a series and summing. Both want a per-frame energy
axis, and a NeXus frame stack carries one calibration for the whole
stack, which is the honest reason the test writes one file per step.

**No viewer button, and this is a decision rather than an omission.**
The three existing analysis buttons act on the viewer's single camera,
and `viewer/app.py::_choose_camera` prefers the **Ronchigram** camera
wherever there is one — so on the default Nion configuration an EELS
button would be pointed at a camera with angular axes and would refuse
every click with "no single energy-calibrated axis". Beyond that, every
EELS operation past loading needs a parameter an operator must choose:
`estimate_thickness` requires a threshold or a ZLP signal (eXSpy raises
without one), background removal requires a fit window, elemental
mapping requires the edges. `align_zero_loss_peak` is the one with
usable defaults, and its result is a recalibrated copy of the input —
nothing to put in a napari layer that the existing mean-projection
button does not already show. A button that ran an arbitrary operation
on the wrong camera would be worse than no button. What would change
the answer: a camera *selector* in the viewer, plus the parameter
widgets, which is the "interactive half" this document already declines
to cost.

### Thermometry — 2–3 days

| Task | Size |
|---|---|
| Energy-gain/energy-loss ratio temperature measurement, from two fitted regions of a spectrum | 2–3 d |

What it does: an EELS spectrum has a small energy-*gain* side mirroring
its energy-loss side, and the ratio of the two is fixed by detailed
balance at the specimen temperature. Swift fits a near region and a far
region, takes the ratio, and solves for temperature.

Who would miss it: anyone doing vibrational EELS on a monochromated
instrument — a small group, and the group most likely to be the reason a
Nion is in the room. Nothing upstream was found: a search of eXSpy for
temperature, detailed balance, and energy gain returned nothing.

Costed as a port of *behaviour*, which is what the licence permits: the
physics is published, and the implementation is a two-region fit and a
ratio. It needs a real vibrational spectrum to check against, which this
project does not have and the simulator will not produce.

### Fourier-filter mask shapes — 2–4 days

| Task | Size |
|---|---|
| Spot, band-pass, wedge, and lattice masks in reciprocal space, with rotation | 2–4 d |

What it does: Swift's Fourier Filter takes its mask from graphics the
operator draws on the FFT — a spot pair, an annular band pass, an angular
wedge, a lattice of spots — and the filtered result updates live as they
drag them. Applying a mask is one call everywhere; **the shapes are the
gap.** LiberTEM's mask library has circular, ring, rectangular and radial
bins, all detector-space; HyperSpy's `mask_from_rois` currently rasterises
polygons only.

Who would miss it: anyone removing periodic noise from a lattice image,
which is routine on a STEM and not obscure.

Deliberately costed as mask *generation* only — pure array functions with
no UI. Making them draggable on a napari FFT layer is viewer work and a
separate item, and it is where most of the value is; without it this is a
scripting convenience.

### Double Gaussian — 1 day

| Task | Size |
|---|---|
| Weighted difference-of-Gaussians filter in Fourier space | 1 d |

A standard STEM image-enhancement filter: two Gaussians in Fourier space
with a relative weight, suppressing both high-frequency noise and
low-frequency contrast. Nothing upstream offers it as a named operation,
though it is a handful of lines over an FFT. Cheap, well defined, and the
kind of thing an operator asks for by name.

### Radial power spectrum — 1–2 days

| Task | Size |
|---|---|
| FFT magnitude, then a radial profile about the centre | 1–2 d |

A composition rather than a gap: both halves exist (`niondata` has
`xd.radial_profile`, py4DSTEM has `radial_integral`), and what is missing
is the composed operation with a sensible centre default. Listed
separately because it is the one operators reach for to judge information
transfer and resolution, and "compose it yourself" is a poor answer at
the microscope.

### Two-area background — 1–2 days, and probably not

| Task | Size |
|---|---|
| Two-window background fit for EELS | 1–2 d |

HyperSpy fits a background over a single range. The two-area method fits
over two separated windows, which behaves differently on edges with
structure between them. Small, but do it only if someone asks — it is a
preference about a method, not a missing capability.

### Not estimable

**The interactive half of everything above.** Swift's real advantage is
not any single operation; it is that a mask, a pick point, or a line
profile is a graphic the operator drags while the result recomputes live.
This project has no computation graph and deliberately does not want
Swift's ([§7](migration-plan.md), and
[pre-hardware work](pre-hardware-work.md) on why the document model was
not portable). Whether the answer is napari layer events, an explicit
recompute button, or nothing at all is a design question nobody has
framed yet, and costing it before it is framed would be a guess. It
should be scoped with an operator in the room during the
[Phase 5](migration-plan.md) pilot.

## What is not worth porting, and why

This list matters as much as the one above.

**All fifty-odd core menu operations, as ports.** Not because they are
unimportant — they are the ones an operator touches hourly — but because
porting is the wrong verb. They are an Apache-2.0 dependency away.
Reimplementing `xd.rebin_image` would be writing a worse version of code
this project is already entitled to call.

**RGB channel extraction and composition** (six operations). This project
records monochrome detector data, and [vendor support](vendor-support.md)
already decided that a colour sensor should deliver its **raw Bayer plane
as 2D** with the CFA pattern in metadata, precisely because demosaicing
invents two thirds of every pixel. Porting operations that split a
demosaiced image back into channels would be tooling for a data model
this project deliberately rejected.

**Flip Horizontal/Vertical, Rotate Left/Right, and affine transforms as
data operations.** In Swift each creates a new data item: a copy of the
data with the pixels moved and the calibration adjusted. In napari the
same thing is a layer affine — free, non-destructive, and already there.
Writing a rotated copy to disk to look at an image sideways is a habit
worth *not* carrying over. The exception is when the rotation is a real
correction (scan rotation against detector orientation), which belongs in
the calibration and metadata rather than in a menu item.

**Snapshot, Duplicate, Generate Data, Assign Variable Reference,
Redimension, Squeeze.** Document-model operations, not analyses. Swift
needs them because a library of live-linked data items needs a way to
break the link; this project's unit is a file on disk, and the
equivalents are `cp` and `numpy.reshape`.

**Calibrate Spectrum.** Subsumed, and by something better: this project
reads dispersion and offset from the device's own calibration controls
([pre-hardware work §1](pre-hardware-work.md)), so a spectrum arrives
calibrated instead of being calibrated by hand afterwards. A manual
override is worth having eventually; a port of Swift's dialog is not.

**Elemental quantification from Swift's cross sections.** Subsumed by a
strictly better upstream implementation, as read above: K-shell
hydrogenic only, against eXSpy's tabulated DFT and Dirac databases.

**The AREELS High Contrast colour map.** A colour map. napari has its own,
and adding one is a registration call, not a port.

**Live thickness and live ZLP readouts, as analyses.** The *computation*
is one line and eXSpy has it. What makes them useful in Swift is that
they update continuously beside a live spectrum — which is a viewer
feature, and it should be built as one, once, in a way that serves any
scalar measurement rather than these two specifically.

**`sum_project`, `sum_masked`, and drift tracking.** Real capabilities,
wrong document. They are acquisition-time features that need a
synchronized scan-position/camera-frame mode this project does not have
([Phase 4's py4DSTEM note](migration-plan.md) measures why), so they
belong with that work and not on an analysis-parity list.

## Recommended order

Ordered by what an operator would miss first on a real instrument, which
is the same rule [Phase 5's pilot list](migration-plan.md) uses — not by
what is cheapest or most interesting.

1. **`niondata` as an MIT-side extra, with the calibration conversion**
   (3–5 d). Everything else is smaller afterwards, it is the only
   permissively-licensed option on the page, and it closes the core menu
   in one move rather than fifty. If only one item from this document is
   ever done, this is it.
2. **eXSpy and the EELS chain** (4–7 d, ~2–3 d of it done). The project
   had *no* EELS capability, on an instrument class where EELS is often
   the reason the instrument exists. This was the largest real gap, and
   it was invisible until someone checked what HyperSpy 2.x actually
   contains. `load_as_eels_signal` closes the adapter half; what remains
   is the operator-facing workflow and the instrument geometry above.
3. **Fourier-filter mask shapes** (2–4 d), and the viewer work to make
   them draggable. Lattice imaging is routine; periodic-noise removal is
   routine; the current answer is "write a script".
4. **Radial power spectrum** (1–2 d). The first thing someone checks
   after a tuning change, and currently a composition the operator has to
   assemble.
5. **Double Gaussian** (1 d). Cheap, named, asked for by name.
6. **Thermometry** (2–3 d). Genuinely absent upstream and genuinely
   Swift-specific, but it serves the narrowest group and cannot be
   validated without a vibrational spectrum nobody here has. Do it when
   there is someone to do it *for*, and data to check it against.
7. **Two-area background** (1–2 d). Only on request.

Two things deliberately not in the list. The **interactive recompute
question** outranks items 3–7 in value and is not costed, so it cannot be
ranked against them; it wants an operator and a pilot, not an estimate.
And the **acquisition-time group** is not analysis work at all — it
follows the synchronized acquisition mode, whenever that arrives.

## What is unverified

Stated plainly, because an audit whose facts are guessed is worse than no
audit.

- **Swift's published documentation was never read.** `readthedocs.io` is
  blocked by this environment's egress proxy, so the entire Swift-side
  inventory comes from the repositories: the menu registrations, the
  computation expressions, and the plugins' own registration lists. That
  is the more authoritative source, but it means anything Nion documents
  but does not register in code — a workflow, a keyboard-driven habit, a
  recipe in the scripting guide — is not represented here. What would
  settle it: reading the Processing, Graphics, and Extended Data pages
  from an unblocked network.
- **No claim here is based on using Swift.** This is a source-level
  capability audit. Which of these fifty-odd operations an operator
  actually reaches for daily is exactly the question
  [Phase 5](migration-plan.md) reserves for a usage audit, and this
  document is not a substitute for it. The ordering above is a considered
  guess at what would be missed first; a week beside a real operator
  would beat it.
- **Framewise Dark Correction has no confirmed upstream equivalent.**
  Plain dark-reference subtraction is covered by both LiberTEM and
  py4DSTEM. The per-frame variant — tracking a drifting dark level frame
  by frame — was not found in either, but "not found" here means a
  targeted search of module and function names, not an exhaustive read.
  What would settle it: reading LiberTEM's correction set and py4DSTEM's
  `preprocess.darkreference` in full.
- **Nothing about pyxem was checked.** It is the obvious place for
  azimuthal and radial integration and several 4D operations, and it is
  not one of this project's extras, so it was left out of scope rather
  than assessed. If radial and polar work grows past items 3 and 4 above,
  pyxem should be evaluated before anything is written.
- **The two-area background comparison is a reading of HyperSpy's
  `remove_background` signature**, not a numerical comparison against
  Swift's implementation on the same spectrum. It is called *adjacent*
  for that reason.
- **The licence facts are metadata, not legal advice.** `niondata` and
  `nionutils` declare Apache-2.0 in their PyPI metadata, their repository
  licence files, and the installed distribution metadata of the exact
  version this project pins — three places, checked. That is strong
  evidence about the licence and no evidence at all about whether any
  particular arrangement complies with it. §6's question about in-process
  analysis imports stays open, and should be answered by someone
  qualified to answer it.
