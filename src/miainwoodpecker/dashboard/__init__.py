"""
The parts of a browser dashboard that are not the browser.

``notebooks/instrument_dashboard.py`` is a marimo app: a live grid of
tiles over an instrument, plus an Acquire action and an append-only
session log. This package is everything that app does *besides* laying
out widgets - deciding which targets get a tile and in what order,
turning a frame into pixels a page can draw, taking a lease off the
notebook's own thread, and recording what happened.

The split is not tidiness. A marimo cell cannot be exercised without
marimo's runtime, and marimo is an optional dependency this project's
test environments do not install; a decision made inside one is a
decision nothing checks. Everything here is plain Python over the
broker's own data types, so the unit suite covers it whether or not
anybody has marimo installed - and a second front end (a plain script, a
different notebook, an agent) can reuse it rather than reimplementing
the same judgements slightly differently.

Nothing in this package imports marimo, and nothing in it touches a
device handle. It watches through
:meth:`~miainwoodpecker.broker.interface.InstrumentBroker.snapshot` and
drives only inside a lease.

The modules, in the order an acquisition passes through them:

- :mod:`~miainwoodpecker.dashboard.connection` finds the broker.
- :mod:`~miainwoodpecker.dashboard.tiles` decides what is watchable and
  what the chrome says.
- :mod:`~miainwoodpecker.dashboard.images` turns a frame into pixels a
  browser will draw.
- :mod:`~miainwoodpecker.dashboard.acquisition` takes the lease off the
  notebook's thread and names the signals an acquisition produced.
- :mod:`~miainwoodpecker.dashboard.session_log` records the outcome, one
  entry per acquisition and one row per signal.
- :mod:`~miainwoodpecker.dashboard.saving` gets signals that never had a
  file onto a disk somebody chose.
"""

from miainwoodpecker.dashboard.acquisition import (
    AcquisitionJob,
    AcquisitionRequest,
    by_channel,
    camera_request,
    named,
    scan_request,
)
from miainwoodpecker.dashboard.connection import (
    INVITATION_ENV_VAR,
    connect_dashboard,
    resolve_invitation,
)
from miainwoodpecker.dashboard.images import (
    THUMBNAIL_MAX_EDGE,
    TILE_EDGES,
    TILE_MAX_EDGE,
    greyscale_png,
    is_image,
    png_data_uri,
)
from miainwoodpecker.dashboard.saving import (
    NEXUS_MIMETYPE,
    FailedSignal,
    SavedSignal,
    SaveError,
    SaveJob,
    SaveReport,
    download_name,
    nexus_bytes,
    save_pending,
)
from miainwoodpecker.dashboard.session_log import (
    METADATA_HIGHLIGHTS,
    LoggedDataset,
    SessionLog,
    SessionLogEntry,
    describe_dataset,
    describe_frames,
    highlights,
)
from miainwoodpecker.dashboard.tiles import (
    FRAME_SOURCE_KINDS,
    FrameTile,
    channel_labels,
    frame_sources,
    frame_tiles,
    lease_text,
    rate_text,
    tile_status,
)

__all__ = [
    "FRAME_SOURCE_KINDS",
    "INVITATION_ENV_VAR",
    "METADATA_HIGHLIGHTS",
    "NEXUS_MIMETYPE",
    "THUMBNAIL_MAX_EDGE",
    "TILE_EDGES",
    "TILE_MAX_EDGE",
    "AcquisitionJob",
    "AcquisitionRequest",
    "FailedSignal",
    "FrameTile",
    "LoggedDataset",
    "SaveError",
    "SaveJob",
    "SaveReport",
    "SavedSignal",
    "SessionLog",
    "SessionLogEntry",
    "by_channel",
    "camera_request",
    "channel_labels",
    "connect_dashboard",
    "describe_dataset",
    "describe_frames",
    "download_name",
    "frame_sources",
    "frame_tiles",
    "greyscale_png",
    "highlights",
    "is_image",
    "lease_text",
    "named",
    "nexus_bytes",
    "png_data_uri",
    "rate_text",
    "resolve_invitation",
    "save_pending",
    "scan_request",
    "tile_status",
]
