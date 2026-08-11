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

### Changed

- Default HDF5 compression is now gzip + byte shuffle, which measured smaller,
  faster to write, *and* faster to read than plain gzip on every dataset
  (Ronchigram frames 0.694 → 0.532 ratio with write time roughly halved). Faster
  blosc2 codecs are opt-in behind a new `compression` extra, because a
  plugin-compressed file cannot be read without `hdf5plugin` installed.
- `storage/legacy.py` reads `.ndata` with the standard library instead of Nion's
  `NDataHandler`, so the MIT application no longer imports GPL-3.0 code
  in-process. This module now needs no optional dependency group.

### Fixed

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
