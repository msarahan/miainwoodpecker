"""
Finding the broker a dashboard is supposed to watch.

A notebook cannot be handed a port and a shared secret on a command
line: it is opened from a browser, possibly by somebody who did not
start the broker. The broker already solves this by *publishing* where
it is (:class:`~miainwoodpecker.broker.invitation.BrokerInvitation`), so
all that is left is agreeing on where to look - and doing that in one
function rather than in a cell the operator has to edit before the
notebook will run at all.

Three places, in order, and the order is the point. An explicit path
wins because somebody typed it. Then the environment, because that is
what a launcher or a site's start-up script sets for every notebook at
once. Then :data:`DEFAULT_INVITATION` in the working directory, which is
what ``miainwoodpecker-broker --publish .`` leaves behind and therefore
what "it just worked" looks like on one machine.

**No fallback to a local broker of its own, deliberately.** A dashboard
that could not find the instrument and quietly opened a second driver on
it would be the exact interleaving :mod:`miainwoodpecker.broker` exists
to prevent - two clients on one shared-memory segment, producing frames
that are half one pass and half the next, with no exception raised
anywhere. Failing with a sentence naming everywhere it looked is the
only safe answer.
"""

from __future__ import annotations

import os
import pathlib
import typing

from miainwoodpecker.broker.invitation import DEFAULT_FILENAME, BrokerInvitation
from miainwoodpecker.broker.remote import connect_broker

if typing.TYPE_CHECKING:
    from collections.abc import Mapping

    from miainwoodpecker.broker.remote import RemoteBroker

INVITATION_ENV_VAR = "MIAINWOODPECKER_BROKER"
"""
Environment variable naming the published invitation.

Spelled without a ``_INVITATION`` suffix because it is the only
broker-related thing a client needs to be told, and a site setting it in
a login profile should not have to remember which of two names is
current.
"""

DEFAULT_INVITATION = DEFAULT_FILENAME
"""The filename a broker publishes into a directory, re-exported for callers."""


def resolve_invitation(
    source: str | os.PathLike[str] | None = None,
    *,
    environ: Mapping[str, str] | None = None,
    directory: str | os.PathLike[str] | None = None,
) -> pathlib.Path:
    """
    Decide which published invitation to read.

    Separated from :func:`connect_dashboard` because it is the part with
    a decision in it, and therefore the part worth testing without a
    running broker.

    Parameters
    ----------
    source : str | os.PathLike[str] | None
        An explicit file, or a directory holding
        :data:`DEFAULT_INVITATION`. None falls through to the
        environment and then the working directory.
    environ : Mapping[str, str] | None
        Environment to read :data:`INVITATION_ENV_VAR` from. None reads
        the process's own.
    directory : str | os.PathLike[str] | None
        Where to look for :data:`DEFAULT_INVITATION` as a last resort.
        None means the current working directory.

    Returns
    -------
    pathlib.Path
        The invitation file to read.

    Raises
    ------
    FileNotFoundError
        If no invitation was found, with every place that was tried
        named in the message. An operator reading "no broker.json in
        /data/today" can act; one reading "connection refused" cannot.
    """
    variables = os.environ if environ is None else environ
    root = pathlib.Path.cwd() if directory is None else pathlib.Path(directory)
    tried: list[pathlib.Path] = []
    for candidate in (source, variables.get(INVITATION_ENV_VAR), root):
        if candidate is None:
            continue
        path = pathlib.Path(candidate)
        if path.is_dir():
            path = path / DEFAULT_INVITATION
        tried.append(path)
        if path.is_file():
            return path
    looked = ", ".join(str(path) for path in tried)
    message = (
        f"no broker invitation found (looked in: {looked}). Start one with "
        f"'miainwoodpecker-broker --publish <directory>', or set "
        f"${INVITATION_ENV_VAR} to the file it wrote."
    )
    raise FileNotFoundError(message)


def connect_dashboard(
    source: str | os.PathLike[str] | None = None,
) -> RemoteBroker:
    """
    Connect to the running broker a published invitation points at.

    Parameters
    ----------
    source : str | os.PathLike[str] | None
        An invitation file, a directory containing one, or None to
        search as :func:`resolve_invitation` describes.

    Returns
    -------
    RemoteBroker
        A connected client, implementing the same
        :class:`~miainwoodpecker.broker.interface.InstrumentBroker`
        protocol the Qt viewer holds in process.
    """
    invitation = BrokerInvitation.read_from(resolve_invitation(source))
    return connect_broker(invitation.address(), invitation.authkey)
