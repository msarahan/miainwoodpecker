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
| `miainwoodpecker.devices` | The instrument: `Camera`, `Scanner`, and `InstrumentController` (stage, defocus, beam blanker). Vendor-neutral, in operator units — pixels, microseconds, nanometres. |
| `miainwoodpecker.acquisition` | Series as generators: `scan_series`, `camera_series`, `focal_series`, plus `record()` to stream any of them to disk and `LiveAcquisition` for a latest-frame-wins live loop. |
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

The instrument controls are directly available too — check
`available_controls()` first, since not every microscope has every
control:

```python
instrument = microscope.instrument
instrument.set_defocus_nm(12.5)
instrument.set_stage_position_nm(100.0, -50.0)   # (y, x)
instrument.set_beam_blanked(blanked=True)
instrument.park()   # safe unattended state: blanks the beam if one exists
```

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

## API reference

The generated [API reference](autoapi/index) documents every public
module; the docstrings there include the design reasoning behind each
piece.
