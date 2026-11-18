from datetime import date
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field


class _In(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class SupplierIn(_In):
    code: str | None = Field(default=None, max_length=30)
    name: str = Field(min_length=1, max_length=160)
    contact_name: str | None = Field(default=None, max_length=120)
    phone: str | None = Field(default=None, max_length=40)
    email: str | None = Field(default=None, max_length=160)
    address: str | None = None
    city: str | None = Field(default=None, max_length=80)
    tax_number: str | None = Field(default=None, max_length=60)
    payment_terms_days: int = Field(default=0, ge=0, le=365)
    currency: str = Field(default="PKR", min_length=3, max_length=3)
    notes: str | None = None


class SupplierUpdate(_In):
    name: str | None = Field(default=None, min_length=1, max_length=160)
    contact_name: str | None = None
    phone: str | None = None
    email: str | None = None
    address: str | None = None
    city: str | None = None
    tax_number: str | None = None
    payment_terms_days: int | None = Field(default=None, ge=0, le=365)
    notes: str | None = None
    is_active: bool | None = None


class POLineIn(_In):
    variant_id: int
    qty_ordered: int = Field(gt=0)
    unit_cost: Decimal = Field(ge=0)
    notes: str | None = None


class POIn(_In):
    supplier_id: int
    location_id: int
    order_date: date
    expected_date: date | None = None
    supplier_reference: str | None = Field(default=None, max_length=80)
    notes: str | None = None
    lines: list[POLineIn] = Field(min_length=1)
    approve_now: bool = False


class POUpdate(_In):
    supplier_id: int | None = None
    location_id: int | None = None
    order_date: date | None = None
    expected_date: date | None = None
    supplier_reference: str | None = None
    notes: str | None = None
    lines: list[POLineIn] | None = None


class ReasonIn(_In):
    reason: str = Field(min_length=1)


class NoteIn(_In):
    note: str | None = None


class InvoiceLineIn(_In):
    po_line_id: int | None = None
    variant_id: int | None = None
    description: str | None = Field(default=None, max_length=200)
    qty: int = Field(gt=0)
    unit_price: Decimal = Field(ge=0)


class ChargeIn(_In):
    description: str = Field(min_length=1, max_length=160)
    amount: Decimal = Field(gt=0)


class InvoiceIn(_In):
    supplier_id: int
    invoice_no: str = Field(min_length=1, max_length=60)
    invoice_date: date
    due_date: date | None = None
    purchase_order_id: int | None = None
    lines: list[InvoiceLineIn] = Field(min_length=1)
    charges: list[ChargeIn] = Field(default_factory=list)
    discount: Decimal = Field(default=Decimal(0), ge=0)
    tax: Decimal = Field(default=Decimal(0), ge=0)
    notes: str | None = None
    post_now: bool = True


class DecisionIn(_In):
    approve: bool
    note: str | None = None


class AllocationIn(_In):
    invoice_id: int
    amount: Decimal = Field(gt=0)


class PaymentIn(_In):
    supplier_id: int
    amount: Decimal = Field(gt=0)
    method: str
    paid_date: date
    reference: str | None = Field(default=None, max_length=80)
    notes: str | None = None
    allocations: list[AllocationIn] = Field(default_factory=list)
    on_account: bool = False
    idempotency_key: str | None = Field(default=None, max_length=60)


class AllocateIn(_In):
    allocations: list[AllocationIn] = Field(min_length=1)


class AdjustmentIn(_In):
    supplier_id: int
    kind: str
    amount: Decimal = Field(gt=0)
    adjustment_date: date
    document_no: str | None = Field(default=None, max_length=60)
    reason: str = Field(min_length=1)
    invoice_id: int | None = None
    supplier_return_id: int | None = None


class ReturnLineIn(_In):
    variant_id: int
    batch_id: int | None = None
    bucket: str = "damaged"
    qty: int = Field(gt=0)


class ReturnIn(_In):
    supplier_id: int
    location_id: int
    reason: str = Field(min_length=1)
    notes: str | None = None
    purchase_order_id: int | None = None
    receipt_id: int | None = None
    lines: list[ReturnLineIn] = Field(min_length=1)
    dispatch_now: bool = False


class PurchasingSettingsIn(_In):
    po_self_approve_limit: Decimal | None = Field(default=None, ge=0)
    receipt_over_tolerance_pct: Decimal | None = Field(default=None, ge=0, le=100)
    invoice_price_tolerance_pct: Decimal | None = Field(default=None, ge=0, le=100)
    invoice_qty_tolerance_pct: Decimal | None = Field(default=None, ge=0, le=100)
    reason: str | None = None


class SupplierOpeningIn(_In):
    amount: Decimal = Field(gt=0)
    as_of: date
    note: str | None = Field(default=None, max_length=200)
