"""Unit tests for deterministic mapping impact analysis and compatibility classification."""

from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory

from src.scd2_copilot.onboarding.drift.engine import DeterministicSchemaDiffEngine
from src.scd2_copilot.onboarding.drift.impact import MappingImpactAnalyzer
from src.scd2_copilot.onboarding.models.approval import (
    ApprovedMappingDefinition,
    ApprovedMappingVersion,
    ReviewDecisionType,
)
from src.scd2_copilot.onboarding.models.drift import (
    DriftType,
    MappingCompatibilityState,
    SchemaDriftReport,
)
from src.scd2_copilot.onboarding.models.mapping import MappingType
from src.scd2_copilot.onboarding.models.schema_snapshot import (
    ColumnSnapshot,
    SourceSchemaSnapshot,
)
from src.scd2_copilot.onboarding.profiler.fingerprint import compute_schema_fingerprint


def _make_snapshot(source_id: str, version: int, columns: list[ColumnSnapshot]) -> SourceSchemaSnapshot:
    fp = compute_schema_fingerprint(columns)
    return SourceSchemaSnapshot(
        source_id=source_id,
        schema_version=version,
        fingerprint=fp,
        columns=columns,
    )


def _make_approved_mapping(
    mapping_version_id: str,
    source_id: str,
    mappings: list[ApprovedMappingDefinition],
    source_fingerprint: str = "fake_hash",
) -> ApprovedMappingVersion:
    return ApprovedMappingVersion(
        mapping_version_id=mapping_version_id,
        source_id=source_id,
        source_fingerprint=source_fingerprint,
        source_schema_version=1,
        canonical_schema_name="customer",
        canonical_schema_version=1,
        version_number=1,
        mappings=mappings,
        is_complete=True,
        unmapped_required_fields=[],
        approved_by="steward_1",
    )


def test_unrelated_added_column_leaves_mapping_compatible():
    cols_v1 = [
        ColumnSnapshot(
            original_name="cust_id",
            normalized_name="cust_id",
            inferred_type="string",
            polars_type="String",
            nullable=False,
            ordinal_position=0,
        )
    ]
    cols_v2 = [
        ColumnSnapshot(
            original_name="cust_id",
            normalized_name="cust_id",
            inferred_type="string",
            polars_type="String",
            nullable=False,
            ordinal_position=0,
        ),
        ColumnSnapshot(
            original_name="unrelated_col",
            normalized_name="unrelated_col",
            inferred_type="string",
            polars_type="String",
            nullable=True,
            ordinal_position=1,
        ),
    ]
    s1 = _make_snapshot("crm", 1, cols_v1)
    s2 = _make_snapshot("crm", 2, cols_v2)

    approved = _make_approved_mapping(
        "map_v1",
        "crm",
        [
            ApprovedMappingDefinition(
                source_field="cust_id",
                target_field="customer_id",
                mapping_type=MappingType.DIRECT,
                decision=ReviewDecisionType.APPROVE,
                reviewer="steward",
                confidence=1.0,
                provenance_reason="Direct match",
            )
        ],
    )

    diff = DeterministicSchemaDiffEngine().diff(s1, s2)
    analyzer = MappingImpactAnalyzer()
    report = analyzer.analyze_impact(approved, diff)

    assert report.overall_compatibility == MappingCompatibilityState.COMPATIBLE
    assert report.review_required is False
    assert len(report.impacted_mappings) == 0


def test_removed_unmapped_column_leaves_mapping_compatible():
    cols_v1 = [
        ColumnSnapshot(
            original_name="cust_id",
            normalized_name="cust_id",
            inferred_type="string",
            polars_type="String",
            nullable=False,
            ordinal_position=0,
        ),
        ColumnSnapshot(
            original_name="fax",
            normalized_name="fax",
            inferred_type="string",
            polars_type="String",
            nullable=True,
            ordinal_position=1,
        ),
    ]
    cols_v2 = [
        ColumnSnapshot(
            original_name="cust_id",
            normalized_name="cust_id",
            inferred_type="string",
            polars_type="String",
            nullable=False,
            ordinal_position=0,
        )
    ]
    s1 = _make_snapshot("crm", 1, cols_v1)
    s2 = _make_snapshot("crm", 2, cols_v2)

    approved = _make_approved_mapping(
        "map_v1",
        "crm",
        [
            ApprovedMappingDefinition(
                source_field="cust_id",
                target_field="customer_id",
                mapping_type=MappingType.DIRECT,
                decision=ReviewDecisionType.APPROVE,
                reviewer="steward",
                confidence=1.0,
                provenance_reason="Direct match",
            ),
            ApprovedMappingDefinition(
                source_field="fax",
                target_field=None,
                mapping_type=MappingType.UNMAPPED,
                decision=ReviewDecisionType.REJECT,
                reviewer="steward",
                confidence=1.0,
                provenance_reason="Ignored unmapped field",
            ),
        ],
    )

    diff = DeterministicSchemaDiffEngine().diff(s1, s2)
    analyzer = MappingImpactAnalyzer()
    report = analyzer.analyze_impact(approved, diff)

    assert report.overall_compatibility == MappingCompatibilityState.COMPATIBLE
    assert report.review_required is False
    assert len(report.impacted_mappings) == 0


def test_removed_mapped_column_produces_broken_impact():
    cols_v1 = [
        ColumnSnapshot(
            original_name="cust_id",
            normalized_name="cust_id",
            inferred_type="string",
            polars_type="String",
            nullable=False,
            ordinal_position=0,
        ),
        ColumnSnapshot(
            original_name="email",
            normalized_name="email",
            inferred_type="string",
            polars_type="String",
            nullable=False,
            ordinal_position=1,
        ),
    ]
    cols_v2 = [
        ColumnSnapshot(
            original_name="cust_id",
            normalized_name="cust_id",
            inferred_type="string",
            polars_type="String",
            nullable=False,
            ordinal_position=0,
        )
    ]
    s1 = _make_snapshot("crm", 1, cols_v1)
    s2 = _make_snapshot("crm", 2, cols_v2)

    approved = _make_approved_mapping(
        "map_v1",
        "crm",
        [
            ApprovedMappingDefinition(
                source_field="cust_id",
                target_field="customer_id",
                mapping_type=MappingType.DIRECT,
                decision=ReviewDecisionType.APPROVE,
                reviewer="steward",
                confidence=1.0,
                provenance_reason="Direct match",
            ),
            ApprovedMappingDefinition(
                source_field="email",
                target_field="email",
                mapping_type=MappingType.DIRECT,
                decision=ReviewDecisionType.APPROVE,
                reviewer="steward",
                confidence=1.0,
                provenance_reason="Direct match",
            ),
        ],
    )

    diff = DeterministicSchemaDiffEngine().diff(s1, s2)
    analyzer = MappingImpactAnalyzer()
    report = analyzer.analyze_impact(approved, diff)

    assert report.overall_compatibility == MappingCompatibilityState.BROKEN
    assert report.review_required is True
    assert len(report.impacted_mappings) == 1
    impact = report.impacted_mappings[0]
    assert impact.source_field == "email"
    assert impact.target_field == "email"
    assert impact.compatibility == MappingCompatibilityState.BROKEN
    assert impact.impact_category == DriftType.REMOVED_COLUMN


def test_possible_rename_provides_replacement_candidate():
    cols_v1 = [
        ColumnSnapshot(
            original_name="signup_date",
            normalized_name="signup_date",
            inferred_type="date",
            polars_type="Date",
            nullable=False,
            ordinal_position=0,
        )
    ]
    cols_v2 = [
        ColumnSnapshot(
            original_name="registration_date",
            normalized_name="registration_date",
            inferred_type="date",
            polars_type="Date",
            nullable=False,
            ordinal_position=0,
        )
    ]
    s1 = _make_snapshot("crm", 1, cols_v1)
    s2 = _make_snapshot("crm", 2, cols_v2)

    approved = _make_approved_mapping(
        "map_v1",
        "crm",
        [
            ApprovedMappingDefinition(
                source_field="signup_date",
                target_field="created_at",
                mapping_type=MappingType.DIRECT,
                decision=ReviewDecisionType.APPROVE,
                reviewer="steward",
                confidence=1.0,
                provenance_reason="Direct match",
            )
        ],
    )

    diff = DeterministicSchemaDiffEngine().diff(s1, s2)
    analyzer = MappingImpactAnalyzer()
    report = analyzer.analyze_impact(approved, diff)

    assert report.overall_compatibility == MappingCompatibilityState.BROKEN
    assert len(report.impacted_mappings) == 1
    impact = report.impacted_mappings[0]
    assert impact.source_field == "signup_date"
    assert impact.replacement_candidate == "registration_date"
    assert impact.replacement_confidence is not None
    assert impact.replacement_confidence >= 0.6


def test_type_changed_on_mapped_field_evaluates_compatibility():
    # 1. Castable type change (e.g. string to date when mapped to date target created_at)
    cols_v1 = [
        ColumnSnapshot(
            original_name="signup_date",
            normalized_name="signup_date",
            inferred_type="string",
            polars_type="String",
            nullable=False,
            ordinal_position=0,
        )
    ]
    cols_v2 = [
        ColumnSnapshot(
            original_name="signup_date",
            normalized_name="signup_date",
            inferred_type="date",
            polars_type="Date",
            nullable=False,
            ordinal_position=0,
        )
    ]
    s1 = _make_snapshot("crm", 1, cols_v1)
    s2 = _make_snapshot("crm", 2, cols_v2)

    approved = _make_approved_mapping(
        "map_v1",
        "crm",
        [
            ApprovedMappingDefinition(
                source_field="signup_date",
                target_field="created_at",
                mapping_type=MappingType.TRANSFORMED,
                decision=ReviewDecisionType.APPROVE,
                reviewer="steward",
                confidence=1.0,
                provenance_reason="Date parse",
            )
        ],
    )

    diff = DeterministicSchemaDiffEngine().diff(s1, s2)
    analyzer = MappingImpactAnalyzer()
    report = analyzer.analyze_impact(approved, diff)

    # Date to date target is castable/compatible -> COMPATIBLE_WITH_REVIEW
    assert report.overall_compatibility == MappingCompatibilityState.COMPATIBLE_WITH_REVIEW
    assert report.review_required is True


def test_incompatible_type_change_evaluates_to_broken():
    cols_v1 = [
        ColumnSnapshot(
            original_name="created_val",
            normalized_name="created_val",
            inferred_type="date",
            polars_type="Date",
            nullable=False,
            ordinal_position=0,
        )
    ]
    cols_v2 = [
        ColumnSnapshot(
            original_name="created_val",
            normalized_name="created_val",
            inferred_type="float",
            polars_type="Float64",
            nullable=False,
            ordinal_position=0,
        )
    ]
    s1 = _make_snapshot("crm", 1, cols_v1)
    s2 = _make_snapshot("crm", 2, cols_v2)

    approved = _make_approved_mapping(
        "map_v1",
        "crm",
        [
            ApprovedMappingDefinition(
                source_field="created_val",
                target_field="created_at",
                mapping_type=MappingType.DIRECT,
                decision=ReviewDecisionType.APPROVE,
                reviewer="steward",
                confidence=1.0,
                provenance_reason="Direct",
            )
        ],
    )

    diff = DeterministicSchemaDiffEngine().diff(s1, s2)
    analyzer = MappingImpactAnalyzer()
    report = analyzer.analyze_impact(approved, diff)

    # Float is not compatible with canonical date target created_at -> BROKEN
    assert report.overall_compatibility == MappingCompatibilityState.BROKEN
    assert report.review_required is True


def test_nullability_change_on_required_field_evaluates_to_review():
    cols_v1 = [
        ColumnSnapshot(
            original_name="cust_id",
            normalized_name="cust_id",
            inferred_type="string",
            polars_type="String",
            nullable=False,
            ordinal_position=0,
        )
    ]
    cols_v2 = [
        ColumnSnapshot(
            original_name="cust_id",
            normalized_name="cust_id",
            inferred_type="string",
            polars_type="String",
            nullable=True,
            ordinal_position=0,
        )
    ]
    s1 = _make_snapshot("crm", 1, cols_v1)
    s2 = _make_snapshot("crm", 2, cols_v2)

    approved = _make_approved_mapping(
        "map_v1",
        "crm",
        [
            ApprovedMappingDefinition(
                source_field="cust_id",
                target_field="customer_id",
                mapping_type=MappingType.DIRECT,
                decision=ReviewDecisionType.APPROVE,
                reviewer="steward",
                confidence=1.0,
                provenance_reason="Direct",
            )
        ],
    )

    diff = DeterministicSchemaDiffEngine().diff(s1, s2)
    analyzer = MappingImpactAnalyzer()
    report = analyzer.analyze_impact(approved, diff)

    # Primary key became nullable in source -> COMPATIBLE_WITH_REVIEW
    assert report.overall_compatibility == MappingCompatibilityState.COMPATIBLE_WITH_REVIEW
    assert report.review_required is True


def test_mapping_immutability_preserved():
    cols = [
        ColumnSnapshot(
            original_name="id",
            normalized_name="id",
            inferred_type="string",
            polars_type="String",
            nullable=False,
            ordinal_position=0,
        )
    ]
    s1 = _make_snapshot("crm", 1, cols)
    s2 = _make_snapshot("crm", 2, cols)

    approved = _make_approved_mapping(
        "map_v1",
        "crm",
        [
            ApprovedMappingDefinition(
                source_field="id",
                target_field="customer_id",
                mapping_type=MappingType.DIRECT,
                decision=ReviewDecisionType.APPROVE,
                reviewer="steward",
                confidence=1.0,
                provenance_reason="Direct",
            )
        ],
    )

    mapping_dict_before = approved.model_dump(mode="json")
    diff = DeterministicSchemaDiffEngine().diff(s1, s2)
    analyzer = MappingImpactAnalyzer()
    _ = analyzer.analyze_impact(approved, diff)
    mapping_dict_after = approved.model_dump(mode="json")

    assert mapping_dict_before == mapping_dict_after


def test_drift_report_artifact_roundtrip():
    cols_v1 = [
        ColumnSnapshot(
            original_name="id",
            normalized_name="id",
            inferred_type="string",
            polars_type="String",
            nullable=False,
            ordinal_position=0,
        )
    ]
    cols_v2 = [
        ColumnSnapshot(
            original_name="id",
            normalized_name="id",
            inferred_type="string",
            polars_type="String",
            nullable=False,
            ordinal_position=0,
        ),
        ColumnSnapshot(
            original_name="new_col",
            normalized_name="new_col",
            inferred_type="string",
            polars_type="String",
            nullable=True,
            ordinal_position=1,
        ),
    ]
    s1 = _make_snapshot("crm", 1, cols_v1)
    s2 = _make_snapshot("crm", 2, cols_v2)

    approved = _make_approved_mapping(
        "map_v1",
        "crm",
        [
            ApprovedMappingDefinition(
                source_field="id",
                target_field="customer_id",
                mapping_type=MappingType.DIRECT,
                decision=ReviewDecisionType.APPROVE,
                reviewer="steward",
                confidence=1.0,
                provenance_reason="Direct",
            )
        ],
    )

    diff = DeterministicSchemaDiffEngine().diff(s1, s2)
    report = MappingImpactAnalyzer().analyze_impact(approved, diff)

    with TemporaryDirectory() as tmpdir:
        path = Path(tmpdir) / "report.json"
        report.save_artifact(path)
        assert path.exists()

        loaded = SchemaDriftReport.load_artifact(path)
        assert loaded.report_id == report.report_id
        assert loaded.source_id == report.source_id
        assert loaded.overall_compatibility == report.overall_compatibility
        assert loaded.prior_fingerprint == report.prior_fingerprint
        assert len(loaded.drift_events) == len(report.drift_events)
