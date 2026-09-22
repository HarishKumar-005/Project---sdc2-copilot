"""Tests for the canonical customer schema contract (customer.v1)."""

from scd2_copilot.onboarding.canonical import CanonicalSchema, get_canonical_customer_v1


def test_canonical_customer_v1_contract() -> None:
    schema = get_canonical_customer_v1()
    assert isinstance(schema, CanonicalSchema)
    assert schema.schema_name == "customer"
    assert schema.version == 1
    assert len(schema.fields) == 7

    # Check field lookup
    cust_id = schema.get_field("customer_id")
    assert cust_id is not None
    assert cust_id.data_type == "string"
    assert cust_id.required is True
    assert cust_id.unique is True

    email = schema.get_field("email")
    assert email is not None
    assert email.required is True
    assert email.unique is False

    status = schema.get_field("status")
    assert status is not None
    assert status.data_type == "enum"
    assert status.allowed_values == ["ACTIVE", "INACTIVE"]

    dob = schema.get_field("date_of_birth")
    assert dob is not None
    assert dob.required is False

    # Check required fields
    required = schema.get_required_fields()
    required_names = {f.name for f in required}
    assert required_names == {
        "customer_id",
        "first_name",
        "last_name",
        "email",
        "status",
        "created_at",
    }
