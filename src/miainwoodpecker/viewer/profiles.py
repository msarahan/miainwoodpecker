"""
Scan profiles: the same field of view, scanned three different ways.

An operator does not have one set of scan settings, they have several
that they move between constantly, and the difference between them is
*dwell and resolution* while the region stays put. Making them switch a
single dwell box back and forth is what a profile replaces.

The three, and what each is for
-------------------------------
:data:`VIEW`
    The continuous live loop. Short dwell, modest resolution, chosen so
    the display keeps up — it exists to show that something is there and
    where it is moving.
:data:`PREVIEW`
    **In between the other two, and the one whose purpose is least
    obvious from its settings.** It is a single scan at intermediate
    dwell and resolution, taken so the operator can judge *focus and
    astigmatism by eye* at a signal-to-noise the live view cannot reach.
    It is for looking, not for keeping: it updates the display and
    records nothing, because a focus check that littered the session
    with files would stop being used.
:data:`ACQUIRE`
    The image that is kept. Long dwell, full resolution, written to the
    session.

The field of view is deliberately **not** part of a profile. It is the
region under the probe — what the operator navigated to — and it must
not change when they switch from checking focus to taking the picture.
A profile that carried its own field of view would move the specimen
out from under them at the moment they were happiest with it.
"""

from __future__ import annotations

import dataclasses

VIEW = "view"
"""Profile name: the continuous live loop."""

PREVIEW = "preview"
"""Profile name: a single higher-SNR scan for judging focus by eye."""

ACQUIRE = "acquire"
"""Profile name: the scan that is kept."""

PROFILE_NAMES = (VIEW, PREVIEW, ACQUIRE)
"""Every profile, in increasing order of dwell — which is panel order."""

PROFILE_LABELS = {
    VIEW: "View",
    PREVIEW: "Preview",
    ACQUIRE: "Acquire",
}
"""How each profile is titled in the panel."""

PROFILE_TOOLTIPS = {
    VIEW: "The continuous live view - short dwell, so the display keeps up",
    PREVIEW: (
        "A single scan at higher signal-to-noise, for judging focus and "
        "astigmatism by eye. Shown, not saved"
    ),
    ACQUIRE: "The scan image that is kept - long dwell, full resolution",
}
"""What each profile is for, in the operator's words."""


@dataclasses.dataclass(frozen=True)
class ScanProfile:
    """
    One named combination of dwell and resolution.

    Attributes
    ----------
    dwell_us : float
        Dwell time per pixel, in microseconds.
    size_px : int
        Scan size per side, in pixels. Square, matching the size control
        the panel has always offered; a non-square scan is the
        target-area feature's business, not a profile's.

    Raises
    ------
    ValueError
        If either value is not positive. A zero dwell is not a fast scan
        and a zero size is not a small one.
    """

    dwell_us: float
    size_px: int

    def __post_init__(self) -> None:
        """Reject settings no scan could run."""
        if self.dwell_us <= 0:
            msg = f"dwell_us must be positive, got {self.dwell_us!r}"
            raise ValueError(msg)
        if self.size_px <= 0:
            msg = f"size_px must be positive, got {self.size_px!r}"
            raise ValueError(msg)


DEFAULT_PROFILES = {
    # An order of magnitude apart in dwell, which is the point: a live
    # view fast enough to follow drift, a focus check the eye can read,
    # and a kept image worth waiting for. The numbers are starting
    # points an operator is expected to change, not claims about any
    # particular column.
    VIEW: ScanProfile(dwell_us=1.0, size_px=256),
    PREVIEW: ScanProfile(dwell_us=4.0, size_px=512),
    ACQUIRE: ScanProfile(dwell_us=20.0, size_px=512),
}
"""The profiles a first launch comes up with."""


def stored_profiles(values: dict[str, object]) -> dict[str, ScanProfile]:
    """
    Return remembered profiles, falling back per profile to the defaults.

    Per profile rather than all-or-nothing: a file that has grown a
    typo in one entry should cost that one entry, not the other two.

    Parameters
    ----------
    values : dict[str, object]
        Loaded preferences.

    Returns
    -------
    dict[str, ScanProfile]
        One profile per name in :data:`PROFILE_NAMES`.
    """
    stored = values.get("scan_profiles")
    stored = stored if isinstance(stored, dict) else {}
    profiles = {}
    for name in PROFILE_NAMES:
        entry = stored.get(name)
        profiles[name] = _profile_from(entry) or DEFAULT_PROFILES[name]
    return profiles


def _profile_from(entry: object) -> ScanProfile | None:
    """
    Build a profile from a stored mapping, or None if it cannot be trusted.

    Parameters
    ----------
    entry : object
        The stored entry.

    Returns
    -------
    ScanProfile | None
        The profile, or None when the entry is missing or unusable.
    """
    if not isinstance(entry, dict):
        return None
    try:
        return ScanProfile(
            dwell_us=float(entry["dwell_us"]),
            size_px=int(entry["size_px"]),
        )
    except (KeyError, TypeError, ValueError):
        return None


def as_stored(profiles: dict[str, ScanProfile]) -> dict[str, dict[str, float]]:
    """
    Render profiles for the preferences file.

    Parameters
    ----------
    profiles : dict[str, ScanProfile]
        The profiles to store.

    Returns
    -------
    dict[str, dict[str, float]]
        A JSON-safe mapping.
    """
    return {
        name: {"dwell_us": profile.dwell_us, "size_px": profile.size_px}
        for name, profile in profiles.items()
    }
