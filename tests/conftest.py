"""
Guards that apply to the whole test suite.

Currently one: keep tests out of the developer's real config file.
"""

import pytest

from miainwoodpecker.viewer import preferences


@pytest.fixture(autouse=True)
def _isolated_preferences(tmp_path, monkeypatch) -> None:
    """
    Point operator preferences at a temporary file for every test.

    Two problems, one fix. The obvious one is **pollution**: preferences
    outlive a session by design, so a widget that saves its detector
    selection in one test hands it to the widget built in the next, and
    a suite that passes alone starts failing in company — which is
    exactly how this fixture came to exist.

    The one that matters more is that without it the suite **writes to
    the machine it is running on**. Running the tests would quietly
    rewrite the developer's own checkbox selection and scan profiles,
    and on a CI image it would scribble in a home directory nobody
    intended to be persistent. A test that reaches outside its
    ``tmp_path`` is a test with a side effect, whatever it asserts.

    Parameters
    ----------
    tmp_path : pathlib.Path
        Pytest's per-test temporary directory.
    monkeypatch : pytest.MonkeyPatch
        Used to redirect the preferences path.
    """
    monkeypatch.setattr(
        preferences,
        "preferences_path",
        lambda: tmp_path / "viewer-preferences.json",
    )
