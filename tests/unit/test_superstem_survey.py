"""
Pin the safety properties of the survey script handed to a facility.

This script is different from the other diagnostics in ``scripts/``: it
runs on somebody else's instrument computer, possibly mid-session, and
nobody here will be watching when it does. Its value is entirely in what
it does *not* do, and "does not touch the instrument" is a claim made in
writing to a third party. These tests are what make it a fact.
"""

from __future__ import annotations

import importlib.util
import socket
import sys
import typing
import urllib.request
from pathlib import Path

import pytest

from miainwoodpecker.devices.dectris_server import SimulatedControlUnit

if typing.TYPE_CHECKING:
    import types
    from collections.abc import Iterator

_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "superstem_survey.py"

_ELA_500K_WIDTH = 1030
"""The simulated detector's sensor width, as the mock control unit serves it."""

_FAKE_ENERGY_OFFSET_EV = 12.5
_FAKE_STAGE_SIZE_NM = 2000.0
_FAKE_STAGE_POSITION_M = (1e-6, -2e-6)


def _load_survey() -> types.ModuleType:
    """
    Import the script by path, since ``scripts/`` is not a package.

    Returns
    -------
    types.ModuleType
        The loaded module.
    """
    spec = importlib.util.spec_from_file_location("superstem_survey", _SCRIPT)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(name="survey")
def survey_fixture() -> types.ModuleType:
    """
    Provide the survey module.

    Returns
    -------
    types.ModuleType
        The loaded module.
    """
    return _load_survey()


@pytest.fixture(name="control_unit")
def control_unit_fixture() -> Iterator[SimulatedControlUnit]:
    """
    Serve a simulated DECTRIS control unit for the duration of a test.

    Yields
    ------
    SimulatedControlUnit
        The running control unit.
    """
    unit = SimulatedControlUnit()
    try:
        yield unit
    finally:
        unit.shutdown()


def test_the_dectris_probe_only_ever_reads(survey, control_unit, monkeypatch):
    """
    Every request the DECTRIS section makes is a ``GET``.

    The promise made to the facility is that this is safe to run while
    somebody else is using the detector. That rests on one thing: no
    request that could write configuration, arm, trigger, disarm or
    abort. Asserting it at the transport rather than by reading the code
    means a future probe added to the section cannot quietly break it.
    """
    methods: list[str] = []
    original = urllib.request.Request

    def recording(
        url: str,
        *args: typing.Any,  # noqa: ANN401 - passthrough
        **kwargs: typing.Any,  # noqa: ANN401 - passthrough
    ) -> urllib.request.Request:
        request = original(url, *args, **kwargs)
        # Asks the request itself rather than trusting the method keyword:
        # a body passed as 'data' makes it a POST with no keyword in sight.
        methods.append(request.get_method())
        return request

    monkeypatch.setattr(urllib.request, "Request", recording)

    before = control_unit.model.state
    report = survey.probe_dectris(control_unit.address)
    after = control_unit.model.state

    assert report["reachable"] is True
    assert methods, "the section made no requests at all"
    assert set(methods) == {"GET"}
    assert after == before


def test_the_dectris_probe_reports_the_detector_state_it_found(survey, control_unit):
    """
    The state is read and reported, because it says who else is using it.

    An ELA is usually reachable from Gatan's GMS and from LiberTEM-live
    as well. Whoever runs this needs to see, in the report, whether the
    detector was idle or busy when it was asked — otherwise a
    configuration snapshot taken mid-series looks like a resting state.
    """
    report = survey.probe_dectris(control_unit.address)

    assert report["api_version"] == "1.8.0"
    assert report["state"]["ok"] is True
    assert report["state"]["value"] == "idle"
    assert report["config"]["x_pixels_in_detector"]["value"] == _ELA_500K_WIDTH


def test_an_unreachable_control_unit_is_recorded_rather_than_raised(survey):
    """
    A dead address produces a report saying so, not a traceback.

    The person running this cannot debug it and we cannot see their
    screen. A survey that dies on its first unreachable address answers
    nothing, whereas one that records the failure still delivers every
    other section.
    """
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]

    report = survey.probe_dectris(f"127.0.0.1:{port}")

    assert report["reachable"] is False
    assert report["api_version"] is None
    assert "control unit" in report["note"]


def test_the_nion_section_never_loads_a_device_plugin(survey, monkeypatch):
    """
    An empty registry is reported as such, not populated.

    This is the one that would do real harm. This project's device server
    loads Nion plug-ins deliberately, because that is how components get
    registered in a process of its own. Doing the same here would mean a
    second process claiming hardware a running Nion Swift already owns.
    So when the registry has nothing in it, the only correct behaviour is
    to say so and explain where to run instead.
    """
    loaded: list[str] = []

    class _Registry:
        @staticmethod
        def get_component(name: str) -> None:
            loaded.append(name)

    fake = type(sys)("nion.utils")
    fake.Registry = _Registry  # type: ignore[attr-defined]
    package = type(sys)("nion")
    package.utils = fake  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "nion", package)
    monkeypatch.setitem(sys.modules, "nion.utils", fake)

    report = survey.probe_nion()

    assert report["registry_available"] is True
    assert report["stem_controller"] is None
    assert "does not load the plug-ins" in report["note"]
    assert loaded == ["stem_controller"], "it looked past an empty registry"


def test_the_hitachi_section_locates_without_importing(survey, monkeypatch):
    """
    Modules are resolved with ``find_spec``, never imported.

    Importing a vendor control module may open a connection to the
    column, which is exactly the class of side effect a survey must not
    have. ``find_spec`` resolves where a module *would* come from without
    executing it, and this asserts the section uses it that way.
    """
    imported: list[str] = []
    original = importlib.util.find_spec

    def watching(
        name: str,
        *args: typing.Any,  # noqa: ANN401 - passthrough
        **kwargs: typing.Any,  # noqa: ANN401 - passthrough
    ) -> importlib.machinery.ModuleSpec | None:
        imported.append(name)
        return original(name, *args, **kwargs)

    monkeypatch.setattr(survey.importlib.util, "find_spec", watching)
    monkeypatch.setattr(survey, "_HITACHI_SEARCH_ROOTS", ())

    report = survey.probe_hitachi()

    wanted = survey._HITACHI_MODULES  # noqa: SLF001 - the script is the subject
    assert set(imported) >= set(wanted)
    assert all(name not in sys.modules for name in wanted)
    for name in wanted:
        assert report["modules"][name]["ok"] is True


def test_a_failing_probe_is_recorded_as_data(survey):
    """
    One question that cannot be answered does not cost the other answers.

    Every probe goes through this, because a survey that stops at the
    first surprise is useless on a machine nobody can reach remotely.
    """

    def _explode() -> None:
        message = "no such control"
        raise RuntimeError(message)

    result = survey._safe("a question", _explode)  # noqa: SLF001 - the subject

    assert result["ok"] is False
    assert result["label"] == "a question"
    assert result["error"] == "RuntimeError: no such control"


class _RecordingController:
    """
    A stand-in stem controller that remembers every attribute asked for.

    It answers ``TryGetVal`` for two controls and ``GetVal2D`` only with
    the ``axis`` keyword, so the survey's hedging between the two calling
    conventions is exercised rather than assumed.
    """

    def __init__(self) -> None:
        self.asked: list[str] = []
        self.stage_size_nm = _FAKE_STAGE_SIZE_NM
        self.eels_camera = None
        self.ronchigram_camera = None

    def __getattribute__(self, name: str) -> typing.Any:  # noqa: ANN401 - stand-in
        if not name.startswith("_") and name != "asked":
            object.__getattribute__(self, "asked").append(name)
        return object.__getattribute__(self, name)

    @staticmethod
    def TryGetVal(name: str) -> tuple[bool, float | None]:  # noqa: N802 - vendor name
        known = {"EELS_MagneticShift_Offset": _FAKE_ENERGY_OFFSET_EV, "C10": 5e-7}
        return (name in known, known.get(name))

    @staticmethod
    def GetVal2D(name: str, *, axis: tuple[str, str]) -> tuple[float, float]:  # noqa: N802
        assert name == "stage_position_m"
        assert axis == ("x", "y")
        return _FAKE_STAGE_POSITION_M

    def SetVal(self, name: str, value: float) -> bool:  # noqa: N802 - vendor name
        pytest.fail(f"the survey set {name} to {value}")


def _install_fake_nion(monkeypatch, controller) -> None:
    """Put a fake ``nion`` package into ``sys.modules`` around a controller."""

    class _Registry:
        @staticmethod
        def get_component(name: str) -> typing.Any:  # noqa: ANN401 - stand-in
            return controller if name == "stem_controller" else None

        @staticmethod
        def get_components_by_type(name: str) -> set[typing.Any]:
            del name
            return set()

    utils = type(sys)("nion.utils")
    utils.Registry = _Registry  # type: ignore[attr-defined]
    package = type(sys)("nion")
    package.utils = utils  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "nion", package)
    monkeypatch.setitem(sys.modules, "nion.utils", utils)


def test_the_nion_section_reads_both_energy_offset_names_and_sets_nothing(
    survey, monkeypatch
):
    """
    The energy offset is asked for under Nion's name and the simulator's.

    ``ZLPoffset`` is what usim publishes and what this project's server
    drives; ``EELS_MagneticShift_Offset`` is what Nion's own acquisition
    preferences name. A survey that asked for only the first would report
    "no energy offset" on a column that has one under the second, and the
    Gatan branch would be planned on that mistake. And whatever is read,
    nothing may be written: every attribute the controller was asked for
    is recorded, and none of them is a setter.
    """
    controller = _RecordingController()
    _install_fake_nion(monkeypatch, controller)

    report = survey.probe_nion()

    controls = report["controls"]
    assert controls["EELS_MagneticShift_Offset"]["value"]["exists"] is True
    assert (
        controls["EELS_MagneticShift_Offset"]["value"]["value"]
        == _FAKE_ENERGY_OFFSET_EV
    )
    assert controls["ZLPoffset"]["value"]["exists"] is False
    assert controls["C_Blank"]["value"]["exists"] is False
    stage = report["controls_2d"]["stage_position_m"]["value"]
    assert stage["with_axis"]["ok"] is True
    assert stage["with_axis"]["value"] == list(_FAKE_STAGE_POSITION_M)
    assert stage["without_axis"]["ok"] is False
    assert report["stem_controller"]["stage_size_nm"]["value"] == _FAKE_STAGE_SIZE_NM
    setters = [name for name in controller.asked if name.lower().startswith("set")]
    assert setters == [], f"the survey reached for a setter: {setters}"


def test_the_nion_section_lists_plugins_without_importing_them(survey, monkeypatch):
    """
    The installed plug-in modules are listed by file, never loaded.

    The point of the list is to tell the device server which vendor
    module to load by name; loading one here would be the second-driver
    problem the whole section is built to avoid.
    """
    before = set(sys.modules)
    monkeypatch.setattr(survey, "_installed_versions", lambda _prefixes: {})

    report = survey.probe_nion()

    plugins = report["plugins"]
    assert plugins["ok"] is True
    assert "modules" in plugins["value"]
    loaded_since = {
        name
        for name in set(sys.modules) - before
        if name.startswith("nionswift_plugin")
    }
    assert loaded_since == set(), f"the survey imported {loaded_since}"


def test_the_gatan_section_locates_without_importing(survey, monkeypatch):
    """
    ``DigitalMicrograph`` is located, and recognised if already loaded, never imported.

    Outside GMS the module is absent and the section says where to run
    instead. Inside GMS it is already in ``sys.modules`` — that is how the
    section knows it is inside — and its surface is listed with ``dir``,
    which reads nothing. Neither path may import it: outside GMS it would
    fail, and the failure would look like an answer.
    """
    monkeypatch.setattr(survey, "_HITACHI_SEARCH_ROOTS", ())
    monkeypatch.delitem(sys.modules, "DigitalMicrograph", raising=False)
    imported: list[str] = []
    original = importlib.util.find_spec

    def watching(
        name: str,
        *args: typing.Any,  # noqa: ANN401 - passthrough
        **kwargs: typing.Any,  # noqa: ANN401 - passthrough
    ) -> importlib.machinery.ModuleSpec | None:
        imported.append(name)
        return original(name, *args, **kwargs)

    monkeypatch.setattr(survey.importlib.util, "find_spec", watching)

    outside = survey.probe_gatan()

    assert outside["inside_gms"] is False
    assert outside["module"]["locatable"] is False
    assert "DM's own Python window" in outside["note"]
    assert "DigitalMicrograph" in imported
    assert "DigitalMicrograph" not in sys.modules

    fake = type(sys)("DigitalMicrograph")
    fake.GetFrontImage = lambda: pytest.fail("the survey read an image")  # type: ignore[attr-defined]
    fake.ExecuteScriptString = lambda _s: pytest.fail("the survey ran a DM script")  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "DigitalMicrograph", fake)

    inside = survey.probe_gatan()

    assert inside["inside_gms"] is True
    assert "GetFrontImage" in inside["module_surface"]
    assert "ExecuteScriptString" in inside["module_surface"]
    assert "note" not in inside
    assert survey.interpreter_report()["inside_gms"] is True


def test_the_check_mode_probes_nothing(survey, monkeypatch):
    """
    ``--check`` is safe to run first on any machine, mid-experiment.

    It exists so that the interpreter question can be settled before
    anything goes near hardware, which only holds if it genuinely reaches
    nothing. Asserting no section runs is what keeps it that way.
    """
    for name in ("probe_nion", "probe_dectris", "probe_gatan", "probe_hitachi"):
        monkeypatch.setattr(
            survey,
            name,
            lambda *_args, **_kwargs: pytest.fail("--check ran a probe"),
        )

    assert survey.main(["--check"]) == 0


def test_the_declared_python_floor_matches_what_the_file_needs(survey):
    """
    The floor is 3.7 because Gatan Microscopy Suite embeds 3.7.

    Worth pinning rather than leaving as a comment: the file must parse
    on the oldest interpreter it claims, and an interpreter older than
    the floor fails at parse time, before any check inside can report it.
    A change here is a change to what the runbook promises.
    """
    assert survey._MINIMUM_PYTHON == (3, 7)  # noqa: SLF001 - the subject
    source = _SCRIPT.read_text(encoding="utf-8")
    assert ":=" not in source, "the walrus operator needs Python 3.8"
    assert "from __future__ import annotations" in source
