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

A third user arrived after the device layer:
:mod:`miainwoodpecker.analysis.transfer` moves analysis payloads through
the same segments (see docs/analysis-isolation.md). It needs one thing the
device path never did — a **bare array**, with no ``Frame`` or ``Spectrum``
around it — because a frame stack's array and an analysis result are
arrays and nothing else. :class:`SharedArrayRef`,
:meth:`SharedFrameWriter.publish_array` and
:meth:`SharedFrameReader.read_array` are that, and they are three lines
each because ``_store``/``_copy_out`` were already the array-shaped core
that :meth:`SharedFrameWriter.publish` and
:meth:`SharedFrameWriter.publish_spectrum` were both wrappers around.
Nothing about the reuse rule, the ownership rule, or the
one-in-flight invariant changes: the analysis protocol is synchronous
request/response for the same reason
:mod:`miainwoodpecker.devices.rpc` is.

The simultaneous multi-channel scan
(:meth:`~miainwoodpecker.devices.interface.Scanner.scan_frames`) then
asked the one question the synchronous protocol makes interesting: **N
frames, one reply.** The reuse rule allows exactly one publish per
request/response cycle, so N publishes into the reused segment would each
overwrite the last while the client is still on the previous reply. The
frames of a pass share shape and dtype by construction, so they cross as
one stacked ``(n, height, width)`` block —
:class:`SharedFrameSetRef`, :meth:`SharedFrameWriter.publish_frames` and
:meth:`SharedFrameReader.read_frames` — which costs one copy in and one
copy out at the same per-byte price as a single frame of the combined
size, and leaves the ownership rules untouched because it is still one
segment per source. The alternatives are recorded where the choice is:
see :class:`SharedFrameSetRef`.

MIT, no ``nion.*`` import — used by
:mod:`miainwoodpecker.devices.nion_server`,
:mod:`miainwoodpecker.devices.remote`, and
:mod:`miainwoodpecker.analysis.transfer`.
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
    from collections.abc import Sequence

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


def _stop_tracking(name: str) -> None:
    """
    Tell this process's ``resource_tracker`` to forget an attached segment.

    Only ever called by a :class:`SharedFrameReader` constructed with
    ``stop_tracking=True``, and only for a segment that reader has just
    attached to by name — see that class for why the analysis layer needs
    it and the device layer does not.

    The leading slash is what ``multiprocessing.shared_memory`` registers
    internally (its public ``name`` strips it), so it has to be put back
    or the unregister names a segment the daemon has never heard of.

    Parameters
    ----------
    name : str
        The segment's public name, without the leading slash.
    """
    try:
        from multiprocessing import resource_tracker  # noqa: PLC0415

        resource_tracker.unregister(f"/{name}", "shared_memory")
    except Exception:  # noqa: BLE001, S110 - see below
        # Best-effort by design. On Windows there is no tracker to talk to
        # and nothing was registered; on POSIX a failure here costs a
        # spurious unlink attempt at exit, which is the behaviour this
        # module already had. Neither is worth failing a frame over.
        pass


@dataclass(frozen=True)
class SharedArrayRef:
    """
    A reference to a bare array living in a (possibly reused) segment.

    The same three fields :class:`SharedFrameRef` needs to reconstruct an
    array, and none of the ones it needs to reconstruct a *frame*. It
    exists for :mod:`miainwoodpecker.analysis.transfer`, whose payloads —
    a frame stack's ``(n, h, w)`` block, a mean projection, a fitted
    diffraction pattern — are arrays with no timestamp and no device
    metadata. Inventing a timestamp to reuse :class:`SharedFrameRef` would
    put a fiction in the wire format; carrying an empty metadata mapping
    would say the acquisition recorded nothing rather than that this is not
    an acquisition.

    Attributes
    ----------
    shm_name : str
        Name of the shared memory segment holding the array.
    shape : tuple[int, ...]
        Array shape.
    dtype : str
        Array dtype, as a string (``np.dtype(dtype)`` reconstructs it).
    """

    shm_name: str
    shape: tuple[int, ...]
    dtype: str


@dataclass(frozen=True)
class SharedFrameSetRef:
    """
    A reference to several same-shape frames stacked in one shared segment.

    How the frames of one simultaneous multi-channel scan
    (:meth:`~miainwoodpecker.devices.interface.Scanner.scan_frames`) cross
    the process boundary. **One block, not N refs**, deliberately: the
    frames of a pass are produced together and must be returned together,
    and the reused-segment design allows exactly one publish per
    request/response cycle — N sequential publishes into one segment would
    have each overwrite the last, and N segments per source would multiply
    the ownership and orphan-cleanup rules this module is careful about.
    Frames from one pass share shape and dtype by construction (same scan
    geometry, same device), so stacking them as ``(n, height, width)`` in
    the existing segment costs one copy in and one copy out — the same
    per-byte price as any single frame of the combined size (measured at
    ~0.75-0.9 ms/MB; see ``scripts/ipc_overhead_benchmark.py``).

    The third option, **N sequential replies** — one round trip per
    channel — was rejected on more than speed. It would make the server
    hold a finished pass's frames between calls, so a client that died
    mid-pass would pin them, and it would break the property the
    multi-channel call exists to give: that one request is one
    acquisition, which is what makes the dose accounting checkable from
    the outside. Its cost is real too, since each reply pays the
    round-trip latency again, but that is the smaller half of the
    argument.

    Sizes, so the choice is not made on principle alone: a two-channel
    1536² float64 pass is 37.7MB, about 30ms of copying either way at the
    measured rate — against roughly 340ms had the same two frames gone
    through pickle at that size (+168ms per 33.6MB frame, measured).
    Below :data:`~miainwoodpecker.devices.rpc.SHARED_MEMORY_THRESHOLD_BYTES`
    the set stays on the pickle path as one ordinary list, exactly as a
    small single frame does.

    Per-frame identity travels alongside the block: entry ``i`` of
    ``timestamps``/``metadata`` belongs to plane ``i`` of the stack, in
    the order the frames were published.

    Attributes
    ----------
    shm_name : str
        Name of the shared memory segment holding the stacked block.
    shape : tuple[int, ...]
        The stacked block's shape: ``(frame count, *frame shape)``.
    dtype : str
        Array dtype, as a string (``np.dtype(dtype)`` reconstructs it).
    timestamps : tuple[datetime.datetime, ...]
        Each original frame's timestamp, in stack order.
    metadata : tuple[typing.Mapping[str, typing.Any], ...]
        Each original frame's metadata, in stack order.
    """

    shm_name: str
    shape: tuple[int, ...]
    dtype: str
    timestamps: tuple[datetime.datetime, ...]
    metadata: tuple[typing.Mapping[str, typing.Any], ...]


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


# Every reference shape this module hands out names a segment, a shape,
# and a dtype - which is all the reader's attach-and-wrap core needs.
_AnySharedRef = SharedFrameRef | SharedSpectrumRef | SharedArrayRef | SharedFrameSetRef


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

    def publish_frames(self, frames: Sequence[Frame]) -> SharedFrameSetRef:
        """
        Copy several same-shape frames into the reused segment as one block.

        The same segment, the same grow-only reuse rule, and the same
        one-in-flight invariant as :meth:`publish` — the whole set is one
        publish, which is what makes it safe at all (see
        :class:`SharedFrameSetRef` for why N separate publishes are not
        an option here).

        Parameters
        ----------
        frames : Sequence[Frame]
            The frames to publish. Must be non-empty and agree on shape
            and dtype, which frames of one scanned pass do by
            construction.

        Returns
        -------
        SharedFrameSetRef
            A reference the reader can use to retrieve every frame.

        Raises
        ------
        ValueError
            If ``frames`` is empty, or if the frames disagree on shape
            or dtype — a set this method cannot stack, and one no scanned
            pass produces, so refusing is diagnosis rather than policy.
        """
        if not frames:
            msg = "publish_frames() needs at least one frame"
            raise ValueError(msg)
        arrays = [np.ascontiguousarray(frame.data) for frame in frames]
        first = arrays[0]
        if any(
            array.shape != first.shape or array.dtype != first.dtype
            for array in arrays[1:]
        ):
            msg = (
                f"publish_frames() needs frames agreeing on shape and dtype; "
                f"got {[(array.shape, str(array.dtype)) for array in arrays]}"
            )
            raise ValueError(msg)
        shape = (len(arrays), *first.shape)
        with self._lock:
            segment = self._ensure_capacity(first.nbytes * len(arrays))
            destination = np.ndarray(shape, dtype=first.dtype, buffer=segment.buf)
            # Plane by plane rather than one np.stack: stacking would
            # build the whole block in ordinary memory first, so every
            # byte would be copied twice on a path whose entire cost is
            # the copy (~0.75-0.9 ms/MB).
            for index, array in enumerate(arrays):
                destination[index] = array
            shm_name = segment.name
        return SharedFrameSetRef(
            shm_name=shm_name,
            shape=shape,
            dtype=str(first.dtype),
            timestamps=tuple(frame.timestamp for frame in frames),
            metadata=tuple(frame.metadata for frame in frames),
        )

    def publish_array(
        self,
        array: npt.NDArray[typing.Any],
    ) -> SharedArrayRef:
        """
        Copy a bare array into the reused segment, growing it if needed.

        The same segment, the same reuse rule, and the same one-in-flight
        invariant as :meth:`publish`. Used by
        :mod:`miainwoodpecker.analysis.transfer` in **both** directions —
        an analysis client publishes its request's frame stack and an
        analysis worker publishes its result — which is the one structural
        difference from the device path, where only the server ever writes.
        Each side owns its own writer, so the ownership rule is unchanged:
        whoever created a segment is who unlinks it.

        Parameters
        ----------
        array : npt.NDArray[typing.Any]
            The array to publish, contiguous or not.

        Returns
        -------
        SharedArrayRef
            A reference the reader can use to retrieve the array.
        """
        shm_name, data = self._store(array)
        return SharedArrayRef(
            shm_name=shm_name,
            shape=data.shape,
            dtype=str(data.dtype),
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
            segment = self._ensure_capacity(data.nbytes)
            destination = np.ndarray(data.shape, dtype=data.dtype, buffer=segment.buf)
            destination[:] = data
            shm_name = segment.name
        return shm_name, data

    def _ensure_capacity(self, size: int) -> shared_memory.SharedMemory:
        """
        Return the current segment, grown first if it cannot hold ``size``.

        The grow-only reuse rule lives here alone, called by every
        publish path with ``self._lock`` already held. One place decides
        when a segment is replaced, because that decision is the measured
        one this module exists for: replacing costs a syscall pair per
        publish, which was worse than plain pickling at moderate sizes.

        Parameters
        ----------
        size : int
            Bytes the caller is about to write.

        Returns
        -------
        shared_memory.SharedMemory
            A segment of at least ``size`` bytes.
        """
        if self._segment is None or size > self._segment.size:
            self._replace_segment(size)
        assert self._segment is not None  # noqa: S101 - just created above
        return self._segment

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

    Parameters
    ----------
    stop_tracking : bool
        Unregister each attached segment from *this* process's
        ``resource_tracker``, so that this process exiting cannot unlink a
        segment it does not own. Default ``False``, which is the device
        layer's behaviour unchanged.

        It exists because the analysis layer inverted a lifetime the
        device layer never had to think about. There, the reader is the
        long-lived application and the writer is the subprocess, so the
        reader's tracker gets its chance to misbehave only as the whole
        session ends, by which point the writer has already unlinked and
        the tracker finds nothing — the harmless "warns once per segment"
        wart this module's header describes. In an analysis worker the
        roles are reversed: the *reader* is the subprocess, it attaches to
        a segment the long-lived client owns, and when the worker exits
        its tracker cheerfully unlinks the client's live segment out from
        under it. Measured, not predicted — the first end-to-end run of
        the isolated path failed on exactly this, with the client's own
        ``close()`` raising ``FileNotFoundError`` for a segment it had
        created and never released.

        ``resource_tracker.unregister`` is the documented fix, and the
        reason this module's header records it as having *made things
        worse* does not apply here. That attempt unregistered a name from
        a daemon that had never registered it — writer and reader being
        independent ``Popen`` processes with a tracker each — and landed a
        ``KeyError`` in the wrong daemon's main loop. This unregisters, in
        the attaching process's own daemon, a name that same daemon
        registered a line earlier. Same call, opposite side of the
        boundary.

        Opt-in rather than always-on because the device layer's tests pin
        its current behaviour and a reader that never unlinks is correct
        either way; there is no reason to move a measured thing to fix a
        problem it does not have.
    """

    def __init__(self, *, stop_tracking: bool = False) -> None:
        self._segment: shared_memory.SharedMemory | None = None
        self._last_name: str | None = None
        self._stop_tracking = stop_tracking

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

    def read_frames(self, reference: SharedFrameSetRef) -> list[Frame]:
        """
        Copy every referenced frame out of the writer's shared segment.

        Each frame gets its own private copy of its plane of the stacked
        block, for exactly the reason :meth:`read` copies: nothing
        downstream may hold a view of a buffer the writer will overwrite
        on the next call — and per-frame copies rather than one block
        copy sliced into views, so releasing one frame does not silently
        keep its whole pass alive.

        Parameters
        ----------
        reference : SharedFrameSetRef
            A reference returned by :meth:`SharedFrameWriter.publish_frames`.

        Returns
        -------
        list[Frame]
            The frames, in the order they were published, each with its
            own timestamp, metadata, and private array.
        """
        view = self._view(reference)
        return [
            Frame(
                data=view[index].copy(),
                timestamp=timestamp,
                metadata=metadata,
            )
            for index, (timestamp, metadata) in enumerate(
                zip(reference.timestamps, reference.metadata, strict=True),
            )
        ]

    def read_array(self, reference: SharedArrayRef) -> npt.NDArray[typing.Any]:
        """
        Copy a referenced bare array out of the writer's shared segment.

        Parameters
        ----------
        reference : SharedArrayRef
            A reference returned by :meth:`SharedFrameWriter.publish_array`.

        Returns
        -------
        npt.NDArray[typing.Any]
            The array, as a copy the caller owns.
        """
        return self._copy_out(reference)

    def _copy_out(
        self,
        reference: SharedFrameRef | SharedSpectrumRef | SharedArrayRef,
    ) -> npt.NDArray[typing.Any]:
        """
        Attach if needed and return a private copy of the referenced array.

        Parameters
        ----------
        reference : SharedFrameRef | SharedSpectrumRef | SharedArrayRef
            Any reference naming a segment, a shape, and a dtype.

        Returns
        -------
        npt.NDArray[typing.Any]
            A copy the caller owns, so nothing downstream reads a buffer
            the writer may overwrite on the next call.
        """
        return self._view(reference).copy()

    def _view(self, reference: _AnySharedRef) -> npt.NDArray[typing.Any]:
        """
        Attach if needed and return a *view* of the referenced array.

        The attach-and-wrap core :meth:`_copy_out` and :meth:`read_frames`
        share. The view aliases the writer's live buffer, which is why it
        stays private to this class: every public read path copies out of
        it before returning.

        Parameters
        ----------
        reference : _AnySharedRef
            Any reference naming a segment, a shape, and a dtype.

        Returns
        -------
        npt.NDArray[typing.Any]
            A view over the shared segment, valid until the next call
            that re-attaches.
        """
        if self._segment is None or self._segment.name != reference.shm_name:
            if self._segment is not None:
                self._segment.close()  # detach only: the writer owns unlink()
            self._segment = shared_memory.SharedMemory(name=reference.shm_name)
            self._last_name = reference.shm_name
            if self._stop_tracking:
                _stop_tracking(reference.shm_name)
        return np.ndarray(
            reference.shape,
            dtype=np.dtype(reference.dtype),
            buffer=self._segment.buf,
        )

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
