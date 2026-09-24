# Acquisition UX survey: three basic workflows, and what stands between here and them

Surveyed 2026-09-24 against the code as it is, not the documentation —
every claim below names the file it was read from. The question was:
**what has to happen before an operator can walk up to a SuperSTEM
instrument and take a STEM image, an EELS spectrum, and a spectrum
image through this application?**

The short answer is in three lines, then the long one follows.

- **A STEM image** works end to end on the simulator and is one
  hardware day from working on a Nion column. The window has the
  controls an operator needs; what is missing is verification, and a
  few scan-geometry fields.
- **An EELS spectrum** works as a *camera frame*. What is missing is
  the spectrometer half of the workflow: the energy offset lives on the
  wrong panel and under a name the real column may not use, there are
  no camera profiles (the live view runs at whatever the device was left
  at), and nothing offers a dark reference.
- **A spectrum image** has a complete UI and storage path, and **no
  hardware backend that can supply it.** Only the in-process preview
  and the replay device implement the synchronised pass; the Nion
  server does not, and neither does the RPC proxy in front of it. This
  is the largest single item, and the survey script now records
  whether each Nion column offers the device-level methods that would
  close it.

## What the three workflows were taken to mean

This is the working definition the survey was made against. **If it is
not what SuperSTEM's operators would recognise, this section is the one
to correct first**, because the gap list below follows from it.

**STEM image.** Live HAADF (and MAADF alongside) at a short dwell, to
navigate and to focus by eye; then one clean pass at a long dwell and
full resolution, saved with its calibration, its detector names and the
session context. Rotation and a sub-region of the field of view are
part of the ordinary case, not extras.

**EELS spectrum.** A live spectrum from the spectrometer camera in its
projected readout, with an exposure and a vertical binning chosen for
viewing; the zero-loss peak put where the operator wants it by an energy
offset that lives *next to the spectrum*, not on another panel; a dark
reference taken and applied; then one spectrum kept at an acquisition
exposure, with an energy axis in eV that is right.

**Spectrum image.** A region drawn on the HAADF image, a grid and a
per-position exposure chosen, the pass started and watched as it fills
in — the map and the current spectrum both — with the HAADF read out of
the same pass, streamed to a file a reader can open while it is still
being written, and stoppable without losing what was taken.

## Where each workflow stands, by backend

| | Preview (in-process) | usim via Nion server | Nion hardware | DECTRIS ELA | Gatan bridge |
|---|---|---|---|---|---|
| Live scan, several detectors, one pass | yes | yes | untested | no scanner | no scanner |
| Acquire one scan image | yes | yes | untested | — | — |
| Live camera / spectrometer | yes | yes | untested | yes, mock only | never run |
| Acquire one spectrum, eV axis | yes | yes | untested | 2D image only, no eV axis | 0.5 eV/ch constant |
| Energy offset | yes | yes (`ZLPoffset`) | **control name uncertain** | via column | `IFSetEnergyLoss`, unverified, and cannot run on DM 1.x/2.x |
| Dark / gain reference | no | no | no | not applicable | no |
| Spectrum image | yes | **no** | **no** | **no** | **no** |

"Untested" means the code path exists and has not met an instrument;
"no" means there is no code path.

## STEM image: what is there, what is missing

**There.** The Scan section
([`viewer/panels/devices.py:49-152`](../src/miainwoodpecker/viewer/panels/devices.py))
has Start/Stop, Preview, Acquire, Save and Record, detector checkboxes
that all read out of one pass, a shared field of view, and three
profiles with their own dwell and size. `Scanner.scan_frames` is a real
single-pass multi-channel read on the Nion server, stamped with a shared
`scan_pass_id`
([`devices/nion_server.py:904-1120`](../src/miainwoodpecker/devices/nion_server.py)).
Frames carry high tension, defocus, beam current, rotation, centre and
flyback as metadata. The dashboard can take a single scan as well.

**Missing, in the order it will bite:**

1. **Nothing here has met a Nion column.** `hardware_instrument`'s own
   docstring says "untested against real hardware" and lists what it
   assumes: that plug-ins register from `run()`, that a real camera's
   `camera_type` is `"ronchigram"` or `"eels"`, that the controller
   answers to `C10`, `C_Blank` and `stage_position_m`
   ([`nion_server.py:1769-1784`](../src/miainwoodpecker/devices/nion_server.py)).
   The survey script now reads every one of those off the instrument.
2. **Scan geometry has no rotation, centre or sub-region.**
   `ScanParameters` is height, width, dwell and field of view
   ([`devices/interface.py:253-311`](../src/miainwoodpecker/devices/interface.py)),
   and the server reads rotation and centre back as metadata only,
   saying "this interface offers no way to set them"
   ([`nion_server.py:815-818`](../src/miainwoodpecker/devices/nion_server.py)).
   Nion's `ScanFrameParameters` carries both, and its controller has a
   `subscan_region` — this is plumbing, not research. The spectrum-image
   region below needs the same fields, so they should arrive together.
3. **Scans are square, and sizes are 128/256/512.** Height is set to
   width ([`viewer/live.py:1175-1180`](../src/miainwoodpecker/viewer/live.py));
   the size list is a constant ([`panels/defaults.py:9`](../src/miainwoodpecker/viewer/panels/defaults.py)).
   The dashboard already offers up to 2048.
4. **The stage size falls back to 1 µm when the controller does not
   publish it**, which silently sets every default field of view
   ([`nion_server.py:278-282`](../src/miainwoodpecker/devices/nion_server.py)).
   The survey reads `stage_size_nm` off the controller so this is
   known before hardware day.
5. **The plug-in to load is not named in any instrument file.** All
   three SuperSTEM files leave `plugins = []` for autodiscovery. The
   survey lists the installed `nionswift_plugin` modules; the answer
   goes into the instrument file, and autodiscovery's skip list is
   checked against it.

Not missing, and worth saying: focus and stigmation by eye at a short
dwell is exactly what the View and Preview profiles are for, and the
Instrument panel's defocus box is enough for a first session. A focus
series exists as a function ([`acquisition/sequence.py:232`](../src/miainwoodpecker/acquisition/sequence.py))
and has no button, which is fine for now.

## EELS spectrum: what is there, what is missing

**There.** A camera section per spectrometer with Start/Stop, Acquire,
Save and Record; an exposure and a per-axis binning for the acquired
frame; a readout control that switches the device between `image` and
`projected`; a spectrum panel that plots counts against the device's
own eV calibration, and says "channel" when none arrived
([`viewer/live.py:3508-3600`](../src/miainwoodpecker/viewer/live.py)).
`camera_image` configures, takes one frame and restores the previous
settings ([`sequence.py:152-194`](../src/miainwoodpecker/acquisition/sequence.py)).
An energy-offset series exists as a function.

**Missing:**

1. **The energy offset may be driven under the wrong name on a real
   column.** The server sets `ZLPoffset`
   ([`nion_server.py:243`](../src/miainwoodpecker/devices/nion_server.py)),
   which is the simulator's control. Nion's own acquisition preferences
   name the energy offset `EELS_MagneticShift_Offset`
   (`nion.instrumentation.AcquisitionPreferences.acquisition_controls`).
   The simulator publishes *both*, measured today, which is why the
   tests never noticed. The survey reads both; the server should try the
   kit's name first and fall back to the simulator's, which is a one-line
   change plus a test once the survey says which one a real column has.
2. **The energy offset is on the Instrument panel, not beside the
   spectrum.** It is a Set box under Defocus
   ([`viewer/panels/instrument.py:101-104`](../src/miainwoodpecker/viewer/panels/instrument.py)).
   An operator centring the zero-loss peak is looking at the spectrum
   panel. It belongs in the spectrometer's section, with the current
   value shown, and with a "put the ZLP at 0" helper that reads the peak
   position off the live spectrum and sets the offset from it — the one
   step every EELS session starts with.
3. **No camera profiles.** Exposure and binning apply only to Acquire;
   the live view runs at whatever the device was last configured to
   ([`broker/interface.py:915-919`](../src/miainwoodpecker/broker/interface.py),
   [`live.py:1932-1975`](../src/miainwoodpecker/viewer/live.py)). The
   scan has View/Preview/Acquire; the camera needs at least View and
   Acquire. Nion keeps three profiles per camera and the survey records
   what the operators have them set to, which is where the defaults
   should come from.
4. **No dark reference, no gain reference.** Nothing in devices,
   acquisition or the broker offers either. Nion's camera device
   protocol has `is_dark_subtraction_available`, `set_dark_image` and
   the gain equivalents (`CameraDevice3`); the survey now reports
   whether each real camera offers them. The right shape is an
   optional `Camera` extension — "dark subtraction on/off" and "take a
   dark reference now (N frames, beam blanked)" — that the UI shows only
   when the device says it can. A counting detector such as the ELA has
   no dark reference, and its section should not pretend otherwise.
5. **Nothing shows or sets the dispersion.** The eV-per-channel value
   comes from the calibration control `eels_x_scale` on Nion, is a
   constructor constant on the Gatan bridge (`_DEFAULT_DISPERSION_EV =
   0.5`, [`gatan_bridge.py:171`](../src/miainwoodpecker/devices/gatan_bridge.py)),
   and is absent on the ELA. Showing the dispersion and the energy range
   in the section is cheap and catches a spectrometer left on the wrong
   setting. Setting it is vendor-specific and is one of the operator
   questions in the survey runbook.

   **The Gatan bridge cannot be the answer on SuperSTEM 1 or 2.** It
   runs inside Gatan Microscopy Suite's own Python, and SuperSTEM 1 runs
   DigitalMicrograph 1.x and SuperSTEM 2 runs 2.x — neither embeds
   Python; that arrived with GMS 3.4. So on SuperSTEM 2 the Enfina is
   either Nion's `eels_camera`, which the survey's first run settles, or
   it needs something this project has not built: a DM script on the DM
   side speaking a socket, or a Gatan-supplied controller. On SuperSTEM
   1 the same holds, and what drives the *column* is not yet known.
6. **Vertical binning on an Enfina.** The spectrometer on SuperSTEM 1
   (and, if it is Nion's `eels_camera`, on SuperSTEM 2) reads out
   1340×100 and bins the rows. `NionCamera.configure` accepts one
   binning factor for both axes and refuses a `(rows, channels)` pair
   ([`nion_server.py:532`](../src/miainwoodpecker/devices/nion_server.py));
   `projected` sums all rows on the server side, which is the right
   physics but pays the readout noise of every row. Nion's device has a
   settable `readout_area`, which is the mechanism for binning fewer
   rows. This can wait until the survey says which device it is.
7. **On HERMES an EELS spectrum through this path is a 2D image with
   pixel axes.** The DECTRIS adapter refuses `projected` because it does
   not know the dispersion direction, and publishes no calibration
   ([`dectris_server.py:880-898, 108-114`](../src/miainwoodpecker/devices/dectris_server.py)).
   The dispersion and the offset on HERMES are properties of Nion's
   IRIS spectrometer, i.e. of the *column*, so the survey reads the
   `eels_*` calibration controls from the column's controller even
   though the detector is not Nion's. If they are there, the ELA's
   frames can be calibrated from them and the dispersion direction can
   be stated in the instrument file.
8. **The dashboard has no spectrum plot** — a 1D frame shows as the
   text "1D readout - not an image"
   ([`notebooks/instrument_dashboard.py:293-299`](../notebooks/instrument_dashboard.py)).

## Spectrum image: what is there, what is missing

**There.** More than expected. A Positions grid and a per-position
detector in the Scan settings; a pass that leases the scanner and the
camera together and runs on its own thread; a `PassWriter` that streams
each position into a chunked HDF5 dataset; a live virtual-detector map
and a live spectrum of the position just left; a status line that says
whether a spectrum image or a 4D stack landed
([`viewer/live.py:2099-2428`](../src/miainwoodpecker/viewer/live.py),
[`storage/passes.py`](../src/miainwoodpecker/storage/passes.py)). All
of it works on the preview instrument and on a replayed session.

**Missing, and it is one thing before all the others:**

1. **No hardware backend implements the synchronised pass.** The
   `SynchronisedScanner` protocol
   ([`interface.py:1351-1467`](../src/miainwoodpecker/devices/interface.py))
   is implemented by the preview scanner and the replay device and by
   nothing else. `NionScanner` has no `synchronised_targets`
   ([`nion_server.py:850-1201`](../src/miainwoodpecker/devices/nion_server.py)),
   `RemoteScanner` cannot proxy one
   ([`devices/remote.py:1193-1240`](../src/miainwoodpecker/devices/remote.py)),
   and the broker reports `synchronises=False` for any of them
   ([`broker/local.py:448-466`](../src/miainwoodpecker/broker/local.py)).
   The button says so, correctly, on every real instrument.

   **The route on a Nion column is narrower than the migration plan
   records.** [Migration plan §7](migration-plan.md) says a real 4D-STEM
   mode needs Swift's `ScanHardwareSource`/`Application` layer, which
   this project has twice found too heavy to stand up. That was
   measured on the *simulator's* camera, which reads the probe position
   from the hardware source. But Swift's own `grab_synchronized` is
   built on two **device-level** methods that need no application:
   `ScanDevice.prepare_synchronized_scan(frame_parameters,
   camera_exposure_ms=…)` and
   `CameraDevice3.acquire_synchronized_prepare/begin/continue/end(collection_shape)`
   (`nion.instrumentation.scan_base:2634`, `camera_base:720-770`, read
   from the installed kit). The device server already holds both
   devices. Implementing `SynchronisedScanner` in `nion_server` on
   those two calls — the behaviour, not the code — is the work, and it
   can be started against usim, whose devices expose both methods
   (measured with the survey script today). Whether a *real* Nion scan
   unit and camera expose them is exactly what the survey now records,
   per device, under `capabilities`.

   Then the same protocol has to cross the RPC boundary: a
   `synchronised_targets`/`scan_synchronised` pair on `RemoteScanner`
   and on the broker's leased scanner, streaming positions into the
   caller's `into=` destination the way the in-process preview does.

   **On HERMES the detector is not Nion's**, so the camera half is the
   ELA triggered by the scan unit's per-pixel output (`trigger_mode =
   exts`, one trigger per position) with frames arriving over the
   ZeroMQ stream that LiberTEM-live already consumes, not over the
   monitor. That is a second, larger step; the survey's DECTRIS run
   records the trigger configuration the detector is in today.

2. ~~**The region.**~~ **Built, on the preview, after this survey was
   written.** `ScanParameters` gained `center_nm`, the Scan toolbar
   gained a region button that draws an editable rectangle on the
   survey scan, the grid takes its aspect ratio from the rectangle with
   Positions along the long side, and the scan channels build live in
   `Acquiring (HAADF)` beside the spectrum. What remains of this item
   is the hardware half: the Nion server passes `center_nm` through to
   the column's own `ScanFrameParameters`, but has no synchronised pass
   to use it in (item 1).
3. **No cancel.** Stop recording cancels a `RecordingJob` only
   ([`live.py:2615-2627`](../src/miainwoodpecker/viewer/live.py)); a
   running pass has no stop, and a second press starts a second pass
   over the first (`_run_spectrum_image` overwrites `self._pass_job`).
4. **Every scan channel is read, not the checked ones**
   ([`live.py:2226`](../src/miainwoodpecker/viewer/live.py)), and the
   detector calibration is not handed to the writer
   ([`live.py:2267`](../src/miainwoodpecker/viewer/live.py)). The
   stale docstrings that said the pass blocks the window are fixed.
5. **After the pass.** A saved pass appears as "0 frames" in the File
   list and reopens as a plain image layer with no spectrum picker
   ([`live.py:2717-2767`](../src/miainwoodpecker/viewer/live.py)). Not
   acquisition, but the first thing an operator does after one.
6. **The dashboard has no spectrum-image workflow at all.**

Drift correction is deliberately not on this list. Nion has a
`DriftTracker`; the preview simulates drift; nothing here corrects it.
It is not part of a *basic* spectrum image, and it should follow the
first real one.

## The order the work wants doing in

1. **Run the survey** ([runbook](superstem-survey.md)). Everything
   below is cheaper with its answers, and two items — the energy-offset
   name and the synchronised-acquisition methods — change shape
   depending on them.
2. **Close the assumptions the survey answers**: the plug-in name in
   each instrument file, the energy-offset control name, the camera
   classification, the stage size. Small changes, each with a test.
3. **The spectrometer section**: energy offset moved beside the
   spectrum with a ZLP helper; View and Acquire camera profiles; the
   dispersion and range shown; dark subtraction as an optional `Camera`
   extension surfaced only when the device offers it.
4. **Scan geometry**: rotation, centre and non-square size on
   `ScanParameters`, through the Nion server, the proxy and the panel.
5. **The synchronised pass on Nion**, device-level, proven on usim by
   measurement (a moving probe must change the frame) before it is
   claimed, then across the RPC boundary and the broker.
6. **The region, the cancel and the channel selection** on the
   spectrum-image UI, which are all small once 4 and 5 exist.
7. **Hardware day**, by the [checklist](hardware-validation-checklist.md),
   with the synchronised pass added to it.
8. **HERMES**: the ELA triggered from the scan, over the stream.

## What was not surveyed

The EDX detectors (SuperSTEM 2's Bruker, SuperSTEM 4's Oxford) — the
`SpectrumDetector` protocol exists and has no UI and no hardware
adapter, and it is outside the three workflows. SuperSTEM 4 as a whole:
the Hitachi column has no adapter and the [Hitachi page](adapters/hitachi.md)
is the record; the survey's third run is its first step. Analysis after
acquisition, which has its own [parity page](analysis-parity.md).
