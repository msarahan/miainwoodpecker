"""
Spectrum recordings on disk: NXspectrum's layout, and what it refuses.

The frame writer's tests check that a stack of images round-trips. These
check something a little different, because the risk is different: a
spectrum's whole meaning is its energy axis, so the failures worth
guarding against are the ones that leave a perfectly readable file whose
numbers are wrong — an axis with the wrong dispersion, a detector fact
silently dropped, a second acquisition joined to a first it does not
belong with.

Nothing here needs h5py knowledge beyond opening the file, and that is
deliberate: the layout constants live in ``storage.layout`` and the
assertions go through them, so a layout change breaks one place.
"""

from __future__ import annotations

import datetime
import json

import h5py
import numpy as np
import pytest

from miainwoodpecker.devices.interface import PROJECTED_READOUT, Frame, Spectrum
from miainwoodpecker.storage import layout
from miainwoodpecker.storage.calibration import AxisKind
from miainwoodpecker.storage.spectra import (
    EELS_TECHNIQUE,
    MAP_LAYOUT,
    SERIES_LAYOUT,
    TECHNIQUE_KEY,
    SpectrumWriter,
    read_spectra,
    spectrum_from_projected_frame,
    write_spectra,
)

_CHANNELS = 64
_OFFSET_EV = -480.0
_SCALE_EV = 10.0
_SPECTRA = 3
_MAP_SHAPE = (4, 5)


def _spectrum(index: int = 0, data=None, **overrides: object) -> Spectrum:
    """Build one spectrum carrying the full detector vocabulary."""
    metadata = {
        "device_id": "simulated_sdd",
        "spectrum_index": index,
        "live_time_s": 10.0,
        "real_time_s": 12.5,
        "azimuth_deg": 45.0,
        "elevation_deg": 18.0,
        "solid_angle_sr": 0.7,
        "energy_resolution_ev": 129.0,
        "detector_type": "sdd",
        "technique": "eds",
        "high_tension_v": 100000.0,
    }
    metadata.update(overrides.pop("metadata", {}))
    fields = {
        "timestamp": datetime.datetime(2026, 1, 1, tzinfo=datetime.UTC),
        "energy_offset_ev": _OFFSET_EV,
        "energy_scale_ev": _SCALE_EV,
    }
    fields.update(overrides)
    counts = data if data is not None else np.full(_CHANNELS, index + 1, dtype="uint32")
    return Spectrum(data=counts, metadata=metadata, **fields)


def test_a_recording_round_trips_with_the_energy_axis_it_was_acquired_with(tmp_path):
    """
    The axis on disk is the detector's, recovered exactly, not approximately.

    The single most important property of this format. An energy axis is
    a scale and an offset, and both are stored as *values* rather than as
    the two numbers — which is right, because that is what ``NXdata``
    describes and what every reader expects — but it means the numbers
    have to survive being written out as a coordinate array and
    differenced back. A dispersion recovered as 9.999999 instead of 10
    would put every identified line slightly off and nothing would say so.
    """
    path = tmp_path / "eds.nxs"
    written = write_spectra(path, [_spectrum(index) for index in range(_SPECTRA)])
    assert written == _SPECTRA

    recording = read_spectra(path)
    assert recording.data.shape == (_SPECTRA, _CHANNELS)
    assert recording.energy_scale_ev == _SCALE_EV
    assert recording.energy_offset_ev == _OFFSET_EV
    assert not recording.is_map
    assert recording.count == _SPECTRA
    # The counts are the counts, in order - so nothing wrote the same
    # buffer three times, which a resize-and-index bug would do silently.
    assert [int(row[0]) for row in recording.data] == [1, 2, 3]


def test_the_file_speaks_nxspectrum_rather_than_this_projects_own_names(tmp_path):
    """
    ``intensity``, ``axis_energy``, and ``NXdata`` hints that point at them.

    The layout is NeXus's, not ours, and this is what pins that: a reader
    that knows ``NXspectrum`` and nothing about this project can find the
    signal and its axis. The signal being called ``intensity`` is also
    what lets a reader tell a spectrum recording from a frame recording,
    since both live at ``entry/data`` — so the name is load-bearing twice.
    """
    path = tmp_path / "eds.nxs"
    write_spectra(path, [_spectrum(index) for index in range(_SPECTRA)])

    with h5py.File(path, "r") as handle:
        assert layout.SPECTRUM_INTENSITY in handle
        assert layout.SPECTRUM_ENERGY_AXIS in handle
        assert layout.SPECTRUM_INDEX_AXIS in handle
        data_group = handle[layout.SPECTRUM_DATA_GROUP]
        assert data_group.attrs["NX_class"] == "NXdata"
        assert data_group.attrs["signal"] == "intensity"
        assert list(data_group.attrs["axes"]) == ["indices_spectrum", "axis_energy"]
        energy = handle[layout.SPECTRUM_ENERGY_AXIS]
        assert energy.attrs["units"] == "eV"
        assert energy.attrs["long_name"] == "Energy"
        assert handle[f"{layout.METADATA_GROUP}/nxspectrum_layout"][()] == (
            SERIES_LAYOUT.encode()
        )


def test_nxdata_says_the_energy_axis_is_the_signal_in_either_layout(tmp_path):
    """
    ``interpretation = "spectrum"``, for a series and for a map alike.

    The counterpart to the frame writer's ``"image"``, and the reason it
    is one value for both layouts is NXspectrum's own rule that the
    energy axis is always the fastest dimension: a reader honouring the
    attribute then makes ``axis_energy`` the signal and leaves whatever
    precedes it — a spectrum index, or a map's two spatial axes — as
    navigation. Asserted on the group and on the field for the reason
    the frame writer's twin test gives.
    """
    series = tmp_path / "eds.nxs"
    write_spectra(series, [_spectrum(index) for index in range(_SPECTRA)])
    height, width = _MAP_SHAPE
    spectrum_image = tmp_path / "map.nxs"
    write_spectra(
        spectrum_image,
        [_spectrum(0, data=np.ones((height, width, _CHANNELS), dtype="uint32"))],
    )

    for path in (series, spectrum_image):
        with h5py.File(path, "r") as handle:
            group = handle[layout.SPECTRUM_DATA_GROUP]
            intensity = handle[layout.SPECTRUM_INTENSITY]
            assert group.attrs["interpretation"] == "spectrum"
            assert intensity.attrs["interpretation"] == "spectrum"


def test_a_spectrum_file_claiming_nxem_leaves_the_reader_hint_out(tmp_path):
    """
    Same trade as the frame writer's, for the same measured reason.

    ``interpretation`` is undocumented in NXem's NXDL, so a file carrying
    it fails validation; a file claiming the definition therefore goes
    without. See the frame writer's twin test.
    """
    path = tmp_path / "appdef.nxs"
    write_spectra(
        path,
        [_spectrum(0)],
        definition="NXem",
        sample={"is_simulation": True, "atom_types": "Si,O"},
    )
    with h5py.File(path, "r") as handle:
        assert handle["entry/definition"][()].decode() == "NXem"
        assert "interpretation" not in handle[layout.SPECTRUM_DATA_GROUP].attrs
        assert "interpretation" not in handle[layout.SPECTRUM_INTENSITY].attrs


def test_one_spectrum_is_stored_the_same_way_as_many(tmp_path):
    """
    A stack of one, not ``spectrum_0d``, so a reader never branches on count.

    ``NXspectrum`` does define ``spectrum_0d`` for exactly one spectrum,
    and using it was considered and rejected: it would make the *rank* of
    the signal depend on how many spectra an acquisition happened to
    produce, so a recording stopped after one would read back differently
    from the same recording that managed two. That is the kind of
    difference that only shows up in the field.
    """
    path = tmp_path / "one.nxs"
    write_spectra(path, [_spectrum(0)])
    recording = read_spectra(path)
    assert recording.data.shape == (1, _CHANNELS)
    assert recording.count == 1


def test_the_detector_facts_land_in_nxdetector_rather_than_a_json_blob(tmp_path):
    """
    Every EDS number a quantification needs, in the field NeXus defines for it.

    This is what makes the file useful to someone who does not have this
    project. ``count_time`` is the one worth naming: NeXus has no field
    called "live time", it has ``count_time`` documented as "elapsed
    actual counting time", which is the same quantity — so the mapping is
    an existing home rather than an invented one. ``dead_time`` is
    derived from the pair, because the ratio is what a real correction
    uses and neither time alone gives it.
    """
    path = tmp_path / "eds.nxs"
    write_spectra(path, [_spectrum(0)])

    with h5py.File(path, "r") as handle:
        detector = handle[layout.SPECTRUM_DETECTOR_GROUP]
        assert detector.attrs["NX_class"] == "NXdetector"
        assert detector["count_time"][()] == 10.0  # noqa: PLR2004 - live time
        assert detector["count_time"].attrs["units"] == "s"
        assert detector["real_time"][()] == 12.5  # noqa: PLR2004
        assert detector["dead_time"][()] == pytest.approx(2.5)
        assert detector["azimuthal_angle"][()] == 45.0  # noqa: PLR2004
        assert detector["azimuthal_angle"].attrs["units"] == "deg"
        assert detector["polar_angle"][()] == 18.0  # noqa: PLR2004
        assert detector["solid_angle"][()] == 0.7  # noqa: PLR2004
        assert detector["solid_angle"].attrs["units"] == "sr"
        assert detector["type"][()] == b"sdd"
        # And the accelerating voltage in NXsource, exactly as the frame
        # writer puts it - it matters more here, because it is what sets
        # which X-ray lines can be excited at all.
        assert handle[layout.SOURCE_VOLTAGE][()] == 100000.0  # noqa: PLR2004


def test_a_fact_the_detector_did_not_report_is_absent_rather_than_zero(tmp_path):
    """
    An omitted key stays omitted, because a stored zero would be a claim.

    The rule the whole metadata vocabulary is built on, applied where it
    bites hardest: a detector geometry of zero degrees is a *plausible*
    number that an absorption correction would happily use, so writing
    one for an instrument that never reported it would be worse than
    saying nothing.
    """
    path = tmp_path / "sparse.nxs"
    sparse = Spectrum(
        data=np.ones(_CHANNELS, dtype="uint32"),
        timestamp=datetime.datetime(2026, 1, 1, tzinfo=datetime.UTC),
        energy_offset_ev=_OFFSET_EV,
        energy_scale_ev=_SCALE_EV,
        metadata={"device_id": "minimal", "live_time_s": 1.0},
    )
    write_spectra(path, [sparse])

    with h5py.File(path, "r") as handle:
        detector = handle[layout.SPECTRUM_DETECTOR_GROUP]
        assert "count_time" in detector
        assert "azimuthal_angle" not in detector
        assert "solid_angle" not in detector
        # No real time was reported, so no dead time can be derived, and
        # none is invented.
        assert "dead_time" not in detector
        assert layout.SOURCE_GROUP not in handle


def test_a_spectrum_image_gets_real_spatial_axes_from_the_scan_geometry(tmp_path):
    """
    ``axis_j``/``axis_i`` in nanometres, through the path a scanned frame uses.

    Reusing ``fov_size_nm`` rather than inventing a second geometry key
    is the point. A spectrum image and a HAADF image of the same region
    should describe the same region, and they will if they are calibrated
    from the same number by the same code — which is what
    ``FrameCalibration.from_field_size`` is.
    """
    path = tmp_path / "map.nxs"
    height, width = _MAP_SHAPE
    cube = _spectrum(
        0,
        data=np.ones((height, width, _CHANNELS), dtype="uint32"),
        metadata={"fov_size_nm": (80.0, 100.0)},
    )
    write_spectra(path, [cube])

    recording = read_spectra(path)
    assert recording.is_map
    assert recording.data.shape == (height, width, _CHANNELS)
    assert recording.navigation.y.kind is AxisKind.REAL_SPACE
    assert recording.navigation.y.scale == pytest.approx(80.0 / height)
    assert recording.navigation.x.scale == pytest.approx(100.0 / width)
    assert recording.navigation.y.units == "nm"

    with h5py.File(path, "r") as handle:
        data_group = handle[layout.SPECTRUM_DATA_GROUP]
        assert list(data_group.attrs["axes"]) == ["axis_j", "axis_i", "axis_energy"]
        assert data_group.attrs["axis_energy_indices"] == 2  # noqa: PLR2004
        assert handle[f"{layout.METADATA_GROUP}/nxspectrum_layout"][()] == (
            MAP_LAYOUT.encode()
        )


def test_a_map_with_no_reported_geometry_stays_honestly_uncalibrated(tmp_path):
    """
    Bare pixel indices, not a guessed field of view.

    Same rule as everywhere else here: a confidently wrong spatial scale
    is worse than an admitted pixel axis, because every distance measured
    from it is wrong by that factor and nothing says so.
    """
    path = tmp_path / "map.nxs"
    height, width = _MAP_SHAPE
    write_spectra(
        path,
        [_spectrum(0, data=np.ones((height, width, _CHANNELS), dtype="uint32"))],
    )
    recording = read_spectra(path)
    assert not recording.navigation.is_calibrated


def test_spectra_that_do_not_belong_together_are_refused(tmp_path):
    """
    A different energy axis, a different channel count, or a second map.

    One file carries one ``axis_energy`` for the whole stack — that is
    ``NXspectrum``'s ``stack_0d``, not a shortcut — so a spectrum
    acquired at a different dispersion cannot join without being stored
    under an axis that is not its own. Silently accepting it would be the
    worst available outcome: a readable file, plausible numbers, wrong
    energies for part of it.

    The second-map case is refused with the name of the layout that
    *would* hold it (``stack_2d``), so the message tells a future
    implementer what to build rather than only that this failed.
    """
    path = tmp_path / "mixed.nxs"
    with SpectrumWriter(path) as writer:
        writer.append(_spectrum(0))
        with pytest.raises(ValueError, match="energy axis"):
            writer.append(_spectrum(1, energy_scale_ev=5.0))
        with pytest.raises(ValueError, match="channels"):
            writer.append(_spectrum(1, data=np.ones(32, dtype="uint32")))

    map_path = tmp_path / "two-maps.nxs"
    height, width = _MAP_SHAPE
    cube = _spectrum(0, data=np.ones((height, width, _CHANNELS), dtype="uint32"))
    with SpectrumWriter(map_path) as writer:
        writer.append(cube)
        with pytest.raises(ValueError, match="stack_2d"):
            writer.append(cube)


def test_a_line_scan_is_refused_by_name_rather_than_stored_wrongly(tmp_path):
    """
    ``spectrum_1d`` is a real ``NXspectrum`` layout this writer does not produce.

    Refusing with the name of the thing that is missing is the difference
    between "this does not work" and "here is what to implement". A line
    scan is a perfectly ordinary EDX acquisition — AZtec and ESPRIT both
    offer one — so this is a gap rather than an impossibility, and the
    error says which.
    """
    path = tmp_path / "line.nxs"
    line = _spectrum(0, data=np.ones((5, _CHANNELS), dtype="uint32"))
    with (
        SpectrumWriter(path) as writer,
        pytest.raises(ValueError, match="spectrum_1d"),
    ):
        writer.append(line)


def test_per_spectrum_metadata_is_kept_per_spectrum(tmp_path):
    """
    One JSON object each, not just the first one's.

    The frame writer learned this the hard way (architecture review,
    §1.4): keeping only the first frame's metadata silently threw away
    exactly what a parameter sweep varies. The same applies here and more
    so, because live time and dead time genuinely differ spectrum to
    spectrum on a real detector.
    """
    path = tmp_path / "eds.nxs"
    write_spectra(
        path,
        [
            _spectrum(index, metadata={"live_time_s": 1.0 + index})
            for index in range(_SPECTRA)
        ],
    )
    recording = read_spectra(path)
    assert [entry["live_time_s"] for entry in recording.metadata] == [1.0, 2.0, 3.0]

    with h5py.File(path, "r") as handle:
        stored = [json.loads(entry) for entry in handle[layout.SPECTRUM_METADATA][()]]
    assert [entry["spectrum_index"] for entry in stored] == [0, 1, 2]


def test_reading_a_frame_recording_as_spectra_says_which_kind_it_is(tmp_path):
    """
    Not "this file is empty", which would be false and unhelpful.

    A frame recording is a perfectly good file; it is simply the other
    kind. The frame reader's own ``NoFramesError`` docstring makes this
    argument about a different case, and it applies just as much across
    the two layouts: the message names both signal paths and the reader
    that handles the other one.
    """
    from miainwoodpecker.devices.interface import Frame  # noqa: PLC0415
    from miainwoodpecker.storage.nexus import write_frames  # noqa: PLC0415

    path = tmp_path / "frames.nxs"
    write_frames(
        path,
        [
            Frame(
                data=np.zeros((4, 4)),
                timestamp=datetime.datetime(2026, 1, 1, tzinfo=datetime.UTC),
            ),
        ],
    )
    with pytest.raises(layout.NoSpectraError, match="not a spectrum recording"):
        read_spectra(path)


def test_an_unopened_writer_refuses_rather_than_failing_obscurely(tmp_path):
    """The same contract NexusWriter has: use it as a context manager."""
    writer = SpectrumWriter(tmp_path / "unused.nxs")
    with pytest.raises(RuntimeError, match="not open"):
        writer.append(_spectrum(0))


def _projected_frame(
    *,
    kind: str = "energy",
    units: str = "eV",
    scale: float = _SCALE_EV,
    offset: float = _OFFSET_EV,
    axes: dict | None = None,
    **metadata: object,
) -> Frame:
    """
    Build the 1D frame a camera's projected readout produces.

    Parameters
    ----------
    kind : str
        The surviving axis's calibration kind.
    units : str
        The surviving axis's units.
    scale : float
        Dispersion per channel, in ``units``.
    offset : float
        The axis value at channel 0, in ``units``.
    axes : dict | None
        Replace the whole ``calibration`` mapping; ``{}`` means the frame
        carries none at all.
    **metadata : object
        Extra frame metadata, overriding the defaults.

    Returns
    -------
    Frame
        A 1D frame shaped like one a projected EELS readout delivers.
    """
    calibration = (
        {"x": {"kind": kind, "scale": scale, "offset": offset, "units": units}}
        if axes is None
        else axes
    )
    return Frame(
        data=np.arange(_CHANNELS, dtype="float32"),
        timestamp=datetime.datetime(2024, 5, 6, 7, 8, 9, tzinfo=datetime.UTC),
        metadata={
            "device_id": "eels_camera",
            "camera_type": "eels",
            "readout": PROJECTED_READOUT,
            "projected_by": "server",
            "exposure_ms": 25.0,
            **({"calibration": calibration} if calibration else {}),
            **metadata,
        },
    )


def test_a_projected_frame_keeps_its_dispersive_axis_verbatim():
    """
    The surviving axis's calibration crosses unchanged; the summed axis is gone.

    This is the whole correctness claim of the projected readout: the
    non-dispersive direction was summed away, so nothing about the energy
    axis may move. An offset or dispersion altered here would put every
    eV in the recording wrong with the file still perfectly readable.
    """
    spectrum = spectrum_from_projected_frame(_projected_frame())

    assert spectrum.energy_offset_ev == pytest.approx(_OFFSET_EV)
    assert spectrum.energy_scale_ev == pytest.approx(_SCALE_EV)
    assert spectrum.channel_count == _CHANNELS
    assert spectrum.navigation_shape == ()
    assert spectrum.timestamp.tzinfo is not None


def test_a_projected_frames_energy_axis_is_converted_into_this_layouts_ev():
    """
    A meV axis arrives as eV, converted once rather than left loose.

    ``SpectrumWriter``'s layout is electronvolts by definition, and a
    camera calibrated in meV is an ordinary high-resolution EELS setup —
    so the conversion has to happen somewhere, and doing it here means
    the number written and the number read back are the same quantity.
    """
    spectrum = spectrum_from_projected_frame(
        _projected_frame(units="meV", scale=200.0, offset=-40_000.0),
    )

    assert spectrum.energy_scale_ev == pytest.approx(0.2)
    assert spectrum.energy_offset_ev == pytest.approx(-40.0)


def test_an_eels_camera_readout_says_so_where_the_layout_cannot():
    """
    ``technique`` is stamped, because the shape stops distinguishing here.

    Once a projected EELS readout is in the same ``NXspectrum`` layout as
    EDX the two files are the same shape, and the only thing standing
    between eXSpy and fitting X-ray lines to electron energy losses is
    the recording saying which it is.
    """
    spectrum = spectrum_from_projected_frame(_projected_frame())
    assert spectrum.metadata[TECHNIQUE_KEY] == EELS_TECHNIQUE
    # The frame's own facts travel with it, so the exposure and who did
    # the summing survive into the file's per-spectrum metadata.
    assert spectrum.metadata["projected_by"] == "server"
    assert spectrum.metadata["exposure_ms"] == pytest.approx(25.0)


def test_an_adapter_that_already_named_its_technique_is_not_overruled():
    """
    A camera that says what it is keeps saying it.

    ``camera_type == "eels"`` is evidence, not authority: a spectrometer
    doing cathodoluminescence on the same camera would be mislabelled by
    an unconditional stamp, and the adapter is the layer that knows.
    """
    spectrum = spectrum_from_projected_frame(_projected_frame(technique="cl"))
    assert spectrum.metadata[TECHNIQUE_KEY] == "cl"


def test_a_camera_that_is_not_a_spectrometer_claims_no_technique():
    """
    Nothing is stamped when nothing said so, which is what keeps the claim real.

    An absent ``technique`` means "the recording does not say" — the
    honest state — rather than a default the EELS loader would then
    accept as a claim.
    """
    spectrum = spectrum_from_projected_frame(
        _projected_frame(camera_type="ronchigram"),
    )
    assert TECHNIQUE_KEY not in spectrum.metadata


def test_an_image_frame_is_refused_by_name_rather_than_flattened():
    """
    A 2D frame belongs to ``NexusWriter``, and is told so.

    Flattening it here would be the layout guess this project refuses on
    both sides: ``NexusWriter`` declines 1D rather than inventing a
    spectrum layout, and this declines 2D rather than inventing a
    projection direction.
    """
    frame = Frame(
        data=np.zeros((4, _CHANNELS), dtype="float32"),
        timestamp=datetime.datetime.now(tz=datetime.UTC),
        metadata={},
    )
    with pytest.raises(ValueError, match="belongs to NexusWriter"):
        spectrum_from_projected_frame(frame)


def test_a_projected_frame_with_no_calibration_is_refused():
    """
    A spectrum cannot exist without its energy axis, so none is invented.

    An uncalibrated *image* is still an image and this project supports
    that state deliberately. An array of counts with no dispersion is not
    a spectrum, and writing one with a fabricated axis would produce a
    file whose every energy is made up.
    """
    with pytest.raises(ValueError, match="cannot exist without its energy axis"):
        spectrum_from_projected_frame(_projected_frame(axes={}))


def test_a_projected_frame_whose_axis_is_not_energy_is_refused():
    """
    Projecting a diffraction camera gives counts against angle, not a spectrum.

    The refusal is what keeps the ``NXspectrum`` layout meaning what it
    says: its ``axis_energy`` is of NeXus unit category ``NX_ENERGY``, so
    a summed Ronchigram has no home in it and should not be given one.
    """
    with pytest.raises(ValueError, match="not energy"):
        spectrum_from_projected_frame(
            _projected_frame(kind="angle", units="rad", scale=0.001, offset=0.0),
        )


def test_a_projected_frame_round_trips_through_the_spectrum_layout(tmp_path):
    """
    The end of the route: a projected frame reads back as an eV spectrum.

    Written through the same ``SpectrumWriter`` an EDX detector uses and
    read back through the same ``read_spectra``, which is the point of
    routing it here rather than growing a third layout.
    """
    path = tmp_path / "projected.nxs"
    write_spectra(path, [spectrum_from_projected_frame(_projected_frame())])

    recording = read_spectra(path)
    assert recording.data.shape == (1, _CHANNELS)
    assert recording.energy_offset_ev == pytest.approx(_OFFSET_EV)
    assert recording.energy_scale_ev == pytest.approx(_SCALE_EV)
    assert recording.metadata[0][TECHNIQUE_KEY] == EELS_TECHNIQUE
    with h5py.File(path, "r") as handle:
        # NXdetector's description is where the technique lands, which is
        # what the typed loaders read to tell EELS from EDX.
        description = handle[f"{layout.SPECTRUM_DETECTOR_GROUP}/description"][()]
    assert description.decode() == EELS_TECHNIQUE
