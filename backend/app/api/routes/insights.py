"""Natural-language insight endpoints."""

from __future__ import annotations

import logging
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status

from app.api.deps import CurrentUser, DbSession
from app.core.rate_limit import RateLimitedInsightsUser
from app.schemas.insights import (
    InsightResponse,
    QuestionRequest,
    SourceResponse,
    SuggestionResponse,
)
from app.services.ai_gateway import AIError, AIGateway, get_ai_gateway
from app.services.insights import UngroundedAnswer, answer_question

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/insights", tags=["insights"])

Gateway = Annotated[AIGateway, Depends(get_ai_gateway)]

# Static, and each one maps onto a tool that exists. Suggesting a question the
# tools cannot answer teaches the user that the feature is unreliable.
SUGGESTIONS = (
    "Why did I spend more this month?",
    "What subscriptions could I cancel?",
    "Where is most of my money going?",
    "How long would my savings last?",
)


@router.get("/suggestions", response_model=list[SuggestionResponse])
def suggestions(current_user: CurrentUser) -> list[SuggestionResponse]:
    return [SuggestionResponse(question=text) for text in SUGGESTIONS]


@router.post("", response_model=InsightResponse)
def ask(
    payload: QuestionRequest,
    # `RateLimitedInsightsUser` authenticates *and* meters. Declaring the
    # limit as the thing that produces the user means it cannot be dropped
    # while the endpoint still works -- remove it and there is no user.
    current_user: RateLimitedInsightsUser,
    db: DbSession,
    gateway: Gateway,
) -> InsightResponse:
    """Ask a question about your own finances.

    Note the failure modes, because they are the design. This endpoint would
    rather return an error than an answer it cannot substantiate:

    * 429 -- too many questions. This is the only endpoint here that spends
      money per call, so the limit is on the request rather than on the reply.
    * 503 -- the feature is not configured. Raised by the gateway dependency
      before this body runs, so no request is made and nothing is charged.
      Not a fallback answer.
    * 422 -- the model produced a figure no query supports. The answer is
      discarded; the user is told the check failed rather than shown the text
      with a warning next to it, because a number on screen is a number people
      remember.
    """
    try:
        insight = answer_question(db, current_user, gateway, payload.question)
    except UngroundedAnswer:
        # The offending figures are in the logs. They are not echoed here:
        # returning them would put the fabricated number on the user's screen,
        # which is the exact outcome the check exists to prevent.
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=(
                "That answer could not be verified against your data, so it "
                "was discarded. Please try rephrasing the question."
            ),
        )
    except ValueError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail=str(exc))
    except AIError as exc:
        logger.warning("Insight request failed: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="The insights service could not answer right now.",
        )

    return InsightResponse(
        answer=insight.answer,
        sources=[
            SourceResponse(tool=source.name, arguments=source.arguments)
            for source in insight.sources
            if source.ok
        ],
    )
