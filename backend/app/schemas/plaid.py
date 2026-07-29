"""Request and response shapes for the Plaid endpoints."""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from app.models.enums import PlaidItemStatus, SyncStatus, SyncTrigger


class LinkTokenResponse(BaseModel):
    link_token: str
    expiration_note: str = (
        "Link tokens are short-lived. Request a new one for each Link session."
    )


class ExchangePublicTokenRequest(BaseModel):
    # `min_length=1` so an empty string is rejected by validation rather than
    # being forwarded to Plaid and coming back as a confusing 400.
    public_token: str = Field(min_length=1, max_length=512)


class InstitutionSummary(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    name: str
    logo_url: str | None = None
    primary_color: str | None = None


class PlaidItemResponse(BaseModel):
    """A connected institution.

    Note the absence of `access_token_encrypted` and `transactions_cursor`.
    Neither is any of the client's business, and the response schema is an
    allowlist -- so neither can be exposed by accident later.
    """

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    status: PlaidItemStatus
    institution: InstitutionSummary | None = None
    last_successful_sync_at: datetime | None = None
    consent_expires_at: datetime | None = None
    # A code such as ITEM_LOGIN_REQUIRED, so the UI can prompt a reconnect.
    error_code: str | None = None
    created_at: datetime


class SyncRunResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    status: SyncStatus
    trigger: SyncTrigger
    started_at: datetime
    finished_at: datetime | None
    transactions_added: int
    transactions_modified: int
    transactions_removed: int
    transactions_skipped: int
    accounts_updated: int
    error_code: str | None


class WebhookAck(BaseModel):
    received: bool = True
