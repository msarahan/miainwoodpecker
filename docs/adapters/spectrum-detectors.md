# Spectrum detectors: the device-layer shape for EDX and its relatives

`docs/vendor-support.md` ends its Bruker section with a deferral:

> Live control would need ESPRIT's scripting environment and a new
> protocol (spectra and maps, not frames), which is a bigger design
> question than a vendor adapter.

This page is that design question, answered, plus the code that answers
it. It is not a Bruker adapter and not an Oxford one — it is the space
either slots into, with a simulator filling it so the space stays honest.

**The hardware this was designed against is real and is two vendors.**
SuperSTEM 2 carries a **Bruker XFlash 6T-100** silicon drift detector
driven by ESPRIT, *alongside* a UHV Enfina EEL spectrometer. SuperSTEM 4
carries an **Oxford Instruments Ultim Extreme** driven by AZtec. So
"simultaneous EDX and EELS" is a workflow on an instrument that exists,
not a hypothetical, and the two vendors have to be first-class rather
than one being the example.

---

## 1. What real EDX detectors expose

### The interchange format is older and better-specified than any SDK

Before looking at either vendor's control API it is worth noting what
they agree on, because it turned out to settle most of the metadata
design. The **EMSA/MAS Standard File Format for Spectral Data Exchange**
defines a spectral header that both vendors export and that HyperSpy
already consumes. Read off RosettaSciIO's `msa` reader
(`rsciio/msa/_api.py`), which maps it item by item onto eXSpy:

| EMSA keyword | meaning | eXSpy item |
|---|---|---|
| `XPERCHAN` | energy per channel | axis `scale` |
| `OFFSET` | energy at channel 0 | axis `offset` |
| `LIVETIME` | counting time | `…Detector.EDS.live_time` |
| `REALTIME` | clock time | `…Detector.EDS.real_time` |
| `AZIMANGLE` | detector azimuth | `…Detector.EDS.azimuth_angle` |
| `ELEVANGLE` | detector elevation | `…Detector.EDS.elevation_angle` |
| `SOLIDANGLE` | collection solid angle | `…Detector.EDS.solid_angle` |
| `FWHMMNKA` | resolution at Mn Kα | `…Detector.EDS.energy_resolution_MnKa` |
| `EDSDET` | detector type | `…Detector.EDS.EDS_det` |
| `BEAMKV` | accelerating voltage | `Acquisition_instrument.TEM.beam_energy` |

That is the entire metadata vocabulary this project's `Spectrum` carries.
It was not designed; it was adopted.

### Bruker: ESPRIT and the QUANTAX API

Bruker publishes a **QUANTAX API** described as "intended for control of
basic acquisition and quantification functions", letting user software
"control spectrometry and imaging hardware, start and stop spectrum and
image acquisition, and quantify acquired or loaded spectra". Two details
from its description shaped the protocol here:

- **Acquisition can be stopped by command at any time, and spectrum
  transfer to a buffer can be initiated independently of running or
  stopped accumulation.** That is a start/stop/read lifecycle with an
  *accumulating* counter behind it, not a frame stream — which is why
  `SpectrumDetector` has `start`/`stop`/`acquire_spectrum` and why the
  contract says only that each spectrum reports the live and real time it
  actually covers, rather than promising independence between calls.
- **Multiple spectrometers operate independently, assigned to separate
  buffers.** Hence a target *tuple* rather than a single name — an
  instrument with an EDX and a WDS spectrometer is ordinary.

ESPRIT's higher-level automation ("Jobs") covers "point spectra, line
scans, maps, HyperMaps, and image acquisition". Those four acquisition
shapes are exactly `NXspectrum`'s `spectrum_0d`/`_1d`/`_2d` plus imaging,
which is a useful independent confirmation that the storage layout is the
right set of cases.

For **offline** Bruker data nothing above is needed: RosettaSciIO's
`bruker` reader handles `bcf` and `spx` directly into a HyperSpy EDS
signal. Its metadata extraction (`gen_detector_node`) reads
`ElevationAngle`, `AzimutAngle`, `RealTime`, `LifeTime` and the detector
type — the EMSA set again — and it builds the energy axis in **keV** from
`CalibAbs`/`CalibLin`. Its HyperMap layout is `(height, width, Energy)`.

### Oxford: AZtec and H5OINA

Oxford's programmatic surface is thinner and more scattered — the public
material covers AZtec's *Point Automation* for unattended stage-position
acquisition and an AZtec3D "interface which allows simultaneous EDS and
EBSD data acquisition & analysis to be initiated from outside of AZtec",
rather than a documented general-purpose control API.

What Oxford *does* publish openly, and it is more useful than an SDK
would be, is **H5OINA** — an HDF5 data-exchange specification maintained
in the open at `github.com/oinanoanalysis/h5oina`. Its EDS layout:

| H5OINA dataset | type | units |
|---|---|---|
| `Spectrum` | int32 `(size, channels)` | raw counts |
| `Channel Width` | float | **electronvolt** |
| `Start Channel` | float | **electronvolt** |
| `Number Channels` | int32 | — |
| `Live Time` / `Real Time` | float | seconds |
| `Detector Elevation` / `Detector Azimuth` | float | **radians** |
| `Detector Serial Number`, `Detector Type Id` | — | — |

Two things follow. First, Oxford stores its energy calibration in **eV**
while Bruker's reader works in **keV** — so a neutral layer has to pick
one, and eV is both Oxford's choice and this project's canonical energy
unit already (`storage/calibration.py`, `InstrumentController.energy_offset_ev`).
Second, H5OINA's spatial layout is *flattened*: `size = width × height`
with each spectrum one row. That is the same energy-last ordering,
reshaped.

**Asymmetry worth recording:** RosettaSciIO has a Bruker reader and, as
of 0.14, **no Oxford/H5OINA reader** (checked directly against the plugin
list). So "just read what the vendor wrote" is a complete answer for
SuperSTEM 2's Bruker and only a partial one for SuperSTEM 4's Oxford,
unless AZtec is asked to export EMSA `.msa`, which RosettaSciIO does read.

### Thermo Fisher, for completeness

Pathfinder is the EDS software on Thermo's own analysers; Velox writes
`emd`, which RosettaSciIO reads (`rsciio/emd/_emd_velox.py`) and which
produces EDS signals. Not present at the target facility, so not
researched further.

### HyperSpy's EDS model — and the version trap

The constraint the recorded data has to satisfy. **In HyperSpy 2.x the
EDS classes are not in HyperSpy.** They moved to **eXSpy** when HyperSpy
split its domain code out. Measured, on hyperspy 2.4.0:

```
>>> hs.print_known_signal_types()
+-------------+---------+------------+---------+
| signal_type | aliases | class name | package |
+-------------+---------+------------+---------+
+-------------+---------+------------+---------+
```

An empty table. So `set_signal_type("EDS_TEM")` on a bare HyperSpy leaves
a plain `Signal1D` **silently** — no error, no warning, just no EDS
behaviour. `exspy` is therefore in this project's `analysis` extra, and
`load_as_eds_signal` raises a message naming it rather than returning
something that looks right.

What eXSpy's `EDSTEMSpectrum` wants (`exspy/signals/eds_tem.py`):

- `Acquisition_instrument.TEM.beam_energy` — **keV**, not volts
- `…TEM.Detector.EDS.live_time`, `.real_time` — seconds
- `…TEM.Detector.EDS.azimuth_angle`, `.elevation_angle` — **degrees**
- `…TEM.Detector.EDS.energy_resolution_MnKa` — eV
- `…TEM.Detector.EDS.solid_angle` — steradians
- `…TEM.Stage.tilt_alpha` — degrees

and, on the signal axis, units of `"eV"` or `"keV"` — anything else and
`_get_line_energy` raises outright. Absent items are filled from eXSpy's
*preferences*, i.e. a default detector geometry for some other
instrument, which is why a recording that omits them is worse than it
looks.

---

## 2. The design decisions

### 2.1 A spectrum is its own type, not a `Frame` with a 1D array

**Argued both ways, then decided.**

*For reuse.* `Frame` already carries data, an aware timestamp, and an
arbitrary metadata dict; `storage/calibration.py` already models an
energy axis kind in eV/meV; the RPC layer, the shared-memory transport,
and the writer all already handle `Frame`. Reusing it would have been
close to free, and `Frame.data`'s own docstring had said "may be 1D for
binned spectra" since Phase 1 — the intent was already there.

*Against.* Three things, and the third is decisive.

1. **A spectrum cannot exist without its energy axis; a frame can exist
   without a calibration.** An uncalibrated image is still an image and
   this project deliberately supports that state (`AxisKind.UNCALIBRATED`
   is first-class). An array of counts with no dispersion is not a
   spectrum — there is nothing to put the counts against. On `Frame` the
   calibration is an optional metadata key; making it a required
   constructor argument is what turns "should have one" into "cannot
   exist without one".
2. **A spectrum image is rank 3**, and `Frame.data` refuses that
   explicitly, for a reason (colour sensors) this change has no business
   overturning.
3. **`FrameCalibration` is exactly two axes named `y` and `x`.** A spot
   spectrum has one axis; a spectrum image has three. Either case would
   have to lie about one of the two, and there is no third option that
   does not amount to rewriting the calibration model — which would touch
   every existing camera and scan recording to serve a case none of them
   has.

The false economy would have been real: `Frame` would have gained a
document-only invariant ("if 1D then the metadata must contain an energy
calibration") that nothing enforces, and `NexusWriter` would have gained
a rank branch selecting between two entirely different NeXus layouts.
Both types end up worse.

**What *is* reused, and it is the right level:** `AxisCalibration` and
`AxisKind` from `storage/calibration.py`, in the storage layer; the
`ScanParameters` type, for a map's geometry; the `fov_size_nm` metadata
key, so a spectrum image and an image of the same region cannot disagree
about their extent; `HIGH_TENSION_V_KEY`; `SharedFrameWriter`'s segment
machinery; and the whole server/serving/remote skeleton.

**What the device type does *not* import.** The energy calibration
crosses as two plain floats (`energy_offset_ev`, `energy_scale_ev`)
rather than as an `AxisCalibration`. That was a deliberate second choice:
typing it would have made `devices/interface.py` import
`storage.calibration`, which via `storage/__init__` imports
`storage.nexus`, which imports `devices.interface` — a genuine circular
import. Beyond the mechanics, plain values in operator units is the
established convention here (`ScanParameters.pixel_time_us`,
`fov_nm`, `CameraParameters.exposure_ms`), and it keeps the out-of-tree
adapter contract at the two imports
`tests/unit/test_out_of_tree_server.py` pins.

**EELS is not affected and must not be.** An EEL spectrometer disperses
onto a 2D detector, so the device produces a `Frame` whose one direction
is energy — which is exactly what `Camera` plus a per-axis
`FrameCalibration.spectrum` describes, and what `nion_server` already
does (reading *which* direction from the device rather than assuming).
`Spectrum` is for detectors that are **natively** 1D. The two converge at
the analysis layer, not the device layer — see §5.

### 2.2 Spot versus map: the detector drives, and that is not the whole answer

`SpectrumDetector` reports `acquisition_modes` — `spot`, `map`, or both —
and a caller consults it before driving one, exactly as it consults
`InstrumentController.available_controls()`. A benchtop XRF head or an
unsynchronised analyser has `spot` and not `map`, and saying so is the
difference between a clean refusal and a plausible cube in which pixel
and spectrum have no relationship.

`acquire_map(ScanParameters)` lives on the **detector**, not the scanner,
because that is where both vendors put it: ESPRIT's HyperMap job and
AZtec's map are instructions to the analyser, which owns the
synchronisation. Taking `ScanParameters` — the scanner's own type — is
what lets storage calibrate the navigation axes through the existing
`FrameCalibration.from_field_size` path.

**How the synchronisation actually works, and that this interface cannot
establish it.** On real hardware it is one of two hardware arrangements:
the analyser drives the column's external scan input (analyser is
master), or the column drives and the analyser advances on a pixel-clock
/ line-sync line (column is master). Nothing in an RPC protocol
establishes either. So `acquire_map` assumes the adapter arranged one out
of band and *requires* it to record which, in `metadata["scan_sync"]`.
A recording that says `"none"` is still a useful recording; one that
silently implied synchronisation it did not have would not be.

### 2.3 Simultaneity: the transport composes, the protocol does not — and this is the big finding

The brief asked whether `rpc.py`'s per-target connections and strictly
synchronous request/response compose with a detector that must be
integrating while the scan runs. The answer has two halves.

**Concurrency: yes, and this is measured.** Each target gets its own
connection, and `serving.accept_loop` gives every connection its own
handler thread. A caller can drive the spectrum detector from one thread
while another drives the scanner; neither blocks the other, and the
detector really can integrate while the scan runs.
`test_the_detector_integrates_while_another_target_is_driven` starts a
deliberately slow acquisition and shows other calls completing during it.
"Strictly synchronous" is a property of one connection, not of the server.

**Correlation: no, and no amount of transport work fixes it.** Two
overlapping calls overlap in *wall-clock time*. On a scanned instrument
what matters is sharing *probe positions*, and nothing here ties two
results to one pass of the probe: no shared trigger, no shared clock, no
identifier.

And the constraint is stronger than "EDX alongside imaging". **A scanned
instrument acquires several signals from one pass as a matter of course**
— HAADF and MAADF arrive together on the Nion columns this project
already drives, and on an EDX-fitted instrument the X-ray spectra come
with them. Serial acquisition is the special case. That makes the current
`Scanner.scan_frame(parameters, channel)` shape — one channel per call —
wrong in production today, not merely limited: a second channel costs a
second pass of dose, lets the specimen drift between them, and makes
DPC/iDPC/centre-of-mass invalid, since those difference segments *at the
same probe position*. (`Frame`'s note declining a `scan_id` on the
premise that "a second channel is a second pass of the beam" reasons from
a false premise and is superseded; correcting it belongs with the
`Scanner` change.)

**So: what is the honest recommendation?**

The device-layer shape in this change is correct for what it claims and
does not have to be redone:

- `acquire_spectrum` is genuinely standalone. A spot spectrum is one
  detector integrating for a live time, exactly as a camera frame is one
  sensor integrating for an exposure. No pass concept is needed and none
  is assumed.
- `acquire_map` models the **vendor-owned map job**, which is a real
  case that both ESPRIT and AZtec implement as a single instruction. It
  is a *device-level primitive*, and it is labelled as one.

What is **not** solvable at this layer, and should not be attempted here:

- a spectrum image collected simultaneously with HAADF, MAADF and EELS
  from one pass;
- multi-channel scanning at all;
- 4D-STEM (`docs/migration-plan.md` §7 already records this as open).

**These are one missing concept, not three.** The unit of acquisition
that is absent is a **pass**: one traversal of the probe over a region
yielding a *set* of correlated outputs — N image channels (2D), 0..M
spectrum images (3D), 0..K camera-per-pixel stacks (4D) — sharing one
scan geometry and one identifier. Nion's own stack already has this shape
(`nion.instrumentation.Acquisition`'s synchronised acquisition, with
`HardwareSourceChannelDescription` entries and a collection shape), which
is both a precedent and a warning: this project twice found that layer
too heavy to stand up outside Swift's process, so adopting the *concept*
without adopting the machinery is the work.

Options, with the recommendation:

| Option | What it costs | Verdict |
|---|---|---|
| **A. Ship the device shape now; design the pass separately.** *(what this change does)* | Spot EDX works end to end today. `acquire_map` serves the vendor-map case and is honest about the rest. | **Recommended.** Nothing here has to be unpicked, and the pass design gets to be informed by the `Scanner` change rather than racing it. |
| **B. Land the pass concept together with the `Scanner` multi-channel change, and defer all spectrum work until then.** | Delays a working spot-EDX path behind a much larger design. | Rejected: spot spectra genuinely do not need it, and blocking them on it would be the "no shape at all" state this task exists to end. |
| **C. Add a `scan_id`/`pass_id` to `Frame` and `Spectrum` now as a correlation hint.** | Cheap. | Rejected, and worth saying why: an id that nothing establishes is a *claim* that two results share a pass. This project has been bitten by exactly that shape of thing before (`probe_position`, which accepted a value, echoed it back, and was silently dropped). `metadata["simultaneous_with"]` is in the vocabulary instead — absent by default, meaning nothing is claimed, and available for an adapter that actually knows. |

**If the pass concept is built, the spectrum side needs nothing new from
this change.** `Spectrum` already carries a rank-3 map with its scan
geometry; what a pass adds is a container grouping it with the image
channels from the same traversal, and `metadata["simultaneous_with"]` is
where a device that knows already says so.

### 2.4 `TARGET_NAMES`: add minimally, do not do the redesign

`docs/vendor-support.md` lists the fixed positional tuple under "What is
still the wrong shape" and proposes replacing it: bind one well-known
port for `instrument`, let the server choose and report the rest through
`describe()`. It also says explicitly that the redesign should land *with
a second column adapter*, against a real device list.

**Decision: add `spectrum_detector` to the tuple; do not do the
redesign.** Reasons:

- An X-ray detector is **not a second column**, so it does not meet the
  stated trigger. Doing the redesign here would be doing it on spec —
  precisely what that section warns against.
- The redesign touches spawn, connect, teardown and roughly a dozen
  tests. Settling a protocol change in the same commit as a new device
  *shape* makes both harder to review and harder to revert
  independently.
- Adding a name is mechanically safe here, and that is checkable rather
  than hoped: every server in this tree reads `len(TARGET_NAMES)` at run
  time (`nargs=len(TARGET_NAMES)`) rather than counting for itself, which
  is also the documented contract for out-of-tree adapters. The name is
  inserted immediately **before** `instrument`, so every existing name
  keeps its argv position and `instrument` stays last — the invariant
  `tests/unit/test_rpc.py` already pins.

Cost of the choice: one unused localhost port per session for servers
that serve no spectrum detector. Measured against the alternative, that
is nothing.

**It does strengthen the case for the redesign**, and that should be
recorded: the tuple is now Nion's device list *plus a detector class Nion
does not have*, which is the clearest statement yet that a fixed list is
the wrong mechanism.

---

## 3. Storage: the NeXus layout is real, and where it goes was measured

### `NXspectrum` is the vocabulary

`NXspectrum` is a NeXus base class (shipped in `pynxtools`) defining
exactly the cases needed:

| group | signal | axes |
|---|---|---|
| `spectrum_0d` | `intensity(n_energy)` | `axis_energy` |
| `stack_0d` | `intensity(n_spc, n_energy)` | `indices_group`, `indices_spectrum`, `axis_energy` |
| `spectrum_1d` | `intensity(n_i, n_energy)` | `axis_i`, `axis_energy` |
| `spectrum_2d` | `intensity(n_j, n_i, n_energy)` | `axis_j`, `axis_i`, `axis_energy` |

with `axis_energy` of NeXus unit category `NX_ENERGY` and the spatial
axes `NX_LENGTH`. Its symbol table states the invariant this project
adopted: energy bins are **"always the fastest dimension"**.

`NXem_eds` also exists but is the wrong thing: it extends `NXprocess` and
describes *indexing results* — identified peaks, element-specific maps,
`atom_types`. It is where a quantification's output belongs, not where an
acquisition's counts belong.

`NXfluo` was checked and **rejected on evidence**: it requires
`NXsource/probe = "x-ray"` (an enumeration with that single value) and an
`NXmonochromator` with a wavelength. Electron-excited EDX has an electron
probe and no monochromator. There is no `NXxrf` in the NeXus definitions
at all.

### Where it goes: measured, because the obvious placement fails

`NXem` documents `NXspectrum` only at
`measurement/eventID*/spectrumID*`. Tested with
`pynxtools.dataconverter.validation.validate_hdf_group_against`:

| file | valid NXem? |
|---|---|
| frame recording (control) | **yes** |
| `NXspectrum` group directly in the `NXentry` | **no** |
| `NXdata` at `entry/data` using `NXspectrum`'s field names, plus an `NXdetector` | **yes** |
| `measurement/eventID1/spectrumID1/spectrum_0d` | **no** — needs `measurement/instrument` to be an `NXem_instrument` carrying `ebeam_column` and `fabrication` |

This is the same result this project already got for `NXebeam_column`
(`storage/nexus.py::_write_source`), and it is settled the same way: the
data goes in the `NXdata` at `entry/data` that `NXem` *does* document,
spelled throughout in `NXspectrum`'s field names (`intensity`,
`axis_energy`, `axis_j`, `axis_i`, `indices_spectrum`). Both halves of
that claim — that this validates, and that the tempting alternative does
not — are asserted in `scripts/validate_nexus_schema.py`, so neither can
rot quietly.

Reaching `NXem`'s own path is *possible* and now precisely costed: an
`NXem_instrument` with `ebeam_column` and `fabrication` under a
`measurement` group, i.e. the entry restructuring `nexus.py` already
declined once for one field. Worth doing when something needs the
`measurement`/`event` hierarchy for its own sake — a pass concept (§2.3)
is exactly such a thing — and not before.

### The detector's own facts go in `NXdetector`, not a JSON blob

| `Spectrum` metadata | `NXdetector` field | units |
|---|---|---|
| `live_time_s` | `count_time` | s |
| `real_time_s` | `real_time` | s |
| (derived) | `dead_time` | s |
| `azimuth_deg` | `azimuthal_angle` | deg |
| `elevation_deg` | `polar_angle` | deg |
| `solid_angle_sr` | `solid_angle` | sr |
| `detector_type` | `type` | — |
| `technique` | `description` | — |
| `device_id` | `local_name` | — |
| `high_tension_v` | `NXsource/voltage` | V |

`count_time` is worth naming: NeXus has no field called "live time", it
has `count_time` documented as *"elapsed actual counting time"* — the
same quantity, and an existing home rather than an invented one.
`energy_resolution_ev` is the one EDS number `NXdetector` has no field
for, so it stays in the per-spectrum JSON with everything else NeXus does
not describe.

A field the detector did not report is **absent**, not zero. A geometry
of zero degrees is a plausible number an absorption correction would
happily use.

### One spectrum is stored as a stack of one

`spectrum_0d` exists and is deliberately not produced. Using it would
make the *rank* of the signal depend on how many spectra an acquisition
happened to yield, so a recording stopped after one would read back
differently from the same recording that managed two. `stack_0d` with
`n_spc = 1` is honest and uniform.

---

## 4. Units: the conversions, each in exactly one place

| quantity | this project | Bruker | Oxford | eXSpy |
|---|---|---|---|---|
| energy calibration | **eV** | keV | eV | eV or keV |
| detector angles | **deg** | deg | **rad** | deg |
| accelerating voltage | **V** | kV | — | **keV** |
| beam current | **A** | — | — | **nA** |

The volts→keV and amps→nA conversions happen once each, in
`analysis/hyperspy_bridge`. The others are an adapter's job at the point
where vendor data enters. This is the same discipline the py4DSTEM
adapter already applies to `1/nm` → `Å⁻¹`, and for the same reason: a
factor loose in the metadata makes every downstream number wrong by
exactly that factor with nothing saying so.

**The beam-current row is there because it was got wrong first.** The
EDS metadata mapping wrote `beam_current_a` straight into
`Acquisition_instrument.TEM.beam_current`, and eXSpy reads that item as
**nanoamps** — `exspy/signals/eds_tem.py`'s dose calculation multiplies
it by 1e-9 to reach coulombs, with the comment saying so. A 200 pA probe
therefore arrived as 2e-10 nA and made every dose a billion times too
small, silently, since neither end range-checks it. Nothing in the test
suite caught it because nothing computed a dose. Exactly the failure
this section exists to prevent, one row short of preventing it.

---

## 5. Where EELS and EDX converge — one loader, not two

The project owner's framing: *"EDX and EELS are fairly similar, except
that EELS typically is collected on a camera device… The EELS signal is
ultimately analyzed as a 1d spectrum. EDX is collected as a 1d
spectrum."*

So:

```
EELS spectrometer → Camera  → Frame (2D, one axis is energy) → NexusWriter
                                                                    ↓
                                                  load_as_hyperspy_spectrum  →  Signal1D
                                                                    ↑
EDX detector  → SpectrumDetector → Spectrum (1D)  → SpectrumWriter
```

`load_as_hyperspy_spectrum` is **one function reading both layouts**,
dispatching on which signal dataset the file actually holds (`data` for a
frame recording, `intensity` for a spectrum recording) rather than on
what the caller believes. The EELS behaviour is unchanged: sum along the
non-dispersive direction, keep the frame axis as navigation in seconds,
refuse when no single axis is energy.

The obvious alternative — a second `load_as_eds_spectrum` beside the
existing one — was rejected: two functions returning the same type from
two layouts diverge the first time either grows an option, and the
convergence is the point.

`load_as_eds_signal` then sits *on top of* that shared path, adding only
the EDS signal type and eXSpy's detector metadata.

`load_as_eels_signal` is that same thin layer for the camera path, and
it now exists: it adds the `EELS` signal type, eXSpy's TEM metadata, and
one thing the EDS side does not need — normalizing the energy axis to
eV, because eXSpy's EELS code assumes eV everywhere and checks nowhere,
while its EDS code validates (`_get_line_energy` raises for any unit but
eV/keV). See `docs/analysis-parity.md` for the item-by-item mapping and
for the two semi-angles nothing here records.

Each **refuses the other's layout**, deliberately. Both end up as 1D
spectra, which is exactly why: once loaded they are indistinguishable,
so nothing downstream would catch an EELS recording wearing `EDS_TEM`
while eXSpy happily fitted X-ray lines to electron energy losses, or an
EDX recording wearing `EELS` while it fitted ionisation edges to X-ray
lines. The question is one the file can answer — `intensity` for a
spectrum recording, `data` for a frame recording — so neither loader has
to ask the caller.

The one case that will need more than the layout to decide is the
projected EELS readout §6 specifies: a camera summing its own
non-dispersive direction produces a 1D frame that belongs in
`SpectrumWriter`'s layout, and would then be an EELS recording wearing
the EDX shape. The evidence to key on already exists — `NXdetector`'s
`description`, written from the spectrum's `technique` metadata — so
that refusal becomes a check of what the recording says it is. Nothing
writes `technique = "EELS"` today, so nothing implements it today.

---

## 6. The adjacent gap: EELS binning is anisotropic and `CameraParameters` is not

The owner: *"binning is usually done vertically to trade off dynamic
range and SNR"* — bin along the non-dispersive axis, leave the dispersive
axis at full resolution. `CameraParameters.binning` is a single `int` and
`Camera.binning_values` returns a sequence of `int`. A scalar cannot
express that.

**This is real, and it is deliberately not fixed in this change. The
reason is a measurement, not scope.** Nion's own camera API — the only
adapter behind `CameraParameters` — has no per-axis binning at all:

```
>>> CameraFrameParameters().as_dict().keys()
exposure_ms, exposure, binning, processing, integration_count, active_masks
```

`binning` is scalar; `get_expected_dimensions(binning)` takes a scalar;
`camera_base.build_calibration(..., binning, ...)` takes a scalar that
multiplies *both* axes' scale. What Nion has instead is
`processing = "sum_project"`, which sums the **whole** non-dispersive
direction and returns a 1D spectrum
(`nion/usim_device/CameraDevice.py`, and Nion's own
`CameraControl_test` notes it is "for sequence/SI only" — not live view).
That is vertical binning taken to its limit, expressed as a readout mode
rather than as a factor.

Consequences of doing it anyway, today:

- A `(y, x)` binning tuple would be a field the only adapter behind it
  **must refuse** — vendor-neutral in name only, which is the exact
  failure `ScanParameters.fov_nm`'s docstring warns about.
- `NionCamera._binning_of(shape)` recovers the binning a frame was
  *actually* taken at by asking the device `get_expected_dimensions(v)`
  for each supported scalar factor and matching the shape. That trick —
  which exists because a camera reconfigured while running finishes the
  frame in flight at the old settings — has no per-axis equivalent to
  query, so it would have to be replaced with division-and-hope on the
  one path where a wrong answer puts an axis wrong by the whole binning
  factor.

**Specification, for whoever takes it:**

1. **Model the readout mode, not a per-axis factor.** Add
   `CameraParameters.readout` with a small closed vocabulary —
   `"image"` (default, current behaviour) and `"projected"` (sum the
   non-dispersive direction) — mapping onto Nion's `processing`. That is
   the operation an EELS operator actually asks for, and it is the one
   the only adapter can honour.
2. **A projected readout produces a 1D `Frame`.** `NexusWriter` refuses
   any rank but 2; `nion_server.calibration_metadata` already handles the
   1D shape ("only the `x` axis is calibrated, as Nion does"). The layout
   now exists — `storage/spectra.py` — so route a projected EELS readout
   into `SpectrumWriter` and it lands in the same `NXspectrum` layout as
   EDX, reaching `load_as_hyperspy_spectrum` with no flattening. That is
   the convergence of §5 moving one layer *down*, and it is the strongest
   argument for doing it this way.
3. **Only then consider a per-axis binning tuple**, and only with a
   second camera adapter behind it that can honour one — Gatan's and
   DECTRIS's cameras do expose per-axis binning. Same standing rule as
   the target-name redesign: land it with the adapter that needs it.
4. **Keep the scalar API working.** `binning: int` stays; a tuple, if it
   ever arrives, arrives as a separate field with the scalar as a
   derived property, and `_binning_of`'s shape-recovery property must be
   preserved or replaced with something that keeps a running camera's
   in-flight frame correctly labelled.

Sizing: **2–4 days** for items 1 and 2 together, including the storage
route and tests. Item 3 is not estimable without the second adapter.

---

## 7. What is here, and how to run it

```python
from miainwoodpecker.devices.remote import remote_instrument
from miainwoodpecker.devices.interface import ScanParameters, SpectrumParameters
from miainwoodpecker.storage.spectra import write_spectra
from miainwoodpecker.analysis.hyperspy_bridge import load_as_eds_signal

with remote_instrument(
    server_module="miainwoodpecker.devices.spectrum_server",
) as scope:
    detector = scope.spectrum_detector
    detector.configure(SpectrumParameters(live_time_s=10.0, channel_count=4096))
    detector.start()
    write_spectra("eds.nxs", [detector.acquire_spectrum()])

signal = load_as_eds_signal("eds.nxs")     # an exspy EDSTEMSpectrum
signal.add_elements(["O", "Si", "Al", "Fe"])
signal.get_lines_intensity()
```

- `devices/interface.py` — `Spectrum`, `SpectrumParameters`,
  `SpectrumDetector`, `SPOT_MODE`/`MAP_MODE`.
- `devices/spectrum_server.py` — three backend names, one of which never
  returns a device. `simulated` synthesises a physically-shaped spectrum
  (Kramers continuum ending at the Duane–Hunt limit, Gaussian lines at
  real energies with Fano/noise widths, Poisson counts, a dead-time
  fraction) and needs **nothing** installed. `replay` opens a recording
  this project wrote, which is how instrument data becomes a fixture that
  runs anywhere. `hardware` is accepted by the parser and then **refused
  with a sentence** — neither ESPRIT's nor AZtec's control library can be
  redistributed here, so there is no in-tree vendor backend.

  Playback is `replay` rather than a flavour of `hardware` on purpose.
  `viewer/app.py` names the two failures its backend selector exists to
  prevent: driving a microscope you meant to simulate, and *believing you
  are on hardware when you are not*. A `hardware` backend that opens a
  file is the second one, and per-spectrum `backend: "replay"` metadata
  does not undo it — by the time anyone reads that metadata the session
  has already happened. So `hardware` refuses even when handed a
  perfectly good recording, and a test pins that.
- `storage/spectra.py` — `SpectrumWriter`, `write_spectra`,
  `read_spectra`.
- `analysis/hyperspy_bridge.py` — `load_as_hyperspy_spectrum` (both
  layouts), `load_as_eds_signal`, `load_as_eels_signal`.

**A real vendor adapter is out-of-tree**, launched with
`remote_instrument(server_module=...)`, exactly as
`tests/unit/test_out_of_tree_server.py` demonstrates. The protocol
plumbing is about eighty lines; everything else is vendor work.

## 8. What could not be resolved without hardware or a vendor conversation

- **Whether ESPRIT's QUANTAX API can be driven out-of-process at all.**
  The public description covers what it does, not how it is reached
  (in-app scripting, COM, TCP?). If it is in-app only, a Bruker adapter
  is a bridge running inside ESPRIT connecting *out* — the Gatan
  topology `docs/vendor-support.md` already identifies as the one case
  the current design does not fit.
- **Whether AZtec exposes any general control API.** Only Point
  Automation and the AZtec3D external interface surfaced. Oxford would
  have to be asked.
- **Which side is scan master on each instrument**, and whether the
  analyser can be slaved to the Nion scan generator at all. This decides
  whether `acquire_map` is reachable on the real hardware or whether
  spot mode is the whole of it.
- **The real detectors' geometry numbers** — azimuth, elevation, solid
  angle, and the as-installed Mn Kα resolution for the XFlash 6T-100 and
  the Ultim Extreme. The simulator's defaults are the published *class*
  of values, not these units' calibration reports.
- **Whether EDX and EELS can physically run together** on SuperSTEM 2
  (dose, dead time, and whether the Enfina's own acquisition blocks the
  scan). The workflow is claimed; the interaction is not documented
  anywhere reachable.
