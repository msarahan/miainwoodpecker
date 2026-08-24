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
| `--cameras 2` | Two cameras, each with its own section, live loop and viewing panel — a Ronchigram camera and an **EEL spectrometer**, because the second target is `eels_camera` and the name decides the detector. This is the shape to open for spectrum-image work. Also the cheapest way to see the viewing area tile three panels at once, with the scan. |
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

### Gathering a spectrum image

With `--cameras 2` the instrument has an EEL spectrometer on the
`eels_camera` target, and the window can acquire a real spectrum image
against it:

```shell
uv run --extra viewer miainwoodpecker-preview --cameras 2 --session /tmp/scratch
```

1. In the EELS camera's section, set **Detector readout** to
   `projected`. This configures the device immediately, unlike the
   exposure and binning beside it — readout decides the rank of every
   frame the detector produces. (Try it on the Ronchigram camera too:
   it refuses, with a sentence, because it has no dispersive direction.)
2. In the Scan group, set **Per-position detector** to `eels_camera` and
   choose a **Positions** count.
3. Click **Acquire spectrum image**.

One traversal of the probe follows, with a spectrum kept at every beam
position and every scan channel read out of the same pass. The status
line names what actually landed — leave the spectrometer imaging and you
get a 4D stack instead, which is a real experiment rather than a
mistake, and the line says so.

**Two panels open while it runs**, and they answer different questions.
`Acquiring (eels_camera)` is the virtual-detector map — one number per
beam position, which is where drift, contamination and vacuum show up.
`Acquiring (eels_camera): spectrum` is the spectrum at the position the
probe is on, captioned with that position, which is where "the
spectrometer is not on the loss I set it to" shows up. A map cannot say
that: a spectrometer parked off the edge sums to a perfectly plausible
number at every pixel.

You can also just start the spectrometer with **Detector readout** on
`projected` and no pass at all. Its panel is then a plot rather than a
picture, which is what a 1D readout is; see "A spectrum is a plot"
below.

**What the spectrum contains, and why each part is there.** A zero-loss
peak at 0 eV, silicon and amorphous-carbon plasmons, the power-law
background every EELS quantification fits and subtracts, and the
silicon L<sub>2,3</sub> (99.8 eV) and carbon K (284.2 eV) edges. The
energy axis is `nionswift-usim`'s — 0.5 eV per channel, channel 0 at
−20 eV — so a spectrum from here is directly comparable with one from
the simulator.

The point is that something computed from it has a **right answer**: the
two edge heights are complementary across the specimen, so a silicon map
integrated above 99.8 eV rises and falls with the HAADF channel of the
same pass. An analysis run against a cube of one repeated spectrum would
"succeed" and prove nothing, which is what this exists to prevent.

Driving the **Energy offset** control moves the zero-loss peak across the
detector's *channels* while the calibrated axis moves with it, so the
peak stays at 0 eV. That is what makes
`acquisition.sequence.energy_offset_series` demonstrable here rather than
merely runnable.

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

### The viewing area is many napari viewers, not one

`viewer/documents.py` gives every dataset its own `napari.Viewer` inside
a `QMdiArea` sub-window, which is what makes zoom, pan and contrast
per-panel. Two consequences are worth knowing before editing it.

**It reparents `viewer.window._qt_window`, which is private napari
API.** Nothing else exposes the Qt widget behind a viewer, and the
alternative is not a tidier accessor — it is giving up per-panel zoom
and napari's own layer controls. The reach-ins are confined to that one
module (ruff's `SLF001` is silenced there and nowhere else) and each is
wrapped, so a napari upgrade that renames a dock leaves the chrome
showing rather than failing to open a window.

**`LiveInstrumentWidget` does not know any of this exists.**
`DocumentBoard` presents the slice of the `napari.Viewer` API the widget
uses — `add_image`, `add_shapes`, and membership, lookup and deletion on
`layers` — and routes each call to the right document. So a plain
`napari.Viewer` still works wherever the board does, which is why every
widget test constructs one directly and why a single-canvas window
remains a supported way to run the application.

### A spectrum is a plot, and napari has no plot

`viewer/plots.py` is a pyqtgraph curve in a `QWidget`, and
`documents.PanelDocument` is what puts a plain widget into the same MDI
area the napari viewers live in. A projecting detector's readout goes
there instead of into an image layer, decided by the **rank of the
array** and nothing else — a camera's readout mode can change between
one frame and the next, so the shape is the fact and the label would be
a guess.

Before this existed, a 1D frame did not merely display badly: it could
not be displayed at all. `axes.frame_calibration` unpacks a height and a
width from `data.shape[-2:]`, and a spectrum has one axis, so putting a
spectrometer into `projected` and starting it raised `ValueError` out of
the display timer. `axes.spectrum_axis` is the 1D answer to the same
question, and it reports anything that is not an energy axis as bare
channels rather than labelling counts with a ruler they were not
measured against.

Two things worth knowing before editing it:

- **The colours come from the Qt palette**, not from a constant here.
  napari sets that palette from its theme for the whole application, so
  the curve and axes follow light or dark without this module knowing
  napari exists.
- **The plot is a document like any other.** Tiling, closing, raising
  and the View menu all work on it, which is why `DocumentArea` holds
  `Document | PanelDocument` and the View menu dispatches by method
  *name*: a plot has no "actual resolution" and answers by doing
  nothing, rather than by the menu changing as panels come and go.

**Qt does not promise when move, resize and zoom events arrive**, and
this module reads all three to tell the operator's actions from its own.
Both latches — "has the operator arranged the windows" and "has the
operator zoomed this panel" — therefore guard on *both* an in-progress
flag and the value the application last set. Either guard alone was
tried and neither is sufficient: the flag misses queued events, and the
value alone misses synchronous ones. `test_documents.py` pins both
cases.

The preview's own tests split along the same line. The devices are
ordinary objects, so `tests/unit/test_viewer_preview.py` needs no
display and runs in about two seconds;
`tests/integration/test_preview_window.py` opens real windows and needs
one.
