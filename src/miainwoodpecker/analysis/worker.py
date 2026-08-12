"""
The analysis-library half of the isolation boundary.

This is the module that imports HyperSpy, eXSpy, py4DSTEM or LiberTEM —
one of them, in its own process, chosen by the target it is asked to
serve. :mod:`miainwoodpecker.analysis.remote` is the client, and
:mod:`miainwoodpecker.analysis.transfer` is what goes between them. Run
only via ``python -m miainwoodpecker.analysis.worker <target> <port>``;
nothing in the application imports it.

**On the licence, precisely and without overclaiming.** HyperSpy, eXSpy,
RosettaSciIO and py4DSTEM are GPL-3.0 (LiberTEM is MIT); this project is
MIT, and MIT is GPL-compatible, so nothing here is *forbidden* in either
arrangement — the question is only what licence governs a combination
when one is conveyed. This module lets the viewer's analysis buttons run
without those libraries ever being imported into the application's own
interpreter, which is the same shape
[docs/migration-plan.md §6](../../../docs/migration-plan.md) chose for the
device layer. It does **not** settle the licence question, and
docs/analysis-isolation.md is explicit about why: this project's
*documented* analysis API (``load_as_hyperspy_signal`` and its siblings)
returns live library objects to the caller's own process by design, and
no process boundary can change that without destroying what the API is
for. The isolation here is worth having for crash containment, thread
control and dependency separation on its own terms; treating it as a
licence answer would be claiming more than it does.

Why this is not shaped like a device server
-------------------------------------------
It reuses the device layer's protocol wholesale — ``Call``/``Result``,
:func:`~miainwoodpecker.devices.serving.accept_loop`,
:func:`~miainwoodpecker.devices.serving.serve_connection`, the reused
shared-memory segment — and differs in four ways that all follow from
what an analysis *is*:

- **One target per process, and a target is a library.** A device server
  serves several devices because they are one instrument. Three analysis
  libraries in one interpreter would be three dependency trees in one
  resolver, which is the thing the three separate extras exist to
  prevent.
- **Bulk in both directions**, so this side has a reader as well as a
  writer. See :mod:`miainwoodpecker.analysis.transfer`.
- **Its thread budget is set before it starts.**
  :mod:`miainwoodpecker.analysis.threads` says plainly that
  ``OMP_NUM_THREADS`` and friends are read once when the native library
  loads, which is why the in-process cap has to be a runtime
  ``threadpoolctl`` call scoped in *time* rather than to a thread. A
  fresh process has no such constraint: the client sets those variables
  in the environment it spawns with, before ``import numpy`` happens at
  all, and the cap is then a property of the process. That is the one
  limitation ``threads.py`` documents and cannot fix, fixed.
- **Dying is survivable, so the client restarts it.**
  ``remote.py``'s teardown deliberately does not reconnect, because a
  fresh device server is a fresh instrument construction and a recording
  in progress would silently continue against differently-configured
  hardware. An analysis worker holds no state that matters: its input is
  still on disk or still in the client's memory, and its only output is
  the result of the call that died. So a dead worker costs one result and
  a respawn, and :mod:`miainwoodpecker.analysis.remote` does exactly
  that. The inversion is deliberate and its precondition is stated here
  so it is not read as an inconsistency.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import threading
import typing

from miainwoodpecker.analysis import operations
from miainwoodpecker.analysis.transfer import (
    ANALYSIS_TARGETS,
    publish_array,
    read_source,
)
from miainwoodpecker.devices.serving import accept_loop
from miainwoodpecker.devices.shared_frame import SharedFrameReader, SharedFrameWriter

if typing.TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    from miainwoodpecker.analysis.operations import AnalysisInput
    from miainwoodpecker.analysis.transfer import AnalysisSource

_LOGGER = logging.getLogger("miainwoodpecker.analysis.worker")

PORT_UNAVAILABLE_EXIT_STATUS = 3
"""
Exit status when the worker's listener cannot bind its port.

The same number and the same meaning as the device servers use, so a
client that retries with a fresh port reads one convention rather than
two.
"""


class AnalysisWorkerTarget:
    """
    The object an analysis worker's calls dispatch against.

    One per worker process, holding the reader and writer that make up
    this side of the transport plus the operations the target library can
    run. Deliberately a plain object with plain methods, because
    :func:`~miainwoodpecker.devices.serving.invoke` dispatches by
    ``getattr`` — the same rule the device targets are written to, so this
    needed no protocol of its own.

    Parameters
    ----------
    name : str
        The target being served, one of
        :data:`~miainwoodpecker.analysis.transfer.ANALYSIS_TARGETS`.
    stop_event : threading.Event
        Set when :meth:`shutdown` is acknowledged, letting :func:`serve`
        return so the process exits.
    """

    def __init__(self, name: str, stop_event: threading.Event) -> None:
        self._name = name
        self._stop = stop_event
        # stop_tracking, and it is load-bearing rather than tidy: this
        # reader attaches to a segment the *client* created and still
        # owns, and without it this process's resource_tracker unlinks
        # that live segment when this process exits. See
        # SharedFrameReader for the measurement that found it.
        self._reader = SharedFrameReader(stop_tracking=True)
        self._writer = SharedFrameWriter()

    def health(self) -> dict[str, object]:
        """
        Answer "is this worker alive?" without importing anything.

        Reads process state only — no library, no array, no lock — for the
        reason ``nion_server``'s own health check does: it must not queue
        behind an analysis that is already running, and it must not be the
        thing that pays a 5.2 s import.

        Returns
        -------
        dict[str, object]
            The worker's target name, pid, and which analysis libraries it
            has actually imported so far.
        """
        return {
            "ok": True,
            "pid": os.getpid(),
            "target": self._name,
            "imported": sorted(
                target.probe_module
                for target in ANALYSIS_TARGETS.values()
                if target.probe_module in sys.modules
            ),
        }

    def shutdown(self) -> dict[str, object]:
        """
        Release this worker's shared segment and let the process exit.

        Returns
        -------
        dict[str, object]
            A plain-data report, in the same shape the device servers
            reply with so one teardown path reads both.
        """
        errors: list[str] = []
        for closable in (self._writer, self._reader):
            try:
                closable.close()
            except Exception as error:  # noqa: BLE001 - reported, not raised
                errors.append(f"{type(error).__name__}: {error}")
        self._stop.set()
        return {"target": self._name, "errors": errors}

    def mean_projection(self, source: AnalysisSource) -> object:
        """
        Run :func:`~miainwoodpecker.analysis.operations.mean_projection`.

        Parameters
        ----------
        source : AnalysisSource
            The file or frames to analyse.

        Returns
        -------
        object
            The mean frame, as a shared-memory reference or inline.
        """
        return self._array_result(operations.mean_projection, source)

    def sum_projection(self, source: AnalysisSource) -> object:
        """
        Run :func:`~miainwoodpecker.analysis.operations.sum_projection`.

        The executor is left to size itself against this process's own
        core count, which is the whole point of running here: the
        environment this process started with already carries the cap, so
        LiberTEM's ``psutil``-derived default is bounded by the
        environment rather than by a runtime override.

        Parameters
        ----------
        source : AnalysisSource
            The file or frames to analyse.

        Returns
        -------
        object
            The summed frame, as a shared-memory reference or inline.
        """
        return self._array_result(operations.sum_projection, source)

    def fit_central_disk(self, source: AnalysisSource) -> object:
        """
        Run :func:`~miainwoodpecker.analysis.operations.fit_central_disk`.

        The one operation whose result is not a bare array, so it is the
        one that does not go through :meth:`_array_result`: only the
        pattern is worth a segment, and the three fitted numbers ride the
        pickle channel they were always small enough for.

        Parameters
        ----------
        source : AnalysisSource
            The file or frames to analyse.

        Returns
        -------
        object
            ``(pattern, radius, x0, y0)`` with the pattern routed through
            shared memory when it is large enough to be worth it.
        """
        pattern, radius, x0, y0 = operations.fit_central_disk(
            read_source(self._reader, source),
        )
        return (publish_array(self._writer, pattern), radius, x0, y0)

    def _array_result(
        self,
        operation: Callable[[AnalysisInput], typing.Any],
        source: AnalysisSource,
    ) -> object:
        """
        Unpack a source, run one array-returning operation, publish the result.

        Parameters
        ----------
        operation : Callable[[AnalysisInput], typing.Any]
            The function in :mod:`miainwoodpecker.analysis.operations` to
            run.
        source : AnalysisSource
            What the client sent.

        Returns
        -------
        object
            A shared-memory reference, or the array itself when it is
            below the threshold.
        """
        return publish_array(
            self._writer,
            operation(read_source(self._reader, source)),
        )


def serve(name: str, port: int, authkey: bytes) -> None:
    """
    Bind one port for one target and serve it until told to stop.

    Parameters
    ----------
    name : str
        The target to serve.
    port : int
        The localhost port to bind.
    authkey : bytes
        Shared secret for the listener.

    Raises
    ------
    SystemExit
        With :data:`PORT_UNAVAILABLE_EXIT_STATUS` when the listener cannot
        bind, so the client can retry with a fresh port — the same
        contract the device servers offer.
    """
    from multiprocessing.connection import Listener  # noqa: PLC0415 - see nion_server

    stop_event = threading.Event()
    target = AnalysisWorkerTarget(name, stop_event)
    try:
        listener = Listener(("localhost", port), authkey=authkey)
    except OSError as error:
        _LOGGER.error(  # noqa: TRY400 - a traceback adds nothing here
            "could not bind port %s (%s); the client will retry",
            port,
            error,
        )
        raise SystemExit(PORT_UNAVAILABLE_EXIT_STATUS) from error

    _LOGGER.info("serving analysis target %s on port %s", name, port)
    # The writer is handed to accept_loop for its exclusivity check, not
    # for its publishing: this target publishes its own results, because
    # they are bare arrays rather than Frames and `invoke` only knows the
    # latter. What the check buys is that a second client cannot
    # interleave calls and overwrite a segment the first is still copying
    # out of - the same invariant, enforced by the same code.
    threading.Thread(
        target=accept_loop,
        args=(listener, name, target, SharedFrameWriter(), stop_event),
        daemon=True,
    ).start()
    stop_event.wait()
    listener.close()


def _parse_args(argv: Sequence[str]) -> argparse.Namespace:
    """
    Parse the worker's command line.

    Parameters
    ----------
    argv : Sequence[str]
        Arguments after the program name.

    Returns
    -------
    argparse.Namespace
        With ``target`` and ``port``.
    """
    parser = argparse.ArgumentParser(
        description="Serve one analysis library over the miainwoodpecker protocol.",
    )
    parser.add_argument("target", choices=sorted(ANALYSIS_TARGETS))
    parser.add_argument("port", type=int)
    return parser.parse_args(list(argv))


def main(argv: Sequence[str] | None = None) -> int:
    """
    Run the worker from the command line.

    Parameters
    ----------
    argv : Sequence[str] | None
        Arguments after the program name, or None to read ``sys.argv``.

    Returns
    -------
    int
        Process exit status.
    """
    logging.basicConfig(
        level=os.environ.get("MIAINWOODPECKER_ANALYSIS_LOG_LEVEL", "WARNING"),
        format="%(asctime)s %(levelname)s %(name)s [%(process)d]: %(message)s",
    )
    arguments = _parse_args(sys.argv[1:] if argv is None else argv)
    serve(
        arguments.target,
        arguments.port,
        bytes.fromhex(os.environ["MIAINWOODPECKER_AUTHKEY"]),
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
