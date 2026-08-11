"""
Adapter: read a NexusWriter file as a LiberTEM ``DataSet``.

The migration plan's Phase 4 note (docs/migration-plan.md, §5) originally
deferred LiberTEM (and py4DSTEM) in favor of HyperSpy, reasoning that both
target 4D-STEM (scan-position x diffraction-pattern) data specifically,
and this app's device interface has no synchronized scan/camera
acquisition mode yet (``devices/interface.py``'s ``Scanner`` docstring) —
so there is no 4D-STEM datacube for either to operate on.

That reasoning holds for py4DSTEM, whose core object (``DataCube``) is
built around a 4D ``(scan_y, scan_x, det_y, det_x)`` array. It does
**not** hold for LiberTEM: LiberTEM's core abstraction is a ``DataSet``
with an arbitrary-rank "navigation" shape processed by user-defined
functions (UDFs), and its HDF5 ``DataSet`` reader infers that navigation
shape directly from whatever leading dimensions the array actually has.
Verified directly against a real file written by
:func:`~miainwoodpecker.storage.nexus.write_frames` (shape
``(n_frames, height, width)``, i.e. a plain frame stack, exactly what
``camera_series``/``scan_series`` produce today): ``libertem.io.dataset.
hdf5.H5DataSet`` infers a genuinely **one-dimensional** navigation shape,
``(n_frames,)``, not a padded/reshaped 2-tuple, and ``Context.run_udf``
runs real built-in UDFs (``SumUDF``, ``StdDevUDF``) against it without
complaint. So a genuine, non-synthetic LiberTEM PoC is possible today,
on the same data this app already produces — no 4D-STEM acquisition
mode required.

This module is the genuine gap-filling half of that PoC: our NeXus
layout is our own convention over HDF5 (see
:mod:`miainwoodpecker.storage.nexus`), not one of the formats LiberTEM's
built-in loaders already recognize by name, so pointing LiberTEM's own
HDF5 reader at the right dataset path is the one thing this app needs to
supply. Unlike :mod:`miainwoodpecker.analysis.hyperspy_bridge`, this
adapter does not read the array with ``h5py`` itself and hand it to the
library — ``Context.load("hdf5", ...)`` already does that internally.
The only genuinely new code here is validating the file has frames to
read (mirroring the HyperSpy adapter's own check, and giving a clearer
error than LiberTEM's own "unable to infer dataset" message would) and
naming the dataset path our writer actually uses.

**A real, honest limitation, and it did not change when the calibration
model arrived.** The per-axis calibration work that gave camera frames real
physical axes (:mod:`miainwoodpecker.storage.calibration`) reached the
HyperSpy and py4DSTEM adapters; there is still nothing here to hand it to,
re-checked against LiberTEM 0.16 rather than carried over as a claim.
``DataSetMeta.__init__`` takes ``shape``, ``array_backends``,
``image_count``, ``raw_dtype``, ``dtype``, ``metadata``, and
``sync_offset`` — nothing like ``AxesManager``'s per-axis
``scale``/``offset``/``units``, and ``shape`` is a
``libertem.common.Shape`` of plain integer extents. The one candidate,
that free-form ``metadata`` passthrough, is not reachable from here
anyway: ``H5DataSet.__init__`` has no ``metadata`` parameter, so
``Context.load("hdf5", ...)`` cannot set it — and an untyped blob nothing
in LiberTEM reads would be a place to *put* calibration, not a model that
*uses* it, which is the distinction this note is about. A ``DataSet``'s
frames stay addressed by integer navigation/signal indices.

So this adapter deliberately attempts no axis-calibration round trip. A
caller who needs the calibration alongside a UDF result reads it from the
same file with
:func:`miainwoodpecker.storage.nexus.read_calibration`, which returns the
same :class:`~miainwoodpecker.storage.calibration.FrameCalibration` the
HyperSpy adapter transfers onto an ``AxesManager``. This is a genuine
difference between the two libraries' object models, not a gap in this
adapter — and
``tests/integration/test_libertem_bridge.py`` asserts it as a canary, so if
LiberTEM ever grows a per-axis calibration field the test fails and this
adapter can start using it instead of this paragraph aging quietly.

**Also investigated and found not to apply here**: a stronger
demonstration would run LiberTEM against a real, published 4D-STEM
dataset (genuine 2D navigation) rather than this app's 1D-navigation
frame stack. LiberTEM's own documentation only lists such datasets
hosted on Zenodo (0.18-14.2 GB, DOIs under 10.5281/zenodo.*); py4DSTEM's
small sample-data registry hosts its files on Google Drive. Both hosts
returned a blocked ``CONNECT`` (HTTP 403) through this environment's
proxy, as did HuggingFace, OSF, and Figshare when tried as alternatives
— this environment's network policy allows package registries
(PyPI, npm, crates.io, the Go proxy) and GitHub, not general data
hosting. py4DSTEM's registry entry for its smallest nominal sample
(``small_datacube``) also turned out to be an unreliable candidate on
its own terms: its own source carries a ``TODO`` noting the ID currently
points at the same file as an unrelated fixture (``vac_probe``), pending
a replacement that was never made smaller. This is a network-reachability
finding about *this environment*, not a claim that no such dataset
exists or that LiberTEM needs one to be useful here.

Requires the ``libertem`` optional dependency group
(``pip install miainwoodpecker[libertem]``).
"""

from __future__ import annotations

import typing

from miainwoodpecker.storage import layout
from miainwoodpecker.storage.nexus import require_frames

if typing.TYPE_CHECKING:
    import os

    from libertem.api import Context
    from libertem.io.dataset.base import DataSet

_DATASET_PATH = layout.ABSOLUTE_NXDATA_DATA


def load_as_libertem_dataset(
    ctx: Context, path: os.PathLike[str] | str
) -> DataSet:
    """
    Read a NexusWriter file's frame stack as a LiberTEM ``DataSet``.

    The frame-stack dataset (``/entry/data/data``, shape
    ``(n_frames, height, width)``) becomes a ``DataSet`` with a
    one-dimensional navigation axis (frame index) and two signal axes
    (``height``, ``width``) — LiberTEM's own HDF5 reader infers this
    shape from the array itself; nothing here reimplements that.

    Parameters
    ----------
    ctx : Context
        An existing LiberTEM ``Context`` (owns the executor the
        ``DataSet`` will run UDFs on). Not created here: a ``Context``'s
        lifecycle — inline vs. dask executor, one per app run vs. one
        per call — is a caller concern, not a file-reading one.
    path : os.PathLike[str] | str
        An HDF5 file written by :class:`~miainwoodpecker.storage.nexus.NexusWriter`.

    Returns
    -------
    DataSet
        The frame stack as a LiberTEM ``DataSet``, ready for
        :meth:`Context.run_udf`.

    Raises
    ------
    NoFramesError
        If the file was written by an acquisition that produced no
        frames, so it has no ``NXdata`` group to read. Checked here
        rather than left to LiberTEM, whose own "unable to infer
        dataset" message does not say what is actually wrong.
    """
    require_frames(path)
    return ctx.load("hdf5", path=str(path), ds_path=_DATASET_PATH)
