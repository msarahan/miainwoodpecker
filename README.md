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
* **acquisition sequences** (`miainwoodpecker.acquisition`) that stream to
  disk as they run, and
* **NeXus/HDF5 storage** (`miainwoodpecker.storage`), including an importer
  for legacy Nion Swift `.ndata` files.

### Run the live viewer

```shell
# needs both extras, plus a display
uv run --extra device --extra viewer miainwoodpecker-viewer
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

You can install this package using either `pip` or `uv`. We recommend that
you create a new Python environment to work in when installing this
package. Use whatever environment manager you wish!

To install the package using pip:

```shell
pip install miainwoodpecker
```

To install the package using uv:

```shell
uv pip install miainwoodpecker
```

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
