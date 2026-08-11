# Change Log

## Unreleased

### Added

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

### Fixed

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
