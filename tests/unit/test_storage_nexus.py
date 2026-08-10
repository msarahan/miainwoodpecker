"""Tests for the NeXus HDF5 writer."""

import datetime
import json

import h5py
import numpy as np
import pytest

from miainwoodpecker.devices import Frame
from miainwoodpecker.storage import NexusWriter, read_series, write_frames


def _frame(index: int, shape=(4, 6), *, fov_nm: float | None = None) -> Frame:
    """Return a frame whose pixels all equal its index."""
    metadata: dict[str, object] = {"index": index}
    if fov_nm is not None:
        metadata["fov_nm"] = fov_nm
    return Frame(
        data=np.full(shape, index, dtype=np.float32),
        timestamp=datetime.datetime(2026, 1, 1, tzinfo=datetime.UTC)
        + datetime.timedelta(seconds=index),
        metadata=metadata,
    )


def test_write_frames_round_trips_data_and_times(tmp_path):
    """Frames read back in order with their elapsed times."""
    path = tmp_path / "series.nxs"
    written = write_frames(path, [_frame(i) for i in range(3)])
    expected_count = 3
    assert written == expected_count

    recovered = list(read_series(path))
    assert len(recovered) == expected_count
    for index, (data, elapsed) in enumerate(recovered):
        assert np.array_equal(data, np.full((4, 6), index, dtype=np.float32))
        assert elapsed == pytest.approx(float(index))


def test_nexus_structure_and_plotting_hints(tmp_path):
    """The file carries the NeXus class/signal/axes conventions."""
    path = tmp_path / "series.nxs"
    write_frames(path, [_frame(0)], title="my scan")

    with h5py.File(path, "r") as handle:
        assert handle.attrs["NX_class"] == "NXroot"
        assert handle.attrs["default"] == "entry"
        entry = handle["entry"]
        assert entry.attrs["NX_class"] == "NXentry"
        assert entry.attrs["default"] == "data"
        assert entry["definition"][()].decode() == "NXem"
        assert entry["title"][()].decode() == "my scan"
        # start/end times must be ISO 8601 parseable.
        datetime.datetime.fromisoformat(entry["start_time"][()].decode())
        datetime.datetime.fromisoformat(entry["end_time"][()].decode())
        assert entry["instrument"].attrs["NX_class"] == "NXinstrument"
        assert entry["instrument/detector"].attrs["NX_class"] == "NXdetector"

        data_group = entry["data"]
        assert data_group.attrs["NX_class"] == "NXdata"
        assert data_group.attrs["signal"] == "data"
        assert list(data_group.attrs["axes"]) == ["frame_time", "y", "x"]
        assert data_group.attrs["y_indices"] == 1
        assert data_group["data"].attrs["units"] == "counts"


def test_nxdata_links_rather_than_copies_the_array(tmp_path):
    """NXdata/data is a hard link to the detector array, not a second copy."""
    path = tmp_path / "series.nxs"
    write_frames(path, [_frame(0)])
    with h5py.File(path, "r") as handle:
        assert handle["entry/data/data"] == handle["entry/instrument/detector/data"]


def test_scan_fov_becomes_calibrated_axes_in_nm(tmp_path):
    """A frame reporting fov_nm yields spatial axes in nanometres."""
    path = tmp_path / "calibrated.nxs"
    write_frames(path, [_frame(0, shape=(4, 4), fov_nm=20.0)])
    with h5py.File(path, "r") as handle:
        x_axis = handle["entry/data/x"]
        assert x_axis.attrs["units"] == "nm"
        # 4 pixels across 20 nm, sampled at the pixel edges.
        assert np.allclose(x_axis[()], [0.0, 5.0, 10.0, 15.0])


def test_axes_fall_back_to_pixels_without_calibration(tmp_path):
    """Without fov_nm the axes are bare pixel indices, honestly labelled."""
    path = tmp_path / "uncalibrated.nxs"
    write_frames(path, [_frame(0, shape=(2, 3))])
    with h5py.File(path, "r") as handle:
        assert handle["entry/data/x"].attrs["units"] == "pixel"
        assert np.allclose(handle["entry/data/x"][()], [0.0, 1.0, 2.0])


def test_vendor_metadata_is_preserved_as_json(tmp_path):
    """Vendor metadata survives into an NXcollection as JSON."""
    path = tmp_path / "series.nxs"
    index, fov_nm = 7, 15.0
    write_frames(path, [_frame(index, fov_nm=fov_nm)])
    with h5py.File(path, "r") as handle:
        collection = handle["entry/metadata"]
        assert collection.attrs["NX_class"] == "NXcollection"
        payload = json.loads(collection["vendor_metadata_json"][()].decode())
        assert payload["index"] == index
        assert payload["fov_nm"] == fov_nm


def test_mismatched_frame_shape_is_rejected(tmp_path):
    """Appending a differently shaped frame fails loudly rather than corrupting."""
    path = tmp_path / "series.nxs"
    with NexusWriter(path) as writer:
        writer.append(_frame(0, shape=(4, 4)))
        with pytest.raises(ValueError, match="does not match"):
            writer.append(_frame(1, shape=(8, 8)))


def test_append_outside_context_is_an_error(tmp_path):
    """Using the writer without entering it is a clear error, not a crash."""
    writer = NexusWriter(tmp_path / "unused.nxs")
    with pytest.raises(RuntimeError, match="not open"):
        writer.append(_frame(0))


def test_empty_series_still_writes_a_valid_readable_file(tmp_path):
    """A zero-frame acquisition produces a readable file, not a broken one."""
    path = tmp_path / "empty.nxs"
    assert write_frames(path, []) == 0
    with h5py.File(path, "r") as handle:
        assert handle["entry"].attrs["NX_class"] == "NXentry"
        assert "data" not in handle["entry"]
    assert list(read_series(path)) == []
