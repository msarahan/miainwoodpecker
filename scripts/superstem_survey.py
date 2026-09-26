"""
Read-only survey of a SuperSTEM instrument, to answer questions we cannot guess.

Run this on an instrument control computer and send back the JSON file it
writes. Every question it asks decides work that is otherwise estimated
blind — the Gatan branch, the Hitachi adapter, and the DECTRIS trigger
arithmetic between them are worth several weeks of guessing.

**This script does not touch the instrument.** That is a design property,
not an intention, and it is worth stating exactly what it means:

- **Nion**: it *reads* a component registry that something else populated
  and reads named controls. It never sets a control, never blanks or
  unblanks, never moves the stage, never starts a scan, and — the one
  that matters most — it **never loads a device plug-in**. Loading
  plug-ins is how this project's own device server registers components,
  and doing it here would mean a second process claiming hardware a
  running Nion Swift already owns. If the registry is empty this script
  says so and asks you to run it from Swift's own Python console, rather
  than populating the registry itself.
- **DECTRIS**: HTTP ``GET`` only. It never ``PUT``s a configuration,
  never arms, triggers, disarms or aborts. It is safe to run while
  somebody else is using the detector, and it will tell you if somebody
  is.
- **Hitachi**: it resolves module *specifications* with
  ``importlib.util.find_spec``, which locates a module without executing
  it, and lists directories. It never imports a vendor control module,
  because importing one may open a connection to the column.
- **Gatan**: the same ``find_spec`` for ``DigitalMicrograph``, plus a
  look at whether that module is *already loaded* — which is how it
  knows it is running inside Gatan Microscopy Suite's own Python window.
  It executes no DM script and reads no image.

What the Nion section now records, beyond whether the spectrometer is
there: the installed ``nionswift_plugin`` modules (so the device server
can be told which one to load), every registered camera's own account
of itself (sensor shape, binning factors, dark and gain support, and the
calibration controls its energy axis comes from), the scan unit's
channels and current parameters, the saved acquisition profiles of each
hardware source (how the operators actually acquire here), and whether
the scan and cameras offer Nion's synchronised-acquisition methods —
which is whether this column can take a spectrum image through Nion's
stack at all. Two names are read for the energy offset, because the
simulator's (``ZLPoffset``) and the instrumentation kit's own
(``EELS_MagneticShift_Offset``) differ, and which one answers decides a
control name in this project's server.

Python environment
------------------
**Nothing needs installing.** No pip, no virtual environment, no network
access to a package index — the script imports only the standard library,
plus the instrument's own ``nion`` packages when running the Nion section.

**Python 3.7 or newer**, which is the version Gatan Microscopy Suite
embeds and therefore the oldest interpreter plausibly found near this
hardware. On anything older the file will not even parse, and the failure
will be a ``SyntaxError`` rather than a useful message, so check first::

    python -c "import sys; print(sys.version)"

Run ``--check`` before anything else. It touches no hardware and no
network at all; it reports which interpreter it is in and which sections
that interpreter could answer.

**Which interpreter matters more than which machine.** The Nion and
Hitachi sections can only see what is importable *from the interpreter
they run in*:

- The Nion section wants the interpreter Nion Swift itself runs, ideally
  Swift's own Python console. A system Python on the same computer will
  usually report an empty registry, which is a fact about that
  interpreter and not about the instrument.
- The Hitachi section wants whichever interpreter the vendor software
  installed its modules into. Run from the wrong one, a missing
  ``MfExtCont`` means nothing. If there are several Pythons on that
  machine, run the section once per interpreter with a different
  ``--out``; that costs a minute and removes a false negative that would
  otherwise cost weeks.
- The DECTRIS section has no such constraint. Any Python 3.7+ on any
  machine that can reach the control unit will do, including a laptop.

Usage
-----
On a Nion column (**preferably from Nion Swift's Python console**, where
the registry is already populated by the running application)::

    python superstem_survey.py --nion --out superstem2.json

On any machine that can reach a DECTRIS control unit::

    python superstem_survey.py --dectris 192.168.1.10 --out hermes.json

On the Hitachi SU9000II control computer::

    python superstem_survey.py --hitachi --out superstem4.json

On any machine that may have Gatan Microscopy Suite — and, only where
GMS is 3.4 or newer, **from DM's own Python window**. SuperSTEM 1 and 2
run DigitalMicrograph 1.x and 2.x, which embed no Python, so there the
section runs from whatever Python the machine has and records where DM
is installed::

    python superstem_survey.py --gatan --out gatan.json

The Nion run is wanted on **every Nion column** — SuperSTEM 2 and
SuperSTEM 3 both, and SuperSTEM 1 if it turns out to be one — because
each answers a different question: whether the Enfina is Nion's EELS
camera on SuperSTEM 2, and whether Nion *also* registers the ELA on
SuperSTEM 3, which would put two drivers on one detector.

Several sections can be combined in one run, and ``--all`` runs every
section that makes sense on the machine it finds itself on.
"""

from __future__ import annotations

import argparse
import contextlib
import importlib.util
import json
import os
import pkgutil
import platform
import socket
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

try:  # Python 3.8+; the backport is what Python 3.7 has, if anything.
    from importlib import metadata as _metadata
except ImportError:  # pragma: no cover - only on 3.7
    try:
        import importlib_metadata as _metadata  # type: ignore[no-redef]
    except ImportError:  # pragma: no cover - neither available
        _metadata = None  # type: ignore[assignment]

_MINIMUM_PYTHON = (3, 7)
"""
The oldest interpreter this file parses under.

Chosen because Gatan Microscopy Suite embeds Python 3.7, which makes it
the oldest interpreter plausibly sitting next to this hardware. Anything
older fails at parse time, before any check here can run, which is why
the runbook asks for ``python -c "import sys; print(sys.version)"`` first.
"""

_SIMPLON_VERSIONS = ("1.8.0", "1.6.0")
"""API versions to try, newest first. The version is part of the URL path."""

_SIMPLON_TIMEOUT_S = 5.0

_DETECTOR_CONFIG_KEYS = (
    "description",
    "detector_number",
    "software_version",
    "sensor_material",
    "sensor_thickness",
    "x_pixels_in_detector",
    "y_pixels_in_detector",
    "x_pixel_size",
    "y_pixel_size",
    "bit_depth_image",
    "bit_depth_readout",
    "count_time",
    "frame_time",
    "nimages",
    "ntrigger",
    "trigger_mode",
    "incident_energy",
    "threshold_energy",
    "roi_mode",
    "compression",
    "countrate_correction_applied",
)
"""
Configuration this project's adapter reads or would like to.

Asked one at a time rather than as a block, because *which of these an
ELA actually publishes* is itself one of the unknowns — a 404 on any one
of them is data, not a failure.
"""

_NION_CONTROLS = (
    "EHT",
    "BeamCurrent",
    "C10",
    "C12",
    "C21",
    "C30",
    "C_Blank",
    "ZLPoffset",
    "EELS_MagneticShift_Offset",
    "eels_x_scale",
    "eels_x_offset",
    "eels_y_scale",
    "eels_y_offset",
    "ronchigram_x_scale",
    "ronchigram_x_offset",
    "ronchigram_y_scale",
    "ronchigram_y_offset",
    "ConvergenceAngle",
    "CAperture",
    "StageOutX",
    "StageOutY",
    "probe_ha",
)
"""
Named controls to ask for, by read only.

The energy offset is asked for under **two** names, and which one answers
matters more than any other control here. ``ZLPoffset`` is what the
``nionswift-usim`` simulator publishes, and it is the name this project's
own device server drives today. ``EELS_MagneticShift_Offset`` is the name
Nion's instrumentation kit itself uses for the energy offset in its
acquisition preferences (``AcquisitionPreferences.acquisition_controls``),
which is the better guess for a real column. If only the second exists,
the server's control name is wrong and must change before the energy
offset can be driven; if neither exists, the spectrometer is not driven
through Nion at all. Either answer is one control read.

``C_Blank`` is Nion's documented blanker control. The ``eels_*`` and
``ronchigram_*`` names are the *calibration* controls a Nion camera
resolves its energy and angular axes against; their presence says
whether recorded spectra will carry an eV axis, and ``eels_x_scale`` is
the dispersion in eV per channel.
"""

_NION_2D_CONTROLS = ("stage_position_m",)
"""
Two-dimensional controls, read with ``GetVal2D`` rather than ``TryGetVal``.

Tried both with and without an ``axis`` keyword, because Nion's own
reference implementation and its ``STEMController`` protocol disagree
about whether the keyword is required, and this project's server hedges
between them. Which one a real controller accepts is recorded rather
than assumed.
"""

_NION_PROFILE_COUNT = 3
"""
Nion's scan and camera hardware sources each keep three profiles
(view, record and a third). Reading all three records how the operators
actually acquire on this machine — dwell, size, exposure, binning,
processing — which is what a replacement's defaults should be built from.
"""

_NION_CAMERA_FACTS = (
    "camera_id",
    "camera_name",
    "camera_type",
    "camera_version",
    "sensor_dimensions",
    "readout_area",
    "binning_values",
    "exposure_precision",
    "flip",
    "is_dark_subtraction_available",
    "is_dark_subtraction_enabled",
    "is_gain_normalization_available",
    "is_gain_normalization_enabled",
    "calibration_controls",
    "configuration_properties",
)
"""
Attributes read from each registered camera device, all properties.

``calibration_controls`` is the mapping from axis to instrument control
name — it says where the energy axis comes from. The dark and gain
entries say whether the *device* offers reference correction, which
decides whether a replacement UI needs to build it or merely switch it
on. ``camera_type`` is what this project's server sorts cameras by, and
``"ronchigram"``/``"eels"`` are assumed values, never checked.
"""

_NION_CAMERA_CAPABILITIES = (
    "acquire_synchronized_prepare",
    "acquire_synchronized_begin",
    "acquire_sequence_prepare",
    "acquire_sequence_begin",
    "acquire_single_begin",
    "set_dark_image",
    "set_gain_image",
    "get_expected_dimensions",
)
"""
Camera device methods whose *existence* is the question, never called.

``acquire_synchronized_*`` is how Nion's stack reads a camera in step
with a scan — a spectrum image. A device without it cannot take one
through Nion, whatever a UI offers.
"""

_NION_SCAN_CAPABILITIES = (
    "prepare_synchronized_scan",
    "calculate_flyback_pixels",
    "calculate_max_field_of_view",
    "read_partial",
    "get_buffer_data",
    "set_sequence_buffer_size",
)
"""
Scan device methods whose existence is the question, never called.

``prepare_synchronized_scan`` is the scan half of a Nion spectrum image.
"""

_NION_SCAN_SOURCE_CAPABILITIES = (
    "grab_synchronized",
    "grab_synchronized_get_info",
    "record_immediate",
    "prepare_sequence_mode",
)
"""
Scan *hardware source* methods whose existence is the question.

``grab_synchronized`` is Nion's spectrum-image entry point. Whether it
is there, and whether the camera beside it has
``acquire_synchronized_*``, together say whether this column can take a
spectrum image through Nion's own stack today.
"""

_GATAN_SEARCH_FRAGMENTS = ("gatan", "digitalmicrograph", "gms")
_GATAN_ENVIRONMENT_FRAGMENTS = ("GMS", "GATAN", "DIGITALMICROGRAPH")

_HITACHI_MODULES = ("MfExtCont", "MfKeyMouse", "MfCommon")
"""
Undocumented external-control modules evidenced on a Hitachi SU7000.

Whether they are also on an SU9000II is the single question that decides
between a 12-18 day adapter and a vendor negotiation. Resolved with
``find_spec``, which locates without importing: importing a vendor
control module may open a connection to the column.
"""

_HITACHI_SEARCH_ROOTS = (
    r"C:\Program Files",
    r"C:\Program Files (x86)",
    r"C:\Hitachi",
    r"C:\HHT",
    r"D:\Hitachi",
    "/opt",
)

_HITACHI_SEARCH_DEPTH = 3


def _safe(label: str, probe: Any) -> dict[str, Any]:  # noqa: ANN401 - any callable
    """
    Run one probe and record its answer or its failure, never raising.

    A survey that stops at the first surprise is useless on a machine
    nobody can debug remotely, so every question is answered with either
    a value or the reason there is none.

    Parameters
    ----------
    label : str
        What is being asked, for the report.
    probe : Any
        A zero-argument callable performing the read.

    Returns
    -------
    dict[str, Any]
        ``{"ok": True, "value": ...}`` or ``{"ok": False, "error": ...}``.
    """
    try:
        return {"label": label, "ok": True, "value": probe()}
    except Exception as error:  # noqa: BLE001 - a failed probe is data, not a crash
        return {
            "label": label,
            "ok": False,
            "error": f"{type(error).__name__}: {error}",
        }


def machine_facts() -> dict[str, Any]:
    """
    Describe the machine, so a later answer can be attributed to it.

    Returns
    -------
    dict[str, Any]
        Host, platform and interpreter identity.
    """
    return {
        "hostname": socket.gethostname(),
        "platform": platform.platform(),
        "machine": platform.machine(),
        "python_version": sys.version,
        "python_version_info": list(sys.version_info[:3]),
        "python_executable": sys.executable,
        "prefix": sys.prefix,
    }


def interpreter_report() -> dict[str, Any]:
    """
    Say what this interpreter is and which sections it could answer.

    Touches no hardware and opens no socket, so it is safe to run first on
    any machine, including one mid-experiment.

    Returns
    -------
    dict[str, Any]
        Machine facts, plus a per-section verdict and any warnings.

    Notes
    -----
    The verdicts are about *this interpreter*, not about the instrument. A
    Nion column whose Swift is running will still report ``nion: no`` from
    a system Python that cannot import ``nion.utils`` — which is the point
    of checking before running, rather than mistaking it for an answer.
    """
    warnings: list[str] = []
    if sys.version_info < _MINIMUM_PYTHON:  # pragma: no cover - would not parse
        warnings.append(
            f"Python {'.'.join(map(str, _MINIMUM_PYTHON))} or newer is needed."
        )
    if _metadata is None:
        warnings.append(
            "importlib.metadata is unavailable, so installed package versions "
            "cannot be listed. Everything else still works."
        )

    can_nion = importlib.util.find_spec("nion") is not None
    if not can_nion:
        warnings.append(
            "'nion' is not importable here. For the Nion section, use the "
            "interpreter Nion Swift runs — ideally Swift's own Python console."
        )

    inside_gms = "DigitalMicrograph" in sys.modules
    if inside_gms:
        warnings.append(
            "This is Gatan Microscopy Suite's own Python. That is the right "
            "place for --gatan, and this interpreter's version is the answer "
            "to the GMS-embedded-Python question on the hardware checklist."
        )

    return {
        "machine": machine_facts(),
        "sections_this_interpreter_can_answer": {
            "nion": can_nion,
            "dectris": True,
            "gatan": True,
            "hitachi": sys.platform.startswith("win") or sys.platform == "linux",
        },
        "inside_gms": inside_gms,
        "warnings": warnings,
    }


def _installed_versions(prefixes: tuple[str, ...]) -> dict[str, str]:
    """
    Return installed distributions whose name starts with any prefix.

    Parameters
    ----------
    prefixes : tuple[str, ...]
        Lowercase name prefixes to match.

    Returns
    -------
    dict[str, str]
        Distribution name to version, empty if this interpreter has no
        ``importlib.metadata`` (Python 3.7 without the backport).
    """
    if _metadata is None:
        return {}
    found: dict[str, str] = {}
    for dist in _metadata.distributions():
        name = (dist.metadata["Name"] or "").strip()
        if name and name.lower().startswith(prefixes):
            found[name] = dist.version or "unknown"
    return found


def _public_names(target: Any) -> list[str]:  # noqa: ANN401 - any object
    """
    List an object's public attribute names, reading none of them.

    Parameters
    ----------
    target : Any
        The object to describe.

    Returns
    -------
    list[str]
        Names not starting with an underscore. ``dir`` on its own reads
        no property, so this is a description of the surface and not a
        sweep of its values.
    """
    return [name for name in dir(target) if not name.startswith("_")]


def _plain(value: Any) -> Any:  # noqa: ANN401 - vendor object in, JSON-shaped out
    """
    Turn a vendor value into something ``json.dumps`` can carry.

    Parameters
    ----------
    value : Any
        A frame-parameters object, a mapping, a tuple, or a scalar.

    Returns
    -------
    Any
        ``as_dict()`` when the object offers it, a ``dict`` for a mapping,
        a list for a sequence, and ``str`` for anything else exotic.
    """
    as_dict = getattr(value, "as_dict", None)
    if callable(as_dict):
        with contextlib.suppress(Exception):
            return _plain(as_dict())
    if isinstance(value, dict):
        return {str(key): _plain(item) for key, item in value.items()}
    if hasattr(value, "items") and callable(value.items):
        with contextlib.suppress(Exception):
            return {str(key): _plain(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(item) for item in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


def _capabilities(target: Any, names: tuple[str, ...]) -> dict[str, bool]:  # noqa: ANN401
    """
    Say which named methods an object has, calling none of them.

    Parameters
    ----------
    target : Any
        The object to inspect.
    names : tuple[str, ...]
        Method names whose presence is the question.

    Returns
    -------
    dict[str, bool]
        Name to whether a callable of that name exists.
    """
    return {name: callable(getattr(target, name, None)) for name in names}


def _nion_plugins() -> dict[str, Any]:
    """
    List the installed ``nionswift_plugin`` modules without importing any.

    Returns
    -------
    dict[str, Any]
        Where the namespace lives and the module names found in it.

    Notes
    -----
    This is the first item on the hardware checklist, and it settles two
    assumptions at once: which vendor plug-in this project's device server
    must load by name, and whether that name is one autodiscovery would
    have skipped. ``find_spec`` locates the namespace package and
    ``pkgutil.iter_modules`` lists files in it; neither runs any of the
    plug-ins, which is the property that makes this safe beside a running
    Swift.
    """
    spec = importlib.util.find_spec("nionswift_plugin")
    if spec is None:
        return {"found": False, "modules": []}
    locations = list(spec.submodule_search_locations or [])
    modules = sorted(
        {info.name for info in pkgutil.iter_modules(locations)},
    )
    return {"found": True, "locations": locations, "modules": modules}


def _nion_camera(controller: Any, attribute: str) -> Any:  # noqa: ANN401 - vendor object
    """
    Describe one camera a stem controller exposes, if it exposes it.

    Parameters
    ----------
    controller : Any
        The registered ``stem_controller`` component.
    attribute : str
        Attribute name: ``ronchigram_camera``, ``eels_camera`` or
        ``slit_camera``.

    Returns
    -------
    Any
        Identity of the camera, or None when the attribute is absent.
    """
    camera = getattr(controller, attribute, None)
    if camera is None:
        return None
    return {
        "present": True,
        "class": type(camera).__name__,
        "hardware_source_id": getattr(camera, "hardware_source_id", None),
        "display_name": getattr(camera, "display_name", None),
        "camera_id": getattr(camera, "camera_id", None),
        "camera_type": getattr(camera, "camera_type", None),
        "camera_name": getattr(camera, "camera_name", None),
    }


def _nion_camera_device(device: Any) -> dict[str, Any]:  # noqa: ANN401 - vendor object
    """
    Read a camera device's descriptive properties and list its methods.

    Parameters
    ----------
    device : Any
        A ``camera_module.camera_device``.

    Returns
    -------
    dict[str, Any]
        Each fact in ``_NION_CAMERA_FACTS`` (as data or as the error
        reading it raised), which of ``_NION_CAMERA_CAPABILITIES`` exist,
        the expected frame shape at binning 1, and the device's public
        surface.

    Notes
    -----
    Every entry is a property read or a ``hasattr``. ``get_expected_dimensions``
    is the one method called, because it is pure arithmetic on the
    readout area and answers the frame-shape question directly.
    """
    facts = {
        name: _safe(name, lambda attribute=name: _plain(getattr(device, attribute)))
        for name in _NION_CAMERA_FACTS
    }
    expected = getattr(device, "get_expected_dimensions", None)
    return {
        "class": f"{type(device).__module__}.{type(device).__name__}",
        "facts": facts,
        "capabilities": _capabilities(device, _NION_CAMERA_CAPABILITIES),
        "expected_dimensions_at_binning_1": _safe(
            "get_expected_dimensions(1)",
            lambda: _plain(expected(1)) if callable(expected) else None,
        ),
        "surface": _public_names(device),
    }


def _nion_camera_modules(registry: Any) -> list[dict[str, Any]]:  # noqa: ANN401
    """
    Describe every registered ``camera_module``, not only the two named ones.

    Parameters
    ----------
    registry : Any
        ``nion.utils.Registry``.

    Returns
    -------
    list[dict[str, Any]]
        One entry per camera module, ordered by camera id.

    Notes
    -----
    The stem controller names at most a Ronchigram, an EELS and a slit
    camera. A column can register more, and a camera whose
    ``camera_type`` is neither ``"ronchigram"`` nor ``"eels"`` is one
    this project's server would sort wrongly, so all of them are listed.
    """
    get_all = getattr(registry, "get_components_by_type", None)
    modules = list(get_all("camera_module")) if callable(get_all) else []
    described = []
    for module in modules:
        device = getattr(module, "camera_device", None)
        described.append(
            {
                "module_class": type(module).__name__,
                "camera_settings_class": type(
                    getattr(module, "camera_settings", None),
                ).__name__,
                "camera_panel_type": getattr(module, "camera_panel_type", None),
                "device": _nion_camera_device(device) if device is not None else None,
            },
        )
    described.sort(key=lambda entry: str((entry.get("device") or {}).get("facts")))
    return described


def _nion_scan_channels(registry: Any) -> Any:  # noqa: ANN401 - vendor object
    """
    Report how many signals one scan pass reads out, and their names.

    Parameters
    ----------
    registry : Any
        ``nion.utils.Registry``.

    Returns
    -------
    Any
        Channel count, per-channel identity, the device's current frame
        parameters and its synchronisation-related methods, or None
        without a scan module.
    """
    scan = registry.get_component("scan_module")
    device = getattr(scan, "device", None) if scan is not None else None
    if device is None:
        return None
    count = getattr(device, "channel_count", None)
    channels = []
    for index in range(int(count or 0)):
        entry: dict[str, Any] = {"index": index}
        info = getattr(device, "get_channel_info", None)
        if callable(info):
            with contextlib.suppress(Exception):
                channel = info(index)
                entry["name"] = getattr(channel, "name", None)
                entry["id"] = getattr(channel, "channel_id", None)
                entry["enabled"] = getattr(channel, "enabled", None)
        get_name = getattr(device, "get_channel_name", None)
        if "name" not in entry and callable(get_name):
            with contextlib.suppress(Exception):
                entry["name"] = get_name(index)
        channels.append(entry)
    return {
        "device_class": f"{type(device).__module__}.{type(device).__name__}",
        "scan_device_id": getattr(device, "scan_device_id", None),
        "scan_device_name": getattr(device, "scan_device_name", None),
        "channel_count": count,
        "channels": channels,
        "channels_enabled": _safe(
            "channels_enabled",
            lambda: _plain(getattr(device, "channels_enabled", None)),
        ),
        "current_frame_parameters": _safe(
            "current_frame_parameters",
            lambda: _plain(getattr(device, "current_frame_parameters", None)),
        ),
        "flyback_pixels_attribute": getattr(device, "flyback_pixels", None),
        "capabilities": _capabilities(device, _NION_SCAN_CAPABILITIES),
        "surface": _public_names(device),
    }


def _nion_profiles(source: Any) -> dict[str, Any]:  # noqa: ANN401 - vendor object
    """
    Read a hardware source's saved profiles and its current parameters.

    Parameters
    ----------
    source : Any
        A scan or camera hardware source.

    Returns
    -------
    dict[str, Any]
        The selected profile index, each profile's parameters, and the
        parameters in force now. Reads only: nothing is selected or set.
    """
    get_profile = getattr(source, "get_frame_parameters", None)
    profiles = {
        str(index): _safe(
            f"profile {index}",
            lambda i=index: _plain(get_profile(i)) if callable(get_profile) else None,
        )
        for index in range(_NION_PROFILE_COUNT)
    }
    current = getattr(source, "get_current_frame_parameters", None)
    return {
        "selected_profile_index": getattr(source, "selected_profile_index", None),
        "profiles": profiles,
        "current": _safe(
            "current frame parameters",
            lambda: _plain(current()) if callable(current) else None,
        ),
    }


def _nion_hardware_sources() -> dict[str, Any]:
    """
    List Swift's hardware sources: the scan and cameras as the application sees them.

    Returns
    -------
    dict[str, Any]
        One entry per hardware source with its identity, features,
        profiles and — for the scan — whether it offers
        ``grab_synchronized``, Nion's spectrum-image entry point.

    Notes
    -----
    Only meaningful from Swift's own console, where the application has
    built these. Everything read is a property, a saved profile or a
    ``hasattr``; no source is started, and no profile is selected.
    """
    from nion.instrumentation import HardwareSource  # noqa: PLC0415 - optional, probed

    manager = HardwareSource.HardwareSourceManager()
    entries = []
    for source in list(manager.hardware_sources):
        entry: dict[str, Any] = {
            "hardware_source_id": getattr(source, "hardware_source_id", None),
            "display_name": getattr(source, "display_name", None),
            "class": f"{type(source).__module__}.{type(source).__name__}",
            "features": _safe("features", lambda s=source: _plain(s.features)),
            "profiles": _safe("profiles", lambda s=source: _nion_profiles(s)),
            "modes": _safe("modes", lambda s=source: _plain(getattr(s, "modes", None))),
            "surface": _public_names(source),
        }
        scan_device = getattr(source, "scan_device", None)
        if scan_device is not None:
            entry["scan"] = {
                "source_capabilities": _capabilities(
                    source,
                    _NION_SCAN_SOURCE_CAPABILITIES,
                ),
                "channel_count": getattr(source, "channel_count", None),
                "subscan_enabled": getattr(source, "subscan_enabled", None),
                "probe_state": getattr(source, "probe_state", None),
            }
        camera = getattr(source, "camera", None)
        if camera is not None:
            entry["camera"] = _safe(
                "camera device", lambda c=camera: _nion_camera_device(c)
            )
        entries.append(entry)
    return {"count": len(entries), "sources": entries}


def _nion_2d_control(controller: Any, name: str) -> dict[str, Any]:  # noqa: ANN401
    """
    Read one 2D control, trying both ``GetVal2D`` calling conventions.

    Parameters
    ----------
    controller : Any
        The registered ``stem_controller`` component.
    name : str
        Control name, e.g. ``stage_position_m``.

    Returns
    -------
    dict[str, Any]
        The value under each convention, or the error each raised.
    """
    getter = getattr(controller, "GetVal2D", None)
    if not callable(getter):
        return {"queried": False, "reason": "no GetVal2D on this controller"}
    return {
        "queried": True,
        "without_axis": _safe("GetVal2D(name)", lambda: _plain(getter(name))),
        "with_axis": _safe(
            "GetVal2D(name, axis=('x', 'y'))",
            lambda: _plain(getter(name, axis=("x", "y"))),
        ),
    }


def _nion_control(controller: Any, name: str) -> Any:  # noqa: ANN401 - vendor return
    """
    Read one named control, by read only.

    Parameters
    ----------
    controller : Any
        The registered ``stem_controller`` component.
    name : str
        Control name, e.g. ``ZLPoffset``.

    Returns
    -------
    Any
        Whether the control exists and its current value if it does.
    """
    getter = getattr(controller, "TryGetVal", None)
    if not callable(getter):
        return {"queried": False, "reason": "no TryGetVal on this controller"}
    ok, value = getter(name)
    return {"queried": True, "exists": bool(ok), "value": value if ok else None}


def probe_nion() -> dict[str, Any]:
    """
    Read what a Nion instrument publishes, without driving anything.

    Returns
    -------
    dict[str, Any]
        Registry contents, camera identities, scan channels and which
        named controls answer.

    Notes
    -----
    Deliberately does **not** load device plug-ins. This project's device
    server does load them, because that is how components get registered
    in a process of its own — but on a live instrument computer a second
    process registering the same hardware is exactly what must not
    happen. If the registry is empty here, the answer is to run this from
    Nion Swift's own Python console, where the running application has
    already populated it.
    """
    report: dict[str, Any] = {"section": "nion"}
    report["packages"] = _safe(
        "installed nion packages",
        lambda: _installed_versions(("nion",)),
    )
    # Which vendor plug-in this project's server must load by name, and
    # whether autodiscovery would have skipped it. Listed, not imported.
    report["plugins"] = _safe("installed nionswift_plugin modules", _nion_plugins)

    try:
        from nion.utils import Registry  # noqa: PLC0415 - optional, probed
    except ImportError as error:
        report["registry_available"] = False
        report["note"] = (
            f"nion.utils is not importable here ({error}). Run this on the "
            f"instrument's control computer, in the interpreter Nion Swift "
            f"uses."
        )
        return report

    report["registry_available"] = True
    controller = Registry.get_component("stem_controller")
    if controller is None:
        report["stem_controller"] = None
        report["note"] = (
            "No 'stem_controller' component is registered in THIS process. "
            "That is expected when the script runs standalone: the "
            "components are registered by whichever process loaded the "
            "device plug-ins, normally Nion Swift itself. Re-run this from "
            "Swift's Python console (it will then see the live instrument). "
            "This script deliberately does not load the plug-ins itself, "
            "because doing so would claim hardware the running Swift "
            "session already owns."
        )
        return report

    report["stem_controller"] = {
        "class": type(controller).__name__,
        "module": type(controller).__module__,
        "surface": _public_names(controller),
        # Assumption 5 on the hardware checklist: the server falls back to
        # a 1 um stage when this is absent, which makes every default
        # field of view wrong rather than failing.
        "stage_size_nm": _safe(
            "stage_size_nm",
            lambda: _plain(getattr(controller, "stage_size_nm", None)),
        ),
        "probe_position": _safe(
            "probe_position",
            lambda: _plain(getattr(controller, "probe_position", None)),
        ),
        "subscan_state": _safe(
            "subscan_state",
            lambda: _plain(getattr(controller, "subscan_state", None)),
        ),
        "drift_tracker": _safe(
            "drift_tracker",
            lambda: type(getattr(controller, "drift_tracker", None)).__name__,
        ),
    }

    # THE question for SuperSTEM 2: if eels_camera is present, the UHV
    # Enfina is reached through Nion and needs no Gatan code at all. The
    # same read on SuperSTEM 3 says whether Nion also registers the ELA,
    # which would put two drivers on one detector.
    report["cameras"] = {
        name: _safe(name, lambda attribute=name: _nion_camera(controller, attribute))
        for name in ("ronchigram_camera", "eels_camera", "slit_camera")
    }
    report["scan_controller"] = _safe(
        "scan_controller",
        lambda: type(getattr(controller, "scan_controller", None)).__name__,
    )

    report["registry_components"] = {
        name: _safe(name, lambda key=name: Registry.get_component(key) is not None)
        for name in ("scan_module", "camera_module", "stem_controller")
    }

    # Every camera the registry holds, with what each device says about
    # itself: shape, binning, dark and gain support, calibration controls,
    # and whether it can be read in step with a scan.
    report["camera_modules"] = _safe(
        "camera modules",
        lambda: _nion_camera_modules(Registry),
    )

    # The multi-channel question: how many signals this column reads out
    # from one pass, and what they are called - plus the scan device's
    # current parameters and whether it can prepare a synchronised scan.
    report["scan"] = _safe("scan channels", lambda: _nion_scan_channels(Registry))

    # The application's view: hardware sources, their saved profiles (how
    # the operators actually acquire here), and grab_synchronized.
    report["hardware_sources"] = _safe("hardware sources", _nion_hardware_sources)

    report["controls"] = {
        name: _safe(name, lambda control=name: _nion_control(controller, control))
        for name in _NION_CONTROLS
    }
    report["controls_2d"] = {
        name: _safe(name, lambda control=name: _nion_2d_control(controller, control))
        for name in _NION_2D_CONTROLS
    }
    return report


def _simplon_get(address: str, module: str, version: str, path: str) -> Any:  # noqa: ANN401
    """
    Perform one SIMPLON ``GET``, returning the decoded ``value``.

    Parameters
    ----------
    address : str
        ``host`` or ``host:port`` of the detector control unit.
    module : str
        SIMPLON subsystem: ``detector``, ``monitor``, ``stream``, ``system``.
    version : str
        API version, which is part of the URL path.
    path : str
        The remainder, e.g. ``config/count_time`` or ``status/state``.

    Returns
    -------
    Any
        The ``value`` field if the answer has one, else the whole answer.

    Notes
    -----
    ``GET`` only, and no proxy. A control unit lives on a private
    instrument network, where an HTTP proxy either cannot route to it or
    resolves the address to something else entirely.
    """
    return _simplon_get_unversioned(address, f"{module}/api/{version}/{path}")


def _simplon_get_unversioned(address: str, path: str) -> Any:  # noqa: ANN401
    """
    Perform one SIMPLON ``GET`` at a path given in full, returning the ``value``.

    Parameters
    ----------
    address : str
        ``host`` or ``host:port`` of the detector control unit.
    path : str
        Everything after the host, e.g. ``detector/api/version``.

    Returns
    -------
    Any
        The ``value`` field if the answer has one, else the whole answer.
    """
    url = f"http://{address}/{path}"
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    # http:// is built above from a host, so the scheme is not attacker-chosen.
    request = urllib.request.Request(url, method="GET")
    with opener.open(request, timeout=_SIMPLON_TIMEOUT_S) as response:
        payload = json.loads(response.read().decode("utf-8"))
    if isinstance(payload, dict) and "value" in payload:
        return payload["value"]
    return payload


def probe_dectris(address: str) -> dict[str, Any]:
    """
    Read a DECTRIS control unit over SIMPLON, without arming it.

    Parameters
    ----------
    address : str
        ``host`` or ``host:port`` of the DCU. SIMPLON is served by the
        *control unit*, on port 80 — not by the detector head.

    Returns
    -------
    dict[str, Any]
        The API version that answered, the detector state, and every
        configuration key that could be read.

    Notes
    -----
    Every request is a ``GET``. Nothing is armed, triggered, disarmed or
    written, so this is safe to run while somebody else is using the
    detector — and the state it reports will say if somebody is.
    """
    report: dict[str, Any] = {"section": "dectris", "address": address}

    # SIMPLON's own answer to "which version", at the one path that is
    # not itself versioned. Recorded beside the by-trial answer below
    # rather than replacing it: an older DCU may not serve this path.
    report["advertised_api_version"] = _safe(
        "GET /detector/api/version",
        lambda: _simplon_get_unversioned(address, "detector/api/version"),
    )

    working: str | None = None
    attempts: dict[str, str] = {}
    for version in _SIMPLON_VERSIONS:
        try:
            _simplon_get(address, "detector", version, "config/description")
        except Exception as error:  # noqa: BLE001 - which version answers is the question
            attempts[version] = f"{type(error).__name__}: {error}"
        else:
            working = version
            break
    report["api_version_attempts"] = attempts
    report["api_version"] = working

    if working is None:
        report["reachable"] = False
        report["note"] = (
            "No SIMPLON API version answered. Check that this is the "
            "control unit's address rather than the detector head's, that "
            "the DCU has finished booting, and that this machine is on the "
            "control network rather than only the 10 GbE data link."
        )
        return report

    report["reachable"] = True

    # Read first, and report it prominently: 'ready' or 'acquire' means
    # something else owns the detector right now (GMS/Stela, Nion Swift,
    # or a LiberTEM-live session).
    report["state"] = _safe(
        "detector state",
        lambda: _simplon_get(address, "detector", working, "status/state"),
    )
    report["config_keys_published"] = _safe(
        "detector config key list",
        lambda: _simplon_get(address, "detector", working, "config/keys"),
    )
    report["status_keys_published"] = _safe(
        "detector status key list",
        lambda: _simplon_get(address, "detector", working, "status/keys"),
    )
    report["command_keys_published"] = _safe(
        "detector command key list",
        lambda: _simplon_get(address, "detector", working, "command/keys"),
    )
    report["config"] = {
        key: _safe(
            key,
            lambda k=key: _simplon_get(address, "detector", working, f"config/{k}"),
        )
        for key in _DETECTOR_CONFIG_KEYS
    }
    report["monitor"] = {
        name: _safe(
            name,
            lambda p=path: _simplon_get(address, "monitor", working, p),
        )
        for name, path in (
            ("mode", "config/mode"),
            ("buffer_size", "config/buffer_size"),
            ("discard_new", "config/discard_new"),
            ("state", "status/state"),
            ("buffer_fill_level", "status/buffer_fill_level"),
        )
    }
    report["stream"] = {
        name: _safe(
            name,
            lambda p=path: _simplon_get(address, "stream", working, p),
        )
        for name, path in (
            ("mode", "config/mode"),
            ("header_detail", "config/header_detail"),
            ("format", "config/format"),
            ("state", "status/state"),
        )
    }
    return report


def _find_directories(
    name_fragment: str,
    roots: tuple[str, ...] = _HITACHI_SEARCH_ROOTS,
) -> list[str]:
    """
    Look for directories whose name contains a fragment, within bounded roots.

    Parameters
    ----------
    name_fragment : str
        Lowercase substring to match against directory names.
    roots : tuple[str, ...]
        Directories to walk, a few levels deep each.

    Returns
    -------
    list[str]
        Matching directory paths, at most a few dozen.

    Notes
    -----
    Bounded in both breadth and depth on purpose: walking a whole drive
    on an instrument computer is neither quick nor polite.
    """
    found: list[str] = []
    for root in roots:
        base = Path(root)
        if not base.is_dir():
            continue
        for current, directories, _files in os.walk(base):
            depth = len(Path(current).relative_to(base).parts)
            if depth >= _HITACHI_SEARCH_DEPTH:
                directories[:] = []
                continue
            found.extend(
                str(Path(current) / directory)
                for directory in directories
                if name_fragment in directory.lower()
            )
            if len(found) > 50:  # noqa: PLR2004 - a bound, not a threshold
                return found
    return found


def probe_hitachi() -> dict[str, Any]:
    """
    Look for Hitachi's undocumented external-control modules, without importing.

    Returns
    -------
    dict[str, Any]
        Whether each module can be located, plus likely install
        directories and any Python sample folders.

    Notes
    -----
    ``importlib.util.find_spec`` resolves where a module *would* be
    imported from without executing it. That distinction is the whole
    safety property here: importing a vendor control module may open a
    connection to the column, which a survey must not do.
    """
    report: dict[str, Any] = {"section": "hitachi"}

    def _locate(name: str) -> dict[str, Any]:
        spec = importlib.util.find_spec(name)
        if spec is None:
            return {"found": False}
        return {
            "found": True,
            "origin": spec.origin,
            "submodule_locations": list(spec.submodule_search_locations or []),
        }

    report["modules"] = {
        name: _safe(name, lambda module=name: _locate(module))
        for name in _HITACHI_MODULES
    }
    report["sys_path"] = list(sys.path)
    report["directories"] = {
        fragment: _safe(fragment, lambda f=fragment: _find_directories(f))
        for fragment in (
            "hitachi",
            "su9000",
            "flow creator",
            "elementview",
            "pc-sem",
            "sem",
        )
    }
    report["python_files_named_mf"] = _safe(
        "Mf*.py on sys.path",
        lambda: [
            str(path)
            for entry in sys.path
            if entry and Path(entry).is_dir()
            for path in Path(entry).glob("Mf*.py")
        ],
    )
    return report


def probe_gatan() -> dict[str, Any]:
    """
    Say whether Gatan Microscopy Suite is here, and whether this is its Python.

    Returns
    -------
    dict[str, Any]
        Whether ``DigitalMicrograph`` can be located, whether it is
        *already loaded* (which means this interpreter is DM's own
        Python window), the module's surface if so, GMS-related
        environment variables, and likely install directories.

    Notes
    -----
    ``find_spec`` locates the module without importing it. If the module
    is already in ``sys.modules`` the script is running inside GMS, and
    listing the module's public names is a ``dir`` call on something
    the host loaded — no DM script is executed, no image is read, and
    nothing on the spectrometer is touched. The one thing worth knowing
    from inside GMS that this cannot see is which imaging-filter
    commands exist; that is a deliberate act for the hardware checklist,
    not a survey.
    """
    report: dict[str, Any] = {"section": "gatan"}
    loaded = sys.modules.get("DigitalMicrograph")
    if loaded is not None:
        # Already loaded by the host. find_spec is not consulted, because
        # an embedded module can carry no spec at all, and asking raises.
        spec = getattr(loaded, "__spec__", None)
        origin = getattr(spec, "origin", None) or getattr(loaded, "__file__", None)
        report["module"] = {"locatable": True, "origin": origin}
    else:
        spec = importlib.util.find_spec("DigitalMicrograph")
        report["module"] = {
            "locatable": spec is not None,
            "origin": getattr(spec, "origin", None),
        }
    report["inside_gms"] = loaded is not None
    report["module_surface"] = _public_names(loaded) if loaded is not None else None
    report["environment"] = {
        key: value
        for key, value in os.environ.items()
        if any(fragment in key.upper() for fragment in _GATAN_ENVIRONMENT_FRAGMENTS)
    }
    report["directories"] = {
        fragment: _safe(
            fragment,
            lambda f=fragment: _find_directories(f, _HITACHI_SEARCH_ROOTS),
        )
        for fragment in _GATAN_SEARCH_FRAGMENTS
    }
    if loaded is None and spec is None:
        report["note"] = (
            "DigitalMicrograph is not importable from this interpreter. That is "
            "expected on DigitalMicrograph 1.x and 2.x, which embed no Python; "
            "the directories and environment above still say whether and where "
            "DM is installed. On GMS 3.4 or newer, run --check and --gatan from "
            "DM's own Python window (Help > Python) as well, to record its "
            "interpreter and the module's surface."
        )
    return report


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """
    Parse the command line.

    Parameters
    ----------
    argv : list[str] | None
        Arguments, or None to read ``sys.argv``.

    Returns
    -------
    argparse.Namespace
        The parsed arguments.
    """
    parser = argparse.ArgumentParser(
        description="Read-only survey of a SuperSTEM instrument.",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="report this interpreter and what it could answer, probing nothing",
    )
    parser.add_argument("--nion", action="store_true", help="probe a Nion column")
    parser.add_argument(
        "--dectris",
        metavar="ADDRESS",
        default=None,
        help="probe a DECTRIS control unit at host or host:port",
    )
    parser.add_argument(
        "--hitachi", action="store_true", help="look for Hitachi control modules"
    )
    parser.add_argument(
        "--gatan",
        action="store_true",
        help="look for Gatan Microscopy Suite, and say if this is its Python",
    )
    parser.add_argument(
        "--all",
        action="store_true",
        help="run the Nion, Gatan and Hitachi sections (DECTRIS needs an address)",
    )
    parser.add_argument(
        "--out",
        default="superstem_survey.json",
        help="where to write the JSON report (default: %(default)s)",
    )
    return parser.parse_args(argv)


def _print_section(section: dict[str, Any]) -> None:
    """
    Print the handful of answers worth reading at the console.

    Parameters
    ----------
    section : dict[str, Any]
        One section of the report. The JSON file holds everything; this
        shows only what tells the operator whether the run went as meant.
    """
    name = section.get("section", "?")
    print(f"--- {name} ---")
    if name == "nion":
        cameras = section.get("cameras", {})
        eels = cameras.get("eels_camera", {}).get("value")
        controller = section.get("stem_controller") or {}
        controls = section.get("controls", {})
        plugins = (section.get("plugins") or {}).get("value") or {}
        sources = (section.get("hardware_sources") or {}).get("value") or {}
        print(f"  registry available : {section.get('registry_available')}")
        print(f"  plug-ins installed : {plugins.get('modules')}")
        print(
            f"  stem_controller    : {controller.get('class') if controller else None}"
        )
        print(f"  eels_camera        : {eels}")
        print(f"  hardware sources   : {sources.get('count')}")
        for control in ("ZLPoffset", "EELS_MagneticShift_Offset", "C_Blank"):
            answer = controls.get(control, {}).get("value") or {}
            exists, value = answer.get("exists"), answer.get("value")
            print(f"  {control:26} : exists={exists} value={value}")
    elif name == "gatan":
        print(f"  DigitalMicrograph  : {section.get('module')}")
        print(f"  inside GMS         : {section.get('inside_gms')}")
    elif name == "dectris":
        print(f"  reachable          : {section.get('reachable')}")
        print(f"  api version        : {section.get('api_version')}")
        print(f"  state              : {section.get('state', {}).get('value')}")
    elif name == "hitachi":
        for module, result in section.get("modules", {}).items():
            print(f"  {module:12} : {result.get('value')}")
    if section.get("note"):
        print(f"  NOTE: {section['note']}")
    print()


def _run_check() -> int:
    """
    Print the interpreter verdict and return an exit status.

    Returns
    -------
    int
        0 always, so that a check on a machine that cannot answer
        everything is not mistaken for a broken script.
    """
    verdict = interpreter_report()
    machine = verdict["machine"]
    print(f"Python     : {machine['python_version'].splitlines()[0]}")
    print(f"Executable : {machine['python_executable']}")
    print(f"Host       : {machine['hostname']}  ({machine['platform']})")
    print()
    print("This interpreter could answer:")
    for section, able in verdict["sections_this_interpreter_can_answer"].items():
        print(f"  {section:8} : {'yes' if able else 'no'}")
    for warning in verdict["warnings"]:
        print(f"\nNOTE: {warning}")
    return 0


def main(argv: list[str] | None = None) -> int:
    """
    Run the requested sections and write one JSON report.

    Parameters
    ----------
    argv : list[str] | None
        Arguments, or None to read ``sys.argv``.

    Returns
    -------
    int
        0 always: a survey that could not answer something has still
        succeeded at surveying, and the report says what it could not do.
    """
    args = _parse_args(argv)
    if args.check:
        return _run_check()

    sections: list[dict[str, Any]] = []
    if args.nion or args.all:
        sections.append(probe_nion())
    if args.dectris:
        sections.append(probe_dectris(args.dectris))
    if args.gatan or args.all:
        sections.append(probe_gatan())
    if args.hitachi or args.all:
        sections.append(probe_hitachi())

    if not sections:
        print("Nothing requested. Try --check first, then --nion, --dectris")
        print("ADDRESS, --gatan, --hitachi or --all.")
        return 0

    report = {
        "machine": machine_facts(),
        "interpreter": interpreter_report(),
        "sections": sections,
    }
    destination = Path(args.out)
    destination.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")

    print(f"Wrote {destination.resolve()}")
    print()
    for section in sections:
        _print_section(section)
    print("Send the JSON file back. It holds no data, no images, no credentials.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
