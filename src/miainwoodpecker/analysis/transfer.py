"""
Wire vocabulary for talking to an analysis worker process.

The analysis-side counterpart of :mod:`miainwoodpecker.devices.rpc`, and
deliberately much smaller than it, because it does not have to define a
protocol: it *reuses* that one. ``Call``/``Result``, the dispatch rules in
:mod:`miainwoodpecker.devices.serving`, the reused-segment transport in
:mod:`miainwoodpecker.devices.shared_frame` and the ``TCP_NODELAY`` fix in
``rpc.disable_nagle`` are all the device layer's, unchanged. What is new
here is only what an analysis call carries that a device call never did.

Like ``rpc.py``, this module is the licence boundary for the analysis
layer: it is imported by the MIT client
(:mod:`miainwoodpecker.analysis.remote`) and by the worker
(:mod:`miainwoodpecker.analysis.worker`, which imports HyperSpy,
py4DSTEM or LiberTEM), and it imports neither of them nor anything they
depend on. See docs/analysis-isolation.md for what that does and — just
as importantly — does not settle.

What is different from a device call
------------------------------------
Three things, and each one is a design consequence rather than a detail.

**Bulk travels in both directions.** A device request is a method name and
a couple of numbers; only the reply is large. An analysis request carries
a whole frame stack — five 2048x2048 float32 Ronchigram frames is 84 MB —
and returns a projection that is itself a full frame. So each side owns a
:class:`~miainwoodpecker.devices.shared_frame.SharedFrameWriter` and a
:class:`~miainwoodpecker.devices.shared_frame.SharedFrameReader`, rather
than the device layer's writer-on-the-server, reader-on-the-client
arrangement. The reuse invariant is unchanged and holds for the same
reason: the protocol is strictly synchronous request/response, and the
viewer refuses a second analysis while one is running, so a segment is
never written while its other side is reading.

**A path is a payload too, and the cheaper one.** The viewer's analysis
input is a NeXus file it has just written, *or* frames it already holds
(:class:`~miainwoodpecker.storage.nexus.FrameStack`). When it is a file,
nothing needs to cross at all — the worker opens it, which is what the
in-process adapters do anyway. :class:`AnalysisSource` carries whichever
of the two the caller actually has, so the transport cost of the
fresh-burst case is zero rather than 84 MB.

**Small arrays stay on the pickle path.** The same
:data:`~miainwoodpecker.devices.rpc.SHARED_MEMORY_THRESHOLD_BYTES` the
device layer measured, applied for the same measured reason: below about
64 KB the two transports are within noise of each other, and a segment
resize is a syscall pair that is not free. A five-frame burst from a
simulated EELS camera is over it; a ``(64, 64)`` test fixture is not, and
that asymmetry is deliberate — the tests exercise both branches without
having to fabricate a large array.
"""

from __future__ import annotations

import typing
from dataclasses import dataclass

import numpy as np

from miainwoodpecker.analysis.operations import AnalysisInput
from miainwoodpecker.devices.rpc import SHARED_MEMORY_THRESHOLD_BYTES
from miainwoodpecker.devices.shared_frame import SharedArrayRef
from miainwoodpecker.storage.nexus import FrameStack

if typing.TYPE_CHECKING:
    import numpy.typing as npt

    from miainwoodpecker.devices.shared_frame import (
        SharedFrameReader,
        SharedFrameWriter,
    )
    from miainwoodpecker.storage.calibration import FrameCalibration

__all__ = [
    "ANALYSIS_TARGETS",
    "AnalysisSource",
    "AnalysisTarget",
    "publish_array",
    "publish_source",
    "read_array",
    "read_source",
]


@dataclass(frozen=True)
class AnalysisTarget:
    """
    One analysis library, as the isolation layer has to think about it.

    A target is a library rather than a device, and the granularity is not
    arbitrary: it is the granularity of *installation*. Each of these is
    its own optional extra because each resolves its own scientific stack
    (measured in ``pyproject.toml``: HyperSpy ~35 packages, py4DSTEM 65,
    LiberTEM ~102), and one worker process serving all three would put
    them back in one interpreter — reintroducing exactly the coupling the
    separate extras exist to prevent.

    Attributes
    ----------
    name : str
        The target name on the wire, and the argument
        ``python -m miainwoodpecker.analysis.worker`` takes.
    extra : str
        The optional dependency group that provides it, named exactly as
        the viewer's status labels already name it.
    probe_module : str
        A top-level module whose presence means the extra is installed.
        Probed with ``importlib.util.find_spec``, which resolves a
        top-level name without importing it — the distinction that makes
        an availability check cost nothing, against the 2.6 s (LiberTEM)
        to 5.2 s (py4DSTEM) a real import costs.
    eager_modules : tuple[str, ...]
        Exactly what the viewer's button handler used to import to decide
        whether its extra was installed, kept verbatim so the in-process
        path's behaviour — including which ``ImportError`` reaches which
        status label — is unchanged. Importing the *bridge* alone would
        not do for LiberTEM: ``libertem_bridge`` defers its own library
        imports, so it imports fine with no LiberTEM installed and the
        button would fail later with a different message.
    """

    name: str
    extra: str
    probe_module: str
    eager_modules: tuple[str, ...]


ANALYSIS_TARGETS: dict[str, AnalysisTarget] = {
    target.name: target
    for target in (
        AnalysisTarget(
            "hyperspy",
            "analysis",
            "hyperspy",
            ("miainwoodpecker.analysis.hyperspy_bridge",),
        ),
        AnalysisTarget(
            "libertem",
            "libertem",
            "libertem",
            ("libertem.udf.sum", "miainwoodpecker.analysis.libertem_bridge"),
        ),
        AnalysisTarget(
            "py4dstem",
            "py4dstem",
            "py4DSTEM",
            (
                "py4DSTEM.process.calibration",
                "miainwoodpecker.analysis.py4dstem_bridge",
            ),
        ),
    )
}
"""
Every analysis worker this project can start, by wire name.

Not :data:`~miainwoodpecker.devices.rpc.TARGET_NAMES`, and not appended
to it. That tuple names *device* targets, and an analysis worker is not
one: it binds exactly one port for exactly one target and needs no
agreement with a device server about names at all. When that tuple was
positional argv, sharing it would have meant a new analysis library
shifting a device server's command line; it no longer is, and the tuple
is still the wrong home — a coupling with no benefit on either side.
"""


@dataclass(frozen=True)
class AnalysisSource:
    """
    What an analysis is to run against: a file, or frames already in hand.

    The two cases are the same two the viewer already distinguishes
    (``LiveInstrumentWidget._analysis_input``), carried across the process
    boundary without collapsing them. Collapsing them would undo the thing
    they exist for: a freshly written burst is a file the worker can open
    itself, and an already-open recording's frames are in the client's
    memory precisely so the file is not read twice.

    Exactly one of ``path`` and ``stack`` is set.

    Attributes
    ----------
    path : str | None
        The NeXus file to open, when there is one.
    stack : SharedArrayRef | npt.NDArray[typing.Any] | None
        The ``(n_frames, height, width)`` block, either as a reference
        into the sender's shared segment or — below
        :data:`~miainwoodpecker.devices.rpc.SHARED_MEMORY_THRESHOLD_BYTES`
        — inline for the pickle channel to carry.
    frame_time : npt.NDArray[typing.Any] | None
        The stack's per-frame elapsed times. Always inline: one float per
        frame is bytes, not megabytes, and giving it a segment of its own
        would cost a second syscall pair to move a hundred bytes.
    calibration : FrameCalibration | None
        The stack's axis calibration, which travels with it for the reason
        :class:`~miainwoodpecker.storage.nexus.FrameStack` keeps the two
        together: an array that arrives without its axes produces a signal
        silently claiming bare pixels.
    origin : str | None
        What to call this input in an adapter's refusal message. Carried
        on the wire rather than reconstructed worker-side, because only
        the client knows whether these frames came from a named recording
        or from a fresh burst.
    """

    path: str | None = None
    stack: SharedArrayRef | npt.NDArray[typing.Any] | None = None
    frame_time: npt.NDArray[typing.Any] | None = None
    calibration: FrameCalibration | None = None
    origin: str | None = None


def publish_array(
    writer: SharedFrameWriter,
    array: npt.NDArray[typing.Any],
) -> SharedArrayRef | npt.NDArray[typing.Any]:
    """
    Route one array through shared memory, or leave it on the pickle path.

    Parameters
    ----------
    writer : SharedFrameWriter
        The sender's own writer. Each side of the analysis protocol owns
        one, unlike the device protocol where only the server writes.
    array : npt.NDArray[typing.Any]
        The array to send.

    Returns
    -------
    SharedArrayRef | npt.NDArray[typing.Any]
        A reference when the array is worth a segment, the array itself
        when it is not.
    """
    if array.nbytes < SHARED_MEMORY_THRESHOLD_BYTES:
        return array
    return writer.publish_array(array)


def read_array(
    reader: SharedFrameReader,
    value: SharedArrayRef | npt.NDArray[typing.Any],
) -> npt.NDArray[typing.Any]:
    """
    Resolve whatever :func:`publish_array` produced back into an array.

    Parameters
    ----------
    reader : SharedFrameReader
        The receiver's own reader.
    value : SharedArrayRef | npt.NDArray[typing.Any]
        A reference into the sender's segment, or an inline array.

    Returns
    -------
    npt.NDArray[typing.Any]
        The array, always a copy this side owns — so nothing downstream
        holds a view of a buffer the sender may overwrite on its next
        call.
    """
    if isinstance(value, SharedArrayRef):
        return reader.read_array(value)
    return np.asarray(value)


def publish_source(writer: SharedFrameWriter, job: AnalysisInput) -> AnalysisSource:
    """
    Package an analysis input for the wire.

    Parameters
    ----------
    writer : SharedFrameWriter
        The client's own writer, used only when there are frames to send.
    job : AnalysisInput
        What the caller wants analysed, in the form the in-process runner
        would use unchanged.

    Returns
    -------
    AnalysisSource
        The payload to put in a
        :class:`~miainwoodpecker.devices.rpc.Call`.

    Raises
    ------
    ValueError
        If neither a path nor frames were given, which would leave the
        worker with nothing to analyse and no way to say what was missing.
    """
    if job.frames is not None:
        return AnalysisSource(
            stack=publish_array(writer, job.frames.data),
            frame_time=job.frames.frame_time,
            calibration=job.frames.calibration,
            origin=job.origin,
        )
    if job.path is None:
        msg = (
            "an analysis source needs either a file to open or frames to "
            "analyse, and was given neither"
        )
        raise ValueError(msg)
    return AnalysisSource(path=job.path, origin=job.origin)


def read_source(reader: SharedFrameReader, source: AnalysisSource) -> AnalysisInput:
    """
    Unpack an :class:`AnalysisSource` on the worker side.

    Parameters
    ----------
    reader : SharedFrameReader
        The worker's own reader.
    source : AnalysisSource
        What the client sent.

    Returns
    -------
    AnalysisInput
        Exactly what the in-process runner would have passed to the same
        operation, so the worker adds no argument shuffling of its own and
        the two paths call one function one way.

    Raises
    ------
    ValueError
        If the payload carries neither a file nor frames, or frames
        without their axes — which means a client built one by hand rather
        than through :func:`publish_source`.
    """
    if source.stack is not None:
        if source.calibration is None or source.frame_time is None:
            msg = (
                "an analysis source carrying frames must carry their frame "
                "times and calibration too; an array without its axes would "
                "be analysed as bare pixels"
            )
            raise ValueError(msg)
        return AnalysisInput(
            frames=FrameStack(
                data=read_array(reader, source.stack),
                frame_time=np.asarray(source.frame_time),
                calibration=source.calibration,
            ),
            origin=source.origin,
        )
    if source.path is None:
        msg = "an analysis source carries neither a file nor frames"
        raise ValueError(msg)
    return AnalysisInput(path=source.path, origin=source.origin)
