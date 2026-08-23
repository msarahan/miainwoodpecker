"""
A front end that connects, looks, and exits: the launcher's other child.

Run as ``python -m front_end_stub`` by
:mod:`tests.integration.test_launcher`, in place of the Qt window, so
that the launcher's own behaviour can be tested end to end without a
display and without waiting for napari. It is a *real* client - it
reads the invitation the broker published, connects over the socket,
and asks what is served - because the point of the test is the handshake
between two processes this module is one end of.

It writes what it found to the path in ``$FRONT_END_REPORT`` and exits,
which is what a window closing looks like from the launcher's side.
"""

from __future__ import annotations

import json
import os
import pathlib
import sys

from miainwoodpecker.broker.invitation import BrokerInvitation
from miainwoodpecker.broker.remote import connect_broker


def main() -> int:
    """
    Connect to the broker this was launched against and report on it.

    Returns
    -------
    int
        Zero once the report is written; 1 if the launcher did not say
        where the broker is, which is a failure of the thing under test
        rather than of this stub.
    """
    published = os.environ.get("MIAINWOODPECKER_BROKER")
    if not published:
        return 1
    invitation = BrokerInvitation.read_from(published)
    broker = connect_broker(invitation.address(), invitation.authkey)
    try:
        described = broker.describe()
        report = {
            "published": published,
            "targets": sorted(described),
            "backend": next(
                (
                    description.backend
                    for description in described.values()
                    if description.backend
                ),
                "",
            ),
        }
    finally:
        broker.close()
    pathlib.Path(os.environ["FRONT_END_REPORT"]).write_text(
        json.dumps(report),
        encoding="utf-8",
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
