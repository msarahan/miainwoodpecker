# Change Log

## Unreleased

### Added

- **A read-only instrument survey to hand to a facility**
  (`scripts/superstem_survey.py`, with `docs/superstem-survey.md` as its
  runbook). Several design decisions rest on guesses about instruments
  nobody here can reach: whether Nion drives SuperSTEM 2's Enfina
  (`eels_camera` plus a `ZLPoffset` control would mean the spectrometer
  is already supported with no Gatan code at all), whether an SU9000II
  has the `Mf*` external-control modules, and which SIMPLON version and
  configuration keys a HERMES ELA actually publishes.
  The script asks those questions and nothing else. Its safety is
  structural rather than careful, which is what makes it handable: the
  Nion section reads a registry Nion Swift populated and **never loads a
  device plug-in**, because this project's own device server does load
  them and a second process doing so would claim hardware the running
  Swift already owns; the DECTRIS section is `GET`-only, so it never
  arms, triggers, disarms or configures, and is safe to run
  mid-experiment; the Hitachi section resolves modules with
  `importlib.util.find_spec`, which locates without executing, because
  importing a vendor control module may open a connection to the column.
  Each of those three properties is pinned by a test rather than left as
  intent — the `GET`-only one asserts at the transport, against the
  simulated control unit, so a probe added later cannot quietly break
  it.
  Stdlib-only and parses on **Python 3.7**, the floor Gatan Microscopy
  Suite sets by embedding it. A `--check` mode reports the interpreter
  and what it could answer while touching nothing, because for the Nion
  and Hitachi sections the interpreter matters more than the machine:
  run from the wrong Python, an empty registry or a missing `MfExtCont`
  is a confident wrong answer rather than an error.

### Changed

- **EDX and EELS run together on SuperSTEM 2** — confirmed by the
  facility, and they do not physically block one another. This was an
  open question in `docs/adapters/spectrum-detectors.md` about dose,
  dead time and whether the Enfina's acquisition takes the scan; the
  blocking half is answered outright, so simultaneous EDX + EELS is a
  workflow to support rather than a configuration this project may
  reject. The consequence for the design recorded there is that the
  **pass** concept now has a confirmed user rather than a projected one:
  two spectrometers of different kinds reading out during one traversal
  is the correlated-output set a pass exists to group, and
  `metadata["simultaneous_with"]` has a device that genuinely knows. It
  does not change the recommendation — the device shape still ships
  first — but it removes "we may never need this" as a reason to defer
  the pass indefinitely.
- **The simultaneous multi-channel scan**, which is what a scanned
  instrument actually does: one pass of the probe, every enabled
  detector reading out at once.
  `Scanner.scan_frames(parameters, channels)` returns one frame per
  requested channel from **one** pass, so two channels cost one pass of
  dose rather than two, nothing drifts between them, and DPC / iDPC /
  centre-of-mass differences are taken between segments at the same
  probe position. `scan_frame` is untouched; the new call is additive,
  and `acquisition.multichannel_scan_series` is its series form.
  Frames of a pass carry `scan_pass_id` (the identity of that one
  traversal) and `simultaneous_channels` (the siblings that shared it).
  **The identity is produced by `scan_frames` and by nothing else** —
  `scan_frame` attaches neither key, so the id can never claim an
  acquisition that did not happen, which is exactly why a bare `scan_id`
  was refused before (this project having been bitten by
  `probe_position`, an identifier nothing established). The old
  `Frame` docstring premise that "a second channel is a second pass of
  the beam" was false on real hardware and is now corrected there.
  The Nion adapter uses the vendor's own mechanism rather than an
  emulation — `set_channel_enabled`, one `start_frame`, `read_partial`
  until complete, which is the loop `scan_base.ScanAcquisitionTask`
  runs, and which returns one data element per enabled channel — and
  verifies what the device did (no bad frame, the frame number did not
  move, every requested channel reported) before stamping a shared id.
  Nion mints a per-frame `uuid4` for the same purpose one layer up
  (`stem.scan.scan_id`), so the concept is the vendor's; the rule that a
  call must establish it is this project's.
  Across the device-server boundary a pass crosses as **one stacked
  block** in the source's existing shared-memory segment
  (`SharedFrameSetRef`): the reused-segment design allows exactly one
  publish per request/response cycle, so N publishes would overwrite
  each other, N segments would double every source's `/dev/shm`
  footprint, and N sequential replies would make the server hold a
  finished pass between calls. Below the shared-memory threshold a pass
  stays on the pickle path as an ordinary list. Storage needed no
  change: `NexusWriter` persists each frame's metadata whole, so a
  recorded series says which frames shared a pass with no new NeXus
  layout invented for it.
  `scan_frames` is part of the `Scanner` protocol rather than an
  optional extra, so an out-of-tree adapter written before it no longer
  satisfies `isinstance` and must add it — twenty lines, as
  `tests/unit/test_out_of_tree_server.py`'s example server shows.
- Process isolation for the viewer's analysis buttons, **on by
  default** (opt out with `MIAINWOODPECKER_ANALYSIS_ISOLATION=inprocess`).
  HyperSpy, eXSpy, py4DSTEM and LiberTEM run in a lazily-spawned worker
  (`python -m miainwoodpecker.analysis.worker`) that reuses the device
  layer's `Call`/`Result` protocol, its dispatch loop, and its
  reused-shared-memory transport. Buys crash containment, a thread
  budget set *before* `import numpy` (which `analysis/threads.py`
  documents as the one thing its runtime cap cannot do), and a
  precondition for separate dependency environments. Measured at about
  0.8 ms per megabyte moved and nothing at all when the input is a file.
- `docs/analysis-isolation.md`: what HyperSpy actually buys this project
  (ten call sites, one of which computes), a capability/licence table
  with permissive alternatives, and a precise statement of the licence
  question — including why isolation cannot answer it, since the
  documented `load_as_*` API returns live library objects by design. It
  shipped off by default so the decision would be the project owner's,
  and the owner made both calls: isolation on, for crash containment —
  a segfault in a native analysis library used to take the session and
  any in-flight recording, and now costs one result and a worker
  restart — and the licensing posture left as it is, expressly not the
  reason for the switch. The opt-out is the whole word `inprocess`, so
  a typo cannot silently disable the protection.
- `scripts/analysis_ipc_benchmark.py`, the analysis-side counterpart to
  `scripts/ipc_overhead_benchmark.py`. Interleaves the two transports
  call by call, because measuring them sequentially produced a
  reproducible 400–560 ms "overhead" that was the container slowing down.
- `miainwoodpecker.analysis.operations`: the three analyses the viewer's
  buttons run, as plain functions taking an `AnalysisInput`, so the
  in-process and isolated paths call one implementation rather than two.

- Device server backend selection (`--backend {simulated,hardware}`, repeatable
  `--plugin MODULE`, with `MIAINWOODPECKER_BACKEND` /
  `MIAINWOODPECKER_HARDWARE_PLUGINS` defaults), so a real instrument can be
  driven without editing code. Real-hardware discovery uses Nion's own
  `PlugInManager`/`Registry` mechanism; `remote_simulated_instrument()` keeps
  its exact signature.
- Graceful shutdown with an instrument park (beam blanked where a blanker
  exists), with SIGTERM demoted to a bounded-timeout fallback.
- Vendor-neutral `InstrumentController` protocol: stage position, defocus, and
  beam blanker, in operator units. `focal_series(..., instrument=...)` now
  sweeps real defocus and records both requested and read-back values.
- Device-server liveness: `instrument.check_health()` distinguishes responsive,
  exited, and unresponsive; device calls raise `RemoteConnectionLostError`
  naming the signal once the process is gone. Structured server-side logging
  via `MIAINWOODPECKER_DEVICE_LOG_LEVEL` / `MIAINWOODPECKER_DEVICE_LOG_FILE`,
  deliberately absent from the frame path.
- `Session`: a recording directory, collision-free naming, operator/sample/notes
  context, and enumeration — wired into the viewer so acquired data can actually
  be kept. Recording and loading run off the GUI thread.
- Session read-back: open recordings from the session or an arbitrary path, and
  run the analysis actions against a file on disk instead of a fresh burst.
- Per-axis, per-acquisition frame calibration (`storage/calibration.py`):
  real space (nm), reciprocal space (1/nm), energy (eV/meV), angle (mrad), and
  an honest uncalibrated (pixel) state, written as real NeXus units and carried
  into the HyperSpy and py4DSTEM adapters. Includes
  `load_as_hyperspy_spectrum` → `Signal1D` for flattened spectra.
- `NexusWriter`: `flush()`, `dtype=`, and `sample=`/`user=`/`notes=` writing
  real `NXsample`/`NXuser`/`NXnote` groups.
- NXem schema validation as a CI job (`hatch run validate:schema`), using
  `pynxtools`' programmatic API because `pynx validate` exits 0 even when it
  reports a file invalid.
- `docs/hardware-validation-checklist.md`: the ordered procedure for what
  cannot be verified against the simulator.

- Two user-facing guides, cross-linked as the documentation's front door:
  [Using the viewer](docs/using-the-viewer.md) for operating from the
  screen (with a translation table for Nion Swift and DigitalMicrograph
  habits) and [Scripting and automation](docs/scripting-and-automation.md)
  for driving the same capabilities from Python, including what it takes
  to put an AI agent at the controls.

- `docs/pre-hardware-work.md`: the counterpart to the hardware checklist —
  what can be built before an instrument is available, sourced from the
  device-layer contracts in Nion's own public acquisition test suites.
  Records that the calibration plumbing §7 lists as missing already exists
  on the GPL side (`calibration_controls` resolved by
  `camera_base.build_calibration`, with real values published by the
  simulator), so it needs neither hardware nor a reimplementation.

- Camera axis calibration resolved from the instrument. A Nion camera
  publishes the *names* of the instrument controls holding its calibration
  rather than the values; the device server resolves them with Nion's own
  `camera_base.build_calibration` and puts per-axis
  `{kind, scale, offset, units}` into the frame metadata as plain data.
  Ronchigram frames arrive with radian axes centred on the optic axis and
  EELS frames with an eV axis — and the dispersive axis is now the one the
  *device* reports, so `dispersive_axis="x"` is no longer an assumption to
  confirm on hardware.

- Every acquired frame now carries the acquisition metadata Nion's own
  required-metadata tests enumerate: device id, gapless `frame_index`,
  high tension, defocus, beam current, and per device either channel and
  scan geometry (rotation, centre, flyback, derived line and frame times)
  or the camera's type, name, and gain. The vocabulary is documented on
  `Frame`. The accelerating voltage is additionally written as
  `NXsource.voltage`, the one piece of it NeXus specifies a home for.

- Camera exposure and binning control: `CameraParameters(exposure_ms,
  binning)`, `Camera.binning_values`/`parameters()`/`configure()`, through
  the device server and over IPC. `configure` reports what the device took
  rather than echoing the request, and refuses a binning the camera does
  not advertise instead of rounding it. Binning multiplies the calibration
  scale, and the binning a *frame* reports is recovered from its shape, so
  a camera reconfigured mid-acquisition cannot mislabel the frame already
  in flight.

- `energy_offset_series`: step the spectrometer's energy offset across a
  series of EELS frames, recording the read-back offset beside the request
  and restoring the original afterwards — the acquisition half of Nion's
  multiple-shift EELS acquire. The camera is stopped around each step,
  because a running camera returns a frame generated before the control
  changed, which would mislabel the whole series by one. Brings a fourth
  control to `InstrumentController` (`energy_offset_ev`), which the
  camera's own calibration already tracks, so the recorded energy axis
  follows the sweep for free.

- Session context adopts Nion Swift's own documented vocabulary:
  `instrument`, `site`, `sample_area`, and `task` join `operator`,
  `sample`, and `notes`. Four map onto real NeXus fields — sample and
  sample area to `NXsample`, the operator to `NXuser`, the microscope to
  `NXinstrument/name`, which had been sitting empty — and are
  schema-checked in CI. `site` and `task` deliberately do not: no NeXus
  field means what they mean, and an approximate one would be a
  confidently wrong claim.

- `remote_instrument(server_module=...)`: the client can launch a device
  server it did not ship, so a vendor adapter can be an out-of-tree
  package rather than a fork. `tests/unit/test_out_of_tree_server.py`
  writes a complete vendor-free server and drives the whole client
  against it with no `device` extra installed, which is both the
  regression test and the specification an adapter writes against. The
  startup diagnostic now names the module it failed to launch.

- A detector-only device server is now supported: `scanner` is optional
  on `RemoteInstrumentDevices`, `cameras()` enumerates what is actually
  served, and the live viewer says so plainly instead of failing deep. A
  Direct Electron, DECTRIS, or Hamamatsu camera driven through its own
  SDK has no scan unit, and `connections["scanner"]` used to be
  unconditional — so "vendor-neutral" quietly meant "must have a scan
  unit shaped like Nion's".

- `docs/vendor-support.md`: what Thermo Fisher, JEOL, Zeiss, Hitachi and
  Bruker actually expose, what the direct detector vendors expose
  (Direct Electron, DECTRIS, Hamamatsu, Merlin, ASI, Gatan), what
  commodity cameras need (pymmcore reaches every UVC microscope in one
  adapter; a DSLR body is its own small gphoto one), and costed tasks for
  each. Also records why every adapter is a subprocess even where no
  licence requires it, and the one place the framework is still
  Nion-shaped — the device target names are a fixed positional tuple —
  and why that redesign should land with the second column adapter rather
  than before it.

- `docs/analysis-parity.md`: every analysis Nion Swift offers — roughly
  ninety operations across `nionswift`, `nionswift-eels-analysis`,
  `nionswift-experimental` and the instrumentation kit — mapped onto
  HyperSpy, LiberTEM and py4DSTEM, with the genuine gaps costed and the
  ones not worth porting argued rather than listed. Closes the last open
  Phase 4 item. Three findings change what that item meant: `niondata` is
  **Apache-2.0**, so Swift's whole core processing menu is a dependency
  declaration on the MIT side rather than fifty ports (four packages
  installed, against HyperSpy's ~35 and LiberTEM's ~102, and it runs
  standalone on plain NumPy arrays); **HyperSpy 2.x contains no EELS** —
  it moved to `exspy` at the 2.0 split — so the `analysis` extra covers
  none of Swift's EELS menu, which is the largest real gap and is this
  project's rather than Swift's; and only five gaps are genuinely
  Swift-specific and worth porting, at 9–15 days in total. Also records
  that `hyperspy` and `py4dstem` are themselves GPL-3.0 and imported
  in-process, which §6 does not currently speak to.

- **A device-layer shape for spectrum-producing detectors.** `Spectrum`,
  `SpectrumParameters` and the `SpectrumDetector` protocol in
  `devices/interface.py`; a `spectrum_detector` RPC target; a simulated
  EDX device server (`devices/spectrum_server.py`, needing nothing
  installed); NeXus storage in `NXspectrum`'s layout
  (`storage/spectra.py`); and `analysis.hyperspy_bridge.load_as_eds_signal`,
  producing an eXSpy `EDSTEMSpectrum` with a real energy axis. Designed
  against the two detectors actually fitted at SuperSTEM — a Bruker
  XFlash 6T-100 and an Oxford Ultim Extreme — without being an adapter
  for either. See `docs/adapters/spectrum-detectors.md`.

  **A spectrum is its own type rather than a `Frame` with a 1D array**,
  and the deciding reason is the calibration model: `FrameCalibration` is
  exactly two axes named `y`/`x`, so a one-axis spot spectrum and a
  three-axis map would each have to lie about one. The false economy
  would have been a document-only invariant on `Frame` plus a rank branch
  in `NexusWriter` choosing between two different NeXus layouts.

  **Storage placement was measured, not read.** `NXem` documents
  `NXspectrum` only under `measurement/eventID*/spectrumID*`, and putting
  it there fails validation for the same reason `ebeam_column` did. Four
  layouts were validated with `pynxtools`; the one that passes is
  `NXdata` at `entry/data` in `NXspectrum`'s field names beside an
  `NXdetector`. `NXfluo` was rejected on evidence rather than taste — it
  requires `NXsource/probe = "x-ray"` and a monochromator wavelength, and
  electron-excited EDX has neither. There is no `NXxrf` in the
  definitions at all.

  **`load_as_hyperspy_spectrum` now reads both storage layouts**, so an
  EELS camera stack and an EDX recording reach one `Signal1D` through one
  function, dispatching on what the file holds rather than on what the
  caller believes. EELS behaviour is unchanged and asserted so. That
  follows from the physics: EELS disperses onto a *camera* and arrives as
  a 2D frame, EDX is natively 1D, and both are a spectrum by the time
  anyone analyses them.

  **`exspy` joins the `analysis` extra.** HyperSpy 2.x moved its EELS and
  EDS classes out; measured on 2.4.0, `hs.print_known_signal_types()`
  returns an empty table, so `set_signal_type("EDS_TEM")` silently leaves
  a plain `Signal1D`. An extra that could not load an EDS signal would be
  claiming something false.

  **No `scan_id` was added.** Concurrency composes already — each target
  has its own connection and thread, and a test shows the detector
  integrating while another target is driven — but *correlation* does
  not, and no transport work fixes it. An identifier nothing establishes
  is a claim, which this project has been bitten by before
  (`probe_position`, accepted and echoed and silently dropped).
  `metadata["simultaneous_with"]` is in the vocabulary instead, absent by
  default, meaning nothing claimed.

  `TARGET_NAMES` gained `spectrum_detector` immediately before
  `instrument`, so every existing name keeps its argv position and
  `instrument` stays last. That is the minimal change, not an endorsement:
  the tuple is now Nion's device list *plus a detector class Nion does not
  have*, which is the clearest evidence yet that a fixed positional tuple
  is the wrong mechanism.

- **An EELS analysis path: `analysis.hyperspy_bridge.load_as_eels_signal`.**
  The project had none, on an instrument class where EELS is often the
  reason the instrument exists — `docs/analysis-parity.md` found the
  cause (HyperSpy 2.x contains no EELS classes; they moved to eXSpy at
  the 2.0 split) and eXSpy was already in the `analysis` extra from the
  EDX work. An EELS camera recording now reaches an
  `exspy.signals.EELSSpectrum`, with the recording's own energy axis,
  through the same shared `load_as_hyperspy_spectrum` loader an EDX
  recording uses. Exactly the thin layer `load_as_eds_signal` is, and
  the two now refuse each other's on-disk layout — once loaded a
  spectrum is a spectrum, so nothing downstream would catch eXSpy
  fitting X-ray lines to electron energy losses or ionisation edges to
  X-ray lines.

  **The energy axis is normalized to eV, and that is not cosmetic.**
  eXSpy's EDS code validates its axis unit (`_get_line_energy` takes eV
  or keV and raises otherwise); its EELS code checks nowhere while
  assuming eV everywhere — tabulated edge onsets in eV,
  `align_zero_loss_peak`'s ±3 eV subpixel window,
  `kramers_kronig_analysis` in eV. This project's vocabulary also admits
  meV (the natural unit for a monochromated vibrational spectrum), so the
  exact within-kind conversion happens on this side or not at all.

  **Two eXSpy items are deliberately left unset**: the convergence and
  collection semi-angles. Nothing here records either — the collection
  angle comes from the spectrometer entrance aperture and camera length,
  which no device reports, and the only convergence angle in the whole
  stack is a control that exists in usim and in no other Nion package,
  so reading it would dress a simulator detail as an instrument
  convention. Absent is also the safe state: eXSpy checks for exactly
  these and *refuses* the operations that need them, where a plausible
  wrong angle would have produced a number that looks like a result.
  Set, from what the recordings do carry: `beam_energy` (volts→keV),
  `beam_current` (amps→nA), and `Detector.EELS.exposure` (ms→s).

  **The axis is proved end to end against the simulator, not against our
  own array.** `tests/integration/test_eels_round_trip.py` sweeps the
  spectrometer with `energy_offset_series` and asserts that eXSpy finds
  the zero-loss peak at 0 eV in every step while the peak itself moves
  160 channels across the detector. The zero-loss peak is at zero by
  definition and the simulator plots it there independently; an adapter
  that lost the offset would report +100/+60/+20 eV, one that halved the
  dispersion would report −50/−30/−10 eV (verified by mutating the
  adapter), and one that lost the calibration would report a channel
  index. Only carrying both numbers through puts it at zero three times.

  **No viewer button**, and that is a decision. The analysis buttons act
  on the viewer's single camera, which `_choose_camera` resolves to the
  **Ronchigram** camera wherever there is one — so the button would
  refuse every click on the default configuration. And every EELS
  operation past loading needs a parameter an operator must choose
  (`estimate_thickness` raises without a threshold or a ZLP; background
  removal needs a fit window; mapping needs edges), while the one with
  usable defaults returns a recalibrated copy of the input rather than
  anything to draw. Reasoning recorded in `docs/analysis-parity.md`.

- **The spectrum server's playback is `--backend replay`, and
  `--backend hardware` refuses.** Playback was originally the `hardware`
  backend, honestly labelled — every spectrum carried `backend: "replay"`
  and the file it came from. That is not enough. `viewer/app.py` names
  the two failures its backend selector exists to prevent: driving a
  microscope you meant to simulate, and *believing you are on hardware
  when you are not*. A `hardware` backend that opens a file is the
  second, and per-spectrum metadata does not undo it — by the time anyone
  reads that metadata the session has already happened. `hardware` is
  still accepted by the parser, so asking for it gets a sentence saying
  there is no in-tree vendor backend, where an adapter goes, and what to
  use instead — rather than an argparse error. It refuses even when
  handed a perfectly good recording, which is the case the rename exists
  for and which a test pins.

- **Anisotropic binning: investigated, specified, deliberately not
  built.** EELS is run with vertical binning to trade dynamic range
  against SNR, and `CameraParameters.binning` is a scalar that cannot say
  so. The reason for not fixing it here is a measurement rather than
  scope: Nion's `CameraFrameParameters` has no per-axis binning at all —
  `get_expected_dimensions(binning)` and `build_calibration(..., binning,
  ...)` both take a scalar multiplying both axes — and what Nion offers
  for "bin vertically" is `processing = "sum_project"`, a full projection
  to 1D that its own tests mark as sequence/SI only. So a `(y, x)` tuple
  would be a field the only adapter behind it must refuse, which is the
  "vendor-neutral in name only" failure `ScanParameters.fov_nm` already
  warns about, and it would break `_binning_of(shape)`, which recovers a
  frame's real binning by matching `get_expected_dimensions` per scalar
  factor — the thing that keeps an in-flight frame correctly labelled.
  The specification models the *readout mode* instead, and routes a
  projected readout into `SpectrumWriter` so it lands in the same
  `NXspectrum` layout as EDX. 2–4 days, spec in
  `docs/adapters/spectrum-detectors.md` §6.

- **`remote.attached_instrument()`: drive a device server this client did
  not launch.** The subprocess rule has one structural exception — an
  adapter whose SDK exists only inside another running application cannot
  be spawned by us. Gatan is the first case (GMS 3's Python cannot be
  executed from outside DigitalMicrograph); anything living on a vendor's
  own control PC is the general one. Both socket directions are supported
  (`ACCEPT_TRANSPORT`, we listen; `CONNECT_TRANSPORT`, the bridge
  listens and the recommended default, since GMS outlives many client
  runs), sharing the whole existing protocol, authkey handshake, device
  contracts and `RemoteInstrumentDevices` shape. `AttachInvitation`
  publishes the rendezvous as a `0600` JSON file plus printed operator
  instructions, with the authkey only in the file.

  Liveness is deliberately *weaker* rather than faked: there is no
  `Popen`, so a new `SERVER_DISCONNECTED` health state reports what is
  actually known instead of reusing `SERVER_EXITED`, and attached errors
  name the bridge's origin and point at its log rather than inventing an
  exit status. Teardown is graceful with a per-device fallback and no
  forced terminate — killing the peer would kill DigitalMicrograph
  mid-acquisition. Shared memory is not used on attached links, because
  the peer may be on another machine. All process knowledge moved behind
  `_ServerLifecycle`; the spawn path's behaviour, messages and states are
  unchanged.

- **`rpc.COMPATIBLE_PICKLE_PROTOCOL`** caps outgoing calls on attached
  links at pickle protocol 4. `multiprocessing.Connection.send` pickles
  with the *sender's* default protocol — 5 since Python 3.8 — while GMS's
  embedded interpreter is Python 3.7, whose `HIGHEST_PROTOCOL` is 4. Every
  `Call` would have been unreadable and every `Result` fine, which
  presents as a broken server rather than a version mismatch. Found by
  reading CPython's source rather than by running it, since the failure
  needs the vendor's interpreter to appear at all. The authkey handshake
  is already cross-version safe: `_create_response` keeps the legacy MD5
  path for an unprefixed challenge.

- **`devices/gatan_bridge.py`** — a device server that runs *inside*
  Gatan Microscopy Suite, with a `simulated` backend that needs no Gatan
  software and is what CI exercises. DM-Script command names are
  constructor parameters with placeholders rather than hard-coded
  constants, because the imaging-filter energy-offset commands could not
  be verified from this environment and guessing them into the source
  would look like knowledge.

- **`docs/adapters/gatan.md`**, and a correction to this project's own
  recorded reasoning. `vendor-support.md` said a Gatan adapter "inverts
  the topology … a bridge running inside DM that connects *out*". Only
  half of that holds: what inverts is *ownership*, not direction.
  DM-Script has both `TCPSocketBind` and `TCPSocketConnect`, SerialEM's
  plug-in has listened inside DM for twenty years, and a published
  DM-SDK/ZeroMQ bridge already exists (Lei, Weber, Clausen & Wilbrink,
  *M&M* 30(S1), 2024). Direction is a firewall question.

  The document also leads with the finding that matters most to the
  facility that prompted it: **a Gatan spectrometer on a Nion column is
  probably already supported.** Nion's instrumentation kit models an
  optional `eels_camera` on the STEM controller, which `nion_server.py`
  already reads, and already maps `energy_offset_ev` onto Nion's
  `ZLPoffset` — the spectrometer drift-tube offset. Nion would not
  publish that control for a spectrometer it does not drive. So the
  SuperSTEM 2 case (UltraSTEM 100 + UHV Enfina) may need no Gatan code at
  all, and settling it costs one `describe()` call on the instrument PC.

- **A DECTRIS device server** (`miainwoodpecker.devices.dectris_server`),
  MIT and in-tree, driving ARINA/ELA/QUADRO/SINGLA-class detectors over
  the SIMPLON REST API — the third adapter, and the first *scientific
  detector with no column around it*. Served on the neutral `camera`
  target with no scanner. Control needs no vendor library at all:
  `urllib` and `json` are the whole dependency.

  The doubt that prompted it was well founded and turned out the right
  way. Gatan sells the ELA as the *Stela* and markets it as the only
  hybrid-pixel detector fully integrated with Gatan Microscopy Suite,
  which makes it look like a GMS peripheral. It is not: the detector
  control unit serves SIMPLON over HTTP itself, and the Nion/DECTRIS
  characterisation paper (Plotkin-Swing et al., *Ultramicroscopy* 217,
  113067) records a complete acquisition path — detector, fibre, DCU,
  10 GbE — with no Gatan software in it. GMS is a second front end.

  SIMPLON also settled where the pull-per-frame line falls, because the
  API draws it itself: the **stream** subsystem pushes every frame over
  ZeroMQ and is the recording path LiberTEM-live already consumes, while
  the **monitor** subsystem serves the latest image as TIFF over HTTP and
  drops frames by design. The adapter is built on `monitor` and does not
  touch `stream`. An ELA runs to 2250 fps full-frame; this path is tens
  of fps and says so. That is not a compromise — aligning a spectrometer
  while watching the zero-loss peak is what LiberTEM-live does not do,
  and a spectrum image is what a `Camera` should not.

  The simulated backend is a mock **control unit** rather than a stub
  camera: a real `http.server` serving the documented resource tree, the
  `na`/`idle`/`ready` state machine, 403 for read-only parameters and for
  configuring an armed detector, 404 for a wrong API version, 408 for an
  empty monitor buffer, and monitor images as real TIFF — with the same
  client pointed at it, so the tests exercise URL construction, JSON
  shapes, HTTP statuses and TIFF decoding rather than a stub that always
  succeeds. New `dectris` extra (`tifffile`) for the hardware backend.

  Metadata is deliberate where it would be easy to guess:
  `photometrically_linear: True` (threshold-discriminated integer counts,
  no gain curve or demosaic; the only real nonlinearity is count-rate
  paralysis, recorded beside it), no `high_tension_v` (the DCU's
  `incident_energy` is what the detector was *configured for* to set its
  discriminator, not a column reading), and **no calibration published**
  — the detector knows its 75 µm pitch and nothing about the optics
  between it and the specimen, so the axis stays honestly uncalibrated.

- **`docs/adapters/dectris.md`** — whether the ELA is reachable without
  Gatan's software, which SIMPLON subset an adapter needs, where the
  pull-per-frame line falls for a 2250 fps detector, what LiberTEM-live
  already covers, and an itemised list of what stays unverified until a
  detector is present. The eleven checklist entries it generates are in
  `docs/hardware-validation-checklist.md`, led by the one most likely to
  be wrong: this adapter's `ints` trigger arithmetic is the inverse of
  LiberTEM-live's, and only hardware settles which is right.

- **Hitachi is estimable after all.** `docs/vendor-support.md` said "no
  public API … not estimable", written without a search. The search found
  undocumented Python external-control modules (`MfExtCont`,
  `MfKeyMouse`, `MfCommon`) driving a Hitachi SU7000 FE-SEM in public
  code — `SetHv`, `GetStagePosition`, `RunStageMove`, `RunAutoAfc`,
  `RunScan`, which is `InstrumentController` and `Scanner` in everything
  but spelling — on the same product lineage as SuperSTEM 4's SU9000II.
  And the SEM external scan connector, which makes the scan purchasable
  from a third party even if the vendor says no, so unlike every other
  column vendor here a refusal still leaves a route to a scanned image.
  `docs/adapters/hitachi.md` has the full working: what drives the
  instrument, where the search looked and found nothing, what is
  reachable without vendor cooperation, what file-watching can and cannot
  do, a sendable vendor question list, and costed estimates for all four
  answers the vendor could give. Every claim is marked verified,
  reported, generalising or unverified — thirteen are unverified, each
  with what would settle it, and the central one (whether `MfExtCont` is
  on an SU9000II rather than an SU7000) is settled by looking at the
  instrument PC rather than by a negotiation.

- **`InstrumentController` was all-or-nothing to `isinstance`.**
  `available_controls()` exists so an instrument can serve some controls
  and not others, but the protocol was `runtime_checkable`, and that
  check demands every method regardless of what the instrument says it
  supports. Two adapters failed it while working perfectly
  (`camera_server.ServerInstrument`, `gatan_bridge.BridgeInstrument`), so
  the check tested for Nion-shapedness rather than conformance. Found
  independently by two adapters, which was the signal that it was the
  abstraction rather than the adapters. Now fixed — see the split into
  `Instrument` and `InstrumentController` under Fixed below.

- **A protocol gap in the instrument we already drive.** `Scanner`
  produces one channel per `scan_frame` call, on the stated premise that
  "a second channel is a second pass of the beam". That premise is false
  in general: a scanned instrument delivers one or more signals
  *simultaneously* from a single pass — HAADF and MAADF together on a
  Nion UltraSTEM, and BF plus each HAADF segment plus SE plus both BSE
  signals on a segmented-detector SEM. Requesting them serially costs a
  pass of dose per channel, takes as many times longer, lets the specimen
  drift between them, and makes DPC/iDPC/centre-of-mass **invalid**,
  since those difference segments at the same probe position. Recorded
  under "What is still the wrong shape" and sized (3–5 d). The fix is a
  multi-channel call, not a `scan_id`: an identifier alone would assert a
  shared acquisition that did not happen, which is the fiction the
  `Frame` docstring was right to refuse. First written down as a Hitachi
  finding and corrected — it is not vendor-specific and should not wait
  for a vendor.

- **A device server for commodity cameras**
  (`miainwoodpecker.devices.camera_server`): USB microscopes, webcams, and
  recorded video files, over OpenCV's `VideoCapture` — no vendor SDK, MIT,
  in-tree. Two backends like the Nion server: `simulated` synthesises
  moving frames and needs nothing installed, `hardware` opens a real
  device (the `camera` extra). Frames carry `photometrically_linear:
  False` and name their `colour_conversion`, because a UVC camera's pixels
  have already been through demosaicing, gamma and white balance and are
  an image rather than a measurement. Binning other than 1 is refused,
  since consumer sensors crop. The camera arrives on a new neutral
  `camera` target rather than being called a Ronchigram camera.

- `devices/serving.py`: the vendor-free half of the server protocol —
  dispatch, the connection loop, and the accept loop — extracted from
  `nion_server` and shared with the camera server. Lifecycle deliberately
  stays with each adapter: a webcam has no beam to park.

- **The viewer runs against a camera-only device server.**
  `--server-module MODULE` names the module the client launches, so the
  shipped application can drive `miainwoodpecker.devices.camera_server`
  (a USB microscope, a webcam, or a video file replayed as a fixture) or
  an out-of-tree vendor adapter, instead of only the Nion server it ships
  with. This does not weaken the licence boundary: the named module is
  launched as a subprocess, never imported by the application.
  `--backend simulated --server-module
  miainwoodpecker.devices.camera_server` needs nothing installed beyond
  the viewer and exercises the whole live path — display, recording,
  session, analysis buttons — which makes it the cheapest end-to-end test
  of the application.

  `LiveInstrumentWidget` accordingly takes an *optional* scanner, and
  requires a scanner or a camera rather than assuming both. The Scan
  group is not built at all without a scanner, so the absent device is
  missing from the window rather than present and broken, and every scan
  entry point is inert instead of raising — `stop_scan()` in particular
  returns True, because its callers read False as "the device is still
  busy, do not proceed". The camera is chosen through `cameras()` with
  the Ronchigram still preferred, so a server this viewer has never heard
  of can supply the live view. `app.py` previously exited with "this
  device server serves no scanner"; it now exits only when there is
  neither a scanner nor a camera.

  The disk-space warning follows the same rule. A scan's frame shape is
  set by the operator before anything is acquired, so it can be estimated
  up front; a commodity camera's is whatever the driver negotiated, so
  without a scanner the estimate waits for a real frame and stays silent
  until one arrives. Free space is still reported throughout — inventing
  a shape would put a number on screen that no acquisition would produce.

### Fixed

- **The instrument runtime check no longer demands controls an
  instrument does not serve.** The `isinstance` question every call site
  was actually asking — "is this an instrument target I can hold a
  session against" — is now its own `runtime_checkable` protocol,
  `Instrument`: identity (`stage_size_nm`), capability
  (`available_controls`), lifecycle (`park`). `InstrumentController` is
  that core plus the per-control methods, for static typing, and is
  deliberately no longer `runtime_checkable`, so the old all-or-nothing
  question raises `TypeError` instead of quietly failing partial
  adapters. `camera_server.ServerInstrument` (zero controls) and
  `gatan_bridge.BridgeInstrument` (one control) both pass the runtime
  check now, each pinned by a test; which *controls* exist is asked
  through `available_controls()`, and the sweep generators' graceful
  "control not available" refusals are unchanged.

- **The port-collision retry no longer depends on a stopwatch.**
  `_free_port()` probes a port and releases it, so the server binds it
  seconds later and can find it taken; the loser exits with status 4 and
  the client is meant to re-pick and respawn. Detection was
  `process.wait(0.4)` immediately after the spawn, which was wrong in
  both directions: a healthy server never exits, so **every** good
  startup paid the full 0.4 s for an answer already known, and a machine
  loaded enough to take longer than 0.4 s to reach its bind — the machine
  most likely to collide in the first place — turned a curable collision
  into an anonymous startup error. Seen once in CI after `TARGET_NAMES`
  grew to five ports.

  The fixed wait is gone and the retry now spans the whole
  spawn-and-connect. `_connect_with_retry` already polls the child on
  every attempt, so when it finds it dead with the port-collision status
  it raises a distinguishable internal error rather than the generic
  diagnostic, and `_spawn_and_connect` answers that with fresh ports, a
  fresh child, and a fresh connect deadline — releasing any connections
  the doomed attempt had already made, since the health connection and
  the per-target connects come *after* the first one. Every other exit
  status keeps its diagnostic verbatim, because a missing instrument or
  an unimportable adapter module would only fail again. A persistent
  collision still ends at the existing attempt budget with the "claiming
  localhost ports faster than they can be used" message. Net: healthy
  startups are 0.4 s faster, and a collision noticed at any point before
  the session is connected is retried rather than fatal.

- **EDS beam current reached eXSpy a billion times too small.**
  `load_as_eds_signal` wrote `beam_current_a` straight into
  `Acquisition_instrument.TEM.beam_current`, which eXSpy reads as
  **nanoamps** — `exspy/signals/eds_tem.py`'s dose calculation multiplies
  it by 1e-9 to reach coulombs, and says so in a comment. A 200 pA probe
  therefore arrived as 2e-10 nA, making every dose-based quantification
  wrong by 1e9 with nothing saying so. Found while mapping the same
  metadata tree for EELS. Now converted in the one place all the other
  unit conversions live, and `docs/adapters/spectrum-detectors.md` §4's
  units table gained the row that would have prevented it.

- **Phase 2's napari-versus-`ndv` question is closed: keep napari.**
  Measured on an M2 Pro across a 16× range of frame sizes, display cost
  is flat — 12.2 / 11.2 / 11.4 ms at 512² / 1024² / 2048² — so it is
  napari's fixed per-update overhead rather than upload or draw. That
  diagnosis is the one `ndv` addresses, and it inverts the conclusion: a
  fixed cost is amortised exactly where it would hurt, since every real
  workload's frame time scales with data and this does not. Display is
  4.7% of a 512² scan frame's beam time and 0.27% of a 2048² one. The
  one regime where 11 ms would bite — small frames at high rate — is
  already routed to LiberTEM-live.

- **Display responsiveness under analysis load is measured, and the fix
  is ours rather than the viewer's.** On the M2 Pro, acquire is unmoved
  by CPU contention (5.5 → 5.7 ms median from zero to eight competing
  numpy workers) because a grab is IPC and a shared-memory read, not
  computation — so contention lands on display alone. Display degrades
  in the tail a full load level before the median: at four workers the
  median *improves* to 3.1 ms while p95 triples to 23.0 ms, which makes
  the benchmark's median-derived frame rate misleading exactly where a
  user first notices trouble. At eight workers the worst update is 4031
  ms — the GUI thread descheduled outright, which no per-update
  efficiency addresses. The conclusion is a scheduling constraint on our
  own code, fixed in the next entry: our `viewer/jobs.py` already runs one
  job at a time, and it is the numpy/BLAS and LiberTEM threads inside it
  that take every core. Also confirms by measurement what `viewer/live.py`
  implied: the camera path costs half the scan path (5.6 ms against 11–12
  ms), because only the scan view autocontrasts every frame.

- **Analysis no longer takes every core out from under the GUI thread.**
  New `analysis/threads.py` resolves one number — `os.cpu_count()` minus
  two, floored at one — and two places apply it: `AnalysisJob` runs every
  analysis inside a `threadpoolctl` limit of that many threads, and the
  LiberTEM button's `Context` now comes from `analysis_context()`, which
  passes the same number to `InlineJobExecutor(inline_threads=...)`. The
  executor knob is needed separately because "inline" bounds the
  *executor*, not the numerics: unconfigured, it still asks for one
  fine-grained thread per physical core and hands that to numba, which
  `threadpoolctl` cannot reach. The floor matters most on the machines
  least able to absorb the problem — a two-core laptop would otherwise get
  a zero-thread limit, which BLAS reads as "use everything" and numba
  refuses outright.

  `OMP_NUM_THREADS` and friends are deliberately not set: they are read
  when the native library loads, so writing them from inside a running
  application is a no-op that looks like a fix. Two limitations stated
  rather than papered over, both also in the module docstring: the runtime
  setters underneath `threadpoolctl` are process-global, so the cap is
  scoped to the *duration* of an analysis rather than to the worker
  thread; and `os.cpu_count()` reports the machine rather than a
  container's CPU quota, since `os.process_cpu_count()` needs Python 3.13
  and this package supports 3.11. New dependency: `threadpoolctl` in the
  `analysis` extra only — `libertem` and `py4dstem` already carried it
  transitively.

- `scripts/phase2_live_benchmark.py` compared display cost against the
  *simulator's* acquire time, which is not what gates a live view. On the
  first hardware-accelerated run that denominator produced "display
  dominates … the empirical argument for ndv" from a 2.05× ratio that
  meant nothing of the sort: the simulator makes a 512² frame in 5.4 ms
  where a real 1 µs-dwell scan takes 262 ms, against which display is
  4.2% of a frame. The verdict now divides by the scan's physical
  duration, reports the sustainable frame rate separately as the ceiling
  a camera-rate source actually faces, and says what experiment would
  decide the remaining question.

### Changed

- `SharedFrameReader` gained an opt-in `stop_tracking=` flag that
  unregisters each attached segment from *its own* process's
  `resource_tracker`. The device layer's behaviour is unchanged (the flag
  defaults off); the analysis worker needs it because it inverts the
  device layer's lifetimes — there the reader is the long-lived
  application, here it is a subprocess whose tracker was unlinking the
  client's live segment on exit. Not the cross-process `unregister` that
  `shared_frame.py` records as having made things worse: this one talks
  to the daemon that did the registering.
- `SharedFrameWriter`/`SharedFrameReader` gained `publish_array` /
  `read_array` and a `SharedArrayRef`, for payloads that are arrays with
  no `Frame` or `Spectrum` around them.

- Analyzing a recording opened in the viewer now reads it **once**, not twice.
  Each analysis adapter grew an in-memory entry point beside its
  file-reading one — `hyperspy_signal_from_frames`,
  `hyperspy_spectrum_from_frames`, `libertem_dataset_from_frames`,
  `diffraction_slice_from_frames` — and the viewer hands them the frames the
  load already read rather than pointing them at the path. The path-taking
  functions are unchanged and are now one call to their in-memory half, so
  the two cannot drift and no script or document has to change.

  Separate names rather than one function accepting a path *or* an array:
  whether a call decompresses a 2048×2048 recording is exactly what the
  caller is choosing, so it belongs in the name rather than in an
  `isinstance` check at the bottom of the stack.

  What made this an adapter API change rather than a wiring change is the
  calibration. Frames handed over without it produce a signal whose axes
  silently claim bare pixels — a worse bug than the duplicated read — so the
  carrier is `FrameStack`, the `(data, frame_time, calibration)` triple
  `read_frames` has always returned, now a named tuple so every existing
  unpacking still works. `LoadedRecording` carries the file's calibration
  too, and `LoadedRecording.frames` declines to offer the frames at all when
  they are not the whole recording: a truncated read, or an unfinalized file
  that never wrote its axes. The viewer additionally re-checks the frame
  count against the file, and falls back to the path when they disagree.

  LiberTEM's in-memory form is `MemoryDataSet` via
  `ctx.load("memory", data=..., sig_dims=2)`, which measured the same
  navigation/signal shape its HDF5 reader infers from the same recording.
  `sig_dims` is explicit because the same call on a 2D array yields a
  dataset with *no frames to navigate* rather than an error, so a flat
  single frame is refused with a sentence. LiberTEM's own note that
  `MemoryDataSet` suits a distributed executor poorly is a reason to keep
  the file-reading form, not to avoid this one: the viewer runs an inline
  executor, where there is no worker to ship an array to.

  A fresh analysis burst deliberately still reads the file it just wrote:
  its frames' calibration is only resolved when `NexusWriter` writes them,
  and short-circuiting that would mean a second implementation of the rule
  that decides what a recording's axes are.

- CI's `integration` job runs its tests in parallel (`pytest -n auto`),
  cutting that suite from ~140s to ~52s. The worker count had to be
  measured against the whole command: without coverage the suite is
  bound by waiting on subprocesses and `-n 8` wins, but coverage makes
  it CPU-bound and the ordering inverts (`-n 8` is 132s against `-n 4`'s
  52s on four cores). The base `test` matrix stays serial, where xdist
  measured slower than the 3.4s it would save.

- Default HDF5 compression is now gzip + byte shuffle, which measured smaller,
  faster to write, *and* faster to read than plain gzip on every dataset
  (Ronchigram frames 0.694 → 0.532 ratio with write time roughly halved). Faster
  blosc2 codecs are opt-in behind a new `compression` extra, because a
  plugin-compressed file cannot be read without `hdf5plugin` installed.
- `storage/legacy.py` reads `.ndata` with the standard library instead of Nion's
  `NDataHandler`, so the MIT application no longer imports GPL-3.0 code
  in-process. This module now needs no optional dependency group.

### Fixed

- The client re-picks ports and respawns when the device server reports
  one was already bound. `_free_port()` probes a port and *releases* it,
  so anything on the machine can claim it before the server binds
  seconds later; the collision previously surfaced as an anonymous
  traceback and a dead server. Rare serially, and likely enough under a
  parallel test run to matter.
- The device server crashed at startup: the connection-accounting methods
  added for orphan detection landed on `NionInstrument` rather than
  `_ServerSession`, so its accept thread and watchdog died with
  `AttributeError`. Nothing caught it before CI, because no test runnable
  without the `device` extra executes `serve()`.
- A client connecting to a half-dead server hung forever rather than
  failing: the crashed accept thread left the port open, so TCP connected
  but the authentication handshake never completed, and
  `multiprocessing.connection.Client` has no timeout. Connection attempts
  are now bounded by the existing connect deadline even mid-handshake.
- `NexusWriter` no longer declares `definition = "NXem"` by default. Validation
  showed the files did not conform (a required `NXsample` group was missing), so
  they now claim no application definition unless real specimen metadata is
  supplied.
- `--plugin` precedence: argparse's `append` action added to its
  environment-seeded default, so an explicit flag extended
  `MIAINWOODPECKER_HARDWARE_PLUGINS` rather than replacing it.
- The shared-memory leak test named its own segments instead of diffing
  whole-directory `/dev/shm` snapshots, which was order-dependently flaky.
- `scripts/phase2_live_benchmark.py` imported the long-removed
  `devices.nion_adapter` and could not run.
