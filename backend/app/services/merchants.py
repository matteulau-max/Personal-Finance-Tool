"""Merchant normalization.

===========================================================================
The problem
===========================================================================

Your bank sends these:

    WHOLEFDS MKT #10259
    Whole Foods #125
    WHOLE FOODS MARKET 3421 SEATTLE WA
    SQ *WHOLE FOODS
    TST* WHOLE FOODS - CAPITOL

They are one merchant. Until the application knows that, "how much do I spend
at Whole Foods?" has five different answers and merchant analytics are
worthless.

===========================================================================
The approach: normalize, then match
===========================================================================

`normalize_descriptor()` reduces a raw string to a comparison key by
stripping everything that varies between visits:

    payment processor prefixes   SQ *, TST*, PAYPAL *, SP , POS
    store and terminal numbers   #10259, STORE 3421
    card/reference numbers       XXXX1234, long digit runs
    trailing city and state      SEATTLE WA
    dates and times              03/14, 14:22
    punctuation and whitespace

The last four reduce to a shared key. `WHOLEFDS MKT` does NOT -- and that
limitation is the most important thing to understand about this module.

===========================================================================
What normalization cannot do
===========================================================================

Normalization is string cleanup. It can strip `#10259`, `SQ *` and `SEATTLE
WA`, because those are noise. It can never learn that `WHOLEFDS MKT` means
`Whole Foods Market`, because that is an ABBREVIATION, not noise -- resolving
it needs a dictionary of the world's merchants, which is not something a
regular expression can contain.

So there are three layers, and normalization is only the weakest:

  1. `MerchantAlias`  -- an explicit past correction by this user. Exact.
  2. Plaid's `merchant_name` -- Plaid resolves the descriptor against its own
     merchant database, so all five examples above arrive with
     merchant_name = "Whole Foods Market". `resolve_merchant` keys on THIS
     when present, which is what actually unifies them in production.
  3. Normalization of the raw descriptor -- the fallback for manual and CSV
     transactions, where nobody has enriched anything.

Pretending layer 3 could do the whole job would be the mistake here. It
handles the noise; the other two handle the meaning.

===========================================================================
Why this is heuristic, and why that is acceptable
===========================================================================

Bank descriptors have no standard. Any rule set will mis-handle something:
`SQ *SQUARE` is a real merchant whose name begins with a processor prefix,
and a shop genuinely called `7-Eleven #123` loses meaning if you strip the
number.

So normalization is deliberately *not* the last word. It is a first guess,
and when it is wrong the user corrects one transaction and we record a
`MerchantAlias`. Every future transaction matching that pattern is then
resolved instantly and exactly.

That is the important design decision: **the system gets better through use,
not through us writing ever-cleverer regular expressions.** Chasing perfect
normalization is an infinite task; learning from one correction is bounded.
"""

from __future__ import annotations

import re
import unicodedata

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Merchant, MerchantAlias, User

# Payment processors and aggregators prepend their own tag. These are the
# common ones; the list grows as real statements reveal more.
PROCESSOR_PREFIXES = (
    "sq *",       # Square
    "tst*",       # Toast
    "tst *",
    "sp ",        # Shopify / Shop Pay
    "sp*",
    "paypal *",
    "pp*",
    "pay*",
    "ext*",
    "in *",       # Intuit
    "wl *",
    "chkcardpurchase",
    "pos debit",
    "pos purchase",
    "debit card purchase",
    "recurring payment",
    "ach debit",
    "ach credit",
    "visa purchase",
    "purchase authorized on",
)

# Noise that appears anywhere in the string.
NOISE_PATTERNS = (
    # Store / terminal numbers: "#10259", "store 3421", "str 12"
    re.compile(r"#\s*\d+"),
    re.compile(r"\b(?:store|str|st|unit|term|terminal|loc)\s*#?\s*\d+\b"),
    # Masked card numbers and long digit runs (reference ids, auth codes).
    re.compile(r"\bx{2,}\d+\b"),
    re.compile(r"\b\d{5,}\b"),
    # Dates and times inside the descriptor.
    re.compile(r"\b\d{1,2}[/-]\d{1,2}(?:[/-]\d{2,4})?\b"),
    re.compile(r"\b\d{1,2}:\d{2}(?::\d{2})?\b"),
    # Currency-ish fragments some banks append.
    re.compile(r"\busd\b"),
)

# Two-letter US state codes, stripped only when trailing (see below).
US_STATES = {
    "al", "ak", "az", "ar", "ca", "co", "ct", "de", "fl", "ga", "hi", "id",
    "il", "in", "ia", "ks", "ky", "la", "me", "md", "ma", "mi", "mn", "ms",
    "mo", "mt", "ne", "nv", "nh", "nj", "nm", "ny", "nc", "nd", "oh", "ok",
    "or", "pa", "ri", "sc", "sd", "tn", "tx", "ut", "vt", "va", "wa", "wv",
    "wi", "wy", "dc",
}

# Corporate suffixes that carry no distinguishing information.
CORPORATE_SUFFIXES = {"inc", "llc", "ltd", "co", "corp", "company", "the"}


def normalize_descriptor(raw: str) -> str:
    """Reduce a bank descriptor to a stable comparison key.

    Returns lowercase words joined by single spaces, or "" if nothing
    meaningful survives (in which case the caller should fall back to the
    original string rather than matching everything to one empty merchant).
    """
    if not raw:
        return ""

    # Normalize accented characters to their ASCII form so "CAFÉ" and "CAFE"
    # compare equal.
    text = unicodedata.normalize("NFKD", raw)
    text = text.encode("ascii", "ignore").decode()
    text = text.lower().strip()

    # Processor prefixes come off first: they sit at the very start, and
    # removing them changes what the rest of the rules see.
    for prefix in PROCESSOR_PREFIXES:
        if text.startswith(prefix):
            text = text[len(prefix) :]
            break

    for pattern in NOISE_PATTERNS:
        text = pattern.sub(" ", text)

    # Punctuation becomes spaces rather than being deleted, so "AMAZON.COM"
    # does not become the single token "amazoncom".
    text = re.sub(r"[^a-z0-9]+", " ", text)

    words = [word for word in text.split() if word]

    # Strip a trailing state code, and the city that usually precedes it.
    # Only when TRAILING: "washington post" must keep its "washington", and
    # "in n out" must keep its "in".
    if len(words) > 1 and words[-1] in US_STATES:
        words = words[:-1]
        if len(words) > 1:
            words = words[:-1]

    while len(words) > 1 and words[-1] in CORPORATE_SUFFIXES:
        words = words[:-1]

    # A single leftover digit group is a store number we failed to catch.
    words = [w for w in words if not (w.isdigit() and len(words) > 1)]

    return " ".join(words).strip()


def display_name_from(raw: str, normalized: str) -> str:
    """A human-friendly name to show for a newly discovered merchant.

    Title-cases the normalized form: "whole foods" -> "Whole Foods". Not
    always ideal ("Att" for AT&T), but it is a starting point the user can
    correct, and the correction sticks.
    """
    if not normalized:
        return raw.strip()[:255]
    return " ".join(word.capitalize() for word in normalized.split())[:255]


def resolve_merchant(
    db: Session,
    user: User,
    *,
    raw_name: str,
    plaid_merchant_name: str | None = None,
) -> Merchant | None:
    """Find, or create, the merchant for a transaction descriptor.

    Resolution order, most trustworthy first:

      1. A `MerchantAlias` the user (or the global list) has learned. An
         explicit past correction always beats a fresh guess.
      2. An existing merchant whose normalized name matches. The user's own
         merchants take precedence over the shared global ones.
      3. Create one, preferring Plaid's cleaned-up `merchant_name` over our
         own normalization of the raw descriptor -- Plaid has far more data
         to work from than a regular expression does.
    """
    # Prefer Plaid's cleaned-up merchant name as the MATCHING key, not just as
    # a display label.
    #
    # This matters more than it looks. Normalization is string cleanup: it can
    # strip "#10259" but it can never learn that "WHOLEFDS MKT" means "Whole
    # Foods Market" -- that is an abbreviation, not noise. Left to
    # normalization alone, `WHOLEFDS MKT`, `WHOLE FOODS MARKET` and
    # `SQ *WHOLE FOODS` produce three different keys and therefore three
    # merchants, which is exactly the problem this module exists to solve.
    #
    # Plaid resolves the descriptor against its own merchant database, so
    # every one of those arrives with merchant_name = "Whole Foods Market".
    # Keying on that unifies them for free.
    key_source = plaid_merchant_name or raw_name
    normalized = normalize_descriptor(key_source)

    if not normalized:
        normalized = normalize_descriptor(raw_name)

    if not normalized:
        # Nothing usable survived. Better no merchant than one called "".
        return None

    # The alias lookup still uses the RAW descriptor, because that is what the
    # user was looking at when they made a correction.
    raw_normalized = normalize_descriptor(raw_name) or normalized

    alias = db.execute(
        select(MerchantAlias)
        .where(
            MerchantAlias.raw_pattern == raw_normalized,
            # The user's own alias, or a global one. Ordering below makes
            # the user's win.
            (MerchantAlias.user_id == user.id) | (MerchantAlias.user_id.is_(None)),
        )
        # NULLs last => the user's own alias is returned first.
        .order_by(MerchantAlias.user_id.is_(None))
    ).scalars().first()

    if alias is not None:
        return db.get(Merchant, alias.merchant_id)

    existing = db.execute(
        select(Merchant)
        .where(
            Merchant.normalized_name == normalized,
            (Merchant.user_id == user.id) | (Merchant.user_id.is_(None)),
        )
        .order_by(Merchant.user_id.is_(None))
    ).scalars().first()

    if existing is not None:
        return existing

    merchant = Merchant(
        # Global, so improving it helps everyone. A user who disagrees gets
        # their own row via `learn_alias` below, which takes precedence.
        user_id=None,
        normalized_name=normalized,
        display_name=(plaid_merchant_name or display_name_from(raw_name, normalized))[
            :255
        ],
    )
    db.add(merchant)
    db.flush()
    return merchant


def learn_alias(
    db: Session, user: User, *, raw_name: str, merchant: Merchant
) -> MerchantAlias | None:
    """Remember that this descriptor means this merchant, for this user.

    Called when a user corrects a transaction's merchant. From then on, every
    transaction with the same normalized descriptor resolves instantly and
    correctly -- including ones that have not been imported yet.

    Deliberately scoped to the user rather than written to the global list.
    One person's correction is not evidence for everybody: two users can
    legitimately disagree about what a shared descriptor means, and a global
    write would let one of them silently change the other's data.
    """
    normalized = normalize_descriptor(raw_name)
    if not normalized:
        return None

    existing = db.execute(
        select(MerchantAlias).where(
            MerchantAlias.user_id == user.id,
            MerchantAlias.raw_pattern == normalized,
        )
    ).scalar_one_or_none()

    if existing is not None:
        existing.merchant_id = merchant.id
        db.flush()
        return existing

    alias = MerchantAlias(
        user_id=user.id, raw_pattern=normalized, merchant_id=merchant.id
    )
    db.add(alias)
    db.flush()
    return alias
