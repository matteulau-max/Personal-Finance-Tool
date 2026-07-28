"""Schemas for categories, merchants, tags, and rules."""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.schemas.rules import validate_actions, validate_conditions

# ---------------------------------------------------------------------------
# Categories
# ---------------------------------------------------------------------------


class CategoryResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    slug: str | None
    parent_id: uuid.UUID | None
    icon: str | None
    color: str | None
    is_income: bool
    is_transfer: bool
    is_system: bool
    sort_order: int


class CategoryCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=128)
    parent_id: uuid.UUID | None = None
    icon: str | None = Field(default=None, max_length=64)
    color: str | None = Field(default=None, max_length=16)
    is_income: bool = False
    is_transfer: bool = False


# ---------------------------------------------------------------------------
# Merchants
# ---------------------------------------------------------------------------


class MerchantResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    display_name: str
    normalized_name: str
    logo_url: str | None
    website: str | None
    is_subscription: bool
    default_category_id: uuid.UUID | None
    # True when this is the user's own override rather than the shared row.
    is_personal: bool = False


class MerchantUpdateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    display_name: str | None = Field(default=None, min_length=1, max_length=255)
    default_category_id: uuid.UUID | None = None
    is_subscription: bool | None = None


# ---------------------------------------------------------------------------
# Tags
# ---------------------------------------------------------------------------


class TagResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    color: str | None
    description: str | None
    created_at: datetime


class TagCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=64)
    color: str | None = Field(default=None, max_length=16)
    description: str | None = Field(default=None, max_length=255)

    @field_validator("name")
    @classmethod
    def _trim(cls, name: str) -> str:
        """Trim before storing.

        " Vacation" and "Vacation" are the same tag to a human, and a user who
        accidentally leaves a leading space would otherwise end up with two
        tags that look identical in the UI and split their reports in half.
        """
        cleaned = name.strip()
        if not cleaned:
            raise ValueError("name cannot be blank")
        return cleaned


class TagUpdateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, min_length=1, max_length=64)
    color: str | None = Field(default=None, max_length=16)
    description: str | None = Field(default=None, max_length=255)


# ---------------------------------------------------------------------------
# Rules
# ---------------------------------------------------------------------------


class RuleResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    description: str | None
    priority: int
    is_active: bool
    stop_processing: bool
    conditions: dict
    actions: dict
    match_count: int
    last_matched_at: datetime | None
    created_at: datetime


class RuleCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=255)
    description: str | None = Field(default=None, max_length=512)
    priority: int = Field(default=100, ge=0, le=10_000)
    is_active: bool = True
    stop_processing: bool = False
    conditions: dict
    actions: dict

    @field_validator("conditions")
    @classmethod
    def _valid_conditions(cls, value: dict) -> dict:
        # Validate at the API boundary, not when the engine eventually runs.
        # A rule rejected here produces a clear error the user can act on; a
        # rule that fails during a background sync fails invisibly.
        validate_conditions(value)
        return value

    @field_validator("actions")
    @classmethod
    def _valid_actions(cls, value: dict) -> dict:
        validate_actions(value)
        return value


class RuleUpdateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, min_length=1, max_length=255)
    description: str | None = Field(default=None, max_length=512)
    priority: int | None = Field(default=None, ge=0, le=10_000)
    is_active: bool | None = None
    stop_processing: bool | None = None
    conditions: dict | None = None
    actions: dict | None = None

    @field_validator("conditions")
    @classmethod
    def _valid_conditions(cls, value: dict | None) -> dict | None:
        if value is not None:
            validate_conditions(value)
        return value

    @field_validator("actions")
    @classmethod
    def _valid_actions(cls, value: dict | None) -> dict | None:
        if value is not None:
            validate_actions(value)
        return value


class RulePreviewResponse(BaseModel):
    """What a rule WOULD do, without doing it.

    Applying an untested rule to three years of history is a frightening
    button to press. Showing the match count first turns it into an informed
    decision -- and a rule that matches 4,000 transactions when you expected
    12 is obviously wrong before it has touched anything.
    """

    matched: int
    sample: list[str]


class RuleApplyResponse(BaseModel):
    enriched: int
