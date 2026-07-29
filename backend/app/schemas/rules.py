"""Validation for rule conditions and actions.

===========================================================================
Why this file exists
===========================================================================

Milestone 2 stored rules as JSONB because conditions nest and their shape
varies. The honest trade-off stated at the time was: **the database can no
longer validate what is inside that JSON.** This module is where that
validation moves to.

Without it, a malformed rule is only discovered when the engine runs -- which
is during a sync, in a background job, against real data, where the failure is
invisible and the user has no idea their rule never worked. Validating at the
API boundary means a bad rule is rejected at the moment it is written, with a
message naming the problem.

That is the general principle: **when you give up a database guarantee, you
owe an equivalent guarantee somewhere else.**
"""

from __future__ import annotations

import uuid
from decimal import Decimal
from typing import Annotated, Literal, Union

from pydantic import BaseModel, ConfigDict, Field, field_validator

# Fields a condition may test. An allowlist, not free text: it stops a rule
# from probing columns that are none of its business (another user's id, an
# encrypted token) and keeps the evaluator's surface small enough to reason
# about.
TEXT_FIELDS = ("description", "raw_name", "merchant_name", "notes")
NUMERIC_FIELDS = ("amount",)
ID_FIELDS = ("account_id", "category_id", "merchant_id")

# Regex is deliberately NOT an operator here.
#
# A user-supplied pattern like (a+)+$ backtracks catastrophically -- one rule
# could hang the sync engine for every user on the server. That is a denial of
# service with no attacker required, just an unlucky pattern. The operators
# below cover every realistic categorization need without the risk.
TEXT_OPERATORS = (
    "contains",
    "not_contains",
    "equals",
    "not_equals",
    "starts_with",
    "ends_with",
    "is_empty",
    "is_not_empty",
)
NUMERIC_OPERATORS = ("eq", "ne", "gt", "gte", "lt", "lte")
ID_OPERATORS = ("is", "is_not")

MAX_CONDITIONS = 25
MAX_NESTING_DEPTH = 3
MAX_VALUE_LENGTH = 500


class TextCondition(BaseModel):
    model_config = ConfigDict(extra="forbid")

    field: Literal["description", "raw_name", "merchant_name", "notes"]
    op: Literal[
        "contains",
        "not_contains",
        "equals",
        "not_equals",
        "starts_with",
        "ends_with",
        "is_empty",
        "is_not_empty",
    ]
    value: str = ""
    # Text matching is case-insensitive by default: nobody writing a rule for
    # "starbucks" means it to miss "STARBUCKS".
    case_sensitive: bool = False

    @field_validator("value")
    @classmethod
    def _bounded(cls, value: str) -> str:
        if len(value) > MAX_VALUE_LENGTH:
            raise ValueError(f"value must be at most {MAX_VALUE_LENGTH} characters")
        return value


class NumericCondition(BaseModel):
    model_config = ConfigDict(extra="forbid")

    field: Literal["amount"]
    op: Literal["eq", "ne", "gt", "gte", "lt", "lte"]
    value: Decimal


class IdCondition(BaseModel):
    model_config = ConfigDict(extra="forbid")

    field: Literal["account_id", "category_id", "merchant_id"]
    op: Literal["is", "is_not"]
    value: uuid.UUID


Condition = Annotated[
    Union[TextCondition, NumericCondition, IdCondition],
    Field(discriminator="field"),
]


class ConditionGroup(BaseModel):
    """A boolean group. Nests, so "A and (B or C)" is expressible."""

    model_config = ConfigDict(extra="forbid")

    operator: Literal["AND", "OR"] = "AND"
    conditions: list[Union[Condition, "ConditionGroup"]] = Field(min_length=1)

    @field_validator("conditions")
    @classmethod
    def _bounded(cls, conditions: list) -> list:
        if len(conditions) > MAX_CONDITIONS:
            raise ValueError(f"at most {MAX_CONDITIONS} conditions per group")
        return conditions

    def depth(self) -> int:
        nested = [c.depth() for c in self.conditions if isinstance(c, ConditionGroup)]
        return 1 + max(nested, default=0)


ConditionGroup.model_rebuild()


class RuleActions(BaseModel):
    """What a matching rule does.

    Note what a rule CANNOT do: change an amount, a date, or which account a
    transaction belongs to. Rules organize; they never rewrite financial
    facts. Allowing that would let one bad rule silently corrupt a year of
    history with no audit trail pointing at a human.
    """

    model_config = ConfigDict(extra="forbid")

    set_category_id: uuid.UUID | None = None
    set_merchant_id: uuid.UUID | None = None
    add_tag_ids: list[uuid.UUID] = Field(default_factory=list, max_length=20)
    set_notes: str | None = Field(default=None, max_length=1000)
    mark_reviewed: bool = False
    hide: bool = False

    @field_validator("add_tag_ids")
    @classmethod
    def _unique(cls, tag_ids: list[uuid.UUID]) -> list[uuid.UUID]:
        return list(dict.fromkeys(tag_ids))

    def is_empty(self) -> bool:
        return not any(
            [
                self.set_category_id,
                self.set_merchant_id,
                self.add_tag_ids,
                self.set_notes,
                self.mark_reviewed,
                self.hide,
            ]
        )


def validate_conditions(payload: dict) -> ConditionGroup:
    """Parse and bound-check a stored or incoming condition tree."""
    group = ConditionGroup.model_validate(payload)

    if group.depth() > MAX_NESTING_DEPTH:
        raise ValueError(f"conditions may nest at most {MAX_NESTING_DEPTH} deep")

    return group


def validate_actions(payload: dict) -> RuleActions:
    actions = RuleActions.model_validate(payload)

    if actions.is_empty():
        # A rule that matches and then does nothing is always a mistake, and
        # a silent one -- it looks like it is working.
        raise ValueError("a rule must do something")

    return actions

