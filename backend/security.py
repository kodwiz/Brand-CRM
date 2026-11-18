"""Password hashing and JWT helpers."""
import secrets
import time
from collections import defaultdict, deque
from datetime import datetime, timedelta, timezone

import jwt
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError

from backend.config import get_settings

_hasher = PasswordHasher()
_DUMMY_HASH = _hasher.hash("timing-equalisation-placeholder")

ALGORITHM = "HS256"


def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(password: str, password_hash: str | None) -> bool:
    """Constant-ish time: verifies against a dummy hash when the user does not exist."""
    try:
        return _hasher.verify(password_hash or _DUMMY_HASH, password) and password_hash is not None
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False


def password_problems(password: str) -> list[str]:
    s = get_settings()
    problems = []
    if len(password) < s.min_password_length:
        problems.append(f"must be at least {s.min_password_length} characters")
    if password.lower() == password or password.upper() == password:
        problems.append("must mix upper and lower case letters")
    if not any(c.isdigit() for c in password):
        problems.append("must contain a digit")
    return problems


def new_jti() -> str:
    return secrets.token_urlsafe(32)


def create_access_token(user_id: int, jti: str) -> tuple[str, datetime]:
    s = get_settings()
    now = datetime.now(timezone.utc)
    exp = now + timedelta(minutes=s.access_token_minutes)
    token = jwt.encode({"sub": str(user_id), "jti": jti, "iat": now, "exp": exp}, s.jwt_secret, algorithm=ALGORITHM)
    return token, exp


def decode_access_token(token: str) -> dict:
    """Raises jwt.PyJWTError on any problem (bad signature, expired, malformed)."""
    return jwt.decode(token, get_settings().jwt_secret, algorithms=[ALGORITHM], options={"require": ["exp", "sub", "jti"]})


class SlidingWindowLimiter:
    """Small in-process limiter for the login endpoint (per client IP).

    Adequate for a single backend process; put a reverse proxy limit in front for
    multi-process deployments (documented in docs/deployment.md).
    """

    def __init__(self) -> None:
        self._hits: dict[str, deque[float]] = defaultdict(deque)

    def allow(self, key: str, limit: int, window_seconds: int = 60) -> bool:
        now = time.monotonic()
        q = self._hits[key]
        while q and now - q[0] > window_seconds:
            q.popleft()
        if len(q) >= limit:
            return False
        q.append(now)
        return True

    def reset(self) -> None:
        self._hits.clear()


login_limiter = SlidingWindowLimiter()
