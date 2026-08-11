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
behave. Those are portable, and several
of them describe behaviour this project currently does not have or does
not check.

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

## The work, in priority order

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

Built as `nion_server._camera_calibration_metadata`: the server resolves
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

### 2. Attach the metadata a frame is supposed to carry

Measured, not assumed: a scan frame from this project carries four
metadata keys, and a camera frame carries two — `frame_number` and
`integration_count`, whatever the simulator happened to put in
`properties`.

Nion has two tests whose entire purpose is to enumerate what must be
there: `test_context_scan_attaches_required_metadata` and
`test_acquire_attaches_required_metadata`. Between them they require the
hardware source id and name, high tension, defocus, and — per device —
binning and exposure, or channel id/name/index, fov, rotation, pixel
time, line time, frame index, and a per-acquisition `scan_id`.

All of it is readable from the simulator today: `EHT` returns 100000.0 V,
`C10` returns 5e−07 m, `BeamCurrent` returns 2e−10 A. NXem has homes for
the instrument values, so this is also the missing content for the NeXus
groups the writer currently leaves thin.

Two of these are worth calling out as more than bookkeeping. A
per-acquisition `scan_id` shared by every channel of one scan is what
makes multi-channel data reassemblable after the fact. And `frame_index`
is the only thing that makes a dropped frame *visible* in a recording
rather than silently absent.

### 3. Exposure and binning control

Absent from the device interface, and §7 already says they want doing
*with* calibration rather than after it. The reason is now precise:
`build_calibration` takes `relative_scale=binning`, so binning multiplies
the calibration scale. Adding binning without wiring it into item 1
produces frames whose axes are wrong by an integer factor.

Contracts to port: `test_changing_binning_is_reflected_in_new_acquisition`,
`test_record_acquires_properly_binned_data`,
`test_first_view_uses_correct_exposure`,
`test_view_followed_by_frame_uses_correct_exposure`, and
`test_changing_frame_parameters_during_view_does_not_affect_current_acquisition`
— the last being the one that says a parameter change must not tear a
frame in half.

### 4. Two frame-identity contracts we do not test at all

`test_frame_do_not_change_after_acquisition` holds four frames, checksums
them, acquires more, and asserts the checksums still hold. Read that
against this project's transport: **frames arrive through a single
reused shared-memory segment.** The `_frame_lock` and the copy-out are
what stop a previously-returned frame being overwritten underneath the
caller, and there is currently no test that would fail if either were
removed. This is the cheapest high-value test on the list.

`test_consecutive_frames_have_unique_data` is its complement — successive
frames must actually differ, which catches a stale-buffer read that
returns the same frame twice. Both are worth having, and their comment is
worth stealing too: both seed the RNG, because the test is invalid if the
detector is saturated.

### 5. Failure and recovery across the RPC boundary

`test_exception_during_view_halts_scan`, `test_exception_during_record_halts_scan`,
and `test_able_to_restart_scan_after_exception_scan` say a device error
must stop acquisition, surface, and leave the device restartable.

In-process for them, that is exception propagation. For us it crosses a
socket, so it is a genuinely different question with a genuinely
different answer, and the third test is the one that matters: after a
device raises, is the *server* still usable, or does the client have to
respawn it? `error_type` already crosses the boundary; nothing asserts
what the device does afterwards.

Related and nearly free: `test_big_scan_does_not_prevent_further_playing`.
There is already a 4096² health-check scan in the suite; it checks that
the big scan succeeds, not that the next small one does.

### 6. An energy-offset series, as a worked example

`MultipleShiftEELSAcquire` is a real operator workflow with a real test:
acquire N EELS frames while stepping the spectrometer energy offset,
optionally take a dark reference, cross-correlate, and sum. usim exposes
the control it needs — `ZLPoffset`, currently −20.0.

`acquisition/sequence.py` already has `focal_series`, and this is the
same shape with a different control. It is worth building specifically
because, unlike focal series, **the simulator can actually demonstrate
it**: shifting the zero-loss peak visibly moves the data, so it works as
a documentation example that a reader can run and see. The hardware
checklist notes focal series cannot do that.

### 7. Adopt Nion's session vocabulary

Their scripting documentation names six session fields:
`stem.session.instrument`, `microscopist`, `sample`, `sample_area`,
`site`, and `task`. This project's sidecar has three: sample, user,
notes.

Adopting the names is a small change with two payoffs. `instrument` and
`site` are the facts that make a recording identifiable a year later, and
both map onto NeXus groups the writer already creates. And it moves the
"[does the sidecar earn its place](migration-plan.md)" question onto
firmer ground: a vocabulary someone else maintains is easier to defend
than one invented here.

## Also worth stealing: the shape of their scripting docs

Nion's scripting guide is fifty numbered recipes — "add two data items",
"convert calibrated values", "tag a graphic for later retrieval" — each a
few lines long and each answering a question a user actually has. It is a
better model for [the scripting guide](scripting-and-automation.md)'s
reference half than prose is, and several of the recipes have direct
equivalents here worth writing out: reading and writing calibrations,
finding which recording a derived result came from, and copying session
metadata from one recording to another.

## What this does not include

Sub-scan support (`test_subscan_has_proper_calibrations` and its dozen
siblings) is a real feature with real calibration consequences —
a sub-scan's calibration must carry the *offset* of the sub-region, not
just a smaller field of view. It is left off this list because it is a
feature decision rather than a gap, and it should follow a request from
someone who wants it rather than the existence of a test for it.
