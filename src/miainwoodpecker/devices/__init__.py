"""
Device layer: vendor-neutral interfaces plus vendor adapters.

Import the protocols and data types from this package; import vendor
adapters from their own modules. ``miainwoodpecker.devices.remote`` is
MIT and has no vendor SDK dependency; it talks to a device server
subprocess. ``miainwoodpecker.devices.nion_server`` is GPL-3.0 (it
imports Nion's device stack directly) and requires the ``device``
optional dependency group — see its module docstring, and
docs/migration-plan.md §6, for why that module must never be imported by
the main application.
"""

from miainwoodpecker.devices.interface import (
    BEAM_BLANKER_CONTROL,
    DEFOCUS_CONTROL,
    STAGE_POSITION_CONTROL,
    Camera,
    CameraParameters,
    Frame,
    InstrumentController,
    ScanParameters,
    Scanner,
)

__all__ = [
    "BEAM_BLANKER_CONTROL",
    "DEFOCUS_CONTROL",
    "STAGE_POSITION_CONTROL",
    "Camera",
    "CameraParameters",
    "Frame",
    "InstrumentController",
    "ScanParameters",
    "Scanner",
]
