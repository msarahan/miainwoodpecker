"""
Integration tests: one command, two processes, and the order they stop in.

These run the launcher for real - a broker over the camera server's
synthetic instrument, and a front end that connects to it
(``front_end_stub.py``) in place of the window. The camera server is
what makes that possible without hardware or a display: ``--backend
simulated --server-module miainwoodpecker.devices.camera_server`` needs
no vendor SDK and no Qt, so the whole three-process arrangement runs in
CI.

What is actually being tested is the *sequence*, since that is the only
thing this module adds: the broker starts, publishes where it is, the
front end finds it there, and when the front end goes the broker is
**asked** to stop rather than killed - because being asked is what parks
the instrument, and on Windows the difference between the two is the
difference between a handler running and a process disappearing.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
import typing
from pathlib import Path

import pytest

from miainwoodpecker.broker.invitation import DEFAULT_FILENAME, BrokerInvitation
from miainwoodpecker.broker.remote import connect_broker
from miainwoodpecker.devices.remote import own_process_group
from miainwoodpecker.launcher import (
    BROKER_ENV_VAR,
    child_command,
    main,
    resolve_environment,
    spawn,
    stop,
    wait_for_invitation,
)

if typing.TYPE_CHECKING:
    from collections.abc import Callable

_STUB = "front_end_stub"
_CAMERA_SERVER = "miainwoodpecker.devices.camera_server"
_DEADLINE_S = 120.0


def _wait_until(condition: Callable[[], bool]) -> bool:
    """
    Poll a condition until it is true or the deadline elapses.

    Parameters
    ----------
    condition : Callable[[], bool]
        Checked repeatedly; polled rather than waited on, because what
        is being waited for happens in another process.

    Returns
    -------
    bool
        Whether it came true in time.
    """
    deadline = time.monotonic() + _DEADLINE_S
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(0.05)
    return False


def _run(report: Path, *extra: str) -> int:
    """
    Run the launcher against the synthetic instrument and the stub front end.

    Parameters
    ----------
    report : Path
        Where the stub should write what it found.
    *extra : str
        Further launcher arguments, before the front-end command.

    Returns
    -------
    int
        The launcher's exit status.
    """
    os.environ["FRONT_END_REPORT"] = str(report)
    # The stub lives beside this file, and the launcher spawns it as a
    # module: this directory has to be importable in the child, which
    # inherits PYTHONPATH from here.
    here = str(Path(__file__).parent)
    existing = os.environ.get("PYTHONPATH", "")
    os.environ["PYTHONPATH"] = (
        f"{here}{os.pathsep}{existing}" if existing else here
    )
    return main(
        [
            "--backend",
            "simulated",
            "--server-module",
            _CAMERA_SERVER,
            *extra,
            "--",
            sys.executable,
            "-m",
            _STUB,
        ],
    )


def test_one_command_serves_an_instrument_and_opens_a_client_on_it(tmp_path):
    """
    The whole point, end to end: nothing is passed between them by hand.

    The broker chooses its port and generates its authkey at startup, so
    neither can be on a command line - the front end learns both from
    the file the broker publishes, and the launcher's only job is to
    wait for it before starting the front end. A launcher that started
    them together would fail here about half the time, which is exactly
    the hand-run sequence this replaces.
    """
    report = tmp_path / "report.json"
    published = tmp_path / "published"

    status = _run(report, "--publish", str(published))

    assert status == 0
    found = json.loads(report.read_text(encoding="utf-8"))
    assert found["published"] == str(published)
    assert found["backend"] == "simulated"
    # The camera server serves an instrument and at least one detector,
    # and the client saw them through the broker rather than by opening
    # a device session of its own.
    assert "instrument" in found["targets"]
    assert len(found["targets"]) > 1


def test_the_temporary_invitation_does_not_outlive_the_session(tmp_path):
    """
    An authkey is left on disk only when somebody asked for one to be.

    Publishing is how a notebook joins the same instrument, so it stays
    available - but the default is a session nobody else is joining, and
    leaving a live credential in a temporary directory afterwards would
    be a poor default for a machine several people log into.
    """
    report = tmp_path / "report.json"

    assert _run(report) == 0

    published = Path(json.loads(report.read_text(encoding="utf-8"))["published"])
    assert not published.exists()


def test_serving_holds_the_instrument_open_for_clients_to_come_and_go(tmp_path):
    """
    Two clients, one after the other, on an instrument that outlives both.

    The shape a microscope actually gets used in: the window closing is
    not the end of the session, because somebody else is still on the
    column - or will be after lunch. The default mode ends when its own
    front end ends, which is right for one sitting and wrong for a day.

    Ctrl-C is what ends it, so the test sends what Ctrl-C sends.
    """
    published = tmp_path / "published"
    invitation = published / DEFAULT_FILENAME
    # The launcher itself as a subprocess, because what is being tested
    # includes how it answers a signal - which it can only be sent as a
    # process of its own.
    serving = spawn(
        [
            sys.executable,
            "-m",
            "miainwoodpecker.launcher",
            "--serve",
            "--publish",
            str(published),
            "--backend",
            "simulated",
            "--server-module",
            _CAMERA_SERVER,
        ],
    )
    try:
        assert _wait_until(invitation.exists)
        # Two clients in turn, each opening and closing its own
        # connection, with the instrument served throughout - which is
        # the whole difference from the default mode, where the first
        # one closing would have ended it.
        for _ in range(2):
            details = BrokerInvitation.read_from(invitation)
            client = connect_broker(details.address(), details.authkey)
            try:
                assert "instrument" in client.describe()
            finally:
                client.close()
            assert serving.poll() is None
        port = json.loads(invitation.read_text(encoding="utf-8"))["port"]
        status = stop(serving)
    finally:
        stop(serving)

    # Asked to stop, so zero: in this mode that is the ordinary ending
    # rather than a session cut short.
    assert status == 0
    # And the broker went with it. A launcher that died without taking
    # its broker down would leave an instrument served by nobody, which
    # is the failure its signal handlers exist to prevent.
    assert _wait_until(lambda: not _listening(port))


def _listening(port: int) -> bool:
    """
    Report whether anything is accepting connections on a port.

    Parameters
    ----------
    port : int
        The port the broker published.

    Returns
    -------
    bool
        True while something answers there.
    """
    with socket.socket() as probe:
        probe.settimeout(1.0)
        return probe.connect_ex(("localhost", port)) == 0


def test_a_child_that_will_not_answer_is_terminated_rather_than_waited_on():
    """
    The wait is bounded, and the bound is the caller's to set.

    Written after an operator watched "pid 24332 did not stop within
    30s" with an unparked instrument behind it: a napari front end
    signalled during its own startup answers nothing, ever, so the
    thirty seconds the *broker* deserves for parking were being spent on
    a window that was never going to close. The front end is given five
    now - and what this asserts is the mechanism underneath that
    choice, on a child that ignores the ask by construction rather than
    by accident of timing.
    """
    ignoring = (
        "import signal, sys, time;"
        "signal.signal("
        "signal.SIGBREAK if sys.platform == 'win32' else signal.SIGTERM,"
        " signal.SIG_IGN);"
        "time.sleep(300)"
    )
    deaf = spawn([sys.executable, "-c", ignoring])
    started = time.monotonic()
    try:
        status = stop(deaf, 1.0)
    finally:
        stop(deaf, 1.0)
    waited = time.monotonic() - started

    # Terminated, so a status rather than a hang - and promptly, which
    # is the whole point: the caller said one second.
    assert status is not None
    assert deaf.poll() is not None
    assert waited < _DEADLINE_S / 4


def test_serving_without_somewhere_to_publish_is_refused(tmp_path):
    """
    A held-open instrument nobody can find is not a held-open instrument.

    The default publish path is a temporary directory that only this
    process knows, which is fine for a session with its own front end
    and useless for one whose whole point is being joined.
    """
    with pytest.raises(SystemExit, match=r"needs somewhere to publish|--publish"):
        main(["--serve"])
    with pytest.raises(SystemExit, match="runs no front end"):
        main(["--serve", "--publish", str(tmp_path), "--", "some-dashboard"])


def test_the_broker_is_asked_to_stop_and_parks_on_the_way_out(tmp_path):
    """
    The instrument is put down, rather than the process being shot.

    The broker parks in its shutdown path, so a launcher that killed it
    would leave a scan running with nothing driving it - the failure
    ``stop`` exists to prevent, and the one that Windows makes easy to
    write by accident, since ``terminate()`` there runs no handler at
    all.

    Asserted against the broker's own log rather than its exit status,
    because the status cannot tell the two apart on every platform and
    the log says which path was taken. This is the test that would have
    caught the arrangement this module started with: a broker spawned
    through ``pixi run`` exits with ``0xC000013A`` and never logs the
    park, because the signal reaches pixi and the broker is taken down
    under it.
    """
    published = tmp_path / "published"
    published.mkdir()
    log = tmp_path / "broker.log"
    # Spawned here rather than through the launcher's own helper, which
    # lets its children inherit the terminal: what the broker *said* on
    # the way out is the evidence, so it goes to a file.
    with log.open("w", encoding="utf-8") as output:
        broker = subprocess.Popen(  # noqa: S603 - fixed argv, no shell
            [
                sys.executable,
                "-m",
                "miainwoodpecker.broker.app",
                "--backend",
                "simulated",
                "--server-module",
                _CAMERA_SERVER,
                "--publish",
                str(published),
            ],
            stdout=output,
            stderr=output,
            **own_process_group(),
        )
        try:
            wait_for_invitation(broker, published / DEFAULT_FILENAME, _DEADLINE_S)
            status = stop(broker)
        finally:
            stop(broker)

    assert status == 0
    assert "parking the instrument" in log.read_text(encoding="utf-8")


def test_a_broker_that_cannot_start_is_reported_rather_than_waited_out(tmp_path):
    """
    A device server that will not start ends the wait, and says so.

    The alternative is two minutes of nothing followed by a timeout that
    names the launcher rather than the instrument, when the answer -
    "there is no such module" - was available in the first second.
    """
    broker = spawn(
        [
            sys.executable,
            "-m",
            "miainwoodpecker.broker.app",
            "--server-module",
            "miainwoodpecker.devices.no_such_server",
        ],
        env={**os.environ},
    )
    started = time.monotonic()
    try:
        with pytest.raises(SystemExit, match="before it published"):
            wait_for_invitation(broker, tmp_path / DEFAULT_FILENAME, _DEADLINE_S)
    finally:
        stop(broker)

    assert time.monotonic() - started < _DEADLINE_S


def test_another_environments_interpreter_is_used_rather_than_this_one(tmp_path):
    """
    The child runs the *other* environment's python, not this process's.

    ``sys.executable`` is this environment's interpreter, and handing it
    to another environment's packages is the exact failure that
    switching environments exists to avoid - so it is substituted for
    the interpreter inside that environment's prefix. The whole
    arrangement of "the broker where the vendor stack is, the window
    where it is not" rests on this one line.
    """
    prefix = tmp_path / "envs" / "device"
    environment = {"CONDA_PREFIX": str(prefix), "PATH": str(tmp_path)}

    built = child_command([sys.executable, "-m", "a.module", "--flag"], environment)

    interpreter = Path(built[0])
    assert built[0] != sys.executable
    # Somewhere inside that prefix, and a python: where exactly differs
    # by platform - `prefix/python.exe` against `prefix/bin/python` -
    # and asserting the layout here would restate the implementation
    # rather than check it, as well as being wrong on one of the two.
    assert prefix in interpreter.parents
    assert interpreter.stem.lower() == "python"
    assert built[1:] == ["-m", "a.module", "--flag"]
    # And with nothing resolved - a pip or uv install, or no --broker-env
    # - the command is run exactly as given.
    assert child_command(["marimo", "run", "x.py"], {}) == ["marimo", "run", "x.py"]


def test_a_front_end_is_looked_up_in_the_environment_it_will_run_in(tmp_path):
    """
    A named command is resolved against the child's path, not this one's.

    ``CreateProcess`` searches the **parent's** path however carefully
    the child's environment was built, so a dashboard installed only in
    the other environment would not be found - and one installed in
    both would silently be this environment's copy.
    """
    binaries = tmp_path / "bin"
    binaries.mkdir()
    dashboard = binaries / ("marimo.exe" if sys.platform == "win32" else "marimo")
    dashboard.write_text("", encoding="utf-8")
    dashboard.chmod(0o755)
    environment = {"CONDA_PREFIX": str(tmp_path), "PATH": str(binaries)}

    built = child_command(["marimo", "run", "x.py"], environment)

    assert Path(built[0]) == dashboard
    with pytest.raises(SystemExit, match="not in the environment"):
        child_command(["not-installed-here"], environment)


def test_asking_for_an_environment_outside_pixi_says_so(monkeypatch):
    """
    A flag that cannot work fails now, naming the reason.

    Later means a child spawned in the wrong environment and an
    ImportError that names a package instead of a mistake.
    """
    monkeypatch.delenv("PIXI_EXE", raising=False)
    monkeypatch.delenv("PIXI_PROJECT_MANIFEST", raising=False)

    with pytest.raises(SystemExit, match=r"not running under|is not set"):
        resolve_environment("device")


@pytest.mark.skipif(
    "PIXI_EXE" not in os.environ,
    reason="not running under pixi, so there is no environment to resolve",
)
def test_resolving_this_workspace_environment_yields_a_real_interpreter():
    """
    The resolution is asked of pixi for real, rather than being mocked flat.

    ``pixi shell-hook --json`` is the whole mechanism behind
    ``--broker-env``, and a change in its output shape would otherwise
    surface on a microscope PC rather than here.
    """
    environment = resolve_environment("default")
    interpreter = child_command([sys.executable, "-c", "pass"], environment)[0]

    assert Path(environment["CONDA_PREFIX"]).is_dir()
    assert Path(interpreter).is_file()
    assert subprocess.run(  # noqa: S603 - argv built here, no shell
        [interpreter, "-c", "import miainwoodpecker"],
        check=False,
        env={**os.environ, **environment},
    ).returncode == 0


def test_stopping_something_already_gone_is_not_an_error():
    """
    The teardown path runs twice on some routes, and must not raise.

    ``main`` stops the broker in its exception handler and the
    supervisor may have stopped it already; a second attempt that raised
    would replace the original failure with a confusing one.
    """
    finished = subprocess.run(
        [sys.executable, "-c", "pass"],
        check=True,
    )
    del finished
    process = spawn([sys.executable, "-c", "pass"])
    process.wait()

    assert stop(process) == 0
    assert stop(process) == 0


def test_the_front_end_is_told_where_the_broker_is(tmp_path, monkeypatch):
    """
    Through the environment, so any front end can be the front end.

    The Qt window takes ``--broker``; the marimo dashboard reads this
    variable and always has. Setting it means a command given after
    ``--`` needs to know nothing about this launcher.
    """
    seen = {}

    def record(command: list, env: dict | None = None) -> subprocess.Popen:
        """
        Stand in for spawning, capturing what the child would be given.

        Parameters
        ----------
        command : list
            The argv.
        env : dict | None
            The child's environment.

        Returns
        -------
        object
            A process that has already finished.
        """
        seen[command[-1]] = env
        return spawn([sys.executable, "-c", "pass"])

    monkeypatch.setattr("miainwoodpecker.launcher.spawn", record)
    monkeypatch.setattr(
        "miainwoodpecker.launcher.wait_for_invitation",
        lambda *_args, **_kwargs: None,
    )
    main(["--publish", str(tmp_path), "--", "some-dashboard"])

    assert seen["some-dashboard"][BROKER_ENV_VAR] == str(tmp_path)
