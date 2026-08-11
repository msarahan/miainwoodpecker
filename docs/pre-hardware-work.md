# Work that does not need the instrument

[The hardware checklist](hardware-validation-checklist.md) says what
*cannot* be settled without a microscope. This is the other side of that
line: what is worth doing before then, and where the specification for it
already exists.

The short answer to "where does the specification come from" is that Nion
already wrote it down, as tests. `nionswift-instrumentation-kit` ships
about 8,900 lines of acquisition tests, and a minority of them — the ones
that assert something about a *device*, rather than about Swift's
document model — are statements about how a STEM acquisition layer must
behave. Those are portable, and several of them described behaviour this
project did not have or did not check.

**All seven items below are now done.** The page is kept as the record of
what was built and why, and of the three §7 open questions the work
closed — two of them by removal rather than by answering. Each item ends
with what the simulator taught, since that turned out to be more than
expected: usim publishes real calibration values, real instrument state,
and a spectrometer control whose effect on the data is visible.

## Reading their tests without taking their code

Everything referenced below is GPL-3.0. What gets ported is the
*behaviour being asserted*, rewritten against this project's own API —
not the test code, and not the implementation. That is the same boundary
[the migration plan §6](migration-plan.md) draws everywhere else: Nion's
code runs in the device server subprocess, and Nion's *ideas about what
correct means* are free to inform anything.

One important consequence, and it is a happy one: where a mechanism is
genuinely Nion's (the calibration machinery below), we do not reimplement
it at all. The server already imports `nion.*`. It can call the real
thing and return plain data across the RPC boundary, which is both less
code and less risk than a clean-room copy.

## What is portable, and what is not

Their acquisition tests split cleanly in three.

**Not portable — most of the volume.** Anything reaching through
`DocumentController`, `DisplayPanel`, `DataItem`, or the computation
graph. `test_deleting_probe_graphic_after_one_frame_acquisition_should_disable_positioned`
is a real contract, but it is a contract about Swift's UI object graph,
which this project deliberately does not have.

**Not portable yet — synchronized acquisition.** All 1,400 lines of
`SynchronizedAcquisition_test.py`, and the 4D-STEM work behind it, need a
`ScanHardwareSource` registered with the `STEMController` — the full
`HardwareSource`/`Application` layer that this project has twice found
too heavy to stand up outside Swift's own process
([migration plan §7](migration-plan.md)). Nothing here changes that.

**Portable — the device-layer contracts.** Scattered through
`ScanControl_test.py`, `CameraControl_test.py`, and
`HardwareSource_test.py` are tests asserting things about frames,
metadata, calibration, and error recovery that are true of any STEM
acquisition layer, ours included. Those are the source for everything
below; each item names the specific ones.

## The work, in the order it was done

### 1. Feed calibration from the instrument — **done**

This closes the largest open item in
[migration plan §7](migration-plan.md): "calibration exists as a model but
nothing feeds it from the instrument."

The model in `storage/calibration.py` is fine. What is missing is the
mechanism to populate it, and Nion's is on disk already. A camera device
publishes a `calibration_controls` mapping that names *instrument control
names* rather than values, and `camera_base.build_calibration` resolves
them against the instrument at acquisition time. The simulator publishes
real ones:

| camera | control | value read from usim |
|---|---|---|
| Ronchigram | `ronchigram_x_scale` | 9.831930e-05 rad/px |
| Ronchigram | `ronchigram_x_offset` | −0.1006790 rad |
| EELS | `eels_x_scale` | 0.5 eV/channel |
| EELS | `eels_x_offset` | −20.0 eV |
| EELS | `eels_y_scale` / units | 1.0, units `""` |

Three things fall out of that table, none of which needed hardware to
learn:

- The Ronchigram offset is the axis centred on the optic axis
  (2048 × 9.83e−05 / 2 = 0.1007). Here the offset *control* already
  returns the centred value, and `build_calibration` separately supports
  an `x_origin_override: "center"` key for devices that leave it to the
  caller — two routes to the same convention, which is good evidence
  that `calibration.py`'s hand-derived centring rule is the vendor's
  rule rather than an inference from one simulator.
- The EELS *y* axis reports empty units, which is how the device says
  "this axis is not calibrated". That is exactly
  `AxisKind.UNCALIBRATED`, arrived at independently.
- **It makes `dispersive_axis="x"` a fallback instead of an
  assumption.** §7 records that default as "grounded in the simulator
  only" and puts confirming it on the hardware checklist. It does not
  have to be: the dispersive axis is the one whose units the *device*
  reports as `eV`. Read it, and a rotated spectrometer is handled
  without a configuration change — with the current default kept only
  for devices that publish no controls.

Built as `NionCamera.calibration_metadata`: the server resolves
the controls with Nion's own `build_calibration` and puts per-axis
`{kind, scale, offset, units}` into the frame metadata as plain data,
where `resolve_frame_calibration` already looks. No `nion.*` crosses the
boundary.

Ported contracts: `test_ronchigram_calibrations` and
`test_eels_calibrations` (an acquired frame ends up with `rad` and `eV`
axes), and `test_calibrator_with_missing_controls` — **missing controls
produce an uncalibrated axis, not an error**, the failure mode that
matters more than the success one. Two deliberate divergences: an axis is
centred on its own length rather than the other axis's (Nion passes
`data_shape[1]` for `y` and `data_shape[0]` for `x`, invisible on a square
sensor), and units outside this project's closed vocabulary degrade to
pixels rather than being written as something nothing can interpret.

### 2. Attach the metadata a frame is supposed to carry — **done**

Measured before: a scan frame carried four metadata keys, and a camera
frame carried two — `frame_number` and `integration_count`, whatever the
simulator happened to put in `properties`.

Nion has two tests whose entire purpose is to enumerate what must be
there: `test_context_scan_attaches_required_metadata` and
`test_acquire_attaches_required_metadata`. The field *set* is now
adopted; the *names* are not, because putting `stem.scan.fov_nm` in the
vendor-neutral layer would be a vendor's schema wearing neutral clothing.
The vocabulary is documented on `Frame` and read entirely from the
instrument: `EHT` → 100000.0 V, `C10` → 500 nm, `BeamCurrent` → 2e−10 A,
plus the scan's rotation, centre, flyback, and derived line and frame
times.

Three decisions came out of building it.

- **Instrument state is read per frame, not cached at connect.** Three
  `TryGetVal` calls, measured at 4.6 µs, against a `focal_series` that
  changes defocus *between* frames — a cached value would label every
  frame in a sweep with the first one's defocus, wrong in exactly the
  workflow that needs it most.
- **No `scan_id`.** Nion's groups the channels of one simultaneous
  multi-channel scan. This interface has no such call — a second channel
  is a second `scan_frame`, and therefore a second pass of the beam — so
  an id claiming to group them would be a fiction. `frame_index` is per
  device, gapless from zero, which is what makes a dropped frame
  *visible* rather than silently absent.
- **The accelerating voltage gets a real NeXus home; the rest does not.**
  `NXsource.voltage` inside the existing `NXinstrument`, measured against
  the schema validator. NXem's own path for it
  (`measurement/eventID/instrument/ebeam_column/electron_source`) does
  **not** work here and it is worth recording why: `NXinstrument`
  documents no electron column, so an `NXebeam_column` inside one makes
  the file stop validating, and NXem's entry has no `instrument` group to
  move to instead. Reaching NXem's path means restructuring the entry
  around its `measurement`/`event` hierarchy — a bigger change than one
  field justifies. Everything else stays in the per-frame JSON, which is
  what that column is for.

### 3. Exposure and binning control — **done**

§7 said these wanted doing *with* calibration rather than after it, and
the reason turned out to be mechanical: `build_calibration` takes
`relative_scale=binning`, so binning multiplies the calibration scale.

`CameraParameters(exposure_ms, binning)` is a value object for the same
reason `ScanParameters` is — the two settings must change together to
stay coherent — and `configure` returns what the device *took* rather
than echoing the request.

Two things the simulator taught, both now load-bearing:

- **usim's `validate_frame_parameters` validates nothing.** Measured: it
  returns `binning=3` unchanged on a camera advertising `[1, 2, 4, 8]`.
  So an unsupported factor is refused here, naming what the camera
  accepts, rather than rounded to a neighbour — a caller asking for 3 has
  a bug, and silently giving them 2 makes every axis wrong by a third.
- **A camera configured while running finishes the frame in flight at the
  old settings.** That is Nion's
  `test_changing_frame_parameters_during_view_does_not_affect_current_acquisition`,
  and it has teeth here that it does not have there: labelling that frame
  with the new binning would put an axis on stored data wrong by the whole
  factor. So the binning a frame reports is recovered from its *shape*
  via the device's own `get_expected_dimensions`, not from the setting.
  Configuring a stopped camera has the first frame already correct, which
  is the path to use when it matters.

### 4. Two frame-identity contracts nothing was testing — **done**

`test_frame_do_not_change_after_acquisition` holds four frames, checksums
them, acquires more, and asserts the checksums still hold. Read that
against this project's transport: **frames arrive through a single
reused shared-memory segment.** The `_frame_lock` and the copy-out are
what stop a previously-returned frame being overwritten underneath the
caller, and nothing in the suite would have failed if the copy were
removed. This was the cheapest high-value test on the list.

`test_consecutive_frames_have_unique_data` is its complement — successive
frames must actually differ, which catches a stale-buffer read that
returns the same frame twice.

Both were verified by removing the copy: with `view.copy()` replaced by
`view`, both fail. The second fails for its own reason, which is why it
is worth having separately — aliased views make every frame identical,
the frozen-image failure a checksum test cannot see.

### 5. Failure and recovery across the RPC boundary — **done**

`test_exception_during_view_halts_scan`, `test_exception_during_record_halts_scan`,
and `test_able_to_restart_scan_after_exception_scan` say a device error
must stop acquisition, surface, and leave the device restartable.

In-process for them, that is exception propagation. For us it crosses a
socket, so it is a genuinely different question with a genuinely
different answer, and the third test is the one that matters. Both halves
are now asserted, using a scan of a channel that does not exist — a plain
operator mistake, which raises `IndexError` inside Nion's own scan
device: the caller learns *what* failed rather than getting one
indistinguishable `RemoteCallError`, the very next scan on the same
device and connection succeeds, and the server still answers a health
check while the other devices keep acquiring. Without the recovery half,
every bad argument would cost a session — and with it the instrument
controls, leaving the column unparked.

`test_big_scan_does_not_prevent_further_playing` came with the frame
identity tests above.

### 6. An energy-offset series, as a worked example — **done**

`MultipleShiftEELSAcquire` is a real operator workflow with a real test:
acquire N EELS frames while stepping the spectrometer energy offset,
optionally take a dark reference, cross-correlate, and sum. usim exposes
the control it needs — `ZLPoffset`, currently −20.0.

`energy_offset_series` is the same shape as `focal_series` with a
different control, and it earns its place for the reason predicted: the
simulator really does demonstrate it, so the docs example is one a reader
can run and watch the zero-loss peak move.

Building it turned up a bug that would have made the whole thing wrong,
and it generalises past this one function. **A running camera is always a
frame ahead.** Acquiring immediately after setting a control returns a
frame generated *before* the change, while its metadata and energy axis
describe the new one — so a series built the obvious way is mislabelled
by one step throughout, which is worse than not having it. Stopping the
camera around each step makes the next frame correct; both behaviours
measured. Nion's `MultipleShiftEELSAcquire` takes a settling delay
between shifts for the same underlying reason.

The energy offset is the fourth control on `InstrumentController`, and it
arrived the way that module says controls should: with the caller that
needed it, not before.

### 7. Adopt Nion's session vocabulary — **done**

Their scripting documentation names six session fields:
`stem.session.instrument`, `microscopist`, `sample`, `sample_area`,
`site`, and `task`. This project's sidecar has three: sample, user,
notes.

All six are now there, with two deliberate divergences. `microscopist`
stays `operator` — the same field in plainer English, and what the
viewer, the sidecar files already on disk, and this API all already say.
And `notes` is ours: Nion has no free-text session field, and `NXnote` is
the obvious home for one.

The payoff is partly that these are the facts making a recording
identifiable a year later, and partly that it moves the
"[does the sidecar earn its place](migration-plan.md)" question onto
firmer ground: a vocabulary someone else maintains is easier to defend
than one invented here.

**Four of the six get real NeXus fields, and two do not.** Sample and
sample area map onto `NXsample`'s `name` and `description`, the operator
onto `NXuser/name`, and the microscope onto `NXinstrument/name` —
`NXinstrument`'s only defined field, which had been sitting empty. All
four are schema-checked in CI. `site` and `task` have no NeXus field that
means what they mean (`NXuser/affiliation` is who the *operator* belongs
to, not where the microscope is), so they stay in the session JSON rather
than being written into an approximate one — the same rule this project
applies to an uncalibrated axis.

## Also worth stealing: the shape of their scripting docs

Nion's scripting guide is fifty numbered recipes — "add two data items",
"convert calibrated values", "tag a graphic for later retrieval" — each a
few lines long and each answering a question a user actually has. It is a
better model for [the scripting guide](scripting-and-automation.md)'s
reference half than prose is, and several of the recipes have direct
equivalents here worth writing out: reading and writing calibrations,
finding which recording a derived result came from, and copying session
metadata from one recording to another.

## What this does not include, and what is left

Sub-scan support (`test_subscan_has_proper_calibrations` and its dozen
siblings) is a real feature with real calibration consequences —
a sub-scan's calibration must carry the *offset* of the sub-region, not
just a smaller field of view. It is left off this list because it is a
feature decision rather than a gap, and it should follow a request from
someone who wants it rather than the existence of a test for it.

Two things the work above did *not* close, both from
[§7](migration-plan.md), and both now the operator-facing half of what
used to be a plumbing problem:

- **No UI selects a microscope mode, or the camera settings.** Exposure,
  binning, and the energy offset are all reachable from code and all
  recorded per frame; nothing in the viewer exposes them, so an operator
  still needs a script. That is a viewer change, not a device one.
- **A mode the device does not describe still needs code.** The
  calibration path reads what the camera publishes; a microscope mode
  that changes what an axis *means* without changing the controls behind
  it has nowhere to say so.

And one recurring lesson worth carrying to hardware day, because it
appeared twice from different directions: **a running camera is always a
frame ahead.** Changing binning or an instrument control while a camera
is live leaves the frame in flight at the old settings. Binning is
recoverable from the frame's own shape, so frames stay honest about it;
exposure and instrument state are not, which is why `energy_offset_series`
stops the camera around each step and why anything that needs a frame
taken at known settings should configure before starting.
