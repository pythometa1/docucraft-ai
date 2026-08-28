import hashlib
from datetime import datetime, timedelta, timezone

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jose import JWTError, jwt
from passlib.context import CryptContext
from sqlalchemy.orm import Session

from app.config import settings
from app.db import get_db
from app.models import User
from app.redis_client import redis_client
from app.tenancy import set_current_org

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")
bearer_scheme = HTTPBearer(auto_error=False)


def _token_key(token: str) -> str:
    return f"revoked_token:{hashlib.sha256(token.encode()).hexdigest()}"


def revoke_token(token: str) -> None:
    """Blacklist a JWT for the remainder of its natural lifetime (spec §14.1/§14.3
    -- session cache in Redis). Cheaper than a stateful session store since the
    blacklist only needs to outlive the token's own expiry."""
    redis_client.setex(_token_key(token), settings.access_token_minutes * 60, "1")


def is_token_revoked(token: str) -> bool:
    return redis_client.exists(_token_key(token)) == 1


def hash_password(password: str) -> str:
    return pwd_context.hash(password)


def verify_password(password: str, hashed: str) -> bool:
    return pwd_context.verify(password, hashed)


def create_access_token(user_id: str, org_id: str) -> str:
    expire = datetime.now(timezone.utc) + timedelta(minutes=settings.access_token_minutes)
    payload = {"sub": user_id, "org_id": org_id, "exp": expire}
    return jwt.encode(payload, settings.jwt_secret, algorithm=settings.jwt_algorithm)


def error(code: str, message: str, status_code: int = 400, details: dict | None = None):
    return HTTPException(
        status_code=status_code,
        detail={"error": {"code": code, "message": message, "details": details or {}}},
    )


def get_current_user(
    creds: HTTPAuthorizationCredentials | None = Depends(bearer_scheme),
    db: Session = Depends(get_db),
) -> User:
    if creds is None:
        raise error("TOKEN_MISSING", "Missing bearer token", 401)
    if is_token_revoked(creds.credentials):
        raise error("TOKEN_REVOKED", "This session has been signed out", 401)
    try:
        payload = jwt.decode(creds.credentials, settings.jwt_secret, algorithms=[settings.jwt_algorithm])
    except JWTError:
        raise error("TOKEN_EXPIRED", "Invalid or expired token", 401)
    org_claim = payload.get("org_id")
    if not org_claim:
        raise error("TOKEN_INVALID", "Token carries no organisation", 401)
    # §16 row-level security: this is where the request's session declares which
    # tenant it speaks for, and everything a handler reads after this point is
    # behind a policy keyed on it. It has to happen here rather than in `get_db`
    # because `get_db` runs before anyone knows who is asking. Inert on SQLite.
    #
    # The claim is only provisional until the line below checks it against the
    # user's own row -- `users` is deliberately outside RLS (authentication has
    # to find the user before it knows the tenant), so nothing but that check
    # stops a token naming an organisation its user does not belong to.
    set_current_org(db, org_claim)

    user = db.get(User, payload.get("sub"))
    if user is None:
        raise error("TOKEN_INVALID", "User no longer exists", 401)
    if user.org_id != org_claim:
        # Either a token that went stale when someone moved between
        # organisations, or a forged one. Both are a refusal, not a correction:
        # quietly adopting the row's org would let a stale token keep working
        # against data it was never issued for.
        raise error("TOKEN_INVALID", "Token does not match this user's organisation", 401)
    return user
