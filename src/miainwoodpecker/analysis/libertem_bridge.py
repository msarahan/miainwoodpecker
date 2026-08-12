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

Reading a file, and not reading one
-----------------------------------
:func:`load_as_libertem_dataset` points LiberTEM's own HDF5 reader at the
file. :func:`libertem_dataset_from_frames` is for the caller who already
holds the frames — the viewer, which read them to display them — and wraps
them with ``ctx.load("memory", ...)``, LiberTEM's ``MemoryDataSet``, so a
2048x2048 recording is not decompressed twice to draw it once and analyze
it once.

These are two genuinely different datasets rather than one with two
constructors, and the difference is worth stating rather than hiding
behind a union-typed parameter. ``MemoryDataSet``'s own docstring says it
"is not recommended for production use since it performs poorly with a
distributed executor" — which is a statement about *where the array lives*,
not a defect: with a dask executor each worker can open the file itself,
while an in-memory dataset has to ship the array to them. Under the inline
executor the viewer uses (one small burst, one UDF run) there is nothing to
ship and nothing to lose, so the choice between the two entry points is the
same question as the choice of executor, and the caller is the one holding
both answers.

Measured against LiberTEM 0.16 rather than assumed, because the shape
rules are not obvious: ``ctx.load("memory", data=stack, sig_dims=2)`` on a
``(n_frames, height, width)`` array infers navigation shape
``(n_frames,)`` and signal shape ``(height, width)`` — byte for byte the
shape ``H5DataSet`` infers from the same recording, which is what makes the
two entry points interchangeable. ``sig_dims=2`` is passed explicitly
because the same call on a *2D* array silently yields an empty navigation
shape with the frame itself as signal; a single frame handed over as
``(height, width)`` would therefore become a dataset with no frames to
navigate rather than an error, so this adapter requires the 3D stack a
recording always is.

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

from miainwoodpecker.analysis.threads import analysis_thread_count
from miainwoodpecker.storage import layout
from miainwoodpecker.storage.nexus import require_frames

if typing.TYPE_CHECKING:
    import os

    from libertem.api import Context
    from libertem.io.dataset.base import DataSet

    from miainwoodpecker.storage.nexus import FrameStack

_DATASET_PATH = layout.ABSOLUTE_NXDATA_DATA
# A frame stack is (n_frames, height, width): one navigation dimension and
# two signal ones. Stated as a constant because both numbers are load
# bearing - see the module docstring for what a 2D array does instead.
_STACK_RANK = 3
_SIGNAL_DIMENSIONS = 2
_UNNAMED_SOURCE = "these frames"


def analysis_context(cpu_count: int | None = None) -> Context:
    """
    Return a ``Context`` whose threads leave room for the GUI thread.

    The executor choice is unchanged and deliberate: an inline executor,
    because a button click here is one UDF over one small, already
    in-memory burst, and standing up a local dask cluster per click would
    be pure overhead. What changes is the number under it. **"Inline"
    bounds the executor, not the numerics**: ``InlineJobExecutor`` with no
    ``inline_threads`` asks for ``psutil.cpu_count(logical=False)``
    fine-grained threads and applies that to numba, pyfftw and the BLAS
    pools around every partition it processes
    (``libertem.common.threading.set_num_threads``), which on an idle
    machine is every physical core — the thing
    :mod:`miainwoodpecker.analysis.threads` exists to stop.

    This is LiberTEM's own knob rather than an environment variable, which
    is why the LiberTEM path gets a factory of its own instead of relying
    on the process-wide limit
    :func:`~miainwoodpecker.analysis.threads.limit_analysis_threads`
    applies: numba's pool is not one ``threadpoolctl`` can reach, and
    LiberTEM already sets it correctly when told a worker count.

    Parameters
    ----------
    cpu_count : int | None
        Cores to size the cap against; defaults to the machine's. See
        :func:`~miainwoodpecker.analysis.threads.analysis_thread_count`.

    Returns
    -------
    Context
        A LiberTEM ``Context``, ready for :func:`load_as_libertem_dataset`
        and :meth:`Context.run_udf`. It is a context manager and owns an
        executor, so close it (``with analysis_context() as ctx:``).
    """
    from libertem.api import Context  # noqa: PLC0415
    from libertem.executor.inline import InlineJobExecutor  # noqa: PLC0415

    return Context(
        executor=InlineJobExecutor(inline_threads=analysis_thread_count(cpu_count))
    )


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
        per call — is a caller concern, not a file-reading one. This
        app's own callers use :func:`analysis_context`, which makes the
        one this project's GUI can afford to share a machine with.
    path : os.PathLike[str] | str
        An HDF5 file written by :class:`~miainwoodpecker.storage.nexus.NexusWriter`.

    Returns
    -------
    DataSet
        The frame stack as a LiberTEM ``DataSet``, ready for
        :meth:`Context.run_udf`.

    Notes
    -----
    Raises :class:`~miainwoodpecker.storage.layout.NoFramesError` (via
    :func:`~miainwoodpecker.storage.nexus.require_frames`) when the file
    recorded no frames. Checked here rather than left to LiberTEM, whose
    own "unable to infer dataset" message does not say what is wrong.

    Reads the file, through LiberTEM's own reader rather than this
    adapter's. :func:`libertem_dataset_from_frames` is the entry point for
    a caller that already holds the frames.
    """
    require_frames(path)
    return ctx.load("hdf5", path=str(path), ds_path=_DATASET_PATH)


def libertem_dataset_from_frames(
    ctx: Context,
    frames: FrameStack,
    *,
    source: str = _UNNAMED_SOURCE,
) -> DataSet:
    """
    Wrap frames already in memory as a LiberTEM ``DataSet``.

    ``ctx.load("memory", ...)``, i.e. LiberTEM's own ``MemoryDataSet``,
    with the navigation/signal split stated explicitly rather than
    inferred. The resulting shape is the same one
    :func:`load_as_libertem_dataset` gets from the same recording, so a UDF
    cannot tell which entry point produced its input — see the module
    docstring for the measurement, and for when the file-reading form is
    still the right one (a distributed executor).

    The calibration in ``frames`` is ignored here, and that is not an
    oversight: LiberTEM models no per-axis calibration at all, which the
    module docstring documents and
    ``tests/integration/test_libertem_bridge.py`` asserts as a canary. It
    is still carried in the parameter rather than stripped at the call
    site, so this adapter takes the same input as its two siblings and a
    caller who reads the calibration out of ``frames`` for its own use has
    it to hand.

    Parameters
    ----------
    ctx : Context
        An existing LiberTEM ``Context``, as for
        :func:`load_as_libertem_dataset` — the executor's lifecycle is a
        caller concern.
    frames : FrameStack
        The frames to wrap, ``(n_frames, height, width)``.
    source : str
        What to call these frames in an error message; the file-reading
        callers pass their path, since the arrays carry no provenance.

    Returns
    -------
    DataSet
        The frame stack as a LiberTEM ``DataSet``, ready for
        :meth:`Context.run_udf`.

    Raises
    ------
    ValueError
        If the stack is not three-dimensional. LiberTEM would not refuse a
        2D array here — it would build a dataset with an empty navigation
        shape — so a single frame passed as ``(height, width)`` is caught
        with a sentence rather than analyzed as something it is not.
    """
    data = frames.data
    if data.ndim != _STACK_RANK:
        msg = (
            f"{source} is a {data.ndim}D array of shape {data.shape}; a "
            f"LiberTEM DataSet is built here from the "
            f"(n_frames, height, width) stack a recording always is. Pass a "
            f"single frame as data[np.newaxis] rather than on its own, which "
            f"LiberTEM would accept as a dataset with no frames to navigate."
        )
        raise ValueError(msg)
    return ctx.load("memory", data=data, sig_dims=_SIGNAL_DIMENSIONS)
