"""Tests for DataProfiler engine: completeness, uniqueness, string lengths, date patterns, and serialization."""

import json
from pathlib import Path
import polars as pl
import pytest

from scd2_copilot.onboarding.models.profile import DataProfile, SamplingConfig, SamplingPolicy
from scd2_copilot.onboarding.profiler.engine import DataProfiler, detect_date_patterns


def test_profiler_computes_exact_statistics() -> None:
    # 10 rows:
    # id: 10 non-null, 10 distinct (100% unique)
    # status: 2 nulls (null_rate = 0.2), 3 distinct non-null
    # name: strings with min length 3 ("Bob"), max length 7 ("Charlie")
    df = pl.DataFrame({
        "id": [1, 2, 3, 4, 5, 6, 7, 8, 9, 10],
        "status": ["A", "A", "B", "C", None, "A", None, "B", "C", "A"],
        "name": ["Bob", "Alice", "Charlie", "Dave", "Eve", "Frank", "Grace", "Heidi", "Ivan", "Judy"],
    })

    profiler = DataProfiler(SamplingConfig(policy=SamplingPolicy.RAW))
    profile = profiler.profile_dataframe("test_source", df)

    assert profile.source_id == "test_source"
    assert profile.total_rows == 10
    assert profile.total_columns == 3

    id_col = profile.get_column("id")
    assert id_col is not None
    assert id_col.total_count == 10
    assert id_col.null_count == 0
    assert id_col.null_rate == 0.0
    assert id_col.distinct_count == 10
    assert id_col.uniqueness_rate == 1.0
    assert id_col.is_unique is True

    status_col = profile.get_column("status")
    assert status_col is not None
    assert status_col.total_count == 10
    assert status_col.null_count == 2
    assert status_col.null_rate == 0.2
    assert status_col.distinct_count == 3
    assert status_col.is_unique is False
    assert len(status_col.top_values) == 3

    name_col = profile.get_column("name")
    assert name_col is not None
    assert name_col.min_length == 3
    assert name_col.max_length == 7


def test_detect_date_patterns() -> None:
    # Unambiguous ISO date
    iso_series = pl.Series("iso_dates", ["2023-01-15", "2023-02-28", "2023-11-05"])
    iso_reports = detect_date_patterns(iso_series)
    assert len(iso_reports) == 1
    assert iso_reports[0].pattern == "%Y-%m-%d"
    assert iso_reports[0].is_ambiguous is False

    # Ambiguous slash date (01/02/2024 could be Jan 2 or Feb 1)
    ambig_series = pl.Series("slash_dates", ["01/02/2024", "05/06/2023"])
    ambig_reports = detect_date_patterns(ambig_series)
    assert len(ambig_reports) == 1
    assert ambig_reports[0].is_ambiguous is True

    # Unambiguous slash date (25/02/2024 - day is 25 > 12)
    unambig_slash = pl.Series("slash_unambig", ["25/02/2024", "30/03/2024"])
    unambig_reports = detect_date_patterns(unambig_slash)
    assert len(unambig_reports) == 1
    assert unambig_reports[0].pattern == "%d/%m/%Y"
    assert unambig_reports[0].is_ambiguous is False


def test_profiler_reproducibility() -> None:
    df = pl.DataFrame({
        "Cust_ID": ["C1", "C2", "C3"],
        "Email": ["c1@a.com", "c2@b.com", "c3@c.com"],
        "Score": [10.5, 20.0, 30.2],
    })

    profiler = DataProfiler()
    profile1 = profiler.profile_dataframe("rep_source", df)
    profile2 = profiler.profile_dataframe("rep_source", df)

    assert profile1.fingerprint_hash == profile2.fingerprint_hash
    assert profile1.total_rows == profile2.total_rows
    assert profile1.total_columns == profile2.total_columns
    for c1, c2 in zip(profile1.columns, profile2.columns):
        assert c1.column_name == c2.column_name
        assert c1.null_count == c2.null_count
        assert c1.distinct_count == c2.distinct_count
        assert c1.samples == c2.samples


def test_save_and_reload_artifact(tmp_path: Path) -> None:
    df = pl.DataFrame({"a": [1, 2], "b": ["x", "y"]})
    profiler = DataProfiler()
    profile = profiler.profile_dataframe("save_test", df)

    artifact_path = tmp_path / "data_profile.json"
    saved_path = DataProfiler.save_artifact(profile, artifact_path)
    assert saved_path.exists()

    loaded_dict = json.loads(saved_path.read_text(encoding="utf-8"))
    reloaded_profile = DataProfile.model_validate(loaded_dict)
    assert reloaded_profile.source_id == "save_test"
    assert reloaded_profile.total_rows == 2
    assert reloaded_profile.total_columns == 2
