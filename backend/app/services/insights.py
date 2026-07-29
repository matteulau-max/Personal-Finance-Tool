"""The insights engine: the loop, and the guarantee.

===========================================================================
What happens when you ask a question
===========================================================================

    1. Your question, plus a system prompt containing today's date and no
       figures whatsoever, goes to the model.
    2. The model asks for one or more tools (`insight_tools.py`).
    3. We run them -- as SELECTs, scoped to you -- and record every number
       that comes back.
    4. Repeat until the model stops asking for tools, or we hit the cap.
    5. Check every number in the answer against what step 3 recorded.
    6. If anything is unaccounted for, **the answer is not returned.**

Step 6 is the milestone. Everything else is plumbing around it.

===========================================================================
Why the loop is written out rather than delegated
===========================================================================

The Anthropic SDK's tool runner would collapse steps 1-4 into one call, and
in most applications that is the right call. Here the loop *is* the security
boundary, and three of its properties are ones we cannot delegate:

* **Provenance.** Every tool result has to be accumulated as it is produced,
  because the grounding check in step 5 is only as good as that record.
* **A hard iteration cap.** Each turn is a paid API call over somebody's
  financial data. An agent that decides to explore is an agent running up a
  bill on data it should be touching as little as possible.
* **Failure containment.** A tool error becomes a `tool_result` with
  `is_error`, so the model can correct a bad date and carry on -- but a
  *scoping* error is not something to hand back and retry.

===========================================================================
On sending financial data to a third party
===========================================================================

This feature sends aggregates, merchant names and (when the user asks for
individual transactions) transaction descriptions to Anthropic's API. That is
a real disclosure and it deserves to be stated plainly rather than buried:

* It is **off unless configured**. No API key, no endpoint -- the route
  returns 503, not a degraded answer.
* Only what a tool returns is sent. Account numbers, access tokens, balances
  of accounts excluded from net worth, and every `raw_payload` from Plaid stay
  where they are, because no tool returns them.
* Nothing is sent until the user asks a question. There is no background
  summarization pass over the transaction history.

A user who wants none of this simply never visits the page, and a deployment
that wants none of it leaves the key unset.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy.orm import Session

from app.models import User
from app.services import grounding
from app.services.ai_gateway import AIError, AIGateway, ToolCall
from app.services.insight_tools import InsightTools, ToolArgumentError

logger = logging.getLogger(__name__)

MAX_TURNS = 6
MAX_QUESTION_LENGTH = 500

SYSTEM_PROMPT = """\
You are the insights assistant inside a personal finance application. You are \
answering the account holder's questions about their own money.

Today is {today}.

## The one rule that matters

You have no knowledge of this person's finances. Every figure you state must \
come from a tool result in this conversation, quoted exactly as the tool \
returned it.

Do not estimate. Do not round. Do not add two figures together and state the \
sum -- if you need a total, a percentage, or a difference, there is a tool \
that computes it, and if there is not, say so instead of doing the arithmetic \
yourself.

This is enforced. An answer containing a figure that no tool returned is \
discarded before the user sees it, so a guessed number does not produce a \
slightly-wrong answer -- it produces no answer at all.

If the tools cannot answer the question, say what you could not find out. \
That is a useful reply. An invented figure is not.

## How to answer

- Lead with the answer, then the supporting figures.
- Plain sentences. No preamble, no "great question", no restating the question.
- Short. Two or three sentences for a simple question; a few bullets for a \
comparison. This is a dashboard panel, not an essay.
- Amounts as the tool gave them, with a dollar sign: $1,234.56.
- When a tool result carries a `note`, respect it -- it is usually telling you \
about a real limitation of the number you are about to quote.
- Never advise on investments, taxes, or specific financial products. \
Describe what the data shows and let the user decide.
"""


@dataclass
class ToolInvocation:
    """One tool call and its result, kept so the answer can cite its sources."""

    name: str
    arguments: dict[str, Any]
    ok: bool = True


@dataclass
class Insight:
    answer: str
    sources: list[ToolInvocation] = field(default_factory=list)
    turns: int = 0
    input_tokens: int = 0
    output_tokens: int = 0


class UngroundedAnswer(RuntimeError):
    """The model produced a figure no query supports. Nothing is returned."""

    def __init__(self, unsupported: tuple[str, ...]) -> None:
        self.unsupported = unsupported
        super().__init__("the answer contained unverifiable figures")


def answer_question(
    db: Session,
    user: User,
    gateway: AIGateway,
    question: str,
    *,
    today: dt.date | None = None,
) -> Insight:
    """Answer one question. Raises rather than returning an unsafe answer."""
    question = (question or "").strip()
    if not question:
        raise ValueError("A question is required")
    if len(question) > MAX_QUESTION_LENGTH:
        # Not politeness: the question is the untrusted part of the prompt, and
        # a bounded one is a smaller lever for anyone trying to steer the model.
        raise ValueError(
            f"Questions are limited to {MAX_QUESTION_LENGTH} characters"
        )

    today = today or dt.date.today()
    tools = InsightTools(db, user, today=today)
    system = SYSTEM_PROMPT.format(today=today.isoformat())

    messages: list[dict[str, Any]] = [{"role": "user", "content": question}]

    # Seeded with the question and the prompt so the model may echo a figure
    # the user themselves supplied ("restaurants over $100") or today's date.
    supported = grounding.collect_supported_numbers(question, system)

    result = Insight(answer="")

    for turn in range(1, MAX_TURNS + 1):
        response = gateway.respond(
            system=system, messages=messages, tools=tools.specs()
        )

        result.turns = turn
        result.input_tokens += response.input_tokens
        result.output_tokens += response.output_tokens

        # Checked before the content is read, not after: on a refusal the
        # blocks are not an answer and must not be presented as one.
        if response.stop_reason == "refusal":
            raise AIError("The assistant declined to answer that question")

        if not response.tool_calls:
            result.answer = response.text.strip()
            break

        messages.append(
            {
                "role": "assistant",
                "content": response.raw_content or _reconstruct(response),
            }
        )

        tool_results = []
        for call in response.tool_calls:
            payload, ok = _run_tool(tools, call)
            result.sources.append(
                ToolInvocation(name=call.name, arguments=call.arguments, ok=ok)
            )
            if ok:
                # Only successful results widen what the model may quote. An
                # error message might contain an echoed argument, and an echoed
                # argument is a number the model chose -- not one we computed.
                supported |= grounding.collect_supported_numbers(payload)

            tool_results.append(
                {
                    "type": "tool_result",
                    "tool_use_id": call.id,
                    "content": json.dumps(payload, default=str),
                    **({"is_error": True} if not ok else {}),
                }
            )

        messages.append({"role": "user", "content": tool_results})
    else:
        # The loop ran out of turns with the model still asking for tools.
        logger.warning("Insight question hit the %s-turn cap", MAX_TURNS)
        raise AIError("That question needed more steps than we allow")

    if not result.answer:
        raise AIError("The assistant returned an empty answer")

    verdict = grounding.verify(result.answer, supported)
    if not verdict.ok:
        logger.error(
            "Refused to return an ungrounded answer after %s tool call(s): %s",
            len(result.sources),
            verdict.unsupported,
        )
        raise UngroundedAnswer(verdict.unsupported)

    return result


def _run_tool(tools: InsightTools, call: ToolCall) -> tuple[Any, bool]:
    """Run one tool. Bad arguments come back as an error the model can fix."""
    try:
        return tools.run(call.name, call.arguments), True
    except ToolArgumentError as exc:
        logger.info("Tool %s rejected its arguments: %s", call.name, exc)
        return {"error": str(exc)}, False
    except Exception:
        # An unexpected failure is ours, not the model's. It gets a generic
        # message -- a database error string handed to a model and quoted back
        # is an information leak with extra steps.
        logger.exception("Tool %s failed", call.name)
        return {"error": "That query could not be completed."}, False


def _reconstruct(response: Any) -> list[dict[str, Any]]:
    """Rebuild assistant content when the gateway supplies none.

    Only fakes take this path; the live gateway passes the provider's blocks
    straight through. It exists so a test fake stays a few lines long instead
    of having to imitate the SDK's content types.
    """
    blocks: list[dict[str, Any]] = []
    if response.text:
        blocks.append({"type": "text", "text": response.text})
    for call in response.tool_calls:
        blocks.append(
            {
                "type": "tool_use",
                "id": call.id,
                "name": call.name,
                "input": call.arguments,
            }
        )
    return blocks
