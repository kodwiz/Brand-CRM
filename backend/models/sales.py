"""Phase 4 tables: customers, sales channels, promotions, orders (lines, history, shipments, payments), customer returns
and customer store credit.

Prices are snapshotted on the order line when the order is confirmed; later price or promotion changes never alter an
existing order. Order status is derived from shipments and returns (see sales_service.refresh_status); payment status is
derived from posted receipts and refunds. Nothing here posts to an accounting ledger (Phase 6).
"""
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import (
    Boolean, CheckConstraint, Date, DateTime, ForeignKey, Index, Integer, Numeric, String, Text, UniqueConstraint, func, text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from backend.db import Base

ORDER_STATUSES = ("draft", "confirmed", "packed", "dispatched", "partially_delivered", "delivered", "delivery_failed",
                  "refused", "cancelled", "partially_returned", "returned")
SHIPMENT_STATUSES = ("packed", "dispatched", "delivered", "failed", "refused", "received_back", "cancelled")
RETURN_STATUSES = ("requested", "approved", "rejected", "received", "inspected", "settled", "cancelled")
RECEIPT_METHODS = ("cash", "bank_transfer", "cod_courier", "card", "store_credit", "other")


def _created():
    return mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class SalesChannel(Base):
    __tablename__ = "sales_channels"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    code: Mapped[str] = mapped_column(String(30), unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String(80), nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    sort_order: Mapped[int] = mapped_column(Integer, default=0, nullable=False)


class Customer(Base):
    __tablename__ = "customers"
    __table_args__ = (
        CheckConstraint("status in ('active','inactive')", name="status_valid"),
        Index("uq_customers_phone", "phone_norm", unique=True),
        Index("ix_customers_name", text("lower(name)")),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    code: Mapped[str] = mapped_column(String(20), unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    phone: Mapped[str] = mapped_column(String(40), nullable=False)
    phone_norm: Mapped[str] = mapped_column(String(20), nullable=False)  # digits only, used for duplicate detection
    email: Mapped[str | None] = mapped_column(String(160))
    category: Mapped[str] = mapped_column(String(30), default="retail", nullable=False)
    notes: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(10), default="active", nullable=False)
    created_by: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    created_at: Mapped[datetime] = _created()
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)

    addresses: Mapped[list["CustomerAddress"]] = relationship(back_populates="customer", cascade="all, delete-orphan", order_by="CustomerAddress.id")


class CustomerAddress(Base):
    __tablename__ = "customer_addresses"
    __table_args__ = (CheckConstraint("kind in ('delivery','billing')", name="kind_valid"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    customer_id: Mapped[int] = mapped_column(ForeignKey("customers.id", ondelete="CASCADE"), nullable=False)
    kind: Mapped[str] = mapped_column(String(10), default="delivery", nullable=False)
    label: Mapped[str | None] = mapped_column(String(60))
    line: Mapped[str] = mapped_column(Text, nullable=False)
    city: Mapped[str | None] = mapped_column(String(80))
    is_default: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    customer: Mapped[Customer] = relationship(back_populates="addresses")


class Promotion(Base):
    """A time-limited discount rule. Empty product fields mean every product; empty customer_category means every customer."""

    __tablename__ = "promotions"
    __table_args__ = (
        CheckConstraint("kind in ('percent','fixed_amount')", name="kind_valid"),
        CheckConstraint("value > 0", name="value_positive"),
        CheckConstraint("kind <> 'percent' or value <= 100", name="percent_range"),
        CheckConstraint("min_qty >= 1", name="min_qty_valid"),
        CheckConstraint("ends_on is null or ends_on >= starts_on", name="dates_valid"),
        CheckConstraint("max_discount_pct is null or (max_discount_pct > 0 and max_discount_pct <= 100)", name="cap_range"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    kind: Mapped[str] = mapped_column(String(12), nullable=False)
    value: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False)       # percent, or amount off per unit
    starts_on: Mapped[date] = mapped_column(Date, nullable=False)
    ends_on: Mapped[date | None] = mapped_column(Date)
    variant_id: Mapped[int | None] = mapped_column(ForeignKey("product_variants.id"))
    product_id: Mapped[int | None] = mapped_column(ForeignKey("products.id"))
    category_id: Mapped[int | None] = mapped_column(ForeignKey("categories.id"))
    customer_category: Mapped[str | None] = mapped_column(String(30))
    min_qty: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    min_order_value: Mapped[Decimal | None] = mapped_column(Numeric(14, 2))
    max_discount_pct: Mapped[Decimal | None] = mapped_column(Numeric(5, 2))     # cap on the total promotional discount of a line
    stackable: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_by: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    created_at: Mapped[datetime] = _created()


# ---- orders -------------------------------------------------------------------------------------------------
class Order(Base):
    __tablename__ = "orders"
    __table_args__ = (
        CheckConstraint("status in ('draft','confirmed','packed','dispatched','partially_delivered','delivered','delivery_failed',"
                        "'refused','cancelled','partially_returned','returned')", name="status_valid"),
        CheckConstraint("payment_terms in ('prepaid','cod','credit')", name="terms_valid"),
        CheckConstraint("discount_approval in ('none','pending','approved','rejected')", name="approval_valid"),
        CheckConstraint("delivery_fee >= 0 and tax_total >= 0 and total >= 0", name="amounts_non_negative"),
        Index("ix_orders_customer", "customer_id"),
        Index("ix_orders_booker", "created_by"),
        Index("ix_orders_status", "status"),
        Index("uq_orders_idem", "idempotency_key", unique=True),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    ref: Mapped[str] = mapped_column(String(20), unique=True, nullable=False)
    customer_id: Mapped[int] = mapped_column(ForeignKey("customers.id"), nullable=False)
    channel_id: Mapped[int] = mapped_column(ForeignKey("sales_channels.id"), nullable=False)
    location_id: Mapped[int] = mapped_column(ForeignKey("locations.id"), nullable=False)               # where the order was taken
    fulfilment_location_id: Mapped[int] = mapped_column(ForeignKey("locations.id"), nullable=False)    # where stock is reserved/shipped from
    order_date: Mapped[date] = mapped_column(Date, nullable=False)
    status: Mapped[str] = mapped_column(String(22), default="draft", nullable=False)
    payment_terms: Mapped[str] = mapped_column(String(10), default="cod", nullable=False)
    delivery_name: Mapped[str | None] = mapped_column(String(160))
    delivery_phone: Mapped[str | None] = mapped_column(String(40))
    delivery_address: Mapped[str | None] = mapped_column(Text)
    delivery_city: Mapped[str | None] = mapped_column(String(80))
    billing_address: Mapped[str | None] = mapped_column(Text)
    courier: Mapped[str | None] = mapped_column(String(80))
    tracking_no: Mapped[str | None] = mapped_column(String(80))
    notes: Mapped[str | None] = mapped_column(Text)
    delivery_fee: Mapped[Decimal] = mapped_column(Numeric(14, 2), default=0, nullable=False)
    tax_rate_pct: Mapped[Decimal] = mapped_column(Numeric(5, 2), default=0, nullable=False)   # snapshot of the setting at confirmation
    subtotal_list: Mapped[Decimal] = mapped_column(Numeric(14, 2), default=0, nullable=False)  # at catalogue prices
    discount_total: Mapped[Decimal] = mapped_column(Numeric(14, 2), default=0, nullable=False)
    tax_total: Mapped[Decimal] = mapped_column(Numeric(14, 2), default=0, nullable=False)
    total: Mapped[Decimal] = mapped_column(Numeric(14, 2), default=0, nullable=False)
    manual_discount_pct: Mapped[Decimal] = mapped_column(Numeric(6, 2), default=0, nullable=False)  # largest manual discount on any line
    discount_approval: Mapped[str] = mapped_column(String(10), default="none", nullable=False)
    discount_approved_by: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    discount_approval_note: Mapped[str | None] = mapped_column(Text)
    idempotency_key: Mapped[str | None] = mapped_column(String(60))
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    cancel_reason: Mapped[str | None] = mapped_column(Text)
    created_by: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    created_at: Mapped[datetime] = _created()
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)

    lines: Mapped[list["OrderLine"]] = relationship(back_populates="order", cascade="all, delete-orphan", order_by="OrderLine.id")
    events: Mapped[list["OrderEvent"]] = relationship(back_populates="order", cascade="all, delete-orphan", order_by="OrderEvent.id")
    shipments: Mapped[list["Shipment"]] = relationship(back_populates="order", order_by="Shipment.id")


class OrderLine(Base):
    __tablename__ = "order_lines"
    __table_args__ = (
        CheckConstraint("qty > 0", name="qty_positive"),
        CheckConstraint("list_price >= 0 and unit_price >= 0 and promo_discount >= 0", name="amounts_non_negative"),
        CheckConstraint("unit_price <= list_price", name="price_not_above_list"),
        UniqueConstraint("order_id", "variant_id", name="uq_order_lines_item"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    order_id: Mapped[int] = mapped_column(ForeignKey("orders.id", ondelete="CASCADE"), nullable=False)
    variant_id: Mapped[int] = mapped_column(ForeignKey("product_variants.id"), nullable=False)
    qty: Mapped[int] = mapped_column(Integer, nullable=False)
    list_price: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False)       # catalogue retail price when priced
    promo_discount: Mapped[Decimal] = mapped_column(Numeric(14, 2), default=0, nullable=False)   # per unit, from promotions
    unit_price: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False)       # agreed price per unit
    promotion_ids: Mapped[str | None] = mapped_column(String(120))                    # comma separated, for the record
    promotion_names: Mapped[str | None] = mapped_column(String(300))
    notes: Mapped[str | None] = mapped_column(String(200))

    order: Mapped[Order] = relationship(back_populates="lines")


class OrderEvent(Base):
    __tablename__ = "order_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    order_id: Mapped[int] = mapped_column(ForeignKey("orders.id", ondelete="CASCADE"), nullable=False)
    event: Mapped[str] = mapped_column(String(40), nullable=False)
    actor_user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    actor_username: Mapped[str | None] = mapped_column(String(60))
    note: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = _created()

    order: Mapped[Order] = relationship(back_populates="events")


class Shipment(Base):
    """One physical parcel of an order. An order may have several (partial deliveries)."""

    __tablename__ = "shipments"
    __table_args__ = (
        CheckConstraint("status in ('packed','dispatched','delivered','failed','refused','received_back','cancelled')", name="status_valid"),
        Index("ix_shipments_order", "order_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    ref: Mapped[str] = mapped_column(String(20), unique=True, nullable=False)
    order_id: Mapped[int] = mapped_column(ForeignKey("orders.id"), nullable=False)
    location_id: Mapped[int] = mapped_column(ForeignKey("locations.id"), nullable=False)
    status: Mapped[str] = mapped_column(String(14), default="packed", nullable=False)
    courier: Mapped[str | None] = mapped_column(String(80))
    tracking_no: Mapped[str | None] = mapped_column(String(80))
    attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    outcome: Mapped[str | None] = mapped_column(String(10))      # failed | refused, kept after the goods are received back
    outcome_reason: Mapped[str | None] = mapped_column(Text)
    packed_by: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    packed_at: Mapped[datetime] = _created()
    dispatched_by: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    dispatched_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    received_back_by: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    received_back_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    stock_txn_id: Mapped[int | None] = mapped_column(ForeignKey("stock_transactions.id"))
    return_txn_id: Mapped[int | None] = mapped_column(ForeignKey("stock_transactions.id"))

    order: Mapped[Order] = relationship(back_populates="shipments")
    lines: Mapped[list["ShipmentLine"]] = relationship(back_populates="shipment", cascade="all, delete-orphan", order_by="ShipmentLine.id")


class ShipmentLine(Base):
    """Packed quantity per order line; at dispatch it is split per batch with the cost of goods that left."""

    __tablename__ = "shipment_lines"
    __table_args__ = (CheckConstraint("qty > 0", name="qty_positive"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    shipment_id: Mapped[int] = mapped_column(ForeignKey("shipments.id", ondelete="CASCADE"), nullable=False)
    order_line_id: Mapped[int] = mapped_column(ForeignKey("order_lines.id"), nullable=False)
    variant_id: Mapped[int] = mapped_column(ForeignKey("product_variants.id"), nullable=False)
    batch_id: Mapped[int | None] = mapped_column(ForeignKey("batches.id"))
    qty: Mapped[int] = mapped_column(Integer, nullable=False)
    stock_value: Mapped[Decimal | None] = mapped_column(Numeric(18, 4))   # cost of goods issued, set at dispatch

    shipment: Mapped[Shipment] = relationship(back_populates="lines")


class OrderPayment(Base):
    """Customer receipts and refunds. Refunds and receipts are separate rows; nothing is edited, only reversed."""

    __tablename__ = "order_payments"
    __table_args__ = (
        CheckConstraint("kind in ('receipt','refund')", name="kind_valid"),
        CheckConstraint("status in ('posted','reversed')", name="status_valid"),
        CheckConstraint("amount > 0", name="amount_positive"),
        Index("ix_order_payments_order", "order_id"),
        Index("uq_order_payments_idem", "idempotency_key", unique=True),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    ref: Mapped[str] = mapped_column(String(20), unique=True, nullable=False)
    order_id: Mapped[int] = mapped_column(ForeignKey("orders.id"), nullable=False)
    customer_id: Mapped[int] = mapped_column(ForeignKey("customers.id"), nullable=False)
    kind: Mapped[str] = mapped_column(String(8), nullable=False)
    method: Mapped[str] = mapped_column(String(14), nullable=False)
    amount: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False)
    paid_date: Mapped[date] = mapped_column(Date, nullable=False)
    reference: Mapped[str | None] = mapped_column(String(80))
    notes: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(10), default="posted", nullable=False)
    sales_return_id: Mapped[int | None] = mapped_column(ForeignKey("sales_returns.id"))
    idempotency_key: Mapped[str | None] = mapped_column(String(60))
    created_by: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    created_at: Mapped[datetime] = _created()
    reversed_by: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    reversed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    reverse_reason: Mapped[str | None] = mapped_column(Text)
    accounting_status: Mapped[str] = mapped_column(String(10), default="pending", nullable=False)  # Phase 6 hook


class CustomerCreditEntry(Base):
    """Append-only customer store-credit ledger. Positive = credit granted, negative = credit used."""

    __tablename__ = "customer_credit_entries"
    __table_args__ = (
        UniqueConstraint("entry_type", "source_type", "source_id", name="uq_customer_credit_source"),
        Index("ix_customer_credit_customer", "customer_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    customer_id: Mapped[int] = mapped_column(ForeignKey("customers.id"), nullable=False)
    entry_type: Mapped[str] = mapped_column(String(20), nullable=False)   # return_credit | applied | applied_reversal
    amount: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False)
    source_type: Mapped[str] = mapped_column(String(30), nullable=False)
    source_id: Mapped[int] = mapped_column(Integer, nullable=False)
    memo: Mapped[str | None] = mapped_column(String(200))
    created_by: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    created_at: Mapped[datetime] = _created()


# ---- customer returns --------------------------------------------------------------------------------------
class SalesReturn(Base):
    __tablename__ = "sales_returns"
    __table_args__ = (
        CheckConstraint("status in ('requested','approved','rejected','received','inspected','settled','cancelled')", name="status_valid"),
        CheckConstraint("refund_mode in ('refund','store_credit')", name="mode_valid"),
        Index("ix_sales_returns_order", "order_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    ref: Mapped[str] = mapped_column(String(20), unique=True, nullable=False)
    order_id: Mapped[int] = mapped_column(ForeignKey("orders.id"), nullable=False)
    customer_id: Mapped[int] = mapped_column(ForeignKey("customers.id"), nullable=False)
    location_id: Mapped[int] = mapped_column(ForeignKey("locations.id"), nullable=False)    # where the goods come back to
    status: Mapped[str] = mapped_column(String(12), default="requested", nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    refund_mode: Mapped[str] = mapped_column(String(12), default="refund", nullable=False)
    refund_delivery_fee: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    return_value: Mapped[Decimal | None] = mapped_column(Numeric(14, 2))    # value of the goods accepted for refund, set at inspection
    refund_paid: Mapped[Decimal | None] = mapped_column(Numeric(14, 2))     # cash/credit actually given back, set at settlement
    requested_by: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    requested_at: Mapped[datetime] = _created()
    decided_by: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    decision_note: Mapped[str | None] = mapped_column(Text)
    received_by: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    received_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    inspected_by: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    inspected_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    settled_by: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    settled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    intake_txn_id: Mapped[int | None] = mapped_column(ForeignKey("stock_transactions.id"))

    lines: Mapped[list["SalesReturnLine"]] = relationship(back_populates="sales_return", cascade="all, delete-orphan", order_by="SalesReturnLine.id")


class SalesReturnLine(Base):
    __tablename__ = "sales_return_lines"
    __table_args__ = (
        CheckConstraint("qty_requested > 0", name="qty_positive"),
        CheckConstraint("qty_received >= 0 and qty_restock >= 0 and qty_damaged >= 0 and qty_expired >= 0 and refund_qty >= 0",
                        name="non_negative"),
        CheckConstraint("qty_restock + qty_damaged + qty_expired <= qty_received", name="disposition_within_received"),
        CheckConstraint("refund_qty <= qty_received", name="refund_within_received"),
        UniqueConstraint("sales_return_id", "order_line_id", name="uq_sales_return_lines_item"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    sales_return_id: Mapped[int] = mapped_column(ForeignKey("sales_returns.id", ondelete="CASCADE"), nullable=False)
    order_line_id: Mapped[int] = mapped_column(ForeignKey("order_lines.id"), nullable=False)
    variant_id: Mapped[int] = mapped_column(ForeignKey("product_variants.id"), nullable=False)
    batch_id: Mapped[int | None] = mapped_column(ForeignKey("batches.id"))
    qty_requested: Mapped[int] = mapped_column(Integer, nullable=False)
    qty_received: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    qty_restock: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    qty_damaged: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    qty_expired: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    refund_qty: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    unit_cost: Mapped[Decimal | None] = mapped_column(Numeric(14, 4))     # original cost of goods sold, used for the stock intake
    inspection_note: Mapped[str | None] = mapped_column(Text)

    sales_return: Mapped[SalesReturn] = relationship(back_populates="lines")
