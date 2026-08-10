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

## Re-benchmark

- [ ] Re-run `scripts/ipc_overhead_benchmark.py` and
      `scripts/phase2_live_benchmark.py` at real frame rates and sizes. The
      64KB shared-memory threshold was fitted against usim's frame sizes and
      one container's memory bandwidth. (Note `phase2_live_benchmark.py`
      currently imports the long-removed `devices.nion_adapter` and needs a
      one-line fix first.)

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
