"""
Run an analysis here, or in a process that has never imported HyperSpy.

The MIT client half of the analysis isolation boundary. It imports no
analysis library — not HyperSpy, not eXSpy, not py4DSTEM, not LiberTEM —
and its availability checks resolve module *specs* rather than importing
anything, so an install with none of the extras loads this module without
error and a check costs microseconds rather than the 2.6 s (LiberTEM) to
5.2 s (py4DSTEM) a real import costs.

Two runners, one interface
--------------------------
:class:`InProcessRunner` calls
:mod:`miainwoodpecker.analysis.operations` directly, which is what the
viewer's three buttons have always done. :class:`WorkerRunner` sends the
same operation, by name, to a subprocess
(:mod:`miainwoodpecker.analysis.worker`) over the device layer's own
``Call``/``Result`` protocol, with the arrays going through the device
layer's own reused shared-memory segment. They compute the same thing
because they call the same function; the difference is the transport, and
docs/analysis-isolation.md measures exactly what that transport costs.

:func:`analysis_runner` picks between them, and **the default is
in-process**, unchanged from before this module existed. That is a
deliberate refusal to make a decision that is not this code's to make:
whether the GPL-3.0 analysis extras should be held at arm's length the
way ``docs/migration-plan.md`` §6 holds ``nion.*`` is a judgement for the
project owner, and the mechanism being ready is a different thing from
the mechanism being on. The engineering arguments for turning it on —
crash containment, a thread budget that can be set before ``import
numpy``, and dependency trees that never meet — are set out and measured
in docs/analysis-isolation.md, which also states plainly what isolation
does *not* buy.

What it does not buy, stated here too
--------------------------------------
This project's documented analysis API — ``load_as_hyperspy_signal``,
``load_as_libertem_dataset``, ``load_as_diffraction_slice`` and their
``*_from_frames`` siblings — returns **live library objects** for the
caller to go on working with. That is the point of them, and it means
they are in-process by contract: a ``Signal2D`` that has to be pickled
back across a boundary is a ``Signal2D`` the receiving interpreter needs
HyperSpy to unpickle. Nothing here changes those functions, and nothing
here should be read as removing HyperSpy from the process of anyone who
uses them.

Restarting, where the device layer deliberately does not
---------------------------------------------------------
``devices/remote.py`` refuses to reconnect to a dead device server, and
gives a good reason: a fresh server is a fresh instrument construction,
and a recording in progress would keep appending frames from
differently-configured hardware. None of that applies here. An analysis
worker holds no state that outlives one call — its input is still on disk
or still in this process's memory, and its output is the result of the
call that died — so a worker that dies costs one result and a respawn.
:meth:`WorkerRunner.run` therefore starts a new one on the next call
rather than ending the session, and the difference from §6's rule is a
difference in what is at stake, not an inconsistency.
"""

from __future__ import annotations

import contextlib
import importlib
import importlib.util
import logging
import os
import secrets
import socket
import subprocess
import sys
import threading
import time
import typing

from miainwoodpecker.analysis.operations import OPERATIONS
from miainwoodpecker.analysis.threads import analysis_thread_count
from miainwoodpecker.analysis.transfer import (
    ANALYSIS_TARGETS,
    publish_source,
    read_array,
)
from miainwoodpecker.devices.rpc import Call, disable_nagle, send_call
from miainwoodpecker.devices.shared_frame import (
    SharedArrayRef,
    SharedFrameReader,
    SharedFrameWriter,
)

if typing.TYPE_CHECKING:
    from multiprocessing.connection import Connection

    from miainwoodpecker.analysis.operations import AnalysisInput

__all__ = [
    "AnalysisRunner",
    "AnalysisWorkerError",
    "InProcessRunner",
    "WorkerRunner",
    "analysis_runner",
    "isolation_enabled",
    "open_runner",
    "target_available",
]

_LOGGER = logging.getLogger("miainwoodpecker.analysis.remote")

ISOLATION_ENV_VAR = "MIAINWOODPECKER_ANALYSIS_ISOLATION"
"""
Environment variable selecting how the viewer's analysis buttons run.

``"process"`` runs each analysis in a worker subprocess; anything else,
including unset, keeps the in-process behaviour this project has always
had. An environment variable rather than a setting in a file because it
is a deployment choice — a facility that wants the isolation wants it for
every session on that machine — and because it can be set for one run
without editing anything, which is what makes the two paths comparable
side by side.
"""

_ISOLATED = "process"

_WORKER_MODULE = "miainwoodpecker.analysis.worker"

_CONNECT_TIMEOUT_S = 30.0
"""
How long to wait for a freshly spawned worker to answer.

Generous on purpose. The worker binds its port before importing anything,
so this is not waiting on py4DSTEM's 5.2 s import — but a cold filesystem
cache on a machine that has just booted can make even the interpreter's
own startup slow, and failing a button click because a laptop was busy
would be a worse failure than waiting.
"""

_CONNECT_POLL_S = 0.02

_SHUTDOWN_TIMEOUT_S = 5.0
"""
How long a worker gets to acknowledge ``shutdown`` before it is signalled.

Shorter than the device layer's 10 s, because there is less to do: a
device server parks the instrument and closes hardware, and a worker
closes one shared-memory segment.
"""

# Set in the environment the worker is spawned with, which is the only
# place they work. analysis/threads.py explains why at length: every one
# of these is read once, when the native library loads, so setting them
# from inside a running Qt application is a no-op that looks like a fix.
# A subprocess has not loaded anything yet, so here they are the real
# knob - and unlike threadpoolctl's runtime call, the cap is a property
# of the process rather than of a window of time in a shared one.
_THREAD_ENV_VARS = (
    "OMP_NUM_THREADS",
    "OPENBLAS_NUM_THREADS",
    "MKL_NUM_THREADS",
    "NUMEXPR_NUM_THREADS",
    "NUMBA_NUM_THREADS",
)


class AnalysisWorkerError(RuntimeError):
    """
    Raised when an analysis worker could not be started or reached.

    Distinct from
    :class:`~miainwoodpecker.devices.rpc.RemoteCallError`, which means the
    worker answered and the *analysis* failed. This one means there was
    nothing to answer — and the viewer reports the two the same way, in a
    status label, because from the operator's side both are "that button
    did not work" and the message is the difference.
    """


def isolation_enabled() -> bool:
    """
    Return whether analyses should run in a worker subprocess.

    Returns
    -------
    bool
        True when :data:`ISOLATION_ENV_VAR` is set to ``"process"``.
    """
    return os.environ.get(ISOLATION_ENV_VAR, "").strip().lower() == _ISOLATED


def target_available(name: str) -> bool:
    """
    Return whether a target's optional extra is installed, without importing it.

    ``importlib.util.find_spec`` on a top-level name resolves the finder's
    answer without executing the module, which is what makes this usable
    from a button handler: importing py4DSTEM to discover that py4DSTEM is
    installed would freeze the GUI for five seconds to answer a question
    with a cheap answer.

    Parameters
    ----------
    name : str
        A key of
        :data:`~miainwoodpecker.analysis.transfer.ANALYSIS_TARGETS`.

    Returns
    -------
    bool
        True when the target's library can be imported.
    """
    module = ANALYSIS_TARGETS[name].probe_module
    try:
        return importlib.util.find_spec(module) is not None
    except (ImportError, ValueError):
        # A namespace package left half-installed, or a finder that raises
        # rather than returning None. Either way the honest answer is "no",
        # and the button says "install the extra" instead of crashing.
        return False


class AnalysisRunner(typing.Protocol):
    """
    What the viewer needs from either transport.

    One method and a teardown. The viewer's three button handlers are
    written against this rather than against a module, which is the whole
    reason both paths could be offered without duplicating a handler.
    """

    def run(self, method: str, job: AnalysisInput) -> object:
        """
        Run one named analysis and return its plain-data result.

        Parameters
        ----------
        method : str
            A key of
            :data:`~miainwoodpecker.analysis.operations.OPERATIONS`.
        job : AnalysisInput
            The file or frames to analyse.

        Returns
        -------
        object
            Arrays and numbers, never a library object.
        """
        ...  # pragma: no cover - protocol

    def close(self) -> None:
        """Release whatever this runner holds. Safe to call twice."""
        ...  # pragma: no cover - protocol


class InProcessRunner:
    """
    Run analyses in this interpreter, exactly as the buttons always have.

    The default, and a real implementation rather than a fallback stub: it
    is what every existing test exercises and what
    ``docs/scripting-and-automation.md`` describes.
    """

    def run(self, method: str, job: AnalysisInput) -> object:
        """
        Call the named operation directly.

        Parameters
        ----------
        method : str
            A key of
            :data:`~miainwoodpecker.analysis.operations.OPERATIONS`.
        job : AnalysisInput
            The file or frames to analyse.

        Returns
        -------
        object
            Whatever the operation returned.
        """
        return OPERATIONS[method](job)

    def close(self) -> None:
        """Do nothing; this runner owns no process and no segment."""


class WorkerRunner:
    """
    Run analyses in a subprocess that owns one analysis library.

    Long-lived rather than one process per job, and the reason is
    measured: a cold ``import py4DSTEM`` costs 5.2 s and ``import
    libertem.api`` 2.6 s in this environment, so a worker per click would
    put that in front of every result. Spawned lazily on the first call
    instead, so a session that never touches an analysis button never
    pays it at all.

    Not thread-safe and deliberately not made so. The viewer runs one
    analysis at a time — a second click while one is running is refused,
    not queued — and that is also the invariant the reused shared-memory
    segment depends on. A lock here would make the class look safe for a
    concurrency the segment could not actually survive.

    Parameters
    ----------
    name : str
        The target to serve, a key of
        :data:`~miainwoodpecker.analysis.transfer.ANALYSIS_TARGETS`.
    cpu_count : int | None
        Cores to size the worker's thread budget against; defaults to the
        machine's. See
        :func:`~miainwoodpecker.analysis.threads.analysis_thread_count`.
    """

    def __init__(self, name: str, cpu_count: int | None = None) -> None:
        self._name = name
        self._threads = analysis_thread_count(cpu_count)
        self._process: subprocess.Popen[bytes] | None = None
        self._connection: Connection | None = None
        self._lock = threading.Lock()
        self._writer = SharedFrameWriter()
        self._reader = SharedFrameReader()

    @property
    def target(self) -> str:
        """Return the analysis target this runner serves."""
        return self._name

    @property
    def thread_budget(self) -> int:
        """Return the thread cap this runner's worker is started with."""
        return self._threads

    def run(self, method: str, job: AnalysisInput) -> object:
        """
        Send one named analysis to the worker and return its result.

        Starts the worker if it is not running, which covers both the
        first call and a worker that died in a previous one — the
        restart-rather-than-fail choice this module's docstring explains.

        Parameters
        ----------
        method : str
            A key of
            :data:`~miainwoodpecker.analysis.operations.OPERATIONS`.
        job : AnalysisInput
            The file or frames to analyse.

        Returns
        -------
        object
            The same arrays and numbers the in-process runner returns,
            with any that travelled through shared memory already copied
            out.

        Raises
        ------
        Exception
            Re-raised unchanged after the worker is retired, because a
            caller that asked for an analysis wants the analysis's own
            error: an :class:`AnalysisWorkerError` when the worker could
            not be started or reached, a
            :class:`~miainwoodpecker.devices.rpc.RemoteCallError` naming
            the worker-side exception when the analysis itself failed, and
            a :class:`~miainwoodpecker.devices.rpc.RemoteConnectionLostError`
            when it died mid-call.
        """
        try:
            connection = self._ensure_started()
            value = send_call(
                connection,
                self._lock,
                Call(
                    target=self._name,
                    method=method,
                    args=(publish_source(self._writer, job),),
                ),
            )
        except Exception:
            # Whatever went wrong, this worker's connection is no longer
            # trustworthy: a lost connection means it died, and a failed
            # analysis leaves its segment in a state only it knows. The
            # next call gets a fresh process, which is exactly the
            # restart policy this class exists to make cheap.
            self.close()
            raise
        return self._resolve(value)

    def close(self) -> None:
        """
        Shut the worker down, gracefully if it will and by signal if not.

        Safe to call on a runner that never started one, and safe to call
        twice — which matters because :meth:`run` calls it on any failure
        and the viewer calls it again on teardown.
        """
        connection, process = self._connection, self._process
        self._connection, self._process = None, None
        if connection is not None:
            with contextlib.suppress(Exception):
                send_call(
                    connection,
                    self._lock,
                    Call(target=self._name, method="shutdown"),
                    timeout_s=_SHUTDOWN_TIMEOUT_S,
                )
            with contextlib.suppress(OSError):
                connection.close()
        if process is not None:
            self._reap(process)
        # Our own segment, not the worker's: this side publishes requests,
        # so this side unlinks them. The worker's result segment is the
        # worker's to unlink, which its shutdown handler does - and if it
        # died instead, its resource_tracker child does, for the reason
        # devices/shared_frame.py measured.
        self._writer.close()
        self._reader.close()

    def _reap(self, process: subprocess.Popen[bytes]) -> None:
        """
        Wait briefly for a worker to exit, then insist.

        Parameters
        ----------
        process : subprocess.Popen[bytes]
            The worker process.
        """
        try:
            process.wait(timeout=_SHUTDOWN_TIMEOUT_S)
        except subprocess.TimeoutExpired:
            process.terminate()
            try:
                process.wait(timeout=_SHUTDOWN_TIMEOUT_S)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()

    def _resolve(self, value: object) -> object:
        """
        Copy any shared-memory references in a result back into this process.

        Parameters
        ----------
        value : object
            What the worker returned: an array, a reference, or a tuple
            whose first element is one of those.

        Returns
        -------
        object
            The same shape with every reference replaced by an array this
            process owns.
        """
        if isinstance(value, SharedArrayRef):
            return read_array(self._reader, value)
        if isinstance(value, tuple):
            return tuple(self._resolve(item) for item in value)
        return value

    def _ensure_started(self) -> Connection:
        """
        Return a live connection to the worker, starting one if needed.

        Returns
        -------
        Connection
            The connection to send calls on.

        Raises
        ------
        AnalysisWorkerError
            If the worker could not be started or could not be reached.
        """
        if self._connection is not None and self._process is not None:
            if self._process.poll() is None:
                return self._connection
            _LOGGER.warning(
                "analysis worker %s exited with status %s; starting another",
                self._name,
                self._process.returncode,
            )
            self.close()
        port = _free_port()
        authkey = secrets.token_bytes(32)
        process = self._spawn(port, authkey)
        try:
            connection = _connect_with_retry(port, authkey, process)
        except AnalysisWorkerError:
            process.kill()
            process.wait()
            raise
        self._process, self._connection = process, connection
        return connection

    def _spawn(self, port: int, authkey: bytes) -> subprocess.Popen[bytes]:
        """
        Launch the worker subprocess with its thread budget already set.

        Parameters
        ----------
        port : int
            The port the worker should bind.
        authkey : bytes
            Shared secret for the listener.

        Returns
        -------
        subprocess.Popen[bytes]
            The running worker.
        """
        env = {
            **os.environ,
            "MIAINWOODPECKER_AUTHKEY": authkey.hex(),
            **{name: str(self._threads) for name in _THREAD_ENV_VARS},
        }
        return subprocess.Popen(  # noqa: S603 - fixed argv shape, no shell
            [sys.executable, "-m", _WORKER_MODULE, self._name, str(port)],
            env=env,
            # Its own process group, for the reason devices/remote.py
            # gives: a Ctrl-C in the launching terminal must not race this
            # process's own teardown, and a process-group kill is the one
            # case the resource_tracker cannot clean up after.
            start_new_session=True,
        )


def _free_port() -> int:
    """
    Return a localhost port that was free a moment ago.

    Returns
    -------
    int
        A port number. Racy by nature — the worker exits with
        :data:`~miainwoodpecker.analysis.worker.PORT_UNAVAILABLE_EXIT_STATUS`
        if it loses the race, and the caller's connect attempt then fails
        with a message naming that.
    """
    with socket.socket() as probe:
        probe.bind(("localhost", 0))
        return int(probe.getsockname()[1])


def _connect_with_retry(
    port: int,
    authkey: bytes,
    process: subprocess.Popen[bytes],
) -> Connection:
    """
    Connect to a starting worker, retrying until it binds or dies.

    Written here rather than reused from
    :mod:`miainwoodpecker.devices.remote`, whose version is welded to that
    module's ``_ServerLifecycle`` and to device-specific diagnostics
    ("the instrument was not parked", a plug-in list) that would be
    misleading in an analysis message. The protocol it speaks is the same
    one; only the sentence it produces on failure differs.

    Parameters
    ----------
    port : int
        The port the worker was told to bind.
    authkey : bytes
        The shared secret it was given.
    process : subprocess.Popen[bytes]
        The worker, so an exit can be reported instead of waited out.

    Returns
    -------
    Connection
        A connection with Nagle disabled, ready for
        :func:`~miainwoodpecker.devices.rpc.send_call`.

    Raises
    ------
    AnalysisWorkerError
        If the worker exited, or never answered within
        :data:`_CONNECT_TIMEOUT_S`.
    """
    from multiprocessing.connection import Client  # noqa: PLC0415 - see nion_server

    deadline = time.monotonic() + _CONNECT_TIMEOUT_S
    last_error: OSError | None = None
    while time.monotonic() < deadline:
        status = process.poll()
        if status is not None:
            msg = (
                f"the analysis worker exited with status {status} before "
                f"accepting a connection on port {port}; run "
                f"'{sys.executable} -m {_WORKER_MODULE}' by hand, or set "
                f"MIAINWOODPECKER_ANALYSIS_LOG_LEVEL=INFO, to see why"
            )
            raise AnalysisWorkerError(msg)
        try:
            connection = Client(("localhost", port), authkey=authkey)
        except OSError as error:
            last_error = error
            time.sleep(_CONNECT_POLL_S)
            continue
        disable_nagle(connection)
        return connection
    detail = f" ({last_error})" if last_error is not None else ""
    msg = (
        f"the analysis worker did not accept a connection on port {port} "
        f"within {_CONNECT_TIMEOUT_S}s{detail}"
    )
    raise AnalysisWorkerError(msg)


def analysis_runner(name: str, cpu_count: int | None = None) -> AnalysisRunner:
    """
    Return the runner this installation is configured to use.

    Parameters
    ----------
    name : str
        The analysis target, a key of
        :data:`~miainwoodpecker.analysis.transfer.ANALYSIS_TARGETS`.
    cpu_count : int | None
        Cores to size an isolated worker's thread budget against; ignored
        by the in-process runner, which is capped at run time by
        :func:`~miainwoodpecker.analysis.threads.limit_analysis_threads`
        instead.

    Returns
    -------
    AnalysisRunner
        A :class:`WorkerRunner` when :func:`isolation_enabled`, and an
        :class:`InProcessRunner` otherwise — the default, unchanged from
        before this module existed. See docs/analysis-isolation.md for
        what turning it on costs and buys.
    """
    if isolation_enabled():
        return WorkerRunner(name, cpu_count)
    return InProcessRunner()


def open_runner(name: str, cpu_count: int | None = None) -> AnalysisRunner:
    """
    Return a runner for a target, refusing if its extra is not installed.

    The refusal is an ``ImportError`` because that is what the viewer's
    three button handlers have always caught, and the point of routing
    both paths through here is that they keep catching exactly one thing
    and printing exactly the message they printed before.

    How the check is made differs between the two paths, deliberately.
    In-process it is the *same imports the handler used to do itself*
    (:attr:`~miainwoodpecker.analysis.transfer.AnalysisTarget.eager_modules`),
    so nothing about that path's behaviour moved — including the case
    where a library is installed but broken, which raises here exactly as
    it raised in the handler. Isolated, it is
    :func:`target_available`, which resolves a module spec without
    executing anything: importing py4DSTEM in the GUI process to find out
    whether the *worker* can import it would defeat the entire exercise.

    Parameters
    ----------
    name : str
        The analysis target.
    cpu_count : int | None
        Cores to size an isolated worker's thread budget against.

    Returns
    -------
    AnalysisRunner
        A runner ready to accept :meth:`AnalysisRunner.run`.

    Raises
    ------
    ImportError
        If the target's optional extra is not installed.
    """
    target = ANALYSIS_TARGETS[name]
    runner = analysis_runner(name, cpu_count)
    if isinstance(runner, WorkerRunner):
        if not target_available(name):
            msg = (
                f"the {target.extra!r} extra is not installed, so no "
                f"{name} analysis worker can be started"
            )
            raise ImportError(msg)
        return runner
    for module in target.eager_modules:
        importlib.import_module(module)
    return runner
