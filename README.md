# miainwoodpecker

## What does this project do?

This is a Nion Swift replacement: instrument control and data analysis for
scanning transmission electron microscopes, built as a thin glue layer over
existing open source projects rather than a from-scratch rewrite. See
[`docs/migration-plan.md`](docs/migration-plan.md) for the architecture and
phased migration plan.

The project is early — most of what's here today is packaging, linting,
testing, and documentation scaffolding rather than application code.

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
