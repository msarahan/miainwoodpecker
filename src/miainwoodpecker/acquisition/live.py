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

    def stop(self, timeout: float = 5.0) -> None:
        """Signal the loop to stop and join the worker thread."""
        self._stop_event.set()
        thread = self._thread
        if thread is not None:
            thread.join(timeout)
        self._thread = None

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
