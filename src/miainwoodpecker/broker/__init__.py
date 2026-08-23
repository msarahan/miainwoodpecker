"""
Broker layer: one instrument, many clients, one driver at a time.

The device layer assumes a single driver and the shared-memory transport
depends on it. This package is where that assumption is enforced once, so
that a notebook kernel, a browser dashboard, the Qt viewer and an agent
can all be clients of the same instrument. See
:mod:`miainwoodpecker.broker.interface` for the two verbs - watch, and
lease - and the reasoning behind each.

The protocols and their data types live here;
:mod:`~miainwoodpecker.broker.local` is the in-process implementation and
:mod:`~miainwoodpecker.broker.remote` the client half of the transport
one, served by :mod:`~miainwoodpecker.broker.server` and run over a
device session by :mod:`~miainwoodpecker.broker.app`.
"""

from miainwoodpecker.broker.interface import (
    DEFAULT_LEASE_TIMEOUT_S,
    DEFAULT_LEASE_TTL_S,
    LEASE_ORDER,
    BrokerError,
    DeviceBusyError,
    InstrumentBroker,
    Lease,
    LeasedDevices,
    LeaseExpiredError,
    NotLiveError,
    TargetDescription,
    TargetState,
    TargetView,
    lease_order,
)

__all__ = [
    "DEFAULT_LEASE_TIMEOUT_S",
    "DEFAULT_LEASE_TTL_S",
    "LEASE_ORDER",
    "BrokerError",
    "DeviceBusyError",
    "InstrumentBroker",
    "Lease",
    "LeaseExpiredError",
    "LeasedDevices",
    "NotLiveError",
    "TargetDescription",
    "TargetState",
    "TargetView",
    "lease_order",
]
