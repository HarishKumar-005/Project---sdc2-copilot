"""Tests for privacy-preserving mapping prompt construction."""

import json
from pathlib import Path
import pytest

from scd2_copilot.onboarding.adapters.csv_adapter import CSVSourceAdapter
from scd2_copilot.onboarding.canonical import get_canonical_customer_v1
from scd2_copilot.onboarding.mapping.candidates import DeterministicCandidateGenerator
from scd2_copilot.onboarding.mapping.prompt import build_mapping_prompt
from scd2_copilot.onboarding.models.profile import SamplingConfig, SamplingPolicy
from scd2_copilot.onboarding.models.source import SourceDefinition, SourceType
from scd2_copilot.onboarding.profiler.engine import DataProfiler


def test_build_mapping_prompt_preserves_privacy(crm_csv_path: Path) -> None:
    defn = SourceDefinition(
        source_id="crm_privacy_test",
        source_name="CRM Test",
        source_type=SourceType.CSV,
        connection_config={"file_path": str(crm_csv_path)},
    )
    adapter = CSVSourceAdapter(defn)
    df = adapter.read_data()
    schema = adapter.discover_schema(df)
    profiler = DataProfiler(SamplingConfig(policy=SamplingPolicy.MASKED))
    profile = profiler.profile_dataframe("crm_privacy_test", df, schema.fingerprint.fingerprint_hash)

    canonical = get_canonical_customer_v1()
    gen = DeterministicCandidateGenerator(canonical)
    candidates = gen.generate_candidates(schema.columns, profile)

    prompt_str = build_mapping_prompt(
        "crm_privacy_test",
        schema.columns,
        profile,
        canonical,
        candidates,
    )

    # 1. Check prompt is valid JSON
    data = json.loads(prompt_str)
    assert data["source_system_id"] == "crm_privacy_test"
    assert "canonical_schema" in data
    assert "source_fields" in data
    assert "approved_transformation_vocabulary" in data

    # 2. Strict privacy check: No raw unmasked customer emails in prompt
    assert "alice.smith@example.com" not in prompt_str
    assert "bob.j@corporate.org" not in prompt_str
    assert "diana@themyscira.com" not in prompt_str

    # 3. Masked email is present
    assert "a***@example.com" in prompt_str or "b***@corporate.org" in prompt_str

    # 4. Check transformation vocabulary is listed
    assert "TRIM" in data["approved_transformation_vocabulary"]
    assert "PARSE_DATE" in data["approved_transformation_vocabulary"]
