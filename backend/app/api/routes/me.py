"""Endpoints about the currently authenticated user.

`/api/me` is the first protected endpoint in the project. It is also what the
frontend calls immediately after sign-in, both to confirm the token works and
to provision the local user row on first login.
"""

from fastapi import APIRouter

from app.api.deps import CurrentUser, DbSession
from app.schemas.user import UserResponse, UserUpdateRequest

router = APIRouter(prefix="/api", tags=["me"])


@router.get("/me", response_model=UserResponse)
def read_current_user(current_user: CurrentUser) -> UserResponse:
    """Return the signed-in user.

    Note there is no `user_id` parameter. The user's identity comes from the
    verified token and nowhere else.

    That is not a stylistic choice. An endpoint like `GET /api/users/{id}`
    invites the classic broken-access-control bug: the client passes an id,
    someone forgets to check it matches the token, and now anyone can read
    anyone's profile by changing a number. If the client cannot specify who
    it is asking about, that bug cannot exist.
    """
    return UserResponse.model_validate(current_user)


@router.patch("/me", response_model=UserResponse)
def update_current_user(
    current_user: CurrentUser,
    db: DbSession,
    payload: UserUpdateRequest,
) -> UserResponse:
    """Update the signed-in user's preferences.

    `exclude_unset=True` distinguishes "the client sent nothing for this
    field" from "the client explicitly sent null". Without it, a PATCH
    containing only `{"timezone": "..."}` would wipe `full_name` -- the
    single most common bug in PATCH endpoints.
    """
    updates = payload.model_dump(exclude_unset=True)

    for field, value in updates.items():
        setattr(current_user, field, value)

    db.commit()
    db.refresh(current_user)

    return UserResponse.model_validate(current_user)
