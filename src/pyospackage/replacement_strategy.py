# MIT License
#
# Copyright (c) 2025 pyOpenSci
#
# Permission is hereby granted, free of charge, to any person obtaining a copy
# of this software and associated documentation files (the "Software"), to deal
# in the Software without restriction, including without limitation the rights
# to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
# copies of the Software, and to permit persons to whom the Software is
# furnished to do so, subject to the following conditions:
#
# The above copyright notice and this permission notice (including the next
# paragraph) shall be included in all copies or substantial portions of the
# Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
# AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
# LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
# OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
# SOFTWARE.

"""Utilities for planning a STEM control and analysis replacement for Nion Swift."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ReinventionArea:
    """
    Describe a custom subsystem that should be replaced with standard tooling.

    Attributes
    ----------
    name : str
        The subsystem that has been reimplemented.
    current_approach : str
        A short description of the current custom approach.
    recommended_replacement : str
        The standard technology that should replace the custom approach.
    performance_concern : str
        The main performance or maintenance problem caused by the custom code.
    """

    name: str
    current_approach: str
    recommended_replacement: str
    performance_concern: str


@dataclass(frozen=True)
class ModernizationStep:
    """
    Describe a modernization task for a replacement or migration plan.

    Attributes
    ----------
    title : str
        The short name of the modernization step.
    reason : str
        Why this step matters.
    leverage : str
        The expected impact of the change.
    """

    title: str
    reason: str
    leverage: str


@dataclass(frozen=True)
class LicensingAssessment:
    """
    Summarize how a proprietary plugin can be used with an adaptation.

    Attributes
    ----------
    may_run_against_user_obtained_plugins : bool
        Whether the adaptation may run against plugins the user already has.
    may_redistribute_proprietary_plugins : bool
        Whether the adaptation may ship the proprietary plugins.
    requires_vendor_license_review : bool
        Whether the plugin license from the vendor must still be checked.
    summary : str
        A short description of the result.
    next_steps : tuple[str, ...]
        Follow-up actions to keep the adaptation compliant.
    """

    may_run_against_user_obtained_plugins: bool
    may_redistribute_proprietary_plugins: bool
    requires_vendor_license_review: bool
    summary: str
    next_steps: tuple[str, ...]


@dataclass(frozen=True)
class ReplacementPlan:
    """
    Capture the recommended starting point for a Swift replacement.

    Attributes
    ----------
    recommended_language : str
        The language that provides the easiest path to a complete solution.
    language_rationale : tuple[str, ...]
        Reasons for the language recommendation.
    hardware_priorities : tuple[str, ...]
        The most important subsystems to support first.
    reinventions : tuple[ReinventionArea, ...]
        Areas where custom Swift infrastructure should be retired.
    modernization_steps : tuple[ModernizationStep, ...]
        Ordered modernization recommendations.
    """

    recommended_language: str
    language_rationale: tuple[str, ...]
    hardware_priorities: tuple[str, ...]
    reinventions: tuple[ReinventionArea, ...]
    modernization_steps: tuple[ModernizationStep, ...]


def build_replacement_plan() -> ReplacementPlan:
    """
    Return a structured recommendation for a STEM application replacement.

    Returns
    -------
    ReplacementPlan
        The recommended starting point based on Swift's architecture and the
        surrounding Python ecosystem.
    """
    reinventions = (
        ReinventionArea(
            name="UI toolkit",
            current_approach="Custom NionUI widgets, layout, and declarative UI",
            recommended_replacement="PySide6 and established Qt tooling",
            performance_concern=(
                "A bespoke UI stack prevents reuse of upstream optimizations and "
                "adds maintenance overhead."
            ),
        ),
        ReinventionArea(
            name="Rendering pipeline",
            current_approach=(
                "Python-generated drawing commands replayed through a custom host"
            ),
            recommended_replacement=(
                "GPU-backed image rendering such as Qt OpenGL/RHI or VisPy"
            ),
            performance_concern=(
                "Python stays in the repaint hot path, causing object churn and "
                "single-threaded CPU rasterization."
            ),
        ),
        ReinventionArea(
            name="Reactive model layer",
            current_approach=(
                "Homegrown events, bindings, observables, streams, and registry"
            ),
            recommended_replacement=(
                "Qt signals, traitlets, or another standard reactive primitive"
            ),
            performance_concern=(
                "Fine-grained event fan-out amplifies repaints, persistence "
                "writes, and dependent recomputation."
            ),
        ),
        ReinventionArea(
            name="Persistence and data storage",
            current_approach="Custom project formats and eager Python materialization",
            recommended_replacement="SQLite for indexing plus zarr or HDF5 for arrays",
            performance_concern=(
                "Opening large libraries requires eager object loading instead of "
                "cheap metadata queries and chunked access."
            ),
        ),
    )

    modernization_steps = (
        ModernizationStep(
            title="Measure live bottlenecks first",
            reason=(
                "Profile repaint latency, event fan-out, and library open time "
                "before replacing any subsystem."
            ),
            leverage="Avoids optimizing the wrong layer in a highly bespoke stack.",
        ),
        ModernizationStep(
            title="Standardize on PySide6",
            reason=(
                "Retire the launcher indirection and align with a maintained Qt "
                "backend that is easier to package and extend."
            ),
            leverage=(
                "Deletes custom host complexity and unblocks direct rendering "
                "work."
            ),
        ),
        ModernizationStep(
            title="Move image display to the GPU",
            reason=(
                "Raster imagery should be uploaded once and composited by the GPU "
                "instead of rebuilt in Python for each frame."
            ),
            leverage=(
                "Directly improves pan, zoom, and live acquisition "
                "responsiveness."
            ),
        ),
        ModernizationStep(
            title="Make library access lazy",
            reason=(
                "Use an index plus chunked array storage so large datasets do not "
                "have to become Python objects at open time."
            ),
            leverage=(
                "Improves startup time, memory use, and downstream "
                "interoperability."
            ),
        ),
        ModernizationStep(
            title="Coalesce reactive updates",
            reason=(
                "Batch bursts of property changes into one repaint and one "
                "persistence write before attempting a wholesale framework swap."
            ),
            leverage="Cuts redundant work without requiring a full rewrite.",
        ),
    )

    return ReplacementPlan(
        recommended_language="Python",
        language_rationale=(
            (
                "Existing microscope control and analysis capabilities already "
                "have a strong Python ecosystem."
            ),
            (
                "Python keeps the easiest path to reuse of proprietary hardware "
                "plugins and scientific tooling."
            ),
            (
                "The main architectural change is to remove Python from "
                "rendering hot loops, not to abandon Python entirely."
            ),
        ),
        hardware_priorities=(
            "scan control",
            "camera control",
            "acquisition orchestration",
            "lazy data access",
            "GPU-backed image display",
        ),
        reinventions=reinventions,
        modernization_steps=modernization_steps,
    )


def assess_plugin_adaptation(
    *, distributed: bool, bundles_proprietary_plugins: bool
) -> LicensingAssessment:
    """
    Assess a safe adaptation path for proprietary hardware plugins.

    Parameters
    ----------
    distributed : bool
        Whether the adaptation will be distributed to other users.
    bundles_proprietary_plugins : bool
        Whether the distribution includes proprietary vendor plugins.

    Returns
    -------
    LicensingAssessment
        Guidance for using proprietary plugins with a GPL-based host.

    Raises
    ------
    ValueError
        Raised when proprietary plugins are marked as bundled for a scenario
        that is not being distributed.
    """
    if bundles_proprietary_plugins and not distributed:
        msg = (
            "bundles_proprietary_plugins only applies to distributed "
            "adaptations."
        )
        raise ValueError(msg)

    if not distributed:
        return LicensingAssessment(
            may_run_against_user_obtained_plugins=True,
            may_redistribute_proprietary_plugins=False,
            requires_vendor_license_review=True,
            summary=(
                "Internal use is the lowest-risk path: GPL obligations are "
                "normally triggered by distribution, but the vendor plugin "
                "license still governs access to the proprietary binaries."
            ),
            next_steps=(
                "Confirm the plugin agreement permits use with a modified host.",
                "Keep proprietary plugins out of any redistributed artifact.",
            ),
        )

    if bundles_proprietary_plugins:
        return LicensingAssessment(
            may_run_against_user_obtained_plugins=True,
            may_redistribute_proprietary_plugins=False,
            requires_vendor_license_review=True,
            summary=(
                "Do not distribute an adaptation bundled with proprietary "
                "plugins unless the vendor explicitly grants redistribution "
                "rights and the combined-work licensing risk is resolved."
            ),
            next_steps=(
                "Distribute only the GPL adaptation.",
                "Require end users to install vendor plugins separately.",
                "Ask the vendor for written permission if redistribution is required.",
            ),
        )

    return LicensingAssessment(
        may_run_against_user_obtained_plugins=True,
        may_redistribute_proprietary_plugins=False,
        requires_vendor_license_review=True,
        summary=(
            "A distributed GPL adaptation can be safer when it does not ship the "
            "proprietary plugins and relies on user-installed vendor binaries."
        ),
        next_steps=(
            "Distribute the adaptation without proprietary plugins.",
            "Document that users must obtain plugins from the vendor.",
            "Review the vendor terms before advertising compatibility.",
        ),
    )
