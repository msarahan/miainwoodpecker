"""
Cross-process frame transfer via shared memory, not pickle-over-socket.

Benchmarked need (``scripts/ipc_overhead_benchmark.py``): pickling a
:class:`~miainwoodpecker.devices.interface.Frame` through
``multiprocessing.connection`` copies its array through serialization and
a kernel socket buffer. That overhead scales with frame size and was
negligible for a 128x128 scan (+0.7ms) but added 74% at 512x512 (+7.4ms)
and more than doubled total latency at 2048x2048 (+168ms on a 163ms
baseline) — squarely the size range real detector data lives in.

:func:`write_frame_to_shared_memory` (server side) copies a frame's array
into a named OS-backed shared memory segment and returns a small
:class:`SharedFrameRef` in its place — that reference is what actually
crosses the small control connection.
:func:`read_frame_from_shared_memory` (client side) attaches to that
segment, copies the data into a private array it owns, and releases the
segment. One memcpy replaces a serialize/deserialize pass plus a socket
copy.

Ownership is deliberately simple rather than a persistent ring buffer: a
fresh segment per frame, created and detached (not destroyed) by the
server, then attached, copied, and destroyed by the client. Simple to
reason about; a create/destroy syscall pair per frame is the cost this
trades for.

MIT, no ``nion.*`` import — used by both
:mod:`miainwoodpecker.devices.nion_server` and
:mod:`miainwoodpecker.devices.remote`.
"""

from __future__ import annotations

import os
import typing
from dataclasses import dataclass
from multiprocessing import shared_memory

import numpy as np

from miainwoodpecker.devices.interface import Frame

if typing.TYPE_CHECKING:
    import datetime

# Every SharedMemory handle - created or attached by name - auto-registers
# with *this process's* resource_tracker, which tries to unlink it again at
# exit. We manage each segment's lifecycle explicitly (server creates and
# closes; client attaches, copies, closes, and unlinks), so that automatic
# cleanup only ever finds the segment already gone and warns.
#
# Two things that do NOT fix this, tried first: a `warnings.filterwarnings`
# call here only touches this interpreter's own filter state, but the
# warning is `warnings.warn()`-ed inside the resource_tracker daemon's own
# separate process, so it's invisible to that call entirely. And the
# documented `resource_tracker.unregister()` workaround assumes
# register/unregister land on the same tracker daemon, which is true within
# one multiprocessing.Process tree but not here: server and client are
# independent subprocess.Popen processes, each with their own daemon, so
# unregister() reached a daemon that never registered that name and crashed
# its main loop with a KeyError instead of merely warning.
#
# What does work: PYTHONWARNINGS is read by *every* interpreter at its own
# startup, including the tracker daemon's (it's forked+exec'd, which
# inherits the environment). Setting it here, before any SharedMemory use
# in this process, suppresses the client side; remote.py sets the same
# variable in the server subprocess's env for the same reason on that side.
os.environ.setdefault(
    "PYTHONWARNINGS",
    "ignore:resource_tracker:UserWarning:multiprocessing.resource_tracker",
)


@dataclass(frozen=True)
class SharedFrameRef:
    """
    A reference to a frame's array living in shared memory.

    Attributes
    ----------
    shm_name : str
        Name of the shared memory segment holding the array.
    shape : tuple[int, ...]
        Array shape.
    dtype : str
        Array dtype, as a string (``np.dtype(dtype)`` reconstructs it).
    timestamp : datetime.datetime
        The original frame's timestamp.
    metadata : typing.Mapping[str, typing.Any]
        The original frame's metadata.
    """

    shm_name: str
    shape: tuple[int, ...]
    dtype: str
    timestamp: datetime.datetime
    metadata: typing.Mapping[str, typing.Any]


def write_frame_to_shared_memory(frame: Frame) -> SharedFrameRef:
    """
    Copy a frame's array into a new shared memory segment.

    Parameters
    ----------
    frame : Frame
        The frame to publish.

    Returns
    -------
    SharedFrameRef
        A reference the reader can use to retrieve the array.
    """
    data = np.ascontiguousarray(frame.data)
    segment = shared_memory.SharedMemory(create=True, size=max(data.nbytes, 1))
    try:
        destination = np.ndarray(data.shape, dtype=data.dtype, buffer=segment.buf)
        destination[:] = data
    finally:
        segment.close()  # detach this process's mapping; the segment persists
    return SharedFrameRef(
        shm_name=segment.name,
        shape=data.shape,
        dtype=str(data.dtype),
        timestamp=frame.timestamp,
        metadata=frame.metadata,
    )


def read_frame_from_shared_memory(reference: SharedFrameRef) -> Frame:
    """
    Copy a referenced frame's array out of shared memory and release it.

    Parameters
    ----------
    reference : SharedFrameRef
        A reference returned by :func:`write_frame_to_shared_memory`.

    Returns
    -------
    Frame
        The frame, with its own private copy of the array.
    """
    segment = shared_memory.SharedMemory(name=reference.shm_name)
    try:
        view = np.ndarray(
            reference.shape,
            dtype=np.dtype(reference.dtype),
            buffer=segment.buf,
        )
        owned = view.copy()
    finally:
        segment.close()
        segment.unlink()
    return Frame(
        data=owned,
        timestamp=reference.timestamp,
        metadata=reference.metadata,
    )
