"""API response shapes for users.

Why a separate schema instead of returning the SQLAlchemy model?

Because the model contains things the client must never see. Today that is
`clerk_user_id`; tomorrow someone adds an internal flag or a token. If
endpoints return models directly, every new column is published to the
internet by default.

A response schema inverts that: fields are private unless explicitly listed.
The safe behaviour becomes the default one, and adding a column to the
database can never accidentally leak it.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict


class UserResponse(BaseModel):
    # Lets Pydantic read attributes off a SQLAlchemy object rather than
    # requiring a dict.
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    # Plain `str`, not `EmailStr`, deliberately.
    #
    # Validation belongs on the way IN, not on the way OUT. If a row somehow
    # holds a value that fails validation, an `EmailStr` response field turns
    # a harmless read into a 500 -- the user cannot even load their profile to
    # fix it. Output schemas should describe what we send, not re-police what
    # we already stored. (Clerk owns and verifies the address anyway.)
    email: str
    full_name: str | None
    default_currency: str
    timezone: str
    created_at: datetime

    # Deliberately NOT exposed: clerk_user_id, is_active, last_seen_at.


class UserUpdateRequest(BaseModel):
    """Fields a user is allowed to change about themselves.

    Note what is absent: `email` (Clerk owns it) and `is_active` (a user
    cannot reactivate their own suspended account). Accepting a field here is
    an explicit decision to let clients write it -- which is exactly why the
    request schema is separate from the response schema.
    """

    full_name: str | None = None
    default_currency: str | None = None
    timezone: str | None = None
