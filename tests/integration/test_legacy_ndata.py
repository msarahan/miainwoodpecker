"""
Integration tests: reading legacy Nion Swift ``.ndata`` files.

Fixtures are written by Nion's own ``NDataHandler`` rather than by
hand-assembling zip files, so these tests exercise the real container
format. That is the entire point of this file, and it is why it still
needs the ``device`` extra even though
:mod:`miainwoodpecker.storage.legacy` no longer does: the reader was
re-implemented on the standard library to keep GPL-3.0 code out of the MIT
application's process (see that module's docstring and
docs/migration-plan.md §6), so the risk that replaces the old one is a
reader that works against our *assumptions* about the format rather than
the format itself. Writing the fixtures with the vendor's own writer is
what closes that gap.

Using Nion's writer here is not the boundary violation the reader was: a
test is not the distributed application, and nothing in this file is
imported by shipped code.

Reader behaviour that needs no vendor code to exercise — error paths,
metadata-less files, hand-built containers — lives in
``tests/unit/test_legacy_reader.py`` so it runs without the extra.
"""

import datetime

import numpy as np
import pytest

pytest.importorskip("nion.swift.model", reason="requires the 'device' extra")

from nion.swift.model import NDataHandler

from miainwoodpecker.storage import read_series, write_frames
from miainwoodpecker.storage.legacy import iter_ndata_directory, read_ndata


def _write_ndata(path, data, properties) -> None:
    """
    Write a genuine .ndata file using Nion's own writer.

    ``file_datetime`` is passed by keyword deliberately: the positional
    form binds on the nionswift build resolved for Python 3.11 but not the
    one resolved for 3.12, which fails with "missing 1 required positional
    argument: 'file_datetime'". The keyword form is correct under both.
    """
    when = datetime.datetime(2020, 5, 6, 7, 8, 9)  # noqa: DTZ001 - Swift writes naive
    handler = NDataHandler.NDataHandler(path)
    try:
        handler.write_data(data, file_datetime=when)
        handler.write_properties(properties, file_datetime=when)
    finally:
        handler.close()


def test_read_ndata_recovers_array_metadata_and_timestamp(tmp_path):
    """A round trip through Swift's own writer preserves data and metadata."""
    path = tmp_path / "item.ndata"
    data = np.arange(12, dtype=np.float32).reshape(3, 4)
    _write_ndata(path, data, {"created": "2020-05-06T07:08:09", "title": "old scan"})

    frame = read_ndata(path)
    assert np.array_equal(frame.data, data)
    assert frame.metadata["title"] == "old scan"
    assert frame.metadata["source_path"] == str(path)
    # Swift writes naive UTC; the frame must expose it as aware.
    assert frame.timestamp.tzinfo is not None
    assert frame.timestamp == datetime.datetime(
        2020, 5, 6, 7, 8, 9, tzinfo=datetime.UTC
    )


def test_read_ndata_falls_back_when_timestamp_is_unparseable(tmp_path):
    """An unusable timestamp yields an aware 'now' rather than raising."""
    path = tmp_path / "no_time.ndata"
    _write_ndata(path, np.zeros((2, 2), dtype=np.float32), {"created": "not-a-date"})
    frame = read_ndata(path)
    assert frame.timestamp.tzinfo is not None


def test_iter_directory_is_sorted_and_recursive(tmp_path):
    """All .ndata files are found, in a deterministic order."""
    (tmp_path / "nested").mkdir()
    for name, value in (("b.ndata", 2), ("a.ndata", 1)):
        _write_ndata(tmp_path / name, np.full((2, 2), value, dtype=np.float32), {})
    _write_ndata(
        tmp_path / "nested" / "c.ndata",
        np.full((2, 2), 3, dtype=np.float32),
        {},
    )

    frames = list(iter_ndata_directory(tmp_path))
    expected = 3
    assert len(frames) == expected
    # Sorted by path: a.ndata, b.ndata, then nested/c.ndata.
    assert [int(frame.data.flat[0]) for frame in frames] == [1, 2, 3]


def test_legacy_library_migrates_into_the_new_nexus_format(tmp_path):
    """The end-to-end migration path: old .ndata library -> new NeXus file."""
    library = tmp_path / "old_library"
    library.mkdir()
    for index in range(3):
        _write_ndata(
            library / f"item{index}.ndata",
            np.full((4, 4), index, dtype=np.float32),
            {"created": "2020-05-06T07:08:09"},
        )

    destination = tmp_path / "migrated.nxs"
    written = write_frames(
        destination,
        iter_ndata_directory(library),
        title="migrated from Swift",
    )
    expected = 3
    assert written == expected
    recovered = [int(data.flat[0]) for data, _ in read_series(destination)]
    assert recovered == [0, 1, 2]
