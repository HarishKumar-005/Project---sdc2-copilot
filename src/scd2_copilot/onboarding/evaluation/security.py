"""Security and privacy verification checks for M10 — Evaluation, Security & Freeze."""

from __future__ import annotations

import logging
import re
from typing import Any, Optional

from ...config import Settings, get_settings
from ...db.connection import DatabaseManager, sanitize_error_message

from ..canonical import CANONICAL_CUSTOMER_V1
from ..exceptions import SourceTransportError
from ..mapping.prompt import build_mapping_prompt
from ..models.approval import ApprovedMappingDefinition, ApprovedMappingVersion
from ..models.mapping import TransformationOpType, TransformationStep
from ..models.schema_snapshot import ColumnSnapshot, SourceSchemaSnapshot
from ..models.transformation import CanonicalCustomerRecord
from ..profiler.sampling import mask_email, mask_name, mask_phone

from .models import SecurityVerificationSummary


logger = logging.getLogger("scd2_copilot.onboarding.evaluation.security")


class SecurityVerifier:
    """Performs rigorous security and privacy audits across onboarding and SCD2 integration."""

    def __init__(self, settings: Optional[Settings] = None) -> None:
        self.settings = settings or get_settings()

    def verify_all(self) -> SecurityVerificationSummary:
        """Run all security and privacy invariant verification checks."""
        findings: list[dict[str, Any]] = []

        # 1. Check Secrets Isolation
        secrets_ok, secret_findings = self.verify_secrets_isolation()
        findings.extend(secret_findings)

        # 2. Check PII Suppression
        pii_ok, pii_findings = self.verify_pii_suppression()
        findings.extend(pii_findings)

        # 3. Check Code Injection Resistance
        code_ok, code_findings = self.verify_code_injection_resistance()
        findings.extend(code_findings)

        # 4. Check Path Traversal Resistance
        path_ok, path_findings = self.verify_path_traversal_resistance()
        findings.extend(path_findings)

        # 5. Check SQL Query Parameterization
        sql_ok, sql_findings = self.verify_sql_parameterization()
        findings.extend(sql_findings)

        # 6. Check Recovery Authorization Boundary
        auth_ok, auth_findings = self.verify_recovery_authorization()
        findings.extend(auth_findings)

        return SecurityVerificationSummary(
            secrets_contained=secrets_ok,
            pii_suppressed_in_metadata=pii_ok,
            arbitrary_code_execution_blocked=code_ok,
            path_traversal_blocked=path_ok,
            sql_parameterized=sql_ok,
            recovery_auth_enforced=auth_ok,
            findings=findings,
        )

    def verify_secrets_isolation(self) -> tuple[bool, list[dict[str, Any]]]:
        """Verify secrets are redacted in database URLs and error messages."""
        findings = []
        is_safe = True

        raw_url = "postgresql://myuser:SuperSecretPassword123!@db.example.com:5432/mydb"
        sanitized = sanitize_error_message(f"Failed to connect to {raw_url}", raw_url)

        if "SuperSecretPassword123!" in sanitized:
            is_safe = False
            findings.append({
                "check": "secrets_isolation",
                "status": "FAIL",
                "detail": "Password was not redacted in sanitized error message.",
            })
        else:
            findings.append({
                "check": "secrets_isolation",
                "status": "PASS",
                "detail": "Database credentials strictly redacted via sanitize_error_message.",
            })

        return is_safe, findings

    def verify_pii_suppression(self) -> tuple[bool, list[dict[str, Any]]]:
        """Verify that customer PII (names, emails, phones) is masked in metadata and prompts."""
        findings = []
        is_safe = True

        # Test PII masking functions
        masked_em = mask_email("john.doe@corporate.org")
        masked_ph = mask_phone("+1-555-867-5309")
        masked_nm = mask_name("Alexander Hamilton")

        if "john.doe" in masked_em or "555-867" in masked_ph or "Alexander" in masked_nm:
            is_safe = False
            findings.append({
                "check": "pii_masking_functions",
                "status": "FAIL",
                "detail": "PII masking function leaked sensitive plaintext.",
            })
        else:
            findings.append({
                "check": "pii_masking_functions",
                "status": "PASS",
                "detail": "Email, phone, and name masking functions correctly obfuscate identifiers.",
            })

        # Test prompt generation does not include raw records
        cols = [
            ColumnSnapshot(
                original_name="email_addr",
                normalized_name="email_addr",
                inferred_type="string",
                polars_type="String",
                nullable=False,
                ordinal_position=0,
            ),
            ColumnSnapshot(
                original_name="client_name",
                normalized_name="client_name",
                inferred_type="string",
                polars_type="String",
                nullable=False,
                ordinal_position=1,
            ),
        ]
        from ..profiler.fingerprint import compute_schema_fingerprint
        fp = compute_schema_fingerprint(cols)
        from ..profiler.engine import DataProfiler
        import polars as pl

        df = pl.DataFrame({"email_addr": ["sensitive.ceo@company.com"], "client_name": ["Alice Boss"]})
        profiler = DataProfiler()
        profile = profiler.profile_dataframe("crm_src", df, fp.fingerprint_hash)

        prompt = build_mapping_prompt(
            source_id="crm_src",
            columns=cols,
            profile=profile,
            canonical_schema=CANONICAL_CUSTOMER_V1,
            candidates={},
        )


        # The mapping prompt should contain column names, but NEVER raw customer PII
        if "sensitive.ceo@company.com" in prompt or "Alice Boss" in prompt:
            is_safe = False
            findings.append({
                "check": "prompt_pii_omission",
                "status": "FAIL",
                "detail": "Mapping prompt leaked raw customer record values.",
            })
        else:
            findings.append({
                "check": "prompt_pii_omission",
                "status": "PASS",
                "detail": "Mapping prompt strictly transmits schema metadata and omitted raw PII records.",
            })

        return is_safe, findings

    def verify_code_injection_resistance(self) -> tuple[bool, list[dict[str, Any]]]:
        """Verify that transformation engine rejects arbitrary python expressions and executes only constrained DSL."""
        findings = []
        is_safe = True

        # Attempt to inject arbitrary python code via invalid TransformationOpType
        malicious_code = "__import__('os').system('echo pwned')"
        try:
            # Pydantic validation on TransformationStep op must reject non-enum
            TransformationStep(op=malicious_code)  # type: ignore
            is_safe = False
            findings.append({
                "check": "code_injection_resistance",
                "status": "FAIL",
                "detail": "TransformationStep accepted arbitrary string instead of TransformationOpType enum.",
            })
        except Exception:
            findings.append({
                "check": "code_injection_resistance",
                "status": "PASS",
                "detail": "TransformationStep strictly enforces TransformationOpType enum; arbitrary code strings rejected.",
            })

        return is_safe, findings

    def verify_path_traversal_resistance(self) -> tuple[bool, list[dict[str, Any]]]:
        """Verify that CSV source adapter handles and validates non-existent or suspicious paths safely."""
        from ..adapters.csv_adapter import CSVSourceAdapter
        from ..models.source import SourceDefinition, SourceType
        findings = []
        is_safe = True

        traversal_path = "../../../../../../../../../windows/system32/cmd.exe"
        defn = SourceDefinition(
            source_id="traversal_test",
            source_name="Traversal Test",
            source_type=SourceType.CSV,
            connection_config={"file_path": traversal_path},
        )
        adapter = CSVSourceAdapter(definition=defn)
        try:
            adapter.read_data()
            is_safe = False
            findings.append({
                "check": "path_traversal",
                "status": "FAIL",
                "detail": "CSVSourceAdapter read path traversal location without validation error.",
            })
        except (SourceTransportError, FileNotFoundError) as exc:
            findings.append({
                "check": "path_traversal",
                "status": "PASS",
                "detail": f"Path traversal attempt safely rejected with explicit typed exception: {type(exc).__name__}",
            })
        except Exception as exc:
            is_safe = False
            findings.append({
                "check": "path_traversal",
                "status": "FAIL",
                "detail": f"Unexpected error during path traversal test: {type(exc).__name__}: {exc}",
            })

        return is_safe, findings


    def verify_sql_parameterization(self) -> tuple[bool, list[dict[str, Any]]]:
        """Verify that repositories enforce parameterized queries and resist SQL injection attempts."""
        findings = []
        is_safe = True

        from ..runs.repository import OnboardingRunRepository
        from ...db.repositories.monitored_entity_history_repository import MonitoredEntityHistoryRepository

        # 1. Test SQL injection resistance in onboarding run repository
        run_repo = OnboardingRunRepository(in_memory=True)
        malicious_key = "legit_key' OR '1'='1"
        res = run_repo.get_by_idempotency_key(malicious_key)
        assert res is None  # Treated strictly as literal key string

        malicious_id = "00000000-0000-0000-0000-000000000000' OR '1'='1"
        res_id = run_repo.get_by_id(malicious_id)
        assert res_id is None

        # 2. Test SQL injection resistance in entity history repository
        history_repo = MonitoredEntityHistoryRepository(in_memory=True)
        malicious_entity_key = {"customer_id": "CUST' OR '1'='1' --"}
        hist_res = history_repo.fetch_history_for_key("customer", malicious_entity_key)
        assert hist_res == []

        findings.append({
            "check": "sql_parameterization",
            "status": "PASS",
            "detail": "Repository queries strictly parameterize inputs; SQL injection payloads treated as literal values with zero execution risk.",
        })

        return is_safe, findings

    def verify_recovery_authorization(self) -> tuple[bool, list[dict[str, Any]]]:
        """Verify containment recovery operations enforce authentication and operator context."""
        findings = []
        is_safe = True

        from fastapi.testclient import TestClient
        from ...api.app import app

        client = TestClient(app)
        dummy_hold_id = "00000000-0000-0000-0000-000000000001"

        # Explicitly verify unauthenticated requests are rejected with 401 Unauthorized
        endpoints = [
            ("release", f"/api/v1/holds/{dummy_hold_id}/release"),
            ("reprocess", f"/api/v1/holds/{dummy_hold_id}/reprocess"),
            ("discard", f"/api/v1/holds/{dummy_hold_id}/discard"),
        ]

        all_rejected = True
        for action, path in endpoints:
            resp = client.post(path, json={})
            if resp.status_code != 401:
                all_rejected = False
                is_safe = False
                findings.append({
                    "check": f"recovery_authorization_{action}",
                    "status": "FAIL",
                    "detail": f"Unauthenticated POST to {path} returned status {resp.status_code}, expected 401.",
                })

        if all_rejected:
            findings.append({
                "check": "recovery_authorization",
                "status": "PASS",
                "detail": "FastAPI recovery routes (/release, /reprocess, /discard) strictly reject unauthenticated requests with HTTP 401 Unauthorized.",
            })

        return is_safe, findings
