"""Merchant normalization and learning tests."""

from __future__ import annotations

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import Merchant, User
from app.services.merchants import (
    learn_alias,
    normalize_descriptor,
    resolve_merchant,
)

# ---------------------------------------------------------------------------
# Normalization
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw",
    [
        "Whole Foods #125",
        "WHOLE FOODS 3421 SEATTLE WA",
        "SQ *WHOLE FOODS",
        "whole foods  ",
    ],
)
def test_noise_is_stripped_to_a_shared_key(raw: str):
    """Descriptors that differ only by NOISE collapse to one key."""
    assert normalize_descriptor(raw) == "whole foods"


def test_normalization_cannot_resolve_abbreviations(db: Session, user: User):
    """An honest test of the limitation, so nobody assumes otherwise.

    "WHOLEFDS MKT" and "WHOLE FOODS" are the same shop, but no amount of
    string cleanup can know that -- it is an abbreviation, not noise.
    Normalization alone therefore produces two different keys.

    This is not a bug to be fixed with a cleverer regex. It is why the two
    stronger layers exist, and the next two tests show them working.
    """
    assert normalize_descriptor("WHOLEFDS MKT #10259") != normalize_descriptor(
        "WHOLE FOODS"
    )


def test_plaids_merchant_name_unifies_what_normalization_cannot(
    db: Session, user: User
):
    """Layer 2, and the reason this works in production.

    Plaid resolves descriptors against its own merchant database, so wildly
    different raw strings arrive with the same `merchant_name`. Keying on that
    is what makes "how much do I spend at Whole Foods?" have one answer.
    """
    first = resolve_merchant(
        db,
        user,
        raw_name="WHOLEFDS MKT #10259",
        plaid_merchant_name="Whole Foods Market",
    )
    second = resolve_merchant(
        db,
        user,
        raw_name="SQ *WHOLE FOODS CAPITOL HILL",
        plaid_merchant_name="Whole Foods Market",
    )
    db.flush()

    assert first.id == second.id
    assert (
        db.execute(
            select(func.count())
            .select_from(Merchant)
            .where(Merchant.normalized_name == "whole foods market")
        ).scalar_one()
        == 1
    )


def test_processor_prefixes_are_stripped():
    assert normalize_descriptor("SQ *BLUE BOTTLE") == "blue bottle"
    assert normalize_descriptor("TST* CHIPOTLE") == "chipotle"
    assert normalize_descriptor("PAYPAL *SPOTIFY") == "spotify"


def test_store_numbers_are_stripped():
    assert normalize_descriptor("TARGET #1234") == "target"
    assert normalize_descriptor("CVS STORE 5678") == "cvs"


def test_trailing_city_and_state_are_stripped():
    assert normalize_descriptor("STARBUCKS SEATTLE WA") == "starbucks"


def test_a_state_code_is_kept_when_not_trailing():
    """"washington post" must keep its "washington".

    Stripping any word that happens to look like a state code would break
    real merchant names. Only a TRAILING code is treated as a location.
    """
    assert normalize_descriptor("WASHINGTON POST SUBSCRIPTION") == "washington post subscription"


def test_dates_and_reference_numbers_are_stripped():
    assert normalize_descriptor("AMAZON MKTPL 03/14 REF 998877123") == "amazon mktpl ref"


def test_accents_are_folded():
    """"CAFÉ" and "CAFE" are the same merchant to a human."""
    assert normalize_descriptor("CAFÉ ROUGE") == normalize_descriptor("CAFE ROUGE")


def test_punctuation_becomes_spaces_not_nothing():
    """"AMAZON.COM" must not become the single token "amazoncom"."""
    assert normalize_descriptor("AMAZON.COM") == "amazon com"


def test_empty_and_useless_inputs_return_empty():
    assert normalize_descriptor("") == ""
    assert normalize_descriptor("   ") == ""
    assert normalize_descriptor("###") == ""


# ---------------------------------------------------------------------------
# Resolution
# ---------------------------------------------------------------------------


def test_resolution_creates_one_merchant_for_many_spellings(
    db: Session, user: User
):
    """The whole point: five descriptors, one merchant."""
    for raw in (
        "WHOLEFDS MKT #10259",
        "WHOLEFDS MKT #99",
        "WHOLEFDS MKT",
    ):
        resolve_merchant(db, user, raw_name=raw)
    db.flush()

    count = db.execute(
        select(func.count()).select_from(Merchant).where(
            Merchant.normalized_name == "wholefds mkt"
        )
    ).scalar_one()
    assert count == 1


def test_plaids_merchant_name_is_preferred_for_the_display_name(
    db: Session, user: User
):
    """Plaid has far more data than a regular expression does."""
    merchant = resolve_merchant(
        db,
        user,
        raw_name="WHOLEFDS MKT #10259",
        plaid_merchant_name="Whole Foods Market",
    )
    assert merchant.display_name == "Whole Foods Market"


def test_display_name_falls_back_to_title_case(db: Session, user: User):
    merchant = resolve_merchant(db, user, raw_name="WHOLEFDS MKT #10259")
    assert merchant.display_name == "Wholefds Mkt"


def test_an_unusable_descriptor_yields_no_merchant(db: Session, user: User):
    """Better no merchant than one called "" that everything matches."""
    assert resolve_merchant(db, user, raw_name="###") is None


def test_a_users_own_merchant_beats_the_global_one(db: Session, user: User):
    global_merchant = Merchant(
        user_id=None, normalized_name="starbucks", display_name="Starbucks"
    )
    personal = Merchant(
        user_id=user.id, normalized_name="starbucks", display_name="My Coffee Place"
    )
    db.add_all([global_merchant, personal])
    db.flush()

    resolved = resolve_merchant(db, user, raw_name="STARBUCKS #123")

    assert resolved.id == personal.id


# ---------------------------------------------------------------------------
# Learning
# ---------------------------------------------------------------------------


def test_an_alias_overrides_normalization(db: Session, user: User):
    """The system gets smarter through use.

    Normalization cannot know that "SQ *TBC 4821" means The Bike Co-op. The
    user corrects it once, and every future transaction with that descriptor
    resolves correctly -- including ones not yet imported.
    """
    bike_shop = Merchant(
        user_id=None, normalized_name="the bike coop", display_name="The Bike Co-op"
    )
    db.add(bike_shop)
    db.flush()

    # Before learning, the mangled descriptor resolves to something else.
    first = resolve_merchant(db, user, raw_name="SQ *TBC 4821")
    assert first.id != bike_shop.id

    learn_alias(db, user, raw_name="SQ *TBC 4821", merchant=bike_shop)

    # A DIFFERENT transaction with the same descriptor now resolves correctly.
    after = resolve_merchant(db, user, raw_name="SQ *TBC 4821")
    assert after.id == bike_shop.id


def test_an_alias_is_scoped_to_the_user_who_taught_it(
    db: Session, user: User, other_user
):
    """One person's correction is not evidence for everybody.

    Two users can legitimately disagree about what a shared descriptor means.
    A global write would let one silently change the other's data.
    """
    stranger, _ = other_user

    target = Merchant(user_id=None, normalized_name="target", display_name="Target")
    db.add(target)
    db.flush()

    learn_alias(db, user, raw_name="TGT *STORE 99", merchant=target)

    mine = resolve_merchant(db, user, raw_name="TGT *STORE 99")
    theirs = resolve_merchant(db, stranger, raw_name="TGT *STORE 99")

    assert mine.id == target.id
    assert theirs.id != target.id


def test_relearning_updates_the_existing_alias(db: Session, user: User):
    first = Merchant(user_id=None, normalized_name="first", display_name="First")
    second = Merchant(user_id=None, normalized_name="second", display_name="Second")
    db.add_all([first, second])
    db.flush()

    learn_alias(db, user, raw_name="AMBIGUOUS CO", merchant=first)
    learn_alias(db, user, raw_name="AMBIGUOUS CO", merchant=second)

    assert resolve_merchant(db, user, raw_name="AMBIGUOUS CO").id == second.id
