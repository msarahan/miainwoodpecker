"""
Unit tests: turning what a broker reports into "is the spectrometer there?".

The interesting cases are all about *not* flattening things that mean
different opposites - a device that did not fully answer against one
whose loop died, an instrument that is idle against one that could not
be asked - and about the grouping, which is what makes this a health
check on the device servers rather than on a list of names.
"""

from __future__ import annotations

from miainwoodpecker.acquisition.live import LiveStats
from miainwoodpecker.broker.interface import Lease, TargetDescription, TargetState
from miainwoodpecker.instrument_config import (
    DeviceConfig,
    InstrumentConfig,
    ServerConfig,
)
from miainwoodpecker.tray.health import (
    UNCONFIGURED_SERVER,
    Condition,
    assess,
    unreachable,
)

_RATE = 9.5


def _description(name: str, kind: str, **kwargs: object) -> TargetDescription:
    """
    Build one target's description.

    Parameters
    ----------
    name : str
        The target name.
    kind : str
        Its kind.
    **kwargs : object
        Further fields, chiefly ``error``.

    Returns
    -------
    TargetDescription
        The description.
    """
    return TargetDescription(name=name, kind=kind, label=name, **kwargs)


def _state(
    name: str, kind: str, *, is_live: bool = False, **kwargs: object
) -> TargetState:
    """
    Build one target's state.

    Parameters
    ----------
    name : str
        The target name.
    kind : str
        Its kind.
    is_live : bool
        Whether a live loop is running on it.
    **kwargs : object
        Further fields - ``stats``, ``lease``, ``error``.

    Returns
    -------
    TargetState
        The state.
    """
    return TargetState(name=name, kind=kind, is_live=is_live, **kwargs)


def _two_server_instrument() -> InstrumentConfig:
    """
    Build a configuration shaped like a column plus a spectrometer.

    Returns
    -------
    InstrumentConfig
        Two servers, three devices, and one of them owning the column.
    """
    return InstrumentConfig(
        name="Test instrument",
        servers=(
            ServerConfig(
                name="column",
                module="miainwoodpecker.devices.nion_server",
                description="the Nion column",
                controls_column=True,
                devices=(
                    DeviceConfig(target="scanner", served_as="scanner"),
                    DeviceConfig(
                        target="ronchigram_camera",
                        served_as="camera",
                    ),
                ),
            ),
            ServerConfig(
                name="spectrometer",
                module="miainwoodpecker.devices.dectris_server",
                description="the ELA behind the prism",
                devices=(DeviceConfig(target="eels_camera", served_as="camera"),),
            ),
        ),
    )


def test_an_instrument_nobody_can_ask_is_its_own_state():
    """
    Not an empty report: the two want opposite things from an operator.

    "Nothing is served" is a microscope to go and start; "no answer" is
    a broker to go and look at. Flattened into one, the second reads as
    the first and the wrong thing gets restarted.
    """
    report = unreachable("the broker did not answer")

    assert report.condition is Condition.UNREACHABLE
    assert report.servers == ()
    assert "did not answer" in report.summary


def test_devices_are_grouped_under_the_server_that_should_have_brought_them():
    """
    The point of the panel: five targets under three headings.

    A flat list of names cannot answer "did the spectrometer come up?",
    because the answer is a property of a *process* that either started
    or did not, and only the configuration knows which names came from
    which one.
    """
    config = _two_server_instrument()
    described = {
        "instrument": _description("instrument", "instrument"),
        "scanner": _description("scanner", "scanner"),
        "ronchigram_camera": _description("ronchigram_camera", "camera"),
        "eels_camera": _description("eels_camera", "camera"),
    }
    states = {name: _state(name, value.kind) for name, value in described.items()}

    report = assess(described, states, config=config)

    assert report.condition is Condition.HEALTHY
    servers = {server.name: server for server in report.servers}
    assert set(servers) == {"column", "spectrometer"}
    # The column's controls are not a listed device - they come from
    # whichever server owns the column, which the file says separately.
    assert {device.name for device in servers["column"].devices} == {
        "instrument",
        "scanner",
        "ronchigram_camera",
    }
    assert servers["spectrometer"].description == "the ELA behind the prism"
    assert "2 servers" in report.summary


def test_one_adapter_named_on_a_command_line_is_still_one_heading():
    """
    A broker started with --backend has no server names to group by.

    It is the common case for a simulator and for a single camera
    server, and the panel should look the same shape rather than
    degrading into an ungrouped list with different chrome.
    """
    described = {"camera": _description("camera", "camera")}
    states = {"camera": _state("camera", "camera")}

    report = assess(described, states)

    assert [server.name for server in report.servers] == [UNCONFIGURED_SERVER]
    assert report.condition is Condition.HEALTHY
    assert "1 device" in report.summary


def test_a_device_that_would_not_answer_is_degraded_rather_than_fine():
    """
    An adapter half-working is the state everything above it hides.

    A camera that would not say what binning it supports produces a
    window with an empty menu, which reads as a poor interface rather
    than as a broken device - so the health report is the one place it
    can be said out loud.
    """
    described = {
        "camera": _description("camera", "camera", error="binning read timed out"),
    }
    states = {"camera": _state("camera", "camera")}

    report = assess(described, states)

    device = report.servers[0].devices[0]
    assert device.condition is Condition.DEGRADED
    assert "binning read timed out" in device.detail
    assert report.condition is Condition.DEGRADED
    assert "camera" in report.summary


def test_a_live_loop_that_died_outranks_a_description_that_was_incomplete():
    """
    Two errors on one device, and the newer one is the actionable one.

    It is also the more serious: a description read at startup is a
    menu that will be short, and a loop that stopped is a tile that has
    gone blank on somebody mid-session.
    """
    described = {
        "camera": _description("camera", "camera", error="binning read timed out"),
    }
    states = {"camera": _state("camera", "camera", error="camera timed out")}

    report = assess(described, states)

    assert report.servers[0].devices[0].condition is Condition.FAILED
    assert report.condition is Condition.FAILED
    assert "stopped" in report.summary


def test_a_server_is_as_healthy_as_its_unhealthiest_device():
    """
    And the instrument as its unhealthiest server, by the same rule.

    Which is what makes one glance at a tray icon meaningful: green can
    only mean that nothing anywhere has reported itself broken.
    """
    config = _two_server_instrument()
    described = {
        "scanner": _description("scanner", "scanner"),
        "ronchigram_camera": _description("ronchigram_camera", "camera"),
        "eels_camera": _description("eels_camera", "camera"),
    }
    states = {
        "scanner": _state("scanner", "scanner"),
        "ronchigram_camera": _state("ronchigram_camera", "camera"),
        "eels_camera": _state("eels_camera", "camera", error="detector offline"),
    }

    report = assess(described, states, config=config)

    servers = {server.name: server for server in report.servers}
    assert servers["column"].condition is Condition.HEALTHY
    assert servers["spectrometer"].condition is Condition.FAILED
    assert report.condition is Condition.FAILED


def test_what_a_device_is_doing_is_reported_beside_whether_it_is_broken():
    """
    Acquiring, at what rate, and who is holding it.

    The health panel is also the answer to "why can I not take the
    scan?", and that answer is a name - somebody else's lease - rather
    than a fault.
    """
    described = {"scanner": _description("scanner", "scanner")}
    states = {
        "scanner": _state(
            "scanner",
            "scanner",
            is_live=True,
            stats=LiveStats(frame_count=120, fps=_RATE),
            lease=Lease(
                lease_id="abc",
                targets=("scanner",),
                holder="notebook-4102",
                reason="energy series, 5 steps",
                granted_at=0.0,
                expires_at=60.0,
            ),
        ),
    }

    report = assess(described, states)

    device = report.servers[0].devices[0]
    assert device.condition is Condition.HEALTHY
    assert device.is_live
    assert device.fps == _RATE
    assert device.holder == "notebook-4102"
    assert "9.5 fps" in device.detail
    assert "notebook-4102" in device.detail
    assert "1 acquiring" in report.summary


def test_a_target_the_broker_stopped_reporting_is_not_silently_dropped():
    """
    The two reads can disagree, and the disagreement is the finding.

    ``describe`` is cached from startup and ``targets`` is live, so a
    name in one and not the other means something changed underneath
    the broker - which is worth a row saying so rather than a row that
    quietly vanishes.
    """
    described = {"camera": _description("camera", "camera")}

    report = assess(described, {})

    device = report.servers[0].devices[0]
    assert device.condition is Condition.DEGRADED
    assert "not reporting its state" in device.detail


def test_an_instrument_serving_nothing_says_so():
    """
    Rather than reporting an empty list of servers as healthy.

    A broker with no targets is a configuration failure that got past
    startup, and "0 devices, all answering" would be a cheerful way to
    describe it.
    """
    report = assess({}, {})

    assert report.servers == ()
    assert "no devices" in report.summary
