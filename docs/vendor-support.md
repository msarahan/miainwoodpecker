# Other vendors: what their SDKs look like, and what adapting one costs

This project's device layer was built vendor-neutral on purpose
([migration plan §2](migration-plan.md)), but only one vendor has ever
been behind it. This page is the check on that claim: what the other
vendors actually expose, what a second adapter would cost, and — the part
that needed code rather than prose — which spaces in the framework turned
out to be the wrong shape.

**Scope note.** Nothing here is a commitment to build any of it. The
migration plan's rule holds: no second vendor adapter until someone has
that instrument. This is the map, not the itinerary.

## The landscape

Three markets, and conflating the first two is the mistake to avoid.
**Column vendors** sell the microscope and control the optics, stage, and
scan. **Detector vendors** sell what hangs off it — usually a different
company, and often reachable without the column vendor's help at all.
**Commodity cameras** are the third: UVC microscopes and camera bodies,
which are real instruments for some purposes and want the same `Camera`
protocol. This project's `Scanner` and `InstrumentController` face the
first; `Camera` faces the other two.

### Column and instrument SDKs

| Vendor | Interface | Language | How you get it | Reachable out-of-process? |
|---|---|---|---|---|
| **Nion** | [nionswift-instrumentation-kit](https://github.com/nion-software/nionswift-instrumentation-kit) | Python | PyPI, GPL-3.0 | Yes — what this project already does |
| **Thermo Fisher** (TEM) | TEM Scripting + Advanced Scripting | COM | Installed with the microscope; [`temscript`](https://github.com/niermann/temscript) (BSD-3) and [`pyTEM`](https://github.com/basf/pyTEM) wrap it in Python | Yes, on Windows |
| **Thermo Fisher** (TEM) | [AutoScript TEM](https://www.thermofisher.com/us/en/home/electron-microscopy/products/software-em-3d-vis/autoscript-tem-software.html) | Python 3.11 | Paid add-on, installed on the instrument | Yes |
| **Thermo Fisher** (SEM/DualBeam) | [AutoScript 4](https://www.thermofisher.com/us/en/home/electron-microscopy/products/software-em-3d-vis/autoscript-4-software.html) | Python | Paid add-on | Yes |
| **JEOL** | [PyJEM](https://www.jeolusa.com/PRODUCTS/Transmission-Electron-Microscopes-TEM/Analytical-Data-Optimization/PyJEM) over TEM_External/TEMCenter | Python | On the TEM PC; [docs are public](https://github.com/PyJEM/PyJEM), package is **confirmed absent from PyPI** | Yes |
| **Zeiss** | [SmartSEM Remote API](https://www.zeiss.com/microscopy/en/products/software/zeiss-smartsem.html) (`CZEMApi.ocx`) | ActiveX/COM | **Requires an agreement with Zeiss** to develop against | Yes, on Windows |
| **Hitachi** | Undocumented Python external control (`MfExtCont`), evidenced on an SU7000 FE-SEM | Python | Not published; on the instrument PC, apparently with sample scripts | Presumed yes — unverified |
| **Bruker** | ESPRIT scripting | In-app | Analyzer software (EDS/EBSD/µXRF), **not a column API** | No |

Three things fall out of that table.

**Thermo Fisher is the cheapest second vendor, by a distance.** The COM
scripting interface ships with the microscope rather than being a paid
add-on, and `temscript` is BSD-3 on PyPI with a documented class map
(`Stage`, `Projection`, `Acquisition`, `CCDCamera`, `STEMDetector`,
`Illumination`) that lines up almost one-for-one with the protocols here.
It also ships a **dummy implementation for offline development**, which
is the thing that made Nion's adapter testable without hardware and would
do the same again.

*(**Verified** — `temscript` 2.1.0's sdist was downloaded from PyPI and
read. All six classes are present in `temscript/_instrument_com.py`; the
licence is BSD-3; the offline dummy is a real `NullMicroscope`, not a
stub interface.)* Reading it turned up something this table did not
know, and it matters more than the confirmation:

> **`temscript` already ships its own out-of-process remoting.** It
> carries a `MicroscopeServer` built on the standard library's
> `http.server`, and a `RemoteMicroscope` client that speaks to it with
> a choice of JSON or PICKLE transport.

That is the same problem this project solves with
`devices/*_server.py`, solved upstream, by the wrapper, permissively
licensed. It bears directly on the 6–10 day Thermo Fisher estimate under
"Task estimates" below: a Thermo adapter
may not need this project's subprocess machinery at all on the
instrument side, and could instead be a client of a server Thermo users
already run. Nobody has costed that variant, and it is not costed here —
but the estimate below assumes work that may be partly done, and should
not be quoted without this paragraph.

**Bruker is not a column vendor.** ESPRIT is EDS/EBSD/µXRF analysis
software that sits *beside* a Zeiss, Thermo, JEOL, or Hitachi column. Its
scripting automates analytical workflows, not the microscope. Adapting it
is not "another `Scanner`" — it is a spectrum/map source that would want
its own protocol, and the honest first question is whether
[RosettaSciIO's `bcf` reader](https://github.com/hyperspy/rosettasciio)
covers the need by reading the files ESPRIT already writes.

**Hitachi publishes nothing, which is not the same as having nothing.**
No manual, no package, no documented surface — but third parties do
drive Hitachi FE-SEMs from Python using modules that arrive on the
instrument, and on a SEM the scan is separately purchasable through the
external scan connector. So the honest description is "undocumented and
probably obtainable", not "absent". See
[adapters/hitachi.md](adapters/hitachi.md).

### Detector SDKs — the `Camera` side

Worth a section of its own rather than a footnote, because on a real
instrument the camera is usually **not** the column vendor's, and going
through the column vendor's interface to reach it is often the harder
path: it adds a licensed intermediary, constrains you to whatever
subset that vendor chose to expose, and breaks when either side updates.
Talking to the detector directly is frequently *less* work, not more.

| Detector | Interface | Language | How you get it |
|---|---|---|---|
| **Direct Electron** (DE-16, Apollo, Celeritas) | [`deapi`](https://github.com/directelectron/deapi) against DE Mission Control / DE-Server | Python | On PyPI and conda; needs Mission Control and a DE detector |
| **DECTRIS** (ARINA, **ELA**, QUADRO, SINGLA — all EIGER2-chip-based) | SIMPLON REST API (HTTP/JSON control, ZeroMQ stream) | any | Published; on the detector control unit itself. **Note the ELA specifically**: it is also sold through Gatan Microscopy Suite as the *Stela* camera, which makes it look like a GMS peripheral. It is not — it speaks SIMPLON directly, and GMS is a second front end. See [DECTRIS](adapters/dectris.md). |
| **Hamamatsu** (ORCA) | [DCAM-API / DCAM-SDK4](https://www.hamamatsu.com/us/en/product/cameras/software/driver-software/dcam-sdk4.html) | C, Windows **and Linux** | Free SDK registration; Python via [`pyDCAM`](https://pypi.org/project/pyDCAM), `pylablib`, or Micro-Manager |
| **Quantum Detectors** (Merlin) | TCP control + data | any | Documented; already wrapped by LiberTEM-live |
| **ASI** (TPX3) | Socket to the ASI software | any | Already wrapped by LiberTEM-live |
| **Gatan** | GMS 3 embedded Python / DM-script | Python inside DM | Ships with GMS — but see below |

Three of these are genuinely easy. **DECTRIS is the easiest interface in
this whole document**: a REST API over HTTP with a documented JSON
schema, no vendor library to install, no Windows requirement, no
in-process anything. **Direct Electron** ships a pip-installable Python
client. **Hamamatsu** is a C SDK, but it is free, has an official Linux
build, and has several maintained Python wrappers.

**Gatan is the exception, but it inverts *ownership*, not direction.**
GMS 3's Python runs inside DigitalMicrograph and, in Gatan's own words,
cannot be executed from outside it — so a Gatan adapter cannot be a
subprocess we launch. It does *not* follow that it must connect out, as
an earlier draft of this page asserted: `gms-socket-plugin` exposes both
`TCPSocketBind` and `TCPSocketConnect` to DM-Script, SerialEM's DM
plug-in has listened inside DM for two decades, and a published
DM-SDK/ZeroMQ bridge already exists (Lei, Weber, Clausen & Wilbrink,
*M&M* **30**(S1), 2024). Direction is a firewall question.
`remote.attached_instrument()` supports both, and
[`devices/gatan_bridge.py`](../src/miainwoodpecker/devices/gatan_bridge.py)
is the reference implementation. See [Gatan](adapters/gatan.md).

**And check Nion first.** A Gatan spectrometer on a Nion column is very
likely reached as Nion's `eels_camera`, which this project already serves
along with the `ZLPoffset` energy offset — supported today, with no Gatan
code at all. That is the SuperSTEM 2 case (UltraSTEM 100 + UHV Enfina),
unverified pending one `describe()` call on the instrument PC.

#### Do not re-plumb high-rate streaming

An ARINA runs to 120 kHz frame rates; an Apollo to thousands per second.
This project's `Camera` is a **pull-per-frame** interface over
synchronous RPC, and that is the right shape for a 10–100 fps survey
camera and the wrong shape for a 4D-STEM stream. Routing a 120 kHz
detector through `acquire_frame()` would be reinventing, badly, something
that exists:
[LiberTEM-live](https://libertem.github.io/LiberTEM-live/) already
supports Merlin, DECTRIS EIGER2, and ASI TPX3, with Gatan K2 IS and
others in progress — and this project already depends on LiberTEM for
analysis.

**Settled, for DECTRIS, and the API drew the line itself.** SIMPLON
publishes two ways to get images out and they are not the same thing: the
**stream** subsystem pushes every frame of a series over ZeroMQ as an
`lz4`/`bslz4` blob and is the recording path (which LiberTEM-live's Rust
receiver already consumes), while the **monitor** subsystem serves the
latest image as a TIFF over HTTP and is explicitly a preview channel that
drops frames by design. The second is pull-per-frame already, so
[`dectris_server`](adapters/dectris.md) is built on it and touches the
stream not at all. An ELA runs to 2250 fps full-frame and past 10 kHz on
a narrow readout; that path is tens of fps and says so in its docstring.
This is not a compromise — configuring a spectrometer while watching the
zero-loss peak is exactly what LiberTEM-live does not do, and a spectrum
image is exactly what a `Camera` should not.

The split is also where the *viewer's* limit lands, measured rather than
assumed: napari's per-update cost is a fixed ~11 ms on an M2 Pro,
independent of frame size (see [Phase 2](migration-plan.md)), so ~85 fps
is a ceiling no frame size changes. Survey-rate sources are nowhere near
it; a high-rate detector is past it before anything else becomes
interesting.

So the recommended split, and the reason it is not a cop-out:

- **Control, configuration, and survey-rate acquisition** → a device
  server implementing `Camera`. Exposure, binning, ROI, gain mode, live
  preview, single shots, focus series.
- **High-rate streaming acquisition** → hand off to LiberTEM-live, whose
  UDFs run unmodified on live and offline data.

Those are complementary rather than competing: the same detector wants
both, and a session that configures through one and streams through the
other is coherent as long as only one of them owns the detector at a
time. That interlock is the design question to settle before building
either, and it is why nothing here is built yet.

#### What a detector-only adapter needs, and now has

A direct detector has **no scan unit**. Until this audit, that did not
work: the cameras were already optional in `remote_instrument`, but
`connections["scanner"]` was not, so a detector-only server died with a
`KeyError` — "vendor-neutral" quietly meant "must have a scan unit shaped
like Nion's". `RemoteInstrumentDevices.scanner` is now `| None`,
`cameras()` enumerates what is actually there, and the live viewer says
so plainly instead of failing three frames deep. Covered by
`tests/unit/test_out_of_tree_server.py`, which drives a detector-only
server end to end.

What such an adapter would still have to work around, honestly:

- **`ScanParameters` is irrelevant to it**, which is fine — it implements
  `Camera` and nothing else.
- **`InstrumentController` may have nothing to control.** A bench camera
  has no stage, defocus, or blanker; `available_controls()` returning an
  empty list is already the supported answer.
- **Detector settings beyond exposure and binning have no home yet.**
  ROI/readout area, gain mode (counting vs integrating), and hardware
  trigger mode are common to all three vendors and absent from
  `CameraParameters`. That is the next field to add, and it should arrive
  the way binning did — with a caller that needs it, and wired into the
  calibration at the same time, since ROI changes the axis offsets.
- **Calibration.** None of these detectors knows the microscope's
  magnification or camera length, so nothing analogous to Nion's
  `calibration_controls` exists. The pixel size is a detector constant
  (physical pitch) plus an optics-dependent scale someone has to supply —
  which is exactly what `FrameCalibration`'s explicit constructors are
  for.

#### Estimates

| Detector | Size | Notes |
|---|---|---|
| ~~**DECTRIS**~~ | **done** | [`devices/dectris_server.py`](../src/miainwoodpecker/devices/dectris_server.py), both backends; the 3–5 d estimate held. ROI/gain/trigger on `CameraParameters` is the outstanding piece — `roi_mode` is where a DECTRIS detector's readout reduction lives, and it is recorded in the metadata but not settable. |
| **Direct Electron** | 4–6 d | `deapi` is pip-installable; needs Mission Control running and a detector to test against |
| **Hamamatsu ORCA** | 5–8 d | C SDK via a Python wrapper; Linux build exists; add ROI and gain mode |
| **Merlin / ASI** | 2–4 d each | Wrap LiberTEM-live's existing connection behind `Camera` |
| **Gatan** | transport **done** (2 d); DM-side 3–5 d | The transport is built (`attached_instrument()`, both socket directions). The remaining vendor work starts by settling whether a Nion column already serves the spectrometer, which would make it zero |
| **ROI / gain / trigger on `CameraParameters`** | 2–3 d | Shared prerequisite for the three direct vendors |
| **LiberTEM-live streaming handoff + ownership interlock** | 3–5 d | The design question above, settled once for all detectors |

### Commodity and consumer cameras

Not every sensor is a research detector. A consumer USB microscope, or a
mirrorless body on a photo port, is a real instrument for alignment,
teaching, documentation, and plenty of light microscopy — and the same
`Camera` protocol fits, with three caveats that matter for treating one
as a *measuring* device rather than a picture-taking one.

**A working one ships in this project.**
`miainwoodpecker.devices.camera_server` is an MIT, in-tree device server
for exactly this class of hardware — no vendor SDK, no licence question,
so there is no reason for it to be out-of-tree:

```python
from miainwoodpecker.devices.remote import remote_instrument

with remote_instrument(
    server_module="miainwoodpecker.devices.camera_server",
    backend="hardware",
    plugin_names=["0"],          # camera index, /dev/video0, or a video file
) as scope:
    frame = scope.camera.acquire_frame()
```

Two backends, for the reason `nion_server` has two: `simulated`
synthesises moving frames and needs **nothing installed**, so the whole
path runs in CI and on a laptop with nothing plugged in; `hardware` opens
a real capture device through OpenCV's `VideoCapture`, which speaks V4L2,
AVFoundation and DirectShow/MSMF — every USB microscope, because they are
all UVC devices pretending to be webcams. Install the `camera` extra for
the second.

A **video file is a first-class device**, which is more useful than it
sounds: it is how a capture becomes a regression fixture, and it is how
the hardware backend is tested here with no camera in the room.

**On macOS, expect the permission dance first.** Camera access is granted
to the application *responsible* for the process — your terminal, not
Python — so a server launched from a shell may never raise the dialog,
and a denied camera can open successfully and then deliver black frames.
Grant your terminal camera access in System Settings ▸ Privacy & Security
▸ Camera, and check pixel values rather than only that frames arrived.
The open-failure message says this; the black-frame case it cannot
detect for you. A Continuity Camera iPhone shows up as another index, and
is a much better sensor than the built-in one.

**One adapter buys hundreds more cameras.**
[`pymmcore`](https://pypi.org/project/pymmcore/) is pip-installable
Python bindings to Micro-Manager's MMCore, with no Java and no GUI, and
Micro-Manager's adapter library includes `OpenCVgrabber` and
`Video4Linux` — which between them are every UVC microscope — alongside
Andor, Basler, PVCAM, Spinnaker, IDS, Allied Vision, Aravis and dozens
more. So the answer to "support commodity USB microscopes" is not one
adapter per device: it is **one device server backed by pymmcore**, which
also picks up a large slice of the scientific camera market for free.

This is also the option that best fits what this project says it is for.
[§4](migration-plan.md) already names pymmcore-plus as prior art worth
studying; wrapping it is adopting that work rather than admiring it.

**A DSLR or mirrorless body is its own small adapter.**
[libgphoto2](https://github.com/gphoto/libgphoto2), through
[`python-gphoto2`](https://github.com/jim-easterbrook/python-gphoto2),
does tethered capture over PTP for a very wide range of bodies, Sony
included, and can retrieve raw files. Two things to know before relying
on it: **live view on Sony bodies is documented-flaky** (several open
gphoto issues about preview freezing or rebooting the camera; the vendor
SDK is the more reliable route for preview but is C++), and tethered raw
capture runs at roughly 1–3 frames per second. That is a stills
instrument, not a live one.

**Three things make a consumer camera different from a detector**, and
all three are the difference between an image and a measurement:

- **Shoot raw or the data is fiction.** A JPEG has gamma, white balance,
  sharpening and lens corrections baked in, so it has no photometric
  linearity and nothing computed from it means anything. `rawpy`/libraw
  reads Sony ARW and most other raw formats.
- **ISO is gain**, which is the same field the direct detectors want on
  `CameraParameters` and which does not exist yet. A pleasing
  convergence: an a7 III and an ORCA are asking for the same addition.
- **Binning is `[1]`.** Consumer bodies crop, they do not bin. Reporting
  a single supported factor is the honest answer, and the interface
  already accommodates it.

**One genuine interface decision, worth making deliberately.**
`Frame.data` is documented as 2D — an RGB frame is `(height, width, 3)`
and does not fit. The resolution this page recommends is *not* to widen
the type: a colour sensor should deliver its **raw Bayer plane as 2D**,
with the CFA pattern in the frame metadata. That fits the existing
interface, and more importantly it is the honest thing to store, because
demosaicing invents two thirds of every pixel and anything measured from
the result is measuring the interpolation. A viewer or an analysis step
can demosaic for display; a recording should not.

#### Estimates

| Target | Size | Notes |
|---|---|---|
| ~~UVC microscopes and webcams~~ | **done** | `devices/camera_server.py`, both backends |
| **pymmcore-backed server** | 4–6 d | Much of the *scientific* camera market in one adapter; UVC is already covered |
| **gphoto DSLR/mirrorless** | 3–4 d | Check first whether Micro-Manager's DSLR adapter already covers the body |
| **Raw decode + CFA metadata** | 1–2 d | The piece that makes a camera body's data measurable |
| **ROI / gain on `CameraParameters`** | 2–3 d | ISO is gain; shared with the direct detectors |

## Transport: why every adapter is a subprocess

The subprocess boundary was built for one reason — keeping GPL-3.0 code
out of an MIT process — and that reason does not apply to most of the
vendors on this page. Several of these SDKs are pure Python, permissively
licensed, and would be simpler to call directly. So the question is fair:
why not let those adapters run in-process, and skip the IPC?

**The recommendation is one production path, and it is the subprocess.**
Not out of consistency for its own sake, but because the boundary turned
out to be earning its keep for four reasons that have nothing to do with
copyright:

- **With the beam on, "the application crashed" must not mean "nobody
  parked the column".** The shutdown handshake, the orphan watchdog and
  the bounded SIGTERM park all exist *because* the device process
  outlives the application's crash. In-process, a repaint bug in the
  viewer leaves a live column unattended. This is the argument that
  decides it.
- **Crash isolation.** A C SDK, a COM object, or a vendor DLL that
  segfaults takes down a process that owns nothing but itself — not the
  session, not the recording in progress, not the UI.
- **Threading.** COM apartment rules, vendor callback threads, and Qt
  owning the application's main thread compose badly. A separate process
  has no shared main thread to fight over.
- **Interpreter version.** AutoScript is pinned to Python 3.11; PyJEM is
  whatever the instrument PC has; Nion pins its own stack. A subprocess
  can run a *different interpreter*. In-process, the vendor chooses your
  Python version — and if two vendors disagree, nobody wins.
- **Remote operation.** The server can run on the instrument's control
  computer with the application on an operator's laptop. Zeiss's API has
  an explicit remote mode; an XP-only control PC may leave no other
  option.

And the cost is small, measured rather than assumed: pickle and shared
memory are within noise of each other from ~30 KB to ~500 KB, with Nagle
disabled and one reused segment
(`scripts/ipc_overhead_benchmark.py`). A call is a socket round trip plus
a memcpy — invisible at 10–100 frames per second. At 120 kHz it is the
wrong architecture, but so is any request/response protocol; that case
belongs to LiberTEM-live, not to an in-process variant of this one.

**This is not the bifurcation it might look like.** The in-process path
already exists and costs nothing: `NionCamera`, `NionScanner` and
`NionInstrument` satisfy the protocols directly, and the test suite
drives the same objects both ways — in-process in
`tests/integration/test_nion_server.py`, over IPC in
`test_remote_nion.py`. An adapter author writes protocol-satisfying
objects and never thinks about transport. What would be a real
bifurcation is *two supported production paths*, with two lifecycle
stories, two error models, two teardown paths, and a "which one are you
on?" question in every bug report. This project has been caught twice by
behaviour that only appeared on one path — the startup hang, and whether
frame metadata survives the shared-memory transport — which is the
argument against having two.

**Where the calculus genuinely differs.** A benchtop camera has no beam
and no column, so the parking argument evaporates entirely, and for a
pure-Python SDK the rest is thinner too. If an in-process production path
is ever wanted, the right shape is not a second architecture: it is a
`local_instrument()` context manager that imports the adapter and yields
the same `InstrumentDevices`, sharing every test through the same
conformance suite. One function, no new concepts. Worth building when
something actually needs it — and worth *not* making the default even
then, because the failure it protects against is one nobody notices until
it matters.

## What the framework got right

Three things, and it is worth being specific because they were not built
for this.

**The subprocess boundary generalises past its original reason.** It
exists to keep GPL-3.0 code out of an MIT process. It happens to be
exactly what a COM/ActiveX SDK wants anyway: COM has apartment threading
rules, `temscript` and SmartSEM are Windows-only, and a vendor SDK that
deadlocks or crashes takes down a process that owns nothing but itself.

**`describe()` already handles instruments with different devices.** The
client connects to the `instrument` target first and only connects to the
device targets it reports. A vendor with a scan unit and one detector —
or a SEM with detectors and no camera at all — works today.

**The frame metadata vocabulary is vendor-neutral by construction.** It
was deliberately *not* Nion's `stem.scan.fov_nm` spelling
([`Frame`](https://github.com/msarahan/miainwoodpecker/blob/main/src/miainwoodpecker/devices/interface.py)),
so a second adapter fills in the same names rather than negotiating a
schema.

## What was the wrong shape, and is now fixed

**The client could only ever launch our own Nion server.**
`_spawn_server` hard-coded `python -m miainwoodpecker.devices.nion_server`,
so a vendor adapter could not be an out-of-tree package — it would have
had to be a fork. `remote_instrument(server_module=...)` now names it, and
the startup diagnostic names the module it failed to launch, since "the
package is not installed in this interpreter" is the realistic first
mistake.

`tests/unit/test_out_of_tree_server.py` writes a complete, vendor-free
device server and drives the whole client against it — command line,
authkey handshake, `describe()`, both device protocols, shutdown — with
no `device` extra installed. It is both the regression test and the
executable specification an adapter writes against, and the measurement
it gives is the useful one: **the protocol plumbing is about a hundred
lines** — the instrument target, an accept loop, a dispatch loop, and a
`main`. Everything beyond that in a real adapter is vendor work.

That number was *eighty* when this paragraph was written, and it has
grown twice since, both times for something a server gained rather than
something the protocol got clumsier about: a `health()` the client can
probe without touching a device, and the `endpoints` map that let the
target list stop being a fixed tuple. It is worth re-measuring rather
than re-quoting — the stand-in server is in the tree, so the count is
checkable.

**`InstrumentController` was all-or-nothing to `isinstance`.**
`available_controls()` exists precisely so an instrument can serve some
controls and not others — a webcam has no defocus, a detector-only server
has no stage — but the protocol was `runtime_checkable`, and a
`runtime_checkable` Protocol's `isinstance` check demands every method
regardless of what the instrument says it supports. Two adapters failed
that check while working perfectly (`camera_server.ServerInstrument` and
`gatan_bridge.BridgeInstrument`), which meant the check was testing for
Nion-shapedness rather than for protocol conformance. Found twice, by two
independent adapters — the signal that it was the abstraction and not the
adapters, and what mapping the `isinstance` call sites confirmed: every
one was asking "is this an instrument target I can hold a session
against", never "does it have a defocus".

The fix splits the protocol along that question. `Instrument` is the
`runtime_checkable` core every instrument target serves — identity
(`stage_size_nm`), capability (`available_controls`), lifecycle (`park`)
— and is what `isinstance` now checks. `InstrumentController` is that
core plus the per-control methods, for static typing, and is
deliberately no longer `runtime_checkable`: `isinstance` against it
raises `TypeError`, so the all-or-nothing question cannot quietly come
back. (A superset by re-declaration rather than subclassing, because
`runtime_checkable` is inherited and has no opt-out; protocols are
structural, so nothing is lost.) Per-control capability is asked through
`available_controls()`, exactly as the sweep generators already did — an
instrument that does not serve a control still gets the graceful "control
not available" refusal, not a call it cannot answer.

Both previously-failing adapters now pass the runtime check, each pinned
by its own test
(`test_gatan_bridge.test_a_one_control_instrument_satisfies_the_instrument_protocol`,
`test_camera_server.test_a_controlless_instrument_satisfies_the_instrument_protocol`),
and the full Nion controller passes it trivially, being a superset. The
widening is pinned from the other side too: `test_gatan_bridge.
test_passing_the_runtime_check_does_not_make_a_missing_control_callable`
runs `focal_series` against the one-control bridge — whose defocus
methods are *absent*, not merely unimplemented — and requires the
documented `ValueError` naming the control, so a skipped capability check
would show up as an `AttributeError` rather than passing quietly.

**`scan_frame` could not express a simultaneous multi-channel scan.** The
most consequential wrong shape this page has carried, and the one that
was not waiting on a second vendor — it was wrong for the instrument this
project already drives. `scan_frame(parameters, channel)` returns one
channel per call, and `Frame`'s docstring declined a `scan_id` on the
grounds that "a second channel is a second pass of the beam". **That
premise is false, and not as an edge case: a scanned instrument gives you
one or more signals simultaneously, always.** One pass of the probe,
every detector reading out at once — HAADF and MAADF together on a Nion
UltraSTEM, and on a segmented-detector SEM such as the SU9000II, BF plus
each HAADF segment plus SE plus LA-BSE plus HA-BSE, all from the same
pass. Simultaneity is what a scanned instrument *is*; serial channels are
the special case, and the API only had the special case. It cost dose
(*k* channels, *k* passes over the specimen), time and drift, and
correctness: DPC, iDPC and centre-of-mass take differences between
segments **at the same probe position**, so segments from different
passes are not merely noisier but invalid, with nothing in the recording
saying so.

`Scanner.scan_frames(parameters, channels)` is the fix, and the shape of
it is the part worth recording. Each returned frame carries
`scan_pass_id` — an opaque id for the one traversal of the specimen they
came from — and `simultaneous_channels`, the siblings that shared it.
**The identity is produced by that call and by nothing else**:
`scan_frame` attaches neither key, so a bare id asserting an acquisition
that did not happen is not reachable, which is what the old refusal was
right to protect against (`probe_position`, again). It is not this
project's invention either: Nion's own `scan_base.ScanAcquisitionTask`
mints a `uuid4` per frame and stamps it on every simultaneously-read
channel as `stem.scan.scan_id`.

The Nion adapter drives the vendor's real mechanism rather than looping —
`set_channel_enabled` for each requested channel, one `start_frame`, and
`read_partial` until complete, which is how `ScanAcquisitionTask` itself
runs and which returns one data element per enabled channel. It then
checks what the device actually did (no bad frame, the frame number did
not move, every requested channel reported) before stamping a shared id
on anything. On the transport, a pass crosses as **one stacked block** in
the source's existing shared segment: the reused-segment design allows
exactly one publish per request/response cycle, so N publishes would
overwrite each other and N segments would duplicate the ownership rules.
Storage needed no change at all — `NexusWriter` already persists each
frame's metadata whole — so a recorded multi-channel series says which
frames shared a pass without a new NeXus layout being invented for it.

What is still missing after this is the **cross-device** pass: a scan
sharing probe positions with a camera or an X-ray detector. That is one
concept, not three (see
[spectrum detectors](adapters/spectrum-detectors.md)), and `scan_frames`
deliberately does not pretend to cover it.

### The target names were a fixed tuple, and positional argv

*Kept with its original diagnosis, because the fix is easier to judge
against the problem it was written for.*

`rpc.TARGET_NAMES = ("ronchigram_camera", "eels_camera", "scanner",
"instrument")` was Nion's device list, and it was *positional argv*: the
client allocated one port per name before it could talk to the server, so
the set could not be discovered. A Thermo Fisher STEM with three
detectors, or a SEM with SE and BSE and no camera, had to map onto those
four names — which works, but means a file's `device_id` is honest while
its target name is a fiction.

The fix is a protocol change, not a rename: bind **one** well-known
port for `instrument`, have the server choose and report the rest through
`describe()`, and let the client connect to what it is told. That removes
the positional-argv fragility as a side effect.

**This is now done, and the trigger was not the one predicted here.**
This section said the redesign should wait for a second *column* adapter,
"when there is a real device list to test it against". The real device
list turned out to be two USB cameras on a desk — a webcam and a USB
microscope — which exercises exactly the same protocol question for the
price of plugging something in, with no beam time and no vendor
conversation. Waiting for a column adapter was waiting for the expensive
version of a cheap test.

What landed:

- `describe()` reports an **`endpoints`** map — target name to `port`,
  `kind` and `label` — so a server can serve targets whose names the
  client could not have allocated a port for.
- Targets the client cannot name bind on **port 0**, which is the OS
  choosing a free ephemeral port. That also removes a race rather than
  only a limitation: a client-allocated port is probed free and bound
  later, and `PORT_UNAVAILABLE_EXIT_STATUS` exists for exactly that
  window. A port assigned at bind time has no such window.
- `camera_server` serves **one camera per `--plugin`**, as `camera`,
  `camera:2`, `camera:3`. The first keeps the name every existing
  recording uses.
- The client honoured `endpoints` when a server reported one and fell
  back to the argv-allocated ports when it did not. That was a
  transitional state, and it is what let the servers move one at a time
  rather than in one commit.
- **All four servers** — `nion_server`, `camera_server`,
  `dectris_server`, `spectrum_server` — now take `--instrument-port` and
  nothing else. The positional port list is gone from the client, from
  every server, and from the two out-of-tree stand-ins in the test
  suite; so is the client's fallback, because there is nothing left to
  fall back for.
- Which handle a target gets is read from the endpoint's **`kind`**
  rather than guessed from its name. A server can therefore serve
  `camera:2`, or a name written after this client shipped, and have it
  arrive as a camera.
- `TARGET_NAMES` is **no longer append-only**, and no longer reaches any
  server's command line. What it still decides is which names get a
  *named attribute* on `RemoteInstrumentDevices`; everything else is
  reached by name through `cameras()` / `additional_cameras`.

What this costs an out-of-tree adapter: the argv change is a **flag
day**. A server written against the old shape takes N positional ports
and will fail argument parsing against this client. The fix is the edits
`tests/unit/test_out_of_tree_server.py` makes — swap the positional
`ports` for `--instrument-port`, bind everything else on port 0, and
return an `endpoints` map from `describe()` — about ten lines. There is
no version negotiation in the protocol to soften it, which is a real
limitation and the honest reason it was worth doing before there were
adapters in the field rather than after.

One thing did **not** change: the attach path
(`attached_instrument()`, `gatan_bridge`) still carries an explicit port
per target in its `AttachInvitation`. It has to. The client is not the
end that binds there, so it cannot learn a port the far end chose
without a rendezvous it does not have — the invitation *is* the
rendezvous, and it is written before either end is listening.

## What is still the wrong shape

Two, neither fixed, both estimated below rather than pre-emptively built.

### `CameraParameters.binning` is scalar, and EELS is not

EELS is run with vertical binning — bin along the non-dispersive
direction to trade dynamic range against SNR, leaving the energy axis at
full resolution. A single `int` cannot say that. But the fix is *not* a
`(y, x)` tuple: Nion's `CameraFrameParameters` has no per-axis binning at
all, and what it offers for "bin vertically" is
`processing = "sum_project"`, a full projection to 1D. A tuple would
therefore be a field the only adapter behind it must refuse — the exact
"vendor-neutral in name only" failure `ScanParameters.fov_nm` warns about
— and it would break the shape-recovery trick that keeps an in-flight
frame correctly labelled. Model the *readout mode* instead, and route a
projected readout into `SpectrumWriter` so it lands in the same
`NXspectrum` layout as EDX. 2–4 days; specification in
[spectrum detectors](adapters/spectrum-detectors.md) §6.

### `ScanParameters.fov_nm` encodes Nion's convention

The field of view spans the **longer** axis, because that is what
`nion.instrumentation.scan_base.get_scan_calibrations` implements, and
the docstring says so — calling it "not a free choice" while one adapter
exists. With two, it is a choice, and the second adapter has to convert.
Vendors that think in magnification rather than field of view (most SEM
software) need a calibration constant to convert at all, which is a
per-instrument value someone has to measure.

This is cheap to handle and expensive to get wrong, so the note belongs
here: a second adapter converts *into* this convention and records what it
converted from in the frame metadata.

## Task estimates

Sizes assume the framework as it stands plus the target-name redesign
above, and someone with access to the instrument. "Days" means
engineering days, not calendar days, and excludes procurement, site
access, and vendor agreements — which for Zeiss and Hitachi are likely to
dominate everything else.

### Common to any second vendor — 4–7 days

| Task | Size |
|---|---|
| Target-name redesign: server-chosen ports, reported through `describe()` | 2–3 d |
| Vendor-neutral adapter template + docs (the test server, promoted to a documented skeleton) | 1 d |
| Convert vendor scan geometry into `ScanParameters`, record what was converted | 0.5–1 d |
| Map vendor state onto the `Frame` metadata vocabulary | 0.5–1 d |
| Adapter-conformance test suite runnable against any server module | 1 d |

That last one is the leverage: every contract already ported from Nion's
tests — frame identity, error recovery, binning-changes-the-calibration,
the metadata vocabulary — is written against the *protocols*, so it can be
made to run against an arbitrary `server_module`. A second adapter would
then arrive with a real acceptance suite on day one.

### Thermo Fisher (TEM, via `temscript`) — 6–10 days

| Task | Size |
|---|---|
| `Scanner` over `STEMDetector`/`STEMAcqParams` | 2–3 d |
| `Camera` over `CCDCamera`/`Acquisition`, exposure and binning | 1–2 d |
| `InstrumentController` over `Stage`, `Projection`, `Illumination` (defocus, stage, blanker, high tension) | 1–2 d |
| Calibration: no `calibration_controls` equivalent — pixel size comes from `Projection` magnification/camera length | 1–2 d |
| Test against `temscript`'s dummy implementation, then on hardware | 1 d |

**Lowest risk of the four.** Permissively-licensed wrapper, offline dummy,
and a class map that matches the protocols. The real work is calibration,
where Nion's "read the controls the device names" trick has no
counterpart — the scale has to be derived from optics state.

**This estimate is probably too high, and is left as it stands rather
than adjusted on a guess.** It assumes a `*_server.py` of this project's
own, but `temscript` 2.1.0 already ships `MicroscopeServer` and
`RemoteMicroscope` (see "The landscape", above).
Whether adopting them is cheaper than writing another server — against
the loss of one uniform transport across every adapter, which §6's
boundary exists to keep — is a design question nobody has answered.
Re-cost it before committing to the number.

### JEOL (via PyJEM) — 8–14 days

| Task | Size |
|---|---|
| Obtain PyJEM and TEM_External access; confirm the offline/simulator mode | 1–2 d |
| `Scanner` over `Scan3`/`Detector3` | 2–3 d |
| `Camera` over `Camera3` (or the actual detector's own SDK — often Gatan) | 2–3 d |
| `InstrumentController` over `Stage3`, `Lens3`, `EOS3`, `Def3` | 2–3 d |
| Calibration and metadata from `EOS3` (magnification, camera length, HT) | 1–2 d |
| Hardware validation | 1 d |

The class map (`Scan3`, `Detector3`, `EOS3`, `Lens3`, `Stage3`,
`Camera3`, `Def3`, `MDS3`) is a good fit for the protocols. The
uncertainty is access and whether the camera is JEOL's at all.

### Zeiss SmartSEM (SEM) — 8–12 days *after* the agreement

| Task | Size |
|---|---|
| Zeiss developer agreement for `CZEMApi.ocx` | **unknown, likely dominant** |
| COM wrapper (or adopt an existing one, e.g. SBEMimage's) | 2–3 d |
| `Scanner` over the SEM's scan and detectors; no `Camera` target at all | 2–3 d |
| `InstrumentController`: stage, working distance/focus, beam blanker, EHT | 2–3 d |
| Local vs remote mode: remote gives 8-bit reduced-resolution images | 1 d |
| Hardware validation | 1–2 d |

The 8-bit remote mode is worth flagging early: it would silently halve
the dynamic range of everything recorded. An adapter should refuse remote
mode for recording, or record what mode it used.

### Hitachi (SU9000II) — 12–18 days, or 10–16, or 3–5

Estimable now, in four scenarios, because the placeholder here was
written without a search and the search found something. Full working:
[adapters/hitachi.md](adapters/hitachi.md).

**There is very likely an API, and it is undocumented.** Public code
drives a Hitachi SU7000 FE-SEM from Python through modules named
`MfExtCont`, `MfKeyMouse` and `MfCommon` — `EXT.SetHv()`,
`EXT.GetStagePosition()`, `EXT.RunStageMove()`, `EXT.RunAutoAfc()`,
`EXT.RunScan()` — which is `InstrumentController` and `Scanner` in
everything but spelling. Nothing about it is published: no manual, no
PyPI package, three files across all of GitHub. Whether it is on an
SU9000II is the question, and it is answered by looking at the
instrument PC, not by a negotiation.

**EM Flow Creator is not that.** It is a recipe engine that "can execute
scripts written in Python" — the instrument owns the loop and calls your
block. That is the Gatan topology, a bridge running inside the vendor's
application connecting out, and it cannot give an interactive session at
all.

**On a SEM the scan is purchasable.** The external scan connector, there
since EDS mapping systems needed it, takes third-party scan generators;
point electronic's DISS6 digitises 4 analog and 12 digital inputs
simultaneously and ships an SDK. So unlike every other column vendor
here, a flat refusal still leaves a route to a scanned image — at the
cost of a measured volts-to-nanometres calibration that no vendor
constant supplies and that changes with working distance and kV.

| Scenario | Size | Drives the microscope? |
|---|---|---|
| Callable control library on the instrument PC | 12–18 d | Yes |
| EM Flow Creator Python blocks only | 10–16 d | Batch, not interactive |
| External scan generator, no vendor cooperation | 10–16 d + hardware | Scan and detectors; not optics or stage |
| Files only — TIFF plus `.txt` sidecar | 3–5 d | No. Ingest, not a device |

All four need the common second-vendor block, and three of them need a
**simultaneous multi-channel scan call** — which now exists
(`Scanner.scan_frames`, see "What was the wrong shape, and is now fixed"
above), so what an SU9000II adapter owes is the vendor half of it rather
than the design.

### Bruker ESPRIT / Oxford AZtec — the protocol now exists

The deferral below ("a new protocol — spectra and maps, not frames") is
discharged: `SpectrumDetector`, `Spectrum` and `NXspectrum`-shaped
storage ship, with a simulated server and a verified HyperSpy EDS round
trip ([spectrum detectors](adapters/spectrum-detectors.md)). Neither
vendor's control library is redistributable, so a *live* adapter is
out-of-tree — and the offline answer is still the right first question.
RosettaSciIO reads Bruker `bcf`/`spx` and the EMSA `msa` both vendors
export. Note that RosettaSciIO 0.14 has **no** Oxford/H5OINA reader, so
that answer is weaker for AZtec than for ESPRIT: SuperSTEM 2's Bruker is
covered, SuperSTEM 4's Oxford needs AZtec asked for `.msa` export.

### Bruker ESPRIT — 3–5 days, and probably the wrong question

Reading ESPRIT's output files through RosettaSciIO is a day, needs no
vendor cooperation, and gives the same data for most purposes. Live
control would need ESPRIT's scripting environment and a new protocol
(spectra and maps, not frames), which is a bigger design question than a
vendor adapter.

### Detectors

Estimated in the detector-SDK section above, since a detector adapter is
a different job from a column adapter and is likely to come first.

## The short version

**Start with a detector or a camera, not a column — and it did.** DECTRIS
was the smallest real adapter in this document — a documented REST API,
no vendor library, no OS constraint — and it is now built
([adapters/dectris.md](adapters/dectris.md)), proving the whole
detector-only path end to end at a fraction of the cost of any column
vendor. Direct Electron is the obvious next one and pip-installable.

**The best coverage per day of work is pymmcore.** One device server
backed by Micro-Manager's core reaches every UVC microscope plus much of
the scientific camera market, and it is the option that most matches what
this project claims to be: §4 already names pymmcore-plus as prior art,
and wrapping it is adopting that work rather than admiring it.

Among column vendors, Thermo Fisher first — permissive wrapper, offline
dummy, best class-map fit. JEOL second. Zeiss only if a site already has
the agreement. Hitachi needs someone to look at the instrument PC
first, and *then* a phone call. Bruker is a file-reading
question wearing a device-adapter costume.

Two pieces of shared groundwork belong before, or with, the first
adapter: **ROI, gain mode, and trigger on `CameraParameters`** if it is a
camera of any kind — a consumer body's ISO and an ORCA's gain are the
same missing field — and the **target-name redesign** if it is a column,
the one piece a second column adapter cannot work around and the only
place where the framework's shape is still Nion's rather than neutral.

Whichever comes first, it is a subprocess. The
[transport section](#transport-why-every-adapter-is-a-subprocess)
explains why that holds even where no licence requires it, and why it is
not the architectural bifurcation it can look like.
