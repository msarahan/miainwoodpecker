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


def test_no_application_definition_is_claimed_by_default(tmp_path):
    """
    A default recording claims no application definition.

    Validated against the real NXDL schema (see
    scripts/validate_nexus_schema.py): a recording without specimen
    metadata is *not* valid NXem, so declaring it would be a false claim.
    """
    path = tmp_path / "unclaimed.nxs"
    write_frames(path, [_frame(0)])
    with h5py.File(path, "r") as handle:
        assert "definition" not in handle["entry"]
        assert "sample" not in handle["entry"]


def test_sample_metadata_becomes_an_nxsample_group(tmp_path):
    """Operator-supplied specimen metadata lands in NXsample, verbatim."""
    path = tmp_path / "claimed.nxs"
    write_frames(
        path,
        [_frame(0)],
        definition="NXem",
        sample={
            "is_simulation": True,
            "preparation_date": "2026-01-01T00:00:00+00:00",
            "atom_types": "Si,O",
        },
    )
    with h5py.File(path, "r") as handle:
        entry = handle["entry"]
        assert entry["definition"][()].decode() == "NXem"
        sample = entry["sample"]
        assert sample.attrs["NX_class"] == "NXsample"
        assert bool(sample["is_simulation"][()]) is True
        assert sample["atom_types"][()].decode() == "Si,O"


def test_session_context_becomes_real_nexus_classes(tmp_path):
    """
    Operator and notes land in NXuser/NXnote, not in the vendor JSON blob.

    Verified against the real schema (scripts/validate_nexus_schema.py):
    NXem documents both groups at entry level, so a file carrying them
    still validates. Unlike `sample` they are optional in NXem — measured
    from the NXDL, where `userID`/`noteID` are minOccurs="0" while
    `sampleID` is minOccurs="1".
    """
    path = tmp_path / "context.nxs"
    write_frames(
        path,
        [_frame(0)],
        user={"name": "A. Operator", "affiliation": "SuperSTEM"},
        notes="aligned at 200 kV",
    )
    with h5py.File(path, "r") as handle:
        entry = handle["entry"]
        assert entry["user"].attrs["NX_class"] == "NXuser"
        assert entry["user/name"][()].decode() == "A. Operator"
        assert entry["notes"].attrs["NX_class"] == "NXnote"
        assert entry["notes/description"][()].decode() == "aligned at 200 kV"


def test_session_context_groups_are_absent_when_not_supplied(tmp_path):
    """No operator or notes means no empty NXuser/NXnote stubs."""
    path = tmp_path / "bare.nxs"
    write_frames(path, [_frame(0)])
    with h5py.File(path, "r") as handle:
        assert "user" not in handle["entry"]
        assert "notes" not in handle["entry"]


def test_flush_makes_appended_frames_readable_before_close(tmp_path):
    """
    flush() gets frames onto disk without finalizing the file.

    This is what bounds worst-case loss if an acquisition is killed: a
    SIGKILL without flushing leaves a file that will not open at all.
    """
    path = tmp_path / "flushed.nxs"
    with NexusWriter(path) as writer:
        writer.append(_frame(0))
        writer.append(_frame(1))
        writer.flush()
        # A second, independent handle sees the flushed frames even though
        # close() has not run yet.
        with h5py.File(path, "r") as handle:
            data = handle["entry/instrument/detector/data"]
            expected_count = 2
            assert data.shape[0] == expected_count
            assert np.array_equal(data[1], np.full((4, 6), 1, dtype=np.float32))


def test_flush_before_opening_is_harmless(tmp_path):
    """flush() on an unopened writer is a no-op, not a crash."""
    NexusWriter(tmp_path / "unused.nxs").flush()


def test_gzip_default_now_includes_the_byte_shuffle_filter(tmp_path):
    """
    The measured winner is on by default, and `compression` still works.

    gzip + shuffle beat plain gzip on ratio, write time, *and* read time on
    every dataset benchmarked, so it needs no opt-in - but the public
    `compression` parameter has to keep behaving.
    """
    path = tmp_path / "shuffled.nxs"
    write_frames(path, [_frame(0)], compression="gzip")
    with h5py.File(path, "r") as handle:
        filters = handle["entry/instrument/detector/data"]._filters  # noqa: SLF001
        assert "gzip" in filters
        assert "shuffle" in filters


def test_compression_can_still_be_disabled_entirely(tmp_path):
    """`compression=None` leaves the frame dataset unfiltered."""
    path = tmp_path / "raw.nxs"
    write_frames(path, [_frame(0)], compression=None)
    with h5py.File(path, "r") as handle:
        dataset = handle["entry/instrument/detector/data"]
        assert dataset.compression is None
        assert dataset.shuffle is False


def test_explicit_dtype_stores_narrower_frames(tmp_path):
    """
    An explicit `dtype` downcasts on write; it is never applied implicitly.

    float32 storage is a 2.3x size win on this project's float64 scan
    frames, but it is lossy, so it stays opt-in - see the module docstring.
    """
    path = tmp_path / "narrow.nxs"
    frame = Frame(
        data=np.full((4, 6), 1.5, dtype=np.float64),
        timestamp=datetime.datetime(2026, 1, 1, tzinfo=datetime.UTC),
        metadata={},
    )
    write_frames(path, [frame], dtype="float32")
    with h5py.File(path, "r") as handle:
        assert handle["entry/instrument/detector/data"].dtype == np.float32

    default_path = tmp_path / "wide.nxs"
    write_frames(default_path, [frame])
    with h5py.File(default_path, "r") as handle:
        assert handle["entry/instrument/detector/data"].dtype == np.float64


def test_plugin_codecs_are_accepted_as_a_filter_mapping(tmp_path):
    """
    An hdf5plugin filter object can be passed straight to `compression`.

    Skipped without the `compression` extra: the *default* codec is a pure
    HDF5 built-in precisely so that plugin is never required to read a file
    this project writes.
    """
    hdf5plugin = pytest.importorskip(
        "hdf5plugin",
        reason="requires the 'compression' extra",
    )
    path = tmp_path / "blosc2.nxs"
    write_frames(
        path,
        [_frame(index) for index in range(2)],
        compression=hdf5plugin.Blosc2(
            cname="zstd",
            clevel=5,
            filters=hdf5plugin.Blosc2.BITSHUFFLE,
        ),
    )
    # Round trips through the plugin, and HDF5's own shuffle is not stacked
    # in front of a codec that bit-shuffles internally.
    recovered = list(read_series(path))
    expected_count = 2
    assert len(recovered) == expected_count
    assert np.array_equal(recovered[1][0], np.full((4, 6), 1, dtype=np.float32))
    with h5py.File(path, "r") as handle:
        assert handle["entry/instrument/detector/data"].shuffle is False
