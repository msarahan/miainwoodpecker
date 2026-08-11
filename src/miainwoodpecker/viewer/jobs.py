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

import typing

from miainwoodpecker.jobs import BackgroundJob

if typing.TYPE_CHECKING:
    from collections.abc import Callable


class AnalysisJob(BackgroundJob):
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
        super().__init__("analysis")
        self._callable = work

    @property
    def result(self) -> object | None:
        """Return what the callable returned, or None if it raised or is unfinished."""
        with self._lock:
            return self._raw_result

    def _work(self) -> object:
        """
        Run the caller's analysis on the worker thread.

        Returns
        -------
        object
            Whatever the analysis produced, for the GUI thread to draw.
        """
        return self._callable()
