"""Onboarding UI views package."""

from .data_quality import render_data_quality_view
from .demo_mode import render_demo_mode_view
from .exceptions import render_exceptions_view
from .guardrail import render_guardrail_view
from .mapping_review import render_mapping_review_view
from .new_onboarding import render_new_onboarding_view
from .overview import render_overview_view
from .runs import render_runs_view
from .scd2_history import render_scd2_history_view
from .schema_drift import render_schema_drift_view
from .settings_status import render_settings_status_view
from .source_onboarding import render_source_onboarding_view

__all__ = [
    "render_overview_view",
    "render_new_onboarding_view",
    "render_source_onboarding_view",
    "render_mapping_review_view",
    "render_data_quality_view",
    "render_exceptions_view",
    "render_runs_view",
    "render_schema_drift_view",
    "render_scd2_history_view",
    "render_guardrail_view",
    "render_settings_status_view",
    "render_demo_mode_view",
]
