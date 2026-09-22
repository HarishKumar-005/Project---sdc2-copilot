"""Deterministic data profiling engine using native Polars expressions."""

from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import re
from typing import Optional, Sequence
import polars as pl

from ..models.profile import (
    ColumnProfile,
    DataProfile,
    DatePatternReport,
    SamplingConfig,
    SamplingPolicy,
)
from ..models.schema_snapshot import ColumnSnapshot, map_polars_type_to_inferred
from .fingerprint import compute_schema_fingerprint, normalize_column_name
from .sampling import extract_samples, compute_top_values

# Regex for common date patterns
ISO_DATE_REGEX = re.compile(r"^\d{4}-\d{2}-\d{2}$")
ISO_DATETIME_REGEX = re.compile(r"^\d{4}-\d{2}-\d{2}[T\s]\d{2}:\d{2}:\d{2}")
SLASH_DATE_REGEX = re.compile(r"^(\d{1,2})/(\d{1,2})/(\d{4})$")
SLASH_YEAR_FIRST_REGEX = re.compile(r"^(\d{4})/(\d{1,2})/(\d{1,2})$")


def detect_date_patterns(series: pl.Series) -> list[DatePatternReport]:
    """Inspect non-null string series to detect recognized date patterns and flag ambiguity."""
    if series.dtype not in (pl.String, pl.Date, pl.Datetime):
        return []

    if series.dtype in (pl.Date, pl.Datetime):
        return [DatePatternReport(pattern="ISO_NATIVE", sample_count=series.drop_nulls().len(), is_ambiguous=False)]

    non_null_strings = series.drop_nulls().head(100).to_list()
    if not non_null_strings:
        return []

    pattern_counts: dict[str, int] = {}
    ambiguous_flags: dict[str, bool] = {}

    for val in non_null_strings:
        s = str(val).strip()
        if ISO_DATETIME_REGEX.match(s):
            pattern_counts["%Y-%m-%dT%H:%M:%SZ"] = pattern_counts.get("%Y-%m-%dT%H:%M:%SZ", 0) + 1
            ambiguous_flags["%Y-%m-%dT%H:%M:%SZ"] = False
        elif ISO_DATE_REGEX.match(s):
            pattern_counts["%Y-%m-%d"] = pattern_counts.get("%Y-%m-%d", 0) + 1
            ambiguous_flags["%Y-%m-%d"] = False
        elif match := SLASH_DATE_REGEX.match(s):
            p1, p2, yr = int(match.group(1)), int(match.group(2)), match.group(3)
            # If both p1 <= 12 and p2 <= 12, ambiguous between %d/%m/%Y and %m/%d/%Y
            is_ambig = (p1 <= 12 and p2 <= 12 and p1 != p2)
            pat = "%d/%m/%Y or %m/%d/%Y" if is_ambig else ("%d/%m/%Y" if p1 > 12 else "%m/%d/%Y")
            pattern_counts[pat] = pattern_counts.get(pat, 0) + 1
            ambiguous_flags[pat] = is_ambig or ambiguous_flags.get(pat, False)
        elif SLASH_YEAR_FIRST_REGEX.match(s):
            pattern_counts["%Y/%m/%d"] = pattern_counts.get("%Y/%m/%d", 0) + 1
            ambiguous_flags["%Y/%m/%d"] = False

    reports: list[DatePatternReport] = []
    for pat, cnt in pattern_counts.items():
        reports.append(
            DatePatternReport(
                pattern=pat,
                sample_count=cnt,
                is_ambiguous=ambiguous_flags.get(pat, False),
            )
        )
    return reports


class DataProfiler:
    """Calculates deterministic statistical profiles for untrusted source datasets."""

    def __init__(self, sampling_config: Optional[SamplingConfig] = None) -> None:
        self.sampling_config = sampling_config or SamplingConfig()

    def profile_dataframe(
        self,
        source_id: str,
        df: pl.DataFrame,
        fingerprint_hash: Optional[str] = None,
    ) -> DataProfile:
        """Generate a complete DataProfile for the given Polars DataFrame."""
        total_rows = df.height
        total_cols = df.width

        # If fingerprint hash is not supplied, compute schema snapshot and hash deterministically
        if fingerprint_hash is None:
            snapshots = []
            for idx, (col_name, dtype) in enumerate(df.schema.items()):
                series = df[col_name]
                snapshots.append(
                    ColumnSnapshot(
                        original_name=col_name,
                        normalized_name=normalize_column_name(col_name),
                        inferred_type=map_polars_type_to_inferred(dtype),
                        polars_type=str(dtype),
                        nullable=series.null_count() > 0,
                        ordinal_position=idx,
                    )
                )
            fingerprint = compute_schema_fingerprint(snapshots)
            resolved_hash = fingerprint.fingerprint_hash
        else:
            resolved_hash = fingerprint_hash

        col_profiles: list[ColumnProfile] = []

        for col_name, dtype in df.schema.items():
            series = df[col_name]
            normalized = normalize_column_name(col_name)
            inferred = map_polars_type_to_inferred(dtype)

            null_count = series.null_count()
            null_rate = round(null_count / total_rows, 4) if total_rows > 0 else 0.0
            non_null_count = total_rows - null_count
            distinct_count = series.drop_nulls().n_unique()

            uniqueness_rate = (
                round(distinct_count / non_null_count, 4) if non_null_count > 0 else 0.0
            )
            is_unique = (distinct_count == non_null_count and non_null_count > 0)

            # String length metrics
            min_len: Optional[int] = None
            max_len: Optional[int] = None
            if dtype == pl.String and non_null_count > 0:
                lens = series.drop_nulls().str.len_chars()
                min_len = int(lens.min()) if lens.len() > 0 else None  # type: ignore
                max_len = int(lens.max()) if lens.len() > 0 else None  # type: ignore

            # Samples and value distributions
            samples = extract_samples(series, col_name, normalized, self.sampling_config)
            top_values = compute_top_values(
                series,
                top_k=self.sampling_config.top_k_frequent,
                max_distinct_threshold=25,
                normalized_name=normalized,
                policy=self.sampling_config.policy,
            )

            # Date pattern detection
            date_patterns = detect_date_patterns(series)

            col_profiles.append(
                ColumnProfile(
                    column_name=col_name,
                    normalized_name=normalized,
                    inferred_type=inferred,
                    polars_type=str(dtype),
                    total_count=total_rows,
                    null_count=null_count,
                    null_rate=null_rate,
                    distinct_count=distinct_count,
                    uniqueness_rate=uniqueness_rate,
                    is_unique=is_unique,
                    min_length=min_len,
                    max_length=max_len,
                    samples=samples,
                    top_values=top_values,
                    date_patterns=date_patterns,
                )
            )

        return DataProfile(
            source_id=source_id,
            fingerprint_hash=resolved_hash,
            total_rows=total_rows,
            total_columns=total_cols,
            columns=col_profiles,
            profiled_at=datetime.now(timezone.utc),
            sampling_policy=self.sampling_config.policy,
        )

    @staticmethod
    def save_artifact(profile: DataProfile, path: Path | str) -> Path:
        """Serialize profile to JSON artifact with deterministic formatting."""
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        content = profile.model_dump_json(indent=2)
        p.write_text(content, encoding="utf-8")
        return p
