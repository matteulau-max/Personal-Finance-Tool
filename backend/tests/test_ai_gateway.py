"""Gateway tests.

`LiveAnthropicGateway` is the one piece the scripted fake cannot exercise, so
without these it would be the only untested code in the milestone -- and it is
the code that turns the provider's response into ours.

A stub client stands in for the SDK. Its objects duck-type the shapes the SDK
returns; nothing here reaches the network.
"""

from __future__ import annotations

import os
from types import SimpleNamespace

import pytest

from app.core.config import get_settings
from app.services.ai_gateway import AIError, LiveAnthropicGateway


class StubStream:
    def __init__(self, message):
        self._message = message

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def get_final_message(self):
        return self._message


class StubMessages:
    def __init__(self, message=None, error: Exception | None = None):
        self._message = message
        self._error = error
        self.kwargs: dict = {}

    def stream(self, **kwargs):
        self.kwargs = kwargs
        if self._error:
            raise self._error
        return StubStream(self._message)


class StubClient:
    def __init__(self, message=None, error: Exception | None = None):
        self.messages = StubMessages(message, error)


def message(*blocks, stop_reason: str = "end_turn"):
    return SimpleNamespace(
        content=list(blocks),
        stop_reason=stop_reason,
        usage=SimpleNamespace(input_tokens=11, output_tokens=22),
    )


def text_block(text: str):
    return SimpleNamespace(type="text", text=text)


def tool_block(name: str, arguments: dict, block_id: str = "toolu_1"):
    return SimpleNamespace(type="tool_use", id=block_id, name=name, input=arguments)


def thinking_block():
    return SimpleNamespace(type="thinking", thinking="considering the dates")


@pytest.fixture(autouse=True)
def _model_configured():
    os.environ["ANTHROPIC_API_KEY"] = "sk-ant-test-not-a-real-key"
    get_settings.cache_clear()
    yield
    os.environ.pop("ANTHROPIC_API_KEY", None)
    get_settings.cache_clear()


def call(gateway):
    return gateway.respond(system="s", messages=[{"role": "user", "content": "q"}], tools=[])


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------


def test_text_and_tool_calls_are_separated():
    gateway = LiveAnthropicGateway(
        client=StubClient(
            message(
                text_block("Let me check."),
                tool_block("spending_by_category", {"start": "2026-07-01"}),
                stop_reason="tool_use",
            )
        )
    )

    response = call(gateway)

    assert response.text == "Let me check."
    assert response.tool_calls[0].name == "spending_by_category"
    assert response.tool_calls[0].arguments == {"start": "2026-07-01"}


def test_thinking_blocks_are_ignored_as_text_but_kept_in_raw_content():
    """Both halves matter.

    Reasoning is not an answer, so it must not be shown -- but the API
    requires the thinking blocks to be sent back with the next turn, so they
    have to survive in `raw_content`. Dropping them breaks the second tool
    call of every conversation, which is a failure that only shows up once
    the feature is genuinely being used.
    """
    gateway = LiveAnthropicGateway(
        client=StubClient(message(thinking_block(), text_block("You spent $10.00.")))
    )

    response = call(gateway)

    assert response.text == "You spent $10.00."
    assert any(getattr(block, "type", None) == "thinking" for block in response.raw_content)


def test_usage_is_reported():
    gateway = LiveAnthropicGateway(client=StubClient(message(text_block("ok"))))

    response = call(gateway)

    assert (response.input_tokens, response.output_tokens) == (11, 22)


def test_a_refusal_stop_reason_survives_to_the_caller():
    """The engine checks this before reading the text. If the gateway
    flattened it to "end_turn", a refusal would be rendered as an answer."""
    gateway = LiveAnthropicGateway(
        client=StubClient(message(text_block("no"), stop_reason="refusal"))
    )

    assert call(gateway).stop_reason == "refusal"


# ---------------------------------------------------------------------------
# Requests
# ---------------------------------------------------------------------------


def test_adaptive_thinking_is_requested():
    """`budget_tokens` -- the older form -- is rejected outright by this model
    family, so getting this wrong is a hard failure rather than a silent
    downgrade."""
    gateway = LiveAnthropicGateway(client=(client := StubClient(message(text_block("ok")))))

    call(gateway)

    assert client.messages.kwargs["thinking"] == {"type": "adaptive"}
    assert "budget_tokens" not in str(client.messages.kwargs)


def test_the_configured_model_is_used():
    gateway = LiveAnthropicGateway(client=(client := StubClient(message(text_block("ok")))))

    call(gateway)

    assert client.messages.kwargs["model"] == get_settings().AI_MODEL


# ---------------------------------------------------------------------------
# Failures
# ---------------------------------------------------------------------------


def test_a_provider_error_becomes_an_opaque_ai_error():
    """Provider exceptions can echo the request body back, and the request
    body here is somebody's financial history. The type is logged; the text
    is not re-raised."""
    secret = "user@example.com spent 4123.55 at ACME"
    gateway = LiveAnthropicGateway(client=StubClient(error=RuntimeError(secret)))

    with pytest.raises(AIError) as caught:
        call(gateway)

    assert secret not in str(caught.value)


def test_constructing_without_a_key_fails_rather_than_defaulting():
    os.environ.pop("ANTHROPIC_API_KEY", None)
    get_settings.cache_clear()

    with pytest.raises(AIError, match="ANTHROPIC_API_KEY"):
        LiveAnthropicGateway()
