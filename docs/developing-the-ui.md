# Developing the UI

This is the guide for working on the viewer's own window — the panels,
the controls, the layout — as opposed to running it at a microscope
(that is [Using the viewer](using-the-viewer.md)).

## The preview instrument

```shell
uv run --extra viewer miainwoodpecker-preview
```

That opens the real viewer window against a synthetic instrument living
in the viewer's own process. It needs the `viewer` extra and nothing
else: no vendor SDK, no `device` extra, no device server.

The **Backend** line at the top of the Instrument panel reads `preview`,
never `simulated`. That distinction is deliberate and worth keeping: a
screenshot taken from this window can never be mistaken for one taken
from the microscope simulator, let alone from an instrument. Everything
on screen is invented.

### Why this exists next to `camera_server`

The viewer can already open against synthesised frames:

```shell
uv run --extra camera --extra viewer miainwoodpecker-viewer \
    --backend simulated --server-module miainwoodpecker.devices.camera_server
```

That path is the honest end-to-end exercise — frames cross a real socket
from a real subprocess, so it proves the IPC, the handshake and the
shutdown — and it is the right thing to run before believing a change
works. The preview deliberately gives all of that up in exchange for
three things it cannot offer:

* **Startup is an import.** No server to spawn, no port to bind, no
  handshake to wait out.
* **The window is reachable in any shape** (see below). Scan-only,
  camera-only, two cameras, an instrument publishing one control out of
  four — otherwise reachable only by owning the matching hardware.
* **A failure is the viewer's.** With no transport underneath, a widget
  that misbehaves here misbehaves in code you just edited.

Use the preview to iterate; use `camera_server` to confirm.

### Opening the window in a particular shape

| Option | The case it opens |
|---|---|
| *(none)* | Scan unit, one camera, all four instrument controls. |
| `--no-scan` | A detector-only instrument. The window has no Scan group — the absent device is missing, not present and broken. |
| `--no-camera` | A scan-only instrument. |
| `--cameras 2` | Two cameras, each with its own section, live loop and napari layer. |
| `--controls defocus,beam_blanker` | An instrument publishing only those controls. The others get no row at all. |
| `--session DIR` | Where recordings go. Point it at a scratch directory. |

`--controls` accepts any comma-separated subset of `defocus`,
`energy_offset`, `stage_position`, `beam_blanker`, and rejects anything
else rather than silently dropping it — a typo would otherwise produce a
missing row that looks exactly like the feature under test working.

### The controls are wired to the image

Blanking the beam collapses the signal, defocus damps the contrast, and
moving the stage moves the field of view. This is the point rather than
a flourish: a preview whose dials did nothing would let a broken
Instrument panel — a signal never connected, a setter called on the
wrong object — look exactly like a working one.

The specimen is an atomic-scale square lattice (0.3 nm columns, so
roughly fifty across the panel's default 15 nm field of view) and the
camera shows a ringed Ronchigram. Both are shaped to be *representative*
rather than pretty: judging a colormap, an autocontrast pass, or a
contrast-limits slider against two smooth grey blobs proves nothing.

Frames are seeded, so a preview session is reproducible and a screenshot
can be retaken.

### What it is not

The numbers are invented and are not measurements of anything.
Recordings made here are real NeXus files full of synthetic data, so
point `--session` at a scratch directory rather than one holding real
work. For a benchmark, use the real ones (`scripts/phase2_live_benchmark.py`,
`scripts/real_4dstem_benchmark.py`).

## Running the widget tests

```shell
uv run --extra tests --extra viewer python -m pytest tests/integration/test_live_widget.py
```

Napari's vispy canvas needs a real GL canvas, so these tests need a
display. On macOS and Windows that is the platform's own window server
and they simply run. On Linux they need X11 or Wayland, or a virtual
display:

```shell
xvfb-run -a uv run --extra device --extra viewer --extra tests pytest
```

`QT_QPA_PLATFORM=offscreen` is **not** a substitute: it gives no
`QOpenGLWidget`, napari's layer lifecycle breaks on teardown, and the
GPU rendering path napari was chosen for is never exercised.

The preview's own tests split along the same line. The devices are
ordinary objects, so `tests/unit/test_viewer_preview.py` needs no
display and runs in about two seconds;
`tests/integration/test_preview_window.py` opens real windows and needs
one.
