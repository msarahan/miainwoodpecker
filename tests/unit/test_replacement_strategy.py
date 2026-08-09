"""
Tests for the STEM replacement planning helpers.
"""

from pyospackage import assess_plugin_adaptation, build_replacement_plan


def test_build_replacement_plan_prefers_python_and_hardware_control():
    """
    The starter plan should prioritize Python and instrument control.
    """

    plan = build_replacement_plan()

    assert plan.recommended_language == "Python"
    assert "scan control" in plan.hardware_priorities
    assert "camera control" in plan.hardware_priorities
    assert any(
        area.name == "Rendering pipeline" for area in plan.reinventions
    )


def test_internal_use_assessment_allows_existing_plugins():
    """
    Internal adaptations may run with existing proprietary plugins.
    """

    assessment = assess_plugin_adaptation(
        distributed=False,
        bundles_proprietary_plugins=False,
    )

    assert assessment.may_use_existing_plugins is True
    assert assessment.may_redistribute_proprietary_plugins is False
    assert assessment.requires_vendor_license_review is True


def test_distributed_bundle_is_not_allowed():
    """
    Distributed bundles should not include proprietary vendor plugins.
    """

    assessment = assess_plugin_adaptation(
        distributed=True,
        bundles_proprietary_plugins=True,
    )

    assert assessment.may_use_existing_plugins is False
    assert assessment.may_redistribute_proprietary_plugins is False
    assert "Do not distribute" in assessment.summary
