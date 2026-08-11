"""
Run an analysis off the GUI thread.

The recording and loading paths already refuse to block the GUI
(:class:`~miainwoodpecker.storage.session.RecordingJob`,
:class:`~miainwoodpecker.storage.session.LoadJob`); the Phase 4 analysis
buttons were the one remaining handler that did its whole job inline. That
work is not small: each button acquires a burst from the camera, writes it
to a NeXus file, reads it back through an adapter, and runs a real
HyperSpy/LiberTEM/py4DSTEM operation over it. For the duration the window
does not repaint, the live feed freezes, and "Stop scan" does not answer.

This is deliberately *not* another bespoke job class. It has
:class:`LoadJob`'s exact shape — a daemon thread, state behind a lock,
exceptions captured rather than raised, and **no Qt anywhere** — but where
those two each own a specific piece of work, this one takes a callable,
because the three analysis buttons differ in what they compute while being
identical in how they need to be run.

Why no Qt: the same reason
:class:`~miainwoodpecker.storage.session.LoadJob` gives. napari's
``thread_worker`` reports completion through Qt signals, which would make
the widget tests depend on driving a Qt event loop — the thing
``tests/integration/test_live_widget.py`` avoids by calling
``refresh_display`` directly. The caller polls this from its own display
timer instead, exactly as it already polls the other two.

The split that makes this safe is the caller's responsibility, not this
class's: everything that reads or writes a widget stays on the GUI thread,
and only the pure compute is handed over. See
``LiveInstrumentWidget._start_analysis``, which reads the operator's
choices *before* starting a job and defers every layer and label update to
``_poll_analysis``.
"""

from __future__ import annotations

import threading
import typing

if typing.TYPE_CHECKING:
    from collections.abc import Callable

_JOIN_TIMEOUT_S = 30.0


class AnalysisJob:
    """
    Run one analysis callable on a worker thread.

    Parameters
    ----------
    work : Callable[[], object]
        The analysis to run. Called once, with no arguments, on the worker
        thread. It must not touch Qt: whatever it returns is handed back to
        the GUI thread through :attr:`result` for display there.
    """

    def __init__(self, work: Callable[[], object]) -> None:
        self._work = work
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._result: object | None = None
        self._error: Exception | None = None

    def start(self) -> None:
        """Start the worker thread; a no-op if it is already running."""
        if self.is_running:
            return
        self._thread = threading.Thread(target=self._run, name="analysis", daemon=True)
        self._thread.start()

    def join(self, timeout: float = _JOIN_TIMEOUT_S) -> None:
        """
        Wait for the worker thread to finish.

        Parameters
        ----------
        timeout : float
            Seconds to wait before giving up.
        """
        thread = self._thread
        if thread is not None:
            thread.join(timeout)

    @property
    def is_running(self) -> bool:
        """Return whether the worker thread is still going."""
        thread = self._thread
        return thread is not None and thread.is_alive()

    @property
    def result(self) -> object | None:
        """Return what the callable returned, or None if it raised or is unfinished."""
        with self._lock:
            return self._result

    @property
    def error(self) -> Exception | None:
        """Return the exception the callable raised, if it raised one."""
        with self._lock:
            return self._error

    def _run(self) -> None:
        """Run the callable, capturing its result or its exception."""
        try:
            value = self._work()
        except Exception as exc:  # noqa: BLE001 - surfaced via .error on the GUI thread
            with self._lock:
                self._error = exc
        else:
            with self._lock:
                self._result = value
