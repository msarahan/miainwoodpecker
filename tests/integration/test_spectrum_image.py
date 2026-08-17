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

import pytest

pytest.importorskip("napari", reason="requires the 'viewer' extra")

import h5py
import napari

from miainwoodpecker.storage.passes import read_pass
from miainwoodpecker.storage.session import Session
from miainwoodpecker.viewer.live import LiveInstrumentWidget
from miainwoodpecker.viewer.preview import build_preview_devices

_A_SMALL_GRID = 4
_TWO_CHANNELS = 2


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
        path = next((tmp_path / "shift").glob("*spectrum-image*"))

        with h5py.File(path, "r") as handle:
            assert handle["entry/data_camera/data"][0, 0].any()
    finally:
        widget.shutdown()
        viewer.close()
