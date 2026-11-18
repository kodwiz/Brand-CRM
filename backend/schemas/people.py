from datetime import date, time
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field


class _In(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class EmployeeIn(_In):
    full_name: str = Field(min_length=1, max_length=120)
    location_id: int
    designation: str | None = Field(default=None, max_length=80)
    phone: str | None = Field(default=None, max_length=40)
    user_id: int | None = None
    device_user_id: str | None = Field(default=None, max_length=40)
    joined_on: date | None = None
    credit_limit: Decimal = Field(default=Decimal(0), ge=0)
    notes: str | None = Field(default=None, max_length=1000)


class EmployeePatch(_In):
    full_name: str | None = Field(default=None, max_length=120)
    location_id: int | None = None
    designation: str | None = Field(default=None, max_length=80)
    phone: str | None = Field(default=None, max_length=40)
    user_id: int | None = None
    device_user_id: str | None = Field(default=None, max_length=40)
    joined_on: date | None = None
    left_on: date | None = None
    status: str | None = None
    credit_limit: Decimal | None = Field(default=None, ge=0)
    notes: str | None = Field(default=None, max_length=1000)
    reason: str | None = Field(default=None, max_length=500)


class EmployeeSettingsIn(_In):
    credit_allowed: bool | None = None
    monthly_purchase_cap: Decimal | None = Field(default=None, ge=0)
    reason: str | None = Field(default=None, max_length=500)


class RuleIn(_In):
    name: str = Field(min_length=1, max_length=80)
    category_id: int | None = None
    pct: Decimal = Field(gt=0, le=100)


class RulePatch(_In):
    name: str | None = Field(default=None, max_length=80)
    pct: Decimal | None = Field(default=None, gt=0, le=100)
    is_active: bool | None = None
    reason: str | None = Field(default=None, max_length=500)


class PurchaseLineIn(_In):
    variant_id: int
    qty: int = Field(gt=0, le=100000)


class QuoteIn(_In):
    lines: list[PurchaseLineIn] = Field(min_length=1)


class PurchaseIn(_In):
    employee_id: int
    location_id: int
    purchase_date: date
    lines: list[PurchaseLineIn] = Field(min_length=1)
    received_now: Decimal = Field(default=Decimal(0), ge=0)
    method: str | None = None
    reference: str | None = Field(default=None, max_length=80)
    receipt_ref: str | None = Field(default=None, max_length=60)
    notes: str | None = Field(default=None, max_length=1000)
    idempotency_key: str | None = Field(default=None, max_length=60)


class DecisionIn(_In):
    approve: bool
    note: str | None = Field(default=None, max_length=500)


class ReasonIn(_In):
    reason: str = Field(min_length=1, max_length=500)


class PaymentIn(_In):
    amount: Decimal = Field(gt=0)
    method: str
    paid_date: date
    reference: str | None = Field(default=None, max_length=80)
    idempotency_key: str | None = Field(default=None, max_length=60)


# ---- attendance ----
class DeviceIn(_In):
    name: str = Field(min_length=1, max_length=80)
    location_id: int
    adapter: str
    brand: str | None = Field(default=None, max_length=80)
    model: str | None = Field(default=None, max_length=80)
    protocol: str | None = Field(default=None, max_length=80)
    export_format: str | None = Field(default=None, max_length=80)
    config: dict = Field(default_factory=dict)


class DevicePatch(_In):
    name: str | None = Field(default=None, max_length=80)
    brand: str | None = Field(default=None, max_length=80)
    model: str | None = Field(default=None, max_length=80)
    protocol: str | None = Field(default=None, max_length=80)
    export_format: str | None = Field(default=None, max_length=80)
    config: dict | None = None
    is_active: bool | None = None
    reason: str | None = Field(default=None, max_length=500)


class PolicyIn(_In):
    location_id: int | None = None
    timezone: str = Field(min_length=1, max_length=60)
    shift_start: time
    shift_end: time
    grace_minutes: int = Field(default=0, ge=0, le=600)
    min_present_minutes: int = Field(default=0, ge=0, le=1440)
    overtime_enabled: bool = False
    overtime_after_minutes: int = Field(default=0, ge=0, le=600)
    weekly_off: list[int] = Field(default_factory=list)
    direction_mode: str = "device"
    reason: str | None = Field(default=None, max_length=500)


class CorrectionIn(_In):
    employee_id: int
    work_date: date
    kind: str
    direction: str | None = None
    punch_time: time | None = None
    punch_id: int | None = None
    reason: str = Field(min_length=1, max_length=500)


class RebuildIn(_In):
    date_from: date
    date_to: date
    location_id: int | None = None
