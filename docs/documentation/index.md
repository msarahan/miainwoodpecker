# Sphinx documentation

:::{toctree}
:maxdepth: 2
:hidden:
:caption: Contents:
:glob:

*

:::

This package uses Sphinx to create documentation.

We like sphinx because it is a community driven project with a community
driven theme that the scientific Python community works on:

`pydata_sphinx_theme`

:::{note}
MkDocs is the other most common documentation tool used in the Python
ecosystem.
:::

## Setting up documentation for a new project

Sphinx documentation for a new project can be scaffolded with
`sphinx-quickstart`, which lays out the `docs` directory and its Sphinx
config. For this package, you may also want to just copy this repository's
structure instead.

## About the conf.py file

The `conf.py` file is what Sphinx uses to configure your documentation. This is
where you setup all of the features that you want your documentation to have.

:::{note}
Every tool and theme that you might use will have different configuration options
that will be placed in the `conf.py` file.
:::

:::{note}
You can remove the `make.bat` and `Makefile` files included with sphinx build.
In this package demo we use [Hatch environments and scripts](hatch-envs-scripts).
:::

## API / Reference documentation

It's useful to have a reference section in your docs that contains documentation
for your package's methods and classes. In this demo package we are using
[`sphinx-autoapi`](https://sphinx-autoapi.readthedocs.io/en/latest/index.html)
which will create these reference docs for you automatically. It's easy
to setup and creates nice-looking API docs without any needed setup.

`Sphinx-autoapi` is also customizable if you want to display things
differently.
