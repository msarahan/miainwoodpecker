"""
Broker layer: one instrument, many clients, one driver at a time.

The device layer assumes a single driver and the shared-memory transport
depends on it. This package is where that assumption is enforced once, so
that a notebook kernel, a browser dashboard, the Qt viewer and an agent
can all be clients of the same instrument. See
:mod:`miainwoodpecker.broker.interface` for the two verbs - watch, and
lease - and the reasoning behind each.

Only the protocols and their data types live here so far; the in-process
and remote implementations are not written yet.
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
    "TargetState",
    "TargetView",
    "lease_order",
]
