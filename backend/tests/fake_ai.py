"""A scripted model, so the grounding guarantee can actually be tested.

===========================================================================
Why the fake is the important part
===========================================================================

The claim this milestone makes is not "the model behaves well". It is "the
system is safe when the model behaves badly", and you cannot demonstrate that
with a real model: you would have to persuade it to lie on demand, and a run
where it happened not to lie proves nothing about the next one.

So the fake lies on demand. A test says "return this answer, containing this
figure, after calling no tools at all", and then asserts that the figure never
reaches the caller. That test is deterministic, offline, and about the
property we actually care about.

The fake implements the same `AIGateway` protocol structurally -- no base
class, no registration. If the protocol changes, mypy notices; if the
behaviour changes, the tests notice.
"""

from __future__ import annotations

from typing import Any

from app.services.ai_gateway import AIError, AIResponse, ToolCall, ToolSpec


class FakeAIGateway:
    """Returns a scripted sequence of turns, one per call."""

    def __init__(self, *turns: AIResponse) -> None:
        self._turns = list(turns)
        self.calls: list[dict[str, Any]] = []

    def respond(
        self,
        *,
        system: str,
        messages: list[dict[str, Any]],
        tools: list[ToolSpec],
        max_tokens: int = 2048,
    ) -> AIResponse:
        self.calls.append(
            {"system": system, "messages": [*messages], "tools": tools}
        )
        if not self._turns:
            raise AssertionError(
                "The fake ran out of scripted turns -- the engine looped more "
                "times than the test expected."
            )
        return self._turns.pop(0)

    # -- introspection used by tests ---------------------------------------

    @property
    def tool_names_offered(self) -> set[str]:
        return {tool.name for tool in self.calls[-1]["tools"]}

    def prompt_text(self) -> str:
        """Everything the model was ever sent, as one string.

        Used by the test that asserts no other user's data can appear in the
        conversation.
        """
        parts = [call["system"] for call in self.calls]
        for call in self.calls:
            for message in call["messages"]:
                parts.append(str(message.get("content", "")))
        return "\n".join(parts)


class ExplodingAIGateway:
    """Fails the way the provider does, to test the error path."""

    def respond(self, **kwargs: Any) -> AIResponse:
        raise AIError("The insights service is temporarily unavailable")


# ---------------------------------------------------------------------------
# Builders
# ---------------------------------------------------------------------------


def says(text: str, *, stop_reason: str = "end_turn") -> AIResponse:
    return AIResponse(text=text, stop_reason=stop_reason)


def calls_tool(name: str, arguments: dict[str, Any] | None = None, *, id: str = "toolu_1") -> AIResponse:
    return AIResponse(
        text="",
        tool_calls=(ToolCall(id=id, name=name, arguments=arguments or {}),),
        stop_reason="tool_use",
    )
