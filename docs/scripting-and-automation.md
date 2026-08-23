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
| `miainwoodpecker.broker` | Arbitration, for when your program is not the only one on the instrument: `InstrumentBroker` with two verbs — *watch*, which cannot move the probe, and *lease*, which is the only way to acquire. Needed as soon as a notebook, a dashboard, the viewer or an agent share one microscope. |
| `miainwoodpecker.storage` | Files and sessions: `Session`, `write_frames`/`read_frames`, per-axis calibration, and the legacy `.ndata` importer. |
| `miainwoodpecker.analysis` | One-line loaders into HyperSpy, LiberTEM, and py4DSTEM. |

A `Frame` — a numpy array plus a timestamp and metadata — is the
currency between all of them.

Two things about the device connection worth knowing before you build
on it:

- **One driver per device.** The device protocol is strictly one
  request at a time, so don't share a camera or scanner between threads
  — the [viewer](using-the-viewer.md#when-something-says-busy-or-try-again)
  enforces the same rule with its "busy" messages. When more than one
  *program* wants the instrument, that rule is what the broker exists to
  keep: see [driving an instrument that other people are also
  using](#driving-an-instrument-that-other-people-are-also-using).
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

## Driving an instrument that other people are also using

Everything above assumes your script is the only program on the
microscope. Often it is not: a notebook and a dashboard, an operator at
the viewer while an agent runs the sweep, a second screen in the control
room. `remote_instrument` gives each of them its own driver, and that is
the one arrangement the device layer cannot survive.

The protocol is strictly one request at a time, and the shared-memory
transport reuses one segment per source *because* of that — the server
cannot publish frame N+1 until the client has copied frame N out. Two
clients on one device therefore do not merely take turns badly. They
interleave on a reused buffer and produce a frame that is half pass N and
half pass N+1, with nothing raised anywhere; the recording simply
contains a torn frame. (That is [architecture review
§1.2](architecture-review.md), which is where it was found.)

`miainwoodpecker.broker` is where the one-driver rule lives once there is
more than one program. One process holds the device session and every
live loop; everybody else is a client of it, and gets exactly two verbs —
**watch** and **lease**.

### Starting one, and connecting to it

```
miainwoodpecker-broker --publish ./instrument
```

It opens a device session exactly as the examples above do — `--backend`,
`--plugin` and `--server-module` are the same choices `remote_instrument`
takes, and the default is the simulator rather than hardware — then
serves it to however many clients connect. `--publish` writes
`instrument/broker.json`: host, port, and a generated shared secret. The
port is normally the OS's to choose, so it cannot be agreed in advance
and has to be published instead; without `--publish` the key is logged
once, to that terminal, and written nowhere. The listener binds
`localhost`; `--host` will bind it wherever you ask, which puts an
instrument's controls on the network, so that is a thing to do knowingly
rather than by default.

Those three flags describe *one* device server, which is enough for the
simulator and for a single-adapter instrument. A microscope with two —
a Nion column plus a DECTRIS detector on the spectrometer, say — is
described by a file instead:

```
miainwoodpecker-broker --config instruments/superstem-3.toml --publish ./instrument
```

The broker then starts every adapter that file enumerates, checks each
one served the hardware the file says the microscope has, and serves the
lot as one instrument. See
[the instrument configuration](instrument-configuration.md); the four
worked examples live in `instruments/`.

```python
from miainwoodpecker.broker.invitation import BrokerInvitation
from miainwoodpecker.broker.remote import connect_broker

invitation = BrokerInvitation.read_from("./instrument")   # or the file itself
broker = connect_broker(invitation.address(), invitation.authkey)
```

That is the whole connection step, from a notebook kernel, a dashboard's
back end or an agent's process. Stopping the broker parks the instrument
whichever way you stop it — Ctrl-C, a supervisor's `SIGTERM`, Windows
Ctrl-Break — because the default disposition of the last two would
terminate the interpreter without ever reaching the park.

### Watching cannot move the probe

```python
for name, state in broker.targets().items():
    print(name, state.kind, "live" if state.is_live else "idle", state.error)

frame = broker.latest("scanner")        # None until the first pass lands
print(broker.controls()["defocus"])     # read-only, from the broker's own polling
```

`targets`, `describe`, `latest`, `latest_frames`, `stats` and `controls`
read what the broker already has. They cost no device call, they cannot
start or stop anything, and that is the point: a caller asking what is on
screen must not be able to move the probe by asking. A dashboard tile is
made entirely of these.

Two shapes to expect. `latest` returns None when no frame has arrived
yet, which is an ordinary state a tile renders every time a loop starts;
`stats` *raises* `NotLiveError` when nothing is running, because a zeroed
`LiveStats` would read as "running at 0 fps", which is exactly what a
stalled loop looks like. And a stopped loop still answers `latest` with
its last frame — stopping the scan is not a reason to blank the screen,
and it is what makes somebody else's lease invisible to a watcher instead
of a flicker to nothing and back. Whether the picture is still *advancing*
is `TargetState.is_live`'s job.

`snapshot()` answers state and pixels for every target in one entry to
the broker, which is what an in-process display tick should call: asking
`targets()` and then `latest_frames()` per source re-takes each loop's
own lock once per call, against the worker that is reacquiring it on
every grab. Over a socket it also ships every target's pixels on every
call, so a *remote* dashboard should ask `targets()` for the chrome and
`latest()` for the one source it is showing.

### A lease is the only way to acquire

```python
from miainwoodpecker.acquisition import focal_series, record
from miainwoodpecker.devices import ScanParameters

parameters = ScanParameters(height=256, width=256, pixel_time_us=1.0, fov_nm=15.0)

with broker.lease("scanner", "instrument", reason="focal series, 5 steps") as leased:
    record(
        focal_series(
            leased.scanner(),
            parameters,
            [-40.0, -20.0, 0.0, 20.0, 40.0],
            instrument=leased.instrument,
        ),
        "focal.nxs",
    )
```

That is [the focal series from earlier](#a-scripted-experiment-focal-series)
with two lines changed, and it is not a coincidence. A lease yields the
*same* `Camera`, `Scanner` and `InstrumentController` objects
`remote_instrument` hands you, so every generator on this page, every
`Session` recording and every analysis bridge works inside one unchanged.
The broker decides *who* may call, never *what* they may call — the
moment it grows acquisition verbs of its own there are two acquisition
APIs to keep in step, and the property that the viewer is built *on* this
API rather than beside it is gone.

Three habits follow from the shape:

- **Name everything the work needs in one lease.** `focal_series` moves
  the defocus and scans, so it wants `instrument` and `scanner` together.
  Two nested leases are how two clients that ask in opposite orders
  deadlock; the broker takes them in its own fixed order instead, with
  the scanner acquired last and released first, so the probe stands
  parked for the grant itself rather than for the negotiation of
  everything else.
- **Say which device when the lease holds two of a kind** —
  `leased.camera("eels_camera")`. `leased.camera()` means "the only one"
  and refuses rather than guessing, because guessing between a Ronchigram
  and an EELS camera is how a recording ends up labelled as the wrong
  detector.
- **Consume inside the block.** These are generators; one built in the
  `with` and consumed after it is a generator whose lease is gone.

### Taking a lease can block for as long as a scan pass

Granting one means stopping each target's live loop and waiting out the
pass *already in flight*, and a pass is height × width × dwell: 0.26 s at
512×512 and 1 µs, but **42 s** at 2048×2048 and 10 µs, and nearly three
minutes at 4096×4096. So `timeout_s` (5 s by default) is a **floor**
under the wait rather than a ceiling over it — the broker can see the
geometry the loop is running and derives the real deadline from it. A
fixed five seconds would refuse every lease on exactly the instruments
this project exists for, forever, with "still finishing a scan — try
again".

The consequence is for anything with a UI: **do not take a lease on a
thread that has to stay responsive.** Take it the way the viewer records
— on a worker, reporting progress — not inside a click handler. That is
not tidiness, it is the viewer's own fixed bug: stopping the scan on the
GUI thread turned a long scan into "still busy, try again" instead of a
wait.

A lease also expires on its own, 300 s after it was granted, unless
somebody renews it. That is not a nicety either: a notebook kernel that
dies mid-lease would otherwise hold the beam indefinitely, with the
process that would have released it gone. Nothing renews for you, so
renew as the work arrives rather than guessing a long time to live up
front:

```python
from miainwoodpecker.acquisition import scan_series

with broker.lease("scanner", reason="drift series") as leased:
    for frame in scan_series(leased.scanner(), parameters, 1000):
        leased.renew()
        ...
```

A thousand frames outlive any fixed deadline, and renewing per frame
means a job that wedges stops renewing and lets the broker take the
instrument back — which is what a time to live is for. A call made
through an expired lease raises `LeaseExpiredError` rather than driving a
device that may already be somebody else's.

### A paused live loop is always restarted on release

Every loop the lease stopped is started again when the block exits —
including when it exits by exception, and including when the lease
expired rather than ended. There is no flag to suppress that, and the
reason is dose rather than convenience.

The beam is on regardless. It is a separate control, outside this
software and outside DigitalMicrograph, so a scan that is not scanning is
not an idle instrument: it is a stationary probe putting the whole dose
into one spot. Scanning spreads it. **This is a deliberate divergence
from DigitalMicrograph**, where stopping the view leaves the probe
standing and it is on the operator to know that. If what you want is the
beam off, that is `set_beam_blanked(blanked=True)` — a control, not a
side effect of stopping a display.

The same fact is why `stop_live` is rarely what you want on a scan unit,
and why `reconfigure_live` exists at all: changing a running scan's field
of view, dwell or enabled detectors takes effect on the next pass, rather
than costing a stop and a start with a parked probe in between.

### Contention is refused, not queued

```python
from miainwoodpecker.broker import DeviceBusyError

try:
    with broker.lease("scanner", reason="quick check") as leased:
        ...
except DeviceBusyError:
    held = broker.targets()["scanner"].lease
    print(f"{held.holder} has it: {held.reason}")
```

There is no queue, and that is a decision rather than an omission. A
queue invites two clients to each believe they are next, and a lease has
no bounded duration for a queue to reason about — the honest answer is
who holds it and what for. The exception carries a message; the *data* is
on `TargetState.lease`, which is why `reason` is worth filling in: an
operator reads "energy series, 5 steps" rather than "busy".

`DeviceBusyError` covers a second case too, and the message says which,
because your response differs: the target's own worker would not finish
in time, meaning an exposure is genuinely still in flight. Either way
nothing is left stopped — a lease is granted whole or refused whole, and
every loop already stopped for the attempt is restarted before the
refusal is raised. Leaving the scan dark because the camera could not be
had would park the probe in exchange for nothing.

### Whoever builds a broker closes it

`broker.close()` on a client closes the connection, and the server
releases whatever that connection still held — at once, rather than
leaving the probe parked for up to five minutes over a socket that is
demonstrably gone.

Closing a client is not closing the broker. `close()` on the broker
itself stops every live loop and parks the instrument, and only the
program that *built* it should call that: one client leaving must not end
the session for the others, blank their beam, or stop their live view.
`miainwoodpecker-broker` does it for you on the way out. If you build a
`LocalBroker` yourself — embedding one in your own program, or in a test
— it is yours to close, and forgetting has already cost once: a test that
handed its broker to a window and then closed only the window leaked a
live loop that kept a fake scanner spinning flat out for the rest of the
session, which read as the whole suite becoming slow rather than as a
failure.

One gap worth knowing about rather than discovering: `miainwoodpecker-viewer`
opens its own device session and builds its own broker, so it cannot yet
be pointed at a broker somebody else started. A window that shares an
instrument is assembled through the API today —
`LiveInstrumentWidget(..., broker=broker)` — not from the command line.

### A live dashboard in the browser

`notebooks/instrument_dashboard.py` is a [marimo](https://marimo.io) app
built on everything above: a fixed grid of live tiles for every frame
source the broker serves, and one Acquire action that takes a lease.
Install the `dashboard` extra, start a broker, and run it:

```
pip install -e ".[dashboard]"
miainwoodpecker-broker --publish .              # one terminal
marimo run notebooks/instrument_dashboard.py    # another
```

`marimo edit` instead of `marimo run` opens the same file with the code
visible, which is what you want while changing it. A marimo notebook *is*
a Python module — cells are functions and the dependency graph is derived
from them — so it diffs, lints and reviews like the rest of the tree, and
there are no stale outputs stored in it.

The notebook finds the broker by itself: the path you type, else
`$MIAINWOODPECKER_BROKER`, else `./broker.json`. If it finds none it says
where it looked and stops. It will **not** fall back to opening its own
device session, because that would be a second driver on the instrument —
the exact interleaving the broker exists to prevent.

Three things about it are worth knowing before you change it:

- **Every tile is a watch.** The poll is one `snapshot()` per tick, which
  is one round trip for the whole grid rather than a `targets()` plus a
  `latest()` per source. Over a socket that ships every source's pixels
  every tick — the caveat [above](#watching-cannot-move-the-probe) — so
  the refresh interval *is* the bandwidth, and it is a control on screen.
  Frames are decimated to at most 512 px and sent as inline greyscale
  PNGs; the tile is a preview, and the acquisition path never sees any of
  it.
- **Acquire does not take its lease in the cell you pressed.** It starts
  an `AcquisitionJob`, which takes the lease on a worker thread and
  renews it per frame; the same one-second poll that draws the tiles
  reports how it is going. A cell that leased inline would hold the
  kernel for as long as the pass in flight — up to minutes — and freeze
  every tile at the moment you most want to see them.
- **Results go into an append-only log panel, not into new cells.** A
  marimo cell cannot write cells: the dependency graph is the notebook,
  and a program that rewrites its own graph while running has no defined
  order. Each acquisition adds one entry with a thumbnail, where the
  frames were written, and the first frame's metadata. Refusals are
  entries too — "the scanner was leased by the viewer" is part of what
  happened.

The judgement the app is made of — which targets get a tile and in what
order, what the chrome says, how a frame becomes pixels a browser draws,
what an acquisition records — lives in `miainwoodpecker.dashboard` rather
than in the cells, and the unit suite covers it whether or not marimo is
installed. Leave the detector checkboxes and binning menu built from
`broker.describe()`: a client in another process has no device handle to
read them off, which is what that call exists for.

### Starting the broker and a front end together

`miainwoodpecker-instrument` runs the two in order — broker first, front
end when the invitation appears, broker stopped last — which is worth
knowing about here for one reason beyond convenience: it stops the
broker by **asking**, so the instrument is parked. A supervisor that
killed it would not be, and on Windows that is the easy mistake to make,
since terminating a process there runs no handler at all.

```
miainwoodpecker-instrument --publish .                    # window
miainwoodpecker-instrument --publish . -- marimo run \
    notebooks/instrument_dashboard.py                     # dashboard
miainwoodpecker-instrument --serve --publish .            # neither
```

`--publish .` matters if you want to join from a notebook too: without
it the invitation goes to a temporary directory that is removed when the
session ends. Whatever runs after `--` is given `$MIAINWOODPECKER_BROKER`,
which is where the dashboard already looks.

The third form is the one to use for a script that should not care
whether anybody is watching. `--serve` starts no front end at all and
holds the instrument open until Ctrl-C, so clients — your script, a
window, the dashboard — attach and detach as they like; the other two
end the session when their own front end closes. `pixi run serve` is
that with the vendor stack's environment already chosen.

### The viewer as one of the clients

The Qt window connects to a broker the same way, and is worth knowing
about here because it changes what a script shares the instrument with:

```
miainwoodpecker-viewer --broker .
```

It launches no device server, holds no device handle, and takes its
turn like everything else — so an operator can watch the live view and
adjust the column while your script runs, and neither of you can
interleave on the other's frames. What the window offers is built from
`describe()`, `controls()` and `camera_parameters()`; if you are writing
a device adapter, those three are what decides whether a window opened
against it has controls or only a picture.

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
  device is enforced at the server; contention is refused and named
  rather than queued; a lost connection ends the session instead of
  silently reconnecting to an instrument in an unknown state; `park()`
  puts the instrument in a safe state and runs automatically on shutdown
  — including when the controlling process dies.
- **A human can watch.** Because scripts and the viewer share sessions,
  an operator can keep the [viewer](using-the-viewer.md) open on the
  same session directory and see an agent's recordings appear as they
  are written — and through a broker, any number of watchers can follow
  the live frames without costing the instrument a device call.

An MCP server is not shipped today, and the shape it should take is
sharper than "a thin wrapper" now that the [broker
exists](#driving-an-instrument-that-other-people-are-also-using): **the
unit of containment is the lease.** Give an agent a broker connection
rather than a device session and the division is already drawn. Every
tool that only reads — what is on screen, how fast it is going, what the
defocus is — is a watch call, which cannot move the probe however it is
invoked or however the model was talked into invoking it. Everything that
*can* move the probe happens inside a lease: exclusive, refused rather
than queued when somebody else has it, carrying the agent's identity and
its stated reason where an operator can read them, expiring on its own if
the agent stops renewing, and restarting the live loops on release
whether the agent finished, raised, or vanished.

What that does not give you is preemption, and it is worth saying plainly
before someone relies on it. An operator cannot take the instrument out
of a lease. What they have is the holder and the reason in
`TargetState.lease`, the time to live, and the fact that ending the
agent's connection releases everything it held immediately. For an
unattended agent that makes the time to live the setting that matters —
short, and renewed as work completes, so a wedged agent's hold on the
beam is bounded by its own progress.

If you are experimenting now, the pragmatic route is to let the agent
write and run short Python scripts against this API, against a broker
that an operator started — which keeps the containment above and leaves
an exact, replayable record of what it did.

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
