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
