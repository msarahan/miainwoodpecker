"""
Cross-process frame transfer via a reused shared memory segment.

Benchmarked need (``scripts/ipc_overhead_benchmark.py``): pickling a
:class:`~miainwoodpecker.devices.interface.Frame` through
``multiprocessing.connection`` copies its array through serialization and
a kernel socket buffer. That overhead scales with frame size and was
negligible for a 128x128 scan (+0.7ms) but added 74% at 512x512 (+7.4ms)
and more than doubled total latency at 2048x2048 (+168ms on a 163ms
baseline) — squarely the size range real detector data lives in.

A first version (create a fresh segment per frame) fixed the large-frame
case but was *worse* than plain pickling at 2.1MB: creating and
destroying a named POSIX segment is its own syscall pair
(shm_open/mmap, then munmap/close/unlink), paid on every single frame,
and that fixed cost dominates at moderate sizes.

This version instead reuses one persistent segment per source
(:class:`SharedFrameWriter` server-side, :class:`SharedFrameReader`
client-side), paying that setup cost once instead of per frame. Reuse is
safe without double-buffering specifically because the RPC protocol in
:mod:`miainwoodpecker.devices.rpc` is strictly synchronous request/response:
the server cannot start writing frame N+1 until it receives the client's
next :class:`~miainwoodpecker.devices.rpc.Call`, which the client only
sends after it has already copied frame N out. There is never a moment
where both sides are touching the buffer at once.

Ownership: unlike a one-shot segment (which the reader could safely
destroy immediately after copying out), a *reused* segment must be
destroyed by whoever keeps recreating it - the writer - once it is
retired (resized, or the source closes), not by the reader on every read.
Named POSIX shared memory segments are not reclaimed when a process
dies (unlike its threads or its anonymous memory): they are persistent
tmpfs entries until explicitly ``unlink()``-ed. This makes the writer's
:meth:`SharedFrameWriter.close` genuinely load-bearing, not a nicety -
see :mod:`miainwoodpecker.devices.remote`'s teardown for why that method
is now called explicitly rather than relying on the server process being
killed.

That leaves the case no writer-side discipline can cover, and measuring it
corrected the expectation. A ``SIGKILL``-ed server runs no cleanup of its
own — but ``multiprocessing``'s ``resource_tracker`` is a *separate child
process*, it auto-registers every segment created here (the same
auto-registration the warning note below is about), and it unlinks
everything it holds when the server's end of its pipe closes. So killing
the server alone leaks nothing. Measured, not assumed — and it is a
CPython implementation detail rather than a guarantee, which fails
precisely when the tracker dies alongside the server: a process-group
kill, a cgroup OOM kill, a container stop.
:meth:`SharedFrameReader.unlink_orphan` is the client-side mitigation for
that case — the reader remembers the names it attached to and can unlink
them once the writer's process is confirmed dead — and its docstring is
explicit about the narrow window even that cannot reach.

MIT, no ``nion.*`` import — used by both
:mod:`miainwoodpecker.devices.nion_server` and
:mod:`miainwoodpecker.devices.remote`.
"""

from __future__ import annotations

import contextlib
import os
import threading
import typing
from dataclasses import dataclass
from multiprocessing import shared_memory

import numpy as np

from miainwoodpecker.devices.interface import Frame, Spectrum

if typing.TYPE_CHECKING:
    import datetime

    import numpy.typing as npt

# Every SharedMemory handle - created or attached by name - auto-registers
# with *this process's* resource_tracker, which tries to unlink it again at
# exit. We manage each segment's lifecycle explicitly (the writer creates,
# resizes, and unlinks; the reader only ever attaches and detaches), so that
# automatic cleanup only ever finds the segment already gone (or, now that
# segments are reused rather than one-shot, still legitimately in use by its
# writer) and warns.
#
# The documented workaround (resource_tracker.unregister on the segment's
# name) was tried and made things worse: writer and reader are independent
# subprocess.Popen processes, each with their own resource_tracker daemon,
# and unregister() can land on a daemon that never registered that name,
# crashing its main loop with a KeyError instead of merely warning.
# PYTHONWARNINGS, read by every interpreter at its own startup including a
# forked+exec'd tracker daemon, is what actually works.
os.environ.setdefault(
    "PYTHONWARNINGS",
    "ignore:resource_tracker:UserWarning:multiprocessing.resource_tracker",
)


@dataclass(frozen=True)
class SharedFrameRef:
    """
    A reference to a frame's array living in a (possibly reused) shared segment.

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


@dataclass(frozen=True)
class SharedSpectrumRef:
    """
    A reference to a spectrum's array living in a (possibly reused) segment.

    The spectrum-side counterpart to :class:`SharedFrameRef`, and a
    separate type rather than a widened one because the two carry
    different things: a spectrum's energy calibration is part of the
    value, not part of its metadata, so it has to travel here or the
    reader cannot reconstruct the
    :class:`~miainwoodpecker.devices.interface.Spectrum` at all.

    A spot spectrum never reaches this path in practice — 4096 uint32
    channels is 16KB against the 64KB of
    :data:`~miainwoodpecker.devices.rpc.SHARED_MEMORY_THRESHOLD_BYTES` —
    and that is fine; the threshold is there to decide, not to be hit.
    What this exists for is the map: a 256x256 scan of 4096-channel
    spectra is 1GB, which is not a size to push through pickle and a
    socket buffer.

    Attributes
    ----------
    shm_name : str
        Name of the shared memory segment holding the array.
    shape : tuple[int, ...]
        Array shape, energy last.
    dtype : str
        Array dtype, as a string (``np.dtype(dtype)`` reconstructs it).
    timestamp : datetime.datetime
        The original spectrum's timestamp.
    energy_offset_ev : float
        Energy at channel 0, in electronvolts.
    energy_scale_ev : float
        Energy per channel, in electronvolts.
    metadata : typing.Mapping[str, typing.Any]
        The original spectrum's metadata.
    """

    shm_name: str
    shape: tuple[int, ...]
    dtype: str
    timestamp: datetime.datetime
    energy_offset_ev: float
    energy_scale_ev: float
    metadata: typing.Mapping[str, typing.Any]


class SharedFrameWriter:
    """
    Server-side: publish frames into one reused, resized-on-demand segment.

    Not shared between threads concurrently in this project (each RPC
    target is served by exactly one connection's handler thread at a
    time), but guarded by a lock anyway - the cost is negligible and it
    turns a latent concurrency bug into a well-defined one if that
    assumption ever stops holding.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._segment: shared_memory.SharedMemory | None = None

    def publish(self, frame: Frame) -> SharedFrameRef:
        """
        Copy a frame's array into the reused segment, growing it if needed.

        The segment is replaced only when the frame does not *fit*, not
        whenever its shape or dtype changes. That distinction is the whole
        point of reuse: replacing means ``shm_open``/``mmap`` plus
        ``munmap``/``unlink``, the per-frame syscall pair this design
        exists to avoid, and keying on exact shape equality paid it for
        every *smaller* frame and on every frame of an alternating
        workload (an operator toggling 512² and 1024², or a benchmark
        sweeping sizes) — reverting to the regime measured as worse than
        plain pickling. Shape and dtype need no tracking here because they
        travel in the returned :class:`SharedFrameRef`; the segment is
        just bytes, and a larger one holds a smaller frame perfectly well.

        Parameters
        ----------
        frame : Frame
            The frame to publish.

        Returns
        -------
        SharedFrameRef
            A reference the reader can use to retrieve the array. Its
            ``shm_name`` is stable across calls until a frame too large
            for the current segment forces a new one.
        """
        shm_name, data = self._store(frame.data)
        return SharedFrameRef(
            shm_name=shm_name,
            shape=data.shape,
            dtype=str(data.dtype),
            timestamp=frame.timestamp,
            metadata=frame.metadata,
        )

    def publish_spectrum(self, spectrum: Spectrum) -> SharedSpectrumRef:
        """
        Copy a spectrum's array into the reused segment, growing it if needed.

        The same segment, the same reuse rule, and the same one-in-flight
        invariant as :meth:`publish`; only the reference type differs,
        because a spectrum's energy calibration has to cross with it.
        Sharing the segment is deliberate rather than incidental: a
        target serves exactly one device, so a spectrum detector's writer
        will only ever hold spectra.

        Parameters
        ----------
        spectrum : Spectrum
            The spectrum to publish.

        Returns
        -------
        SharedSpectrumRef
            A reference the reader can use to retrieve the array.
        """
        shm_name, data = self._store(spectrum.data)
        return SharedSpectrumRef(
            shm_name=shm_name,
            shape=data.shape,
            dtype=str(data.dtype),
            timestamp=spectrum.timestamp,
            energy_offset_ev=spectrum.energy_offset_ev,
            energy_scale_ev=spectrum.energy_scale_ev,
            metadata=spectrum.metadata,
        )

    def _store(
        self,
        array: npt.NDArray[typing.Any],
    ) -> tuple[str, npt.NDArray[typing.Any]]:
        """
        Copy one array into the reused segment and return its name and the copy.

        Parameters
        ----------
        array : npt.NDArray[typing.Any]
            The array to publish, contiguous or not.

        Returns
        -------
        tuple[str, npt.NDArray[typing.Any]]
            The segment's name, and the contiguous array that was
            written — the caller reads ``shape``/``dtype`` off the
            latter rather than off its own input, since
            ``ascontiguousarray`` is what the reader will reconstruct.
        """
        data = np.ascontiguousarray(array)
        with self._lock:
            if self._segment is None or data.nbytes > self._segment.size:
                self._replace_segment(data.nbytes)
            assert self._segment is not None  # noqa: S101 - just created above
            destination = np.ndarray(
                data.shape, dtype=data.dtype, buffer=self._segment.buf,
            )
            destination[:] = data
            shm_name = self._segment.name
        return shm_name, data

    def _replace_segment(self, size: int) -> None:
        """Destroy the current segment, if any, and create a right-sized one."""
        if self._segment is not None:
            self._segment.close()
            self._segment.unlink()
        self._segment = shared_memory.SharedMemory(create=True, size=max(size, 1))

    def close(self) -> None:
        """Destroy the current segment, if any. Call when the source closes."""
        with self._lock:
            if self._segment is not None:
                self._segment.close()
                self._segment.unlink()
                self._segment = None


class SharedFrameReader:
    """
    Client-side: read frames from a writer's reused, possibly-resized segment.

    Remembers the name of the last segment it attached to even after
    detaching, which is not bookkeeping for its own sake: it is the only
    thing that makes :meth:`unlink_orphan` possible after the writer's
    process has been hard-killed. See that method for the ownership
    exception it represents.
    """

    def __init__(self) -> None:
        self._segment: shared_memory.SharedMemory | None = None
        self._last_name: str | None = None

    def read(self, reference: SharedFrameRef) -> Frame:
        """
        Copy a referenced frame's array out of the writer's shared segment.

        Parameters
        ----------
        reference : SharedFrameRef
            A reference returned by :meth:`SharedFrameWriter.publish`.

        Returns
        -------
        Frame
            The frame, with its own private copy of the array.
        """
        return Frame(
            data=self._copy_out(reference),
            timestamp=reference.timestamp,
            metadata=reference.metadata,
        )

    def read_spectrum(self, reference: SharedSpectrumRef) -> Spectrum:
        """
        Copy a referenced spectrum's array out of the writer's shared segment.

        Parameters
        ----------
        reference : SharedSpectrumRef
            A reference returned by
            :meth:`SharedFrameWriter.publish_spectrum`.

        Returns
        -------
        Spectrum
            The spectrum, with its own private copy of the array and the
            energy calibration the writer acquired it with.
        """
        return Spectrum(
            data=self._copy_out(reference),
            timestamp=reference.timestamp,
            energy_offset_ev=reference.energy_offset_ev,
            energy_scale_ev=reference.energy_scale_ev,
            metadata=reference.metadata,
        )

    def _copy_out(
        self,
        reference: SharedFrameRef | SharedSpectrumRef,
    ) -> npt.NDArray[typing.Any]:
        """
        Attach if needed and return a private copy of the referenced array.

        Parameters
        ----------
        reference : SharedFrameRef | SharedSpectrumRef
            Any reference naming a segment, a shape, and a dtype.

        Returns
        -------
        npt.NDArray[typing.Any]
            A copy the caller owns, so nothing downstream reads a buffer
            the writer may overwrite on the next call.
        """
        if self._segment is None or self._segment.name != reference.shm_name:
            if self._segment is not None:
                self._segment.close()  # detach only: the writer owns unlink()
            self._segment = shared_memory.SharedMemory(name=reference.shm_name)
            self._last_name = reference.shm_name
        view = np.ndarray(
            reference.shape,
            dtype=np.dtype(reference.dtype),
            buffer=self._segment.buf,
        )
        return view.copy()

    def close(self) -> None:
        """Detach from the current segment, if any. Never unlinks it."""
        if self._segment is not None:
            self._segment.close()
            self._segment = None

    def unlink_orphan(self) -> str | None:
        """
        Detach and *unlink* the last segment, for a writer that died uncleanly.

        A deliberate, narrow exception to this module's ownership rule
        (the writer creates, resizes, and unlinks; the reader only ever
        attaches and detaches). The rule exists because the writer keeps
        recreating the segment, so the reader must not pull it out from
        under a live writer. That premise fails in exactly one situation:
        the writer's *process* is gone. A named POSIX segment is a tmpfs
        entry rather than a process resource, so it persists until
        something unlinks it by name — and after the writer is dead, the
        reader holds the only remaining record of those names.

        Usually something else gets there first: the writer's
        ``resource_tracker`` child survives a ``SIGKILL`` of the server and
        cleans up on its behalf (see this module's header). This method is
        for when it does not — the tracker killed alongside the server, as
        a process-group or cgroup OOM kill does.

        ``shm_unlink`` needs the name and filesystem permission, not
        creator identity, so this genuinely reclaims the segment. It is
        only sound to call once the writer's process has been confirmed
        dead; :mod:`miainwoodpecker.devices.remote`'s teardown is the only
        caller and checks ``Popen.returncode`` first.

        Note what this cannot cover, which is a real residual limitation
        rather than an oversight: a segment the writer created but whose
        name never reached this reader (killed between ``shm_open`` and
        the reply carrying its :class:`SharedFrameRef`) is unrecoverable
        from here, because nothing on this side ever learned its name.

        Returns
        -------
        str | None
            The name unlinked, or ``None`` if this reader never attached
            to a segment.
        """
        name = self._last_name
        self.close()
        if name is None:
            return None
        self._last_name = None
        try:
            segment = shared_memory.SharedMemory(name=name)
        except FileNotFoundError:
            # Already gone: the writer got to unlink it after all (a
            # graceful shutdown, or the per-device close() fallback).
            return name
        segment.close()
        with contextlib.suppress(FileNotFoundError):
            segment.unlink()
        return name
