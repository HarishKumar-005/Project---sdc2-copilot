"""Unit tests for LazyRecordSequence in models.py (M2.3 Optimization)."""

from __future__ import annotations

from datetime import date
import polars as pl
import pytest

from src.scd2_copilot.models import (
    ChangeRecord,
    ChangeType,
    FieldChange,
    LazyRecordSequence,
)


def test_lazy_record_sequence_len_and_bool():
    df = pl.DataFrame({"id": [1, 2, 3]})
    seq = LazyRecordSequence(df, ["id"], ChangeType.NEW)
    assert len(seq) == 3
    assert bool(seq) is True

    empty_seq = LazyRecordSequence(pl.DataFrame({"id": []}), ["id"], ChangeType.NEW)
    assert len(empty_seq) == 0
    assert bool(empty_seq) is False


def test_lazy_record_sequence_indexing():
    df = pl.DataFrame({"id": [10, 20, 30]})
    seq = LazyRecordSequence(df, ["id"], ChangeType.NEW)

    # Positive indexing
    r0 = seq[0]
    assert isinstance(r0, ChangeRecord)
    assert r0.business_key_values == {"id": 10}
    assert r0.change_type == ChangeType.NEW
    assert r0.field_changes == []

    # Caching verification (same identity on repeated access)
    assert seq[0] is r0

    # Negative indexing
    r_last = seq[-1]
    assert r_last.business_key_values == {"id": 30}
    assert seq[-2].business_key_values == {"id": 20}

    # Out of bounds
    with pytest.raises(IndexError):
        _ = seq[3]
    with pytest.raises(IndexError):
        _ = seq[-4]


def test_lazy_record_sequence_slicing():
    df = pl.DataFrame({"id": [10, 20, 30, 40, 50]})
    seq = LazyRecordSequence(df, ["id"], ChangeType.UNCHANGED)

    sliced = seq[1:4]
    assert isinstance(sliced, list)
    assert len(sliced) == 3
    assert [r.business_key_values["id"] for r in sliced] == [20, 30, 40]
    assert all(r.change_type == ChangeType.UNCHANGED for r in sliced)


def test_lazy_record_sequence_iteration():
    df = pl.DataFrame({"id": [1, 2, 3]})
    seq = LazyRecordSequence(df, ["id"], ChangeType.DELETED)

    items = list(seq)
    assert len(items) == 3
    assert [r.business_key_values["id"] for r in items] == [1, 2, 3]
    assert all(r.change_type == ChangeType.DELETED for r in items)


def test_lazy_record_sequence_composite_key():
    df = pl.DataFrame({
        "org_id": [1, 2],
        "dept_id": [101, 102],
    })
    seq = LazyRecordSequence(df, ["org_id", "dept_id"], ChangeType.NEW)

    assert len(seq) == 2
    r0 = seq[0]
    assert r0.business_key_values == {"org_id": 1, "dept_id": 101}
    assert r0.change_type == ChangeType.NEW

    items = list(seq)
    assert items[1].business_key_values == {"org_id": 2, "dept_id": 102}


def test_lazy_record_sequence_changed_field_changes():
    df = pl.DataFrame({
        "customer_id": [101, 102],
        "name": ["Ravi", "Priya"],
        "city": ["Bengaluru", "Mumbai"],
        "name_target": ["Ravi", "Priya"],
        "city_target": ["Chennai", "Mumbai"],
    })
    seq = LazyRecordSequence(df, ["customer_id"], ChangeType.CHANGED, ["name", "city"])

    assert len(seq) == 2
    r0 = seq[0]
    assert r0.business_key_values == {"customer_id": 101}
    assert r0.change_type == ChangeType.CHANGED
    assert len(r0.field_changes) == 1
    assert r0.field_changes[0] == FieldChange(column="city", old_value="Chennai", new_value="Bengaluru")

    r1 = seq[1]
    assert r1.business_key_values == {"customer_id": 102}
    # Priya city was unchanged in target, so field_changes is empty
    assert r1.field_changes == []


def test_lazy_record_sequence_mutation():
    df = pl.DataFrame({"id": [1, 2]})
    seq = LazyRecordSequence(df, ["id"], ChangeType.NEW)

    extra = ChangeRecord(business_key_values={"id": 3}, change_type=ChangeType.NEW)
    seq.append(extra)
    assert len(seq) == 3
    assert seq[2] is extra
    assert seq[-1] is extra

    extra2 = ChangeRecord(business_key_values={"id": 4}, change_type=ChangeType.NEW)
    seq.extend([extra2])
    assert len(seq) == 4
    assert seq[3] is extra2

    popped = seq.pop()
    assert popped is extra2
    assert len(seq) == 3

    # Copy and to_list
    lst = seq.to_list()
    assert isinstance(lst, list)
    assert len(lst) == 3

    # Clear
    seq.clear()
    assert len(seq) == 0
    assert bool(seq) is False


def test_lazy_record_sequence_addition_and_equality():
    df1 = pl.DataFrame({"id": [1, 2]})
    df2 = pl.DataFrame({"id": [3, 4]})
    seq1 = LazyRecordSequence(df1, ["id"], ChangeType.NEW)
    seq2 = LazyRecordSequence(df2, ["id"], ChangeType.CHANGED)

    combined = seq1 + seq2
    assert isinstance(combined, list)
    assert len(combined) == 4
    assert combined[0].business_key_values == {"id": 1}
    assert combined[2].business_key_values == {"id": 3}

    # Equality against list
    list_version = list(seq1)
    assert seq1 == list_version
    assert seq1 != seq2
    assert seq1 != []
    assert LazyRecordSequence() == []


def test_lazy_record_sequence_repr():
    df = pl.DataFrame({"id": [1, 2]})
    seq = LazyRecordSequence(df, ["id"], ChangeType.NEW)
    rep = repr(seq)
    assert "ChangeRecord" in rep
    assert "id" in rep


def test_lazy_record_sequence_mutable_sequence_methods():
    """Verify count, index, reverse, remove, in (__contains__), insert, setitem, delitem."""
    df = pl.DataFrame({"id": [10, 20, 30]})
    seq = LazyRecordSequence(df, ["id"], ChangeType.NEW)

    # 1. __contains__
    r0 = seq[0]
    assert r0 in seq
    dummy = ChangeRecord(business_key_values={"id": 999}, change_type=ChangeType.NEW)
    assert dummy not in seq

    # 2. count and index
    assert seq.count(r0) == 1
    assert seq.index(r0) == 0

    # 3. insert
    new_rec = ChangeRecord(business_key_values={"id": 15}, change_type=ChangeType.NEW)
    seq.insert(1, new_rec)
    assert len(seq) == 4
    assert seq[1] is new_rec
    assert seq[0].business_key_values == {"id": 10}
    assert seq[2].business_key_values == {"id": 20}

    # 4. __setitem__
    replace_rec = ChangeRecord(business_key_values={"id": 100}, change_type=ChangeType.NEW)
    seq[0] = replace_rec
    assert seq[0] is replace_rec

    # 5. reverse
    seq.reverse()
    assert seq[-1] is replace_rec

    # 6. remove
    seq.remove(replace_rec)
    assert replace_rec not in seq
    assert len(seq) == 3

    # 7. __delitem__
    del seq[0]
    assert len(seq) == 2


def test_detect_changes_zero_eager_materialization():
    """Verify that detect_changes execution does NOT eagerly materialize ChangeRecords."""
    from src.scd2_copilot.detect_changes import detect_changes

    source = pl.DataFrame({"id": [101, 102], "val": ["A", "B_new"]})
    target = pl.DataFrame({
        "id": [101, 102, 103],
        "val": ["A", "B_old", "C"],
        "effective_from": [date(2026, 6, 1)] * 3,
        "effective_to": [None] * 3,
        "is_current": [True] * 3,
    })

    report = detect_changes(source, target, ["id"], ["val"], date(2026, 6, 8))

    # All 4 sequences must be lazy proxies with EMPTY caches and UNMATERIALIZED lists
    assert isinstance(report.new, LazyRecordSequence)
    assert isinstance(report.changed, LazyRecordSequence)
    assert isinstance(report.unchanged, LazyRecordSequence)
    assert isinstance(report.deleted, LazyRecordSequence)

    assert len(report.new._cache) == 0
    assert report.new._materialized_list is None

    assert len(report.changed._cache) == 0
    assert report.changed._materialized_list is None

    assert len(report.unchanged._cache) == 0
    assert report.unchanged._materialized_list is None

    assert len(report.deleted._cache) == 0
    assert report.deleted._materialized_list is None

    # Length and summary checks must be O(1) without populating cache
    assert len(report.changed) == 1
    assert len(report.unchanged) == 1
    assert len(report.deleted) == 1
    assert report.total == 3
    assert len(report.changed._cache) == 0

    # On-demand materialization upon explicit item access
    c0 = report.changed[0]
    assert len(report.changed._cache) == 1
    assert c0.business_key_values == {"id": 102}
    assert c0.change_type == ChangeType.CHANGED
    assert len(c0.field_changes) == 1
    assert c0.field_changes[0] == FieldChange(column="val", old_value="B_old", new_value="B_new")

