# miainwoodpecker

## What's in the name?

This project name goes back to my (@msarahan) time first at SuperSTEM, working
with Iain Godfrey on some instrument control electronics. It is a play on "Nion"
which is a portmanteau of Niklas Delby and Ondrej Krivanek, founders of that
microscope company (acquired by Bruker). Nion produced a software suite they
called "Swift" around 2013. I worked at Nion around 2014-2015, with some 
contribution to Swift. In an effort to avoid confusion and/or legal issues
with Apple's Swift programming language, Nion renamed the project to "nionswift."
Working on/with nionswift often felt like banging one's head against a tree.
I am told by current microscopists who use nionswift that the experience
hasn't changed much. It so happens that there's a bird (not a swift) that
bashes its head against trees, in search of bugs. Here we are. This history is my
(Michael Sarahan) personal take, not the official stance or sentiment of anyone
else. If this project is even remotely successful, it might need a less tongue-in-cheek name.

## What does this project do?

This is a Nion Swift replacement: instrument control and data analysis for
scanning transmission electron microscopes, built as a thin glue layer over
existing open source projects rather than a from-scratch rewrite. See
[`docs/migration-plan.md`](docs/migration-plan.md) for the architecture and
phased migration plan.

The project is early. What exists today:

* a **vendor-neutral device interface** (`miainwoodpecker.devices`), with a
  Nion backend validated against the `nionswift-usim` microscope simulator
  and run as an isolated subprocess (see "A note on licensing" below),
* a **live acquisition loop** (`miainwoodpecker.acquisition`) that decouples
  acquisition rate from display rate, and
* a **live viewer** (`miainwoodpecker.viewer`) — a napari + PySide6 dock
  widget with the live scan/camera feed and scan controls,
* a **broker** (`miainwoodpecker.broker`) that serves one instrument to
  every client at once — a window, a notebook, a dashboard — arbitrating
  who drives, with a **system-tray application**
  (`miainwoodpecker.tray`) that holds one open on a control computer:
  right-click for a window on it, for how each device server is doing,
  or to stop the lot and park the column,
* **acquisition sequences** (`miainwoodpecker.acquisition`) that stream to
  disk as they run, and
* **NeXus/HDF5 storage** (`miainwoodpecker.storage`), including an importer
  for legacy Nion Swift `.ndata` files.

### Try it without a microscope

With [pixi](https://pixi.sh) installed, from a fresh clone:

```shell
pixi run preview
```

That opens the viewer against a **synthetic instrument** living in this
process — a scan unit, a Ronchigram camera and an EEL spectrometer, no
hardware, no vendor SDK and no device server. It is enough to acquire a
scan, a 4D-STEM dataset and an EELS spectrum image, and to see them
written as NeXus files. The numbers are invented and say so on the
panel's face.

`pixi run` installs what it needs first, so there is no separate setup
step, and it resolves nothing: `pixi.lock` is committed, so you get the
versions this was tested against. See
[`docs/developing-the-ui.md`](docs/developing-the-ui.md) for what the
preview can and cannot show you.

### Replay a real session

`devices/replay.py` opens a recorded DigitalMicrograph spectrum-image
session and serves it as a device — one beam position at a time, waiting
the dwell the instrument waited:

```shell
pixi run -e replay replay /path/to/session --list
```

That lists the acquisitions it found; without `--list` it opens the
viewer against one and replays it. The data is real and was acquired
elsewhere, so every frame and every spectrum carries the `replay`
backend name and the file it came from.

### Run the live viewer

Against the `nionswift-usim` microscope simulator, which needs the
GPL-3.0 device stack (see "A note on licensing" below — it lives in its
own environment for exactly that reason):

```shell
pixi run -e device viewer
```

The equivalents without pixi, if you already have an environment:

```shell
uv run --extra device --extra viewer miainwoodpecker-viewer
uv run --extra viewer miainwoodpecker-preview
```

### Use the device layer directly

```python
from miainwoodpecker.devices import ScanParameters
from miainwoodpecker.devices.remote import remote_simulated_instrument

# requires the "device" extra: pip install miainwoodpecker[device]
with remote_simulated_instrument() as microscope:
    camera = microscope.ronchigram_camera
    camera.start()
    try:
        frame = camera.acquire_frame()  # frame.data is a 2D numpy array
    finally:
        camera.stop()

    scan = microscope.scanner.scan_frame(
        ScanParameters(height=256, width=256, pixel_time_us=1.0, fov_nm=15.0)
    )
```

Everything above the device layer depends only on the protocols in
`miainwoodpecker.devices`, never on a vendor SDK, so other vendors can be
added later as new adapters.

`remote_simulated_instrument()` spawns a subprocess and talks to it over
IPC — that's deliberate, not incidental complexity; see "A note on
licensing" below. Driving the underlying device logic in-process instead
(useful for debugging, but importing GPL-3.0 code — never do this in the
shipped application) looks the same, one module over:

```python
from miainwoodpecker.devices.nion_server import simulated_instrument

with simulated_instrument() as microscope:
    ...  # identical API, no subprocess, no IPC
```

### Record a series to NeXus HDF5

```python
from miainwoodpecker.acquisition import record, scan_series
from miainwoodpecker.devices import ScanParameters
from miainwoodpecker.devices.remote import remote_simulated_instrument

with remote_simulated_instrument() as microscope:
    parameters = ScanParameters(
        height=256, width=256, pixel_time_us=1.0, fov_nm=15.0
    )
    # Streams to disk frame by frame rather than buffering in memory.
    record(scan_series(microscope.scanner, parameters, 10), "series.nxs")
```

The result is a standard NeXus file — any NeXus-aware tool can plot it,
with spatial axes calibrated in nanometres. To migrate an existing Swift
library:

```python
from miainwoodpecker.storage import write_frames
from miainwoodpecker.storage.legacy import iter_ndata_directory

write_frames("migrated.nxs", iter_ndata_directory("old_swift_library/"))
```

## How to install

### With pixi (recommended, and what the microscope PCs use)

Install [pixi](https://pixi.sh/latest/#installation), clone, and run:

```shell
pixi run preview
```

There is no separate install step — `pixi run` creates the environment
first. `pixi.lock` is committed and covers Windows, Linux and macOS, so
nothing is resolved on your machine: you get the versions this was
tested against.

This is the recommended path on **Windows**, which is what the
instruments run, and the recommendation is not a preference. The viewer
needs Qt and a GL canvas, storage needs HDF5, and the analysis extras
need the scientific stack — precisely the packages where pip wheels are
least reliable on Windows and conda-forge builds are most. A microscope
PC is also the last machine on which you want to discover that something
needs a compiler.

The environments, and what each is for:

| Command | What you get |
|---|---|
| `pixi run preview` | The viewer against a synthetic instrument. No hardware, no vendor SDK. |
| `pixi run test` | The unit suite. Needs no display. |
| `pixi run -e device viewer` | The viewer against the `nionswift-usim` simulator. |
| `pixi run instrument` | The same simulator, served to *everything at once*: a broker in the `device` environment, and a window on it in an environment without the vendor stack. |
| `pixi run dashboard` | The same, with the browser dashboard as the front end instead of the window. |
| `pixi run serve` | The instrument and nothing else, held open until Ctrl-C, for windows and notebooks to attach to and leave as they like. |
| `pixi run -e replay replay <dir> --list` | What a recorded session directory holds; without `--list`, replays one. |
| `pixi run -e analysis test-all` | Everything, with HyperSpy/eXSpy available. |
| `pixi run -e style lint` | Ruff, without solving the viewer's Qt stack first. |

`pixi run` with no task name lists them all.

The GPL-3.0 device stack lives in its own `device` environment on
purpose; see "A note on licensing" below.

### With pip or uv

If you already manage your own environment:

```shell
pip install miainwoodpecker
```

```shell
uv pip install miainwoodpecker
```

The optional extras are `viewer`, `device`, `analysis`, `camera` and
`compression` — `pip install "miainwoodpecker[viewer]"`. Note that the
`device` extra is GPL-3.0.

Or just run `uv run python` in the directory where the package lives and it
will install it automatically into the chosen uv venv.

## Development

Development documentation can be found in the [DEVELOPMENT.md](DEVELOPMENT.md) file.

## License

`miainwoodpecker` is distributed under the terms of the [MIT](https://spdx.org/licenses/MIT.html) license.

### A note on licensing

Nion's own device stack (`nionswift-usim`, `nionswift-instrumentation`,
`nionswift`, and friends — the `device` extra) is GPL-3.0. This project
never imports it into its own process. `miainwoodpecker.devices.nion_server`
is a separate GPL-3.0 program (it says so in its own module header) that
only ever runs as a subprocess, launched via
`python -m miainwoodpecker.devices.nion_server`; the rest of this
project — including the shipped `miainwoodpecker-viewer` entry point —
talks to it only through the plain-data message protocol in
`miainwoodpecker.devices.rpc`, never by importing it directly. See
[`docs/migration-plan.md`](docs/migration-plan.md), §6, for the reasoning.
