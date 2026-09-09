"""Regression tests for M1.2: Robust Datetime Normalization.

Verifies that:
1. Valid ISO dates and timestamps do not silently turn into NULL.
2. Already-inferred pl.Date and pl.Datetime types are preserved/converted losslessly.
3. Invalid temporal strings fail explicitly with InvalidTemporalValueError.
4. Downstream temporal expectations (pl.Date) are maintained consistently.
"""

from __future__ import annotations

from datetime import date, datetime
import io

import polars as pl
import pytest

from src.scd2_copilot.exceptions import InvalidTemporalValueError, SCD2Error
from src.scd2_copilot.ingestion import _normalize_scd2_columns, load_csv


class TestDatetimeNormalization:
    """Test suite for datetime normalization in SCD2 metadata columns."""

    def test_01_date_only_csv_input(self):
        """Date-only strings (YYYY-MM-DD) are normalized to pl.Date."""
        csv = b"customer_id,name,effective_from,effective_to,is_current\n101,Alice,2026-09-06,,true\n"
        df = load_csv(io.BytesIO(csv))
        assert df["effective_from"].dtype == pl.Date
        assert df["effective_from"][0] == date(2026, 9, 6)
        assert df["effective_to"].dtype == pl.Date
        assert df["effective_to"][0] is None

    def test_02_iso_datetime_space_input(self):
        """ISO datetime with space separator (YYYY-MM-DD HH:MM:SS) normalizes to pl.Date."""
        csv = b"customer_id,name,effective_from,effective_to,is_current\n101,Alice,2026-09-06 14:30:00,2026-09-07 10:15:00,false\n"
        df = load_csv(io.BytesIO(csv))
        assert df["effective_from"].dtype == pl.Date
        assert df["effective_from"][0] == date(2026, 9, 6)
        assert df["effective_to"].dtype == pl.Date
        assert df["effective_to"][0] == date(2026, 9, 7)

    def test_03_iso_datetime_t_separator(self):
        """ISO datetime with 'T' separator (YYYY-MM-DDTHH:MM:SS) normalizes to pl.Date."""
        csv = b"customer_id,name,effective_from,effective_to,is_current\n101,Alice,2026-09-06T14:30:00,,true\n"
        df = load_csv(io.BytesIO(csv))
        assert df["effective_from"].dtype == pl.Date
        assert df["effective_from"][0] == date(2026, 9, 6)

    def test_04_iso_datetime_fractional_seconds(self):
        """ISO datetime with fractional seconds normalizes to pl.Date."""
        csv = (
            b"customer_id,name,effective_from,effective_to,is_current\n"
            b"101,Alice,2026-09-06 14:30:00.123456,2026-09-07T10:00:00.789,false\n"
        )
        df = load_csv(io.BytesIO(csv))
        assert df["effective_from"].dtype == pl.Date
        assert df["effective_from"][0] == date(2026, 9, 6)
        assert df["effective_to"].dtype == pl.Date
        assert df["effective_to"][0] == date(2026, 9, 7)

    def test_05_iso_datetime_with_timezone(self):
        """ISO-8601 timestamps with 'Z' and offset timezone extract calendar date."""
        csv = (
            b"customer_id,name,effective_from,effective_to,is_current\n"
            b"101,Alice,2026-09-06T14:30:00Z,2026-09-07T10:00:00+02:00,false\n"
        )
        df = load_csv(io.BytesIO(csv))
        assert df["effective_from"].dtype == pl.Date
        assert df["effective_from"][0] == date(2026, 9, 6)
        assert df["effective_to"].dtype == pl.Date
        assert df["effective_to"][0] == date(2026, 9, 7)

    def test_06_already_inferred_pl_date_preserved(self):
        """Input with column already typed as pl.Date is preserved without mutation."""
        df_in = pl.DataFrame({
            "customer_id": [101],
            "effective_from": [date(2026, 6, 1)],
            "effective_to": [None],
            "is_current": [True],
        })
        df_out = _normalize_scd2_columns(df_in)
        assert df_out["effective_from"].dtype == pl.Date
        assert df_out["effective_from"][0] == date(2026, 6, 1)
        assert df_out["effective_to"].dtype == pl.Date
        assert df_out["effective_to"][0] is None

    def test_07_already_inferred_pl_datetime_converted_losslessly(self):
        """Input with column already typed as pl.Datetime converts to pl.Date without NULLs."""
        dt_val = datetime(2026, 6, 8, 14, 30, 0)
        df_in = pl.DataFrame({
            "customer_id": [101, 102],
            "effective_from": [dt_val, datetime(2026, 6, 9, 9, 0, 0)],
            "effective_to": [None, dt_val],
            "is_current": [True, False],
        })
        assert isinstance(df_in["effective_from"].dtype, pl.Datetime)
        df_out = _normalize_scd2_columns(df_in)
        assert df_out["effective_from"].dtype == pl.Date
        assert df_out["effective_from"].to_list() == [date(2026, 6, 8), date(2026, 6, 9)]
        assert df_out["effective_to"].dtype == pl.Date
        assert df_out["effective_to"].to_list() == [None, date(2026, 6, 8)]

    def test_08_invalid_date_string_fails_explicitly(self):
        """Invalid date strings (e.g. non-existent calendar date) raise InvalidTemporalValueError."""
        csv = b"customer_id,name,effective_from,effective_to,is_current\n101,Alice,2026-02-31,,true\n"
        with pytest.raises(InvalidTemporalValueError) as exc_info:
            load_csv(io.BytesIO(csv), dataset_name="target")

        err = exc_info.value
        assert issubclass(type(err), SCD2Error)
        assert issubclass(type(err), ValueError)
        assert err.column == "effective_from"
        assert "2026-02-31" in err.invalid_samples[0]
        assert "target" in str(err)

    def test_09_invalid_datetime_string_fails_explicitly(self):
        """Invalid datetime strings (invalid hour or garbage time) raise InvalidTemporalValueError."""
        csv = b"customer_id,name,effective_from,effective_to,is_current\n101,Alice,2026-09-06 25:00:00,,true\n"
        with pytest.raises(InvalidTemporalValueError) as exc_info:
            load_csv(io.BytesIO(csv))

        err = exc_info.value
        assert err.column == "effective_from"
        assert "25:00:00" in err.invalid_samples[0]

        # Test garbage non-temporal string
        csv_garbage = b"customer_id,name,effective_from,effective_to,is_current\n101,Alice,not-a-date,,true\n"
        with pytest.raises(InvalidTemporalValueError) as exc_info2:
            load_csv(io.BytesIO(csv_garbage))
        assert exc_info2.value.column == "effective_from"
        assert "not-a-date" in exc_info2.value.invalid_samples[0]

    def test_10_mixed_valid_and_invalid_fails_without_silent_corruption(self):
        """Column with mixed valid and invalid entries fails completely without partial NULL conversion."""
        csv = (
            b"customer_id,name,effective_from,effective_to,is_current\n"
            b"101,Alice,2026-09-06,,false\n"
            b"102,Bob,corrupt_date,,false\n"
            b"103,Charlie,2026-09-08,,true\n"
        )
        with pytest.raises(InvalidTemporalValueError) as exc_info:
            load_csv(io.BytesIO(csv))

        err = exc_info.value
        assert err.column == "effective_from"
        assert "corrupt_date" in err.invalid_samples[0]

    def test_11_null_and_empty_values_preserved_in_effective_to(self):
        """Null and empty values in effective_to remain None with pl.Date dtype."""
        csv = (
            b"customer_id,name,effective_from,effective_to,is_current\n"
            b"101,Alice,2026-09-06,,true\n"
            b"102,Bob,2026-09-06,   ,true\n"
            b"103,Charlie,2026-09-06,2026-09-07,false\n"
        )
        df = load_csv(io.BytesIO(csv))
        assert df["effective_to"].dtype == pl.Date
        assert df["effective_to"].to_list() == [None, None, date(2026, 9, 7)]

    def test_12_both_effective_from_and_effective_to_normalized(self):
        """Invalid values in effective_to raise InvalidTemporalValueError identifying column."""
        csv = (
            b"customer_id,name,effective_from,effective_to,is_current\n"
            b"101,Alice,2026-09-06,bad-to-date,false\n"
        )
        with pytest.raises(InvalidTemporalValueError) as exc_info:
            load_csv(io.BytesIO(csv))

        err = exc_info.value
        assert err.column == "effective_to"
        assert "bad-to-date" in err.invalid_samples[0]

    def test_13_regression_iso_timestamp_silent_null_eliminated(self):
        """Reproduction of exact defect: '2026-06-08 14:30:00' must NOT become NULL.

        Previously, Polars read_csv inferred Datetime, which was then cast to Utf8
        and parsed with format='%Y-%m-%d', strict=False, turning valid timestamps into None.
        """
        csv = (
            b"customer_id,name,city,tier,effective_from,effective_to,is_current\n"
            b"101,Ravi,Chennai,Gold,2026-06-08 14:30:00,,true\n"
        )
        df = load_csv(io.BytesIO(csv))
        assert df["effective_from"][0] is not None
        assert df["effective_from"][0] == date(2026, 6, 8)
        assert df["effective_from"].dtype == pl.Date

    def test_14_non_temporal_type_column_raises_error(self):
        """Non-temporal unparseable types (like integers) raise InvalidTemporalValueError."""
        df_in = pl.DataFrame({
            "customer_id": [101],
            "effective_from": [20260608],
            "is_current": [True],
        })
        with pytest.raises(InvalidTemporalValueError) as exc_info:
            _normalize_scd2_columns(df_in)
        assert exc_info.value.column == "effective_from"
