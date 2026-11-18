from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy.orm import Session

from backend.config import get_settings
from backend.db import get_db
from backend.deps import Principal, client_ip, current_principal
from backend.schemas.core import ChangePasswordRequest, LocationOut, LoginRequest, MeOut, TokenResponse
from backend.security import login_limiter
from backend.services import auth_service, location_service
from backend.services.auth_service import AccountLockedError, AuthenticationError
from backend.api.serializers import user_out

router = APIRouter(prefix="/auth", tags=["auth"])


@router.post("/login", response_model=TokenResponse)
def login(body: LoginRequest, request: Request, db: Session = Depends(get_db)):
    ip = client_ip(request)
    if not login_limiter.allow(ip or "unknown", get_settings().login_rate_limit_per_minute):
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, "Too many login attempts. Try again shortly.")
    try:
        user, token, exp = auth_service.authenticate(
            db, username=body.username, password=body.password, ip=ip, user_agent=request.headers.get("user-agent")
        )
    except AccountLockedError as e:
        raise HTTPException(status.HTTP_423_LOCKED, f"Account temporarily locked until {e.until.isoformat()}")
    except AuthenticationError:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid username or password")
    return TokenResponse(access_token=token, expires_at=exp, must_change_password=user.must_change_password)


@router.post("/logout", status_code=204)
def logout(p: Principal = Depends(current_principal), db: Session = Depends(get_db)):
    auth_service.revoke_session(db, jti=p.jti)
    db.commit()


@router.get("/me", response_model=MeOut)
def me(p: Principal = Depends(current_principal), db: Session = Depends(get_db)):
    s = get_settings()
    locs = location_service.list_locations(db, p.user)
    return MeOut(
        user=user_out(p.user),
        permissions=sorted(p.permissions),
        all_locations=p.user.role.scope_all_locations,
        locations=[LocationOut.model_validate(x) for x in locs],
        currency=s.default_currency,
        environment=s.environment,
    )


@router.post("/change-password", status_code=204)
def change_password(body: ChangePasswordRequest, request: Request, p: Principal = Depends(current_principal),
                    db: Session = Depends(get_db)):
    from backend.services.errors import BusinessRuleError

    try:
        auth_service.change_password(db, user=p.user, current_password=body.current_password,
                                     new_password=body.new_password, ip=client_ip(request))
    except BusinessRuleError as e:
        db.rollback()
        raise HTTPException(422, str(e))
    db.commit()
