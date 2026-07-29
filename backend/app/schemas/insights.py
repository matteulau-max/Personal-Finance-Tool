"""Insight request and response schemas."""

from __future__ import annotations

from pydantic import BaseModel, Field


class QuestionRequest(BaseModel):
    question: str = Field(min_length=1, max_length=500)


class SourceResponse(BaseModel):
    """Which query produced the numbers in the answer.

    Returned to the client on purpose. "Trust me" is not an acceptable answer
    from a system that talks about somebody's money -- being able to see that
    a figure came from `compare_categories` over a stated date range is what
    makes the answer checkable rather than merely confident.
    """

    tool: str
    arguments: dict = Field(default_factory=dict)


class InsightResponse(BaseModel):
    answer: str
    sources: list[SourceResponse] = Field(default_factory=list)


class SuggestionResponse(BaseModel):
    """A starter question. Static, and deliberately not model-generated --
    there is nothing to gain from paying for a round trip to render four
    buttons, and a suggestion is a place a hallucinated premise could hide."""

    question: str
