# Migration plan: a Nion Swift replacement built from existing open source parts

## 1. Goal and guiding principle

Build a Python-first application that covers what [Nion Swift](https://github.com/nion-software/nionswift)
does today for STEM operation — instrument control (scan, camera, spectrometer),
live acquisition viewing, and data analysis — while minimizing bespoke code.

The guiding principle, carried over from the earlier architecture review of
Swift: **retire custom code by adoption, not rewrite.** Swift's core problem
isn't that it's written in Python — it's that it reinvented a UI toolkit,
a rendering pipeline, a reactive event system, and a data format that the
wider scientific Python and microscopy communities have since solved and
kept improving. This app should look like the *thinnest possible glue layer*
between:

- Nion's own existing, GPL-3.0, hardware-tested device layer (the one part
  with no real substitute), and
- actively maintained, community-supported open source projects for
  everything else (viewer/UI, rendering, data format, and analysis).

## 2. What has no substitute, and must be kept

**The instrument device layer.** No other open source project speaks to
Nion microscope hardware (scan coils, cameras, spectrometers). Rewriting
this would be the highest-risk, highest-effort, least-validated part of the
project, for a component that already works.

**Scope decision**: hardware is Nion-only for now. The device bridge
(Phase 1) is still designed behind a vendor-neutral interface (a small
`Protocol`/ABC per device kind — camera, scanner, etc. — rather than the
rest of the app importing `HardwareSource` classes directly), so a second
vendor's SDK could be added later as another implementation of the same
interface without touching the acquisition, UI, or storage code. This is
a design constraint to hold to now, not work to do now — no non-Nion
adapter is being built until it's actually needed.

That claim has since been audited against what the other vendors actually
publish, and costed:
[**Other vendors**](vendor-support.md). Two findings changed the code
rather than the plan — the client could only ever launch *our* server
module, which is now a parameter, and the device target names are still a
fixed Nion-shaped tuple, which is the one redesign a second adapter cannot
work around.

Reuse directly:

- [`nionswift-instrumentation-kit`](https://github.com/nion-software/nionswift-instrumentation-kit) —
  base classes for STEM cameras and scanners (GPL-3.0).
- [`nionswift-usim`](https://github.com/nion-software/nionswift-usim) — a
  software STEM/scan/camera simulator (GPL-3.0), so device-layer and UI work
  can proceed without live hardware access.

These are Python packages already decoupled from Swift's UI process in
principle (they're driven through Swift's plugin/`HardwareSource` API); the
work here is a thin adapter, not a rewrite. **All of this is GPL-3.0**;
see §6 for how the rest of the application avoids inheriting that license
by isolating this layer behind a process boundary.

## 3. What gets replaced with existing open source projects

| Swift subsystem (reinvented) | Replacement | Why |
|---|---|---|
| NionUI (custom widget toolkit + C++ Qt launcher) | **PySide6** for the application shell, **[napari](https://napari.org/)** for the image panels | Deletes the launcher, the widget abstraction, and the declarative-UI layer in one move. **Corrected after building it: napari is not the shell.** The window is an ordinary `QMainWindow` (`viewer/documents.py`) whose central widget is an MDI area, because every image needs its own canvas — see the note below. |
| `CanvasItem`/`DrawingContext` (Python-driven command-list rendering, CPU rasterized via `QPainter`) | Napari's **VisPy/OpenGL** canvas | GPU compositing instead of Python rebuilding a draw program every repaint — this is the single biggest latency fix Swift needs. |
| `nionutils` (`Event`, `Observable`, `Binding`, `Stream`, `Registry`) | Qt signals/slots, and napari's own **evented models** (`psygnal`) | Standard, widely used reactive primitives instead of a bespoke fan-out graph that amplifies small property changes into cascades. |
| Custom project format + `.ndata` | **HDF5/Zarr** for arrays, **NeXus/NXem** for metadata, via **[pynxtools-em](https://github.com/FAIRmat-NFDI/pynxtools-em)** and **[RosettaSciIO](https://github.com/hyperspy/rosettasciio)** | NXem is a real, current (Oct 2025) NeXus application definition specifically for electron microscopy; RosettaSciIO (spun out of HyperSpy) already reads/writes essentially every EM vendor format (dm3/dm4, EMD, ser, …), so file I/O becomes "use the library" instead of "maintain a format." |
| Built-in analysis tools | **[HyperSpy](https://hyperspy.org/)** for general multidimensional EM analysis (EELS/EDS/etc.), **[py4DSTEM](https://github.com/py4dstem/py4DSTEM)** and/or **[LiberTEM](https://libertem.github.io/LiberTEM/)** for 4D-STEM / high-throughput pixelated-detector data, **[pyxem](https://github.com/pyxem/pyxem)**/**[kikuchipy](https://kikuchipy.org/)** for diffraction workflows if needed | These are the community's actual analysis tools for this data, actively maintained, and already ahead of what a small team can build and keep current. |
| `Facade.py` (hand-maintained versioned API shim) | Not needed | Only existed to keep Swift's own plugin API stable across versions. This anticipated new "plugins" being ordinary napari plugins; in the event, analysis went through direct HyperSpy/py4DSTEM/LiberTEM adapters in subprocesses instead (§6 licence isolation, and crash containment), so there are no plugins of either kind and still no API surface to shim. |

### What napari is actually for here — corrected after Phase 5

The table above overstated napari's role, and the viewing-area work made
the gap concrete enough to be worth recording rather than quietly
fixing.

**What was claimed and did not happen.** The plugin ecosystem was a
headline reason and is entirely unused — no npe2 manifest, no entry
points, no `magicgui`. That is not a failure; analysis deliberately went
out-of-process to adapters instead, for licence isolation and crash
containment, and that decision retired the argument.

**What napari is genuinely providing**, in rough order of value:

1. **The VisPy/OpenGL canvas** — the CanvasItem/DrawingContext
   replacement, and still the single biggest latency fix. Benchmarked
   below: flat ~11 ms per repaint from 512² to 2048², sixteen times the
   pixels for no extra cost.
2. **The `Shapes` layer** — thirteen interaction modes, vertex editing,
   and `to_masks()`. This is what an EELS ROI-sum interaction is built
   out of, and interactive shape editing is exactly the kind of
   low-level primitive Swift's mistake was to write by hand.
3. **Per-axis units and calibrated display** (`viewer/axes.py`), and the
   frame slider a recording's stack gets for free.
4. **The isotropic camera**, which is why "no viewing change stretches an
   image" is structural rather than something the layout must enforce.

**Where the grain runs against us.** napari's model is one canvas with N
layers sharing one world, and this application needs one canvas per
image — because calibration is per image. napari applies units per layer
but draws the scale bar per viewer, and refuses to render units at all
when one viewer's layers disagree:

```
WARNING: Inconsistent units across layers; units will not be used for rendering.
```

A HAADF map in nm beside a Ronchigram in mrad beside an EEL spectrum in
eV therefore *cannot* share a canvas. So the MDI arrangement is a
requirement rather than a preference, and the cost of it —
reparenting the private `_qt_window`, hiding each viewer's own chrome —
is the price of using napari as a panel rather than as a shell.

**On `ndv`.** The benchmark below settled the *speed* question and still
does. The ROI requirement settles the *capability* one in the same
direction and more firmly: `ndv` has no shapes layer, so choosing it
would mean building interactive ROI editing ourselves — the Swift
failure mode, arrived at from the other end. Revisit only if napari's
per-viewer overhead becomes a problem at panel counts we actually
reach.

## 4. Direct prior art to study before building

Two existing projects have already solved close variants of this exact
problem — read their source and docs before designing our own adapters:

- **[Odemis](https://github.com/delmic/odemis)** (Delmic) — open source
  (GPL-2.0), Python, microscope control + live acquisition GUI + analysis,
  for a different vendor's hardware (SECOM/DELPHI/SPARC). Its `odemis.model`
  device-abstraction layer (Pyro-based, out-of-process device access) is
  worth reading closely, even though we won't depend on it — it's the
  closest thing to "someone already built this shape of app." Its GUI is
  wxPython, which is dated; that's a design choice to avoid, not copy.
- **[napari-micromanager](https://github.com/pymmcore-plus/napari-micromanager)** /
  **[pymmcore-plus](https://pymmcore-plus.github.io/pymmcore-plus/)** — the
  closest architectural precedent for exactly the composition we want:
  hardware device layer + Qt widgets + napari as the live-viewer shell.
  Important current signal: the pymmcore-plus team is moving toward
  **[`ndv`](https://github.com/pyapp-kit/ndv)**, a napari-independent
  viewer, specifically because napari's overhead was too high for their
  highest-frequency live-acquisition loops. We should benchmark our own
  live scan-image update rate early (Phase 2 below) rather than assume
  napari is fast enough, and treat `ndv` as a fallback if it isn't.

## 5. Phased plan

**Phase 0 — Groundwork**
- [x] Stand up `nionswift-usim` and confirm the camera/scan device classes
  can be driven headlessly, outside Swift's own process/UI —
  [`scripts/phase0_usim_smoke_test.py`](../scripts/phase0_usim_smoke_test.py)
  (`uv run --extra device python scripts/phase0_usim_smoke_test.py`).
  Note: it talks to the `nion.device_kit` `Camera`/`Device` objects
  directly (`acquire_image()`, `get_scan_data()`), not the higher-level
  `HardwareSource`/`DocumentController` layer — that layer's own headless
  test harness (`AcquisitionTestContext`) turned out to depend on
  application-level registration (`WorkspaceManager.register_filter_panel`)
  that only happens inside a full `nion.swift.Application.initialize()`,
  so it isn't actually the lighter-weight path outside Swift's UI process
  it looks like. Also note: usim constructs *two* camera devices
  (Ronchigram + EELS) up front, each running its own acquisition thread
  from construction time — close both, or the process hangs on exit.
- [x] Stand up a bare napari window with PySide6 as a smoke test —
  [`scripts/phase0_viewer_smoke_test.py`](../scripts/phase0_viewer_smoke_test.py).
  **Requires a real or virtual display**; run it under
  `xvfb-run -a -s "-screen 0 1920x1080x24"`.
  Correction to the original Phase 0 note, which claimed this passed under
  `QT_QPA_PLATFORM=offscreen`: it did, but only because `libEGL` was
  missing from the container, so no GL canvas was ever attempted. Once
  the Qt GL libraries are actually installed, the offscreen platform
  provides no `QOpenGLWidget` and napari's layer teardown raises
  `KeyError` in `napari/_vispy/canvas.py::_remove_layer`. Offscreen is
  therefore not a valid headless substitute — it either crashes or passes
  while proving nothing about the GPU rendering path napari was chosen
  for. The script now refuses to run without a display rather than
  offering false confidence. Container prerequisites for a real canvas:
  `libegl1 libgl1 libxkbcommon0 libxkbcommon-x11-0 libdbus-1-3
  libfontconfig1 libxcb-cursor0` (plus the other `libxcb-*` plugin deps).
- [x] Settle on a package layout beyond the pyOpenSci template scaffold —
  package is named `miainwoodpecker`; the demo `add_numbers` module has
  been replaced by the Phase 1 device bridge.
- [x] Decide the license (see §6): the application stays MIT; the GPL-3.0
  device layer is isolated behind a subprocess boundary rather than
  imported in-process.

**Phase 1 — Device bridge**
- [x] Define a vendor-neutral `Camera`/`Scanner` interface and wrap Nion's
  device objects behind it — implemented in
  [`src/miainwoodpecker/devices/`](https://github.com/msarahan/miainwoodpecker/tree/main/src/miainwoodpecker/devices):
  `interface.py` holds runtime-checkable structural `Protocol`s plus the
  neutral data types (`Frame` = data + aware timestamp + metadata;
  `ScanParameters` in operator units — pixels, µs, nm). Device wrapping
  originally lived in one `nion_adapter.py` importing `nion.device_kit`
  directly (per the Phase 0 finding, *not* the
  `HardwareSource`/`AcquisitionTestContext` layer, which needs a full
  `Application`); §6 splits that into `nion_server.py` (the same wrapping
  logic, GPL-3.0, subprocess-only) plus `remote.py` (MIT, IPC client) once
  the license decision required it. The rest of the app depends only on
  the interface, so a second vendor's adapter can be added later without
  touching those layers; the base `devices` package imports with no
  vendor SDK installed. Design notes: structural protocols (not ABCs) so
  vendor adapters and test fakes satisfy the interface by shape; smallest
  interface that supports the Phase 2 viewer — exposure/settings modeling
  and synchronized multi-signal acquisition deferred to the phases that
  need them; the `(height, width)` scan convention is pinned empirically
  by a non-square scan in the integration tests.
- [x] Validate against `nionswift-usim` — directly, in-process, in
  [`tests/integration/test_nion_server.py`](../tests/integration/test_nion_server.py)
  (auto-skipped unless the `device` extra is installed; the
  `simulated_instrument()` context manager owns the both-cameras-closed
  teardown that the Phase 0 note warns about), and over the actual IPC
  boundary the application uses in
  [`tests/integration/test_remote_nion.py`](../tests/integration/test_remote_nion.py).
- [x] Pin the whole `device` extra exactly (`nionswift-usim`,
  `nionswift-instrumentation`, `nionswift`, `nionswift-io`, `niondata`,
  `nionui`, `nionutils`), not just `nionswift-usim` itself. CI's hatch
  environments live outside `uv.lock` entirely (their own venvs under
  `~/.local/share/hatch`, resolved independently by hatch's own uv
  installer), so a loose `>=` bound floats freely there. It bit twice in
  one session: two hatch env builds minutes apart resolved two different
  nion-stack releases, and `NDataHandler.write_data`/`write_properties`
  do not hold their positional-argument signature stable across
  releases — CI failed with *two different* "missing required
  positional argument" errors for the same call, first `file_datetime`
  then `data_descriptor`, neither reproducible against `uv.lock`'s
  pinned resolution. Introspecting the signature under the exact pins
  (`inspect.signature`, rather than guessing again from error text)
  confirmed the original 2-argument call was correct all along — the
  bug was resolution nondeterminism, not the call site.
- [x] Stage the device layer for hardware without having hardware, so that
  access to an instrument is a validation day rather than a development
  phase. `nion_server.serve()` no longer hard-constructs the simulator:
  `open_instrument(backend, plugin_names)` dispatches to either
  `simulated_instrument()` — byte-for-byte the same usim construction,
  including the both-cameras-must-be-closed teardown the Phase 0 note
  warns about — or a new `hardware_instrument()`, selected with
  `--backend {simulated,hardware}` plus a repeatable `--plugin MODULE`
  (each defaulting to `MIAINWOODPECKER_BACKEND` /
  `MIAINWOODPECKER_HARDWARE_PLUGINS`, command line winning).
  `remote_instrument(backend=...)` threads it through the MIT side;
  `remote_simulated_instrument()` keeps its exact signature as a two-line
  delegation, because the viewer, the session layer, and three benchmark
  scripts are written against it.
  - **The real path is Nion's own discovery mechanism, traced through the
    installed stack rather than guessed at.**
    `nion.swift.model.PlugInManager.load_plug_ins` iterates
    `pkgutil.iter_modules(nionswift_plugin.__path__)`, imports each
    submodule and calls its module-level `run()`; a device plug-in's
    `run()` is what registers `stem_controller`, `scan_module`, and
    `camera_module` with `nion.utils.Registry`. `hardware_instrument()`
    reproduces exactly that step — no plug-in directories, no manifests,
    no `Application` — then reads the registry back and takes the same
    `nion.device_kit`-level objects `NionCamera`/`NionScanner` already
    wrap, so Phase 0's finding about the `HardwareSource` layer still
    holds.
  - **More of that is testable now than expected, which is the useful
    finding.** `nionswift_plugin.usim.run()` works headlessly, so tests
    drive the whole discovery path — import → `run()` → registry read →
    device wrapping → camera classification → teardown via `stop()` — by
    pointing `--plugin` at the usim plug-in *as a stand-in vendor
    plug-in*. What waits for hardware is only *which* plug-in package a
    real instrument ships, plus the six assumptions below.
  - **The failure path is tested, because it is the one that can be.**
    With no vendor plug-in installed, `--backend=hardware` exits with
    status 2 (distinct from 1, so a launcher can tell "no microscope" from
    "crash") and one actionable line naming what it looked for, what it
    skipped, and how to override. Autodiscovery deliberately skips `usim`:
    letting `--backend=hardware` silently resolve to the simulator is the
    single failure mode a backend selector exists to prevent. A client-side
    fix fell out of testing that error over IPC — the server dies before
    binding a listener, so `_connect_with_retry` used to spin for its full
    15s deadline and then raise `ConnectionRefusedError`, discarding the
    only diagnostic that mattered; it now watches `process.poll()` and
    raises `DeviceServerStartupError` naming the exit status.
  - **Six assumptions are unverified without hardware, and are recorded as
    guesses rather than as working code** (each also flagged in a comment
    at its site): that a vendor plug-in registers from a module-level
    `run()`; that `camera_device.camera_type` labels a real camera
    `"ronchigram"`/`"eels"` (an unlabelled camera falls back to the
    Ronchigram slot, so a one-camera instrument still works); the three
    vendor control names (see Phase 3); which `GetVal2D` axis-keyword
    convention a vendor controller follows (the adapter tries the no-axis
    form and falls back on `TypeError`, but only the first branch is
    exercised); that a real controller publishes `stage_size_nm` (absence
    falls back to 1 µm, so a real instrument silently gets a wrong
    field-of-view *hint* until someone checks); and the autodiscovery skip
    list itself. `InstrumentDevices` now holds `eels_camera: … | None` and
    the client asks `instrument.describe()` before connecting to any device
    target, so it only opens connections for targets that exist — usim
    always has both cameras, a real instrument need not.
  - **A precedence bug in `--plugin`, found by a sibling change and fixed at
    the root.** The parser seeded argparse's `action="append"` *default* from
    `MIAINWOODPECKER_HARDWARE_PLUGINS`, and `append` adds to its default
    rather than replacing it — so `--plugin foo` with that variable set meant
    "the environment's plug-ins *and* foo", contradicting the command-line-wins
    precedence documented above. Now `default=None` with the environment read
    after parsing, and five tests pin the precedence rather than leaving it
    assumed. Worth recording because the first fix attempted was a workaround
    in `viewer/app.py` (clearing the variable for the child process), which
    would have left the documented behaviour still false at the layer that
    defines it.
- [ ] Validate against real hardware. Still open, but no longer
  open-ended: what remains is the enumerated substitution above, and
  `docs/hardware-validation-checklist.md` is the ordered procedure for it,
  written so each step's failure is diagnosable before the next one runs.

**Phase 2 — Live viewer MVP**
- [x] Decouple acquisition from display —
  [`src/miainwoodpecker/acquisition/live.py`](../src/miainwoodpecker/acquisition/live.py).
  `LiveAcquisition` runs a grab callable on a daemon worker thread and
  keeps only the newest frame (**latest-frame-wins**); the display polls
  `latest()` at its own rate. This is the pymmcore-plus pattern and the
  direct structural fix for what makes Swift slow: a slow display skips
  frames instead of queueing them, and no per-frame event fan-out ever
  reaches the UI thread. Deliberately UI-agnostic — it imports no Qt.
- [x] A napari + PySide6 dock widget with the live scan/camera feed and
  scan controls (channel, size, dwell, FOV) —
  [`src/miainwoodpecker/viewer/live.py`](../src/miainwoodpecker/viewer/live.py),
  launchable as `miainwoodpecker-viewer`. One `QTimer` on the GUI thread
  drives all display updates. Thread-safety contract: the GUI thread
  writes scan settings to a plain tuple that the worker only reads, so
  workers never touch Qt.
- [x] Benchmark live frame latency —
  [`scripts/phase2_live_benchmark.py`](../scripts/phase2_live_benchmark.py).
  **Measured on real GPU hardware (Apple M2 Pro), 512×512 scan:** acquire
  **5.4 ms** median (p95 6.1); display **11.1 ms** median (p95 15.3, max
  15.6). Tight spread, unlike software rendering's 42%.

  **Verdict: napari is comfortably fast enough for scanned imaging, and
  the ratio that says otherwise is measuring the wrong thing.** Display
  is 2.05× the *simulator's* acquire time — but the simulator produces a
  512×512 frame in 5.4 ms while a real scan at 1 µs dwell takes
  512² × 1 µs = **262 ms**. Against the beam time that actually gates a
  live view, display costs **4.2% of a frame**; at 10 µs dwell, 0.4%.
  This project's own script printed "display dominates … the empirical
  argument for ndv" off that 2.05×, which was a denominator error rather
  than a finding — the script now divides by the scan's physical duration
  and reports the sustainable rate separately.

  **Where the cost is real: cameras.** 11.1 ms per repaint is a hard
  ceiling of ~90 fps regardless of source. Fine for the commodity camera
  server (30–60 fps) and for survey-rate detectors; not fine above that,
  which is the regime where `ndv` would earn its place — and, separately,
  the regime this project already routes to LiberTEM-live.

  **The `ndv` question is now settled, by a size sweep on the same M2
  Pro.** Display cost against scan size, 1 µs dwell throughout:

  | scan | pixels | acquire | display |
  |---|---|---|---|
  | 512² | 0.26 M | 5.4 ms | **12.2 ms** |
  | 1024² | 1.05 M | 17.4 ms | **11.2 ms** |
  | 2048² | 4.19 M | 65.7 ms | **11.4 ms** |

  **Sixteen times the pixels costs nothing.** The 1 ms spread across
  sizes is smaller than the spread *within* any single run (p95 − median
  ≈ 3.3 ms), so display time is flat to the precision available. At 2048²
  a `float64` frame is 33.5 MB and it still repaints in the time 2 MB
  does, which rules out upload and draw as well: this is napari's
  CPU-side per-update overhead, exactly the cost `ndv` exists to remove.

  **So the diagnosis is right and the conclusion inverts.** A fixed cost
  is amortised precisely where it would otherwise hurt: every real
  workload's frame time scales with data while this does not. Acquire
  overtakes display between 512² and 1024², and against *beam* time it is
  not close at any size — 4.7% of a 512² frame at 1 µs dwell, 0.27% of a
  2048² one. There is no scanned-imaging regime in which changing viewer
  would be measurable.

  **Camera live views are the cheaper path, not a separate risk.** Scan
  and camera refresh differ deliberately in `viewer/live.py`: the scan
  view runs `autocontrast_every_frame=True`, which walks the whole array
  twice per frame, and the camera view does not. So the figures above —
  measured on the *scan* loop — are the pessimistic side, and a
  Ronchigram or spectrometer live view costs no more. That the two full
  walks are invisible in the size sweep is itself consistent: two passes
  over 33.5 MB is ~0.7 ms at this memory bandwidth, inside the run-to-run
  spread.

  **The axis that matters is not scan-versus-camera; it is
  display-for-a-human versus process-every-frame.** A live view exists
  for an operator's eye, which does not resolve past ~30 fps and calls
  anything under ~100 ms instant. Against that, 11 ms and a ~85 fps
  ceiling are comfortable for any live view worth showing: a Ronchigram
  at a 20 ms exposure is 32 fps end-to-end, a spectrometer at 10 ms is
  47 fps. Processing *every* frame from a fast detector is a different
  job with a different answer (LiberTEM-live, docs/vendor-support.md),
  and it is not a viewer question at all.

  **Verdict: keep napari.** Phase 2's open question is closed for both
  scanned and camera live views.

- [x] **Measured: display responsiveness while analysis runs — bound the
  analysis threads, not the viewer.** napari's per-update cost is
  CPU-side, and this measurement was demonstrably sensitive to contention
  (42% spread under software rendering against a few percent on an idle
  GPU), so whether a HyperSpy or LiberTEM job on the same machine
  degrades a live view was a real and unanswered question.
  `phase2_live_benchmark.py --load N` runs N CPU-saturating numpy workers
  during the display measurement — numpy, so they compete for cores the
  way analysis does rather than for the GIL — and `--source camera` times
  the camera path directly. Run on the M2 Pro, camera source, 512²:

  | load | acquire median | display median | display p95 | display max |
  |---|---|---|---|---|
  | 0 | 5.5 ms | **5.6 ms** | 7.6 ms | 516 ms |
  | 4 | 5.6 ms | **3.1 ms** | 23.0 ms | 649 ms |
  | 8 | 5.7 ms | **17.8 ms** | 23.7 ms | **4031 ms** |

  **Acquire does not move: 5.5 → 5.7 ms across zero to eight competing
  workers, a 3% spread.** The device server is a separate process and the
  client's side of a grab is IPC plus a shared-memory read, not
  computation, so contention lands on display alone. That is the design
  working, and it means the question really was a viewer question.

  **The tail degrades a full load level before the median does, and the
  median briefly gets *better*.** At four workers the median improves to
  3.1 ms — the background load holds the CPU in a high-performance state
  that an idle machine drops out of — while p95 triples, 7.6 → 23.0 ms.
  Reporting a frame rate off the median (the script prints 318 fps here)
  is therefore actively misleading at exactly the point where the user
  first notices something: a distribution going bimodal, not a mean going
  up. **p95 is the statistic that matters for a live view, and the
  benchmark's headline fps number is not it.**

  **At eight workers it stops being a statistic and becomes a
  four-second freeze.** The median follows the tail up (17.8 ms, ~56 fps)
  and the worst update is 4031 ms — 8× the idle run's worst, which is
  itself a first-paint outlier rather than a steady-state one. That is
  the GUI thread being descheduled outright, and no amount of
  per-update efficiency fixes it: `ndv` would reduce the 5.6 ms, not the
  4 seconds. **So this is not a second argument for changing viewer; it
  is a scheduling constraint on our own code.**

  **The actionable form, now implemented rather than recommended:
  analysis parallelism is capped below the core count.**
  [`src/miainwoodpecker/analysis/threads.py`](../src/miainwoodpecker/analysis/threads.py)
  holds the whole policy as one number — `os.cpu_count()` minus two,
  floored at one — and two places apply it. `AnalysisJob` runs every
  analysis inside `limit_analysis_threads()`, which caps the
  OpenBLAS/MKL/OpenMP pools with `threadpoolctl` and restores them
  afterwards; the LiberTEM button builds its `Context` from
  `analysis_context()`, which hands the same number to
  `InlineJobExecutor(inline_threads=...)`. The second is not redundant
  with the first: **"inline" bounds the executor, not the numerics under
  it** — an `InlineJobExecutor` with no `inline_threads` asks for
  `psutil.cpu_count(logical=False)` fine-grained threads and applies that
  to numba (which `threadpoolctl` cannot reach), pyfftw and the BLAS
  pools around every partition it processes. `viewer/jobs.py` already ran
  one analysis at a time on one worker thread, so our own fan-out never
  was the problem; the library threads inside that one job were. The
  target is the `--load 4` row — two cores left free — which that row
  says costs the live view nothing.

  **The floor is not a detail.** On one or two cores the subtraction is
  zero or negative, and both consumers read that badly: a BLAS pool takes
  zero as "decide for yourself" and goes back to every core, and LiberTEM
  passes the number to `numba.set_num_threads`, which refuses anything
  below one. So the smallest machines would have been the ones where the
  cap either did nothing or crashed. One thread means a slow analysis
  that still shares the machine.

  **`OMP_NUM_THREADS` and friends are deliberately not used**, and would
  not have worked: those are read when the native library loads — for
  numpy, at `import numpy` — so setting them from inside a running Qt
  application is a no-op that looks like a fix. `threadpoolctl` calls each
  library's own runtime setter instead, which is the same thing LiberTEM
  reaches for internally. The honest limitation is scope: those setters
  are process-global, so the cap is bounded in *time* (lifted when the job
  finishes) rather than confined to the worker thread. That costs nothing
  here, since the GUI thread's own work is Qt repaints and napari
  bookkeeping rather than BLAS calls, but it is a real difference from
  "the analysis thread is limited". A second known gap: `os.cpu_count()`
  reports the machine, not a container's CPU quota — `os.process_cpu_count()`
  is the correct call and needs Python 3.13, above this package's 3.11
  floor.

  **Camera live views measured at half the scan path, as predicted from
  the code.** 5.6 ms median here against the scan loop's 11–12 ms above,
  on the same machine — the `autocontrast_every_frame=True`/`False` split
  in `viewer/live.py` reasoned about in the previous item, now observed
  rather than inferred. Note the corollary for the item above: the scan
  figures quoted there are the pessimistic side of the pair, and the
  cheaper path is the one a Ronchigram or spectrometer view takes.

  Earlier figures on this container (llvmpipe software rasterization)
  were acquire ~14.5 ms, display 33.6–47.7 ms across four runs. Real GPU
  hardware is 2.7× faster on acquire and 3–4× on display, so the software
  numbers were the pessimistic floor the script claimed rather than a
  verdict.
  Two measurement traps found while building it, either of which makes
  napari look ~2× faster than it is, and both of which the script now
  avoids: (1) with `show=False` the canvas is hidden, Qt issues no paint
  events, and the GPU draw never happens; (2) assigning `layer.data` only
  *schedules* a repaint, so the event loop must be flushed **inside** the
  timed region. An honest hidden-vs-shown comparison went 5.3 ms → 22.7 ms
  for the same operation.
  - **The originally recorded figures were 12.7 ms acquire / 42.4 ms display
    / "3.35× acquire", and two later corrections matter more than the small
    numeric drift.** First, the +1.8 ms on acquire is explained rather than
    mysterious: the old figure was an in-process call, and the script now
    goes through `remote_simulated_instrument()` — the MIT client the shipped
    viewer actually uses — so it crosses the IPC boundary. A 512×512 float64
    frame is 2.1 MB, and §6 independently measured the shared-memory path's
    overhead at that size as +2.8 ms, which accounts for the difference
    within noise. (The script had also gone stale rather than wrong: it
    imported `devices.nion_adapter`, the module §6 split into
    `nion_server.py` + `remote.py`, so it simply did not run.)
  - **Second, and the reason the ratio should never have been quoted to
    three significant figures: display cost is load-dependent, not merely a
    pessimistic floor.** Across four runs acquire held to a 3% spread
    (14.2–14.7 ms) while display moved 42% (33.6–47.7 ms), falling
    monotonically as other work on the machine finished. That is the
    directly observed signature of what this item originally reasoned about a
    priori. So "2.3–3.3×" is the honest form; a single number invites being
    read as a property of napari rather than of a loaded software rasterizer.
  - **Third, and it qualifies the "display dominates" framing more than a GPU
    would: at 1024×1024 the ordering reverses on this same box** — acquire
    48.5 ms, display 30.8 ms, i.e. display is 0.63× acquire and the script
    prints that napari keeps up with the source. Display cost grows
    sublinearly with pixel count while usim's scan generation grows about
    linearly, so **512×512 is close to napari's worst case here rather than
    representative of it**, and at the larger scans a real experiment is
    likelier to use, the device is already the bottleneck. Anyone reading the
    old 3.35× as "napari is 3× too slow" should note it was size-specific.
    This strengthens rather than weakens the decision to withhold judgement
    until the GPU re-run.
- [x] Re-run the benchmark on GPU hardware. Done, on an M2 Pro (Metal-backed
  OpenGL): the condition set here for moving to `ndv` — display still
  dominating once hardware-accelerated — is not met at any scan size, and
  the one regime where the fixed cost would bite is already routed to
  LiberTEM-live. Keep napari — and the analysis threads are bounded, in
  `analysis/threads.py`, rather than left as advice.

**Phase 3 — Acquisition and storage**
- [x] Acquisition sequences —
  [`src/miainwoodpecker/acquisition/sequence.py`](../src/miainwoodpecker/acquisition/sequence.py).
  Plain lazy generators over the device protocols (`scan_series`,
  `camera_series`, `focal_series`) plus `record()`, which streams them to
  disk as they arrive rather than buffering. `camera_series` stops the
  camera in a `finally`, so abandoning a series early still releases the
  device. `focal_series` originally swept field of view because sweeping
  focus needed instrument controls the Phase 1 interface deliberately did
  not expose; those controls now exist (below), so
  `focal_series(..., instrument=...)` sweeps **real defocus**, while
  omitting `instrument` sweeps field of view exactly as before. It records
  both the `requested_defocus_nm` and the *read-back* `defocus_nm` per
  frame, so a recording says what the instrument did rather than what it
  was asked to do, and restores the original defocus in a `finally` even
  when the consumer abandons the generator early — the same discipline
  `camera_series` already applies to leaving a camera running.
- [x] Instrument controls, deliberately three and no more —
  `InstrumentController` in
  [`src/miainwoodpecker/devices/interface.py`](../src/miainwoodpecker/devices/interface.py):
  stage position, defocus, beam blanker, plus `stage_size_nm()`,
  `available_controls()` and `park()`. Holding to Phase 1's
  smallest-useful-interface discipline mattered more than usual here: a
  real Nion `STEMController` exposes *hundreds* of named controls (`C10`,
  `C12`, `CAperture`, `EHT`, `ZLPoffset`, …), and proxying them would be a
  vendor API wearing vendor-neutral clothing. Units are the operator's,
  matching `ScanParameters` — nanometres, never the vendor's metres — and
  positions are `(y, x)` in the same axis order the `(height, width)` scan
  convention already pins. The adapter drives them through the
  *named-control* API on `stem_controller.STEMController`'s own base class
  (`does_control_exist`/`get_control_output`/`set_control_output`, and
  `GetVal2D`/`SetVal2D` for the stage) rather than `device_kit`'s
  `defocus_m`/`stage_position_m` convenience properties, because the
  properties exist only on Nion's reference implementation while the
  named-control API is what any vendor controller must implement — and is
  how Nion's own higher layers drive controls (`AcquisitionPreferences`
  declares `ControlDescription("blanker", …, "C_Blank", …)`). The control
  *names* are therefore Nion's, taken from Nion's code, not invented here.
  - **Measured for effect on data, not merely for a successful setter**,
    with [`scripts/device_control_verification.py`](../scripts/device_control_verification.py)
    over the real IPC boundary, reporting the shot-noise floor (two frames
    at a *fixed* setting) against the change after moving the control.
    §7's `probe_position` finding is exactly the trap: usim has controls
    that accept a value, echo it back, and are then silently dropped.
    Results (256×256 scans, median of 3): defocus `C10` moves Ronchigram
    data **6.23× the noise floor**; stage position **5.78×** (and drives
    the scanner's frame mean 0.5551 → 0.0003); the blanker **962×**, taking
    the Ronchigram mean from 11840 counts to 0.004.
  - **Three honest negatives, confirmed in the simulator's source and not
    only by measurement**, so they are properties of usim rather than of
    this adapter: defocus does not affect scan data
    (`ScanDataGenerator.generate_scan_data` never reads `C10`), and
    blanking affects neither EELS data (`EELSCameraSimulator` lists
    `is_blanked` in `depends_on` but its `get_frame_data` never gates on
    it) nor scan data. So a defocus focal series over the *scanner*
    produces noise-identical frames in simulation: the control is genuinely
    driven and recorded, and the data response is what waits for hardware.
    `probe_position` is deliberately still **not** exposed — §7's finding
    stands, and adding a setter that silently does nothing is precisely the
    mistake this measurement exists to prevent.
- [x] NeXus/HDF5 storage —
  [`src/miainwoodpecker/storage/nexus.py`](../src/miainwoodpecker/storage/nexus.py).
  **Deliberately written with `h5py` alone, not `pynxtools-em`.** NeXus is
  a *convention over HDF5* (typed `NX_class` groups, `signal`/`axes`
  plotting hints, `units` everywhere); `pynxtools-em` is a
  vendor-format→NXem *reader/converter* and pulls ~70 packages (hyperspy,
  scikit-learn, sympy, xraydb…) to supply, for our purposes, a schema
  convention. Following a documented format is not reinventing one — this
  is precisely what avoids a bespoke project format. `NexusWriter` streams
  into a resizable per-frame-chunked, compressed dataset (gzip + byte
  shuffle by default — see the compression item below) so long acquisitions
  persist incrementally, and scan frames reporting `fov_nm` get real
  spatial axes in nanometres (cameras correctly fall back to `pixel`).
  **Independently validated**: files load in `nexusformat` (the NeXpy
  reference library), which resolves the class hierarchy, `nxsignal`,
  `nxaxes`, and reports `plottable_data` — so standard NeXus tooling can
  plot them without any of our code.
- [x] Legacy `.ndata` importer —
  [`src/miainwoodpecker/storage/legacy.py`](../src/miainwoodpecker/storage/legacy.py).
  Converts to `Frame` and recovers Swift's naive-UTC timestamps as aware.
  Tests write fixtures with Nion's *writer*, so the real container format is
  exercised, and cover the full migration path (old library directory →
  single NeXus file). Originally read via Nion's own `NDataHandler` rather
  than re-implementing its zip container; that import turned out to breach
  §6's license boundary and the reader is now standard-library only, which
  also means this module needs no optional extra — see the end of §6.
- [x] A session, so an operator can actually keep data —
  [`src/miainwoodpecker/storage/session.py`](../src/miainwoodpecker/storage/session.py),
  wired into the viewer and `app.py`. Phase 3 could already *write* NeXus
  files and stream a series into one, but nothing in the running
  application ever chose a filename: the viewer was live-display-only and
  the Phase 4 analysis buttons wrote their bursts into a
  `TemporaryDirectory` that was deleted on the way out. So a Phase 5 pilot
  was blocked for an entirely mundane reason — an operator could press
  nothing and keep their data.
  - **Deliberately small**: a directory, a naming rule, and three pieces of
    context (operator, sample, notes). No database, no catalogue, no
    index — the filesystem is the index and NeXus files are the records.
    `Session.record` is a thin wrapper over `sequence.record` rather than a
    second write path, so the streaming property carries straight over.
  - **Collisions are impossible rather than unlikely.** Names like
    `0001-scan-haadf-20260810T182524Z.nxs` are claimed by *creating* the
    file with `O_EXCL`, not by checking for absence and then writing, so
    two acquisitions in the same second — from two threads, or two
    processes pointed at one directory — cannot collide; the loser
    increments and retries. Tested with concurrent threads, not argued from
    the code.
  - **An existing session directory is reused, never cleared**, because an
    operator restarting mid-shift should land back in the same session
    rather than clobber the morning's data: numbering resumes from the
    highest index on disk, context loads from the previous run's
    `session.json`, and omitting a context field keeps the stored value
    instead of blanking it.
  - **Recording runs off the GUI thread.** A `RecordingJob` mirrors
    `LiveAcquisition`'s shape (daemon thread, state behind a lock, errors
    captured rather than raised) and lives in `session.py` rather than the
    viewer specifically so it *cannot* reach a Qt object. Phase 2's
    thread-safety contract therefore holds in both directions. "Save
    displayed frame" needs no device and leaves the live loop running;
    "Record frames" stops it first, because the device RPC is strictly
    synchronous over one connection per §6, so two threads calling
    `scan_frame` would corrupt the stream rather than merely contend.
  - **Verified through the real entry point**, the standard §6 set: driving
    `app.main()` under `xvfb-run` against the actual device subprocess
    recorded two real 2048×2048 float32 Ronchigram frames into a readable
    23.3MB file, with the click handler returning in 2ms against 5.5s of
    write time, and no `nion_server` process or `/dev/shm` segment
    surviving teardown.
- [x] **What an interrupted acquisition actually produces — measured, and
  the answer changed the writer.** The `NexusWriter` note above says writes
  persist incrementally; that is true of the *dataset* but does not mean an
  abandoned write leaves a readable file. Three interruption modes, writing
  three frames then stopping abnormally: an exception inside the `with`
  block still runs `close()`, so the file is short but **complete and
  valid** — and this is the common case, which is why "Stop recording" is
  cooperative cancellation rather than killing the writer. An abandoned
  writer that exits cleanly leaves all frames readable but **no `/entry/data`
  group, no `end_time`, no metadata** — which matters precisely because the
  Phase 4 adapters read `/entry/data`, so they fail on a file whose frames
  are demonstrably present. And a `SIGKILL` mid-acquisition leaves a file
  that **does not open at all** (`OSError: bad object header version
  number`), not a short-but-valid one, because HDF5 buffers object headers.
  A per-append `flush()` was measured to convert that third case entirely
  into the second, so `NexusWriter.flush()` is now public — and its cost
  across the whole codec sweep is **within run-to-run noise**, so bounding
  worst-case loss to a single frame is essentially free.
  - **Correction, found by the architecture review (§ below): making it
    public was not the same as using it.** For three phases *nothing in
    `src/` ever called it* — not `write_frames`, not `sequence.record`,
    not `Session.record`, not `RecordingJob` — so every real acquisition
    still had the unbounded worst case this item was written to
    eliminate, while the plan read as though it had been fixed. Only the
    benchmark and the tests flushed. `write_frames` now flushes after
    each frame by default (`flush_every=1`), which is what turns the
    measurement above into the guarantee it was always described as.
  SWMR would go
  further but is a genuine architectural conflict rather than an oversight:
  HDF5 forbids creating objects once SWMR is enabled, and `close()` creates
  the NXdata group *after* all appends because it needs the final frame
  shape — supporting it means restructuring the writer to create NXdata up
  front (§7).
- [x] Revisit compression — resolved, and **the winner cost nothing**. The
  default is now gzip level 4 *plus HDF5's byte-shuffle filter*; not
  blosc2, not bitshuffle, not float32. Measured with
  [`scripts/nexus_compression_benchmark.py`](../scripts/nexus_compression_benchmark.py)
  on real acquired frames, timed through the actual `NexusWriter` append
  loop.
  - **First, the recorded 1.08× reconciles exactly, so it needn't be
    re-litigated.** Dataset storage size gives a rock-steady **0.957×** at
    every scan size; the 1.08× was a whole-*file* measurement on a small
    recording, where ~36KB of fixed HDF5 structural overhead dominates (a
    single-frame 64² recording measures 1.874× whole-file against 0.960×
    dataset-only). Both numbers were right, and the original reading —
    gzip was doing essentially nothing on this data — was right too.
  - **Shuffle is a pure win on every axis and every dataset, so it is the
    default rather than an option.** A generic compressor sees an IEEE
    float array as an interleaved stream where every 4th or 8th byte is a
    near-constant exponent and the rest is noisy mantissa, so no useful
    match appears; shuffle transposes into byte planes, making the exponent
    plane long compressible runs and quarantining the noisy mantissa where
    it can only fail to compress rather than poison every match around it.
    Ratios improve 0.957→0.866 on float64 scan, 0.336→0.233 on EELS,
    0.694→0.532 on Ronchigram, **while Ronchigram write time roughly halves
    (747→374 ms/frame) and read-back drops 327→192 ms**, because deflate
    does less work on a stream that has structure. No trade-off to weigh.
  - **blosc2+zstd is better still and is deliberately not the default.** It
    edges the ratio (Ronchigram 0.527) and is dramatically faster (84
    ms/frame write, 82 ms read), but it is a *plugin* codec: a file written
    with it **cannot be opened at all** without `hdf5plugin` in the
    reading environment — and the readers that matter here are other
    people's (`nexusformat`, HyperSpy, LiberTEM, py4DSTEM, `pynxtools`).
    Trading "any HDF5 tool can read this" for a few percent of ratio is the
    wrong trade for a project whose entire storage argument is §3's. So it
    is opt-in behind a new `compression` extra — an interoperability
    argument, not a weight one. Note also that no plugin codec wins
    everywhere: `bitshuffle+lz4` is the fastest tested but collapses on
    EELS data (0.475 against gzip+shuffle's 0.233).
  - **float32 is a bigger win than any codec, and still not the writer's
    decision.** A 1024² scan recording goes 32.1MB → **13.8MB** as
    float32+shuffle, a **2.33×** reduction of which the dtype does 2.0× —
    downcasting matters ~10× more than codec choice. And the precision is
    not physically real: usim's `generate_scan_data` is annotated
    `-> NDArray[float32]` and builds float32 internally; the float64 arrives
    purely from its last line, where `numpy.random.randn()` (float64) is
    added to a float32 array and numpy promotes. A float32 round trip loses
    at most **1.55e-07 of the frame's own noise standard deviation**. But
    downcasting is lossy and irreversible, the writer is handed an array
    rather than a statement about its precision, and no real-hardware dtype
    is validated yet — a writer that quietly narrowed what it was given
    would assert provenance it cannot know. So `dtype=` is an explicit
    opt-in and the accidental promotion is a §7 follow-up to fix upstream.
- [x] Validate output against the official NXem NXDL schema with
  `pynxtools` — done, and **the answer was that our files did not
  validate.** `pynxtools` ships the NXDL definitions inside the wheel, so
  the schema is checkable entirely offline even though the NeXus/FAIRmat
  spec sites remain blocked from this environment.
  - **Exactly one required group was missing**: `/entry/sampleID`, needing
    `is_simulation`, `preparation_date`, and `atom_types`. Everything else
    the writer emits — every `NX_class`, the `signal`/`axes` hints, the
    `units` attributes, `definition`, `start_time` — passed without
    comment, and adding just that group flips the same file to valid.
  - **All three missing fields are facts about the physical specimen, so
    none is fabricable.** Inventing them to make a schema pass would be the
    real failure here: a file that lies about its provenance is worse than
    one that admits an incomplete declaration. **So `definition` now
    defaults to `None`** — the file declares no application definition
    rather than claiming `NXem` falsely. It remains a well-formed NeXus file
    conforming to the base classes (still independently loadable and
    plottable by `nexusformat`); it simply stops asserting conformance it
    cannot demonstrate. Passing `sample=` together with `definition="NXem"`
    produces a file that verifiably validates. Note there is no weaker
    application definition to fall back to — `NXentry` is a base class — so
    "claim something weaker but true" resolves to "claim nothing".
  - **The obvious version of this CI job would have passed silently
    forever**: `pynx validate` exits 0 even when it prints "is NOT valid".
    [`scripts/validate_nexus_schema.py`](../scripts/validate_nexus_schema.py)
    therefore uses the programmatic validation API, which returns a real
    boolean, and exits non-zero itself. It runs three checks, the third
    existing specifically so the job cannot pass vacuously: a default
    recording claims no definition; a fully-described recording *is* valid
    NXem; and a file claiming `NXem` *without* the sample group **is still
    reported invalid** — so if an upgrade ever made the validator stop
    finding problems, the first two would go quiet and this one fails
    loudly. Verified by two deliberate sabotages, each producing exit 1.
  - Session context now lands in real NeXus classes rather than inside a
    vendor-metadata JSON blob: `sample=` → `NXsample`, `user=` → `NXuser`,
    `notes=` → `NXnote`. Checking *which* of those the schema actually
    demanded was the finding — `sampleID` is `minOccurs="1"` and was the
    only failure, while `userID` and `noteID` are optional, so `user` and
    `notes` are there for honesty about where session context belongs
    rather than to satisfy the validator.
- [x] Per-axis calibration for camera frames, configurable per acquisition —
  [`src/miainwoodpecker/storage/calibration.py`](../src/miainwoodpecker/storage/calibration.py).
  Closes the gap §7 recorded: camera frames used to fall back to `"pixel"`
  units because nothing could say what their axes meant. The model is
  `AxisKind` × `AxisCalibration(kind, scale, offset, units)` ×
  `FrameCalibration(y, x)` — five kinds (real space `nm`, reciprocal space
  `1/nm`, energy `eV`/`meV`, angle `mrad`, and uncalibrated `pixel`) with a
  short frozen unit vocabulary and one exact factor per unit. No units
  dependency: this is a data model, not a units framework.
  - **Per *axis*, established by measurement rather than by assumption.**
    usim's `EELSCameraSimulator.get_dimensional_calibrations` returns
    `[Calibration(), Calibration(offset=…, scale=…, units="eV")]` — index 0
    (the 256-row slow axis) carries *nothing*, index 1 (the 1024-column fast
    axis) is dispersive at 0.5 eV/channel. That is why `dispersive_axis`
    defaults to `"x"`, and why it stays a parameter; the cross axis defaults
    to the pixel fallback instead of borrowing the energy unit, because it is
    not an energy axis.
  - **Per *acquisition*, not per detector**, because the axis kind is a
    property of the microscope's mode: the same camera yields reciprocal-space
    diffraction in one mode and something else in another. Supplied via
    `NexusWriter(calibration=…)` or through `metadata["calibration"]` — the
    route `fov_nm` already travels. Nothing new is required, and no existing
    signature or on-disk layout changed.
  - **NeXus has no axis-*kind* attribute, so none was invented.** `NXdata`'s
    `AXISNAME` carries `units` and `long_name`, and the unit string is the
    whole carrier. Measured through `pynxtools.units.NXUnitSet.matches`:
    **`"1/nm"` matches `NX_WAVENUMBER`; `"nm-1"` does not, and neither does
    `"A^-1"`** — so `1/nm` is canonical here and py4DSTEM's spelling is the
    adapter's problem, not the file's. `"pixel"` matches no category at all,
    which is exactly what makes it an honest admission rather than a claim.
  - **Angle is its own kind rather than folded into reciprocal space**, because
    `RonchigramCameraSimulator` reports `units="rad"` and converting angle to
    reciprocal space needs the electron wavelength — which would mean
    inventing a value. Cross-kind conversion is refused outright.
  - **The uncalibrated state is first-class**: an axis labelled `"pixel"`
    cannot carry a scale (enforced), a *missing* calibration falls back
    silently, and a *malformed* one raises on the first `append` rather than at
    `close()`, so the failure lands where the caller can still act on it.
  - **Carried into the adapters, each with a different real constraint.**
    HyperSpy fits natively and every kind round-trips, plus a new
    `load_as_hyperspy_spectrum` → `Signal1D` that sums along the
    non-dispersive direction and refuses when no single energy axis is named.
    py4DSTEM's `Calibration.Q_pixel_units` accepts only `"pixels"`, `"A^-1"`,
    or `"mrad"`, so **nm⁻¹ must be converted to Å⁻¹ (÷10)** — done explicitly
    and asserted at the *values* level, not just the label, because that is
    precisely the class of error that silently makes every downstream number
    wrong by exactly 10×; real-space, energy, mixed-kind, and anisotropic axes
    are refused with messages naming why. LiberTEM still has nowhere to put
    it: re-verified against 0.16 that `DataSetMeta` has no per-axis
    scale/offset/units and `H5DataSet` takes no metadata parameter, now
    recorded as a canary test rather than as prose that could quietly go stale.
- [x] Read a recording back, so the viewer is not write-only —
  `session.load_recording` / `LoadJob`, wired into a Recordings group in the
  widget. Files open from the session or an arbitrary path, multi-frame
  recordings go in as `(frames, h, w)` so napari's own frame slider does the
  navigation (no bespoke stack UI), and reads are bounded by a frame-data
  budget that always admits at least one frame and reports truncation. The
  three Phase 4 analysis buttons can now run against a file on disk instead of
  a fresh burst, in a defined precedence (opened file → session burst →
  temporary burst), leaving the original behaviour and its tests intact.
  Loading runs off the GUI thread on `RecordingJob`'s established shape:
  measured, the click handler returns in 1.7 ms against a 1.5 s read.
  - **The degraded cases were measured against a real `SIGKILL`ed writer, and
    one of them exposed a false error message.** An abandoned-writer file
    opens and displays *every* frame, because `read_series` reads
    `/entry/instrument/detector`, which survives — so the UI reports "frames,
    unfinalized — viewable, not analyzable". Analysis is pre-empted rather
    than passed through, because the adapters' own message ("has no
    `/entry/data` group; it recorded no frames") is *false* for such a file:
    the frames are demonstrably there. A hard-killed file cannot be opened at
    all, and its `OSError: bad object header version number` is wrapped into
    one sentence explaining HDF5's buffered object headers and that the frames
    are unrecoverable — no traceback reaches the UI.
  - Session notes are multi-line and per-recording notes exist, both reaching
    `NexusWriter`'s real `sample=`/`user=`/`notes=` parameters, so
    `NXsample`/`NXuser`/`NXnote` are genuine rather than the `session_`-prefixed
    stand-in the session work flagged as dishonest. Changing session directory
    is a button, refused while a recording is in flight — the job would in fact
    finish correctly into the old directory, but the UI would then describe a
    directory not receiving the file, and cancelling an acquisition to change a
    setting is worse.
- [x] Consider Zarr alongside HDF5 — **evaluated and declined**, on three
  independent grounds, the first decisive on its own. (1) **NeXus is an
  HDF5 convention, so Zarr would mean abandoning it**: `pynxtools` contains
  zero references to zarr, so the validation job just built cannot run
  against a Zarr store at all, and the alternatives are adopting HyperSpy's
  bespoke `.zspy` container or defining our own group layout — which is the
  bespoke project format this project exists to stop maintaining. (2) **The
  analysis libraries already wired in cannot read it**, checked directly
  rather than assumed: exactly one of RosettaSciIO's 38 plugins is
  Zarr-based and it is HyperSpy's own container; LiberTEM's 16 dataset
  types include none; `emdfile`/py4DSTEM none. A Zarr backend would mean a
  second read path in all three adapters, tripling the surface §3 exists to
  shrink. (3) **What Zarr buys addresses workloads this project doesn't
  have** — acquisition here is strictly single-writer by construction, and
  nobody has asked for object-store output. And there is no performance
  argument either: a matched-codec comparison gives ratios within 0.02
  everywhere with timings within noise. Nothing implemented, no dependency
  added.

**Phase 4 — Analysis integration**
- [x] Wire one analysis library in as a menu action operating on the new
  file format — [`src/miainwoodpecker/analysis/hyperspy_bridge.py`](../src/miainwoodpecker/analysis/hyperspy_bridge.py),
  wired into
  [`src/miainwoodpecker/viewer/live.py`](../src/miainwoodpecker/viewer/live.py).
  **HyperSpy chosen over py4DSTEM/LiberTEM for this first adapter**: both
  of the latter are commonly described as 4D-STEM (scan-position ×
  diffraction-pattern) tools, and the Phase 1 device interface
  deliberately has no synchronized scan-position/camera-frame
  acquisition mode yet (`interface.py`'s `Scanner` docstring calls this
  out directly) — so there is no 4D-STEM data for either to operate on
  today. What this app actually produces is plain frame stacks from
  `Scanner`/`Camera`, which is exactly HyperSpy's general case, and it is
  the lighter of the three: `pip install hyperspy` resolved **~35
  packages** (dask, matplotlib, scipy, sympy, rosettasciio, traits, …)
  versus the ~70 the Phase 3 notes measured for `pynxtools-em`. Not free,
  but not the same order of problem. (This reasoning holds for
  py4DSTEM specifically — its `DataCube` is a genuine 4D array — but not
  for LiberTEM; see below.)
  - **The adapter** (`load_as_hyperspy_signal`) reads a NexusWriter file's
    `/entry/data` group directly with `h5py` — the frame stack, the `x`/`y`
    axis datasets and their `units` attributes, and `frame_time` — and
    hands the arrays to `hyperspy.signals.Signal2D`, setting
    `axes_manager` scale/offset/units on the navigation (frame) axis and
    the two signal axes from what NexusWriter already wrote. No axis math
    is reimplemented; HyperSpy's own `AxesManager` does the bookkeeping.
    A scan recording's nanometre calibration (§3, Phase 3) survives the
    round trip; an uncalibrated recording's honest `"pixel"` units survive
    too, rather than inventing a spurious scale.
  - **The wired-in action**: a new "Analyze in HyperSpy" button in the
    live viewer's Camera group. Clicking it stops the camera's live loop
    if running (the `Camera` protocol implies one driver at a time — see
    §2), grabs a 5-frame burst via `acquisition.sequence.camera_series`,
    writes it to a temporary NeXus file with `storage.nexus.write_frames`,
    reads it back through the adapter, runs one real HyperSpy operation —
    `Signal2D.mean()` across the frame axis, a temporal-average projection
    — and pushes the result into napari as a new image layer. This is a
    genuine round trip end to end: acquire → NeXus file on disk → HyperSpy
    signal → a HyperSpy method → napari layer, not a mocked-up shortcut.
    Verified with a real napari widget against a fake camera under a
    virtual display
    ([`tests/integration/test_live_widget.py`](../tests/integration/test_live_widget.py)),
    not yet against real hardware or the actual simulated Ronchigram
    camera end-to-end through the running app (that path is exercised
    manually, not by an automated test, since it needs the `device`,
    `viewer`, and `analysis` extras plus a display all at once).
  - **Kept deliberately thin**: the `hyperspy` import lives inside the
    button's click handler, not at module scope, so the viewer (which only
    needs the `viewer` extra) still imports and runs without the heavier
    `analysis` extra installed; a missing extra reports "install the
    'analysis' extra" in the status label instead of an import crash. Only
    the camera stream is wired up (not the scan stream too) — one
    demonstrated operation, as scoped, not a general analysis UI; the same
    adapter and pattern would extend to scan frames with no changes to
    `hyperspy_bridge.py` itself.
  - **Real caveat**: the Ronchigram camera's frames still fall back to
    `"pixel"` units in `nexus.py` (cameras don't report a field of view the
    way scans do), so a signal built from real camera data carries no
    physically meaningful diffraction-angle calibration yet — that needs
    per-camera calibration data the device interface doesn't expose today.
    The axis-calibration round trip is verified for the case NexusWriter
    already calibrates (scan `fov_nm`), not invented for the case it
    doesn't.
- [x] Wire a second analysis library in as its own menu action —
  [`src/miainwoodpecker/analysis/libertem_bridge.py`](../src/miainwoodpecker/analysis/libertem_bridge.py),
  wired into
  [`src/miainwoodpecker/viewer/live.py`](../src/miainwoodpecker/viewer/live.py).
  **The assumption above about LiberTEM turned out to be wrong, and
  checking that (not assuming it generalized the same way py4DSTEM's
  does) is the actual finding here.** LiberTEM's core abstraction is not
  a fixed-rank 4D datacube; it's a `DataSet` with an arbitrary-shape
  "navigation" axis processed by user-defined functions (UDFs), and its
  HDF5 `DataSet` reader infers that shape directly from the array it is
  pointed at. Verified directly, not assumed from the docs: pointing
  `libertem.io.dataset.hdf5.H5DataSet` at a real file written by this
  app's own `storage.nexus.write_frames` (shape `(n_frames, height,
  width)` — the same plain frame stack `camera_series`/`scan_series`
  already produce, no synthetic data) gives `dataset.shape.nav ==
  (n_frames,)`, a genuinely **one-dimensional** navigation shape, not a
  padded/reshaped 2-tuple, and `Context.run_udf` runs real built-in UDFs
  (`SumUDF`, `StdDevUDF`) against it without complaint
  ([`tests/integration/test_libertem_bridge.py`](../tests/integration/test_libertem_bridge.py)).
  So a genuine, non-synthetic LiberTEM PoC on today's data model is
  possible — the Phase 4 note above correctly ruled out py4DSTEM (whose
  `DataCube` really is a fixed 4D array) but over-generalized that
  reasoning to LiberTEM without checking it separately.
  - **The adapter** (`load_as_libertem_dataset`) is thinner than the
    HyperSpy one: `Context.load("hdf5", path=..., ds_path=...)` already
    reads the array with `h5py` internally, so this function's only real
    job is validating the file has frames (mirroring the HyperSpy
    adapter's own check, and giving a clearer error than LiberTEM's own
    "unable to infer dataset" message) and naming the dataset path this
    app's writer actually uses (`/entry/data/data`).
  - **A real, honest limitation, not carried over from HyperSpy**:
    LiberTEM's `DataSetMeta` has no per-axis scale/offset/units fields —
    nothing like HyperSpy's `AxesManager`. There is no native LiberTEM
    object to hand NexusWriter's `x`/`y`/`frame_time` calibration to, so
    unlike the HyperSpy adapter, this one does not attempt an
    axis-calibration round trip. This is a genuine difference between
    the two libraries' object models, not a gap in this adapter.
  - **The wired-in action**: a new "Sum in LiberTEM" button alongside
    "Analyze in HyperSpy" in the live viewer's Camera group, following
    the identical pattern — stop the camera loop if running, grab a
    5-frame burst via `camera_series`, write it to a temporary NeXus file,
    read it back through the adapter, run one real LiberTEM UDF
    (`libertem.udf.sum.SumUDF`, summing across the frame axis) with an
    `inline` executor `Context`, and push the sum-projection image into
    napari. `inline` rather than LiberTEM's default `dask` executor is a
    deliberate choice: this is one UDF run over one small, already-in-memory
    burst per click, not the large-out-of-core-dataset workload the
    default executor's local cluster exists for — spinning one up per
    click would be pure overhead. Genuine round trip end to end,
    verified the same way as the HyperSpy button: a real napari widget
    against a fake camera under a virtual display
    ([`tests/integration/test_live_widget.py`](../tests/integration/test_live_widget.py)).
  - **Dependency weight, measured the same way as the HyperSpy
    comparison above**: `pip install libertem` alone resolves **~102
    packages** against a 2-package bare-venv baseline — dask,
    distributed, numba, scikit-learn, scikit-image, matplotlib, and a
    Jupyter/ipywidgets stack for LiberTEM's own notebook GUI — roughly
    **3× HyperSpy's ~35**. That weight buys tiled, MapReduce-style
    processing for pixelated-detector datasets much larger than memory;
    this PoC's burst is a handful of small in-memory frames, so the
    adapter exercises LiberTEM's `DataSet`/UDF model genuinely but not
    the scale of problem most projects reach for LiberTEM to solve.
    Given that, and unlike HyperSpy, **LiberTEM gets its own `libertem`
    optional-dependency group rather than joining `analysis`** — a
    consumer who only wants the general HyperSpy path shouldn't have to
    pull in dask/distributed/numba to get it.
  - **Also investigated, with a clean negative result specific to this
    environment**: whether a real, published 4D-STEM dataset (genuine 2D
    navigation, the stronger demonstration) could be substituted for the
    1D-navigation frame stack above. LiberTEM's own documented sample
    datasets (its `sample_datasets.rst` docs page) are hosted on Zenodo
    at 177MB–14.2GB (`10.5281/zenodo.*` DOIs; the smallest, a 177MB 4D
    STO dataset, is in MIB format, which needs its own reader, not the
    HDF5 one this adapter uses); py4DSTEM's small-sample registry hosts
    its files on Google
    Drive. Both hosts, plus HuggingFace, OSF, and Figshare tried as
    alternatives, returned a blocked `CONNECT` (HTTP 403) through this
    environment's outbound proxy — its allowlist covers package
    registries (PyPI, npm, crates.io, the Go proxy) and GitHub, not
    general data hosting. py4DSTEM's smallest nominal sample
    (`small_datacube`, meant to be ~4.2MB) also turned out to be an
    unreliable candidate on its own terms even ignoring reachability:
    its own source has a `TODO` noting the ID currently resolves to the
    same file as an unrelated fixture (`vac_probe`), a replacement that
    was never made. This is a network-reachability finding about *this
    environment*, not a claim that no such dataset exists or that
    LiberTEM needs one to be useful here — the 1D-navigation PoC above
    is real, working, and sufficient to answer the question this item
    was scoped to answer.

    **Superseded, in part, by a later egress change.** `zenodo.org` is
    now reachable from this environment and a real 4D-STEM datacube has
    been fetched and analysed — a (254, 255, 384, 384) `uint8` cube,
    genuine 2D navigation, from Zenodo 8233585. See
    [analysis isolation](analysis-isolation.md). `drive.google.com`
    remains blocked, so py4DSTEM's own downloader still cannot run here
    and both `gdown` findings above stand unchanged. What this
    supersedes is only the "no real dataset is reachable" half; the
    reasoning about LiberTEM's optional-dependency group is unaffected.
- [x] Follow-up PoC: py4DSTEM specifically —
  [`src/miainwoodpecker/analysis/py4dstem_bridge.py`](../src/miainwoodpecker/analysis/py4dstem_bridge.py),
  wired into
  [`src/miainwoodpecker/viewer/live.py`](../src/miainwoodpecker/viewer/live.py).
  **Investigated, rather than assumed, whether the 4D-STEM constraint
  above had moved** — it hasn't, and the reason is more specific than
  "the interface doesn't expose it yet." Checked whether the simulated
  device stack itself exposes any way to move the beam to a single point
  (option (a) in this investigation's brief), reaching past the
  vendor-neutral `Scanner`/`Camera` protocols into `nion.usim_device`'s
  own internals: `nion.device_kit.InstrumentDevice.Instrument` does have
  a real, public, settable per-point `probe_position`, and the
  Ronchigram camera simulator's own code
  (`RonchigramCameraSimulator.get_frame_data`) does read it to offset the
  simulated aberrations. But the read only happens through
  `CameraSimulator._get_frame_settings`, which first asks
  `self.instrument.scan_controller` for a registered
  `ScanHardwareSource` and silently drops `probe_position` back to a
  fixed centred default if that resolves to `None` — and it does resolve
  to `None` here, because that registration only happens inside the full
  `HardwareSource`/`Application` layer, the exact layer this migration
  plan's own Phase 0 note already found too heavy to stand up outside
  Swift's own process. Measured directly against
  `nion.usim_device.DeviceConfiguration.AcquisitionContextConfiguration`
  (the same lightweight construction `nion_server.py` uses, with no
  `HardwareSource` registered anywhere): setting `instrument.probe_position`
  to different points and re-acquiring a 2048×2048 Ronchigram frame each
  time changes nothing beyond shot noise — mean absolute difference
  between frames at *different* probe positions was 12.31 counts,
  statistically identical to the 12.31-count noise floor from
  re-acquiring at the *same* fixed position twice, and the disk's
  brightest pixel jumped to a different, effectively random location call
  to call even with the probe held perfectly still. So a genuine software
  step-scan 4D-STEM acquisition is not buildable today, even by going
  around this project's own device wrapper entirely — not just because
  the vendor-neutral interface hasn't grown the method, but because the
  simulator underneath it won't honor a per-point beam position without
  the heavier application layer this project deliberately avoids.
  A second possible way around it - option (a)'s escape hatch, real
  external 4D-STEM data instead of driving the simulator - was checked
  and is also unavailable in this environment: py4DSTEM ships a
  Google-Drive-backed downloader with real, non-synthetic sample
  datacubes (`small_datacube`, `Au_sim`, `Si_SiGe_exp`, …), but the
  outbound proxy returns a `403` on the CONNECT tunnel to
  `drive.google.com` for both py4DSTEM's own downloader and a bare
  `gdown` call to the same file id - confirmed with `curl` and directly
  with `gdown.download()`, not inferred from py4DSTEM's own wrapper
  alone. That wrapper is also, independently, broken against the `gdown`
  release it resolves today (`py4DSTEM 0.14.18` passes a `fuzzy=` keyword
  `gdown 6.1.0`'s `download()` no longer accepts) - a second, unrelated
  reason this path doesn't work here, worth recording so it isn't
  mis-attributed to the network block alone if retried later on an
  unblocked network with an older `gdown` pin.
  Landed on option (b): real single Ronchigram frames (genuine
  acquisitions, shot noise and all - not scan-position-indexed, and not
  presented as if they were) through py4DSTEM's own single-diffraction-
  pattern operations, which is exactly what py4DSTEM itself applies
  per-pattern inside a full datacube.
  - **The adapter** (`load_as_diffraction_slice`) reads a NexusWriter
    file's `/entry/data` group with `h5py` — the same pattern
    `hyperspy_bridge.py` uses, not a second reader implementation — and
    hands the frame(s) to `py4DSTEM.data.DiffractionSlice`, py4DSTEM's own
    diffraction-space container, calibrated on its `Calibration` object's
    `Q_pixel_size`/`Q_pixel_units` from exactly the axis values
    `nexus.py` already wrote. One real impedance mismatch surfaced and is
    handled explicitly rather than silently: `Calibration.Q_pixel_units`
    only accepts the literal strings `"pixels"`, `"A^-1"`, or `"mrad"` (a
    hard assert in py4DSTEM's own code), so the adapter maps NexusWriter's
    `"pixel"` (singular) onto `"pixels"` and raises a clear `ValueError`
    for anything else — in particular, a *scan* recording's nanometre
    calibration (real-space, Phase 3) is correctly refused rather than
    mislabelled as a diffraction-plane pixel count, since this adapter is
    for camera data specifically.
  - **The wired-in action**: a new "Fit central disk (py4DSTEM)" button in
    the live viewer's Camera group, alongside "Analyze in HyperSpy".
    Clicking it stops the camera's live loop if running, acquires **one**
    real frame via `acquisition.sequence.camera_series` (not a burst —
    a single-pattern operation needs one representative pattern, not an
    average), writes it to a temporary NeXus file, reads it back through
    the adapter, runs one real py4DSTEM operation —
    `py4DSTEM.process.calibration.get_probe_size`, the central-disk
    radius/centre fit py4DSTEM runs per-pattern internally even when it
    does have a full datacube — and pushes both the analyzed frame and a
    napari `Shapes` ellipse at the fitted disk into the viewer. Genuine
    round trip end to end: acquire → NeXus file on disk → py4DSTEM
    `DiffractionSlice` → a real py4DSTEM function → two napari layers.
    Verified with a real napari widget against a fake camera under a
    virtual display
    ([`tests/integration/test_live_widget.py`](../tests/integration/test_live_widget.py)),
    same caveat as the HyperSpy action about real-hardware/full-app
    end-to-end coverage.
  - **Kept deliberately thin and separately gated**: the `py4dstem` import
    lives inside the button's click handler, not at module scope, exactly
    like the HyperSpy button; a missing extra reports "install the
    'py4dstem' extra" instead of an import crash. `py4dstem` is its own
    optional-dependency extra, not folded into `analysis`: a fresh `pip
    install py4dstem` resolved **65 packages** (dask, distributed,
    scikit-image, scikit-learn, scikit-optimize, pylops, mpire, gdown, …)
    — heavier than HyperSpy's ~35 and close to the ~70 the Phase 3 notes
    measured for `pynxtools-em` — so installing one analysis library
    doesn't tax someone who only wanted the other.
- [x] Port Swift-specific analyses not already covered upstream, as small
  adapter functions. **Audited, and the audit changed the shape of the
  answer** — [`docs/analysis-parity.md`](analysis-parity.md) enumerates
  roughly ninety operator-reachable operations across `nionswift`,
  `nionswift-eels-analysis`, `nionswift-experimental` and the
  instrumentation kit, maps each onto HyperSpy/LiberTEM/py4DSTEM (or
  admits it doesn't map), and costs what's left. Three findings are worth
  repeating here because they move Phase 4's premise:
  - **"Port as small adapter functions" is the wrong verb for the core
    menu.** All 56 Processing-menu operations are thin expressions over
    one `nion.data.xdata_1_0` call, and `niondata` is **Apache-2.0**, not
    GPL-3.0 — checked in three places, including the installed metadata
    of the 15.9.1 this project already pins in the `device` extra. It
    installs into a bare venv in four packages (against HyperSpy's ~35,
    py4DSTEM's 65, LiberTEM's ~102) and runs standalone on plain NumPy
    arrays with no Swift and no GUI, verified rather than assumed. So the
    core menu is a dependency declaration on the MIT side plus a
    calibration conversion, not fifty reimplementations.
  - **This project has no EELS capability at all today, and that was
    invisible.** HyperSpy 2.x does not contain EELS — verified by
    introspecting the installed 2.4.0, whose `hs.signals` has no
    `EELSSpectrum` — because EELS and EDS moved to `exspy` at the 2.0
    split. The `analysis` extra is `hyperspy>=2.0`, so it covers none of
    Swift's EELS menu. That is the largest real gap the audit found, and
    it is ours rather than Swift's.
  - **Only five gaps are genuinely Swift-specific and worth porting**, at
    9–15 days total: thermometry (2–3 d), Fourier-filter mask shapes
    (2–4 d), Double Gaussian (1 d), radial power spectrum (1–2 d), and
    a two-area EELS background (1–2 d, on request only). Everything else
    is either covered, subsumed by a better upstream implementation
    (Swift's quantification is K-shell hydrogenic only, against eXSpy's
    tabulated DFT/Dirac databases), display-only, or acquisition-time
    work belonging to the synchronized-acquisition item rather than here.
  - The audit also surfaces, without resolving, that `hyperspy` and
    `py4dstem` are themselves GPL-3.0 and are imported in-process by
    `viewer/live.py` — the shape §6 avoided for the device layer. §6
    should say explicitly whether its boundary covers analysis extras.

**Phase 5 — Parity and cutover**
- Audit which Swift features the team actually uses day to day (not the
  full feature surface) and build a parity checklist from that. Recording
  data (Phase 3's session item) is a *precondition* for this audit, not a
  substitute for it.
- Pilot the new app in parallel with Swift on one instrument before cutover.
- What a pilot still needs beyond the session work, in rough order of how
  quickly each would be missed on a real instrument:
  - **Real hardware validation of the whole path** — everything is measured
    against usim, and how long a real detector's write actually takes is
    what decides whether the per-frame `flush()` is a nicety or a
    requirement.
  - ~~**Multi-line and per-recording notes**~~ — **done**: session notes are
    a `QPlainTextEdit` rather than a single line, and `Session.record()`
    takes a per-recording `note=` that lands in the file's `NXnote` labelled
    with its scope. What is *not* done is annotating a recording after the
    fact, tracked below.
  - ~~**No way to change session directory from the UI**~~ — **done**:
    `change_session_directory()` picks a directory and routes through
    `set_session()`, so a switched-to directory behaves exactly like one
    named at launch — reused if it exists, numbering resumed, context read
    from its own `session.json`, and no context carried across. A recording
    in flight blocks the switch rather than being silently redirected.
  - ~~**Nothing reads a session back**~~ — **done** (§5 Phase 3): recordings
    open from the session or an arbitrary path, and the analysis buttons run
    against a file on disk. Both gaps this left behind are now closed too:
    - ~~*A recording cannot be annotated after the fact*~~ — `annotate()`
      appends into the file's real `NXnote`, so an after-the-fact note lands
      in the same place as one written at acquisition time and nothing
      downstream needs to know the difference. Appended and labelled with
      when it was added, never overwriting the acquisition note, for the
      same reason the session and recording scopes are labelled: a reader
      has to be able to tell an observation made during the shift from one
      added a week later. The button acts on the *opened* recording rather
      than the combo selection, which removes the way to annotate the wrong
      file by leaving the combo elsewhere.
    - ~~*No cross-session enumeration*~~ — `find_recordings()` walks the
      session directories under a base and the Recordings combo can list
      that scope instead of just this session, so "find that scan from last
      Tuesday" no longer means leaving the app. Entries are then qualified
      by directory, because per-session numbering restarts at `0001` and the
      filename alone is ambiguous across sessions. Deliberately a directory
      walk and not an index: the filesystem is already the index, and a
      catalogue would be a second source of truth to keep in sync with a
      directory an operator also moves and renames files in by hand.
  - ~~**Disk-space and long-run behaviour is unexamined**~~ — **done** for
    the reporting half. `free_space()` and `estimate_size()` back a Disk row
    in the Session group that shows what is free and warns when the planned
    frame count would not fit. The estimate is the *uncompressed* size on
    purpose: erring high means warning slightly early, which is the right
    direction to be wrong about running out of disk mid-acquisition, and the
    real ratio depends on data that does not exist yet. What is still not
    done is tracking cumulative usage across a shift, or doing anything
    about it beyond saying so.
  - ~~**A large file is read twice on the analyze-from-disk path**~~ —
    **done**. It was read once by the load job for display and once by the
    adapter, because the adapters took a path rather than an array;
    harmless at pilot scale, 16.8MB per frame paid twice at 2048×2048.
    Each adapter now has an in-memory entry point beside its file-reading
    one — `hyperspy_signal_from_frames`, `hyperspy_spectrum_from_frames`,
    `libertem_dataset_from_frames`, `diffraction_slice_from_frames`,
    targeting exactly the `Signal2D`/`MemoryDataSet`/`DiffractionSlice`
    constructors this note predicted — and the viewer hands over the
    frames it already read. The path-taking forms are unchanged and are
    now one call to their in-memory half, so there is one implementation
    and the documented scripting API did not move.
    - **Separate names, not a union-typed parameter.** Whether a call
      decompresses a large recording is the thing the caller is choosing;
      an `isinstance` check at the bottom of the stack would hide it,
      while a name states it at the call site.
    - **The calibration is what made this an adapter API change rather
      than wiring**, exactly as this item said. Frames handed over without
      their axes produce a signal silently claiming bare pixels — worse
      than the duplicated read, because nothing downstream says so. So the
      carrier is `FrameStack`: the `(data, frame_time, calibration)`
      triple `read_frames` already returned, made a named tuple so every
      existing unpacking still works, rather than a new type. `LoadJob`'s
      `LoadedRecording` now carries the file's calibration too, and its
      `frames` property declines to offer them when they are not the whole
      recording — a truncated read, or an unfinalized file that never
      wrote its axes — so a saved read can never quietly become a
      different answer.
    - **LiberTEM's constraints were measured, not assumed.**
      `ctx.load("memory", data=stack, sig_dims=2)` infers the same
      navigation `(n_frames,)` and signal `(height, width)` shapes its
      HDF5 reader infers from the same file. `sig_dims` is explicit
      because the same call on a *2D* array builds a dataset with no
      frames to navigate instead of raising, so a flat single frame is
      refused here with a sentence. `MemoryDataSet`'s own "not recommended
      with a distributed executor" is a reason to keep the file-reading
      form for that case, not to avoid this one — the viewer's executor is
      inline, where there is no worker to ship an array to.
    - **The fresh-burst path still reads the file it just wrote**, on
      purpose: a burst's calibration is only resolved when `NexusWriter`
      writes it, so short-circuiting that read would mean a second
      implementation of the rule deciding what a recording's axes are, to
      save one read of a file this app created moments earlier.
    - Asserted by counting reads (`tests/integration/test_live_widget.py`
      instruments both frame readers), because the result is identical
      either way — nothing about the *answer* can show which happened.
  - ~~**The analysis buttons still block the GUI thread**~~ — **done**. All
    three now hand off to
    [`AnalysisJob`](../src/miainwoodpecker/viewer/jobs.py), which has
    `LoadJob`'s exact shape: daemon thread, state behind a lock, exceptions
    captured rather than raised, no Qt, polled from the same display timer
    that already collects the recording and load jobs.
    - The split is what makes it safe, and it is the caller's job rather
      than the job class's: `_start_analysis` resolves the Recordings
      checkbox and the note field *before* the thread starts, and defers
      every layer and label update to `_poll_analysis`. `_analysis_input`
      correspondingly takes `existing`/`note` as arguments instead of
      reading the widgets itself, and no longer refreshes the session
      labels from inside the worker.
    - Each button supplies a `compute` (runs on the worker, must not touch
      Qt) and a `display` (runs on the GUI thread, draws and returns the
      status text). That is the whole difference between them; everything
      else — stopping the live camera, acquiring or opening, error
      reporting, refusing a second concurrent run — is now shared.
    - Covered by a race-free test rather than a timing one: layers and
      status text are only ever touched by the poll path, so asserting
      straight after the click that the layer is absent and the label reads
      "working..." holds regardless of how fast the worker finishes, and
      fails every time for a handler that works inline.

## 6. License — resolved: process-boundary isolation

Nion's device-layer packages are GPL-3.0. A Python `import` of a GPL-3.0
library into the same process is generally treated as linking under the
FSF's own interpretation of the GPL (the criterion is forming a combined
work — shared process/address space, calling into each other's internals —
not compile-time vs. runtime binding, which is a common but incorrect
intuition for why this wouldn't apply to an interpreted language). Two
separate programs communicating over a well-defined protocol, rather than
one importing the other's internals, is the boundary that reading doesn't
reach across — the standard answer for exactly this shape of problem (it's
why tools have long shelled out to GPL command-line programs rather than
linking their libraries directly).

**Decision: isolate.** The device layer runs as a separate subprocess; the
main MIT-licensed application never imports `nion.*`.

- [`src/miainwoodpecker/devices/rpc.py`](../src/miainwoodpecker/devices/rpc.py) —
  the entire license boundary. A minimal `Call`/`Result` wire protocol
  (not a general RPC framework — one call shape, dispatch by
  `getattr` + `callable()` check, since properties like `camera_id` are
  evaluated, not invoked). MIT, imports nothing from either side.
- [`src/miainwoodpecker/devices/nion_server.py`](../src/miainwoodpecker/devices/nion_server.py) —
  GPL-3.0 (states so in its own header), imports `nion.*` directly. Holds
  the `NionCamera`/`NionScanner`/`simulated_instrument()` logic unchanged
  from the old in-process adapter, plus a serving loop: one
  `multiprocessing.connection.Listener` per target
  (`ronchigram_camera`/`eels_camera`/`scanner`/`instrument`), one handler
  thread per accepted connection. Runs only via
  `python -m miainwoodpecker.devices.nion_server`; never imported by the
  application.
- [`src/miainwoodpecker/devices/remote.py`](../src/miainwoodpecker/devices/remote.py) —
  MIT, no `nion.*` import. `RemoteCamera`/`RemoteScanner` implement the
  same `Camera`/`Scanner` protocols by sending `Call`s over IPC.
  `remote_simulated_instrument()` spawns the server subprocess and connects
  with a generated authkey. It originally tore down with `Popen.terminate()`
  (SIGTERM) rather than a graceful RPC shutdown — sufficient for a
  simulator, because the whole *process* being killed reclaims its threads
  and sockets regardless of Python-level cleanup — while noting that a
  real-hardware backend would need a gentler path to park the instrument
  safely. **That path is now built, and SIGTERM is demoted to a fallback**
  (see "Graceful shutdown" below).
- [`src/miainwoodpecker/viewer/app.py`](../src/miainwoodpecker/viewer/app.py)
  imports `remote`, not `nion_server` — the actual, shipped application
  never links GPL-3.0 code into its own process. Verified, not assumed:
  launching the real `main()` entry point end-to-end (napari window +
  remote subprocess) and confirming no `nion_server` process survives
  after clean shutdown.

**Raised concern, addressed with data, not assumption**: STEM frames are
large (this project's own simulated Ronchigram camera is already
2048×2048 float32, ~16.8MB), and naive pickle-over-socket serializes and
copies the array twice per round trip. Measured with
[`scripts/ipc_overhead_benchmark.py`](../scripts/ipc_overhead_benchmark.py)
(direct in-process call vs. the same call over IPC, scan frames so the
camera's 1-second simulated exposure doesn't mask the transport cost):
overhead was negligible at 0.1MB (+0.7ms) but grew to +74% at 2.1MB
(+7.4ms) and more than doubled total latency at 33.6MB (+168ms on a
163ms baseline) — squarely the size range real detector data lives in.

First fix (superseded below): [`src/miainwoodpecker/devices/shared_frame.py`](../src/miainwoodpecker/devices/shared_frame.py)
copied a `Frame`'s array into a fresh `multiprocessing.shared_memory`
segment per frame instead of pickling it — but creating and destroying a
named POSIX segment is its own syscall pair (shm_open/mmap, then
munmap/close/unlink), paid on *every* frame, and that fixed cost made it
measurably *worse* than plain pickling at 2.1MB (+17.6ms vs. +7.4ms)
before it was gated by size.

**Second fix, checked against the same benchmark rather than assumed
better**: `SharedFrameWriter`/`SharedFrameReader` now reuse *one*
persistent segment per source, resized only when the frame's shape/dtype
actually changes, instead of one-shot per frame. Reuse is safe without
double-buffering specifically because `rpc.py`'s protocol is strictly
synchronous request/response — the server cannot start writing frame
N+1 until it receives the client's next `Call`, which the client only
sends after it has already copied frame N out, so the two sides are
never touching the buffer at once. Result: pickle and shared memory are
within noise of each other from ~30KB to ~500KB (both +0.3–0.5ms over a
direct in-process call), and shared memory pulls smoothly ahead above
that — +2.8ms at 2.1MB, +9.5ms at 8.4MB, +25ms at 33.6MB, versus the
one-shot design's +72ms or naive pickle's +168ms at that same largest
size. `_SHARED_MEMORY_THRESHOLD_BYTES` (64KB) sits in the
"doesn't matter much either way" band this measured, so it's kept
rather than tuned further — the earlier "revisit the exact crossover"
open item is resolved by the redesign, not by finding a better number
for the old one.

Reuse creates a real correctness obligation the one-shot design didn't
have: a named POSIX segment is *not* reclaimed when its creating process
dies (unlike its threads or its anonymous memory) — it is a persistent
tmpfs entry until explicitly `unlink()`-ed. The one-shot design was
already leak-safe under `Popen.terminate()`, because the *reader*
unlinked immediately after every single read, and readers are ordinary
long-running processes that get to run their own cleanup normally. A
reused, writer-owned segment is not: `remote.py`'s teardown now
explicitly calls `.close()` on each device (triggering the server's
`SharedFrameWriter.close()` → `unlink()`) *before* terminating the
subprocess, rather than relying on the hard kill alone. Verified with a
dedicated leak test (`test_no_shared_memory_segments_leak_after_teardown`)
that snapshots `/dev/shm` before and after a full spawn-to-teardown
session and asserts nothing new remains.

**A third, unrelated bug the same benchmark surfaced**: two scan sizes
among eleven tested (64×64 and 90×90, both on the plain-pickle path)
showed a strikingly consistent ~44ms stall — p95 within 0.2ms of the
median, not the shape ordinary scheduling noise produces. 44ms is close
enough to Linux's ~40ms delayed-ACK timer to be the signature of Nagle's
algorithm and the receiver's delayed ACK waiting on each other.
Confirmed directly: a plain `multiprocessing.connection` socket pair has
`TCP_NODELAY` unset on both ends by default. `rpc.disable_nagle()` sets
it on every connection either side opens, not just the two sizes that
happened to reproduce the stall in one run — Nagle/delayed-ACK
interactions are inherently data-pattern-dependent, which is exactly why
only 2 of 11 tested sizes hit it. Confirmed fixed: both anomalous sizes
dropped to +0.4ms after the change.

One stdlib wart surfaced along the way and is worth recording: each
process's `resource_tracker` auto-registers any `SharedMemory` handle it
touches (create *or* attach-by-name) and tries to clean it up again at
exit, which — since we manage each segment's lifecycle explicitly — just
finds it already gone and warns, once per segment, on every run. The
documented fix (`resource_tracker.unregister()`) assumes register and
unregister land on the same tracker daemon, true within one
`multiprocessing.Process` tree but not here (server and client are
independent `subprocess.Popen` processes, each with their own daemon);
trying it made things worse, crashing the *other* daemon's main loop with
a `KeyError` instead of merely warning. `PYTHONWARNINGS`, read by every
interpreter at its own startup including a forked+exec'd tracker daemon,
is what actually works — set once in `shared_frame.py` and inherited by
the server subprocess's environment.

**A fourth investigation, with a clean negative result: transparent zstd
compression of frames moving through shared memory does not pay for
itself, at any size or level tested.** The question, raised separately
from the above: since the reused-segment redesign made shared memory a
plain memcpy, could compressing frames before the write and decompressing
after the read shrink the bytes moved and reduce end-to-end latency,
using idle cores for the compression work (`zstandard`'s
`ZstdCompressor(threads=N)`, which wraps Facebook's zstd with native
multi-threaded compression)? `zstandard` itself checks out fine as a
dependency choice, unlike Arrow Plasma earlier in this project's history
(confirmed dead, deprecated in Arrow 10, removed ~12): PyPI shows a 0.25.0
release from September 2025, an actively maintained GitHub project, and a
compiled C-extension backend (not the slower pure-Python fallback) in
this environment. The reason to suspect it wouldn't help regardless is
already on record in §5's Phase 3 "Revisit compression" item: gzip
level 4 on noisy float64 scan data measured a 1.08× ratio (bigger than
raw) against 0.69× for float32 camera frames — this project's real
detector data is photon/thermal noise, not the smooth natural images
generic compressors are tuned for.

Measured with a new script,
[`scripts/shared_memory_compression_benchmark.py`](../scripts/shared_memory_compression_benchmark.py)
(same structure as `ipc_overhead_benchmark.py`), against real frames from
`remote_simulated_instrument()` — scan sizes from 64×64 (32KB, below
`_SHARED_MEMORY_THRESHOLD_BYTES`) up to 2048×2048 (33.6MB), plus both
camera frames (EELS 256×1024 float32 ~1.0MB, Ronchigram 2048×2048 float32
~16.8MB) — at zstd levels 1, 3, 9, and 12, single-threaded and with
`threads=os.cpu_count()` (4 in this container), timed through the actual
`SharedFrameWriter`/`SharedFrameReader` classes (the compressed bytes are
published/read as the payload, so the comparison includes the real memcpy
cost of whatever is actually moved, not an isolated compression
microbenchmark):

- **Ratios confirm the gzip finding, and extend it**: scan frames
  compressed to only 0.954–0.958× regardless of level — statistically
  indistinguishable from "doesn't compress," same as gzip found. Camera
  frames compressed better with zstd than the gzip datapoint suggested
  (Ronchigram 0.73× at level 1 down to 0.61× at level 12; EELS 0.30× down
  to 0.21×) — zstd's better modeling helps on this data, but not remotely
  enough to matter given the timings below.
- **Compression is 5×–300× slower than the memcpy it would replace, at
  every size and level tested, with no exceptions.** The raw
  `SharedFrameWriter.publish`+`SharedFrameReader.read` round trip is
  already fast because it is one memory-bandwidth-bound copy each way:
  0.02ms at 32KB, 1.25ms at 8.4MB, 3.81ms at 18.9MB, 26.1ms at 33.6MB for
  scan frames; 0.17ms for the 1.0MB EELS frame; 4.33ms for the 16.8MB
  Ronchigram frame. zstd compression is CPU-bound and orders of magnitude
  more expensive per byte than a copy: the *best* result anywhere in the
  sweep — level 1, `threads=4`, the largest 33.6MB scan frame — still cost
  117ms round trip against a 26.1ms baseline (4.5× slower). Worse cases
  are common: the 1.0MB EELS frame at level 12 cost 53ms against a
  0.17ms baseline (309× slower); the 16.8MB Ronchigram frame at level 12
  cost 1267ms against 4.33ms (293× slower).
- **The threading claim is real but bounded, exactly as expected from how
  zstd multi-threading works (splitting input into independent blocks,
  trading ratio for parallelism)**: at 64×64–256×256 (32KB–524KB),
  `threads=4` made no measurable difference or was marginally worse
  (thread-pool setup cost with too little data to split) than
  `threads=1`. From ~1MB up, `threads=4` did measurably cut wall time —
  ~17–40% faster than `threads=1` at the largest scan and Ronchigram
  sizes — confirming idle cores genuinely engage for frames in the
  multi-megabyte range. It never closed anywhere near the gap to the raw
  memcpy path, because that gap is 1–3 orders of magnitude, not the
  ~2–4× a handful of idle cores can buy back.
- **Why this differs from the naive-pickle-over-socket case compression
  might have helped**: that path pays a real "bytes over a wire" cost —
  serialize, then copy through a kernel socket buffer, on a local
  loopback connection that still round-trips through the TCP/IP stack.
  Shared memory has no wire: `SharedFrameWriter`/`SharedFrameReader` are
  already just `np.ndarray` view assignment and `.copy()` against a
  `mmap`-backed segment, at whatever memory bandwidth the machine has.
  Compression only pays off when it removes work that is actually the
  bottleneck; on this path the bottleneck is memory bandwidth for a copy
  that is already single-digit-to-tens-of-milliseconds for the largest
  real frames this project produces, and zstd's decode+encode cost per
  byte is fundamentally higher than a copy's, no matter how many idle
  cores run it in parallel.

**Verdict: not implemented.** This is a complete, negative answer to the
question, not an unfinished feature — `shared_frame.py` is unchanged, and
`zstandard` was not added to `pyproject.toml` as a dependency (it was
installed ad hoc, `uv pip install zstandard`, only to run the benchmark
script; the script's own docstring notes this so it stays reproducible
without weighing down the shipped dependency set for a capability that
isn't shipping). The benchmark script is kept in `scripts/` as the record
of how this was checked, the same way `ipc_overhead_benchmark.py` and the
Phase 2 live-viewer benchmark are kept regardless of which way their
results pointed.

**Graceful shutdown, with SIGTERM demoted to a fallback.** The handshake is
one RPC on the existing `instrument` target. On `shutdown()` the server
stops every camera, calls `instrument.park()` — which blanks the beam if a
blanker exists and does nothing if not, because parking the stage or
dropping the high tension would be a guess about the hardware rather than a
service to it — closes every device, closes each device's
`SharedFrameWriter`, replies with a plain-data report, and only then lets
`serve()` return so the process exits 0. Every step is individually guarded
and its failure recorded in the report's `errors` list rather than raised,
so a half-successful park still acknowledges *and* still unlinks its
segments. The stop event is set only *after* the reply is sent, which keeps
the acknowledgement from racing the listener teardown.

The client waits a bounded 10s (`rpc.send_call` grew an opt-in `timeout_s`
and `RemoteCallTimeoutError`; ordinary device calls still wait forever,
because an acquisition genuinely takes as long as it takes and a wrong guess
would abort a good exposure). On timeout, on error, *or on a report that
lists errors*, it falls back to the pre-existing path — closing each device
over its own connection, which unlinks the same segments — then
`terminate()`, escalating to `kill()`.

**The shared-memory guarantee is preserved, and that ordering is the whole
point.** Both paths unlink: the graceful one because the server closes its
own writers, the fallback because the client closes each device. Verified
both ways against the same three named segments the leak test uses, and the
wedged case is exercised against a genuinely unresponsive server rather
than a mocked client — `MIAINWOODPECKER_WEDGE_SHUTDOWN` is a documented
test-only hook that makes the shutdown handler block forever without
replying, so the real socket-timeout path runs and the test asserts the
process really died of `SIGTERM` rather than inferring it. Two smaller
consequences: `NionCamera.close()`/`NionScanner.close()` are now idempotent
(park closes devices, and the owning context manager closes them again on
its way out), and an explicit `shutdown()` deliberately is not re-callable
over the same connection, because acknowledging a shutdown ends the
server's life — teardown checks `process.poll()` first and skips to
detaching readers, so calling the handshake yourself yields exit status 0
rather than a pointless second SIGTERM. That last one was found by a test
that assumed idempotence at the wrong layer; the failure was the correct
signal.

**Telling a live server from a dead or wedged one.** `instrument`'s
`check_health()` distinguishes three genuinely different conditions with
three different correct responses: responsive (0.4 ms round trip), exited
(via `Popen.poll()`, reporting the exit status and naming the signal), and
alive-but-unresponsive (a bounded 5 s wait). It is meaningful *because* the
server-side handler reads process state only — no device, no vendor object,
no lock — so it neither perturbs an acquisition nor queues behind one. Once
the process is gone, device and control calls raise
`RemoteConnectionLostError` naming the signal, in under 10 ms both at idle
and from inside an in-flight 4096×4096 scan. Ordinary device calls still
wait forever, unchanged and deliberately: a dead server needs no timeout,
because its socket closes.

**Reconnect was considered and deliberately not built**, and the reason is
data integrity rather than effort. A fresh server subprocess is a fresh
instrument construction, so a started camera, the scan settings in use, and
every instrument control revert to defaults — and a recording in progress
would keep appending frames to the *same file* from a differently-configured
instrument, which is a corrupted scientific record rather than an interrupted
one. Compounding it, a server that died without parking leaves the column in
a state nothing client-side knows, which warrants operator attention rather
than silent recovery. An explicit `reconnect()` was rejected too:
`remote_instrument()` already *is* how a session is obtained, so a second
entry point could only duplicate it while implying a continuity it cannot
deliver. Failing fast, with the context manager as the recovery path, is the
whole design.

**What a hard-killed server actually guarantees — where measuring changed
the answer, so the correction is worth stating plainly.** This section
previously assumed a `SIGKILL`-ed server *must* leak its segments, since
named POSIX segments are tmpfs entries rather than process resources and a
killed process runs no cleanup. Both premises are true and the conclusion
still did not follow: `multiprocessing`'s `resource_tracker` is a **separate
child process** of the server, it auto-registers every segment
`SharedFrameWriter` creates (the same auto-registration recorded above as a
warning-noise wart), and it unlinks everything it holds when the server's end
of its pipe closes. Measured: `SIGKILL` the server alone and the segments are
gone immediately and stay gone.

That is a CPython implementation detail rather than a documented guarantee,
and it has an identifiable failure mode — it needs the tracker to outlive the
server. Kill both (a process-group kill, a cgroup OOM kill, a container stop,
`kill -9` on a process tree) and the segments genuinely persist; verified by
killing the tracker first and confirming all three of a session's segments
survive. So `SharedFrameReader.unlink_orphan()` lets the client reclaim the
names it attached to — a deliberate, narrow exception to this module's
writer-owns-unlink rule, sound only because that rule's premise (a live
writer that keeps recreating the segment) has failed along with the writer's
process. Teardown sweeps with it once the process is gone, *whatever killed
it*, rather than conditionally on exit status: the precondition that makes
unlinking legitimate is a dead writer, not a particular exit code, and
conditioning on status would strand exactly the segment that needed
reclaiming when a server exits 0 having *recorded* a shared-memory error.

| How the server ended | What unlinks its segments |
|---|---|
| Graceful `shutdown()` handshake | the server itself, before replying |
| SIGTERM fallback (wedged server) | the client's per-device `close()`, over live connections, before the kill |
| `SIGKILL` of the server alone | the server's `resource_tracker` child — measured, but a stdlib implementation detail |
| `SIGKILL` of the server **and** its tracker | the client's `unlink_orphan()`, for every segment it read a frame from |

**The non-guarantees are real and are not bugs the client can fix.** A
segment whose name never reached the client (killed between `shm_open` and
the reply carrying its `SharedFrameRef`) is unrecoverable from this side —
nothing here ever learned its name; the window is one create-publish-send
sequence per device per resize, narrow but not zero. And if the client dies
at the same moment as the server and its tracker, nothing runs at all. Both
residuals are bounded by the reuse design itself — one segment per device
per shape, not one per frame — so the worst case is a handful of stale
entries rather than unbounded growth. The tests state exactly this and no
more, and the tracker-dies-too test asserts in two stages (first that the
segments *do* survive, then that teardown reclaims them) so it cannot pass
vacuously if the mitigation were deleted.

**Server-side failures are now diagnosable.** The subprocess inherited the
parent's stderr, so anything it said interleaved anonymously with the
application's own output — including, on hardware day, the one diagnostic
that matters when `--backend=hardware` finds no instrument. It now uses
stdlib `logging` configured only in `main()`, so importing the module
in-process (which its own tests do) leaves logging inert in the standard
way. Covered: startup, per-plug-in load outcome, bound ports, connection
accepts, per-call failures **with the traceback** (the wire protocol carries
only a stringified error, so the log is the only place it survives), and the
shutdown report. Deliberately *not* covered: anything on the frame path — a
successful call is logged at no level, so the shared-memory publish/read
path is untouched and this section's benchmarks stand (re-measured after the
change: acquire median unchanged within run-to-run variance). Quiet by
default via `MIAINWOODPECKER_DEVICE_LOG_LEVEL` (default `WARNING`), with
`MIAINWOODPECKER_DEVICE_LOG_FILE` to take it out of a shared terminal; a bad
level name or unopenable file warns and degrades rather than taking the
server down over its own diagnostics. Every record carries the pid, and the
logger is named explicitly rather than from `__name__`, which under `python
-m` would be the useless `"__main__"`.

**One pre-existing breach of this section's own invariant, found while
auditing the boundary and since closed.**
[`src/miainwoodpecker/storage/legacy.py`](../src/miainwoodpecker/storage/legacy.py)
did `from nion.swift.model import NDataHandler` at module scope — an
in-process import of GPL-3.0 code by an MIT module, exactly what this
section's decision exists to prevent. It predated the subprocess isolation
work and was arguably a *narrower* exposure than the device layer's (a
one-shot file-format read, only reachable with the `device` extra
installed), but "arguably narrower" is not "on the right side of the
boundary", and the original reasoning — reuse a vendor reader rather than
re-implement a container — was correct in general and the wrong trade here.

**The obvious fix does not exist, which is worth recording so it isn't
proposed again.** RosettaSciIO looked like the answer: §3 already names it
as this project's file-I/O building block, so reusing it would have removed
the problem without new code. It has **no `.ndata` reader** — checked
directly, 38 IO plugins and none handles the extension. Reading a claim
like that off a dependency's reputation rather than its plugin list is the
same shortcut this plan keeps having to correct.

**What shipped instead: a standard-library reader.** Re-implementing turned
out to be cheap because the format is documented and trivial —
`NDataHandler`'s own docstring says "ndata files are a zip file consisting
of data.npy file and a metadata.json file. Both files must be
uncompressed." Nion hand-rolls a zip parser because *writing* in place needs
byte offsets into uncompressed members; reading needs none of that, so
`zipfile` + `numpy.load` + `json.load` is the entire implementation.
Verified against a genuine Nion-written file before committing to the
approach: both members are `ZIP_STORED`, the extracted stream is seekable,
and data and properties round-trip exactly. This is reading a documented
format, not the bespoke-format invention §3 warns against — and it *removes*
a dependency rather than adding one: `legacy.py` now needs no optional
extra at all.

The risk that replaces the old one is a reader validated against our own
assumptions about the format rather than the format itself, so the existing
integration tests keep building their fixtures with **Nion's own writer**
(a test is not the distributed application, and nothing there is imported by
shipped code), while `tests/unit/test_legacy_reader.py` covers the error and
edge paths — a non-zip file, a container with no array, a file with no
metadata member — with hand-built zips in the base environment, which is
also the standing proof that the reader needs no vendor code. Two behaviours
worth noting: a file whose `metadata.json` is missing still yields its array
rather than being refused, because the array is the irreplaceable part; and
a truncated or unrelated file raises a `ValueError` naming the path instead
of a `zipfile` traceback.

The invariant now holds mechanically: `nion.*` is imported in
`nion_server.py` and nowhere else in `src/`.

## 7. Open questions

- **A full-stack architecture review has been done, and what it found is
  tracked in [`architecture-review.md`](architecture-review.md)** rather
  than duplicated here. The verdict was that this plan's load-bearing
  decisions hold mechanically — the license boundary, the layering, the
  no-Qt-in-workers rule — and that the defects were at the *seams*, where
  two individually-correct halves met with mismatched assumptions.
  - **The pattern in §8 held for a third time.** That section already
    records two of this plan's own claims turning out false when finally
    checked (`definition = "NXem"` on files that did not validate;
    §6's never-import-`nion.*` invariant already breached by
    `storage/legacy.py`). The review added two more of exactly that
    shape. `fov_nm` was documented as "field of view of the scanned
    region" without saying *which axis*, and storage had picked the
    reading Nion does not use — so every non-square scan was written with
    a slow-axis scale wrong by the aspect ratio, with three of this
    project's own tests asserting the bug. And the per-frame `flush()`
    above was made public, described as the fix, and then never called by
    any shipped code path. Neither would have surfaced from tests,
    because the tests encoded the same assumption the code did.
  - **What that suggests for this plan's method**: "measure, don't
    assume" (§1) has been reliably applied to *new* questions and
    reliably not applied to claims already written down. Both new
    findings came from checking a stated claim against the thing it was a
    claim about — Nion's own calibration source, and a grep for callers.
- **Is "session" the right concept at all?** Raised by @msarahan from
  operator experience: people tend to use plain filesystem folders to
  represent a session and keep the data for one grouped in that folder, so
  a named abstraction risks being ceremony over something the filesystem
  already does. Worth noting how little separates the two positions —
  `storage/session.py`'s own docstring already says "the filesystem is the
  index and NeXus files are the records", and a `Session` *is* a directory.
  Over a bare folder it adds exactly three things: a collision-free naming
  rule, a `session.json` sidecar, and background jobs so slow I/O does not
  freeze the GUI. The first and third are plumbing any design needs. So the
  live question is narrower than the framing suggests: **does the sidecar
  earn its place?** Since the writer grew `sample=`/`user=`/`notes=`, every
  fact it holds is also written into each file as real NeXus groups, which
  argues for demoting it from a second source of truth to remembered
  defaults for the next recording in that folder. Not yet acted on — the
  sidecar is still what makes reopening a directory mid-shift restore its
  context, and cross-session enumeration (below) would lean on it too.
- **Grouping analyses with the data they came from**, also from @msarahan:
  at some point it may be worth keeping a derived result in the same
  container as its source rather than as a loose sibling file. NeXus needs
  no invention for this — multiple `NXentry` groups in one file is the
  standard shape, and `NXprocess` exists for exactly "this was derived, by
  this program, from that". Deliberately not designed yet: Phase 4's
  adapters are proofs of concept, and the right container layout follows
  from a real analysis workflow rather than preceding one.
- **Bluesky/ophyd**: the [Bluesky](https://blueskyproject.io/) experiment
  orchestration framework (device abstraction via `ophyd`/`ophyd-async`,
  scripted acquisition via a `RunEngine`) is a real, actively developed
  option for the device/acquisition layer, used across synchrotron
  facilities. It's not currently used for electron microscopy, and it's
  script-first rather than live-tuning-first, which doesn't match how STEM
  operators actually work moment to moment. Recommendation: skip it for v1
  (Phases 1–2), and revisit only if/when scripted multi-step acquisitions
  (automated tilt series, autotuning) become a priority.
- **Real hardware validation** (§2's remaining open item): the device
  server's serving loop, shared-memory transport, and threshold have only
  been exercised against `nionswift-usim`. Real hardware may have
  different frame-rate/size characteristics worth re-benchmarking against
  once available. The gating item is no longer open-ended, though: it is
  now the six enumerated assumptions under §5 Phase 1 plus a vendor
  `nionswift_plugin` package, and
  [`docs/hardware-validation-checklist.md`](hardware-validation-checklist.md)
  is the ordered procedure. Highest-consequence item on it: sanity-check
  the defocus *magnitude* before trusting it, because a metres/nanometres
  mix-up is a factor of 1e9 sent to the column, not a rounding error.
- ~~**Calibration exists as a model but nothing feeds it from the
  instrument.**~~ **Closed.** §5 Phase 3's calibration work built the model;
  the plumbing turned out to already exist, on the far side of the licence
  boundary. A Nion camera device publishes no calibration *values* — it
  publishes a `calibration_controls` mapping naming the *instrument
  controls* that hold them, because a camera's angular scale depends on the
  projector lenses and is therefore instrument state. `nion_server`'s
  `NionCamera.calibration_metadata` resolves them with Nion's own
  `camera_base.build_calibration` and returns per-axis
  `{kind, scale, offset, units}` as plain data in the frame metadata, which
  `resolve_frame_calibration` already reads. No reimplementation, no
  `nion.*` across the boundary, and no hardware: usim publishes real values
  (Ronchigram 9.83e-05 rad/px offset -0.1007 rad, EELS 0.5 eV/channel offset
  -20 eV, EELS slow axis reporting empty units for "not calibrated").
  Two divergences from Nion, both deliberate: an axis is given its own
  length rather than the other axis's when centring, and an axis whose units
  fall outside this project's closed vocabulary degrades to pixels rather
  than propagating a unit nothing downstream can interpret.
  Exposure and binning control landed with it, for the mechanical reason
  the two want doing together: binning multiplies the calibration scale
  (`build_calibration`'s `relative_scale`). `CameraParameters` and
  `Camera.configure` carry them, and the binning a frame *reports* is
  recovered from its shape rather than from the setting, because a camera
  reconfigured while running finishes the frame in flight at the old
  settings — measured against usim, and a frame mislabelled there would
  carry an axis wrong by the whole binning factor.
  What remains of this bullet is the *operator-facing* half: no UI selects a
  microscope mode or exposes these controls, so a mode the device does not
  describe still needs code.
- ~~**The EELS dispersive-axis default is grounded in the simulator only.**~~
  **Closed, by removal.** `dispersive_axis="x"` remains a parameter of
  `FrameCalibration.spectrum` for hand-built calibrations, but no acquired
  EELS frame reaches it any more: the dispersive axis is the one whose units
  the *device* reports as `eV`, which the calibration path above now reads.
  A rotated spectrometer is handled without a configuration change, and this
  is off the hardware checklist rather than on it.
- **Scan data arrives as `float64` for no physical reason.** usim's
  `generate_scan_data` is annotated `-> NDArray[float32]` and builds float32
  internally; the promotion happens in its final line, where
  `numpy.random.randn()` (float64) is added to a float32 array. Storing
  float32 is a measured **2.33×** file-size win at a quantization error
  1.6e-07 of the data's own noise standard deviation (§5 Phase 3), so the
  fix belongs upstream of storage rather than as a silent downcast in the
  writer. Real-hardware dtype is unvalidated, which is the other reason not
  to change the default yet.
- **SWMR-mode writing** conflicts with `NexusWriter` creating its `NXdata`
  group at `close()` (it needs the final frame shape for the axes).
  Supporting it means restructuring the writer to create NXdata up front —
  a real design change, and the remaining step beyond the per-frame
  `flush()` that already bounds worst-case loss to one frame.
- ~~**`storage/legacy.py` imports GPL-3.0 code in-process**~~ —
  **resolved**: re-implemented on the standard library, so `nion.*` is now
  imported in `nion_server.py` and nowhere else in `src/`. See the end of
  §6, including why RosettaSciIO (the obvious reuse candidate) could not do
  it.
- ~~**Shared-memory threshold precision**~~ — **resolved** by the
  reused-segment redesign (§6), not by fitting a better number for the old
  design. The threshold is now 64KB and sits inside a band the benchmark
  measured as "doesn't matter much either way" (pickle and shared memory
  within 0.3–0.5ms of each other from ~30KB to ~500KB). It does want
  re-measuring at real frame rates, which is on the hardware checklist.
- **A real 4D-STEM acquisition mode**: §5's Phase 4 py4DSTEM follow-up
  measured, rather than assumed, that even the simulated device stack
  cannot produce scan-position-varying diffraction frames without
  registering a `ScanHardwareSource` with the simulator's `STEMController`
  - which needs the full `HardwareSource`/`Application` layer this
    project has twice now (Phase 0, and this investigation) found too
    heavy to stand up outside Swift's own process. If py4DSTEM's/
    LiberTEM's headline `DataCube` type is ever wanted for real
    (synchronized scan-position × diffraction-pattern) data, that
    application-layer question needs answering first - it is not solvable
    by adding a method to the vendor-neutral `Scanner`/`Camera`
    interface alone.

## 8. Summary

Beyond the device layer, almost nothing here needs to be built from
scratch: napari + PySide6 for the shell and rendering, HDF5 + NeXus/NXem
+ RosettaSciIO for storage and I/O (Zarr evaluated and declined — §5
Phase 3), and HyperSpy/py4DSTEM/LiberTEM for analysis. The actual new code
this project needs to write is the device bridge (Phase 1), the live-viewer
dock widget (Phase 2), the acquisition sequencer, session layer, and
legacy-data importer (Phase 3), and analysis wiring (Phase 4: one adapter
function and one menu action each into HyperSpy, LiberTEM, and, on
single-diffraction-pattern terms, py4DSTEM) — glue, as intended.

Two of this plan's own claims turned out to be false when finally checked,
which is worth recording as a pattern rather than as two isolated
corrections. The files declared `definition = "NXem"` for three phases
before anyone validated them, and they did not conform — one required
`NXsample` group short. And §6's own central invariant, that the MIT
application never imports `nion.*`, was already breached by
`storage/legacy.py` while §6 was being written. Both were found by checking
a stated claim rather than by anything failing, and neither would have
surfaced from tests, which is the argument for §1's "measure, don't assume"
extending to the documentation as well as the code.
The LiberTEM adapter is also a useful lesson in the plan's own "measure,
don't assume" principle (§1): an earlier version of this plan grouped
LiberTEM with py4DSTEM as both needing 4D-STEM data this app doesn't
produce yet, reasoning by category (“pixelated-detector analysis tool”)
rather than by checking LiberTEM's actual object model — checking it
directly found the category-level assumption wrong for one of the two
libraries, not both. py4DSTEM's own headline `DataCube` type stays out of
reach for now, not from an unchecked assumption but from a direct
measurement (§5, §7): the simulated device stack won't vary a
diffraction frame with beam position without an application layer this
project has twice found too heavy to stand up outside Swift's own
process.
