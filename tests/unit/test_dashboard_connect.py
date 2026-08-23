"""
Where a dashboard looks for the broker, and what it does when it cannot find one.

The search order is the whole of the logic, and it is worth pinning
because the failure it protects against is silent: a notebook that
could not find the instrument and fell back to opening its own device
session would be a *second driver* on hardware the broker exists to
arbitrate - two clients interleaving on one shared-memory segment,
producing frames that are half one pass and half the next, with nothing
raised anywhere. So there is no fallback, and the refusal names
everywhere it looked.
"""

import pathlib

import pytest

from miainwoodpecker.broker.invitation import BrokerInvitation
from miainwoodpecker.dashboard.connection import (
    INVITATION_ENV_VAR,
    resolve_invitation,
)


@pytest.fixture
def published(tmp_path: pathlib.Path) -> pathlib.Path:
    """
    Write an invitation into a directory, as the broker's --publish does.

    Parameters
    ----------
    tmp_path : pathlib.Path
        Pytest's per-test temporary directory.

    Returns
    -------
    pathlib.Path
        The file that was written.
    """
    invitation = BrokerInvitation(host="localhost", port=51234, authkey=b"secret")
    return invitation.write_to(tmp_path)


def test_an_explicit_file_wins(published, tmp_path):
    """Somebody typed it, so nothing else gets a say."""
    assert resolve_invitation(published, environ={}, directory=tmp_path) == published


def test_a_directory_is_read_as_the_file_the_broker_publishes_into(
    published,
    tmp_path,
):
    """``--publish <dir>`` writes broker.json inside it, and this follows."""
    assert resolve_invitation(tmp_path, environ={}, directory=tmp_path) == published


def test_the_environment_is_next(published, tmp_path):
    """What a site's start-up script sets for every notebook at once."""
    resolved = resolve_invitation(
        None,
        environ={INVITATION_ENV_VAR: str(published)},
        directory=tmp_path.parent,
    )
    assert resolved == published


def test_the_working_directory_is_the_last_resort(published, tmp_path):
    """The one-machine case: publish here, open the notebook here."""
    assert resolve_invitation(None, environ={}, directory=tmp_path) == published


def test_an_absent_invitation_names_everywhere_it_looked(tmp_path):
    """
    A refusal an operator can act on beats a connection timeout.

    Failing here rather than falling back is the point: there is nothing
    safe to fall back *to*.
    """
    with pytest.raises(FileNotFoundError) as raised:
        resolve_invitation(None, environ={}, directory=tmp_path)
    message = str(raised.value)
    assert str(tmp_path / "broker.json") in message
    assert "miainwoodpecker-broker --publish" in message
    assert INVITATION_ENV_VAR in message


def test_a_path_that_does_not_exist_falls_through_rather_than_failing_there(
    published,
    tmp_path,
):
    """
    A stale ``$MIAINWOODPECKER_BROKER`` should not hide a published file here.

    The variable outlives the broker it was set for - a login profile
    keeps it across restarts - so a name that no longer exists is
    treated as one more place that was tried, not as the answer.
    """
    resolved = resolve_invitation(
        None,
        environ={INVITATION_ENV_VAR: str(tmp_path / "gone" / "broker.json")},
        directory=tmp_path,
    )
    assert resolved == published
