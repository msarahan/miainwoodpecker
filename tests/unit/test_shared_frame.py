"""
Unit tests for the shared-memory frame transport.

Like ``rpc.py``, ``shared_frame.py`` is pure standard library plus numpy -
no vendor SDK, no subprocess - but was covered only by device-extra-gated
integration tests, so a base environment exercised none of it. The
segment-reuse design carries real correctness obligations (a named POSIX
segment outlives the process that made it), and they are checkable here.
"""

from __future__ import annotations

import datetime
import pathlib

import numpy as np
import pytest

from miainwoodpecker.devices.interface import Frame
from miainwoodpecker.devices.shared_frame import (
    SharedFrameReader,
    SharedFrameWriter,
)

_SHM_DIR = pathlib.Path("/dev/shm")  # noqa: S108 - the POSIX shared-memory
# mount is exactly what this module manages; it is not a temp-file location
# chosen for convenience.


def _frame(value: float, shape: tuple[int, int] = (4, 8), dtype=np.float64) -> Frame:
    """Build a frame whose contents identify it."""
    return Frame(
        data=np.full(shape, value, dtype=dtype),
        timestamp=datetime.datetime.now(tz=datetime.UTC),
        metadata={"tag": value},
    )


@pytest.fixture
def writer():
    """Yield a writer that always unlinks its segment, even on failure."""
    instance = SharedFrameWriter()
    try:
        yield instance
    finally:
        instance.close()


@pytest.fixture
def reader():
    """Yield a reader that always detaches."""
    instance = SharedFrameReader()
    try:
        yield instance
    finally:
        instance.close()


def test_a_frame_round_trips_through_the_segment(writer, reader):
    """The basic contract: what the writer publishes is what the reader gets."""
    original = _frame(3.5)
    received = reader.read(writer.publish(original))
    assert np.array_equal(received.data, original.data)
    assert received.timestamp == original.timestamp
    assert received.metadata == original.metadata


def test_the_reader_owns_its_copy(writer, reader):
    """
    The reader must copy out, not hand back a view of a reused buffer.

    A view would alias the segment the writer is about to overwrite, so
    an operator's "previous frame" would silently change under them.
    """
    received = reader.read(writer.publish(_frame(1.0)))
    writer.publish(_frame(2.0))
    assert np.array_equal(received.data, np.full((4, 8), 1.0))


def test_the_segment_is_reused_across_same_sized_frames(writer):
    """
    Reuse is the whole design: one segment per source, not one per frame.

    Creating and destroying a named segment is a syscall pair that was
    measured as worse than plain pickling when paid per frame.
    """
    names = {writer.publish(_frame(float(index))).shm_name for index in range(5)}
    assert len(names) == 1


def test_a_smaller_frame_reuses_the_existing_segment(writer, reader):
    """
    Shrinking must not replace the segment: a larger one holds it fine.

    Keying reuse on exact shape equality paid the create/unlink pair for
    every smaller frame, and on every frame of an alternating workload -
    the regime the reuse redesign exists to avoid.
    """
    first = writer.publish(_frame(1.0, shape=(64, 64)))
    second = writer.publish(_frame(2.0, shape=(8, 8)))
    assert second.shm_name == first.shm_name
    assert np.array_equal(reader.read(second).data, np.full((8, 8), 2.0))


def test_alternating_sizes_do_not_thrash_the_segment(writer):
    """An operator toggling scan sizes must not pay a resize per frame."""
    big = _frame(1.0, shape=(64, 64))
    small = _frame(2.0, shape=(16, 16))
    names = set()
    for _ in range(4):
        names.add(writer.publish(big).shm_name)
        names.add(writer.publish(small).shm_name)
    assert len(names) == 1


def test_a_larger_frame_grows_the_segment(writer, reader):
    """A frame that does not fit genuinely needs a new, larger segment."""
    first = writer.publish(_frame(1.0, shape=(8, 8)))
    second = writer.publish(_frame(2.0, shape=(128, 128)))
    assert second.shm_name != first.shm_name
    assert np.array_equal(reader.read(second).data, np.full((128, 128), 2.0))


def test_the_reader_follows_the_writer_to_a_new_segment(writer, reader):
    """A resize must not strand the reader on the old segment."""
    small = reader.read(writer.publish(_frame(1.0, shape=(8, 8))))
    large = reader.read(writer.publish(_frame(2.0, shape=(128, 128))))
    assert np.array_equal(small.data, np.full((8, 8), 1.0))
    assert np.array_equal(large.data, np.full((128, 128), 2.0))


def test_dtype_travels_in_the_reference_not_the_segment(writer, reader):
    """Shape and dtype are carried by the ref, so the segment is just bytes."""
    published = writer.publish(_frame(7.0, shape=(4, 4), dtype=np.float32))
    received = reader.read(published)
    assert received.data.dtype == np.float32
    assert np.array_equal(received.data, np.full((4, 4), 7.0, dtype=np.float32))


def test_a_non_contiguous_frame_is_published_correctly(writer, reader):
    """
    A strided view from a vendor buffer must survive the copy intact.

    ``np.ascontiguousarray`` handles this; the test pins that a
    transposed array does not arrive scrambled.
    """
    source = np.arange(24, dtype=np.float64).reshape(4, 6).T
    assert not source.flags["C_CONTIGUOUS"]
    frame = Frame(data=source, timestamp=datetime.datetime.now(tz=datetime.UTC))
    received = reader.read(writer.publish(frame))
    assert np.array_equal(received.data, source)


@pytest.mark.skipif(not _SHM_DIR.is_dir(), reason="needs POSIX /dev/shm")
def test_closing_the_writer_unlinks_its_segment():
    """
    A named segment is a tmpfs entry, not a process resource.

    Nothing reclaims it implicitly, so the writer's close() is the only
    thing standing between a long session and a filling /dev/shm.
    """
    instance = SharedFrameWriter()
    name = instance.publish(_frame(1.0)).shm_name
    assert (_SHM_DIR / name).exists()
    instance.close()
    assert not (_SHM_DIR / name).exists()


@pytest.mark.skipif(not _SHM_DIR.is_dir(), reason="needs POSIX /dev/shm")
def test_growing_the_segment_unlinks_the_one_it_replaces():
    """A resize must not leak the segment it outgrew."""
    instance = SharedFrameWriter()
    try:
        first = instance.publish(_frame(1.0, shape=(8, 8))).shm_name
        second = instance.publish(_frame(2.0, shape=(128, 128))).shm_name
        assert first != second
        assert not (_SHM_DIR / first).exists()
        assert (_SHM_DIR / second).exists()
    finally:
        instance.close()


def test_closing_the_writer_twice_is_harmless(writer):
    """
    Idempotent by design: park closes devices and the context manager does too.

    Both paths run on the normal shutdown sequence.
    """
    writer.publish(_frame(1.0))
    writer.close()
    writer.close()


def test_the_reader_never_unlinks_what_it_reads(writer, reader):
    """
    Ownership rule: the writer creates and unlinks, the reader only attaches.

    A reader that unlinked would destroy the segment the writer is still
    publishing into.
    """
    published = writer.publish(_frame(1.0))
    reader.read(published)
    reader.close()
    # Still the writer's, still usable.
    assert np.array_equal(
        SharedFrameReader().read(writer.publish(_frame(2.0))).data,
        np.full((4, 8), 2.0),
    )
