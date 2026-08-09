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

"""Tests for the STEM replacement planning helpers."""

from pyospackage import assess_plugin_adaptation, build_replacement_plan


def test_build_replacement_plan_prefers_python_and_hardware_control():
    """The starter plan should prioritize Python and instrument control."""
    plan = build_replacement_plan()

    assert plan.recommended_language == "Python"
    assert "scan control" in plan.hardware_priorities
    assert "camera control" in plan.hardware_priorities
    assert any(
        area.name == "Rendering pipeline" for area in plan.reinventions
    )


def test_internal_use_assessment_allows_existing_plugins():
    """Internal adaptations may run with existing proprietary plugins."""
    assessment = assess_plugin_adaptation(
        distributed=False,
        bundles_proprietary_plugins=False,
    )

    assert assessment.may_use_existing_plugins is True
    assert assessment.may_redistribute_proprietary_plugins is False
    assert assessment.requires_vendor_license_review is True


def test_distributed_bundle_is_not_allowed():
    """Distributed bundles should not include proprietary vendor plugins."""
    assessment = assess_plugin_adaptation(
        distributed=True,
        bundles_proprietary_plugins=True,
    )

    assert assessment.may_use_existing_plugins is False
    assert assessment.may_redistribute_proprietary_plugins is False
    assert "Do not distribute" in assessment.summary
