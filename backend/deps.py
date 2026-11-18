"""FastAPI dependencies: authentication, permission checks and location checks.

Every protected endpoint must depend on `require(...)` (or `current_user`); the UI hiding a
button is never the control.
"""
from __future__ import annotations

from datetime import datetime, timezone

import jwt
from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.db import get_db
from backend.models import AuthSession, User
from backend.security import decode_access_token
from backend.services.scope import can_access_location

_bearer = HTTPBearer(auto_error=False)


class Principal:
    """The authenticated user plus the permission set resolved from their role."""

    def __init__(self, user: User, jti: str):
        self.user = user
        self.jti = jti
        self.permissions: frozenset[str] = frozenset(p.permission for p in user.role.permissions)

    def has(self, permission: str) -> bool:
        return permission in self.permissions


def client_ip(request: Request) -> str | None:
    return request.client.host if request.client else None


def current_principal(
    creds: HTTPAuthorizationCredentials | None = Depends(_bearer), db: Session = Depends(get_db)
) -> Principal:
    unauth = HTTPException(status.HTTP_401_UNAUTHORIZED, "Not authenticated", headers={"WWW-Authenticate": "Bearer"})
    if creds is None:
        raise unauth
    try:
        claims = decode_access_token(creds.credentials)
    except jwt.PyJWTError:
        raise unauth
    session = db.scalar(select(AuthSession).where(AuthSession.jti == claims["jti"]))
    now = datetime.now(timezone.utc)
    if session is None or session.revoked_at is not None or session.expires_at <= now:
        raise unauth
    user = db.get(User, int(claims["sub"]))
    if user is None or not user.is_active or not user.role.is_active:
        raise unauth
    return Principal(user, claims["jti"])


def require(*permissions: str):
    """Dependency factory: caller must hold ALL listed permissions."""

    def checker(p: Principal = Depends(current_principal)) -> Principal:
        missing = [x for x in permissions if not p.has(x)]
        if missing:
            raise HTTPException(status.HTTP_403_FORBIDDEN, "You do not have permission to perform this action")
        return p

    checker.required, checker.mode = permissions, "all"      # read by the permission sweep in the tests, never by request handling
    return checker


def require_any(*permissions: str):
    def checker(p: Principal = Depends(current_principal)) -> Principal:
        if not any(p.has(x) for x in permissions):
            raise HTTPException(status.HTTP_403_FORBIDDEN, "You do not have permission to perform this action")
        return p

    checker.required, checker.mode = permissions, "any"
    return checker


def ensure_location_access(p: Principal, location_id: int) -> None:
    """Server-side location check; 404 (not 403) so out-of-scope ids are not confirmed to exist."""
    if not can_access_location(p.user, location_id):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Not found")
