"""Tests for the NeXus HDF5 writer."""

import datetime
import json

import h5py
import numpy as np
import pytest

from miainwoodpecker.devices import Frame
from miainwoodpecker.devices.interface import HIGH_TENSION_V_KEY
from miainwoodpecker.storage import (
    AxisCalibration,
    AxisKind,
    FrameCalibration,
    NexusWriter,
    layout,
    read_calibration,
    read_series,
    write_frames,
)


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


def test_nxdata_says_the_frames_are_images_so_a_reader_finds_the_signal(tmp_path):
    """
    ``interpretation`` on the group *and* on the field, because readers differ.

    Without it a NeXus reader has only the rank to go on. Measured
    against RosettaSciIO 0.14.0: the stack comes back as three
    *navigation* axes and no image, so HyperSpy builds a ``BaseSignal``
    rather than the ``Signal2D`` the analysis adapters expect. rsciio
    reads the attribute from the ``NXdata`` group; the NeXus manual
    defines it on the field. Both are written, so both are asserted —
    dropping either one would satisfy a spec and break a reader, or the
    reverse.
    """
    path = tmp_path / "series.nxs"
    write_frames(path, [_frame(0)])
    with h5py.File(path, "r") as handle:
        assert handle["entry/data"].attrs["interpretation"] == "image"
        assert handle["entry/data/data"].attrs["interpretation"] == "image"
        # The field attribute belongs to the one dataset both paths name,
        # which is why the detector's own array reports it too.
        detector = handle["entry/instrument/detector/data"]
        assert detector.attrs["interpretation"] == "image"


def test_a_file_claiming_nxem_leaves_the_reader_hint_out(tmp_path):
    """
    The schema claim wins over the reader hint, because the two conflict.

    Measured with ``pynxtools`` in ``scripts/validate_nexus_schema.py``:
    NXem's NXDL documents no ``interpretation`` attribute, and an
    undocumented attribute fails validation wherever it is written — so a
    file cannot both say ``NXem`` and carry the hint. Which one gives way
    is the same order of priority the writer already applies to
    ``definition`` itself: a claim, once made, is kept honest.
    """
    path = tmp_path / "appdef.nxs"
    write_frames(
        path,
        [_frame(0)],
        definition="NXem",
        sample={"is_simulation": True, "atom_types": "Si,O"},
    )
    with h5py.File(path, "r") as handle:
        assert handle["entry/definition"][()].decode() == "NXem"
        assert "interpretation" not in handle["entry/data"].attrs
        assert "interpretation" not in handle["entry/data/data"].attrs


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


def test_a_diffraction_calibration_writes_reciprocal_axes(tmp_path):
    """
    A camera frame can now carry a real reciprocal-space axis.

    The gap this closes: before, every camera frame fell back to 'pixel'
    because only scans reported a field of view. '1/nm' is the spelling
    NeXus' NX_WAVENUMBER category accepts ('nm-1' does not - see
    scripts/validate_nexus_schema.py).
    """
    path = tmp_path / "diffraction.nxs"
    write_frames(
        path,
        [_frame(0, shape=(4, 4))],
        calibration=FrameCalibration.diffraction(0.05, shape=(4, 4)),
    )
    with h5py.File(path, "r") as handle:
        x_axis = handle["entry/data/x"]
        assert x_axis.attrs["units"] == "1/nm"
        assert x_axis.attrs["long_name"] == "scattering vector"
        # Centred on the optic axis, not the detector corner.
        assert np.allclose(x_axis[()], [-0.1, -0.05, 0.0, 0.05])

    recovered = read_calibration(path)
    assert recovered.x.kind is AxisKind.RECIPROCAL_SPACE
    assert recovered.x.scale == pytest.approx(0.05)
    assert recovered.x.offset == pytest.approx(-0.1)


def test_a_spectrum_calibration_writes_one_energy_axis_and_one_pixel_axis(tmp_path):
    """
    An EELS frame's two axes are different kinds, and the file says so.

    This is the case a single unit per frame could not express: the fast
    axis is energy-dispersive and the slow axis genuinely is not
    calibrated, so it keeps the honest 'pixel' fallback rather than
    borrowing the energy unit.
    """
    path = tmp_path / "eels.nxs"
    write_frames(
        path,
        [_frame(0, shape=(4, 8))],
        calibration=FrameCalibration.spectrum(0.5, offset=-20.0),
    )
    with h5py.File(path, "r") as handle:
        x_axis, y_axis = handle["entry/data/x"], handle["entry/data/y"]
        assert x_axis.attrs["units"] == "eV"
        assert x_axis.attrs["long_name"] == "energy"
        assert np.allclose(x_axis[()][:3], [-20.0, -19.5, -19.0])
        assert y_axis.attrs["units"] == "pixel"
        assert np.allclose(y_axis[()], [0.0, 1.0, 2.0, 3.0])

    recovered = read_calibration(path)
    assert recovered.energy_axis_name() == "x"
    assert recovered.y.kind is AxisKind.UNCALIBRATED


def test_an_angular_camera_axis_round_trips_in_mrad(tmp_path):
    """
    A scattering angle is its own kind, because converting needs the HT.

    The one camera calibration that exists in this stack is angular (the
    simulated Ronchigram camera reports radians), so 'mrad' has to be
    expressible without inventing an electron wavelength.
    """
    path = tmp_path / "angles.nxs"
    write_frames(
        path,
        [_frame(0, shape=(4, 4))],
        calibration=FrameCalibration.diffraction(0.4, units="mrad"),
    )
    with h5py.File(path, "r") as handle:
        assert handle["entry/data/x"].attrs["units"] == "mrad"
    assert read_calibration(path).x.kind is AxisKind.ANGLE


def test_calibration_can_arrive_through_frame_metadata(tmp_path):
    """
    The route fov_nm already travels also carries the other axis kinds.

    This is what lets calibration reach the writer without the device,
    viewer, or session layers growing a parameter first: a frame's own
    metadata says what its axes mean.
    """
    path = tmp_path / "from-metadata.nxs"
    frame = Frame(
        data=np.zeros((4, 6), dtype=np.float32),
        timestamp=datetime.datetime(2026, 1, 1, tzinfo=datetime.UTC),
        metadata={
            "calibration": {
                "x": {"kind": "energy", "scale": 0.25, "units": "meV"},
                "y": {"kind": "real_space", "scale": 2.0},
            },
        },
    )
    write_frames(path, [frame])
    recovered = read_calibration(path)
    assert recovered.x.units == "meV"
    assert recovered.x.scale == pytest.approx(0.25)
    assert recovered.y.units == "nm"
    assert recovered.y.scale == pytest.approx(2.0)


def test_an_explicit_calibration_overrides_a_frames_fov(tmp_path):
    """The writer's keyword option wins over what the frames happen to say."""
    path = tmp_path / "override.nxs"
    write_frames(
        path,
        [_frame(0, shape=(4, 4), fov_nm=20.0)],
        calibration=FrameCalibration.diffraction(0.05),
    )
    assert read_calibration(path).x.kind is AxisKind.RECIPROCAL_SPACE


def test_a_malformed_calibration_fails_on_the_first_frame_not_at_close(tmp_path):
    """
    A mis-specified calibration aborts before the acquisition, not after.

    Resolving it at the first append rather than in close() means an
    operator finds out before spending a recording on it.
    """
    path = tmp_path / "broken.nxs"
    frame = Frame(
        data=np.zeros((4, 4), dtype=np.float32),
        timestamp=datetime.datetime(2026, 1, 1, tzinfo=datetime.UTC),
        metadata={"calibration": {"x": {"kind": "energy", "dispersion": 0.5}}},
    )
    with NexusWriter(path) as writer, pytest.raises(ValueError, match="unknown key"):
        writer.append(frame)


def test_the_uncalibrated_fallback_still_reads_back_as_uncalibrated(tmp_path):
    """
    The honest 'pixel' state survives a full write-and-reread round trip.

    It has to stay a first-class answer rather than degrading into a
    real-space axis with scale 1 nm, which would be a fabricated claim.
    """
    path = tmp_path / "pixels.nxs"
    write_frames(path, [_frame(0, shape=(2, 3))])
    recovered = read_calibration(path)
    assert recovered.is_calibrated is False
    assert recovered.x.units == "pixel"
    assert recovered.x.scale == pytest.approx(1.0)


def test_reading_a_scan_recordings_calibration_recovers_nanometres(tmp_path):
    """
    The fov_nm path reads back as a real-space calibration with square pixels.

    A non-square scan is the case that distinguishes the conventions:
    fov_nm spans the longer axis, so both scales are 20/8, not 20/4 and
    20/8. This test asserted the latter before the convention was pinned
    against Nion's own scan calibration code.
    """
    path = tmp_path / "scan.nxs"
    write_frames(path, [_frame(0, shape=(4, 8), fov_nm=20.0)])
    recovered = read_calibration(path)
    assert recovered.y.kind is AxisKind.REAL_SPACE
    assert recovered.y.scale == pytest.approx(2.5)
    assert recovered.x.scale == pytest.approx(2.5)


def test_reading_a_calibration_from_an_empty_recording_is_a_clear_error(tmp_path):
    """A zero-frame file has no axes to describe, and says so."""
    path = tmp_path / "empty.nxs"
    write_frames(path, [])
    with pytest.raises(ValueError, match="no frames"):
        read_calibration(path)


def test_a_calibration_written_by_axis_objects_needs_no_mapping_form(tmp_path):
    """FrameCalibration is constructible axis by axis for asymmetric cases."""
    path = tmp_path / "mixed.nxs"
    write_frames(
        path,
        [_frame(0, shape=(4, 4))],
        calibration=FrameCalibration(
            y=AxisCalibration(AxisKind.REAL_SPACE, 1.5),
            x=AxisCalibration(AxisKind.ENERGY, 0.25, -10.0, "eV"),
        ),
    )
    recovered = read_calibration(path)
    assert recovered.y.kind is AxisKind.REAL_SPACE
    assert recovered.x.kind is AxisKind.ENERGY
    assert recovered.x.offset == pytest.approx(-10.0)


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


def test_every_frames_metadata_is_kept_not_just_the_firsts(tmp_path):
    """
    Per-frame metadata survives, which is what a focal series depends on.

    ``acquisition.sequence.focal_series`` varies ``defocus_nm`` and
    ``requested_defocus_nm`` frame by frame precisely so a recording says
    what the instrument did rather than what it was asked to do. The
    writer used to keep only the first frame's mapping, so all of that
    was silently discarded - the recording looked complete and had one
    defocus value repeated nowhere, only frame 0's stored once.
    """
    path = tmp_path / "focal.nxs"
    frames = [
        Frame(
            data=np.full((4, 6), index, dtype=np.float32),
            timestamp=datetime.datetime(2026, 1, 1, tzinfo=datetime.UTC)
            + datetime.timedelta(seconds=index),
            metadata={"requested_defocus_nm": index * 10.0,
                      "defocus_nm": index * 10.0 + 0.5},
        )
        for index in range(4)
    ]
    write_frames(path, frames)

    with h5py.File(path, "r") as handle:
        recorded = [
            json.loads(entry)
            for entry in handle["entry/metadata/frame_metadata_json"][()]
        ]
    assert [item["requested_defocus_nm"] for item in recorded] == [
        0.0, 10.0, 20.0, 30.0,
    ]
    assert [item["defocus_nm"] for item in recorded] == [0.5, 10.5, 20.5, 30.5]


def test_per_frame_metadata_lives_in_the_nxcollection(tmp_path):
    """
    Non-standard data goes in NXcollection, never inside NXdetector.

    NXcollection is NeXus's own container for what no base class
    describes, and a JSON blob of vendor keys is exactly that. Putting it
    in NXdetector instead made files claiming ``NXem`` fail validation,
    because a detector's contents *are* specified - caught by the schema
    job rather than by any test, which is why this one exists.
    """
    path = tmp_path / "layout.nxs"
    write_frames(path, [_frame(i) for i in range(2)])

    with h5py.File(path, "r") as handle:
        detector = handle["entry/instrument/detector"]
        assert set(detector) == {"data", "frame_time"}
        collection = handle["entry/metadata"]
        assert collection.attrs["NX_class"] == "NXcollection"
        assert "frame_metadata_json" in collection
        assert "vendor_metadata_json" in collection


def test_the_writer_copies_metadata_rather_than_holding_the_callers_dict(tmp_path):
    """A caller reusing one dict across a series must not rewrite frame 0."""
    path = tmp_path / "reused.nxs"
    shared: dict[str, object] = {"step": 0}
    frame = Frame(
        data=np.zeros((4, 6), dtype=np.float32),
        timestamp=datetime.datetime(2026, 1, 1, tzinfo=datetime.UTC),
        metadata=shared,
    )
    with NexusWriter(path) as writer:
        writer.append(frame)
        shared["step"] = 99  # the caller moves on; the file must not follow

    with h5py.File(path, "r") as handle:
        blob = json.loads(handle["entry/metadata/vendor_metadata_json"][()])
    assert blob["step"] == 0


def test_a_one_dimensional_frame_is_refused_at_append(tmp_path):
    """
    A 1D frame fails where the caller can act, not at close().

    Frame.data's docstring allows 1D for binned spectra, but the NXdata
    layout this writer builds names two frame axes. Before the check, a
    1D series raised IndexError inside close() - after the whole
    acquisition had been appended.
    """
    path = tmp_path / "spectrum.nxs"
    spectrum = Frame(
        data=np.zeros((8,), dtype=np.float32),
        timestamp=datetime.datetime(2026, 1, 1, tzinfo=datetime.UTC),
    )
    with pytest.raises(ValueError, match="stores 2D frames"), NexusWriter(path) as w:
        w.append(spectrum)


def test_a_three_dimensional_frame_is_refused_at_append(tmp_path):
    """A 3D frame used to write a rank-4 signal described by two axes."""
    path = tmp_path / "cube.nxs"
    cube = Frame(
        data=np.zeros((2, 4, 6), dtype=np.float32),
        timestamp=datetime.datetime(2026, 1, 1, tzinfo=datetime.UTC),
    )
    with pytest.raises(ValueError, match="stores 2D frames"), NexusWriter(path) as w:
        w.append(cube)


def test_a_dtype_change_mid_series_is_refused(tmp_path):
    """
    Documented as raising since append() was written; h5py would recast.

    Silently narrowing what it was handed is exactly what the opt-in
    dtype= parameter exists to prevent the writer doing on its own.
    """
    path = tmp_path / "mixed.nxs"
    first = _frame(0)
    second = Frame(
        data=np.zeros((4, 6), dtype=np.float64),
        timestamp=first.timestamp + datetime.timedelta(seconds=1),
    )
    with NexusWriter(path) as w:
        w.append(first)
        with pytest.raises(ValueError, match="does not match"):
            w.append(second)


def test_an_explicit_dtype_still_casts_deliberately(tmp_path):
    """dtype= means the caller asked for one dtype, so mixed input is fine."""
    path = tmp_path / "cast.nxs"
    first = _frame(0)
    second = Frame(
        data=np.full((4, 6), 1, dtype=np.float64),
        timestamp=first.timestamp + datetime.timedelta(seconds=1),
    )
    assert write_frames(path, [first, second], dtype="float32") == 2  # noqa: PLR2004

    with h5py.File(path, "r") as handle:
        assert handle["entry/instrument/detector/data"].dtype == np.float32


def test_reusing_a_writer_does_not_leave_phantom_frames(tmp_path):
    """
    close() resets the frame counter, not only the handles.

    Leaving _count behind made a reused writer resize to count+1 and
    write at count, so frames 0..count-1 were HDF5 fill values
    indistinguishable from real data.
    """
    path = tmp_path / "reused_writer.nxs"
    writer = NexusWriter(path)
    with writer:
        writer.append(_frame(0))
        writer.append(_frame(1))
    with writer:
        writer.append(_frame(7))

    with h5py.File(path, "r") as handle:
        data = handle["entry/instrument/detector/data"]
        assert data.shape == (1, 4, 6)
        assert np.array_equal(data[0], np.full((4, 6), 7, dtype=np.float32))


def test_the_accelerating_voltage_becomes_an_nxsource(tmp_path):
    """
    A frame's high tension is written where a NeXus reader looks for it.

    Everything else a frame reports about the instrument stays in the
    per-frame JSON, because NeXus describes no home for it. This one has
    a home — ``NXsource.voltage``, inside the ``NXinstrument`` the file
    already has — so leaving it in a blob would hide it from every reader
    that speaks NeXus rather than this project.
    """
    path = tmp_path / "voltage.nxs"
    frame = _frame(0)
    with NexusWriter(path) as writer:
        writer.append(
            Frame(
                data=frame.data,
                timestamp=frame.timestamp,
                metadata={**frame.metadata, HIGH_TENSION_V_KEY: 100000.0},
            ),
        )

    with h5py.File(path, "r") as handle:
        source = handle[layout.SOURCE_GROUP]
        assert source.attrs["NX_class"] == "NXsource"
        assert source["probe"][()].decode() == "electron"
        assert handle[layout.SOURCE_VOLTAGE][()] == pytest.approx(100000.0)
        assert handle[layout.SOURCE_VOLTAGE].attrs["units"] == "V"


def test_no_source_group_is_written_when_no_frame_reported_a_voltage(tmp_path):
    """
    An unreported voltage is absent, never zero.

    A stored 0 V would read as a measurement — an instrument with its high
    tension off — which is a specific and wrong claim about the
    acquisition. The same rule the device layer follows for every other
    control it cannot read.
    """
    path = tmp_path / "no_voltage.nxs"
    with NexusWriter(path) as writer:
        writer.append(_frame(0))

    with h5py.File(path, "r") as handle:
        assert layout.SOURCE_GROUP not in handle
