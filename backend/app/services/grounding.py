"""Grounding: no figure reaches the user unless a query produced it.

===========================================================================
The problem, stated precisely
===========================================================================

A language model asked "how much did I spend on groceries?" will answer. If
it has the figure, it will use it. If it does not, it will still answer, in
the same confident register, with a number that looks exactly as plausible as
a real one. There is no tell. That is the whole difficulty: the failure mode
is indistinguishable from success at the point of reading.

In most products a made-up number is embarrassing. In this one it is
disqualifying -- somebody could make a financial decision on it.

===========================================================================
What we can and cannot promise
===========================================================================

It is worth being exact here, because it is easy to overclaim.

We **cannot** stop a model from generating a wrong number. Nothing in an API
can. Prompting helps and is not a guarantee; "the prompt says not to" is not
a security control.

We **can** stop the application from ever displaying one. That is a different
and much stronger claim, and it is achievable because it does not depend on
the model's behaviour at all:

  1. Every figure the model is given comes from a SQL query in `analytics.py`
     (Milestone 6), executed against this user's rows only.
  2. Before an answer is returned, every number in its text is checked against
     the set of numbers that actually appeared -- in tool results, in the
     user's own question, or in the system prompt.
  3. An answer containing an unaccounted-for number is **not shown**. The
     request fails loudly rather than rendering.

Step 3 is the one that matters. Steps 1 and 2 make good behaviour likely;
step 3 makes bad behaviour harmless. The tests in `test_grounding.py` script
a model that fabricates and assert the fabrication never reaches the caller.

===========================================================================
Why verbatim, and not "close enough"
===========================================================================

The check requires exact numeric equality. If a query returned $1,234.56 and
the answer says "about $1,200", verification fails and the answer is
rejected -- even though a human would call that honest rounding.

That is a deliberate trade. Allowing tolerance means picking a threshold, and
any threshold is a number a wrong answer can hide inside. 5% of a $40,000
balance is $2,000. The prompt instead tells the model to quote figures
exactly, which is easy to comply with, and rounding is left to the frontend
formatter where it is applied by code rather than by inference.

The cost is real: an occasional false rejection on a well-meant paraphrase.
The alternative cost is showing someone a number nobody computed. Given this
project's stated priority order -- security, then data integrity, then
everything else -- the strict check wins.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any

logger = logging.getLogger(__name__)


# Matches anything a reader would perceive as a number: 1234, 1,234.56,
# $1,234.56, -12.5, 3.4%, 2026-07-01 (as three separate tokens).
#
# Deliberately greedy about what counts as a number. A pattern that misses
# something lets that thing through unchecked, and the failure mode of this
# module is silence -- so it errs towards flagging.
_NUMBER = re.compile(r"\d[\d,]*(?:\.\d+)?")


@dataclass(frozen=True)
class GroundingResult:
    ok: bool
    unsupported: tuple[str, ...] = ()

    @property
    def message(self) -> str:
        if self.ok:
            return "every figure is traceable to a query"
        return (
            "the answer contained figures that no query produced: "
            + ", ".join(self.unsupported)
        )


def extract_numbers(text: str) -> list[str]:
    """Every numeric token in a piece of text, as written."""
    return _NUMBER.findall(text or "")


def _canonical(token: str) -> str | None:
    """Reduce a numeric token to a comparable form.

    "$1,234.50", "1234.5" and "1,234.500" all become the same string, so
    formatting differences do not cause false rejections while genuine value
    differences still do.
    """
    cleaned = token.replace(",", "").strip()
    if not cleaned:
        return None
    try:
        value = Decimal(cleaned)
    except InvalidOperation:
        return None
    # normalize() collapses trailing zeros but renders large integers in
    # exponent form (100 -> 1E+2); +value restores plain notation. Both sides
    # of every comparison go through this function, so the form only has to be
    # consistent, not pretty.
    return str(+value.normalize())


def collect_supported_numbers(*sources: Any) -> set[str]:
    """Build the set of numbers an answer is allowed to contain.

    Sources are anything JSON-serializable: tool results, the user's question,
    the system prompt. Every numeric token found in any of them is permitted.

    Note what this permits and what it does not. It permits the model to
    *repeat* any figure it was given, including intermediate ones it chose not
    to headline. It does not permit arithmetic: if two queries return $300 and
    $200, the model may not write "$500". That is intentional -- a derived
    figure has to be derived by a query, because arithmetic done in prose is
    arithmetic nobody checked. Where a derived figure is genuinely wanted
    (percentage change, savings rate, remaining budget), the corresponding
    analytics function computes it in SQL and returns it as its own field.
    """
    supported: set[str] = set()

    for source in sources:
        if source is None:
            continue
        text = source if isinstance(source, str) else json.dumps(source, default=str)
        for token in extract_numbers(text):
            canonical = _canonical(token)
            if canonical is not None:
                supported.add(canonical)

    return supported


def verify(answer: str, supported: set[str]) -> GroundingResult:
    """Check an answer against the supported set.

    Returns rather than raises: the caller decides what a failure means, and
    the unsupported tokens are wanted for the log either way.
    """
    unsupported: list[str] = []

    for token in extract_numbers(answer):
        canonical = _canonical(token)
        if canonical is None:
            continue
        if canonical not in supported and token not in unsupported:
            unsupported.append(token)

    if unsupported:
        # The tokens, not the answer: the answer is financial content and does
        # not belong in an application log.
        logger.warning("Rejected an ungrounded answer. Unsupported: %s", unsupported)
        return GroundingResult(ok=False, unsupported=tuple(unsupported))

    return GroundingResult(ok=True)
