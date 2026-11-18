from datetime import date
from decimal import Decimal
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class _In(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class CategoryIn(_In):
    name: str = Field(min_length=1, max_length=80)


class VariantIn(_In):
    sku: str = Field(min_length=1, max_length=60)
    name: str = Field(min_length=1, max_length=160)
    barcode: str | None = Field(default=None, max_length=40)
    shade_name: str | None = Field(default=None, max_length=80)
    shade_hex: str | None = Field(default=None, max_length=7)
    size: str | None = Field(default=None, max_length=40)
    purchase_cost: str | Decimal | None = None
    retail_price: str | Decimal | None = None
    employee_price: str | Decimal | None = None
    distributor_price: str | Decimal | None = None
    pricing_status: str | None = None
    reorder_threshold: int = Field(default=0, ge=0)
    status: str = "active"


class VariantUpdate(_In):
    name: str | None = Field(default=None, max_length=160)
    barcode: str | None = Field(default=None, max_length=40)
    shade_name: str | None = Field(default=None, max_length=80)
    shade_hex: str | None = Field(default=None, max_length=7)
    size: str | None = Field(default=None, max_length=40)
    purchase_cost: str | Decimal | None = None
    retail_price: str | Decimal | None = None
    employee_price: str | Decimal | None = None
    distributor_price: str | Decimal | None = None
    pricing_status: str | None = None
    reorder_threshold: int | None = Field(default=None, ge=0)
    status: str | None = None


class ProductIn(_In):
    code: str = Field(min_length=1, max_length=40)
    name: str = Field(min_length=1, max_length=160)
    brand: str | None = Field(default=None, max_length=80)
    category_id: int
    description: str | None = None
    status: str = "active"
    tracks_batches: bool = False
    tracks_expiry: bool = False
    discount_eligible: bool = True
    variants: list[VariantIn] = []


class ProductUpdate(_In):
    name: str | None = Field(default=None, max_length=160)
    brand: str | None = Field(default=None, max_length=80)
    category_id: int | None = None
    description: str | None = None
    status: str | None = None
    tracks_batches: bool | None = None
    tracks_expiry: bool | None = None
    discount_eligible: bool | None = None


class ReceiptLineIn(_In):
    variant_id: int
    batch_no: str | None = Field(default=None, max_length=60)
    expiry_date: date | None = None
    qty_expected: int | None = Field(default=None, ge=0)
    qty_received: int = Field(ge=0)
    qty_accepted: int = Field(ge=0)
    qty_damaged: int = Field(default=0, ge=0)
    qty_rejected: int = Field(default=0, ge=0)
    unit_cost: Decimal | None = Field(default=None, ge=0)  # taken from the purchase order when the receipt has one
    notes: str | None = None


class ReceiptCostIn(_In):
    description: str = Field(min_length=1, max_length=160)
    amount: Decimal = Field(gt=0)


class ReceiptIn(_In):
    purchase_order_id: int | None = None
    supplier_id: int | None = None
    additional_costs: list[ReceiptCostIn] = Field(default_factory=list)
    location_id: int
    received_date: date
    purchase_reference: str | None = Field(default=None, max_length=80)
    supplier_reference: str | None = Field(default=None, max_length=120)
    notes: str | None = None
    lines: list[ReceiptLineIn] = Field(min_length=1)
    post_now: bool = False


class OpeningLine(_In):
    location_id: int
    variant_id: int
    batch_no: str | None = None
    expiry_date: date | None = None
    qty: int = Field(gt=0)
    unit_cost: Decimal = Field(ge=0)


class OpeningIn(_In):
    lines: list[OpeningLine] = Field(min_length=1)
    dry_run: bool = True
    idempotency_key: str | None = Field(default=None, max_length=60)


class MoveIn(_In):
    action: str
    location_id: int
    variant_id: int
    batch_id: int | None = None
    qty: int = Field(gt=0)
    reason: str = Field(min_length=1)
    notes: str | None = None
    idempotency_key: str | None = Field(default=None, max_length=80)


class ReasonIn(_In):
    reason: str = Field(min_length=1)


class NoteIn(_In):
    note: str | None = None


class RequiredNoteIn(_In):
    note: str = Field(min_length=1)


class ChangeLineIn(_In):
    variant_id: int
    batch_id: int | None = None
    bucket: str = "good"
    qty_delta: int
    unit_cost: Decimal | None = Field(default=None, ge=0)


class ChangeRequestIn(_In):
    kind: str
    location_id: int
    reason: str = Field(min_length=1)
    lines: list[ChangeLineIn] = Field(min_length=1)


class TransferLineIn(_In):
    variant_id: int
    batch_id: int | None = None
    qty: int = Field(gt=0)


class TransferIn(_In):
    from_location_id: int
    to_location_id: int
    notes: str | None = None
    lines: list[TransferLineIn] = Field(min_length=1)


class TransferReceiveLine(_In):
    line_id: int
    qty_good: int = Field(default=0, ge=0)
    qty_damaged: int = Field(default=0, ge=0)


class TransferReceiveIn(_In):
    lines: list[TransferReceiveLine] = Field(min_length=1)
    notes: str | None = None


class CountCreateIn(_In):
    location_id: int
    variant_ids: list[int] | None = None
    notes: str | None = None


class CountEntry(_In):
    line_id: int
    counted_qty: int | None = Field(default=None, ge=0)
    reason: str | None = None


class CountEntriesIn(_In):
    entries: list[CountEntry]


class CostingIn(_In):
    method: str
    reason: str | None = None


class ThresholdsIn(_In):
    adjustment_auto_approve_value: Decimal | None = Field(default=None, ge=0)
    expiry_warning_days: int | None = Field(default=None, ge=1, le=730)
    reason: str | None = None


class ImportCommitIn(_In):
    confirm: bool
