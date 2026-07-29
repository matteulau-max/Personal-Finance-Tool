"""The boundary between our code and the Claude API.

===========================================================================
Why a gateway, again
===========================================================================

This is the same pattern as `plaid_gateway.py`, for the same reasons: nothing
outside this file imports `anthropic`, the SDK's types never reach our domain,
and tests substitute the boundary rather than the internet.

For an AI feature the testability argument is much stronger than it was for
Plaid, because of what we need to prove. The claim this milestone makes is:

    the product will never show the user a number the model made up

You cannot test that against a live model. A live model is non-deterministic,
so "it did not fabricate a number in these ten runs" is not evidence about the
eleventh. What you *can* do is script the boundary: make the fake gateway
return a confidently-worded answer containing a number that no query ever
produced, and assert that the system refuses to show it.

That test is deterministic, runs offline in milliseconds, and tests the
property we actually care about -- which is not "the model behaves" but "the
system is safe when the model misbehaves."

===========================================================================
Why a one-turn boundary and a hand-written loop
===========================================================================

The Anthropic SDK ships a tool runner (`client.beta.messages.tool_runner`)
that drives the whole request -> execute -> repeat cycle for you, and for most
applications it is the right choice.

It is not the right choice here, for one specific reason: **the tool loop is
the security perimeter.** Every tool call has to be bound to the requesting
user, every returned figure has to be recorded so the answer can be checked
against it afterwards, and the iteration count has to be capped. Those are
properties of the loop, so the loop is ours -- see `insights.py`.

The gateway is therefore deliberately small: one turn in, one turn out. That
is also the narrowest possible thing for a fake to implement.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Protocol

from app.core.config import get_settings

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Our own types. No `anthropic` classes past this file.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ToolSpec:
    """A tool as advertised to the model."""

    name: str
    description: str
    input_schema: dict[str, Any]


@dataclass(frozen=True)
class ToolCall:
    """The model asking for a tool to be run."""

    id: str
    name: str
    arguments: dict[str, Any]


@dataclass(frozen=True)
class AIResponse:
    """One assistant turn.

    `stop_reason` matters more than it looks. `"refusal"` means the model
    declined, and in that case the content blocks must not be treated as an
    answer -- so callers check the stop reason before reading text.
    """

    text: str
    tool_calls: tuple[ToolCall, ...] = ()
    stop_reason: str = "end_turn"
    input_tokens: int = 0
    output_tokens: int = 0
    # Opaque assistant content, passed straight back on the next turn so the
    # model sees its own tool_use blocks. Never inspected outside the gateway.
    raw_content: list[Any] = field(default_factory=list)


class AIError(RuntimeError):
    """Any failure talking to the model provider.

    Deliberately opaque. Provider errors can echo request content back, and
    request content here is somebody's financial history -- so the message we
    keep is generic and the detail goes to logs, never to the client.
    """


class AIGateway(Protocol):
    """The one operation the insights engine needs."""

    def respond(
        self,
        *,
        system: str,
        messages: list[dict[str, Any]],
        tools: list[ToolSpec],
        max_tokens: int = 2048,
    ) -> AIResponse: ...


# ---------------------------------------------------------------------------
# The real thing
# ---------------------------------------------------------------------------


class LiveAnthropicGateway:
    """Talks to the Claude API.

    Two API details worth knowing, because both changed recently enough that
    older examples get them wrong:

    * Extended thinking is requested as `{"type": "adaptive"}`. The older
      `budget_tokens` form is *rejected with a 400* on this model family, so
      code copied from a 2025 tutorial fails immediately rather than quietly
      degrading. Adaptive lets the model spend more reasoning on "why did my
      spending change across nine categories" than on "what did I spend".

    * Streaming is used because a multi-tool answer can take a while, and a
      non-streamed request can hit the provider's request timeout. We do not
      need the individual events, so `get_final_message()` reassembles the
      whole turn for us.
    """

    def __init__(self, client: Any | None = None) -> None:
        settings = get_settings()

        if client is not None:
            self._client = client
        else:
            if not settings.ANTHROPIC_API_KEY:
                raise AIError("ANTHROPIC_API_KEY is not configured")
            # Imported here, not at module scope, so the package is only
            # required by deployments that actually enable insights.
            import anthropic

            self._client = anthropic.Anthropic(
                api_key=settings.ANTHROPIC_API_KEY,
                timeout=settings.AI_TIMEOUT_SECONDS,
                max_retries=settings.AI_MAX_RETRIES,
            )

        self._model = settings.AI_MODEL

    def respond(
        self,
        *,
        system: str,
        messages: list[dict[str, Any]],
        tools: list[ToolSpec],
        max_tokens: int = 2048,
    ) -> AIResponse:
        try:
            with self._client.messages.stream(
                model=self._model,
                max_tokens=max_tokens,
                system=system,
                messages=messages,
                thinking={"type": "adaptive"},
                tools=[
                    {
                        "name": tool.name,
                        "description": tool.description,
                        "input_schema": tool.input_schema,
                    }
                    for tool in tools
                ],
            ) as stream:
                message = stream.get_final_message()
        except Exception as exc:  # noqa: BLE001 -- mapped deliberately
            # `exc` may contain the request body. Log the type, not the text.
            logger.error("Claude API call failed: %s", type(exc).__name__)
            raise AIError("The insights service is temporarily unavailable") from exc

        text_parts: list[str] = []
        calls: list[ToolCall] = []

        for block in message.content:
            if block.type == "text":
                text_parts.append(block.text)
            elif block.type == "tool_use":
                calls.append(
                    ToolCall(
                        id=block.id,
                        name=block.name,
                        arguments=dict(block.input or {}),
                    )
                )

        return AIResponse(
            text="".join(text_parts).strip(),
            tool_calls=tuple(calls),
            stop_reason=message.stop_reason or "end_turn",
            input_tokens=getattr(message.usage, "input_tokens", 0),
            output_tokens=getattr(message.usage, "output_tokens", 0),
            raw_content=list(message.content),
        )


def get_ai_gateway() -> AIGateway:
    """FastAPI dependency. Overridden wholesale in tests.

    The "not configured" case is answered *here* rather than in the route.
    That is not a stylistic choice -- a dependency is resolved before the
    endpoint body, so a check inside the endpoint runs too late: constructing
    the client has already failed by then, and the caller gets a 500 where a
    503 was intended. The first version of this file had exactly that bug,
    and the test asserting a 503 is what surfaced it.
    """
    if not get_settings().ai_configured:
        from fastapi import HTTPException, status

        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Insights are not configured on this server.",
        )
    return LiveAnthropicGateway()
