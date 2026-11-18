from datetime import datetime
from typing import Any, Generic, Literal, TypeVar

from pydantic import BaseModel, ConfigDict, Field

T = TypeVar("T")


class Page(BaseModel, Generic[T]):
    items: list[T]
    total: int
    page: int
    page_size: int


class LoginRequest(BaseModel):
    username: str = Field(min_length=1, max_length=60)
    password: str = Field(min_length=1, max_length=200)


class TokenResponse(BaseModel):
    access_token: str
    token_type: Literal["bearer"] = "bearer"
    expires_at: datetime
    must_change_password: bool


class ChangePasswordRequest(BaseModel):
    current_password: str
    new_password: str = Field(max_length=200)


class RoleOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    code: str
    name: str
    description: str | None
    scope_all_locations: bool
    is_system: bool
    is_active: bool
    permissions: list[str] = []


class LocationOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    code: str
    name: str
    city: str
    address: str | None
    location_type: str
    is_active: bool


class LocationCreate(BaseModel):
    code: str = Field(min_length=2, max_length=20)
    name: str = Field(min_length=1, max_length=120)
    city: str = Field(min_length=1, max_length=80)
    address: str | None = None
    location_type: str = "branch"


class LocationUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=120)
    city: str | None = Field(default=None, min_length=1, max_length=80)
    address: str | None = None
    location_type: str | None = None
    is_active: bool | None = None
    reason: str | None = None


class UserOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    username: str
    full_name: str
    email: str | None
    phone: str | None
    role_code: str
    role_name: str
    is_active: bool
    must_change_password: bool
    last_login_at: datetime | None
    location_ids: list[int]


class UserCreate(BaseModel):
    username: str = Field(min_length=3, max_length=60, pattern=r"^[A-Za-z0-9._-]+$")
    full_name: str = Field(min_length=1, max_length=120)
    password: str = Field(max_length=200)
    role_id: int
    email: str | None = None
    phone: str | None = None
    location_ids: list[int] = []


class UserUpdate(BaseModel):
    full_name: str | None = Field(default=None, min_length=1, max_length=120)
    email: str | None = None
    phone: str | None = None
    role_id: int | None = None
    is_active: bool | None = None
    reason: str | None = None


class UserLocationsUpdate(BaseModel):
    location_ids: list[int]
    reason: str | None = None


class PasswordReset(BaseModel):
    new_password: str = Field(max_length=200)


class RolePermissionsUpdate(BaseModel):
    permissions: list[str]
    reason: str | None = None


class MeOut(BaseModel):
    user: UserOut
    permissions: list[str]
    all_locations: bool
    locations: list[LocationOut]
    currency: str
    environment: str


class AuditOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    created_at: datetime
    actor_user_id: int | None
    actor_username: str | None
    action: str
    entity_type: str
    entity_id: str | None
    location_id: int | None
    reason: str | None
    before: dict[str, Any] | None
    after: dict[str, Any] | None
    ip_address: str | None
