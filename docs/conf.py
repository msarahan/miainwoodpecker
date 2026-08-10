# Below we grab the current date and time using python
# this is used to them support the copyright date always being the current year
# when you build your docs
import subprocess
from datetime import datetime

current_year = datetime.now().year
organization_name = "SuperSTEM"

project = "miainwoodpecker"
copyright = f"{current_year}, {organization_name}"
author = "SuperSTEM"

# *********** RELEASE NUMBER **************
# This is optional - if you want the release of your docs to align with your
# package release cycle then the code below will get the recent tag and use
# that to generate your documentation release value.
try:
    release_value = (
        subprocess.check_output(["git", "describe", "--tags"])
        .decode("utf-8")
        .strip()
    )
    release_value = release_value[:4]
except subprocess.CalledProcessError:
    release_value = "0.1"  # Default value in case there's no tag

# Update the release value
release = release_value

# -- General configuration ---------------------------------------------------
# Extensions add additional functionality to your documentation.
# TODO: describe each extension below
extensions = [
    # Autodoc will create API docs for you -
    "autoapi.extension",
    # Converts numpy-style docstring sections into proper RST. Without this,
    # autoapi passes docstrings through verbatim: the Parameters/Returns
    # sections do not render as sections, and "**kwargs" is parsed as an
    # unterminated strong-emphasis marker (which fails the -W docs build).
    "sphinx.ext.napoleon",
    "sphinx_design",
    "sphinx_copybutton",
    "sphinx.ext.intersphinx",
    # This allows you to create :::{todo} sections that will not be rendered
    # in the live docs
    "sphinx.ext.todo",
    "myst_parser",
]


# Render docstring "Attributes" sections as :ivar: fields instead of separate
# attribute directives. Without this, napoleon documents each dataclass
# attribute once from the docstring and autoapi documents it again from the
# source, which the -W build rejects as a duplicate object description.
napoleon_use_ivar = True

# Re-exporting names (e.g. Camera from both miainwoodpecker.devices and
# .devices.interface) gives autoapi two valid targets for the same object, so
# every type reference to one is reported as ambiguous. The duplication is
# intentional API design and both targets are correct, so this specific
# category is suppressed rather than warned on.
suppress_warnings = ["ref.python"]

templates_path = ["_templates"]
exclude_patterns = ["_build", "Thumbs.db", ".DS_Store"]

# ****** setup MYST ******
# colon fence for card support in md
myst_enable_extensions = [
    "colon_fence",
    "deflist",
    "attrs_block",
]
myst_heading_anchors = 3
myst_footnote_transition = False

# -- Options for HTML output -------------------------------------------------
# https://www.sphinx-doc.org/en/master/usage/configuration.html#options-for-html-output

html_theme = "pydata_sphinx_theme"
html_static_path = ["_static"]

# Configure autoapi
autoapi_dirs = ["../src"]
