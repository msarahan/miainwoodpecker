"""
The broker as an application: an icon, a menu, and no terminal.

:mod:`miainwoodpecker.launcher` serves an instrument from a command
line and ends when that command line does.
:mod:`miainwoodpecker.tray.app` is the same session with somewhere to
live - a notification-area icon whose right-click opens a window on the
instrument, shows how the device servers underneath it are doing, and
stops the lot.

Three modules, split by what needs a display:
:mod:`~miainwoodpecker.tray.session` supervises the broker and the
windows and imports no Qt at all,
:mod:`~miainwoodpecker.tray.health` turns what the broker reports into
a per-server verdict and imports no Qt either, and
:mod:`~miainwoodpecker.tray.app` is the icon.
"""

from miainwoodpecker.tray.health import (
    Condition,
    InstrumentHealth,
    ServerHealth,
    TargetHealth,
    assess,
    unreachable,
)
from miainwoodpecker.tray.session import (
    FrontEnd,
    InstrumentSession,
    Opened,
    SessionState,
    SessionStatus,
)

__all__ = [
    "Condition",
    "FrontEnd",
    "InstrumentHealth",
    "InstrumentSession",
    "Opened",
    "ServerHealth",
    "SessionState",
    "SessionStatus",
    "TargetHealth",
    "assess",
    "unreachable",
]
