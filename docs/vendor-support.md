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

Two separate markets, and conflating them is the first mistake to avoid.
**Column vendors** sell the microscope and control the optics, stage, and
scan. **Detector vendors** sell what hangs off it. This project's
`Scanner` and `InstrumentController` face the first; `Camera` faces the
second, and often a different company.

### Column and instrument SDKs

| Vendor | Interface | Language | How you get it | Reachable out-of-process? |
|---|---|---|---|---|
| **Nion** | [nionswift-instrumentation-kit](https://github.com/nion-software/nionswift-instrumentation-kit) | Python | PyPI, GPL-3.0 | Yes — what this project already does |
| **Thermo Fisher** (TEM) | TEM Scripting + Advanced Scripting | COM | Installed with the microscope; [`temscript`](https://github.com/niermann/temscript) (BSD-3) and [`pyTEM`](https://github.com/basf/pyTEM) wrap it in Python | Yes, on Windows |
| **Thermo Fisher** (TEM) | [AutoScript TEM](https://www.thermofisher.com/us/en/home/electron-microscopy/products/software-em-3d-vis/autoscript-tem-software.html) | Python 3.11 | Paid add-on, installed on the instrument | Yes |
| **Thermo Fisher** (SEM/DualBeam) | [AutoScript 4](https://www.thermofisher.com/us/en/home/electron-microscopy/products/software-em-3d-vis/autoscript-4-software.html) | Python | Paid add-on | Yes |
| **JEOL** | [PyJEM](https://www.jeolusa.com/PRODUCTS/Transmission-Electron-Microscopes-TEM/Analytical-Data-Optimization/PyJEM) over TEM_External/TEMCenter | Python | On the TEM PC; [docs are public](https://github.com/PyJEM/PyJEM), package is not on PyPI | Yes |
| **Zeiss** | [SmartSEM Remote API](https://www.zeiss.com/microscopy/en/products/software/zeiss-smartsem.html) (`CZEMApi.ocx`) | ActiveX/COM | **Requires an agreement with Zeiss** to develop against | Yes, on Windows |
| **Hitachi** | None published | — | EM Wizard / EM Flow Creator are GUI automation, not an API | Unknown — ask the vendor |
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

**Bruker is not a column vendor.** ESPRIT is EDS/EBSD/µXRF analysis
software that sits *beside* a Zeiss, Thermo, JEOL, or Hitachi column. Its
scripting automates analytical workflows, not the microscope. Adapting it
is not "another `Scanner`" — it is a spectrum/map source that would want
its own protocol, and the honest first question is whether
[RosettaSciIO's `bcf` reader](https://github.com/hyperspy/rosettasciio)
covers the need by reading the files ESPRIT already writes.

**Hitachi publishes nothing.** No documented API surfaced for SU-series
SEMs or the TEMs; the automation products are workflow recorders. That is
a finding, not a gap in the search: an adapter would start with a
conversation with Hitachi, and until then it cannot be estimated.

### Detector SDKs — the `Camera` side

Worth listing separately, because on a real instrument the camera is
often not the column vendor's, and because **someone has already done
this work**:
[LiberTEM-live](https://libertem.github.io/LiberTEM-live/) supports
Quantum Detectors Merlin (TCP control + data), DECTRIS EIGER2 (ARINA,
QUADRO), and ASI TPX3, with Gatan K2 IS and others in progress. Its
detector connections are the closest existing thing to this project's
`Camera` protocol, and this project already depends on LiberTEM for
analysis.

The exception is **Gatan**: GMS 3's Python integration runs *inside*
DigitalMicrograph and, in Gatan's own words, cannot be executed from
outside the application. A Gatan adapter is therefore not a subprocess
this client launches — it is a bridge running inside DM that connects
*out*. That inverts the process topology and is the one detector case the
current design does not fit.

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
it gives is the useful one: **the protocol plumbing is about eighty lines**.
Everything beyond that in a real adapter is vendor work.

## What is still the wrong shape

Two, neither fixed, both estimated below rather than pre-emptively built.

### The target names are a fixed tuple

`rpc.TARGET_NAMES = ("ronchigram_camera", "eels_camera", "scanner",
"instrument")` is Nion's device list, and it is *positional argv*: the
client allocates one port per name before it can talk to the server, so
the set cannot be discovered. A Thermo Fisher STEM with three detectors,
or a SEM with SE and BSE and no camera, has to map onto those four names —
which works, but means a file's `device_id` is honest while its target
name is a fiction.

The fix is a protocol change, not a rename: bind **one** well-known
port for `instrument`, have the server choose and report the rest through
`describe()`, and let the client connect to what it is told. That removes
the positional-argv fragility as a side effect. It touches spawn,
connect, teardown, and roughly a dozen tests. Deliberately not done
speculatively — it should land *with* the second adapter, when there is a
real device list to test it against.

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

### Hitachi — not estimable

No public API. Step one is a vendor conversation. If the answer is "no
API", the honest options are file-watching whatever EM Flow Creator
writes, or nothing.

### Bruker ESPRIT — 3–5 days, and probably the wrong question

Reading ESPRIT's output files through RosettaSciIO is a day, needs no
vendor cooperation, and gives the same data for most purposes. Live
control would need ESPRIT's scripting environment and a new protocol
(spectra and maps, not frames), which is a bigger design question than a
vendor adapter.

### Detectors, if the column vendor's camera is not the one you want

| Detector | Route | Size |
|---|---|---|
| Merlin, DECTRIS, ASI TPX3 | Wrap LiberTEM-live's existing connections behind `Camera` | 2–4 d each |
| Gatan | **Different topology**: a bridge inside DigitalMicrograph connecting out, not a subprocess launched in | 5–8 d, plus a design decision |

## The short version

Thermo Fisher first, if the question is "prove the interface is neutral"
— permissive wrapper, offline dummy, best class-map fit. JEOL second.
Zeiss only if a site already has the agreement. Hitachi needs a phone
call. Bruker is a file-reading question wearing a device-adapter costume.

And before any of them, the target-name redesign — because it is the one
piece of this that a second adapter cannot work around, and the only
place where the framework's shape is still Nion's rather than neutral.
