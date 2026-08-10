# miainwoodpecker

## What does this project do?

This is a Nion Swift replacement: instrument control and data analysis for
scanning transmission electron microscopes, built as a thin glue layer over
existing open source projects rather than a from-scratch rewrite. See
[`docs/migration-plan.md`](docs/migration-plan.md) for the architecture and
phased migration plan.

The project is early. What exists today is the Phase 1 device bridge: a
vendor-neutral device interface (`miainwoodpecker.devices`) and a Nion
adapter validated against the `nionswift-usim` microscope simulator.

```python
from miainwoodpecker.devices import ScanParameters
from miainwoodpecker.devices.nion_adapter import simulated_instrument

# requires the "device" extra: pip install miainwoodpecker[device]
with simulated_instrument() as microscope:
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

### Linting & Code Formatting

All linting and code formatting is implemented in this package using a combination
of pre-commit hooks and Ruff. Ruff is a fast, rust-based linter and code
formatter that covers functionality previously implemented by Black and isort
(formatters that are commonly used in the Python ecosystem). Ruff simplifies
your linting and code format setup by running all of the checks and fixes
using a single tool.

## License

`miainwoodpecker` is distributed under the terms of the [MIT](https://spdx.org/licenses/MIT.html) license.
