from datetime import date
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field


class _In(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class AddressIn(_In):
    kind: str = "delivery"
    label: str | None = Field(default=None, max_length=60)
    line: str = Field(min_length=1)
    city: str | None = Field(default=None, max_length=80)
    is_default: bool = False


class CustomerIn(_In):
    name: str = Field(min_length=1, max_length=160)
    phone: str = Field(min_length=1, max_length=40)
    email: str | None = Field(default=None, max_length=160)
    category: str = Field(default="retail", min_length=1, max_length=30)
    notes: str | None = None
    addresses: list[AddressIn] = Field(default_factory=list)


class CustomerUpdate(_In):
    name: str | None = Field(default=None, max_length=160)
    phone: str | None = Field(default=None, max_length=40)
    email: str | None = None
    category: str | None = None
    notes: str | None = None
    status: str | None = None
    addresses: list[AddressIn] | None = None


class ChannelIn(_In):
    code: str = Field(min_length=1, max_length=30, pattern=r"^[a-z0-9_]+$")
    name: str = Field(min_length=1, max_length=80)
    is_active: bool = True


class ChannelUpdate(_In):
    name: str | None = Field(default=None, max_length=80)
    is_active: bool | None = None


class PromotionIn(_In):
    name: str = Field(min_length=1, max_length=120)
    kind: str
    value: Decimal = Field(gt=0)
    starts_on: date
    ends_on: date | None = None
    variant_id: int | None = None
    product_id: int | None = None
    category_id: int | None = None
    customer_category: str | None = None
    min_qty: int = Field(default=1, ge=1)
    min_order_value: Decimal | None = Field(default=None, ge=0)
    max_discount_pct: Decimal | None = Field(default=None, gt=0, le=100)
    stackable: bool = False
    is_active: bool = True


class PromotionUpdate(_In):
    name: str | None = None
    value: Decimal | None = Field(default=None, gt=0)
    starts_on: date | None = None
    ends_on: date | None = None
    min_qty: int | None = Field(default=None, ge=1)
    min_order_value: Decimal | None = None
    max_discount_pct: Decimal | None = None
    stackable: bool | None = None
    is_active: bool | None = None


class OrderLineIn(_In):
    variant_id: int
    qty: int = Field(gt=0)
    unit_price: Decimal | None = Field(default=None, ge=0)
    notes: str | None = Field(default=None, max_length=200)


class DeliveryIn(_In):
    name: str | None = Field(default=None, max_length=160)
    phone: str | None = Field(default=None, max_length=40)
    address: str | None = None
    city: str | None = Field(default=None, max_length=80)
    billing_address: str | None = None
    courier: str | None = Field(default=None, max_length=80)


class OrderIn(_In):
    customer_id: int
    channel_id: int
    location_id: int
    fulfilment_location_id: int | None = None
    order_date: date | None = None
    payment_terms: str = "cod"
    delivery: DeliveryIn = Field(default_factory=DeliveryIn)
    delivery_fee: Decimal = Field(default=Decimal(0), ge=0)
    notes: str | None = None
    lines: list[OrderLineIn] = Field(min_length=1)
    idempotency_key: str | None = Field(default=None, max_length=60)
    confirm_now: bool = False


class OrderUpdate(_In):
    customer_id: int | None = None
    channel_id: int | None = None
    location_id: int | None = None
    fulfilment_location_id: int | None = None
    order_date: date | None = None
    payment_terms: str | None = None
    delivery: DeliveryIn | None = None
    delivery_fee: Decimal | None = Field(default=None, ge=0)
    notes: str | None = None
    lines: list[OrderLineIn] | None = None


class QuoteLineIn(_In):
    variant_id: int
    qty: int = Field(gt=0)
    unit_price: Decimal | None = Field(default=None, ge=0)


class QuoteIn(_In):
    customer_id: int
    order_date: date | None = None
    delivery_fee: Decimal = Field(default=Decimal(0), ge=0)
    lines: list[QuoteLineIn] = Field(min_length=1)


class DecisionIn(_In):
    approve: bool
    note: str = Field(min_length=1)


class ReasonIn(_In):
    reason: str = Field(min_length=1)


class PackLineIn(_In):
    order_line_id: int
    qty: int = Field(gt=0)


class PackIn(_In):
    lines: list[PackLineIn] = Field(min_length=1)
    notes: str | None = None


class DispatchIn(_In):
    courier: str | None = Field(default=None, max_length=80)
    tracking_no: str | None = Field(default=None, max_length=80)


class OutcomeIn(_In):
    result: str
    reason: str | None = None


class ReceiptIn(_In):
    amount: Decimal = Field(gt=0)
    method: str
    paid_date: date | None = None
    reference: str | None = Field(default=None, max_length=80)
    notes: str | None = None
    idempotency_key: str | None = Field(default=None, max_length=60)


class RefundIn(_In):
    amount: Decimal = Field(gt=0)
    method: str
    reference: str | None = Field(default=None, max_length=80)
    reason: str = Field(min_length=1)


class ReturnLineIn(_In):
    order_line_id: int
    qty: int = Field(gt=0)


class ReturnIn(_In):
    lines: list[ReturnLineIn] = Field(min_length=1)
    reason: str = Field(min_length=1)
    refund_mode: str = "refund"
    refund_delivery_fee: bool = False


class ReturnDecisionIn(_In):
    approve: bool
    note: str | None = None


class ReceiveLineIn(_In):
    line_id: int
    qty_received: int = Field(ge=0)
    batch_id: int | None = None


class ReceiveReturnIn(_In):
    lines: list[ReceiveLineIn] = Field(min_length=1)


class InspectLineIn(_In):
    line_id: int
    restock: int = Field(default=0, ge=0)
    damaged: int = Field(default=0, ge=0)
    expired: int = Field(default=0, ge=0)
    refund_qty: int | None = Field(default=None, ge=0)
    note: str | None = None


class InspectIn(_In):
    lines: list[InspectLineIn] = Field(min_length=1)


class SettleIn(_In):
    refund_amount: Decimal | None = Field(default=None, ge=0)
    method: str | None = None
    reference: str | None = Field(default=None, max_length=80)


class SalesSettingsIn(_In):
    max_manual_discount_pct: Decimal | None = Field(default=None, ge=0, le=100)
    tax_rate_pct: Decimal | None = Field(default=None, ge=0, le=100)
    return_window_days: int | None = Field(default=None, ge=0, le=3650)
