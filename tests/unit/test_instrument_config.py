"""
The per-microscope file: what an instrument has, and who serves it.

Two halves. The first is the schema - every rule the parser enforces,
exercised against dictionaries rather than files, because a rule that
can only be tested by writing TOML to disk tends not to get tested. The
second is the four files in ``instruments/``, parsed as they ship: an
example configuration that stopped being valid is a worked example of
how to get it wrong, and nobody would notice until somebody copied one
at an instrument.
"""

import pathlib

import pytest

from miainwoodpecker.instrument_config import (
    SCHEMA_VERSION,
    InstrumentConfigError,
    load_instrument_config,
    parse_instrument_config,
)

EXAMPLES = pathlib.Path(__file__).resolve().parents[2] / "instruments"


def _config(**overrides: object) -> dict:
    """
    Return a minimal valid configuration, with keys replaced.

    Parameters
    ----------
    **overrides : object
        Top-level keys to add or replace.

    Returns
    -------
    dict
        Ready for :func:`parse_instrument_config`.
    """
    data = {
        "schema": SCHEMA_VERSION,
        "name": "Test instrument",
        "server": [
            {
                "name": "column",
                "module": "miainwoodpecker.devices.nion_server",
                "controls_column": True,
                "device": [{"target": "scanner"}],
            },
        ],
    }
    data.update(overrides)
    return data


def test_a_device_is_served_under_the_name_the_instrument_gives_it():
    """
    ``served_as`` is the whole reason two adapters can coexist.

    The DECTRIS server serves one detector called ``camera`` because
    that is all a detector adapter can honestly know. Which detector it
    is on *this* column is a fact about the column, so the file says it,
    and clients see ``eels_camera``.
    """
    config = parse_instrument_config(
        _config(
            server=[
                {
                    "name": "ela",
                    "module": "miainwoodpecker.devices.dectris_server",
                    "device": [{"target": "eels_camera", "served_as": "camera"}],
                },
            ],
        ),
    )
    (device,) = config.servers[0].devices
    assert device.target == "eels_camera"
    assert device.served_as == "camera"


def test_served_as_defaults_to_the_target_name():
    """
    Most adapters serve the name the instrument uses.

    Saying it twice would be noise in every file, and noise in a file
    like this is what stops people reading it.
    """
    config = parse_instrument_config(_config())
    assert config.servers[0].devices[0].served_as == "scanner"


def test_a_server_that_says_nothing_gets_the_simulated_backend():
    """
    Hardware is never what you get by leaving something out.

    The same rule every other entry point in this project follows, and
    the reason it is worth a test: the default is one word in a
    dataclass, and flipping it would be a silent change to what a file
    with no ``backend`` line drives.
    """
    config = parse_instrument_config(_config())
    assert config.servers[0].backend == "simulated"


def test_the_session_map_leaves_out_what_is_switched_off():
    """
    A disabled entry still records that the hardware exists.

    ``servers`` is the instrument's inventory; ``devices()`` is this
    session's target map. Keeping both is what lets an EDX detector with
    no adapter yet stay written down instead of being deleted.
    """
    config = parse_instrument_config(
        _config(
            server=[
                {
                    "name": "column",
                    "module": "m",
                    "controls_column": True,
                    "device": [
                        {"target": "scanner"},
                        {"target": "eels_camera", "enabled": False},
                    ],
                },
                {"name": "edx", "module": "m2", "enabled": False},
            ],
        ),
    )
    assert [server.name for server in config.servers] == ["column", "edx"]
    assert set(config.devices()) == {"scanner"}
    assert [server.name for server in config.enabled_servers()] == ["column"]


def test_the_controlling_server_is_the_one_whose_instrument_is_the_column():
    """
    Every adapter has an ``instrument`` target; one of them is the microscope.

    A DECTRIS server's ``instrument`` answers ``describe`` and
    ``shutdown`` and knows nothing about a stage, so which one is the
    column cannot be inferred from the shape of what is served.
    """
    config = parse_instrument_config(
        _config(
            server=[
                {"name": "ela", "module": "m2", "device": [{"target": "camera"}]},
                {
                    "name": "column",
                    "module": "m",
                    "controls_column": True,
                    "device": [{"target": "scanner"}],
                },
            ],
        ),
    )
    assert config.controlling_server().name == "column"


def test_a_detector_only_rig_owns_no_column():
    """
    None is an answer, not a failure.

    The device layer already treats a scanner as optional; refusing to
    describe a detector-only rig would be this file inventing a
    requirement the rest of the stack does not have.
    """
    config = parse_instrument_config(
        _config(
            server=[
                {"name": "ela", "module": "m", "device": [{"target": "camera"}]},
            ],
        ),
    )
    assert config.controlling_server() is None


def test_two_servers_cannot_both_own_the_column():
    """
    The broker serves one instrument target.

    A file naming two candidates has not said which stage moves, and
    picking one of them here would be this module guessing.
    """
    with pytest.raises(InstrumentConfigError, match="controls_column"):
        parse_instrument_config(
            _config(
                server=[
                    {"name": "a", "module": "m", "controls_column": True},
                    {"name": "b", "module": "m", "controls_column": True},
                ],
            ),
        )


def test_two_adapters_cannot_claim_the_same_target():
    """
    A duplicate target would not fail, it would silently drop a detector.

    The broker's target map is a dictionary, so the second entry wins
    and the first camera becomes unreachable with nothing said about it.
    That is exactly the class of failure this file exists to prevent.
    """
    with pytest.raises(InstrumentConfigError, match="'camera' appears twice"):
        parse_instrument_config(
            _config(
                server=[
                    {"name": "a", "module": "m", "device": [{"target": "camera"}]},
                    {"name": "b", "module": "m", "device": [{"target": "camera"}]},
                ],
            ),
        )


def test_one_server_cannot_serve_the_same_source_name_twice():
    """
    Renaming one served device into two targets cannot be honoured.

    Two entries with the same ``served_as`` would hand out the same
    handle under two names, and the broker would arbitrate them as if
    they were separate hardware - two leases on one camera.
    """
    with pytest.raises(InstrumentConfigError, match="served_as"):
        parse_instrument_config(
            _config(
                server=[
                    {
                        "name": "a",
                        "module": "m",
                        "device": [
                            {"target": "eels_camera", "served_as": "camera"},
                            {"target": "ronchigram_camera", "served_as": "camera"},
                        ],
                    },
                ],
            ),
        )


def test_server_names_must_be_unique():
    """
    A server name is how a process is identified everywhere else.

    It goes in every log line and every startup error, so two of one
    name identifies neither.
    """
    with pytest.raises(InstrumentConfigError, match="server names"):
        parse_instrument_config(
            _config(
                server=[
                    {"name": "column", "module": "m"},
                    {"name": "column", "module": "m2"},
                ],
            ),
        )


def test_the_instrument_target_is_reserved():
    """
    ``controls_column`` says whose instrument target is the microscope's.

    Letting a device claim the name as well would mean two different
    answers to "what are the column controls" in one file.
    """
    with pytest.raises(InstrumentConfigError, match="reserved"):
        parse_instrument_config(
            _config(
                server=[
                    {"name": "a", "module": "m", "device": [{"target": "instrument"}]},
                ],
            ),
        )


def test_a_misspelt_key_is_refused_rather_than_ignored():
    """
    The failure mode of ignoring one is silence on an instrument.

    ``plugin = "..."`` where the key is ``plugins`` starts a hardware
    server with no arguments and no complaint, and the session is spent
    driving whatever autodiscovery found.
    """
    with pytest.raises(InstrumentConfigError, match="unknown key 'plugin'"):
        parse_instrument_config(
            _config(server=[{"name": "a", "module": "m", "plugin": ["x"]}]),
        )


def test_a_file_from_a_different_version_is_refused():
    """
    A file half-understood is worse than a file rejected.

    Here the half being guessed at is which hardware to open, which is
    not a guess worth making on a microscope.
    """
    with pytest.raises(InstrumentConfigError, match="schema"):
        parse_instrument_config(_config(schema=SCHEMA_VERSION + 1))


def test_a_backend_the_broker_cannot_start_is_refused_at_parse_time():
    """
    Better than a subprocess that spawns and dies with an anonymous status.

    ``replay`` is a real device-server backend and deliberately not one
    of these: the client that spawns servers accepts two names, so a
    file offering a third would fail at launch rather than at the point
    somebody could fix it.
    """
    with pytest.raises(InstrumentConfigError, match="backend 'replay'"):
        parse_instrument_config(
            _config(server=[{"name": "a", "module": "m", "backend": "replay"}]),
        )


def test_a_quoted_boolean_is_refused():
    """
    ``enabled = "false"`` is truthy.

    Accepted as written, it would enable exactly the thing it was meant
    to switch off - which is why this is not a one-line ``bool()``.
    """
    with pytest.raises(InstrumentConfigError, match="true or false"):
        parse_instrument_config(
            _config(server=[{"name": "a", "module": "m", "enabled": "false"}]),
        )


def test_plugins_must_be_a_list_of_strings():
    """
    They become the server's ``--plugin`` values, in order.

    A bare string is the plausible mistake, and it has no sensible
    reading: neither one argument nor several.
    """
    with pytest.raises(InstrumentConfigError, match="list of strings"):
        parse_instrument_config(
            _config(server=[{"name": "a", "module": "m", "plugins": "usim"}]),
        )


def test_a_server_needs_a_module():
    """
    There is nothing to launch without one.

    Said here rather than by a subprocess that cannot start, which is
    the difference between naming the line and naming an exit status.
    """
    with pytest.raises(InstrumentConfigError, match="no module"):
        parse_instrument_config(_config(server=[{"name": "a"}]))


def test_an_instrument_needs_at_least_one_server():
    """A file with no servers describes a microscope nothing can start."""
    with pytest.raises(InstrumentConfigError, match="at least one"):
        parse_instrument_config(_config(server=[]))


def test_a_missing_file_says_which_file(tmp_path):
    """
    Reading a configuration is a command an operator runs at an instrument.

    A traceback about ``FileNotFoundError`` three frames into a loader
    does not name the path that was wrong; the message does.
    """
    missing = tmp_path / "nope.toml"
    with pytest.raises(InstrumentConfigError, match=r"nope\.toml"):
        load_instrument_config(missing)


def test_broken_toml_says_so_rather_than_raising_a_parser_error(tmp_path):
    """
    One exception type for every way a file can be unusable.

    Missing, unparseable, or self-contradictory: the operator opens the
    same file in every case, so they get the same exception.
    """
    broken = tmp_path / "instrument.toml"
    broken.write_text("schema = = 1\n", encoding="utf-8")
    with pytest.raises(InstrumentConfigError, match="not valid TOML"):
        load_instrument_config(broken)


def test_a_directory_is_read_as_the_conventional_filename(tmp_path):
    """
    Naming the directory is shorthand for the file inside it.

    The same shorthand the published broker invitation already uses, for
    the same reason: an operator types a location, not a filename.
    """
    (tmp_path / "instrument.toml").write_text(
        "\n".join(
            [
                f"schema = {SCHEMA_VERSION}",
                'name = "Bench"',
                "[[server]]",
                'name = "column"',
                'module = "m"',
            ],
        ),
        encoding="utf-8",
    )
    config = load_instrument_config(tmp_path)
    assert config.name == "Bench"
    assert config.source == tmp_path / "instrument.toml"


def test_an_error_names_the_file_it_came_from(tmp_path):
    """
    The path is carried through parsing, not just used to open the file.

    A rule broken deep in a file still has to produce a message an
    operator can act on without guessing which of four instrument
    configurations they were editing.
    """
    path = tmp_path / "instrument.toml"
    path.write_text(f"schema = {SCHEMA_VERSION}\n", encoding="utf-8")
    with pytest.raises(InstrumentConfigError, match=r"instrument\.toml"):
        load_instrument_config(path)


@pytest.mark.parametrize(
    "filename",
    ["simulator.toml", "superstem-1.toml", "superstem-2.toml", "superstem-3.toml"],
)
def test_the_shipped_examples_parse(filename):
    """
    An example that stopped being valid teaches how to get it wrong.

    Nobody would notice until somebody copied one at an instrument, so
    the files ship under test rather than under review.
    """
    config = load_instrument_config(EXAMPLES / filename)
    assert config.name
    assert config.servers
    assert config.describe().startswith(config.name)


def test_the_simulator_example_serves_the_scan_and_both_cameras():
    """
    The file the test suite and every laptop run against, pinned.

    Named devices rather than a count: the point of the simulator
    configuration is that it produces the same three targets a Nion
    column does, so the multi-adapter path can be rehearsed against
    something that needs nothing plugged in.
    """
    config = load_instrument_config(EXAMPLES / "simulator.toml")
    assert set(config.devices()) == {"scanner", "ronchigram_camera", "eels_camera"}
    assert config.controlling_server().name == "column"
    assert all(server.backend == "simulated" for server in config.servers), (
        "the simulator file must never name a hardware backend"
    )


def test_the_superstem_3_example_renames_the_ela_onto_the_eels_target():
    """
    The two-adapter instrument this schema was shaped by.

    Worth pinning because it is the only shipped file where a target
    name and a served name differ, which is the feature that lets a Nion
    column and a DECTRIS detector be one instrument.
    """
    config = load_instrument_config(EXAMPLES / "superstem-3.toml")
    server, device = config.devices()["eels_camera"]
    assert server.module == "miainwoodpecker.devices.dectris_server"
    assert device.served_as == "camera"
    assert config.controlling_server().name == "column"
