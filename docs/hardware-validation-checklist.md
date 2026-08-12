# Hardware day: validation checklist

Everything in this project has been built and tested against
`nionswift-usim`. This is the procedure for the parts that *cannot* be
verified against a simulator, ordered so each step's failure is diagnosable
before the next one runs. Anything not on this list is already covered by
the automated suite.

Context and the reasoning behind each item are in
[the migration plan](migration-plan.md) — §5 Phase 1 for the six unverified
assumptions (referenced below as **Assumption 1–6**), §5 Phase 3 for the
instrument controls, and §6 for shutdown.

For the other side of the line — the work that *can* be done first, and
where its specification already exists — see
[work that does not need the instrument](pre-hardware-work.md). Two items
on this checklist turn out to be avoidable that way rather than
verifiable: the EELS dispersive axis is reported by the device, and the
calibration values themselves come from instrument controls.

## Before touching the instrument

- [ ] List what is actually installed on the instrument control computer:

  ```
  python -c "import nionswift_plugin, pkgutil; print([m.name for m in pkgutil.iter_modules(nionswift_plugin.__path__)])"
  ```

  Record the vendor plug-in module name(s). If any is a name this project's
  autodiscovery skips, that skip list needs amending (**Assumption 6**).

- [ ] Confirm the vendor plug-in registers from a module-level `run()`:

  ```
  python -c "import nionswift_plugin.<name> as p; print(callable(p.run), callable(getattr(p, 'stop', None)))"
  ```

  **Assumption 1.** If `run()` is absent or does not register,
  `_load_device_plugins` needs a second discovery strategy.

- [ ] Run the device server alone, with no application:

  ```
  MIAINWOODPECKER_AUTHKEY=00 python -m miainwoodpecker.devices.nion_server \
      --backend hardware 5001 5002 5003 5004
  ```

  Expect it to bind and block. A `HardwareNotAvailableError` here means the
  registry components were not found — its message names which.

- [ ] Check the camera labels the registry reports (`camera_device.camera_type`
      for each `camera_module`). **Assumption 2.** If they are not
      `"ronchigram"`/`"eels"`, `_cameras_from_registry`'s classification
      needs the real vocabulary.

- [ ] Check `stage_size_nm` is published and plausible rather than the 1 µm
      fallback. **Assumption 5.** A wrong value silently produces wrong
      default fields of view.

## Instrument controls — beam off or blanked where possible

- [ ] `instrument.available_controls()` must report all three. A missing one
      means that vendor control name is wrong (**Assumption 3**).

- [ ] If `stage_position` is missing but the stage works, the `GetVal2D`
      axis-keyword hedge is the suspect (**Assumption 4**). Confirm by
      calling `GetVal2D("stage_position_m")` with and without
      `axis=("x","y")` directly.

- [ ] Read `defocus_nm()` and **sanity-check the magnitude**. This is the
      single highest-consequence check on this list: a metres/nanometres
      mix-up shows up as a factor of 1e9, not a rounding error, and a 1e9
      error in a defocus setpoint is a physical command to the column.

- [ ] Set defocus to a *small* known offset, confirm read-back, then confirm
      on the operator's own console that the column agrees. **Do not trust
      read-back alone** — a control that echoes its setpoint without acting
      is exactly the failure the migration plan records for
      `probe_position`.

- [ ] Same for stage position, with a small move, confirming direction and
      axis order: `set_stage_position_nm(y, x)` must move the *slow* scan
      axis for `y`. Sign conventions are unverified; note Nion's own
      `change_stage_position` negates both axes.

- [ ] Blank the beam via `set_beam_blanked(blanked=True)` and confirm on the
      instrument — a current reading or a dark detector — not just via
      `is_beam_blanked()`.

## Effect on data — re-run the measurement, do not assume

- [ ] Re-run `python scripts/device_control_verification.py` against the
      hardware backend and record the real ratios beside the simulator's.
      Expect the three simulator no-ops — defocus-on-scan,
      blanking-on-EELS, blanking-on-scan — to become real effects. **If any
      is still 1.0× on hardware, the control is not reaching the column**
      and the read-back above was a lie.

- [ ] Run a real `focal_series` over defocus and confirm the *images* change
      through focus, not just the metadata. This is the one thing the
      simulator provably cannot demonstrate.

## Shutdown, with the instrument live

- [ ] Acquire enough frames to cross the shared-memory threshold, then let a
      session tear down normally. Confirm the report shows
      `beam_blanked: true`, `errors: []`, and that the beam really is
      blanked afterwards.

- [ ] Confirm `/dev/shm` gained nothing: `ls /dev/shm | grep psm_`.

- [ ] **Time the park.** The 10s `_SHUTDOWN_TIMEOUT_S` was chosen for a
      simulator where blanking is a flag flip; on hardware it is a physical
      operation. If a real park takes longer, the client will SIGTERM a
      server that was mid-park — raise the timeout.

- [ ] Test the fallback deliberately (`MIAINWOODPECKER_WEDGE_SHUTDOWN=1`)
      **with the beam on**, and decide whether "the application killed the
      server without parking" is acceptable for this instrument or whether a
      hardware watchdog or interlock is needed as well. This is a policy
      question the code cannot answer.

- [ ] Poll `instrument.check_health()` through a real acquisition and confirm
      it stays responsive. It deliberately takes no device lock, so it should
      answer while a long exposure is in flight; if it blocks on real
      hardware, something in the vendor path serializes more than usim does.

- [ ] **`SIGKILL` the server with the beam on**, and decide whether the
      resulting state is acceptable for this instrument. The client will fail
      fast and name the cause, but nothing parked the column — this is the
      same policy question as the wedged-server case, and it is the more
      likely one.

- [ ] After that kill, check `/dev/shm | grep psm_`. Expect nothing, because
      the server's `resource_tracker` child normally reclaims segments — but
      that is a CPython implementation detail, not a guarantee. If anything
      survives here, the tracker died with the server (a process-group or
      cgroup kill), which is the case the client's `unlink_orphan()` sweep
      exists for.

- [ ] Confirm a killed server's client-side error names the cause on this
      platform. Signal reporting via `Popen.poll()` is POSIX-shaped; a Windows
      instrument-control computer will report differently.

- [ ] Run the server once with `MIAINWOODPECKER_DEVICE_LOG_LEVEL=INFO` and
      keep the output. On hardware day it records the backend, each plug-in's
      load outcome, and the bound ports — which is the fastest way to see what
      the vendor stack actually registered. Use
      `MIAINWOODPECKER_DEVICE_LOG_FILE` to keep it out of the operator's
      terminal.

## Re-benchmark

- [ ] Re-run `scripts/ipc_overhead_benchmark.py` and
      `scripts/phase2_live_benchmark.py` at real frame rates and sizes. The
      64KB shared-memory threshold was fitted against usim's frame sizes and
      one container's memory bandwidth. Run the live benchmark at more than
      one scan size: on the development box the display/acquire ordering
      *reverses* between 512² and 1024², so a single size is not a verdict.

- [ ] Re-run `scripts/nexus_compression_benchmark.py` on real detector
      frames. The gzip+shuffle default was chosen on simulated data whose
      noise characteristics may differ, and how long a real write takes is
      what decides whether the per-frame `flush()` is a nicety or a
      requirement.

## Storage claims

- [ ] Confirm the real detector dtype. If real scan data is genuinely
      `float32`, the `float64` promotion noted in §7 is simulator-only and
      the storage default should be revisited.

- [ ] Supply real specimen metadata (`sample=`) so files can legitimately
      declare `definition="NXem"`. Until then they deliberately declare no
      application definition rather than claiming one falsely.

- ~~Confirm which EELS axis is the dispersive one.~~ **No longer needed.**
      The device reports it: the dispersive axis is the one whose
      `calibration_controls` units are `eV`. A rotated spectrometer needs no
      configuration change. Worth a glance at a real recording's axes anyway,
      but there is nothing here to get backwards.

- [ ] Confirm the diffraction-plane **units** a real camera reports through
      its `calibration_controls`. usim reports `rad`, which this project
      records as an angle axis; a vendor reporting `1/nm` would be recorded
      as reciprocal space, and one reporting a spelling outside this
      project's vocabulary degrades to a pixel axis — silently by design, so
      **check the axes on the first real recording** rather than assuming.
      Converting angle to reciprocal space needs the electron wavelength,
      which the code deliberately refuses to invent.

## DECTRIS detector — before arming, then with beam

Everything in [`devices/dectris_server.py`](../src/miainwoodpecker/devices/dectris_server.py)
was verified against a mock control unit built from the published SIMPLON
documentation. **No item below has met a real detector.** Full reasoning
and sources: [adapters/dectris.md](adapters/dectris.md).

- [ ] `curl http://<dcu>/detector/api/1.8.0/config/description` — confirm
      the ELA serves **1.8.0** at that path. A different version 404s
      cleanly and the adapter says so, but the version is a guess until
      checked.

- [ ] Read `.../status/state` before starting anything. It must be
      `idle`. `ready` or `acquire` means GMS/Stela, Nion Swift, or a
      LiberTEM-live session already owns the detector — that is the
      ownership interlock, and the point is that it is diagnosed here
      rather than three calls later.

- [ ] **Highest-consequence item on this list: the trigger-mode
      arithmetic.** The adapter uses `trigger_mode=ints`, `nimages=1`,
      `ntrigger=65536` — one image per software `trigger`. LiberTEM-live's
      controller uses the inverse for `ints` (`nimages` = the whole
      series, `ntrigger=1`). If the ELA's firmware treats one `trigger` in
      `ints` as starting the *whole* series, `acquire_frame()` will
      free-run and the monitor buffer will lag. Confirm by arming, sending
      exactly one `trigger`, and checking that
      `monitor/status/buffer_fill_level` (or successive `images/next`
      calls) shows **one** image, not `nimages`. If it is wrong, switch to
      `inte` or re-arm per frame.

- [ ] Enable the monitor (`PUT monitor/api/1.8.0/config/mode` =
      `"enabled"`) and confirm `GET monitor/api/1.8.0/images/next` returns
      **408** on an empty buffer rather than blocking. The adapter's poll
      loop depends on it.

- [ ] Save one monitor image and inspect its TIFF tags: compression, byte
      order, bit depth, strip layout. The built-in decoder handles
      uncompressed little-endian strips only and refuses anything else by
      name; if the ELA sends something else, the `dectris` extra is
      required rather than optional and the docs should say so.

- [ ] Confirm the configuration really is **read-only while armed** (`PUT
      count_time` while `ready` must fail). `configure()`'s
      disarm/write/re-arm depends on it; if writes are accepted while
      armed, the dance is unnecessary and costs a frame.

- [ ] Check which of `description`, `detector_number`, `software_version`,
      `incident_energy`, `threshold_energy`, `roi_mode`,
      `sensor_thickness` an ELA actually publishes. Absent keys are
      tolerated and omitted from the metadata, but which are absent is
      unknown.

- [ ] **Measure the achievable frame rate** through `acquire_frame()`. One
      HTTP round trip per frame predicts tens of fps; record the number
      rather than leaving the docstring's estimate standing.

- [ ] Sanity-check the `count_time` round-trip magnitude (seconds versus
      milliseconds). A 1000× exposure error is a wasted session, not a
      rounding error.

- [ ] Confirm `stop()`/`close()` leave the detector `idle`, then hand it
      to GMS or LiberTEM-live and back. A detector left armed is silently
      denied to everything else.

- [ ] Try **LiberTEM-live against the ELA** (`DectrisConnectionBuilder`).
      It documents ARINA and QUADRO and does not name the ELA; whether
      that is a documentation gap or a real one decides whether the
      streaming half needs an upstream fix.

## Gatan spectrometer — and the one check that may make it moot

Full reasoning and sources: [adapters/gatan.md](adapters/gatan.md).

- [ ] **Settle the Nion question first — this is cheap and may close the
      whole case.** On SuperSTEM 2's control computer, run the existing
      Nion server with `--backend hardware` and record what `describe()`
      reports. If `eels_camera` and `energy_offset` are there, the UHV
      Enfina is already supported through Nion with no Gatan code, and
      every item below becomes unnecessary for that instrument. Nion
      publishes `ZLPoffset` — the spectrometer drift-tube offset — which
      it would not do for a spectrometer it does not drive.

- [ ] If it is *not* there: identify what does read the Enfina — GMS, a
      Gatan controller with its own interface, or something site-specific.

- [ ] Confirm GMS's embedded Python version (`import sys; sys.version` in
      DM's Python window) and whether `pip install miainwoodpecker` is
      possible inside `GMS_VENV_PYTHON`. The pickle cap assumes 3.7; if
      GMS has moved on, the cap is unnecessary but harmless.

- [ ] Run the bridge's `simulated` backend *inside GMS* against a client
      on the same network. This exercises the transport, the pickle
      protocol cap and the authkey handshake across the real interpreter
      pair, with no hardware at risk. Do this before anything touches a
      detector.

- [ ] Find the real imaging-filter DM-Script commands for this
      spectrometer and record them, then replace the placeholder snippets.
      They are constructor parameters precisely because they could not be
      verified without the vendor's documentation.

- [ ] Confirm `DM.GetFrontImage()` returns the live spectrum image while a
      live view runs, and that `GetNumArray()` gives the expected shape
      and dtype.

- [ ] Measure the pickle-only frame cost at this detector's real frame
      size. Attached links deliberately do not use shared memory, because
      the peer may be on another machine.
