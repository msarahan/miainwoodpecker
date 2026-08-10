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
| NionUI (custom widget toolkit + C++ Qt launcher) | **PySide6** + **[napari](https://napari.org/)** as the application shell | Napari is a mature, actively developed Qt/VisPy-based n-dimensional image viewer with a real plugin ecosystem; adopting it deletes the launcher, the widget abstraction, and the declarative-UI layer in one move. |
| `CanvasItem`/`DrawingContext` (Python-driven command-list rendering, CPU rasterized via `QPainter`) | Napari's **VisPy/OpenGL** canvas | GPU compositing instead of Python rebuilding a draw program every repaint — this is the single biggest latency fix Swift needs. |
| `nionutils` (`Event`, `Observable`, `Binding`, `Stream`, `Registry`) | Qt signals/slots, and napari's own **evented models** (`psygnal`) | Standard, widely used reactive primitives instead of a bespoke fan-out graph that amplifies small property changes into cascades. |
| Custom project format + `.ndata` | **HDF5/Zarr** for arrays, **NeXus/NXem** for metadata, via **[pynxtools-em](https://github.com/FAIRmat-NFDI/pynxtools-em)** and **[RosettaSciIO](https://github.com/hyperspy/rosettasciio)** | NXem is a real, current (Oct 2025) NeXus application definition specifically for electron microscopy; RosettaSciIO (spun out of HyperSpy) already reads/writes essentially every EM vendor format (dm3/dm4, EMD, ser, …), so file I/O becomes "use the library" instead of "maintain a format." |
| Built-in analysis tools | **[HyperSpy](https://hyperspy.org/)** for general multidimensional EM analysis (EELS/EDS/etc.), **[py4DSTEM](https://github.com/py4dstem/py4DSTEM)** and/or **[LiberTEM](https://libertem.github.io/LiberTEM/)** for 4D-STEM / high-throughput pixelated-detector data, **[pyxem](https://github.com/pyxem/pyxem)**/**[kikuchipy](https://kikuchipy.org/)** for diffraction workflows if needed | These are the community's actual analysis tools for this data, actively maintained, and already ahead of what a small team can build and keep current. |
| `Facade.py` (hand-maintained versioned API shim) | Not needed | Only existed to keep Swift's own plugin API stable across versions. If new "plugins" are just ordinary napari plugins, there's no bespoke API surface to shim. |

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
  [`src/miainwoodpecker/devices/`](https://github.com/SuperSTEM/miainwoodpecker/tree/main/src/miainwoodpecker/devices):
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
- [ ] Validate against real hardware.

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
  **Result on this container (llvmpipe software rasterization), 512×512
  scan:** acquire 12.7 ms median (79 fps ceiling); display 42.4 ms median,
  p95 57 ms (23.6 fps) — display costs **3.35× acquire**.
  *This is not yet a verdict against napari.* Under software rendering the
  rasterizer competes with acquisition for the same CPU, so the figure is
  a pessimistic floor; the script detects the GL renderer and says so.
  Two measurement traps found while building it, either of which makes
  napari look ~2× faster than it is, and both of which the script now
  avoids: (1) with `show=False` the canvas is hidden, Qt issues no paint
  events, and the GPU draw never happens; (2) assigning `layer.data` only
  *schedules* a repaint, so the event loop must be flushed **inside** the
  timed region. An honest hidden-vs-shown comparison went 5.3 ms → 22.7 ms
  for the same operation.
- [ ] Re-run the benchmark on a GPU workstation at real scan rates. Only a
  hardware-accelerated result showing display still dominating justifies
  moving to `ndv` or a custom VisPy canvas.

**Phase 3 — Acquisition and storage**
- [x] Acquisition sequences —
  [`src/miainwoodpecker/acquisition/sequence.py`](../src/miainwoodpecker/acquisition/sequence.py).
  Plain lazy generators over the device protocols (`scan_series`,
  `camera_series`, `focal_series`) plus `record()`, which streams them to
  disk as they arrive rather than buffering. `camera_series` stops the
  camera in a `finally`, so abandoning a series early still releases the
  device. `focal_series` currently sweeps field of view: sweeping focus or
  stage tilt needs instrument controls the Phase 1 interface deliberately
  does not expose yet, but the generator shape carries over.
- [x] NeXus/HDF5 storage —
  [`src/miainwoodpecker/storage/nexus.py`](../src/miainwoodpecker/storage/nexus.py).
  **Deliberately written with `h5py` alone, not `pynxtools-em`.** NeXus is
  a *convention over HDF5* (typed `NX_class` groups, `signal`/`axes`
  plotting hints, `units` everywhere); `pynxtools-em` is a
  vendor-format→NXem *reader/converter* and pulls ~70 packages (hyperspy,
  scikit-learn, sympy, xraydb…) to supply, for our purposes, a schema
  convention. Following a documented format is not reinventing one — this
  is precisely what avoids a bespoke project format. `NexusWriter` streams
  into a resizable per-frame-chunked, gzip dataset so long acquisitions
  persist incrementally, and scan frames reporting `fov_nm` get real
  spatial axes in nanometres (cameras correctly fall back to `pixel`).
  **Independently validated**: files load in `nexusformat` (the NeXpy
  reference library), which resolves the class hierarchy, `nxsignal`,
  `nxaxes`, and reports `plottable_data` — so standard NeXus tooling can
  plot them without any of our code.
- [x] Legacy `.ndata` importer —
  [`src/miainwoodpecker/storage/legacy.py`](../src/miainwoodpecker/storage/legacy.py).
  Uses Nion's own `NDataHandler` rather than re-implementing its zip
  container, converts to `Frame`, and recovers Swift's naive-UTC
  timestamps as aware. Tests write fixtures with Nion's *writer*, so the
  real container format is exercised, and cover the full migration path
  (old library directory → single NeXus file).
- [ ] Validate output against the official NXem NXDL schema with
  `pynxtools`. Files declare `definition = "NXem"` to state intent, but
  that claim is currently unverified — the NeXus/FAIRmat spec sites are
  unreachable from this environment, so the required-field list could not
  be checked. This belongs in a CI validation step, not the runtime.
- [ ] Revisit compression. gzip level 4 on noisy `float64` scan data
  measured a *1.08× ratio* — i.e. slightly larger than raw — while
  `float32` camera frames compressed to 0.69×. Worth evaluating
  bitshuffle/blosc, or storing scan data as `float32`. Still open for
  *this* (HDF5 storage) context, but §6 now has a related, resolved data
  point for the shared-memory IPC context: zstd on the same kind of
  frames confirms scan data barely compresses (0.95–0.96×) and shows
  camera frames compress better with zstd than gzip suggested
  (0.61–0.73×) — consistent with, and a useful cross-check on, this
  item's numbers, even though that investigation concluded compression
  doesn't belong on the IPC path regardless of ratio, for reasons
  (memcpy vs. CPU-bound compression time) specific to shared memory that
  don't necessarily transfer to the storage question here.
- [ ] Consider Zarr alongside HDF5 for parallel/cloud-friendly writes.

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
- [ ] Port Swift-specific analyses not already covered upstream, as small
  adapter functions. **Deferred, not attempted**: this PoC's scope was
  proving the wiring shape (adapter + one real menu action) works end to
  end, not auditing Swift's analysis feature set for gaps HyperSpy/
  py4DSTEM/LiberTEM don't already cover. That audit is real work for a
  follow-up, not a checkbox to wave through here.

**Phase 5 — Parity and cutover**
- Audit which Swift features the team actually uses day to day (not the
  full feature surface) and build a parity checklist from that.
- Pilot the new app in parallel with Swift on one instrument before cutover.

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
  `remote_simulated_instrument()` spawns the server subprocess, connects
  with a generated authkey, and tears down with `Popen.terminate()`
  (SIGTERM) rather than a graceful RPC shutdown — sufficient here because
  the whole *process* being killed reclaims its threads and sockets
  regardless of Python-level cleanup; a real-hardware backend would likely
  need a gentler path to park the instrument safely, simulated hardware
  does not.
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

## 7. Open questions

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
  once available.
- **Shared-memory threshold precision**: see §6 — 8MB is conservative,
  not precisely fitted; the actual crossover between plain-pickle and
  shared-memory transport is noisier than a single benchmark run resolved.

## 8. Summary

Beyond the device layer, almost nothing here needs to be built from
scratch: napari + PySide6 for the shell and rendering, HDF5/Zarr + NeXus/NXem
+ RosettaSciIO for storage and I/O, and HyperSpy/py4DSTEM/LiberTEM for
analysis. The actual new code this project needs to write is the device
bridge (Phase 1), the live-viewer dock widget (Phase 2), the acquisition
sequencer and legacy-data importer (Phase 3), and analysis wiring
(Phase 4: two adapter functions, HyperSpy and LiberTEM, one menu action
each driving them) — glue, as intended. The LiberTEM adapter is also a
useful lesson in the plan's own "measure, don't assume" principle (§1):
an earlier version of this plan grouped LiberTEM with py4DSTEM as both
needing 4D-STEM data this app doesn't produce yet, reasoning by category
(“pixelated-detector analysis tool”) rather than by checking LiberTEM's
actual object model — checking it directly found the category-level
assumption wrong for one of the two libraries, not both.
