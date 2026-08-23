"""
Integration tests: acquiring a spectrum image from the window.

Most of these are about the *refusal*. Synchronised acquisition is a
hardware fact, and a backend without the wiring cannot do it — producing
a plausible cube anyway would be the worst outcome available, since it
has the same shape as a real one and every per-pixel number computed
from it would be computed against a position nothing established.

Skipped without a display (see conftest.py).
"""

import pathlib
import time

import pytest

pytest.importorskip("napari", reason="requires the 'viewer' extra")

import h5py
import napari

from miainwoodpecker.devices.interface import (
    ENERGY_OFFSET_CONTROL,
    IMAGE_READOUT,
    PROJECTED_READOUT,
    ScanParameters,
)
from miainwoodpecker.storage.passes import read_pass
from miainwoodpecker.storage.session import Session
from miainwoodpecker.viewer.live import LiveInstrumentWidget
from miainwoodpecker.viewer.preview import _EELS_TARGET, build_preview_devices

_A_SMALL_GRID = 4
_TWO_CHANNELS = 2
# Two cameras, because the preview decides a camera's kind from the
# target name it is served under and a single one takes the neutral name.
_TWO_CAMERAS = 2
_FOUR_AXES = 4


class _UnsynchronisedScanner:
    """
    A scan unit with no synchronised mode, like every real backend today.

    Wraps the preview's scanner and withholds exactly the two methods
    :class:`SynchronisedScanner` asks for, which is what usim and a
    column whose trigger is not wired both look like from here.
    """

    def __init__(self, inner: object) -> None:
        self._inner = inner

    @property
    def scanner_id(self) -> str:
        """Return the wrapped scanner's id."""
        return self._inner.scanner_id

    @property
    def channel_names(self) -> object:
        """Return the wrapped scanner's channels."""
        return self._inner.channel_names

    def scan_frame(self, parameters: object, channel: int = 0) -> object:
        """Scan one frame through the wrapped scanner."""
        return self._inner.scan_frame(parameters, channel)

    def scan_frames(self, parameters: object, channels: object) -> object:
        """Scan one pass through the wrapped scanner."""
        return self._inner.scan_frames(parameters, channels)

    def close(self) -> None:
        """Release the wrapped scanner."""
        self._inner.close()



_DEADLINE_S = 30.0


def _finish_pass(widget: LiveInstrumentWidget) -> None:
    """
    Drive the display poll until the synchronised pass has finished.

    A spectrum image runs on a worker thread now, so that the window
    keeps repainting and can show the map building. Tests therefore have
    to do what the display timer does — call ``refresh_display`` — rather
    than reading a status line the instant they asked for the pass.

    Returns immediately when nothing is running, so a call after a
    refusal is harmless.

    Parameters
    ----------
    widget : LiveInstrumentWidget
        The widget whose pass to wait for.

    Raises
    ------
    AssertionError
        If the pass does not finish before the deadline.
    """
    deadline = time.monotonic() + _DEADLINE_S
    while time.monotonic() < deadline:
        widget.refresh_display()
        if widget._pass_job is None:  # noqa: SLF001
            return
        time.sleep(0.005)
    msg = "the spectrum image did not finish"
    raise AssertionError(msg)


def _open(
    tmp_path: pathlib.Path,
    *,
    scanner: object = None,
    session: bool = True,
) -> tuple:
    """
    Open a widget over preview devices, optionally with a given scanner.

    Parameters
    ----------
    tmp_path : pathlib.Path
        Where the session is written.
    scanner : object
        Scanner to use instead of the preview's own, or None.
    session : bool
        Whether to attach a session.

    Returns
    -------
    tuple
        The viewer, the widget, and the devices.
    """
    devices = build_preview_devices(scan=True, camera=True)
    viewer = napari.Viewer(show=False)
    widget = LiveInstrumentWidget(
        viewer,
        scanner if scanner is not None else devices.scanner,
        cameras=devices.cameras,
        instrument=devices.instrument,
    )
    if session:
        widget.set_session(Session(tmp_path / "shift"))
    widget._positions_spin.setValue(_A_SMALL_GRID)  # noqa: SLF001
    return viewer, widget, devices


def test_a_backend_without_synchronisation_refuses_and_says_why(tmp_path):
    """
    The refusal is the feature on every backend but the preview.

    A silent no-op would look like a slow acquisition; a fabricated cube
    would look like a real one. Naming the missing capability is the only
    outcome an operator can act on.
    """
    devices = build_preview_devices(scan=True, camera=True)
    viewer, widget, _ = _open(
        tmp_path, scanner=_UnsynchronisedScanner(devices.scanner),
    )
    try:
        widget.acquire_spectrum_image()
        _finish_pass(widget)

        status = widget._recording_status.text()  # noqa: SLF001
        assert "cannot acquire a spectrum image" in status
        assert "synchronised" in status
        assert not list((tmp_path / "shift").glob("*spectrum*"))
    finally:
        widget.shutdown()
        viewer.close()


def test_acquiring_with_no_session_is_refused(tmp_path):
    """Nothing is saved without somewhere to save it."""
    viewer, widget, _ = _open(tmp_path, session=False)
    try:
        widget.acquire_spectrum_image()
        _finish_pass(widget)
        assert "no session" in widget._recording_status.text()  # noqa: SLF001
    finally:
        widget.shutdown()
        viewer.close()


def test_a_scanner_with_no_wired_camera_refuses(tmp_path):
    """
    A synchronisable scan unit with nothing wired to it still cannot.

    Distinct from the capability refusal above, and worth its own
    message: the fix is different — wire a detector, rather than use a
    different instrument.
    """
    devices = build_preview_devices(scan=True, camera=False)
    viewer = napari.Viewer(show=False)
    widget = LiveInstrumentWidget(
        viewer,
        devices.scanner,
        cameras={},
        instrument=devices.instrument,
    )
    widget.set_session(Session(tmp_path / "shift"))
    widget._positions_spin.setValue(_A_SMALL_GRID)  # noqa: SLF001
    try:
        widget.acquire_spectrum_image()
        _finish_pass(widget)
        assert "no camera is wired" in widget._recording_status.text()  # noqa: SLF001
    finally:
        widget.shutdown()
        viewer.close()


def test_a_pass_acquires_and_saves(tmp_path):
    """
    The workflow's last step, end to end on the one backend that can.

    Acquires into the session and reads the file back, so this covers
    the device, the writer and the wiring between them at once.
    """
    viewer, widget, _ = _open(tmp_path)
    try:
        widget.acquire_spectrum_image()
        _finish_pass(widget)

        status = widget._recording_status.text()  # noqa: SLF001
        assert "saved" in status, status
        files = sorted((tmp_path / "shift").glob("*spectrum-image*"))
        assert len(files) == 1

        recording = read_pass(files[0])
        assert recording.scan_sync == "scanner"
        assert recording.signals["data_camera"][:2] == (
            _A_SMALL_GRID,
            _A_SMALL_GRID,
        )
    finally:
        widget.shutdown()
        viewer.close()


def test_the_saved_pass_carries_its_image_channels_too(tmp_path):
    """
    A 4D pass keeps the survey images from the same traversal.

    They are free — the probe went there anyway — and they are what an
    operator navigates the cube with afterwards.
    """
    viewer, widget, _ = _open(tmp_path)
    try:
        widget.acquire_spectrum_image()
        _finish_pass(widget)
        path = next((tmp_path / "shift").glob("*spectrum-image*"))

        signals = read_pass(path).signals
        assert "data_HAADF" in signals
        assert "data_MAADF" in signals
        assert len(signals) == _TWO_CHANNELS + 1
    finally:
        widget.shutdown()
        viewer.close()


def test_a_pass_file_does_not_break_the_recordings_list(tmp_path):
    """
    The Recordings list survives a file it does not understand.

    A pass carries no frame stack, so ``recordings()`` reports it with a
    frame count of zero. That is misleading — it reads as an empty
    recording rather than as a 4D dataset — and it is *listed as a known
    limitation* rather than fixed here: teaching the list about passes
    belongs with the panel work, and the thing that must not happen
    meanwhile is the whole list raising because one file is a pass.
    """
    viewer, widget, _ = _open(tmp_path)
    try:
        widget.acquire_spectrum_image()
        _finish_pass(widget)

        recordings = widget.session.recordings()
        assert len(recordings) == 1
        assert recordings[0].frame_count == 0
    finally:
        widget.shutdown()
        viewer.close()


def test_the_saved_cube_is_not_empty(tmp_path):
    """The acquisition wrote through to disk rather than allocating only."""
    viewer, widget, _ = _open(tmp_path)
    try:
        widget.acquire_spectrum_image()
        _finish_pass(widget)
        path = next((tmp_path / "shift").glob("*spectrum-image*"))

        with h5py.File(path, "r") as handle:
            assert handle["entry/data_camera/data"][0, 0].any()
    finally:
        widget.shutdown()
        viewer.close()


def _open_with_spectrometer(tmp_path: pathlib.Path, *, projected: bool = True) -> tuple:
    """
    Open a widget over an instrument that has an EEL spectrometer wired.

    Parameters
    ----------
    tmp_path : pathlib.Path
        Where the session is written.
    projected : bool
        Whether to put the spectrometer into its projected readout, which
        is what decides whether the pass yields spectra or a 4D stack.

    Returns
    -------
    tuple
        The viewer, the widget, and the devices.
    """
    devices = build_preview_devices(scan=True, camera=True, camera_count=_TWO_CAMERAS)
    viewer = napari.Viewer(show=False)
    widget = LiveInstrumentWidget(
        viewer,
        devices.scanner,
        cameras=devices.cameras,
        instrument=devices.instrument,
    )
    widget.set_session(Session(tmp_path / "shift"))
    widget._positions_spin.setValue(_A_SMALL_GRID)  # noqa: SLF001
    widget._sync_target_combo.setCurrentText(_EELS_TARGET)  # noqa: SLF001
    if projected:
        widget.set_camera_readout(_EELS_TARGET, PROJECTED_READOUT)
    return viewer, widget, devices


class TestAcquiringAnEELSSpectrumImage:
    """
    The workflow this whole change exists for, driven from the window.

    Two clicks from a running instrument: put the spectrometer into its
    projected readout, then acquire. Everything else — which detector,
    what shape the file needs, whether the result is spectra or a stack —
    follows from the state the operator can see on the panel.
    """

    def test_the_readout_control_configures_the_device(self, tmp_path):
        """
        Applied immediately, unlike the exposure and binning beside it.

        Readout decides the rank of every frame the detector produces, so
        a camera whose live view imaged while its next acquisition
        projected would be a camera in two states at once.
        """
        viewer, widget, devices = _open_with_spectrometer(tmp_path)
        try:
            camera = devices.cameras[_EELS_TARGET]
            assert camera.parameters().readout == PROJECTED_READOUT
        finally:
            widget.shutdown()
            viewer.close()

    def test_a_camera_that_cannot_project_says_so_on_the_panel(self, tmp_path):
        """
        The refusal reaches the operator, which is why the control offers it.

        There is no capability to ask a `Camera` about, so the modes are
        offered and `configure` answers — and an operator who never sees
        the answer learns nothing about why their Ronchigram camera is
        not a spectrometer.
        """
        viewer, widget, devices = _open_with_spectrometer(tmp_path, projected=False)
        try:
            ronchigram = next(
                name for name in devices.cameras if name != _EELS_TARGET
            )
            widget.set_camera_readout(ronchigram, PROJECTED_READOUT)

            binding = widget._binding(ronchigram)  # noqa: SLF001
            assert "refused" in binding.status.text()
            assert devices.cameras[ronchigram].parameters().readout == IMAGE_READOUT
            # And the combo went back, so the panel is not showing a mode
            # the device is not in.
            assert binding.readout_combo.currentText() == IMAGE_READOUT
        finally:
            widget.shutdown()
            viewer.close()

    def test_acquiring_saves_a_spectrum_image(self, tmp_path):
        """
        End to end: the device, the writer, and the wiring between them.

        The status line names what actually landed rather than what the
        button is called — an operator who left the spectrometer imaging
        has a 4D stack, and this is the first place they could notice.
        """
        viewer, widget, _ = _open_with_spectrometer(tmp_path)
        try:
            widget.acquire_spectrum_image()
            _finish_pass(widget)

            status = widget._recording_status.text()  # noqa: SLF001
            assert "spectrum image saved" in status, status
            path = next((tmp_path / "shift").glob("*spectrum-image*"))
            signals = read_pass(path).signals
            assert signals[f"data_{_EELS_TARGET}"][:2] == (
                _A_SMALL_GRID,
                _A_SMALL_GRID,
            )
        finally:
            widget.shutdown()
            viewer.close()

    def test_the_spectra_land_beside_the_scan_channels(self, tmp_path):
        """
        One traversal, so the image channels come free and are kept.

        They are what an operator navigates a spectrum image with
        afterwards, and they share its probe positions by construction
        rather than by assumption.
        """
        viewer, widget, _ = _open_with_spectrometer(tmp_path)
        try:
            widget.acquire_spectrum_image()
            _finish_pass(widget)
            path = next((tmp_path / "shift").glob("*spectrum-image*"))

            signals = read_pass(path).signals
            assert "data_HAADF" in signals
            assert "data_MAADF" in signals
        finally:
            widget.shutdown()
            viewer.close()

    def test_the_stored_spectra_carry_an_energy_axis(self, tmp_path):
        """
        A spectrum cannot exist without its energy axis, on disk either.

        Written in ``NXspectrum``'s vocabulary, so a reader that knows
        how to find spectra finds these under the name it looks for.
        """
        viewer, widget, _ = _open_with_spectrometer(tmp_path)
        try:
            widget.acquire_spectrum_image()
            _finish_pass(widget)
            path = next((tmp_path / "shift").glob("*spectrum-image*"))

            with h5py.File(path, "r") as handle:
                group = handle[f"entry/data_{_EELS_TARGET}"]
                units = group["axis_energy"].attrs["units"]
                assert (units.decode() if isinstance(units, bytes) else units) == "eV"
                assert group["intensity"][0, 0].any()
        finally:
            widget.shutdown()
            viewer.close()

    def test_leaving_the_spectrometer_imaging_gives_a_stack_and_says_so(
        self, tmp_path,
    ):
        """
        A spectrometer read out in 2D is a real experiment, not an error.

        What makes it a spectrometer is that one axis is calibrated in
        energy, not that the other one has been summed away — so this is
        acquired and stored rather than refused, and the status line is
        honest about which of the two it was.
        """
        viewer, widget, _ = _open_with_spectrometer(tmp_path, projected=False)
        try:
            widget.acquire_spectrum_image()
            _finish_pass(widget)

            status = widget._recording_status.text()  # noqa: SLF001
            assert "4D stack saved" in status, status
            path = next((tmp_path / "shift").glob("*spectrum-image*"))
            assert len(read_pass(path).signals[f"data_{_EELS_TARGET}"]) == _FOUR_AXES
        finally:
            widget.shutdown()
            viewer.close()

    def test_a_target_the_scanner_cannot_synchronise_is_refused(self, tmp_path):
        """
        The panel's choice is honoured, and not silently replaced.

        Falling back to the first target would acquire against a detector
        the operator did not choose and store it under that detector's
        name — a file that is wrong in a way nothing about it looks
        wrong.
        """
        viewer, widget, _ = _open_with_spectrometer(tmp_path)
        try:
            widget._sync_target_combo.addItem("not_fitted")  # noqa: SLF001
            widget._sync_target_combo.setCurrentText("not_fitted")  # noqa: SLF001
            widget.acquire_spectrum_image()
            _finish_pass(widget)

            assert "not one this scan unit can synchronise" in (
                widget._recording_status.text()  # noqa: SLF001
            )
            assert not list((tmp_path / "shift").glob("*spectrum-image*"))
        finally:
            widget.shutdown()
            viewer.close()
class _FixedGeometryScanner:
    """
    A scan unit that can acquire exactly one grid, as a replay device can.

    Wraps the preview's scanner and adds the one method that says so, so
    the viewer's handling of such a device is testable without anyone's
    recorded data. The grid is non-square on purpose: that is the shape a
    real recording turns out to have, and it is the shape the panel's
    square Positions spin box cannot express.
    """

    def __init__(self, inner: object, parameters: ScanParameters) -> None:
        self._inner = inner
        self._parameters = parameters
        self.requested: ScanParameters | None = None

    def native_parameters(self) -> ScanParameters:
        """Return the only geometry this device can acquire."""
        return self._parameters

    @property
    def scanner_id(self) -> str:
        """Return the wrapped scanner's id."""
        return self._inner.scanner_id

    @property
    def channel_names(self) -> object:
        """Return the wrapped scanner's channels."""
        return self._inner.channel_names

    def scan_frame(self, parameters: object, channel: int = 0) -> object:
        """Scan one frame through the wrapped scanner."""
        return self._inner.scan_frame(parameters, channel)

    def scan_frames(self, parameters: object, channels: object) -> object:
        """Scan one pass through the wrapped scanner."""
        return self._inner.scan_frames(parameters, channels)

    def synchronised_targets(self) -> object:
        """Return what the wrapped scanner can synchronise."""
        return self._inner.synchronised_targets()

    def scan_synchronised(self, parameters: ScanParameters, **kwargs: object) -> object:
        """Record what was asked for, then acquire it."""
        self.requested = parameters
        return self._inner.scan_synchronised(parameters, **kwargs)

    def close(self) -> None:
        """Release the wrapped scanner."""
        self._inner.close()


class TestADeviceWithOneGeometry:
    """
    A replay device holds the grid the probe actually visited.

    Asking it for the panel's numbers would be refused every time, and an
    operator would have to guess a shape they cannot see - a recording is
    22x25, which a square spin box cannot even express. So the device is
    asked first.
    """

    def test_the_devices_own_grid_is_used(self, tmp_path):
        """The panel's Positions count does not override the device."""
        devices = build_preview_devices(scan=True, camera=True)
        native = ScanParameters(
            height=3, width=5, pixel_time_us=1.0, fov_nm=8.0,
        )
        scanner = _FixedGeometryScanner(devices.scanner, native)
        viewer = napari.Viewer(show=False)
        widget = LiveInstrumentWidget(
            viewer,
            scanner,
            cameras=devices.cameras,
            instrument=devices.instrument,
        )
        widget.set_session(Session(tmp_path / "shift"))
        widget._positions_spin.setValue(_A_SMALL_GRID)  # noqa: SLF001
        try:
            widget.acquire_spectrum_image()
            _finish_pass(widget)

            assert scanner.requested == native
            path = next((tmp_path / "shift").glob("*spectrum-image*"))
            assert read_pass(path).signals["data_camera"][:2] == (3, 5)
        finally:
            widget.shutdown()
            viewer.close()

    def test_the_status_line_names_the_grid_that_was_acquired(self, tmp_path):
        """
        An operator has to be able to see that their spin box was not used.

        Silently acquiring a different grid than the panel shows is the
        kind of surprise that gets noticed at analysis time.
        """
        devices = build_preview_devices(scan=True, camera=True)
        scanner = _FixedGeometryScanner(
            devices.scanner,
            ScanParameters(height=3, width=5, pixel_time_us=1.0, fov_nm=8.0),
        )
        viewer = napari.Viewer(show=False)
        widget = LiveInstrumentWidget(
            viewer, scanner, cameras=devices.cameras,
            instrument=devices.instrument,
        )
        widget.set_session(Session(tmp_path / "shift"))
        try:
            widget.acquire_spectrum_image()
            _finish_pass(widget)
            assert "3x5 positions" in widget._recording_status.text()  # noqa: SLF001
        finally:
            widget.shutdown()
            viewer.close()
class _EnergyOffsetOnlyInstrument:
    """
    An instrument publishing one control and implementing only that one.

    What a replay device is, and what the ``Instrument`` /
    ``InstrumentController`` split exists to support: a partial
    *controller* is a whole *instrument*. Every backend before this one
    implemented all four control methods even when it published fewer,
    so the case was unreachable and the panel had a latent bug in it.
    """

    def describe(self) -> dict:
        """Report one control, as a real partial instrument does."""
        return {
            "backend": "replay",
            "targets": ["scanner"],
            "controls": [ENERGY_OFFSET_CONTROL],
        }

    def stage_size_nm(self) -> float:
        """Return some extent to size a field of view against."""
        return 1000.0

    def available_controls(self) -> list:
        """Return the one control this instrument has."""
        return [ENERGY_OFFSET_CONTROL]

    def park(self) -> None:
        """Do nothing, honestly."""

    def energy_offset_ev(self) -> float:
        """Return the one value this instrument can report."""
        return 160.0


class TestAPartialInstrument:
    """An instrument may implement only the controls it publishes."""

    def test_the_panel_reads_it_without_demanding_the_rest(self):
        """
        The panel used to raise ``AttributeError`` before it could report.

        Its reader table bound every control's method while the table was
        built, so an instrument without ``defocus_nm`` failed outside the
        per-control ``try`` and the window never appeared. A replay
        device publishes only the spectrometer's energy offset, which is
        what made the case reachable.
        """
        devices = build_preview_devices(scan=True, camera=True)
        viewer = napari.Viewer(show=False)
        widget = LiveInstrumentWidget(
            viewer,
            devices.scanner,
            cameras=devices.cameras,
            instrument=_EnergyOffsetOnlyInstrument(),
        )
        try:
            widget.refresh_instrument()

            assert widget._instrument_status.text() == "read"  # noqa: SLF001
        finally:
            widget.shutdown()
            viewer.close()

    def test_an_unpublished_control_gets_no_row(self):
        """
        A dead dial invites hunting for hardware that is not fitted.

        The panel builds rows from ``available_controls``, so this is
        also what keeps the reader table above from being asked for a
        method the instrument has not got.
        """
        devices = build_preview_devices(scan=True, camera=True)
        viewer = napari.Viewer(show=False)
        widget = LiveInstrumentWidget(
            viewer,
            devices.scanner,
            cameras=devices.cameras,
            instrument=_EnergyOffsetOnlyInstrument(),
        )
        try:
            controls = widget._instrument_controls  # noqa: SLF001
            assert list(controls) == [ENERGY_OFFSET_CONTROL]
        finally:
            widget.shutdown()
            viewer.close()
