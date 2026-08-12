"""
Unit tests for the analysis thread cap: the policy, not the scheduling.

What is under test here is arithmetic and a context manager, deliberately.
The thing the cap exists to prevent — a four-second GUI freeze under eight
competing workers (docs/migration-plan.md, Phase 2) — is a scheduling
outcome on a loaded machine, and a test that tried to observe it would be
measuring the CI runner's other tenants. So these tests pin the resolved
worker count for a given core count, including the small-machine edges
where a naive ``cores - 2`` goes to zero or negative, and check that the
context manager applies and then removes a real limit.

No GUI, no device extra, and no analysis library. ``AnalysisJob`` is
imported here rather than left to the viewer's own integration tests
because :mod:`miainwoodpecker.viewer.jobs` deliberately touches no Qt, so
the wiring between it and the policy is testable without a display.
"""

import builtins
import os
import typing
from unittest import mock

import numpy as np
import pytest

from miainwoodpecker.analysis.threads import (
    GUI_RESERVED_CORES,
    analysis_thread_count,
    limit_analysis_threads,
)
from miainwoodpecker.viewer.jobs import AnalysisJob

_MODULE = "miainwoodpecker.analysis.threads.os.cpu_count"
_JOB_TIMEOUT_S = 10.0


@pytest.mark.parametrize(
    ("cores", "expected"),
    [
        (64, 62),
        (16, 14),
        (12, 10),
        (8, 6),
        (4, 2),
        (3, 1),
    ],
)
def test_the_cap_leaves_two_cores_on_a_machine_that_has_them(cores, expected):
    """Above the floor the policy is exactly 'every core but two'."""
    assert analysis_thread_count(cores) == expected
    assert analysis_thread_count(cores) == cores - GUI_RESERVED_CORES


@pytest.mark.parametrize("cores", [2, 1])
def test_a_small_machine_gets_one_thread_rather_than_none(cores):
    """
    On one or two cores the subtraction would be zero or negative.

    Both ends of that are wrong in a way that matters more than the cap
    itself: a BLAS pool reads zero as "decide for yourself" and goes back
    to using every core, and LiberTEM's executor refuses a non-positive
    worker count outright. The floor means a two-core laptop runs its
    analysis single-threaded and slowly, which is the intended trade,
    rather than either ignoring the limit or failing to start.
    """
    assert analysis_thread_count(cores) == 1


@pytest.mark.parametrize("cores", [0, -1])
def test_a_nonsensical_core_count_still_resolves_to_a_usable_one(cores):
    """A count no real machine reports must not become a bad thread count."""
    assert analysis_thread_count(cores) == 1


def test_the_cap_is_never_zero_or_negative_for_any_plausible_machine():
    """
    The floor holds across the whole range, not just the cases spelled out.

    Stated as its own property because every consumer of this number —
    ``threadpoolctl``'s limit, LiberTEM's ``inline_threads`` — has a
    different bad reaction to a non-positive one, and none of them is a
    clear error message.
    """
    assert all(analysis_thread_count(cores) >= 1 for cores in range(1, 257))


def test_the_cap_is_taken_from_the_machine_when_none_is_given():
    """Omitting the count asks the machine rather than assuming a size."""
    with mock.patch(_MODULE, return_value=12):
        expected_on_twelve_cores = 10
        assert analysis_thread_count() == expected_on_twelve_cores

    with mock.patch(_MODULE, return_value=2):
        assert analysis_thread_count() == 1


def test_a_machine_that_cannot_count_its_cores_gets_the_floor():
    """
    ``os.cpu_count()`` returning None is treated as the smallest machine.

    It is documented as possible and, unhandled, ``None - 2`` is a
    ``TypeError`` raised from inside an analysis job — surfaced to the
    operator as a failed analysis rather than as a slow one.
    """
    with mock.patch(_MODULE, return_value=None):
        assert analysis_thread_count() == 1


def test_the_real_machine_resolves_to_something_sane():
    """The default path is exercised unmocked, since it is what ships."""
    resolved = analysis_thread_count()

    assert resolved >= 1
    assert resolved <= max(1, (os.cpu_count() or 1))


def test_the_context_manager_hands_back_the_number_it_applied():
    """
    The yielded count is the contract LiberTEM's executor is sized from.

    :func:`~miainwoodpecker.analysis.libertem_bridge.analysis_context`
    calls the policy directly rather than reading this value, so the two
    agreeing is the thing worth asserting: a caller inside the block that
    starts its own pool must not be told a different number from the one
    the BLAS pools were just limited to.
    """
    with mock.patch(_MODULE, return_value=8), limit_analysis_threads() as limit:
        expected_on_eight_cores = 6
        assert limit == expected_on_eight_cores

    with limit_analysis_threads(4) as limit:
        expected_on_four_cores = 2
        assert limit == expected_on_four_cores


def test_the_limit_is_applied_to_real_pools_and_then_lifted():
    """
    A real BLAS pool is capped inside the block and restored after it.

    Asserted against ``threadpoolctl``'s own report of the pools numpy
    actually loaded, rather than against a mock of it, because the failure
    this guards against is the one the module docstring is about: setting
    a limit the libraries do not read. A three-core machine is claimed so
    the expected limit is 1 and the assertion is an equality rather than
    an inequality that a runner with few cores would satisfy anyway.
    Skipped where numpy's wheel exposes no controllable pool at all (some
    ARM and conda builds), since there is then nothing to observe rather
    than something wrong.
    """
    threadpoolctl = pytest.importorskip(
        "threadpoolctl", reason="requires the 'analysis' extra"
    )
    np.zeros((2, 2)) @ np.zeros((2, 2))  # force numpy's BLAS pool to load
    before = threadpoolctl.threadpool_info()
    if not before:
        pytest.skip("this numpy build exposes no controllable thread pool")

    with limit_analysis_threads(3) as limit:
        during = threadpoolctl.threadpool_info()

    assert limit == 1
    assert [pool["num_threads"] for pool in during] == [1] * len(before)
    assert [pool["num_threads"] for pool in threadpoolctl.threadpool_info()] == [
        pool["num_threads"] for pool in before
    ]


def test_the_cap_still_yields_when_threadpoolctl_is_missing():
    """
    A viewer-only install imports this module; it must not fail there.

    ``miainwoodpecker.viewer.jobs`` imports the limiter unconditionally,
    while the extras that install ``threadpoolctl`` are the ones that make
    an analysis runnable at all. So the no-``threadpoolctl`` path is
    unreachable from an install that can run an analysis, and it still has
    to be a working no-op rather than an ``ImportError`` at widget
    construction.
    """
    real_import = builtins.__import__

    def without_threadpoolctl(name: str, *args: object, **kwargs: object) -> object:
        if name == "threadpoolctl":
            raise ImportError(name)
        return real_import(name, *args, **kwargs)

    with (
        mock.patch("builtins.__import__", without_threadpoolctl),
        limit_analysis_threads(8) as limit,
    ):
        expected_on_eight_cores = 6
        assert limit == expected_on_eight_cores


def test_an_analysis_job_runs_its_work_under_the_cap():
    """
    The wiring, not just the policy: ``AnalysisJob`` applies it.

    ``miainwoodpecker.viewer.jobs`` is importable without the ``viewer``
    extra — it holds the threading contract and deliberately touches no
    Qt — so this belongs with the policy it consumes rather than in the
    widget integration tests. What is asserted is the property the whole
    change exists for: by the time an analysis callable runs, the BLAS
    pools it is about to use are already capped, and they are back to
    normal once the job is done.
    """
    threadpoolctl = pytest.importorskip(
        "threadpoolctl", reason="requires the 'analysis' extra"
    )
    np.zeros((2, 2)) @ np.zeros((2, 2))  # force numpy's BLAS pool to load
    before = threadpoolctl.threadpool_info()
    if not before:
        pytest.skip("this numpy build exposes no controllable thread pool")

    job = AnalysisJob(threadpoolctl.threadpool_info)
    job.start()
    assert job.join(timeout=_JOB_TIMEOUT_S)

    assert job.error is None
    inside = typing.cast("list[dict]", job.result)
    assert [pool["num_threads"] for pool in inside] == [analysis_thread_count()] * len(
        before
    )
    assert [pool["num_threads"] for pool in threadpoolctl.threadpool_info()] == [
        pool["num_threads"] for pool in before
    ]
