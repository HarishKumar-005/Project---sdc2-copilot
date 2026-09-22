"""Evaluation datasets with known ground truth matching 12_verification_and_evaluation.md §22."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, time, timezone
from typing import Any, Optional

from ..canonical import CANONICAL_CUSTOMER_V1, CanonicalSchema
from ..models.approval import ApprovedMappingDefinition, ApprovedMappingVersion, ReviewDecisionType
from ..models.mapping import (
    CandidateMapping,
    MappingType,
    TransformationOpType,
    TransformationStep,
)
from ..models.schema_snapshot import ColumnSnapshot, SourceSchemaSnapshot
from ..profiler.fingerprint import compute_schema_fingerprint, normalize_column_name


def _make_col(
    original_name: str,
    inferred_type: str = "string",
    polars_type: str = "String",
    nullable: bool = False,
    ordinal_position: int = 0,
) -> ColumnSnapshot:
    """Helper to construct a valid, contract-conforming ColumnSnapshot."""
    return ColumnSnapshot(
        original_name=original_name,
        normalized_name=normalize_column_name(original_name),
        inferred_type=inferred_type,
        polars_type=polars_type,
        nullable=nullable,
        ordinal_position=ordinal_position,
    )



@dataclass(frozen=True)
class GroundTruthDataset:
    """A labeled evaluation dataset with explicit ground truth for mapping and validation."""

    dataset_id: str
    name: str
    description: str
    raw_records: list[dict[str, Any]]
    source_schema: SourceSchemaSnapshot
    ground_truth_mappings: dict[str, str]  # source_col -> canonical_col
    expected_transformations: dict[str, list[TransformationOpType]]
    expected_review_required: set[str]
    injected_violation_count: int
    expected_clean_count: int
    expected_violations: list[dict[str, Any]] = field(default_factory=list)


def get_dataset_a_clean(row_count: int = 50) -> GroundTruthDataset:
    """Dataset A — Clean: Zero data-quality errors, 1:1 direct identity mappings."""
    records: list[dict[str, Any]] = []
    base_date = date(2026, 1, 1)

    for i in range(1, row_count + 1):
        records.append({
            "customer_id": f"CUST-A{i:04d}",
            "first_name": f"FirstName{i}",
            "last_name": f"LastName{i}",
            "email": f"user.a{i}@example.com",
            "date_of_birth": "1985-06-15",
            "status": "ACTIVE" if i % 2 == 0 else "INACTIVE",
            "created_at": f"2026-01-01T12:{i % 60:02d}:00Z",
        })

    col_names = ["customer_id", "first_name", "last_name", "email", "date_of_birth", "status", "created_at"]
    cols = [_make_col(c, ordinal_position=idx) for idx, c in enumerate(col_names)]
    fp = compute_schema_fingerprint(cols)
    schema = SourceSchemaSnapshot(
        source_id="dataset_a_clean",
        schema_version=1,
        fingerprint=fp,
        columns=cols,
    )


    gt_mappings = {
        "customer_id": "customer_id",
        "first_name": "first_name",
        "last_name": "last_name",
        "email": "email",
        "date_of_birth": "date_of_birth",
        "status": "status",
        "created_at": "created_at",
    }

    return GroundTruthDataset(
        dataset_id="dataset_a_clean",
        name="Dataset A — Clean",
        description="Clean CRM customer dataset with zero formatting anomalies or validation failures.",
        raw_records=records,
        source_schema=schema,
        ground_truth_mappings=gt_mappings,
        expected_transformations={
            "created_at": [TransformationOpType.PARSE_DATE],
            "date_of_birth": [TransformationOpType.PARSE_DATE],
        },
        expected_review_required=set(),
        injected_violation_count=0,
        expected_clean_count=row_count,
        expected_violations=[],
    )


def get_dataset_b_moderately_messy(row_count: int = 50) -> GroundTruthDataset:
    """Dataset B — Moderately Messy: Renamed columns, formatting differences, injected errors."""
    records: list[dict[str, Any]] = []
    injected: list[dict[str, Any]] = []

    # Target: 5 injected violations among rows
    # Row 3: invalid email format
    # Row 7: invalid email format (no domain)
    # Row 12: missing required customer_id
    # Row 18: invalid status code
    # Row 25: unparseable date_of_birth
    for i in range(1, row_count + 1):
        cust_id = f"CUST-B{i:04d}"
        email = f"user.b{i}@example.com"
        status_code = "A" if i % 2 == 0 else "I"
        dob = "1988-04-12"
        signup = "2026-02-15 09:30:00"

        # Injections
        if i == 3:
            email = "not-an-email"
            injected.append({"row_index": i, "rule_id": "EMAIL_FORMAT", "field": "email"})
        elif i == 7:
            email = "user@@broken.com"
            injected.append({"row_index": i, "rule_id": "EMAIL_FORMAT", "field": "email"})
        elif i == 12:
            cust_id = ""  # Missing required
            injected.append({"row_index": i, "rule_id": "REQUIRED", "field": "customer_id"})
        elif i == 18:
            status_code = "UNKNOWN_CODE"
            injected.append({"row_index": i, "rule_id": "ENUM", "field": "status"})
        elif i == 25:
            dob = "invalid-date-format-999"
            injected.append({"row_index": i, "rule_id": "FORMAT", "field": "date_of_birth"})

        records.append({
            "cust_no": f"  {cust_id}  ",  # tests TRIM
            "first_name": f" Bob{i} ",
            "last_name": f" Smith{i} ",
            "email_address": f"  {email}  ",
            "dob": dob,
            "acct_status": status_code,
            "signup_date": signup,
        })

    col_names = ["cust_no", "first_name", "last_name", "email_address", "dob", "acct_status", "signup_date"]
    cols = [_make_col(c, ordinal_position=idx) for idx, c in enumerate(col_names)]
    fp = compute_schema_fingerprint(cols)
    schema = SourceSchemaSnapshot(
        source_id="dataset_b_moderately_messy",
        schema_version=1,
        fingerprint=fp,
        columns=cols,
    )


    gt_mappings = {
        "cust_no": "customer_id",
        "first_name": "first_name",
        "last_name": "last_name",
        "email_address": "email",
        "dob": "date_of_birth",
        "acct_status": "status",
        "signup_date": "created_at",
    }

    return GroundTruthDataset(
        dataset_id="dataset_b_moderately_messy",
        name="Dataset B — Moderately Messy",
        description="Realistic CRM dataset with renamed columns, whitespace, and 5 injected violations.",
        raw_records=records,
        source_schema=schema,
        ground_truth_mappings=gt_mappings,
        expected_transformations={
            "cust_no": [TransformationOpType.TRIM],
            "first_name": [TransformationOpType.TRIM],
            "last_name": [TransformationOpType.TRIM],
            "email_address": [TransformationOpType.TRIM, TransformationOpType.NORMALIZE_EMAIL],
            "dob": [TransformationOpType.PARSE_DATE],
            "acct_status": [TransformationOpType.MAP_ENUM],
            "signup_date": [TransformationOpType.PARSE_DATE],
        },
        expected_review_required=set(),
        injected_violation_count=len(injected),
        expected_clean_count=row_count - len(injected),
        expected_violations=injected,
    )


def get_dataset_c_highly_messy(row_count: int = 50) -> GroundTruthDataset:
    """Dataset C — Highly Messy: Ambiguous names, missing values, duplicate IDs, schema drift."""
    records: list[dict[str, Any]] = []
    injected: list[dict[str, Any]] = []

    for i in range(1, row_count + 1):
        # Injected violations:
        # Row 5 & 6: duplicate customer_id
        # Row 10: None customer_id
        # Row 15: Invalid email
        # Row 20: Invalid status
        # Row 30: Invalid date
        if i == 6:
            cust_id = "CUST-C0005"  # duplicate of row 5
            injected.append({"row_index": i, "rule_id": "DUPLICATE", "field": "customer_id"})
        elif i == 10:
            cust_id = None
            injected.append({"row_index": i, "rule_id": "REQUIRED", "field": "customer_id"})
        else:
            cust_id = f"CUST-C{i:04d}"

        email = f"user.c{i}@domain.co"
        if i == 15:
            email = "missing-at-sign.com"
            injected.append({"row_index": i, "rule_id": "EMAIL_FORMAT", "field": "email"})

        status_val = "ACTIVE"
        if i == 20:
            status_val = "SUSPENDED_INVALID"
            injected.append({"row_index": i, "rule_id": "ENUM", "field": "status"})

        dob = "1992-11-20"
        if i == 30:
            dob = "32/13/2099"  # invalid date
            injected.append({"row_index": i, "rule_id": "FORMAT", "field": "date_of_birth"})

        records.append({
            "cust_ref": cust_id,
            "first_name": f"Charlie{i}",
            "last_name": f"Davis{i}",
            "contact_channel": email,
            "born_on": dob,
            "state_flag": status_val,
            "reg_time": f"2026-03-01T10:{i % 60:02d}:00Z",
            "audit_tag": f"TAG-{i}",  # unmapped column
        })

    col_names = ["cust_ref", "first_name", "last_name", "contact_channel", "born_on", "state_flag", "reg_time", "audit_tag"]
    cols = [
        _make_col(
            c,
            ordinal_position=idx,
            nullable=(c in ("cust_ref", "contact_channel", "born_on", "audit_tag")),
        )
        for idx, c in enumerate(col_names)
    ]
    fp = compute_schema_fingerprint(cols)
    schema = SourceSchemaSnapshot(
        source_id="dataset_c_highly_messy",
        schema_version=1,
        fingerprint=fp,
        columns=cols,
    )


    gt_mappings = {
        "cust_ref": "customer_id",
        "first_name": "first_name",
        "last_name": "last_name",
        "contact_channel": "email",
        "born_on": "date_of_birth",
        "state_flag": "status",
        "reg_time": "created_at",
    }

    return GroundTruthDataset(
        dataset_id="dataset_c_highly_messy",
        name="Dataset C — Highly Messy",
        description="High-entropy dataset with ambiguous column names, unmapped columns, and injected errors.",
        raw_records=records,
        source_schema=schema,
        ground_truth_mappings=gt_mappings,
        expected_transformations={
            "born_on": [TransformationOpType.PARSE_DATE],
            "reg_time": [TransformationOpType.PARSE_DATE],
        },
        expected_review_required={"contact_channel", "cust_ref"},
        injected_violation_count=len(injected),
        expected_clean_count=row_count - len(injected),
        expected_violations=injected,
    )


def create_approved_mapping_for_dataset(
    dataset: GroundTruthDataset,
    version_str: str = "1.0.0",
    approver: str = "evaluator@scd2.test",
) -> ApprovedMappingVersion:
    """Helper to build an ApprovedMappingVersion from ground truth definitions."""
    rules = []
    for src_col, canon_col in dataset.ground_truth_mappings.items():
        ops = dataset.expected_transformations.get(src_col, [])
        steps = [TransformationStep(op=op) for op in ops]
        # Special enum map for dataset B
        if dataset.dataset_id == "dataset_b_moderately_messy" and src_col == "acct_status":
            steps = [
                TransformationStep(
                    op=TransformationOpType.MAP_ENUM,
                    params={"enum_map": {"A": "ACTIVE", "I": "INACTIVE", "S": "SUSPENDED"}},

                )
            ]

        rules.append(
            ApprovedMappingDefinition(
                source_field=src_col,
                target_field=canon_col,
                mapping_type=MappingType.DIRECT if not steps else MappingType.TRANSFORMED,
                transformations=steps,
                decision=ReviewDecisionType.APPROVE,
                reviewer=approver,
                confidence=1.0,
                provenance_reason="Ground truth mapping rule",
            )
        )

    return ApprovedMappingVersion(
        mapping_version_id=f"map_{dataset.dataset_id}_v1",
        source_id=dataset.dataset_id,
        source_fingerprint=dataset.source_schema.schema_fingerprint,
        canonical_schema_name="customer",
        canonical_schema_version=1,
        version_number=1,
        mappings=rules,
        is_complete=True,
        approved_by=approver,
    )
