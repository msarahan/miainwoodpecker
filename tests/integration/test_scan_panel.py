"""
Integration tests: detector checkboxes, scan profiles, and the preview.

The panel now claims two things the old one did not — that several
detectors can be live at once, and that dwell and resolution differ by
what you are doing while the region stays put. These tests are about
those claims being true rather than merely displayed.

Skipped without a display (see conftest.py).
"""

import time

import pytest

pytest.importorskip("napari", reason="requires the 'viewer' extra")

import napari

from miainwoodpecker.viewer import preferences, profiles
from miainwoodpecker.viewer.live import LiveInstrumentWidget
from miainwoodpecker.viewer.preview import build_preview_devices

_DEADLINE_S = 10.0
_TWO_CHANNELS = 2
_A_DWELL_US = 3.5


def _open() -> tuple:
    """
    Open a widget over preview devices.

    Returns
    -------
    tuple
        The viewer, the widget, and the devices.
    """
    devices = build_preview_devices(scan=True, camera=True)
    viewer = napari.Viewer(show=False)
    widget = LiveInstrumentWidget(
        viewer,
        devices.scanner,
        cameras=devices.cameras,
        instrument=devices.instrument,
    )
    return viewer, widget, devices


def _wait_until(condition) -> bool:
    """
    Drive the display and poll a condition until it holds.

    Parameters
    ----------
    condition : Callable[[], bool]
        What to wait for.

    Returns
    -------
    bool
        Whether it held before the deadline.
    """
    deadline = time.monotonic() + _DEADLINE_S
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(0.01)
    return False


class TestDetectorChecks:
    """Several detectors at once, which is what a scanned instrument does."""

    def test_every_detector_gets_a_checkbox(self):
        """A choice of one described serial acquisition, which is the rare case."""
        viewer, widget, devices = _open()
        try:
            assert set(widget._channel_checks) == set(  # noqa: SLF001
                devices.scanner.channel_names,
            )
        finally:
            widget.shutdown()
            viewer.close()

    def test_enabling_a_second_detector_gives_it_a_layer(self):
        """
        Both checked detectors show live, from one pass.

        The whole point of the checkboxes: checking two boxes and seeing
        one image would be a panel that lies about the hardware.
        """
        viewer, widget, _ = _open()
        try:
            for check in widget._channel_checks.values():  # noqa: SLF001
                check.setChecked(True)
            widget.start_scan()

            def both_layers() -> bool:
                widget.refresh_display()
                # Membership by name: iterating a napari LayerList yields
                # Layer objects, so a set comparison against names is
                # always false and would make this pass vacuously never.
                return all(
                    name in viewer.layers
                    for name in ("Scan (HAADF)", "Scan (MAADF)")
                )

            assert _wait_until(both_layers)
        finally:
            widget.shutdown()
            viewer.close()

    def test_the_live_channels_come_from_one_pass(self):
        """
        Both layers are fed from the same ``scan_frames`` call.

        Two single-channel loops would cost twice the dose and let the
        specimen drift between the images, which is what the shared
        ``scan_pass_id`` exists to rule out.
        """
        viewer, widget, _ = _open()
        try:
            for check in widget._channel_checks.values():  # noqa: SLF001
                check.setChecked(True)
            widget.start_scan()
            assert _wait_until(
                lambda: len(widget._scan_loop.latest_frames()) == _TWO_CHANNELS,  # noqa: SLF001
            )

            frames = widget._scan_loop.latest_frames()  # noqa: SLF001
            identities = {frame.metadata["scan_pass_id"] for frame in frames}
            assert len(identities) == 1
        finally:
            widget.shutdown()
            viewer.close()

    def test_unchecking_the_last_detector_is_refused(self):
        """
        A scan with nothing enabled reads nothing out.

        Refused with an explanation rather than allowed, because the
        state is not a preference an operator could mean and a silently
        dead Start button teaches nothing.
        """
        viewer, widget, _ = _open()
        try:
            names = list(widget._channel_checks)  # noqa: SLF001
            for name in names:
                widget._channel_checks[name].setChecked(False)  # noqa: SLF001

            assert widget.enabled_channels()
            assert "at least one detector" in widget._scan_status.text()  # noqa: SLF001
        finally:
            widget.shutdown()
            viewer.close()

    def test_unchecking_a_detector_drops_its_layer(self):
        """
        A layer left behind keeps showing a feed that has stopped.

        Which reads as a live image that has silently frozen — worse
        than the layer being gone.
        """
        viewer, widget, _ = _open()
        try:
            for check in widget._channel_checks.values():  # noqa: SLF001
                check.setChecked(True)
            widget.start_scan()
            def maadf_showing() -> bool:
                widget.refresh_display()
                return "Scan (MAADF)" in viewer.layers

            assert _wait_until(maadf_showing)

            widget._channel_checks["MAADF"].setChecked(False)  # noqa: SLF001

            assert "Scan (MAADF)" not in viewer.layers
        finally:
            widget.shutdown()
            viewer.close()

    def test_the_selection_is_remembered(self):
        """
        Which detectors an operator uses follows them, not the shift.

        Written to the config directory rather than the session, and
        read back by the next widget built.
        """
        viewer, widget, _ = _open()
        try:
            for check in widget._channel_checks.values():  # noqa: SLF001
                check.setChecked(True)
        finally:
            widget.shutdown()
            viewer.close()

        stored = preferences.load()
        assert sorted(stored["scan_channels"]) == ["HAADF", "MAADF"]

        viewer, widget, _ = _open()
        try:
            assert len(widget.enabled_channels()) == _TWO_CHANNELS
        finally:
            widget.shutdown()
            viewer.close()


class TestProfiles:
    """Three ways to scan one region, and the region does not move."""

    def test_each_profile_gets_its_own_controls(self):
        """View, preview and acquire are all visible at once."""
        viewer, widget, _ = _open()
        try:
            assert set(widget._profile_controls) == set(  # noqa: SLF001
                profiles.PROFILE_NAMES,
            )
        finally:
            widget.shutdown()
            viewer.close()

    def test_the_field_of_view_is_shared(self):
        """
        Switching profiles must not move the region under the probe.

        A profile carrying its own field of view would move the specimen
        out from under the operator at the moment they were happiest
        with it.
        """
        viewer, widget, _ = _open()
        try:
            widget._fov_spin.setValue(42.0)  # noqa: SLF001

            for name in profiles.PROFILE_NAMES:
                assert widget.scan_parameters(name).fov_nm == 42.0  # noqa: PLR2004
        finally:
            widget.shutdown()
            viewer.close()

    def test_profiles_differ_in_dwell_and_size(self):
        """Each profile carries its own settings, which is the whole idea."""
        viewer, widget, _ = _open()
        try:
            widget._profile_controls[profiles.PREVIEW][0].setValue(_A_DWELL_US)  # noqa: SLF001

            assert widget.scan_parameters(profiles.PREVIEW).pixel_time_us == (
                _A_DWELL_US
            )
            assert widget.scan_parameters(profiles.VIEW).pixel_time_us != _A_DWELL_US
        finally:
            widget.shutdown()
            viewer.close()

    def test_the_live_loop_runs_the_view_profile(self):
        """
        Changing Acquire must not disturb a running live view.

        Preview and Acquire are read when their own action is taken.
        """
        viewer, widget, _ = _open()
        try:
            widget._profile_controls[profiles.VIEW][0].setValue(_A_DWELL_US)  # noqa: SLF001
            assert widget._scan_request[0].pixel_time_us == _A_DWELL_US  # noqa: SLF001
        finally:
            widget.shutdown()
            viewer.close()

    def test_profiles_are_remembered(self):
        """Settings an operator arrived at over a shift survive a restart."""
        viewer, widget, _ = _open()
        try:
            widget._profile_controls[profiles.ACQUIRE][0].setValue(_A_DWELL_US)  # noqa: SLF001
        finally:
            widget.shutdown()
            viewer.close()

        viewer, widget, _ = _open()
        try:
            assert widget.scan_parameters(profiles.ACQUIRE).pixel_time_us == (
                _A_DWELL_US
            )
        finally:
            widget.shutdown()
            viewer.close()


class TestPreview:
    """A focus check: shown, and deliberately not kept."""

    def test_preview_updates_the_display(self):
        """It is for looking at, so it has to reach the screen."""
        viewer, widget, _ = _open()
        try:
            widget.preview_scan()
            assert "Scan (HAADF)" in viewer.layers
        finally:
            widget.shutdown()
            viewer.close()

    def test_preview_saves_nothing(self, tmp_path):
        """
        A focus check that littered the session with files would stop being used.

        Asserted with a session attached, so "nothing was written" is a
        real observation rather than a consequence of having nowhere to
        write.
        """
        from miainwoodpecker.storage.session import Session  # noqa: PLC0415

        viewer, widget, _ = _open()
        try:
            widget.set_session(Session(tmp_path / "shift"))
            widget.preview_scan()

            assert widget.session.recordings() == []
            assert "not saved" in widget._scan_status.text()  # noqa: SLF001
        finally:
            widget.shutdown()
            viewer.close()

    def test_preview_uses_the_preview_profile(self):
        """
        Its whole purpose is a signal-to-noise the live view cannot reach.

        So it must not quietly run at the view profile's dwell.
        """
        viewer, widget, _ = _open()
        try:
            widget._profile_controls[profiles.PREVIEW][0].setValue(_A_DWELL_US)  # noqa: SLF001
            widget.preview_scan()

            assert f"{_A_DWELL_US:g} us" in widget._scan_status.text()  # noqa: SLF001
        finally:
            widget.shutdown()
            viewer.close()
