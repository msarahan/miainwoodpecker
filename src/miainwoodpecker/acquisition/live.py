"""
Live acquisition loop: grab frames on a worker thread, latest frame wins.

The loop repeatedly calls a grab callable (``camera.acquire_frame`` or a
closure over ``scanner.scan_frame``) and stores only the most recent
frame. A display can then poll :meth:`LiveAcquisition.latest` at its own
rate: acquisition and display are decoupled, slow consumers simply skip
frames, and no per-frame event fan-out ever reaches the UI thread — the
failure mode that made the system this project replaces slow (see
docs/migration-plan.md, §3).

This module is deliberately UI-agnostic: it knows nothing about Qt or
napari, only about :class:`~miainwoodpecker.devices.interface.Frame`.
"""

from __future__ import annotations

import collections
import threading
import time
import typing
from dataclasses import dataclass

if typing.TYPE_CHECKING:
    from collections.abc import Callable

    from miainwoodpecker.devices.interface import Frame


@dataclass(frozen=True)
class LiveStats:
    """
    A snapshot of a live acquisition loop's progress.

    Attributes
    ----------
    frame_count : int
        Total frames grabbed since the loop last started.
    fps : float
        Recent acquisition rate in frames per second, measured over a
        sliding window; 0.0 until at least two frames have arrived.
    """

    frame_count: int
    fps: float


class LiveAcquisition:
    """
    Run a grab callable in a loop on a daemon worker thread.

    Parameters
    ----------
    grab : Callable[[], Frame]
        Called repeatedly on the worker thread; each returned frame
        replaces the previous one (latest wins). A raised exception
        stops the loop and is exposed via :attr:`error`.
    window : int
        Number of recent frame times used for the fps estimate.
    """

    def __init__(self, grab: Callable[[], Frame], *, window: int = 30) -> None:
        self._grab = grab
        self._lock = threading.Lock()
        self._latest: Frame | None = None
        self._frame_count = 0
        self._frame_times: collections.deque[float] = collections.deque(maxlen=window)
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._error: Exception | None = None

    def start(self) -> None:
        """Start the worker thread; a no-op if the loop is already running."""
        if self.is_running:
            return
        self._stop_event.clear()
        self._error = None
        with self._lock:
            self._frame_count = 0
            self._frame_times.clear()
        self._thread = threading.Thread(
            target=self._run, name="live-acquisition", daemon=True
        )
        self._thread.start()

    def stop(self, timeout: float = 5.0) -> bool:
        """
        Signal the loop to stop and join the worker thread.

        Returns whether the worker actually finished, and callers that are
        about to touch the device themselves **must** check it. A grab
        already in flight cannot be interrupted — it is blocked inside the
        device call — so a long exposure or a slow scan can outlast
        ``timeout``. Reporting success anyway (which this used to do, by
        discarding ``join``'s outcome and clearing the thread handle
        unconditionally) told callers the device was free while the worker
        was still driving it. That is not mere contention: the client's
        shared-memory copy-out and the server's next publish would then
        overlap on one reused segment, producing a frame that is half scan
        N and half scan N+1 with no exception raised anywhere
        (docs/architecture-review.md, §1.2).

        On failure the thread handle is deliberately kept, so
        :attr:`is_running` keeps reporting the truth and a later
        :meth:`stop` can join the same worker again.

        Parameters
        ----------
        timeout : float
            Seconds to wait for the worker to finish.

        Returns
        -------
        bool
            True if the worker thread finished (or was never started).
            False if it is still running, in which case the device is
            still in use.
        """
        self._stop_event.set()
        thread = self._thread
        if thread is None:
            return True
        thread.join(timeout)
        if thread.is_alive():
            return False
        self._thread = None
        return True

    @property
    def is_running(self) -> bool:
        """Return whether the worker thread is currently alive."""
        thread = self._thread
        return thread is not None and thread.is_alive()

    @property
    def error(self) -> Exception | None:
        """Return the exception that stopped the loop, if any."""
        return self._error

    def latest(self) -> Frame | None:
        """Return the most recently grabbed frame, or None before the first one."""
        with self._lock:
            return self._latest

    @property
    def stats(self) -> LiveStats:
        """Return a snapshot of the loop's frame count and recent fps."""
        with self._lock:
            frame_times = list(self._frame_times)
            frame_count = self._frame_count
        fps = 0.0
        if len(frame_times) >= 2 and frame_times[-1] > frame_times[0]:  # noqa: PLR2004
            fps = (len(frame_times) - 1) / (frame_times[-1] - frame_times[0])
        return LiveStats(frame_count=frame_count, fps=fps)

    def _run(self) -> None:
        while not self._stop_event.is_set():
            try:
                frame = self._grab()
            except Exception as exc:  # noqa: BLE001 - surfaced to callers via .error
                self._error = exc
                return
            with self._lock:
                self._latest = frame
                self._frame_count += 1
                self._frame_times.append(time.perf_counter())


class MultiChannelLiveAcquisition(LiveAcquisition):
    """
    A live loop whose every grab reads several detectors out of one pass.

    **One loop, not one per detector, and the difference is dose.** Two
    single-channel loops would drive the scanner twice per displayed
    frame: twice the dose on the specimen, drift between the two images,
    and a pair of channels that cannot be differenced per pixel because
    the probe was not in the same place for both. That is precisely what
    :meth:`~miainwoodpecker.devices.interface.Scanner.scan_frames`
    exists to prevent, and a display built out of two loops would undo it
    while looking fine on screen.

    So the grab returns a *sequence* — the frames of one pass, in the
    order the channels were requested — and the whole sequence replaces
    the previous one under the lock. A consumer therefore never sees a
    half-updated set with HAADF from this pass and MAADF from the last.

    :attr:`stats` counts **passes**, not frames: it is the number the
    operator reads as "how fast is the scan going", and it does not
    change when they enable a second detector that costs no extra time.

    Parameters
    ----------
    grab : Callable[[], typing.Sequence[Frame]]
        Called repeatedly on the worker thread; returns one pass's
        frames. A raised exception stops the loop and is exposed via
        :attr:`error`.
    window : int
        Number of recent pass times used for the rate estimate.
    """

    def __init__(
        self,
        grab: Callable[[], typing.Sequence[Frame]],
        *,
        window: int = 30,
    ) -> None:
        super().__init__(
            typing.cast("Callable[[], Frame]", grab),
            window=window,
        )

    def latest(self) -> Frame | None:
        """
        Return the first frame of the most recent pass, or None.

        The single-frame accessor is kept working, and returns the
        *first requested channel* rather than an arbitrary one, so code
        that only wants "a frame from the scan" — a size estimate, a
        saved snapshot — behaves as it did before this class existed.

        Returns
        -------
        Frame | None
            The pass's first frame, or None before the first pass.
        """
        frames = self.latest_frames()
        return frames[0] if frames else None

    def latest_frames(self) -> tuple[Frame, ...]:
        """
        Return every frame of the most recent pass.

        Returns
        -------
        tuple[Frame, ...]
            The pass's frames in channel-request order, empty before the
            first pass completes.
        """
        with self._lock:
            latest = self._latest
        return tuple(latest) if latest else ()
