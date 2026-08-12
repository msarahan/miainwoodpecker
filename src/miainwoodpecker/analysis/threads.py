"""
Leave the GUI thread somewhere to run while an analysis is going.

**Measured, not assumed** (docs/migration-plan.md, Phase 2).
``scripts/phase2_live_benchmark.py --source camera --load N`` runs N
CPU-saturating numpy workers beside the live display. On an M2 Pro,
512x512 camera frames, the worst single display update went 516 ms at
zero competitors to 649 ms at four to **4031 ms** at eight, and the
median followed only at the end (5.6, 3.1, 17.8 ms — it briefly
*improves* under moderate load, because the background work holds the CPU
in a high-performance state an idle machine drops out of).

Acquire does not move across the same sweep (5.5 -> 5.7 ms median, a 3%
spread): the device server is a separate process and the client's side of
a grab is IPC plus a shared-memory read, not computation, so contention
lands on display alone. Four seconds is not a slow repaint — it is the Qt
main thread descheduled outright, which no amount of per-update
efficiency addresses. The four-competitor row is the one to design for:
the tail triples (p95 7.6 -> 23.0 ms) while the machine stays usable.

**The fan-out is not ours.** :mod:`miainwoodpecker.viewer.jobs` already
runs one :class:`~miainwoodpecker.viewer.jobs.AnalysisJob` at a time on
one worker thread. The threads that take the whole machine live *inside*
that one job, and each library brings its own:

- numpy's BLAS — OpenBLAS, MKL, or Accelerate depending on the wheel —
  sizes its thread pool to the core count when the shared library loads.
- LiberTEM's inline executor asks for ``psutil.cpu_count(logical=False)``
  fine-grained threads per task (``InlineJobExecutor._get_threads``), and
  applies that to numba, pyfftw, torch and the BLAS pools around every
  UDF partition (``libertem.common.threading.set_num_threads``). "Inline"
  bounds the *executor*, not the numerics under it.
- HyperSpy computes through dask and scipy, both of which land back on
  the same BLAS pools.

So the actionable form of the measurement is a cap on those inner
threads: :data:`GUI_RESERVED_CORES` cores stay out of analysis's reach,
and :func:`analysis_thread_count` is the one place that arithmetic lives
— derived from the machine rather than hardcoded, and floored at one
thread so a two-core laptop gets a slow analysis rather than a stalled
one.

Why ``threadpoolctl`` and not ``OMP_NUM_THREADS``
-------------------------------------------------
``OMP_NUM_THREADS``, ``OPENBLAS_NUM_THREADS`` and ``MKL_NUM_THREADS`` are
read **once, when the native library loads**, which for numpy is at
``import numpy``. Setting them from inside a Qt application — after
numpy, napari, and h5py are all long since imported — is not a bounded
version of this fix; it is a no-op that looks like one. ``threadpoolctl``
calls each library's own runtime setter (``openblas_set_num_threads``,
``mkl_set_num_threads``, ``omp_set_num_threads``) instead, which is the
knob that still works after load, and restores the previous limits when
the block exits. LiberTEM reaches for the same library internally, for
the same reason.

It is a dependency of the ``libertem`` and ``py4dstem`` extras already
(via LiberTEM itself, py4DSTEM itself, and scikit-learn), so it is new
weight only for the ``analysis`` extra, where it is one pure-Python wheel
with no dependencies of its own.

What this does **not** claim
----------------------------
The limit is scoped in *time*, not to a thread. ``openblas_set_num_threads``
and ``mkl_set_num_threads`` are process-global knobs, so while an analysis
runs the cap applies to whoever calls into BLAS, and it is lifted when the
job finishes. That is the strongest scoping the libraries offer, and it
costs nothing here: the GUI thread's own work is Qt repaints and napari's
per-update bookkeeping, not BLAS calls. Nothing is set process-wide for
the lifetime of the application, and no environment variable is written.

Numba's thread pool is not one ``threadpoolctl`` knows about. The one
path in this project that uses numba is LiberTEM, which sets numba itself
from the worker count its executor is constructed with — see
:func:`miainwoodpecker.analysis.libertem_bridge.analysis_context`, which
is handed the same number this module resolves.
"""

from __future__ import annotations

import contextlib
import os
import typing

if typing.TYPE_CHECKING:
    from collections.abc import Iterator

GUI_RESERVED_CORES = 2
"""
Cores an analysis may not take, so the GUI thread keeps one to run on.

Two rather than one because the Qt main thread is not the only thing that
has to be scheduled for a live view to stay responsive: the display timer
fires on it, but the acquisition worker
(:class:`~miainwoodpecker.acquisition.live.LiveAcquisition`) and the
device-server client both need to run for a frame to arrive at all. The
benchmark's four-competitor row on an 8-core machine — four cores
nominally free — is the configuration this reproduces, and it costs the
live view nothing.
"""


def analysis_thread_count(cpu_count: int | None = None) -> int:
    """
    Return how many threads analysis work may use on this machine.

    The whole policy, in one place so the LiberTEM executor's worker count
    and the BLAS pools' limit cannot drift apart: every core except
    :data:`GUI_RESERVED_CORES`, and never fewer than one.

    The floor is the part worth stating. On a one- or two-core machine the
    subtraction is zero or negative, and both a BLAS pool and a LiberTEM
    executor read that as "decide for yourself" or refuse it outright, so
    a naive cap would either do nothing or crash on exactly the machines
    least able to absorb it. Returning 1 means the analysis runs
    single-threaded and slowly, sharing the machine with the GUI rather
    than evicting it.

    Parameters
    ----------
    cpu_count : int | None
        Cores to size the cap against. Defaults to :func:`os.cpu_count`,
        which itself returns None when it cannot tell — treated the same
        way as a single-core machine. Passed explicitly by the tests,
        which is why the policy is a function of a number rather than a
        function of the machine.

    Returns
    -------
    int
        At least 1.

    Notes
    -----
    :func:`os.cpu_count` reports the machine, not this process's CPU
    affinity, so a container pinned to fewer cores than the host has gets
    a cap that is too generous. ``os.process_cpu_count()`` is the correct
    call and arrived in Python 3.13; this package supports 3.11, so the
    over-generous answer is the one available. It errs in the direction
    the benchmark says is survivable (the median stays fine well before
    the tail does), and the floor still protects the small end.
    """
    cores = os.cpu_count() if cpu_count is None else cpu_count
    if cores is None:
        return 1
    return max(1, cores - GUI_RESERVED_CORES)


@contextlib.contextmanager
def limit_analysis_threads(cpu_count: int | None = None) -> Iterator[int]:
    """
    Cap the native thread pools for the duration of one analysis.

    Wrap analysis work in this and the BLAS pools underneath numpy, scipy,
    HyperSpy and py4DSTEM run at :func:`analysis_thread_count` threads
    instead of one per core, returning to whatever they were on the way
    out. See the module docstring for why this is a runtime call rather
    than an environment variable, and for what "scoped" does and does not
    mean here.

    Parameters
    ----------
    cpu_count : int | None
        Cores to size the cap against; defaults to the machine's. See
        :func:`analysis_thread_count`.

    Yields
    ------
    int
        The cap that was applied, so a caller that also configures its own
        executor (LiberTEM) can use the same number without recomputing
        it.

    Notes
    -----
    Degrades to yielding the number without applying it when
    ``threadpoolctl`` is not installed. That combination is not reachable
    from an install that can actually run an analysis — all three of the
    ``analysis``, ``libertem`` and ``py4dstem`` extras depend on it — but
    this module is imported by the viewer, which must import with none of
    them present.
    """
    limit = analysis_thread_count(cpu_count)
    try:
        # Imported here, not at module scope: the viewer imports this
        # module unconditionally and must still work in an install with
        # no analysis extra at all.
        import threadpoolctl  # noqa: PLC0415
    except ImportError:
        yield limit
        return
    with threadpoolctl.threadpool_limits(limits=limit):
        yield limit
