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
work here is a thin adapter, not a rewrite. **All of this is GPL-3.0**, which
in practice means the new application inherits that license unless the
device layer is kept fully isolated behind a process boundary.

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
  [`scripts/phase0_viewer_smoke_test.py`](../scripts/phase0_viewer_smoke_test.py)
  (`uv run --extra viewer python scripts/phase0_viewer_smoke_test.py`).
  Passed cleanly with `QT_QPA_PLATFORM=offscreen`, so this also runs in
  headless CI without a real display.
- [x] Settle on a package layout beyond the pyOpenSci template scaffold —
  package is named `miainwoodpecker`; the demo `add_numbers` module has
  been replaced by the Phase 1 device bridge.
- [ ] Decide the license (see §6).

**Phase 1 — Device bridge**
- [x] Define a vendor-neutral `Camera`/`Scanner` interface and wrap Nion's
  device objects behind it — implemented in
  [`src/miainwoodpecker/devices/`](../src/miainwoodpecker/devices/):
  `interface.py` holds runtime-checkable structural `Protocol`s plus the
  neutral data types (`Frame` = data + aware timestamp + metadata;
  `ScanParameters` in operator units — pixels, µs, nm), and
  `nion_adapter.py` wraps the `nion.device_kit` camera/scan objects
  directly (per the Phase 0 finding, *not* the
  `HardwareSource`/`AcquisitionTestContext` layer, which needs a full
  `Application`). The rest of the app depends only on the interface, so a
  second vendor's adapter can be added later without touching those
  layers; the base `devices` package imports with no vendor SDK
  installed. Design notes: structural protocols (not ABCs) so vendor
  adapters and test fakes satisfy the interface by shape; smallest
  interface that supports the Phase 2 viewer — exposure/settings modeling
  and synchronized multi-signal acquisition deferred to the phases that
  need them; the `(height, width)` scan convention is pinned empirically
  by a non-square scan in the integration tests.
- [x] Validate against `nionswift-usim` —
  [`tests/integration/test_nion_usim_adapter.py`](../tests/integration/test_nion_usim_adapter.py)
  (auto-skipped unless the `device` extra is installed); the
  `simulated_instrument()` context manager owns the both-cameras-closed
  teardown that the Phase 0 note warns about.
- [ ] Validate against real hardware.

**Phase 2 — Live viewer MVP**
- A napari + PySide6 shell with a dock widget (napari-micromanager-shaped)
  showing the live scan/camera feed and basic parameters (FOV, rotation,
  dwell time) — the core "look at the sample, adjust settings" loop.
- Benchmark live frame latency against real scan rates; fall back to `ndv`
  or a custom VisPy canvas if napari's per-frame overhead is too high.

**Phase 3 — Acquisition and storage**
- Implement acquisition sequences (single frame, spectrum image, tilt
  series) writing to HDF5/Zarr with NXem metadata via `pynxtools-em`.
- Build a one-time importer for existing Swift libraries/`.ndata` files
  using Nion's own `niondata`/IO code plus RosettaSciIO as the target-side
  writer, so existing data isn't orphaned by the migration.

**Phase 4 — Analysis integration**
- Wire HyperSpy / py4DSTEM / LiberTEM in as napari plugins or menu actions
  operating on the new file format.
- Port only the handful of Swift-specific analyses (if any) that aren't
  already covered upstream, as small adapter functions — not reimplementations.

**Phase 5 — Parity and cutover**
- Audit which Swift features the team actually uses day to day (not the
  full feature surface) and build a parity checklist from that.
- Pilot the new app in parallel with Swift on one instrument before cutover.

## 6. Open questions

- **License**: Nion's device-layer packages are GPL-3.0. Depending on how
  tightly the new app links against them, the whole application likely
  needs to be GPL-3.0-compatible too. Worth confirming this is acceptable
  before committing to reusing that code directly (vs. isolating it behind
  a process boundary, e.g. a small RPC service, if a more permissive license
  is required for the rest of the app).
- **Bluesky/ophyd**: the [Bluesky](https://blueskyproject.io/) experiment
  orchestration framework (device abstraction via `ophyd`/`ophyd-async`,
  scripted acquisition via a `RunEngine`) is a real, actively developed
  option for the device/acquisition layer, used across synchrotron
  facilities. It's not currently used for electron microscopy, and it's
  script-first rather than live-tuning-first, which doesn't match how STEM
  operators actually work moment to moment. Recommendation: skip it for v1
  (Phases 1–2), and revisit only if/when scripted multi-step acquisitions
  (automated tilt series, autotuning) become a priority.

## 7. Summary

Beyond the device layer, almost nothing here needs to be built from
scratch: napari + PySide6 for the shell and rendering, HDF5/Zarr + NeXus/NXem
+ RosettaSciIO for storage and I/O, and HyperSpy/py4DSTEM/LiberTEM for
analysis. The actual new code this project needs to write is the device
bridge (Phase 1), the live-viewer dock widget (Phase 2), the acquisition
sequencer and legacy-data importer (Phase 3), and plugin wiring (Phase 4) —
glue, as intended.
