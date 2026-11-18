from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import (
    CheckConstraint, Date, DateTime, ForeignKey, Index, Integer, Numeric, PrimaryKeyConstraint, String, Text, func, text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from backend.db import Base

BUCKETS = ("good", "damaged", "expired", "inspection", "transit")
VALUED_BUCKETS = ("good", "damaged", "expired", "inspection")  # physically at the location, owned
MOVEMENT_TYPES = (
    "opening_stock", "receipt", "transfer_dispatch", "transfer_receipt", "transfer_loss", "adjustment", "count_variance",
    "damage", "expiry", "writeoff", "return_intake", "return_disposition", "reversal", "customer_sale",
    "distributor_dispatch", "employee_purchase", "supplier_return",
)


class StockBalance(Base):
    """Quantities per location / variant / batch (batch_id NULL = non-batch stock).
    Maintained inside posting transactions and reconciled to the ledger by tests."""

    __tablename__ = "stock_balances"
    __table_args__ = (
        CheckConstraint("qty_good >= 0 and qty_reserved >= 0 and qty_damaged >= 0 and qty_expired >= 0 "
                        "and qty_inspection >= 0 and qty_transit >= 0", name="non_negative"),
        CheckConstraint("qty_reserved <= qty_good", name="reserved_within_good"),
        Index("uq_stock_balances_key", "location_id", "variant_id", text("coalesce(batch_id, 0)"), unique=True),
        Index("ix_stock_balances_variant", "variant_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    location_id: Mapped[int] = mapped_column(ForeignKey("locations.id"), nullable=False)
    variant_id: Mapped[int] = mapped_column(ForeignKey("product_variants.id"), nullable=False)
    batch_id: Mapped[int | None] = mapped_column(ForeignKey("batches.id"))
    qty_good: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    qty_reserved: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    qty_damaged: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    qty_expired: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    qty_inspection: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    qty_transit: Mapped[int] = mapped_column(Integer, default=0, nullable=False)  # incoming to this location
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)


class StockCost(Base):
    """Weighted-average cost state per location + variant. Average = total_value / valued_qty (derived)."""

    __tablename__ = "stock_costs"
    __table_args__ = (
        PrimaryKeyConstraint("location_id", "variant_id", name="pk_stock_costs"),
        CheckConstraint("valued_qty >= 0", name="valued_qty_non_negative"),
        CheckConstraint("total_value >= 0", name="total_value_non_negative"),
    )

    location_id: Mapped[int] = mapped_column(ForeignKey("locations.id"), nullable=False)
    variant_id: Mapped[int] = mapped_column(ForeignKey("product_variants.id"), nullable=False)
    valued_qty: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    total_value: Mapped[Decimal] = mapped_column(Numeric(18, 4), default=0, nullable=False)
    last_avg_cost: Mapped[Decimal | None] = mapped_column(Numeric(14, 4))


class StockTransaction(Base):
    __tablename__ = "stock_transactions"
    __table_args__ = (
        CheckConstraint("status in ('posted','reversed')", name="status_valid"),
        Index("ix_stock_transactions_created_at", "created_at"),
        Index("ix_stock_transactions_source", "source_type", "source_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    ref: Mapped[str] = mapped_column(String(20), unique=True, nullable=False)
    txn_type: Mapped[str] = mapped_column(String(30), nullable=False)
    location_id: Mapped[int] = mapped_column(ForeignKey("locations.id"), nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(10), default="posted", nullable=False)
    reason: Mapped[str | None] = mapped_column(Text)
    notes: Mapped[str | None] = mapped_column(Text)
    idempotency_key: Mapped[str | None] = mapped_column(String(80), unique=True)
    reverses_txn_id: Mapped[int | None] = mapped_column(ForeignKey("stock_transactions.id"), unique=True)
    source_type: Mapped[str | None] = mapped_column(String(30))
    source_id: Mapped[int | None] = mapped_column(Integer)
    accounting_status: Mapped[str] = mapped_column(String(12), default="pending", nullable=False)
    created_by: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    movements: Mapped[list["StockMovement"]] = relationship(back_populates="txn", order_by="StockMovement.id", lazy="selectin")


class StockMovement(Base):
    """Immutable ledger line. One bucket change at one location."""

    __tablename__ = "stock_movements"
    __table_args__ = (
        CheckConstraint("bucket in ('good','damaged','expired','inspection','transit')", name="bucket_valid"),
        CheckConstraint("qty_delta <> 0", name="qty_nonzero"),
        CheckConstraint("cost_effect in ('in','out','none')", name="cost_effect_valid"),
        Index("ix_stock_movements_loc_var", "location_id", "variant_id"),
        Index("ix_stock_movements_txn", "txn_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    txn_id: Mapped[int] = mapped_column(ForeignKey("stock_transactions.id"), nullable=False)
    location_id: Mapped[int] = mapped_column(ForeignKey("locations.id"), nullable=False)
    variant_id: Mapped[int] = mapped_column(ForeignKey("product_variants.id"), nullable=False)
    batch_id: Mapped[int | None] = mapped_column(ForeignKey("batches.id"))
    bucket: Mapped[str] = mapped_column(String(12), nullable=False)
    qty_delta: Mapped[int] = mapped_column(Integer, nullable=False)
    unit_cost: Mapped[Decimal] = mapped_column(Numeric(14, 4), nullable=False)
    value_delta: Mapped[Decimal] = mapped_column(Numeric(18, 4), nullable=False)
    cost_effect: Mapped[str] = mapped_column(String(5), default="none", nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    txn: Mapped[StockTransaction] = relationship(back_populates="movements")


class StockReservation(Base):
    __tablename__ = "stock_reservations"
    __table_args__ = (
        CheckConstraint("qty > 0", name="qty_positive"),
        CheckConstraint("status in ('active','released','consumed')", name="status_valid"),
        Index("uq_stock_reservations_active", "ref_type", "ref_id", "location_id", "variant_id", text("coalesce(batch_id, 0)"),
              unique=True, postgresql_where=text("status = 'active'")),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    location_id: Mapped[int] = mapped_column(ForeignKey("locations.id"), nullable=False)
    variant_id: Mapped[int] = mapped_column(ForeignKey("product_variants.id"), nullable=False)
    batch_id: Mapped[int | None] = mapped_column(ForeignKey("batches.id"))
    qty: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(10), default="active", nullable=False)
    ref_type: Mapped[str] = mapped_column(String(30), nullable=False)
    ref_id: Mapped[str] = mapped_column(String(40), nullable=False)
    created_by: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


# ---- receiving ------------------------------------------------------------------------------------
class StockReceipt(Base):
    __tablename__ = "stock_receipts"
    __table_args__ = (CheckConstraint("status in ('draft','posted','cancelled','reversed')", name="status_valid"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    ref: Mapped[str] = mapped_column(String(20), unique=True, nullable=False)
    location_id: Mapped[int] = mapped_column(ForeignKey("locations.id"), nullable=False, index=True)
    purchase_reference: Mapped[str | None] = mapped_column(String(80))
    supplier_reference: Mapped[str | None] = mapped_column(String(120))  # the supplier's delivery note / free text
    supplier_id: Mapped[int | None] = mapped_column(ForeignKey("suppliers.id"), index=True)  # Phase 3
    purchase_order_id: Mapped[int | None] = mapped_column(ForeignKey("purchase_orders.id"), index=True)  # Phase 3
    received_date: Mapped[date] = mapped_column(Date, nullable=False)
    status: Mapped[str] = mapped_column(String(10), default="draft", nullable=False)
    notes: Mapped[str | None] = mapped_column(Text)
    txn_id: Mapped[int | None] = mapped_column(ForeignKey("stock_transactions.id"))
    created_by: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    posted_by: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    posted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    lines: Mapped[list["StockReceiptLine"]] = relationship(
        back_populates="receipt", cascade="all, delete-orphan", order_by="StockReceiptLine.id", lazy="selectin")
    costs: Mapped[list["ReceiptCost"]] = relationship(  # noqa: F821  (defined in backend.models.purchasing)
        "ReceiptCost", cascade="all, delete-orphan", order_by="ReceiptCost.id", lazy="selectin")


class StockReceiptLine(Base):
    __tablename__ = "stock_receipt_lines"
    __table_args__ = (
        CheckConstraint("qty_received >= 0 and qty_accepted >= 0 and qty_damaged >= 0 and qty_rejected >= 0 and unit_cost >= 0",
                        name="non_negative"),
        CheckConstraint("qty_accepted + qty_damaged + qty_rejected = qty_received", name="split_matches_received"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    receipt_id: Mapped[int] = mapped_column(ForeignKey("stock_receipts.id", ondelete="CASCADE"), nullable=False, index=True)
    variant_id: Mapped[int] = mapped_column(ForeignKey("product_variants.id"), nullable=False)
    batch_no: Mapped[str | None] = mapped_column(String(60))
    expiry_date: Mapped[date | None] = mapped_column(Date)
    qty_expected: Mapped[int | None] = mapped_column(Integer)
    qty_received: Mapped[int] = mapped_column(Integer, nullable=False)
    qty_accepted: Mapped[int] = mapped_column(Integer, nullable=False)
    qty_damaged: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    qty_rejected: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    unit_cost: Mapped[Decimal] = mapped_column(Numeric(14, 4), nullable=False)
    po_line_id: Mapped[int | None] = mapped_column(ForeignKey("purchase_order_lines.id"), index=True)  # Phase 3
    landed_alloc: Mapped[Decimal] = mapped_column(Numeric(18, 4), default=0, server_default=text("0"), nullable=False)  # additional cost allocated to this line
    notes: Mapped[str | None] = mapped_column(Text)

    receipt: Mapped[StockReceipt] = relationship(back_populates="lines")


# ---- transfers ------------------------------------------------------------------------------------
TRANSFER_STATUSES = ("requested", "approved", "dispatched", "partially_received", "discrepancy_review", "received",
                     "rejected", "cancelled")


class Transfer(Base):
    __tablename__ = "transfers"
    __table_args__ = (
        CheckConstraint("from_location_id <> to_location_id", name="different_locations"),
        CheckConstraint("status in ('requested','approved','dispatched','partially_received','discrepancy_review',"
                        "'received','rejected','cancelled')", name="status_valid"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    ref: Mapped[str] = mapped_column(String(20), unique=True, nullable=False)
    from_location_id: Mapped[int] = mapped_column(ForeignKey("locations.id"), nullable=False, index=True)
    to_location_id: Mapped[int] = mapped_column(ForeignKey("locations.id"), nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(20), default="requested", nullable=False)
    notes: Mapped[str | None] = mapped_column(Text)
    requested_by: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    requested_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    approved_by: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    dispatched_by: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    dispatched_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    shortage_closed_by: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    shortage_reason: Mapped[str | None] = mapped_column(Text)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    decision_reason: Mapped[str | None] = mapped_column(Text)

    lines: Mapped[list["TransferLine"]] = relationship(
        back_populates="transfer", cascade="all, delete-orphan", order_by="TransferLine.id", lazy="selectin")
    events: Mapped[list["TransferEvent"]] = relationship(
        back_populates="transfer", cascade="all, delete-orphan", order_by="TransferEvent.id", lazy="selectin")


class TransferLine(Base):
    __tablename__ = "transfer_lines"
    __table_args__ = (
        CheckConstraint("qty_requested > 0 and qty_dispatched >= 0 and qty_received_good >= 0 and "
                        "qty_received_damaged >= 0 and qty_short >= 0", name="non_negative"),
        CheckConstraint("qty_received_good + qty_received_damaged + qty_short <= qty_dispatched", name="settled_within_dispatched"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    transfer_id: Mapped[int] = mapped_column(ForeignKey("transfers.id", ondelete="CASCADE"), nullable=False, index=True)
    variant_id: Mapped[int] = mapped_column(ForeignKey("product_variants.id"), nullable=False)
    batch_id: Mapped[int | None] = mapped_column(ForeignKey("batches.id"))
    qty_requested: Mapped[int] = mapped_column(Integer, nullable=False)
    qty_dispatched: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    qty_received_good: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    qty_received_damaged: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    qty_short: Mapped[int] = mapped_column(Integer, default=0, nullable=False)  # approved shortage written off
    dispatch_value: Mapped[Decimal] = mapped_column(Numeric(18, 4), default=0, nullable=False)  # fixed at dispatch
    value_settled: Mapped[Decimal] = mapped_column(Numeric(18, 4), default=0, nullable=False)

    transfer: Mapped[Transfer] = relationship(back_populates="lines")


class TransferEvent(Base):
    __tablename__ = "transfer_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    transfer_id: Mapped[int] = mapped_column(ForeignKey("transfers.id", ondelete="CASCADE"), nullable=False, index=True)
    event: Mapped[str] = mapped_column(String(40), nullable=False)
    actor_user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    actor_username: Mapped[str | None] = mapped_column(String(60))
    note: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    transfer: Mapped[Transfer] = relationship(back_populates="events")


# ---- approvals for adjustments / write-offs / count variances ---------------------------------------
class StockChangeRequest(Base):
    __tablename__ = "stock_change_requests"
    __table_args__ = (
        CheckConstraint("kind in ('adjustment','writeoff','count_variance')", name="kind_valid"),
        CheckConstraint("status in ('pending','approved','rejected','cancelled')", name="status_valid"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    ref: Mapped[str] = mapped_column(String(20), unique=True, nullable=False)
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    location_id: Mapped[int] = mapped_column(ForeignKey("locations.id"), nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(10), default="pending", nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    requested_by: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    requested_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    decided_by: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    decision_note: Mapped[str | None] = mapped_column(Text)
    txn_id: Mapped[int | None] = mapped_column(ForeignKey("stock_transactions.id"))
    count_id: Mapped[int | None] = mapped_column(ForeignKey("stock_counts.id"))
    estimated_value: Mapped[Decimal | None] = mapped_column(Numeric(18, 4))

    lines: Mapped[list["StockChangeLine"]] = relationship(
        back_populates="request", cascade="all, delete-orphan", order_by="StockChangeLine.id", lazy="selectin")


class StockChangeLine(Base):
    __tablename__ = "stock_change_lines"
    __table_args__ = (CheckConstraint("qty_delta <> 0", name="qty_nonzero"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    request_id: Mapped[int] = mapped_column(ForeignKey("stock_change_requests.id", ondelete="CASCADE"), nullable=False, index=True)
    variant_id: Mapped[int] = mapped_column(ForeignKey("product_variants.id"), nullable=False)
    batch_id: Mapped[int | None] = mapped_column(ForeignKey("batches.id"))
    bucket: Mapped[str] = mapped_column(String(12), default="good", nullable=False)
    qty_delta: Mapped[int] = mapped_column(Integer, nullable=False)
    unit_cost: Mapped[Decimal | None] = mapped_column(Numeric(14, 4))  # only needed for a gain when no cost is known

    request: Mapped[StockChangeRequest] = relationship(back_populates="lines")


# ---- stock counts ---------------------------------------------------------------------------------
class StockCount(Base):
    __tablename__ = "stock_counts"
    __table_args__ = (CheckConstraint("status in ('in_progress','submitted','cancelled')", name="status_valid"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    ref: Mapped[str] = mapped_column(String(20), unique=True, nullable=False)
    location_id: Mapped[int] = mapped_column(ForeignKey("locations.id"), nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(12), default="in_progress", nullable=False)
    notes: Mapped[str | None] = mapped_column(Text)
    snapshot_movement_id: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    created_by: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    submitted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    lines: Mapped[list["StockCountLine"]] = relationship(
        back_populates="count", cascade="all, delete-orphan", order_by="StockCountLine.id", lazy="selectin")


class StockCountLine(Base):
    __tablename__ = "stock_count_lines"
    __table_args__ = (
        Index("uq_stock_count_lines_key", "count_id", "variant_id", text("coalesce(batch_id, 0)"), unique=True),
        CheckConstraint("counted_qty is null or counted_qty >= 0", name="counted_non_negative"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    count_id: Mapped[int] = mapped_column(ForeignKey("stock_counts.id", ondelete="CASCADE"), nullable=False, index=True)
    variant_id: Mapped[int] = mapped_column(ForeignKey("product_variants.id"), nullable=False)
    batch_id: Mapped[int | None] = mapped_column(ForeignKey("batches.id"))
    expected_qty: Mapped[int] = mapped_column(Integer, nullable=False)  # good-bucket quantity at snapshot
    counted_qty: Mapped[int | None] = mapped_column(Integer)
    reason: Mapped[str | None] = mapped_column(Text)

    count: Mapped[StockCount] = relationship(back_populates="lines")
