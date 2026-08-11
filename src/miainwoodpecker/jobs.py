"""
One shape for "run this off the GUI thread and tell me how it went".

Three classes had grown the same body independently —
:class:`~miainwoodpecker.storage.session.RecordingJob`,
:class:`~miainwoodpecker.storage.session.LoadJob`, and
:class:`~miainwoodpecker.viewer.jobs.AnalysisJob` — each a daemon thread
with state behind a lock, exceptions captured rather than raised, and no
Qt anywhere. Three copies of a threading contract is three chances to
diverge, and they had: two declared their own ``_JOIN_TIMEOUT_S``, and
:meth:`join` returned ``None`` whether the worker finished or the timeout
expired, so a caller polling afterwards could not distinguish "done, and
it produced nothing" from "still running".

This module holds the shared half. It lives at the package root rather
than in ``storage`` or ``viewer`` because both use it and neither should
import the other — the session layer's jobs are deliberately outside the
viewer package precisely so they *cannot* reach a Qt object.

:class:`~miainwoodpecker.acquisition.live.LiveAcquisition` deliberately
does **not** derive from this. It is a loop with a latest-frame-wins
buffer and a stop event, not a one-shot with a result; forcing it into
this shape would mean widening the base until it described neither well.
"""

from __future__ import annotations

import threading
import typing

if typing.TYPE_CHECKING:
    from collections.abc import Callable

JOIN_TIMEOUT_S = 30.0
"""
Default seconds :meth:`BackgroundJob.join` waits.

Generous because what these jobs wait on is real I/O — writing a
compressed frame stack, decompressing tens of megabytes, running an
analysis — and a join that gave up early would leave the thread running
anyway. Shutdown paths that cannot afford this should pass a shorter
timeout and check the result.
"""


class BackgroundJob:
    """
    Run one unit of work on a daemon thread, capturing how it ended.

    Subclasses supply the work and a typed view of the result; everything
    about *running* it lives here.

    The contract, stated once so the three subclasses cannot drift:

    - :meth:`start` is a no-op while the job is running, and starting a
      finished job runs it again.
    - :meth:`join` returns whether the worker actually finished. Callers
      that act on the outcome must check it, because a timed-out join
      leaves the thread running and the result still absent.
    - :attr:`error` and :attr:`result` are both read under the lock, and
      exactly one of them is set once the job has finished.
    - Nothing here touches Qt, and nothing here may.
    """

    def __init__(self, name: str) -> None:
        self._job_name = name
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._raw_result: object | None = None
        self._error: Exception | None = None

    def _work(self) -> object:
        """
        Do the job's actual work on the worker thread.

        Returns
        -------
        object
            Whatever the job produced, exposed through the subclass's
            own typed ``result`` property.

        Raises
        ------
        NotImplementedError
            If a subclass does not override this.
        """
        raise NotImplementedError

    def start(self) -> None:
        """Start the worker thread; a no-op if the job is already running."""
        if self.is_running:
            return
        with self._lock:
            self._raw_result = None
            self._error = None
        self._reset()
        self._thread = threading.Thread(
            target=self._run,
            name=self._job_name,
            daemon=True,
        )
        self._thread.start()

    def _reset(self) -> None:
        """
        Clear any per-run state a subclass keeps, before a fresh start.

        Exists because ``RecordingJob`` kept a ``_cancelled`` flag that
        :meth:`start` never cleared, so a cancelled-then-restarted job
        stopped on its first frame and wrote an empty file.
        """

    def join(self, timeout: float = JOIN_TIMEOUT_S) -> bool:
        """
        Wait for the worker thread to finish.

        Parameters
        ----------
        timeout : float
            Seconds to wait before giving up.

        Returns
        -------
        bool
            True if the worker finished (or was never started). False if
            it is still running, in which case :attr:`result` and
            :attr:`error` are both still absent and whatever the job
            holds — a device, a file handle — is still held.
        """
        thread = self._thread
        if thread is None:
            return True
        thread.join(timeout)
        return not thread.is_alive()

    @property
    def is_running(self) -> bool:
        """Return whether the worker thread is still going."""
        thread = self._thread
        return thread is not None and thread.is_alive()

    @property
    def error(self) -> Exception | None:
        """Return the exception the work raised, if it raised one."""
        with self._lock:
            return self._error

    def _run(self) -> None:
        """Run the work, capturing its result or its exception."""
        try:
            value = self._work()
        except Exception as exc:  # noqa: BLE001 - surfaced to the caller via .error
            with self._lock:
                self._error = exc
        else:
            with self._lock:
                self._raw_result = value


def run_in_background(work: Callable[[], object], name: str) -> BackgroundJob:
    """
    Wrap a callable as a started :class:`BackgroundJob`.

    Parameters
    ----------
    work : Callable[[], object]
        The work to run once, on the worker thread. Must not touch Qt.
    name : str
        Thread name, for debuggers and stack dumps.

    Returns
    -------
    BackgroundJob
        The job, already started.
    """

    class _CallableJob(BackgroundJob):
        def _work(self) -> object:
            return work()

    job = _CallableJob(name)
    job.start()
    return job
