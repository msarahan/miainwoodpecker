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
    ACQUISITION_MODES,
    BEAM_BLANKER_CONTROL,
    DEFOCUS_CONTROL,
    ENERGY_OFFSET_CONTROL,
    IMAGE_READOUT,
    MAP_MODE,
    PROJECTED_READOUT,
    READOUT_MODES,
    SPOT_MODE,
    STAGE_POSITION_CONTROL,
    Camera,
    CameraParameters,
    Frame,
    Instrument,
    InstrumentController,
    ScanParameters,
    SCAN_SYNC_DETECTOR,
    SCAN_SYNC_MODES,
    SCAN_SYNC_NONE,
    SCAN_SYNC_SCANNER,
    DiffractionStack,
    ScanPass,
    Scanner,
    SynchronisedScanner,
    Spectrum,
    SpectrumDetector,
    SpectrumParameters,
)

__all__ = [
    "ACQUISITION_MODES",
    "BEAM_BLANKER_CONTROL",
    "DEFOCUS_CONTROL",
    "ENERGY_OFFSET_CONTROL",
    "IMAGE_READOUT",
    "MAP_MODE",
    "PROJECTED_READOUT",
    "READOUT_MODES",
    "SCAN_SYNC_DETECTOR",
    "SCAN_SYNC_MODES",
    "SCAN_SYNC_NONE",
    "SCAN_SYNC_SCANNER",
    "SPOT_MODE",
    "STAGE_POSITION_CONTROL",
    "Camera",
    "CameraParameters",
    "DiffractionStack",
    "Frame",
    "Instrument",
    "InstrumentController",
    "ScanParameters",
    "ScanPass",
    "Scanner",
    "Spectrum",
    "SpectrumDetector",
    "SpectrumParameters",
    "SynchronisedScanner",
]
