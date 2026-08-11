"""
Unit tests: the ``.ndata`` reader, without any vendor code.

These run in the base test environment, which is the point:
:mod:`miainwoodpecker.storage.legacy` was re-implemented on the standard
library so the MIT application never imports Nion's GPL-3.0 code in-process
(docs/migration-plan.md §6), and a reader that needs no extra to work
should need no extra to test either.

Containers here are hand-built with :mod:`zipfile` to the format Nion's own
``NDataHandler`` documents — ``data.npy`` and ``metadata.json``, both
uncompressed. ``tests/integration/test_legacy_ndata.py`` covers the same
reader against fixtures written by the vendor's actual writer, which is
what proves the format assumed here is the real one.
"""

import datetime
import io
import json
import zipfile

import numpy as np
import pytest

from miainwoodpecker.storage.legacy import iter_ndata_directory, read_ndata


def _write_ndata(path, data=None, properties=None, *, omit_metadata=False) -> None:
    """Hand-build a .ndata container: stored (uncompressed) zip members."""
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_STORED) as archive:
        if data is not None:
            buffer = io.BytesIO()
            np.save(buffer, data)
            archive.writestr("data.npy", buffer.getvalue())
        if not omit_metadata:
            archive.writestr("metadata.json", json.dumps(properties or {}))


def test_reads_data_metadata_and_source_path(tmp_path):
    """The happy path: array, properties, and the injected source_path."""
    path = tmp_path / "item.ndata"
    data = np.arange(12, dtype=np.float32).reshape(3, 4)
    _write_ndata(path, data, {"created": "2020-05-06T07:08:09", "title": "old scan"})

    frame = read_ndata(path)
    assert np.array_equal(frame.data, data)
    assert frame.data.dtype == np.float32
    assert frame.metadata["title"] == "old scan"
    assert frame.metadata["source_path"] == str(path)


def test_recovers_naive_swift_timestamps_as_aware(tmp_path):
    """Swift writes naive UTC; the frame must expose it as timezone-aware."""
    path = tmp_path / "item.ndata"
    _write_ndata(path, np.zeros((2, 2)), {"created": "2020-05-06T07:08:09"})

    frame = read_ndata(path)
    assert frame.timestamp == datetime.datetime(
        2020, 5, 6, 7, 8, 9, tzinfo=datetime.UTC
    )


@pytest.mark.parametrize(
    "key", ["created", "datetime_original", "timestamp"],
)
def test_accepts_each_timestamp_key_swift_versions_use(tmp_path, key):
    """Different Swift versions record the acquisition time under different keys."""
    path = tmp_path / "item.ndata"
    _write_ndata(path, np.zeros((2, 2)), {key: "2019-01-02T03:04:05"})

    frame = read_ndata(path)
    expected_year = 2019
    assert frame.timestamp.year == expected_year
    assert frame.timestamp.tzinfo is not None


def test_unparseable_timestamp_falls_back_to_now(tmp_path):
    """A bad timestamp yields an aware 'now' rather than raising."""
    path = tmp_path / "item.ndata"
    _write_ndata(path, np.zeros((2, 2)), {"created": "not-a-date"})

    frame = read_ndata(path)
    assert frame.timestamp.tzinfo is not None


def test_a_file_without_metadata_still_yields_its_array(tmp_path):
    """
    Swift has written .ndata files with no metadata member.

    The array is the irreplaceable part, so it is recovered rather than
    refused; only the timestamp degrades to 'now'.
    """
    path = tmp_path / "bare.ndata"
    data = np.full((2, 3), 7.0)
    _write_ndata(path, data, omit_metadata=True)

    frame = read_ndata(path)
    assert np.array_equal(frame.data, data)
    assert frame.metadata == {"source_path": str(path)}


def test_missing_data_member_is_a_clear_error(tmp_path):
    """A container with metadata but no array names the file it failed on."""
    path = tmp_path / "empty.ndata"
    _write_ndata(path, data=None, properties={"created": "2020-01-01T00:00:00"})

    with pytest.raises(ValueError, match="no data array found"):
        read_ndata(path)


def test_a_non_zip_file_is_a_clear_error_not_a_zipfile_traceback(tmp_path):
    """Truncated or unrelated files raise ValueError naming the path."""
    path = tmp_path / "junk.ndata"
    path.write_bytes(b"this is not a zip container")

    with pytest.raises(ValueError, match=r"not a valid \.ndata"):
        read_ndata(path)


def test_iter_directory_is_sorted_and_recursive(tmp_path):
    """All .ndata files are found, in a deterministic path order."""
    (tmp_path / "nested").mkdir()
    for name, value in (("b.ndata", 2), ("a.ndata", 1)):
        _write_ndata(tmp_path / name, np.full((2, 2), value), {})
    _write_ndata(tmp_path / "nested" / "c.ndata", np.full((2, 2), 3), {})

    frames = list(iter_ndata_directory(tmp_path))
    expected = 3
    assert len(frames) == expected
    assert [int(frame.data.flat[0]) for frame in frames] == [1, 2, 3]


def test_iter_directory_can_stay_shallow(tmp_path):
    """recursive=False ignores subdirectories."""
    (tmp_path / "nested").mkdir()
    _write_ndata(tmp_path / "top.ndata", np.zeros((2, 2)), {})
    _write_ndata(tmp_path / "nested" / "deep.ndata", np.zeros((2, 2)), {})

    frames = list(iter_ndata_directory(tmp_path, recursive=False))
    assert len(frames) == 1
