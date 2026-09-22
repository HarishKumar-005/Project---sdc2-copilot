"""Realistic schema evolution scenarios for CRM, Billing, and Support sources."""

from __future__ import annotations

from src.scd2_copilot.onboarding.drift.engine import DeterministicSchemaDiffEngine
from src.scd2_copilot.onboarding.drift.impact import MappingImpactAnalyzer
from src.scd2_copilot.onboarding.drift.service import SchemaDriftService
from src.scd2_copilot.onboarding.models.approval import (
    ApprovedMappingDefinition,
    ApprovedMappingVersion,
    ReviewDecisionType,
)
from src.scd2_copilot.onboarding.models.drift import (
    DriftType,
    MappingCompatibilityState,
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


def test_crm_realistic_schema_evolution_rename_and_added_column():
    """CRM v1 -> v2:

    - 'Created Date' replaced by 'Registration Date' (possible rename)
    - 'Loyalty Tier' added (unrelated added column)
    - 'Contact Email' nullability changed from False to True
    """
    v1_cols = [
        ColumnSnapshot(
            original_name="Client ID",
            normalized_name="client_id",
            inferred_type="string",
            polars_type="String",
            nullable=False,
            ordinal_position=0,
        ),
        ColumnSnapshot(
            original_name="First Name",
            normalized_name="first_name",
            inferred_type="string",
            polars_type="String",
            nullable=False,
            ordinal_position=1,
        ),
        ColumnSnapshot(
            original_name="Last Name",
            normalized_name="last_name",
            inferred_type="string",
            polars_type="String",
            nullable=False,
            ordinal_position=2,
        ),
        ColumnSnapshot(
            original_name="Contact Email",
            normalized_name="contact_email",
            inferred_type="string",
            polars_type="String",
            nullable=False,
            ordinal_position=3,
        ),
        ColumnSnapshot(
            original_name="Created Date",
            normalized_name="created_date",
            inferred_type="date",
            polars_type="Date",
            nullable=False,
            ordinal_position=4,
        ),
    ]

    v2_cols = [
        ColumnSnapshot(
            original_name="Client ID",
            normalized_name="client_id",
            inferred_type="string",
            polars_type="String",
            nullable=False,
            ordinal_position=0,
        ),
        ColumnSnapshot(
            original_name="First Name",
            normalized_name="first_name",
            inferred_type="string",
            polars_type="String",
            nullable=False,
            ordinal_position=1,
        ),
        ColumnSnapshot(
            original_name="Last Name",
            normalized_name="last_name",
            inferred_type="string",
            polars_type="String",
            nullable=False,
            ordinal_position=2,
        ),
        ColumnSnapshot(
            original_name="Contact Email",
            normalized_name="contact_email",
            inferred_type="string",
            polars_type="String",
            nullable=True,  # Became nullable
            ordinal_position=3,
        ),
        ColumnSnapshot(
            original_name="Registration Date",  # Renamed from Created Date
            normalized_name="registration_date",
            inferred_type="date",
            polars_type="Date",
            nullable=False,
            ordinal_position=4,
        ),
        ColumnSnapshot(
            original_name="Loyalty Tier",  # New column
            normalized_name="loyalty_tier",
            inferred_type="string",
            polars_type="String",
            nullable=True,
            ordinal_position=5,
        ),
    ]

    crm_v1 = _make_snapshot("crm_customer", 1, v1_cols)
    crm_v2 = _make_snapshot("crm_customer", 2, v2_cols)

    mapping_v1 = ApprovedMappingVersion(
        mapping_version_id="map_ver_crm_v1",
        source_id="crm_customer",
        source_fingerprint=crm_v1.fingerprint.fingerprint_hash,
        source_schema_version=1,
        canonical_schema_name="customer",
        canonical_schema_version=1,
        version_number=1,
        mappings=[
            ApprovedMappingDefinition(
                source_field="Client ID",
                target_field="customer_id",
                mapping_type=MappingType.DIRECT,
                decision=ReviewDecisionType.APPROVE,
                reviewer="steward",
                confidence=1.0,
                provenance_reason="Direct",
            ),
            ApprovedMappingDefinition(
                source_field="First Name",
                target_field="first_name",
                mapping_type=MappingType.DIRECT,
                decision=ReviewDecisionType.APPROVE,
                reviewer="steward",
                confidence=1.0,
                provenance_reason="Direct",
            ),
            ApprovedMappingDefinition(
                source_field="Last Name",
                target_field="last_name",
                mapping_type=MappingType.DIRECT,
                decision=ReviewDecisionType.APPROVE,
                reviewer="steward",
                confidence=1.0,
                provenance_reason="Direct",
            ),
            ApprovedMappingDefinition(
                source_field="Contact Email",
                target_field="email",
                mapping_type=MappingType.DIRECT,
                decision=ReviewDecisionType.APPROVE,
                reviewer="steward",
                confidence=1.0,
                provenance_reason="Direct",
            ),
            ApprovedMappingDefinition(
                source_field="Created Date",
                target_field="created_at",
                mapping_type=MappingType.DIRECT,
                decision=ReviewDecisionType.APPROVE,
                reviewer="steward",
                confidence=1.0,
                provenance_reason="Direct",
            ),
        ],
        is_complete=True,
        unmapped_required_fields=[],
        approved_by="steward",
    )

    service = SchemaDriftService()
    report = service.evaluate_drift(
        prior_schema=crm_v1,
        current_schema=crm_v2,
        approved_mapping=mapping_v1,
        persist=False,
    )

    # Assert overall compatibility is BROKEN because Created Date is removed
    assert report.overall_compatibility == MappingCompatibilityState.BROKEN
    assert report.review_required is True

    # Assert possible rename detected
    rename_event = next(
        (e for e in report.drift_events if e.drift_type == DriftType.POSSIBLE_RENAME),
        None,
    )
    assert rename_event is not None
    assert rename_event.old_field_name == "Created Date"
    assert rename_event.new_field_name == "Registration Date"

    # Assert Created Date impact carries candidate replacement
    created_impact = next(
        (i for i in report.impacted_mappings if i.source_field == "Created Date"),
        None,
    )
    assert created_impact is not None
    assert created_impact.compatibility == MappingCompatibilityState.BROKEN
    assert created_impact.replacement_candidate == "Registration Date"

    # Assert Contact Email nullability change is marked for review
    email_impact = next(
        (i for i in report.impacted_mappings if i.source_field == "Contact Email"),
        None,
    )
    assert email_impact is not None
    assert email_impact.compatibility == MappingCompatibilityState.COMPATIBLE_WITH_REVIEW

    # Assert Loyalty Tier is not present in impacted mappings because it was not mapped
    assert not any(i.source_field == "Loyalty Tier" for i in report.impacted_mappings)


def test_billing_realistic_schema_evolution_type_change():
    """Billing v1 -> v2:

    - 'acct_code' unchanged
    - 'balance_due' changed from float to string (e.g. "$1,250.00")
    - 'billing_cycle' added
    """
    v1_cols = [
        ColumnSnapshot(
            original_name="acct_code",
            normalized_name="acct_code",
            inferred_type="string",
            polars_type="String",
            nullable=False,
            ordinal_position=0,
        ),
        ColumnSnapshot(
            original_name="balance_due",
            normalized_name="balance_due",
            inferred_type="float",
            polars_type="Float64",
            nullable=True,
            ordinal_position=1,
        ),
    ]
    v2_cols = [
        ColumnSnapshot(
            original_name="acct_code",
            normalized_name="acct_code",
            inferred_type="string",
            polars_type="String",
            nullable=False,
            ordinal_position=0,
        ),
        ColumnSnapshot(
            original_name="balance_due",
            normalized_name="balance_due",
            inferred_type="string",
            polars_type="String",
            nullable=True,
            ordinal_position=1,
        ),
        ColumnSnapshot(
            original_name="billing_cycle",
            normalized_name="billing_cycle",
            inferred_type="string",
            polars_type="String",
            nullable=False,
            ordinal_position=2,
        ),
    ]

    billing_v1 = _make_snapshot("billing_accounts", 1, v1_cols)
    billing_v2 = _make_snapshot("billing_accounts", 2, v2_cols)

    # In mapping, acct_code -> customer_id, balance_due -> unmapped
    mapping_v1 = ApprovedMappingVersion(
        mapping_version_id="map_ver_billing_v1",
        source_id="billing_accounts",
        source_fingerprint=billing_v1.fingerprint.fingerprint_hash,
        source_schema_version=1,
        canonical_schema_name="customer",
        canonical_schema_version=1,
        version_number=1,
        mappings=[
            ApprovedMappingDefinition(
                source_field="acct_code",
                target_field="customer_id",
                mapping_type=MappingType.DIRECT,
                decision=ReviewDecisionType.APPROVE,
                reviewer="steward",
                confidence=1.0,
                provenance_reason="Direct",
            ),
            ApprovedMappingDefinition(
                source_field="balance_due",
                target_field=None,
                mapping_type=MappingType.UNMAPPED,
                decision=ReviewDecisionType.REJECT,
                reviewer="steward",
                confidence=1.0,
                provenance_reason="Excluded from customer profile",
            ),
        ],
        is_complete=False,
        unmapped_required_fields=["first_name", "last_name", "email"],
        approved_by="steward",
    )

    service = SchemaDriftService()
    report = service.evaluate_drift(
        prior_schema=billing_v1,
        current_schema=billing_v2,
        approved_mapping=mapping_v1,
        persist=False,
    )

    # Since balance_due is UNMAPPED and billing_cycle is unmapped, active mapping acct_code is COMPATIBLE
    assert report.overall_compatibility == MappingCompatibilityState.COMPATIBLE
    assert report.review_required is False
    assert len(report.impacted_mappings) == 0


def test_support_realistic_schema_evolution_missing_required_column():
    """Support v1 -> v2:

    - 'reporter_email' removed completely
    - Mapping previously mapped 'reporter_email' -> 'email' (mandatory canonical target)
    """
    v1_cols = [
        ColumnSnapshot(
            original_name="customer_id",
            normalized_name="customer_id",
            inferred_type="string",
            polars_type="String",
            nullable=False,
            ordinal_position=0,
        ),
        ColumnSnapshot(
            original_name="reporter_email",
            normalized_name="reporter_email",
            inferred_type="string",
            polars_type="String",
            nullable=False,
            ordinal_position=1,
        ),
    ]
    v2_cols = [
        ColumnSnapshot(
            original_name="customer_id",
            normalized_name="customer_id",
            inferred_type="string",
            polars_type="String",
            nullable=False,
            ordinal_position=0,
        ),
    ]

    support_v1 = _make_snapshot("support_tickets", 1, v1_cols)
    support_v2 = _make_snapshot("support_tickets", 2, v2_cols)

    mapping_v1 = ApprovedMappingVersion(
        mapping_version_id="map_ver_support_v1",
        source_id="support_tickets",
        source_fingerprint=support_v1.fingerprint.fingerprint_hash,
        source_schema_version=1,
        canonical_schema_name="customer",
        canonical_schema_version=1,
        version_number=1,
        mappings=[
            ApprovedMappingDefinition(
                source_field="customer_id",
                target_field="customer_id",
                mapping_type=MappingType.DIRECT,
                decision=ReviewDecisionType.APPROVE,
                reviewer="steward",
                confidence=1.0,
                provenance_reason="Direct",
            ),
            ApprovedMappingDefinition(
                source_field="reporter_email",
                target_field="email",
                mapping_type=MappingType.DIRECT,
                decision=ReviewDecisionType.APPROVE,
                reviewer="steward",
                confidence=1.0,
                provenance_reason="Direct",
            ),
        ],
        is_complete=False,
        unmapped_required_fields=["first_name", "last_name"],
        approved_by="steward",
    )

    service = SchemaDriftService()
    report = service.evaluate_drift(
        prior_schema=support_v1,
        current_schema=support_v2,
        approved_mapping=mapping_v1,
        persist=False,
    )

    assert report.overall_compatibility == MappingCompatibilityState.BROKEN
    assert report.review_required is True
    assert len(report.impacted_mappings) == 1
    assert report.impacted_mappings[0].source_field == "reporter_email"
    assert report.impacted_mappings[0].target_field == "email"
    assert report.impacted_mappings[0].replacement_candidate is None


def test_version_boundary_separation():
    """Verify that source schema version, fingerprint, canonical version, and mapping version remain distinct."""
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
    s2 = _make_snapshot("crm", 2, cols + [
        ColumnSnapshot(
            original_name="added",
            normalized_name="added",
            inferred_type="string",
            polars_type="String",
            nullable=True,
            ordinal_position=1,
        )
    ])

    mapping = ApprovedMappingVersion(
        mapping_version_id="map_v1",
        source_id="crm",
        source_fingerprint=s1.fingerprint.fingerprint_hash,
        source_schema_version=1,
        canonical_schema_name="customer",
        canonical_schema_version=1,
        version_number=1,
        mappings=[],
        approved_by="tester",
    )

    report = SchemaDriftService().evaluate_drift(s1, s2, mapping, persist=False)

    # 1. Source schema version evolves: 1 -> 2
    assert report.prior_schema_version == 1
    assert report.current_schema_version == 2
    assert report.prior_fingerprint != report.current_fingerprint

    # 2. Canonical schema version remains 1
    assert report.canonical_schema_version == 1

    # 3. Mapping version remains map_v1
    assert report.mapping_version_id == "map_v1"

    # 4. Prior schema was NOT mutated
    assert len(s1.columns) == 1
    assert s1.schema_version == 1
