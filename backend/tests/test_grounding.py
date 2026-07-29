"""Grounding tests: the check that decides whether a figure may be shown.

These are pure functions over strings, so they need no database and no model.
That is the point -- the guarantee lives somewhere it can be tested exactly.
"""

from __future__ import annotations

import pytest

from app.services import grounding


# ---------------------------------------------------------------------------
# Extraction
# ---------------------------------------------------------------------------


def test_currency_symbols_and_separators_do_not_hide_a_number():
    """A check that only recognized bare digits would miss "$1,234.56" --
    which is the exact form every fabricated amount would take."""
    found = grounding.extract_numbers("You spent $1,234.56 and saved 12%.")

    assert found == ["1,234.56", "12"]


def test_text_with_no_numbers_is_trivially_fine():
    assert grounding.verify("You have no transactions yet.", set()).ok


# ---------------------------------------------------------------------------
# Normalization
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("written", "returned"),
    [
        ("$1,234.50", "1234.5"),
        ("1234.500", "1234.5"),
        ("100", "100.00"),
        ("0.5", "0.50"),
    ],
)
def test_formatting_differences_do_not_cause_false_rejections(written, returned):
    """The tool returns "1234.50" and the model writes "$1,234.50". Those are
    the same number, and rejecting the answer over a comma would make the
    check useless in practice -- everyone would turn it off."""
    supported = grounding.collect_supported_numbers({"total": returned})

    assert grounding.verify(f"You spent {written}.", supported).ok


# ---------------------------------------------------------------------------
# The rejections that matter
# ---------------------------------------------------------------------------


def test_an_invented_figure_is_rejected():
    supported = grounding.collect_supported_numbers({"total": "412.00"})

    verdict = grounding.verify("You spent $980.00 on groceries.", supported)

    assert not verdict.ok
    assert verdict.unsupported == ("980.00",)


def test_arithmetic_the_model_did_itself_is_rejected():
    """Two real figures, one invented sum. This is the subtle case: every
    input is genuine, so nothing about the sentence looks wrong -- and the
    sum is still a number no query produced and nobody checked."""
    supported = grounding.collect_supported_numbers(
        {"groceries": "300.00", "dining": "200.00"}
    )

    verdict = grounding.verify("Together that is $500.00 on food.", supported)

    assert not verdict.ok
    assert verdict.unsupported == ("500.00",)


def test_rounding_is_rejected_even_though_a_human_would_allow_it():
    """Documents a deliberate trade-off rather than an oversight.

    "About $1,200" for $1,234.56 is honest. Permitting it means choosing a
    tolerance, and any tolerance is room a genuinely wrong number can hide
    inside. Exactness is cheap for the model to comply with; a threshold is
    not cheap to get right.
    """
    supported = grounding.collect_supported_numbers({"total": "1234.56"})

    assert not grounding.verify("You spent about $1,200.", supported).ok


def test_every_unsupported_figure_is_reported_not_just_the_first():
    supported = grounding.collect_supported_numbers({"total": "10.00"})

    verdict = grounding.verify("$20 here, $30 there, $10 correct.", supported)

    assert verdict.unsupported == ("20", "30")


def test_a_figure_the_user_supplied_is_allowed():
    """"Show restaurants over $100" -- the model echoing $100 is quoting the
    question, not inventing a fact."""
    supported = grounding.collect_supported_numbers("show restaurants over $100")

    assert grounding.verify("Here are restaurant charges over $100.", supported).ok


def test_any_figure_in_a_tool_result_is_quotable_not_just_the_headline():
    """The supported set is harvested from the whole result payload, so
    adding a field to a tool cannot silently make it unquotable. A grounding
    check that needs updating whenever a tool changes is one that will
    eventually be out of date."""
    supported = grounding.collect_supported_numbers(
        {"categories": [{"category": "Groceries", "total": "412.00", "transaction_count": 17}]}
    )

    assert grounding.verify("17 grocery purchases totalling $412.00.", supported).ok


def test_dates_from_a_tool_result_are_supported():
    supported = grounding.collect_supported_numbers({"start": "2026-07-01"})

    assert grounding.verify("Since 2026-07-01.", supported).ok


def test_a_message_names_the_offending_figures():
    supported = grounding.collect_supported_numbers({})

    message = grounding.verify("You spent $77.", supported).message

    assert "77" in message
