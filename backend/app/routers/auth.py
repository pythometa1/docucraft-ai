from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import get_db
from app.models import User
from fastapi.security import HTTPAuthorizationCredentials

from app.rate_limit import rate_limit_by_ip
from app.security import bearer_scheme, create_access_token, error, get_current_user, revoke_token, verify_password

router = APIRouter(tags=["auth"])


class TokenRequest(BaseModel):
    email: str
    password: str


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"


@router.post("/auth/token", response_model=TokenResponse)
def login(request: Request, body: TokenRequest, db: Session = Depends(get_db)):
    rate_limit_by_ip(request)
    user = db.scalar(select(User).where(User.email == body.email.lower()))
    if not user or not verify_password(body.password, user.password_hash):
        raise error("INVALID_CREDENTIALS", "Incorrect email or password", 401)
    return TokenResponse(access_token=create_access_token(user.id, user.org_id))


@router.post("/auth/logout")
def logout(creds: HTTPAuthorizationCredentials = Depends(bearer_scheme)):
    if creds:
        revoke_token(creds.credentials)
    return {"status": "signed_out"}


@router.get("/me")
def me(user: User = Depends(get_current_user)):
    """Who you are, and what this role lets you do.

    `capabilities` is sent so the UI can disable a control the server would
    refuse, rather than letting somebody type a rejection note and discover the
    rule as a 403. It is not a security boundary and is not treated as one --
    every capability is still checked server-side by `require()`; this is the
    same list, sent early enough to be useful.
    """
    from app.authz import capabilities_of

    return {
        "capabilities": sorted(capabilities_of(user.role_key)),
        "id": user.id,
        "org_id": user.org_id,
        "email": user.email,
        "full_name": user.full_name,
        "job_title": user.job_title,
        "timezone": user.timezone,
        "role": user.role_key,
        "function": user.function,
    }
