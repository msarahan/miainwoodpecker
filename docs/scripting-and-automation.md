# Scripting and automation

This is the guide for driving the microscope from code — a script, a
notebook, or an AI agent. Everything the
[viewer](using-the-viewer.md) does with a button exists here as a plain
function call, because the viewer is built *on* this API rather than
beside it. If you would rather work from the screen, start with the
[viewer guide](using-the-viewer.md); the two produce identical files and
can share a session.

All examples run against the simulated microscope, so they work with no
hardware attached (install the `device` extra). On a real instrument,
swap `remote_simulated_instrument()` for
`remote_instrument(backend="hardware")` and nothing else changes.

If you have no microscope but do have a USB microscope or a webcam, that
works too — see [running against a commodity
camera](#running-against-a-commodity-camera) at the end.

## Five minutes: connect, scan, record

```python
from miainwoodpecker.acquisition import record, scan_series
from miainwoodpecker.devices import ScanParameters
from miainwoodpecker.devices.remote import remote_simulated_instrument

with remote_simulated_instrument() as microscope:
    parameters = ScanParameters(
        height=256, width=256, pixel_time_us=1.0, fov_nm=15.0
    )

    # One frame, in hand as a numpy array:
    frame = microscope.scanner.scan_frame(parameters)
    print(frame.data.shape, frame.data.mean())

    # Ten frames, streamed to a NeXus file as they arrive:
    record(scan_series(microscope.scanner, parameters, 10), "series.nxs")
```

The result is a standard NeXus HDF5 file with calibrated axes in
nanometres. Any HDF5 tool can open it; the
[analysis section below](#loading-recordings-into-analysis-tools) opens
it in HyperSpy in one line.

## The building blocks

The API is small, and each layer only depends on the one below it:

| Layer | What it gives you |
|---|---|
| `miainwoodpecker.devices` | The instrument: `Camera` (with exposure and binning), `Scanner`, and `InstrumentController` (stage, defocus, beam blanker, spectrometer energy offset). Vendor-neutral, in operator units — pixels, microseconds, nanometres, electronvolts. |
| `miainwoodpecker.acquisition` | Series as generators: `scan_series`, `multichannel_scan_series`, `camera_series`, `focal_series`, `energy_offset_series`, plus `record()` to stream any of them to disk and `LiveAcquisition` for a latest-frame-wins live loop. |
| `miainwoodpecker.storage` | Files and sessions: `Session`, `write_frames`/`read_frames`, per-axis calibration, and the legacy `.ndata` importer. |
| `miainwoodpecker.analysis` | One-line loaders into HyperSpy, LiberTEM, and py4DSTEM. |

A `Frame` — a numpy array plus a timestamp and metadata — is the
currency between all of them.

Two things about the device connection worth knowing before you build
on it:

- **One driver per device.** The device protocol is strictly one
  request at a time, so don't share a camera or scanner between threads
  — the [viewer](using-the-viewer.md#when-something-says-busy-or-try-again)
  enforces the same rule with its "busy" messages.
- **Sessions don't reconnect.** If the device connection is lost, the
  `with` block is over; open a new one. That is deliberate: a fresh
  connection is a fresh instrument state, and pretending otherwise
  would let a script keep appending to a file from an instrument whose
  settings silently reset.

## A scripted experiment: focal series

Sweeping a parameter and recording what actually happened is one
generator:

```python
from miainwoodpecker.acquisition import focal_series, record
from miainwoodpecker.devices import ScanParameters
from miainwoodpecker.devices.remote import remote_simulated_instrument

with remote_simulated_instrument() as microscope:
    parameters = ScanParameters(height=256, width=256,
                                pixel_time_us=1.0, fov_nm=15.0)
    defocus_steps = [-40.0, -20.0, 0.0, 20.0, 40.0]  # nm
    record(
        focal_series(
            microscope.scanner,
            parameters,
            defocus_steps,
            instrument=microscope.instrument,
        ),
        "focal.nxs",
    )
```

Each frame's metadata records both the requested defocus and the value
read back from the instrument, so the file says what the microscope
*did*, not just what it was asked. The original defocus is restored
afterwards, even if you abandon the series early.

## Two detectors, one pass of the beam

A scanned instrument reads *every* enabled detector out as the probe goes
past, so asking for HAADF and then MAADF as two separate scans costs
twice the dose, takes twice as long, and lets the specimen drift in
between. `scan_frames` asks for both at once:

```python
from miainwoodpecker.acquisition import multichannel_scan_series, record
from miainwoodpecker.devices import ScanParameters
from miainwoodpecker.devices.remote import remote_simulated_instrument

with remote_simulated_instrument() as microscope:
    parameters = ScanParameters(height=256, width=256,
                                pixel_time_us=1.0, fov_nm=15.0)

    # One pass, two frames - HAADF and MAADF, in request order:
    haadf, maadf = microscope.scanner.scan_frames(parameters, [0, 1])
    assert haadf.metadata["scan_pass_id"] == maadf.metadata["scan_pass_id"]

    # Or a series of passes, streamed to disk:
    record(
        multichannel_scan_series(
            microscope.scanner, parameters, 10, channels=(0, 1),
        ),
        "two-channel.nxs",
    )
```

Both frames carry the same `scan_pass_id` and a `simultaneous_channels`
list naming the channels that shared the pass, and both land in the
recording with that metadata — so a difference taken between two frames
of one pass (DPC, iDPC, centre of mass) is a difference at the *same*
probe position, and the file says so. A single-channel `scan_frame`
carries neither key, deliberately: its frame shared a pass with nothing,
and an id claiming otherwise would be a fiction.

Use `scanner.channel_names` to see which channels the instrument has;
asking for one it does not raises `IndexError`.

## A second experiment: stepping the spectrometer

The EELS counterpart has the same shape, and unlike a focal series the
bundled simulator can actually show you the effect — the zero-loss peak
moves across the detector as the offset steps:

```python
from miainwoodpecker.acquisition import energy_offset_series, record
from miainwoodpecker.devices.remote import remote_simulated_instrument

with remote_simulated_instrument() as microscope:
    record(
        energy_offset_series(
            microscope.eels_camera,
            [-40.0, -20.0, 0.0, 20.0, 40.0],   # eV
            instrument=microscope.instrument,
        ),
        "energy-series.nxs",
    )
```

The recorded energy axis follows the sweep on its own, because the
camera resolves its calibration from the same instrument control. This
is the acquisition half of what Nion Swift calls a multiple-shift EELS
acquire; summing and aligning the series afterwards is HyperSpy's job,
and [the analysis section](#loading-recordings-into-analysis-tools) is how you
hand it over.

One thing worth knowing: the camera is stopped and restarted around each
step. A running camera is always a frame ahead, so acquiring straight
after changing a control gives you the *previous* step's data with the
new step's label — a series wrong by one throughout. Restarting costs a
little time and removes the possibility.

## Instrument controls

The controls are directly available too — check `available_controls()`
first, since not every microscope has every control:

```python
instrument = microscope.instrument
instrument.set_defocus_nm(12.5)
instrument.set_stage_position_nm(100.0, -50.0)   # (y, x)
instrument.set_beam_blanked(blanked=True)
instrument.set_energy_offset_ev(-20.0)
instrument.park()   # safe unattended state: blanks the beam if one exists
```

Cameras carry their own two settings, applied together because binning
changes the axis calibration as well as the frame size:

```python
from miainwoodpecker.devices import CameraParameters

camera = microscope.eels_camera
print(camera.binning_values)                # what this detector supports
took = camera.configure(CameraParameters(exposure_ms=40.0, binning=2))
print(took)                                 # what it actually accepted
```

Configure a camera *before* starting it if you need the very first frame
to be at the new settings — a running camera finishes the frame already
in flight first.

## Sessions from scripts

`Session` is the same folder-of-recordings the viewer uses — automatic
collision-proof filenames, context written into every file:

```python
from miainwoodpecker.acquisition import scan_series
from miainwoodpecker.storage.session import Session

session = Session("data/2026-08-11-au-grid",
                  operator="M. Sarahan", sample="Au on C")
recording = session.record(
    scan_series(microscope.scanner, parameters, 10),
    label="scan-haadf",
    note="hole 4, after tilting",
)
print(recording.path, recording.frame_count)
```

The context fields are Nion Swift's own six, so a habit carries over:
`operator` (Swift calls it *microscopist*), `instrument`, `site`,
`sample`, `sample_area`, and `task`, plus free-text `notes`. Fill in what
you know — anything left out is simply absent rather than blank:

```python
session.update_context(instrument="Nion UltraSTEM 200",
                       site="SuperSTEM",
                       sample_area="hole 4, upper left",
                       task="ADF survey before EELS")
```

Four of them land in real NeXus fields, so a file that travels away from
its folder still says what it was: sample and sample area in `NXsample`,
the operator in `NXuser`, the microscope in `NXinstrument`. `site` and
`task` have no NeXus field that means what they mean, so they stay in the
session's own JSON rather than being written into an approximate one.

A script and the viewer can share one session directory — point the
viewer at it and your scripted recordings appear in its file list, and
vice versa. Reading back:

```python
from miainwoodpecker.storage.session import (
    annotate, find_recordings, load_recording, read_session_context,
)

for rec in find_recordings("data"):          # every session under a directory
    print(rec.path.name, rec.frame_count)

loaded = load_recording(recording.path)      # frames + times, memory-bounded
print(read_session_context(recording.path))  # operator/sample/notes back out
annotate(recording.path, "drift visible after frame 6")
```

## Loading recordings into analysis tools

Each adapter is one call from a recording to the library's native
object, with axis calibrations carried over where the library can hold
them:

```python
from miainwoodpecker.analysis.hyperspy_bridge import load_as_hyperspy_signal

signal = load_as_hyperspy_signal("series.nxs")   # hyperspy Signal2D
signal.mean(axis=signal.axes_manager.navigation_axes[0]).plot()
```

```python
from libertem.api import Context
from miainwoodpecker.analysis.libertem_bridge import load_as_libertem_dataset
from libertem.udf.sum import SumUDF

with Context.make_with("inline") as ctx:
    dataset = load_as_libertem_dataset(ctx, "series.nxs")
    result = ctx.run_udf(dataset=dataset, udf=SumUDF())
```

```python
from miainwoodpecker.analysis.py4dstem_bridge import load_as_diffraction_slice

pattern = load_as_diffraction_slice("camera.nxs")  # calibrated DiffractionSlice
```

Spectroscopy has its own two, which add the signal type and the metadata
the quantification models read — an EELS camera recording and an EDX
detector recording, each refusing the other's layout:

```python
from miainwoodpecker.analysis.hyperspy_bridge import (
    load_as_eds_signal,
    load_as_eels_signal,
)

eels = load_as_eels_signal("energy-series.nxs")  # exspy EELSSpectrum, eV axis
eels.estimate_zero_loss_peak_centre()            # where the ZLP actually is
eels.align_zero_loss_peak()                      # and line the series up on it

eds = load_as_eds_signal("eds.nxs")              # exspy EDSTEMSpectrum
```

Both need the `analysis` extra, and say which one if it is missing.
Two things eXSpy wants for EELS quantification are **not** filled in,
because nothing this project records carries them — the convergence and
collection semi-angles. Supply them yourself for the session:

```python
eels.set_microscope_parameters(convergence_angle=30.0, collection_angle=44.0)
```

Leaving them out is not a silent default: eXSpy refuses the operations
that need them (`estimate_thickness(density=...)` raises) rather than
using someone else's geometry. See
[analysis parity](analysis-parity.md) for the full mapping.

Each of those reads the file. If you already have the frames — because
you read them yourself, or the viewer opened the recording — pass them
instead and nothing is read again:

```python
from miainwoodpecker.analysis.hyperspy_bridge import hyperspy_signal_from_frames
from miainwoodpecker.storage import read_frames

frames = read_frames("series.nxs")      # data, frame_time, calibration
signal = hyperspy_signal_from_frames(frames)
```

`hyperspy_spectrum_from_frames`, `libertem_dataset_from_frames(ctx,
frames)`, and `diffraction_slice_from_frames` are the same for the other
three. They take a `FrameStack` rather than a bare array on purpose: the
calibration travels with the data, so a signal cannot end up silently
claiming pixel axes because the axes were left behind. The two names are
separate rather than one function accepting either, so it is visible at
the call site whether a large file is about to be read.

And because the files are plain NeXus HDF5, tools this project has
never heard of can read them too — that is the point of not having a
private format.

## Migrating a Swift library

Existing Nion Swift `.ndata` files convert to one NeXus file without
Swift installed:

```python
from miainwoodpecker.storage import write_frames
from miainwoodpecker.storage.legacy import iter_ndata_directory

write_frames(
    "migrated.nxs",
    iter_ndata_directory("old_swift_library/", skip_unreadable=True),
)
```

`skip_unreadable=True` logs and skips corrupt files instead of stopping
a ten-thousand-file migration at the first one; leave it off to stop
and see exactly which file failed.

## Driving it with an AI agent

There is no separate "AI interface", and that is a design position
rather than a gap: an agent is just another caller of the API on this
page. The properties that make the API comfortable to script are the
same ones that make it safe to hand to an agent —

- **The whole surface is small and typed.** Three device protocols, a
  handful of acquisition generators, a session. An agent (or the MCP
  server wrapping this API for one) has a few dozen functions to
  expose, not an application to puppet.
- **Everything is inspectable data.** Frames are numpy arrays with
  metadata; files are self-describing NeXus that the agent can read
  back to verify what actually happened, including per-frame read-back
  values like the focal-series defocus above.
- **The dangerous paths refuse rather than misbehave.** One driver per
  device is enforced at the server; a lost connection ends the session
  instead of silently reconnecting to an instrument in an unknown
  state; `park()` puts the instrument in a safe state and runs
  automatically on shutdown — including when the controlling process
  dies.
- **A human can watch.** Because scripts and the viewer share sessions,
  an operator can keep the [viewer](using-the-viewer.md) open on the
  same session directory and see an agent's recordings appear as they
  are written.

An MCP server is not shipped today. When one is wanted, it is a thin
wrapper: expose `remote_instrument`, the acquisition generators, and
`Session` as tools, and the contracts above do the rest. If you are
experimenting now, the pragmatic route is to let the agent write and
run short Python scripts against this API — which also leaves an exact,
replayable record of what it did.

## Running against a commodity camera

A USB microscope, a webcam, or a recorded video file can drive this whole
stack — the client, the session layer, the storage and the viewer — with
no microscope involved. It is the cheapest way to exercise everything
against real hardware, and it needs only the `camera` extra:

```python
from miainwoodpecker.acquisition import camera_series, record
from miainwoodpecker.devices.remote import remote_instrument

with remote_instrument(
    server_module="miainwoodpecker.devices.camera_server",
    backend="hardware",
    plugin_names=["0"],        # camera index, /dev/video0, or a video file
) as scope:
    record(camera_series(scope.camera, 20), "usb-scope.nxs")
```

Drop `backend=` and `plugin_names=` to get a synthetic camera instead,
which needs nothing installed at all — useful for trying the API out.

Three things are different from a scientific detector, and the files say
so rather than hiding it:

- Every frame carries **`photometrically_linear: False`**. A UVC camera's
  pixels have already been through demosaicing, gamma, white balance and
  in-camera sharpening, and none of that is recoverable — so they are an
  image, not a measurement, and an analysis step can check before
  treating them as counts.
- Colour frames arrive **greyscale**, with `colour_conversion` naming
  what was done. `Frame.data` is 2D by design.
- **Binning is refused.** Consumer sensors crop rather than bin, so
  asking for any factor but 1 raises instead of quietly returning
  unbinned frames with a wrongly scaled axis.

The camera arrives as `scope.camera` — the neutral target — rather than
`scope.ronchigram_camera`, because calling a USB microscope a Ronchigram
camera would put a fiction in every file.

## API reference

The generated [API reference](autoapi/index) documents every public
module; the docstrings there include the design reasoning behind each
piece.
