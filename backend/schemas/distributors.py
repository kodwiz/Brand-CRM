from datetime import date
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field


class _In(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class DistributorIn(_In):
    name: str = Field(min_length=1, max_length=160)
    contact_name: str | None = Field(default=None, max_length=120)
    phone: str | None = Field(default=None, max_length=40)
    email: str | None = Field(default=None, max_length=160)
    address: str | None = None
    city: str | None = Field(default=None, max_length=80)
    discount_pct: Decimal = Field(default=Decimal(0), ge=0, le=100)
    credit_limit: Decimal = Field(default=Decimal(0), ge=0)
    payment_terms_days: int = Field(default=30, ge=0, le=365)
    assigned_user_id: int | None = None
    notes: str | None = None


class DistributorUpdate(_In):
    name: str | None = Field(default=None, min_length=1, max_length=160)
    contact_name: str | None = None
    phone: str | None = None
    email: str | None = None
    address: str | None = None
    city: str | None = None
    discount_pct: Decimal | None = Field(default=None, ge=0, le=100)
    credit_limit: Decimal | None = Field(default=None, ge=0)
    payment_terms_days: int | None = Field(default=None, ge=0, le=365)
    assigned_user_id: int | None = None
    notes: str | None = None
    is_active: bool | None = None


class PriceIn(_In):
    price: Decimal | None = Field(default=None, ge=0)        # null removes the agreed price


class DispatchLineIn(_In):
    variant_id: int
    qty: int = Field(gt=0)
    unit_price: Decimal | None = Field(default=None, ge=0)
    batch_id: int | None = None


class DispatchIn(_In):
    distributor_id: int
    location_id: int
    invoice_date: date | None = None
    lines: list[DispatchLineIn] = Field(min_length=1)
    notes: str | None = None
    exchange_return_id: int | None = None
    idempotency_key: str | None = Field(default=None, max_length=60)
    post_now: bool = False


class DispatchUpdate(_In):
    location_id: int | None = None
    invoice_date: date | None = None
    lines: list[DispatchLineIn] | None = Field(default=None, min_length=1)
    notes: str | None = None


class DecisionIn(_In):
    approve: bool
    note: str = Field(min_length=1)


class ReasonIn(_In):
    reason: str = Field(min_length=1)


class OpeningIn(_In):
    amount: Decimal = Field(gt=0)
    as_of: date
    note: str | None = None


class ReturnLineIn(_In):
    invoice_line_id: int
    qty: int = Field(gt=0)


class ReturnIn(_In):
    distributor_id: int
    location_id: int
    return_date: date | None = None
    reason: str = Field(min_length=1)
    lines: list[ReturnLineIn] = Field(min_length=1)


class ReceiveLineIn(_In):
    line_id: int
    qty_received: int = Field(ge=0)


class ReceiveReturnIn(_In):
    lines: list[ReceiveLineIn] = Field(min_length=1)


class InspectLineIn(_In):
    line_id: int
    restock: int = Field(default=0, ge=0)
    damaged: int = Field(default=0, ge=0)
    expired: int = Field(default=0, ge=0)
    credit_qty: int | None = Field(default=None, ge=0)
    note: str | None = Field(default=None, max_length=200)


class InspectIn(_In):
    lines: list[InspectLineIn] = Field(min_length=1)


class AllocationIn(_In):
    invoice_id: int
    amount: Decimal = Field(gt=0)


class PaymentIn(_In):
    distributor_id: int
    amount: Decimal = Field(gt=0)
    method: str
    paid_date: date | None = None
    reference: str | None = Field(default=None, max_length=80)
    allocations: list[AllocationIn] = Field(default_factory=list)
    auto_allocate: bool = False
    notes: str | None = None
    idempotency_key: str | None = Field(default=None, max_length=60)


class AllocateIn(_In):
    source_type: str
    source_id: int
    allocations: list[AllocationIn] = Field(min_length=1)


class AdjustmentIn(_In):
    distributor_id: int
    kind: str
    amount: Decimal = Field(gt=0)
    entry_date: date | None = None
    invoice_id: int | None = None
    reason: str = Field(min_length=1)


class DistributorSettingsIn(_In):
    backdate_days: int | None = Field(default=None, ge=0, le=365)
    max_extra_discount_pct: Decimal | None = Field(default=None, ge=0, le=100)
