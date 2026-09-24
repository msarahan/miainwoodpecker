# MIT License
#
# Copyright (c) 2026 Michael Sarahan
#
# Permission is hereby granted, free of charge, to any person obtaining a copy
# of this software and associated documentation files (the "Software"), to deal
# in the Software without restriction, including without limitation the rights
# to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
# copies of the Software, and to permit persons to whom the Software is
# furnished to do so, subject to the following conditions:
#
# The above copyright notice and this permission notice (including the next
# paragraph) shall be included in all copies or substantial portions of the
# Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
# AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
# LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
# OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
# SOFTWARE.

"""
Instrument control and data analysis for STEM.

Built as a thin glue layer over existing open source projects; see
docs/migration-plan.md for the architecture. The device layer lives in
:mod:`miainwoodpecker.devices`.
"""

try:
    # Written by hatch-vcs at build time, editable builds included, and
    # read from the file rather than from installed metadata on purpose:
    # every pixi environment of one checkout shares this one file, so the
    # broker in `device` and the window in `default` agree on what they
    # are even when their dist-info was built at different times.
    from miainwoodpecker._version import __version__
except ImportError:  # pragma: no cover - a source tree no build has touched
    __version__ = "0+unknown"
