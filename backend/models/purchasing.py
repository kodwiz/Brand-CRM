"""Phase 3 tables: suppliers, purchase orders, supplier invoices, payments, adjustments, returns and the supplier ledger.

Money amounts use 2 decimal places (documents); unit prices/costs use 4 (same as stock). Liability is created in exactly
one place: when a supplier invoice is posted. Goods receipts never create a liability (see docs/purchasing.md).
"""
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import (
    Boolean, CheckConstraint, Date, DateTime, ForeignKey, Index, Integer, Numeric, String, Text, UniqueConstraint, func, text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from backend.db import Base

PO_STATUSES = ("draft", "approved", "partially_received", "received", "closed", "cancelled")
INVOICE_STATUSES = ("draft", "held", "posted", "rejected", "void", "cancelled")
PAYMENT_METHODS = ("cash", "bank_transfer", "cheque", "card", "other")


def _created():
    return mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class Supplier(Base):
    __tablename__ = "suppliers"
    __table_args__ = (
        CheckConstraint("payment_terms_days between 0 and 365", name="terms_range"),
        Index("uq_suppliers_name_ci", text("lower(name)"), unique=True),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    code: Mapped[str] = mapped_column(String(30), unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    contact_name: Mapped[str | None] = mapped_column(String(120))
    phone: Mapped[str | None] = mapped_column(String(40))
    email: Mapped[str | None] = mapped_column(String(160))
    address: Mapped[str | None] = mapped_column(Text)
    city: Mapped[str | None] = mapped_column(String(80))
    tax_number: Mapped[str | None] = mapped_column(String(60))
    payment_terms_days: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    currency: Mapped[str] = mapped_column(String(3), default="PKR", nullable=False)
    notes: Mapped[str | None] = mapped_column(Text)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_by: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    created_at: Mapped[datetime] = _created()
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)


# ---- purchase orders --------------------------------------------------------------------------------------
class PurchaseOrder(Base):
    __tablename__ = "purchase_orders"
    __table_args__ = (
        CheckConstraint("status in ('draft','approved','partially_received','received','closed','cancelled')", name="status_valid"),
        CheckConstraint("payment_terms_days between 0 and 365", name="terms_range"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    ref: Mapped[str] = mapped_column(String(20), unique=True, nullable=False)
    supplier_id: Mapped[int] = mapped_column(ForeignKey("suppliers.id"), nullable=False, index=True)
    location_id: Mapped[int] = mapped_column(ForeignKey("locations.id"), nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(20), default="draft", nullable=False)
    order_date: Mapped[date] = mapped_column(Date, nullable=False)
    expected_date: Mapped[date | None] = mapped_column(Date)
    payment_terms_days: Mapped[int] = mapped_column(Integer, default=0, nullable=False)  # snapshot of the supplier's terms
    supplier_reference: Mapped[str | None] = mapped_column(String(80))
    notes: Mapped[str | None] = mapped_column(Text)
    created_by: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    created_at: Mapped[datetime] = _created()
    approved_by: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    closed_by: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    close_reason: Mapped[str | None] = mapped_column(Text)

    lines: Mapped[list["PurchaseOrderLine"]] = relationship(
        back_populates="po", cascade="all, delete-orphan", order_by="PurchaseOrderLine.id", lazy="selectin")


class PurchaseOrderLine(Base):
    __tablename__ = "purchase_order_lines"
    __table_args__ = (
        CheckConstraint("qty_ordered > 0 and unit_cost >= 0", name="positive"),
        UniqueConstraint("po_id", "variant_id", name="uq_po_lines_variant"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    po_id: Mapped[int] = mapped_column(ForeignKey("purchase_orders.id", ondelete="CASCADE"), nullable=False, index=True)
    variant_id: Mapped[int] = mapped_column(ForeignKey("product_variants.id"), nullable=False)
    qty_ordered: Mapped[int] = mapped_column(Integer, nullable=False)
    unit_cost: Mapped[Decimal] = mapped_column(Numeric(14, 4), nullable=False)
    notes: Mapped[str | None] = mapped_column(Text)

    po: Mapped[PurchaseOrder] = relationship(back_populates="lines")


# ---- receipt extras (the receipt tables themselves are Phase 2; columns are added by the Phase 3 migration) ----
class ReceiptCost(Base):
    """Additional cost (freight, duty, handling) capitalised into the received stock's cost. Never a liability by itself."""

    __tablename__ = "stock_receipt_costs"
    __table_args__ = (CheckConstraint("amount > 0", name="amount_positive"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    receipt_id: Mapped[int] = mapped_column(ForeignKey("stock_receipts.id", ondelete="CASCADE"), nullable=False, index=True)
    description: Mapped[str] = mapped_column(String(160), nullable=False)
    amount: Mapped[Decimal] = mapped_column(Numeric(18, 2), nullable=False)


# ---- supplier invoices ----------------------------------------------------------------------------------
class SupplierInvoice(Base):
    __tablename__ = "supplier_invoices"
    __table_args__ = (
        CheckConstraint("status in ('draft','held','posted','rejected','void','cancelled')", name="status_valid"),
        CheckConstraint("total >= 0", name="total_non_negative"),
        # a supplier's invoice number can only be booked once (voided/cancelled/rejected ones free the number)
        Index("uq_supplier_invoice_no", "supplier_id", text("lower(invoice_no)"), unique=True,
              postgresql_where=text("status in ('draft','held','posted')")),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    ref: Mapped[str] = mapped_column(String(20), unique=True, nullable=False)
    supplier_id: Mapped[int] = mapped_column(ForeignKey("suppliers.id"), nullable=False, index=True)
    purchase_order_id: Mapped[int | None] = mapped_column(ForeignKey("purchase_orders.id"), index=True)
    invoice_no: Mapped[str] = mapped_column(String(60), nullable=False)
    invoice_date: Mapped[date] = mapped_column(Date, nullable=False)
    due_date: Mapped[date] = mapped_column(Date, nullable=False)
    status: Mapped[str] = mapped_column(String(12), default="draft", nullable=False)
    subtotal: Mapped[Decimal] = mapped_column(Numeric(18, 2), nullable=False)
    charges_total: Mapped[Decimal] = mapped_column(Numeric(18, 2), default=0, nullable=False)
    discount: Mapped[Decimal] = mapped_column(Numeric(18, 2), default=0, nullable=False)
    tax: Mapped[Decimal] = mapped_column(Numeric(18, 2), default=0, nullable=False)
    total: Mapped[Decimal] = mapped_column(Numeric(18, 2), nullable=False)
    match_status: Mapped[str] = mapped_column(String(16), default="unchecked", nullable=False)
    variances: Mapped[list | None] = mapped_column(JSONB)
    notes: Mapped[str | None] = mapped_column(Text)
    created_by: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    created_at: Mapped[datetime] = _created()
    posted_by: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    posted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    decided_by: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))  # variance approver / rejecter
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    decision_note: Mapped[str | None] = mapped_column(Text)
    voided_by: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    voided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    void_reason: Mapped[str | None] = mapped_column(Text)

    lines: Mapped[list["SupplierInvoiceLine"]] = relationship(
        back_populates="invoice", cascade="all, delete-orphan", order_by="SupplierInvoiceLine.id", lazy="selectin")
    charges: Mapped[list["SupplierInvoiceCharge"]] = relationship(
        back_populates="invoice", cascade="all, delete-orphan", order_by="SupplierInvoiceCharge.id", lazy="selectin")


class SupplierInvoiceLine(Base):
    __tablename__ = "supplier_invoice_lines"
    __table_args__ = (CheckConstraint("qty > 0 and unit_price >= 0", name="positive"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    invoice_id: Mapped[int] = mapped_column(ForeignKey("supplier_invoices.id", ondelete="CASCADE"), nullable=False, index=True)
    po_line_id: Mapped[int | None] = mapped_column(ForeignKey("purchase_order_lines.id"), index=True)
    variant_id: Mapped[int | None] = mapped_column(ForeignKey("product_variants.id"))
    description: Mapped[str | None] = mapped_column(String(200))
    qty: Mapped[int] = mapped_column(Integer, nullable=False)
    unit_price: Mapped[Decimal] = mapped_column(Numeric(14, 4), nullable=False)
    line_total: Mapped[Decimal] = mapped_column(Numeric(18, 2), nullable=False)

    invoice: Mapped[SupplierInvoice] = relationship(back_populates="lines")


class SupplierInvoiceCharge(Base):
    __tablename__ = "supplier_invoice_charges"
    __table_args__ = (CheckConstraint("amount > 0", name="amount_positive"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    invoice_id: Mapped[int] = mapped_column(ForeignKey("supplier_invoices.id", ondelete="CASCADE"), nullable=False, index=True)
    description: Mapped[str] = mapped_column(String(160), nullable=False)
    amount: Mapped[Decimal] = mapped_column(Numeric(18, 2), nullable=False)

    invoice: Mapped[SupplierInvoice] = relationship(back_populates="charges")


# ---- payments, adjustments, returns ----------------------------------------------------------------------
class SupplierPayment(Base):
    __tablename__ = "supplier_payments"
    __table_args__ = (
        CheckConstraint("amount > 0", name="amount_positive"),
        CheckConstraint("status in ('posted','reversed')", name="status_valid"),
        CheckConstraint("method in ('cash','bank_transfer','cheque','card','other')", name="method_valid"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    ref: Mapped[str] = mapped_column(String(20), unique=True, nullable=False)
    supplier_id: Mapped[int] = mapped_column(ForeignKey("suppliers.id"), nullable=False, index=True)
    amount: Mapped[Decimal] = mapped_column(Numeric(18, 2), nullable=False)
    method: Mapped[str] = mapped_column(String(15), nullable=False)
    paid_date: Mapped[date] = mapped_column(Date, nullable=False)
    reference: Mapped[str | None] = mapped_column(String(80))
    notes: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(10), default="posted", nullable=False)
    idempotency_key: Mapped[str | None] = mapped_column(String(80), unique=True)
    created_by: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    created_at: Mapped[datetime] = _created()
    reversed_by: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    reversed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    reverse_reason: Mapped[str | None] = mapped_column(Text)

    allocations: Mapped[list["PaymentAllocation"]] = relationship(
        back_populates="payment", cascade="all, delete-orphan", order_by="PaymentAllocation.id", lazy="selectin")


class PaymentAllocation(Base):
    """Part of a payment applied to one invoice. Released (not deleted) when the payment is reversed."""

    __tablename__ = "supplier_payment_allocations"
    __table_args__ = (CheckConstraint("amount > 0", name="amount_positive"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    payment_id: Mapped[int] = mapped_column(ForeignKey("supplier_payments.id", ondelete="CASCADE"), nullable=False, index=True)
    invoice_id: Mapped[int] = mapped_column(ForeignKey("supplier_invoices.id"), nullable=False, index=True)
    amount: Mapped[Decimal] = mapped_column(Numeric(18, 2), nullable=False)
    created_at: Mapped[datetime] = _created()
    released_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    payment: Mapped[SupplierPayment] = relationship(back_populates="allocations")


class SupplierAdjustment(Base):
    """A debit/credit adjustment. reduce_payable = credit from the supplier (returns, price corrections, rebates);
    increase_payable = an extra charge from the supplier. Needs an independent approver before it affects the balance."""

    __tablename__ = "supplier_adjustments"
    __table_args__ = (
        CheckConstraint("kind in ('reduce_payable','increase_payable')", name="kind_valid"),
        CheckConstraint("status in ('pending','approved','rejected','cancelled')", name="status_valid"),
        CheckConstraint("amount > 0", name="amount_positive"),
        CheckConstraint("invoice_id is null or kind = 'reduce_payable'", name="invoice_only_for_credits"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    ref: Mapped[str] = mapped_column(String(20), unique=True, nullable=False)
    supplier_id: Mapped[int] = mapped_column(ForeignKey("suppliers.id"), nullable=False, index=True)
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    amount: Mapped[Decimal] = mapped_column(Numeric(18, 2), nullable=False)
    adjustment_date: Mapped[date] = mapped_column(Date, nullable=False)
    document_no: Mapped[str | None] = mapped_column(String(60))  # the supplier's credit/debit note number
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    invoice_id: Mapped[int | None] = mapped_column(ForeignKey("supplier_invoices.id"), index=True)
    supplier_return_id: Mapped[int | None] = mapped_column(ForeignKey("supplier_returns.id"), unique=True)
    status: Mapped[str] = mapped_column(String(10), default="pending", nullable=False)
    requested_by: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    created_at: Mapped[datetime] = _created()
    decided_by: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    decision_note: Mapped[str | None] = mapped_column(Text)


class SupplierReturn(Base):
    __tablename__ = "supplier_returns"
    __table_args__ = (CheckConstraint("status in ('draft','dispatched','credited','cancelled')", name="status_valid"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    ref: Mapped[str] = mapped_column(String(20), unique=True, nullable=False)
    supplier_id: Mapped[int] = mapped_column(ForeignKey("suppliers.id"), nullable=False, index=True)
    location_id: Mapped[int] = mapped_column(ForeignKey("locations.id"), nullable=False, index=True)
    purchase_order_id: Mapped[int | None] = mapped_column(ForeignKey("purchase_orders.id"))
    receipt_id: Mapped[int | None] = mapped_column(ForeignKey("stock_receipts.id"))
    status: Mapped[str] = mapped_column(String(12), default="draft", nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    notes: Mapped[str | None] = mapped_column(Text)
    txn_id: Mapped[int | None] = mapped_column(ForeignKey("stock_transactions.id"))
    created_by: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    created_at: Mapped[datetime] = _created()
    dispatched_by: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    dispatched_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    lines: Mapped[list["SupplierReturnLine"]] = relationship(
        back_populates="ret", cascade="all, delete-orphan", order_by="SupplierReturnLine.id", lazy="selectin")


class SupplierReturnLine(Base):
    __tablename__ = "supplier_return_lines"
    __table_args__ = (
        CheckConstraint("qty > 0", name="qty_positive"),
        CheckConstraint("bucket in ('good','damaged','expired','inspection')", name="bucket_valid"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    return_id: Mapped[int] = mapped_column(ForeignKey("supplier_returns.id", ondelete="CASCADE"), nullable=False, index=True)
    variant_id: Mapped[int] = mapped_column(ForeignKey("product_variants.id"), nullable=False)
    batch_id: Mapped[int | None] = mapped_column(ForeignKey("batches.id"))
    bucket: Mapped[str] = mapped_column(String(12), nullable=False)
    qty: Mapped[int] = mapped_column(Integer, nullable=False)
    stock_value: Mapped[Decimal | None] = mapped_column(Numeric(18, 4))  # book value taken out of stock at dispatch

    ret: Mapped[SupplierReturn] = relationship(back_populates="lines")


# ---- the supplier ledger (append-only; the single source of what we owe) -----------------------------------
LEDGER_TYPES = ("invoice", "invoice_void", "payment", "payment_reversal", "adjustment")


class SupplierLedgerEntry(Base):
    __tablename__ = "supplier_ledger"
    __table_args__ = (
        CheckConstraint("entry_type in ('invoice','invoice_void','payment','payment_reversal','adjustment')", name="type_valid"),
        CheckConstraint("amount <> 0", name="amount_nonzero"),
        # a document can create each kind of entry at most once => a liability can never be booked twice
        UniqueConstraint("entry_type", "source_type", "source_id", name="uq_supplier_ledger_source"),
        Index("ix_supplier_ledger_supplier", "supplier_id", "entry_date"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    supplier_id: Mapped[int] = mapped_column(ForeignKey("suppliers.id"), nullable=False)
    entry_type: Mapped[str] = mapped_column(String(20), nullable=False)
    amount: Mapped[Decimal] = mapped_column(Numeric(18, 2), nullable=False)  # + increases what we owe, - decreases it
    entry_date: Mapped[date] = mapped_column(Date, nullable=False)
    source_type: Mapped[str] = mapped_column(String(30), nullable=False)
    source_id: Mapped[int] = mapped_column(Integer, nullable=False)
    ref: Mapped[str] = mapped_column(String(20), nullable=False)
    memo: Mapped[str | None] = mapped_column(String(200))
    accounting_status: Mapped[str] = mapped_column(String(12), default="pending", nullable=False)  # journal posting is Phase 6
    created_by: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    created_at: Mapped[datetime] = _created()
