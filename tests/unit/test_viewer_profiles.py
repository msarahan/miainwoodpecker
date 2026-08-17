"""
Unit tests for scan profiles and the preferences that outlive a session.

No Qt here: profiles are a value type and preferences are a file, so
both are testable without a window.
"""

import json

import pytest

from miainwoodpecker.viewer import preferences, profiles

_A_DWELL_US = 7.5
_A_SIZE_PX = 128
_THREE_PROFILES = 3


class TestScanProfile:
    """A profile is settings no scan could refuse."""

    def test_a_profile_keeps_its_settings(self):
        """The plain case, so the rejections below mean something."""
        profile = profiles.ScanProfile(dwell_us=_A_DWELL_US, size_px=_A_SIZE_PX)
        assert profile.dwell_us == _A_DWELL_US
        assert profile.size_px == _A_SIZE_PX

    def test_a_zero_dwell_is_refused(self):
        """A zero dwell is not a fast scan, it is not a scan."""
        with pytest.raises(ValueError, match="dwell_us"):
            profiles.ScanProfile(dwell_us=0.0, size_px=_A_SIZE_PX)

    def test_a_zero_size_is_refused(self):
        """A zero size is not a small scan either."""
        with pytest.raises(ValueError, match="size_px"):
            profiles.ScanProfile(dwell_us=_A_DWELL_US, size_px=0)


class TestDefaults:
    """The three defaults say what each profile is for."""

    def test_there_are_three(self):
        """View, preview, acquire."""
        assert len(profiles.DEFAULT_PROFILES) == _THREE_PROFILES
        assert set(profiles.DEFAULT_PROFILES) == set(profiles.PROFILE_NAMES)

    def test_dwell_increases_from_view_to_acquire(self):
        """
        Preview sits between the other two, which is its whole definition.

        It exists to judge focus by eye at a signal-to-noise the live
        view cannot reach, without paying for a kept image.
        """
        dwells = [
            profiles.DEFAULT_PROFILES[name].dwell_us
            for name in profiles.PROFILE_NAMES
        ]
        assert dwells == sorted(dwells)
        assert dwells[0] < dwells[1] < dwells[2]

    def test_no_profile_carries_a_field_of_view(self):
        """
        The region is shared, so switching profiles cannot move it.

        A profile with its own field of view would move the specimen out
        from under the operator at the moment they were happiest with it.
        """
        assert not hasattr(profiles.DEFAULT_PROFILES[profiles.VIEW], "fov_nm")


class TestStoredProfiles:
    """Reading back is per profile, so one bad entry costs one profile."""

    def test_nothing_stored_gives_the_defaults(self):
        """A first launch comes up with the defaults."""
        assert profiles.stored_profiles({}) == profiles.DEFAULT_PROFILES

    def test_a_stored_profile_is_restored(self):
        """What the operator set last time is what they get."""
        stored = {
            "scan_profiles": {
                profiles.VIEW: {"dwell_us": _A_DWELL_US, "size_px": _A_SIZE_PX},
            },
        }
        restored = profiles.stored_profiles(stored)
        assert restored[profiles.VIEW] == profiles.ScanProfile(
            dwell_us=_A_DWELL_US, size_px=_A_SIZE_PX,
        )

    def test_one_bad_entry_does_not_cost_the_others(self):
        """
        A typo in one profile leaves the other two alone.

        All-or-nothing parsing would throw away settings the operator
        spent a shift arriving at because one number grew a stray letter.
        """
        stored = {
            "scan_profiles": {
                profiles.VIEW: {"dwell_us": "not a number", "size_px": 4},
                profiles.ACQUIRE: {"dwell_us": _A_DWELL_US, "size_px": _A_SIZE_PX},
            },
        }
        restored = profiles.stored_profiles(stored)

        assert restored[profiles.VIEW] == profiles.DEFAULT_PROFILES[profiles.VIEW]
        assert restored[profiles.ACQUIRE].dwell_us == _A_DWELL_US

    def test_a_stored_profile_that_no_scan_could_run_is_ignored(self):
        """A zero dwell in the file falls back rather than propagating."""
        stored = {"scan_profiles": {profiles.VIEW: {"dwell_us": 0, "size_px": 4}}}
        restored = profiles.stored_profiles(stored)
        assert restored[profiles.VIEW] == profiles.DEFAULT_PROFILES[profiles.VIEW]

    def test_profiles_round_trip_through_storage(self):
        """What ``as_stored`` writes is what ``stored_profiles`` reads."""
        original = dict(profiles.DEFAULT_PROFILES)
        values = {"scan_profiles": profiles.as_stored(original)}
        assert profiles.stored_profiles(values) == original


class TestPreferences:
    """Preferences are best-effort: they never stop the viewer opening."""

    def test_a_missing_file_is_no_preferences(self, tmp_path):
        """A first launch has nothing to read and says so quietly."""
        assert preferences.load(tmp_path / "absent.json") == {}

    def test_preferences_round_trip(self, tmp_path):
        """What is saved is what is loaded."""
        path = tmp_path / "config" / "prefs.json"
        assert preferences.save({"scan_channels": ["HAADF"]}, path)
        assert preferences.load(path) == {"scan_channels": ["HAADF"]}

    def test_a_malformed_file_is_ignored_rather_than_fatal(self, tmp_path):
        """
        A stray brace costs a checkbox, not the application.

        A viewer that will not open because a settings file is corrupt
        is worse than one that forgets a preference.
        """
        path = tmp_path / "prefs.json"
        path.write_text("{ this is not json", encoding="utf-8")
        assert preferences.load(path) == {}

    def test_a_file_that_is_not_an_object_is_ignored(self, tmp_path):
        """Valid JSON of the wrong shape is still unusable."""
        path = tmp_path / "prefs.json"
        path.write_text(json.dumps([1, 2, 3]), encoding="utf-8")
        assert preferences.load(path) == {}

    def test_saving_somewhere_unwritable_is_reported_not_raised(self, tmp_path):
        """
        An operator who cannot write their config can still use the instrument.

        Asserted through a path whose parent is a *file*, so the
        directory creation fails the way a permission problem would.
        """
        blocker = tmp_path / "blocker"
        blocker.write_text("not a directory", encoding="utf-8")
        assert not preferences.save({"a": 1}, blocker / "nested" / "prefs.json")


class TestStoredChannels:
    """A remembered selection is filtered against what is actually fitted."""

    def test_nothing_stored_enables_the_first_detector(self, tmp_path):  # noqa: ARG002
        """
        Coming up with nothing enabled would look like a broken instrument.

        A scan with no detector selected produces no data at all.
        """
        assert preferences.stored_channels({}, ["HAADF", "MAADF"]) == ["HAADF"]

    def test_a_stored_selection_is_restored(self):
        """Both detectors come back checked."""
        values = {"scan_channels": ["HAADF", "MAADF"]}
        assert preferences.stored_channels(values, ["HAADF", "MAADF"]) == [
            "HAADF",
            "MAADF",
        ]

    def test_a_detector_this_column_lacks_is_dropped(self):
        """
        The file outlives the instrument it was written against.

        Same rule the Instrument panel follows for controls: no widget
        for hardware that is not fitted.
        """
        values = {"scan_channels": ["HAADF", "EELS"]}
        assert preferences.stored_channels(values, ["HAADF", "MAADF"]) == ["HAADF"]

    def test_a_selection_naming_nothing_fitted_falls_back(self):
        """Filtering to empty is the same as having stored nothing."""
        values = {"scan_channels": ["EELS"]}
        assert preferences.stored_channels(values, ["HAADF"]) == ["HAADF"]

    def test_a_scanner_with_no_detectors_gets_nothing(self):
        """Nothing to enable is not the same as failing to enable."""
        assert preferences.stored_channels({"scan_channels": ["X"]}, []) == []
