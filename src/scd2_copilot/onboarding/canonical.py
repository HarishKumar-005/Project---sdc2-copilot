"""Canonical customer schema contract (customer.v1)."""

from __future__ import annotations

from typing import Optional
from pydantic import BaseModel, Field


class CanonicalField(BaseModel):
    """Definition of a canonical target entity field."""

    name: str = Field(..., description="Canonical field identifier")
    data_type: str = Field(..., description="Target data type: string, date, datetime, enum")
    required: bool = Field(..., description="Whether this field is mandatory in canonical records")
    unique: bool = Field(default=False, description="Whether this field must be unique across the canonical batch")
    allowed_values: Optional[list[str]] = Field(default=None, description="Allowed values if field is an enum")
    description: str = Field(..., description="Authoritative semantic description of the field")


class CanonicalSchema(BaseModel):
    """Versioned canonical schema definition."""

    schema_name: str = Field(..., description="Entity name (e.g. customer)")
    version: int = Field(..., description="Schema version number")
    fields: list[CanonicalField] = Field(default_factory=list, description="Ordered canonical fields")

    def get_field(self, name: str) -> Optional[CanonicalField]:
        """Look up canonical field by name."""
        for f in self.fields:
            if f.name == name:
                return f
        return None

    def get_required_fields(self) -> list[CanonicalField]:
        """Return list of all mandatory canonical fields."""
        return [f for f in self.fields if f.required]

    @property
    def field_names(self) -> list[str]:
        """Return list of all canonical field names."""
        return [f.name for f in self.fields]


def get_canonical_customer_v1() -> CanonicalSchema:
    """Return the authoritative customer.v1 canonical schema specification."""
    return CanonicalSchema(
        schema_name="customer",
        version=1,
        fields=[
            CanonicalField(
                name="customer_id",
                data_type="string",
                required=True,
                unique=True,
                description="Stable canonical identifier for a customer. Uniquely identifies the entity.",
            ),
            CanonicalField(
                name="first_name",
                data_type="string",
                required=True,
                unique=False,
                description="Customer's given name.",
            ),
            CanonicalField(
                name="last_name",
                data_type="string",
                required=True,
                unique=False,
                description="Customer's family or surname.",
            ),
            CanonicalField(
                name="email",
                data_type="string",
                required=True,
                unique=False,
                description="Customer's primary email address.",
            ),
            CanonicalField(
                name="date_of_birth",
                data_type="date",
                required=False,
                unique=False,
                description="Customer date of birth.",
            ),
            CanonicalField(
                name="status",
                data_type="enum",
                required=True,
                unique=False,
                allowed_values=["ACTIVE", "INACTIVE"],
                description="Current canonical customer status (ACTIVE, INACTIVE).",
            ),
            CanonicalField(
                name="created_at",
                data_type="datetime",
                required=True,
                unique=False,
                description="Canonical customer record creation timestamp in UTC.",
            ),
        ],
    )


CANONICAL_CUSTOMER_V1: CanonicalSchema = get_canonical_customer_v1()
