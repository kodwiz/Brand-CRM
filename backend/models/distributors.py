"""Phase 5 tables: distributors, agreed prices, credit dispatches ("invoices"), returns/credit notes, payments, allocations,
adjustments and the append-only receivable ledger.

The receivable of a distributor is never stored as a number: it is the sum of `distributor_ledger` rows, which a database
trigger makes append-only. Documents carry amounts; the ledger carries the effect on what the distributor owes
(+ increases the receivable, - decreases it).
"""
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import (
    Boolean, CheckConstraint, Date, DateTime, ForeignKey, Index, Integer, Numeric, String, Text, UniqueConstraint, func, text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from backend.db import Base

INVOICE_KINDS = ("dispatch", "opening", "debit_note")
INVOICE_STATUSES = ("draft", "held", "posted", "rejected", "cancelled", "void")
RETURN_STATUSES = ("requested", "received", "inspected", "credited", "rejected", "cancelled")
PAYMENT_STATUSES = ("pending", "posted", "rejected", "reversed")
PAYMENT_METHODS = ("cash", "bank_transfer", "cheque", "card", "other")
ADJUSTMENT_KINDS = ("write_off", "credit", "debit")


def _created():
    return mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class Distributor(Base):
    __tablename__ = "distributors"
    __table_args__ = (
        CheckConstraint("discount_pct between 0 and 100", name="discount_range"),
        CheckConstraint("credit_limit >= 0", name="limit_non_negative"),
        CheckConstraint("payment_terms_days between 0 and 365", name="terms_range"),
        Index("uq_distributors_name_ci", text("lower(name)"), unique=True),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    code: Mapped[str] = mapped_column(String(30), unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    contact_name: Mapped[str | None] = mapped_column(String(120))
    phone: Mapped[str | None] = mapped_column(String(40))
    email: Mapped[str | None] = mapped_column(String(160))
    address: Mapped[str | None] = mapped_column(Text)
    city: Mapped[str | None] = mapped_column(String(80))
    discount_pct: Mapped[Decimal] = mapped_column(Numeric(5, 2), default=0, nullable=False)       # agreed discount terms
    credit_limit: Mapped[Decimal] = mapped_column(Numeric(18, 2), default=0, nullable=False)      # 0 = no credit allowed
    payment_terms_days: Mapped[int] = mapped_column(Integer, default=30, nullable=False)
    assigned_user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), index=True)
    notes: Mapped[str | None] = mapped_column(Text)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_by: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    created_at: Mapped[datetime] = _created()
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)


class DistributorPrice(Base):
    """Agreed price for one variant, overriding the variant's standard distributor price."""

    __tablename__ = "distributor_prices"
    __table_args__ = (
        UniqueConstraint("distributor_id", "variant_id", name="uq_distributor_price"),
        CheckConstraint("price >= 0", name="price_non_negative"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    distributor_id: Mapped[int] = mapped_column(ForeignKey("distributors.id", ondelete="CASCADE"), nullable=False)
    variant_id: Mapped[int] = mapped_column(ForeignKey("product_variants.id"), nullable=False)
    price: Mapped[Decimal] = mapped_column(Numeric(18, 4), nullable=False)


class DistributorInvoice(Base):
    """A charge to the distributor: a goods dispatch on credit, the one-off opening balance, or a debit note.
    Only dispatches have lines and move stock."""

    __tablename__ = "distributor_invoices"
    __table_args__ = (
        CheckConstraint("kind in ('dispatch','opening','debit_note')", name="kind_valid"),
        CheckConstraint("status in ('draft','held','posted','rejected','cancelled','void')", name="status_valid"),
        CheckConstraint("total >= 0", name="total_non_negative"),
        Index("ix_distributor_invoices_dist", "distributor_id", "status"),
        Index("uq_distributor_opening", "distributor_id", unique=True, postgresql_where=text("kind = 'opening' and status in ('posted','held')")),
        Index("uq_distributor_invoices_idem", "distributor_id", "idempotency_key", unique=True, postgresql_where=text("idempotency_key is not null")),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    ref: Mapped[str] = mapped_column(String(20), unique=True, nullable=False)
    distributor_id: Mapped[int] = mapped_column(ForeignKey("distributors.id"), nullable=False)
    kind: Mapped[str] = mapped_column(String(12), default="dispatch", nullable=False)
    location_id: Mapped[int | None] = mapped_column(ForeignKey("locations.id"))
    invoice_date: Mapped[date] = mapped_column(Date, nullable=False)
    due_date: Mapped[date] = mapped_column(Date, nullable=False)
    status: Mapped[str] = mapped_column(String(12), default="draft", nullable=False)
    subtotal: Mapped[Decimal] = mapped_column(Numeric(18, 2), default=0, nullable=False)          # at the agreed base prices
    discount_total: Mapped[Decimal] = mapped_column(Numeric(18, 2), default=0, nullable=False)
    total: Mapped[Decimal] = mapped_column(Numeric(18, 2), default=0, nullable=False)
    hold_reasons: Mapped[list | None] = mapped_column(JSONB)
    decided_by: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    decision_note: Mapped[str | None] = mapped_column(Text)
    exchange_return_id: Mapped[int | None] = mapped_column(ForeignKey("distributor_returns.id", ondelete="SET NULL"))
    stock_txn_id: Mapped[int | None] = mapped_column(Integer)
    idempotency_key: Mapped[str | None] = mapped_column(String(60))
    notes: Mapped[str | None] = mapped_column(Text)
    void_reason: Mapped[str | None] = mapped_column(Text)
    created_by: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    created_at: Mapped[datetime] = _created()
    posted_by: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    posted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    lines: Mapped[list["DistributorInvoiceLine"]] = relationship(
        back_populates="invoice", cascade="all, delete-orphan", order_by="DistributorInvoiceLine.id", lazy="selectin")


class DistributorInvoiceLine(Base):
    __tablename__ = "distributor_invoice_lines"
    __table_args__ = (CheckConstraint("qty > 0", name="qty_positive"), CheckConstraint("unit_price >= 0", name="price_non_negative"),
                      CheckConstraint("unit_price <= agreed_price", name="price_within_agreed"))

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    invoice_id: Mapped[int] = mapped_column(ForeignKey("distributor_invoices.id", ondelete="CASCADE"), nullable=False, index=True)
    variant_id: Mapped[int] = mapped_column(ForeignKey("product_variants.id"), nullable=False)
    batch_id: Mapped[int | None] = mapped_column(ForeignKey("batches.id"))
    qty: Mapped[int] = mapped_column(Integer, nullable=False)
    base_price: Mapped[Decimal] = mapped_column(Numeric(18, 4), nullable=False)        # agreed or standard distributor price
    discount_pct: Mapped[Decimal] = mapped_column(Numeric(5, 2), default=0, nullable=False)   # the distributor's terms
    agreed_price: Mapped[Decimal] = mapped_column(Numeric(18, 4), nullable=False)      # base less terms discount
    unit_price: Mapped[Decimal] = mapped_column(Numeric(18, 4), nullable=False)        # what is actually charged
    stock_value: Mapped[Decimal | None] = mapped_column(Numeric(18, 4))                # cost of the units at posting

    invoice: Mapped["DistributorInvoice"] = relationship(back_populates="lines")


class DistributorReturn(Base):
    """Goods coming back from a distributor. Credit is only given after inspection and approval."""

    __tablename__ = "distributor_returns"
    __table_args__ = (
        CheckConstraint("status in ('requested','received','inspected','credited','rejected','cancelled')", name="status_valid"),
        Index("ix_distributor_returns_dist", "distributor_id", "status"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    ref: Mapped[str] = mapped_column(String(20), unique=True, nullable=False)
    distributor_id: Mapped[int] = mapped_column(ForeignKey("distributors.id"), nullable=False)
    location_id: Mapped[int] = mapped_column(ForeignKey("locations.id"), nullable=False)
    return_date: Mapped[date] = mapped_column(Date, nullable=False)
    status: Mapped[str] = mapped_column(String(12), default="requested", nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    credit_value: Mapped[Decimal | None] = mapped_column(Numeric(18, 2))
    intake_txn_id: Mapped[int | None] = mapped_column(Integer)
    decision_note: Mapped[str | None] = mapped_column(Text)
    requested_by: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    requested_at: Mapped[datetime] = _created()
    received_by: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    received_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    inspected_by: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    inspected_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    decided_by: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    lines: Mapped[list["DistributorReturnLine"]] = relationship(
        back_populates="ret", cascade="all, delete-orphan", order_by="DistributorReturnLine.id", lazy="selectin")


class DistributorReturnLine(Base):
    __tablename__ = "distributor_return_lines"
    __table_args__ = (CheckConstraint("qty_requested > 0", name="qty_positive"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    return_id: Mapped[int] = mapped_column(ForeignKey("distributor_returns.id", ondelete="CASCADE"), nullable=False, index=True)
    invoice_line_id: Mapped[int] = mapped_column(ForeignKey("distributor_invoice_lines.id"), nullable=False)
    variant_id: Mapped[int] = mapped_column(ForeignKey("product_variants.id"), nullable=False)
    batch_id: Mapped[int | None] = mapped_column(ForeignKey("batches.id"))
    qty_requested: Mapped[int] = mapped_column(Integer, nullable=False)
    qty_received: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    qty_restock: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    qty_damaged: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    qty_expired: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    credit_qty: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    unit_price: Mapped[Decimal] = mapped_column(Numeric(18, 4), nullable=False)       # the price charged on the original dispatch
    unit_cost: Mapped[Decimal | None] = mapped_column(Numeric(18, 4))
    inspection_note: Mapped[str | None] = mapped_column(String(200))

    ret: Mapped["DistributorReturn"] = relationship(back_populates="lines")


class DistributorPayment(Base):
    __tablename__ = "distributor_payments"
    __table_args__ = (
        CheckConstraint("amount > 0", name="amount_positive"),
        CheckConstraint("status in ('pending','posted','rejected','reversed')", name="status_valid"),
        CheckConstraint("method in ('cash','bank_transfer','cheque','card','other')", name="method_valid"),
        Index("ix_distributor_payments_dist", "distributor_id", "status"),
        Index("uq_distributor_payments_idem", "distributor_id", "idempotency_key", unique=True, postgresql_where=text("idempotency_key is not null")),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    ref: Mapped[str] = mapped_column(String(20), unique=True, nullable=False)
    distributor_id: Mapped[int] = mapped_column(ForeignKey("distributors.id"), nullable=False)
    amount: Mapped[Decimal] = mapped_column(Numeric(18, 2), nullable=False)
    method: Mapped[str] = mapped_column(String(20), nullable=False)
    paid_date: Mapped[date] = mapped_column(Date, nullable=False)
    reference: Mapped[str | None] = mapped_column(String(80))
    status: Mapped[str] = mapped_column(String(10), default="posted", nullable=False)
    pending_allocations: Mapped[list | None] = mapped_column(JSONB)       # kept while a backdated payment waits for approval
    hold_reason: Mapped[str | None] = mapped_column(String(200))
    idempotency_key: Mapped[str | None] = mapped_column(String(60))
    notes: Mapped[str | None] = mapped_column(Text)
    created_by: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    created_at: Mapped[datetime] = _created()
    decided_by: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    decision_note: Mapped[str | None] = mapped_column(Text)
    reversed_by: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    reversed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    reverse_reason: Mapped[str | None] = mapped_column(Text)


class DistributorAllocation(Base):
    """Part of a payment, credit note or credit/write-off adjustment applied to one invoice."""

    __tablename__ = "distributor_allocations"
    __table_args__ = (
        CheckConstraint("amount > 0", name="amount_positive"),
        CheckConstraint("source_type in ('payment','credit_note','adjustment')", name="source_valid"),
        Index("ix_distributor_alloc_invoice", "invoice_id"),
        Index("ix_distributor_alloc_source", "source_type", "source_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    source_type: Mapped[str] = mapped_column(String(12), nullable=False)
    source_id: Mapped[int] = mapped_column(Integer, nullable=False)
    invoice_id: Mapped[int] = mapped_column(ForeignKey("distributor_invoices.id"), nullable=False)
    amount: Mapped[Decimal] = mapped_column(Numeric(18, 2), nullable=False)
    created_by: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    created_at: Mapped[datetime] = _created()
    released_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class DistributorAdjustment(Base):
    """Write-off, credit or debit that is not goods or money. Always approved by someone other than the requester."""

    __tablename__ = "distributor_adjustments"
    __table_args__ = (
        CheckConstraint("kind in ('write_off','credit','debit')", name="kind_valid"),
        CheckConstraint("amount > 0", name="amount_positive"),
        CheckConstraint("status in ('pending','approved','rejected')", name="status_valid"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    ref: Mapped[str] = mapped_column(String(20), unique=True, nullable=False)
    distributor_id: Mapped[int] = mapped_column(ForeignKey("distributors.id"), nullable=False, index=True)
    kind: Mapped[str] = mapped_column(String(10), nullable=False)
    amount: Mapped[Decimal] = mapped_column(Numeric(18, 2), nullable=False)
    entry_date: Mapped[date] = mapped_column(Date, nullable=False)
    invoice_id: Mapped[int | None] = mapped_column(ForeignKey("distributor_invoices.id"))   # dispatch the credit/write-off is applied to
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(10), default="pending", nullable=False)
    result_invoice_id: Mapped[int | None] = mapped_column(Integer)                          # debit note created on approval
    requested_by: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    requested_at: Mapped[datetime] = _created()
    decided_by: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    decision_note: Mapped[str | None] = mapped_column(Text)


class DistributorLedgerEntry(Base):
    __tablename__ = "distributor_ledger"
    __table_args__ = (
        CheckConstraint("entry_type in ('invoice','invoice_void','credit_note','payment','payment_reversal','adjustment')", name="type_valid"),
        CheckConstraint("amount <> 0", name="amount_nonzero"),
        UniqueConstraint("entry_type", "source_type", "source_id", name="uq_distributor_ledger_source"),
        Index("ix_distributor_ledger_dist", "distributor_id", "entry_date"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    distributor_id: Mapped[int] = mapped_column(ForeignKey("distributors.id"), nullable=False)
    entry_type: Mapped[str] = mapped_column(String(20), nullable=False)
    amount: Mapped[Decimal] = mapped_column(Numeric(18, 2), nullable=False)    # + increases what the distributor owes us
    entry_date: Mapped[date] = mapped_column(Date, nullable=False)
    source_type: Mapped[str] = mapped_column(String(30), nullable=False)
    source_id: Mapped[int] = mapped_column(Integer, nullable=False)
    ref: Mapped[str] = mapped_column(String(20), nullable=False)
    memo: Mapped[str | None] = mapped_column(String(200))
    accounting_status: Mapped[str] = mapped_column(String(12), default="pending", nullable=False)   # journal posting is Phase 6
    created_by: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    created_at: Mapped[datetime] = _created()
