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

Several sections can be combined in one run, and ``--all`` runs every
section that makes sense on the machine it finds itself on.
"""

from __future__ import annotations

import argparse
import contextlib
import importlib.util
import json
import os
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
    "ZLPoffset",
    "ConvergenceAngle",
    "CAperture",
    "StageOutX",
    "StageOutY",
    "probe_ha",
)
"""
Named controls to ask for, by read only.

``ZLPoffset`` is the load-bearing one: it is the spectrometer's
drift-tube offset, and Nion publishing it would mean Nion drives the
spectrometer — which would make the UHV Enfina on SuperSTEM 2 supported
today, with no Gatan code at all.
"""

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

    return {
        "machine": machine_facts(),
        "sections_this_interpreter_can_answer": {
            "nion": can_nion,
            "dectris": True,
            "hitachi": sys.platform.startswith("win") or sys.platform == "linux",
        },
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


def _nion_camera(controller: Any, attribute: str) -> Any:  # noqa: ANN401 - vendor object
    """
    Describe one camera a stem controller exposes, if it exposes it.

    Parameters
    ----------
    controller : Any
        The registered ``stem_controller`` component.
    attribute : str
        Attribute name, ``ronchigram_camera`` or ``eels_camera``.

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
        "camera_id": getattr(camera, "camera_id", None),
        "camera_type": getattr(camera, "camera_type", None),
        "camera_name": getattr(camera, "camera_name", None),
    }


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
        Channel count and per-channel identity, or None without a scan module.
    """
    scan = registry.get_component("scan_module")
    device = getattr(scan, "device", None) if scan is not None else None
    if device is None:
        return None
    count = getattr(device, "channel_count", None)
    channels = []
    for index in range(int(count or 0)):
        info = getattr(device, "get_channel_info", None)
        entry: dict[str, Any] = {"index": index}
        if callable(info):
            with contextlib.suppress(Exception):
                channel = info(index)
                entry["name"] = getattr(channel, "name", None)
                entry["id"] = getattr(channel, "channel_id", None)
                entry["enabled"] = getattr(channel, "enabled", None)
        channels.append(entry)
    return {"channel_count": count, "channels": channels}


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
    }

    # THE question for SuperSTEM 2: if eels_camera is present, the UHV
    # Enfina is reached through Nion and needs no Gatan code at all.
    report["cameras"] = {
        name: _safe(name, lambda attribute=name: _nion_camera(controller, attribute))
        for name in ("ronchigram_camera", "eels_camera")
    }

    report["registry_components"] = {
        name: _safe(name, lambda key=name: Registry.get_component(key) is not None)
        for name in ("scan_module", "camera_module", "stem_controller")
    }

    # The multi-channel question: how many signals this column reads out
    # from one pass, and what they are called.
    report["scan"] = _safe("scan channels", lambda: _nion_scan_channels(Registry))

    report["controls"] = {
        name: _safe(name, lambda control=name: _nion_control(controller, control))
        for name in _NION_CONTROLS
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
    url = f"http://{address}/{module}/api/{version}/{path}"
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
            ("state", "status/state"),
        )
    }
    report["stream"] = {
        name: _safe(
            name,
            lambda p=path: _simplon_get(address, "stream", working, p),
        )
        for name, path in (("mode", "config/mode"), ("state", "status/state"))
    }
    return report


def _find_directories(name_fragment: str) -> list[str]:
    """
    Look for directories whose name contains a fragment, within bounded roots.

    Parameters
    ----------
    name_fragment : str
        Lowercase substring to match against directory names.

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
    for root in _HITACHI_SEARCH_ROOTS:
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
        for fragment in ("hitachi", "su9000", "flow creator", "pc-sem", "sem")
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
        "--all",
        action="store_true",
        help="run the Nion and Hitachi sections (DECTRIS needs an address)",
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
        print(f"  registry available : {section.get('registry_available')}")
        print(f"  stem_controller    : {section.get('stem_controller')}")
        print(f"  eels_camera        : {eels}")
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
    if args.hitachi or args.all:
        sections.append(probe_hitachi())

    if not sections:
        print("Nothing requested. Try --check first, then --nion, --dectris")
        print("ADDRESS, --hitachi or --all.")
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
