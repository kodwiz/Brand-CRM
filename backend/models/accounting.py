"""Phase 6 tables: chart of accounts, periods, journals (double entry), cash and bank accounts, bank reconciliation,
expenses, opening-balance batches and period requests.

A posted journal entry always balances (checked by the service and by a deferred database trigger) and is immutable:
corrections are new, opposite entries. Every system-generated entry carries (source_type, source_id, event), which is
unique, so a source document can never be posted twice.
"""
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import (
    Boolean, CheckConstraint, Date, DateTime, ForeignKey, Index, Integer, Numeric, String, Text, UniqueConstraint, func, text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from backend.db import Base

ACCOUNT_TYPES = ("asset", "liability", "equity", "income", "contra_income", "expense")
DEBIT_NORMAL = ("asset", "contra_income", "expense")
PARTY_TYPES = ("customer", "supplier", "distributor", "employee", "other")


def _created():
    return mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class Account(Base):
    __tablename__ = "accounts"
    __table_args__ = (
        CheckConstraint("type in ('asset','liability','equity','income','contra_income','expense')", name="type_valid"),
        Index("uq_accounts_name_ci", text("lower(name)"), unique=True),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    code: Mapped[str] = mapped_column(String(12), unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    type: Mapped[str] = mapped_column(String(14), nullable=False)
    category: Mapped[str] = mapped_column(String(40), nullable=False)       # statement section, e.g. current_asset, cost_of_sales
    description: Mapped[str | None] = mapped_column(String(300))
    is_system: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)    # referenced by posting rules; cannot be deactivated
    is_control: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)   # filled by sub-ledgers; no manual lines
    requires_party: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime] = _created()


class AccountingPeriod(Base):
    __tablename__ = "accounting_periods"
    __table_args__ = (CheckConstraint("status in ('open','closed')", name="status_valid"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(10), unique=True, nullable=False)        # YYYY-MM
    start_date: Mapped[date] = mapped_column(Date, unique=True, nullable=False)
    end_date: Mapped[date] = mapped_column(Date, nullable=False)
    fiscal_year: Mapped[int] = mapped_column(Integer, nullable=False)                 # year in which the fiscal year ends
    status: Mapped[str] = mapped_column(String(8), default="open", nullable=False)
    closed_by: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    reopened_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)


class PeriodRequest(Base):
    """Reopening a closed period needs a reason and a decision by another authorised user."""

    __tablename__ = "period_requests"
    __table_args__ = (
        CheckConstraint("action in ('reopen')", name="action_valid"),
        CheckConstraint("status in ('pending','approved','rejected')", name="status_valid"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    period_id: Mapped[int] = mapped_column(ForeignKey("accounting_periods.id"), nullable=False, index=True)
    action: Mapped[str] = mapped_column(String(10), default="reopen", nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(10), default="pending", nullable=False)
    requested_by: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    requested_at: Mapped[datetime] = _created()
    decided_by: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    decision_note: Mapped[str | None] = mapped_column(Text)


class JournalEntry(Base):
    __tablename__ = "journal_entries"
    __table_args__ = (
        CheckConstraint("status in ('draft','posted','reversed')", name="status_valid"),
        CheckConstraint("kind in ('system','manual','opening','reversal','expense')", name="kind_valid"),
        Index("uq_journal_source", "source_type", "source_id", "event", unique=True, postgresql_where=text("source_type is not null")),
        Index("ix_journal_entries_date", "entry_date"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    ref: Mapped[str] = mapped_column(String(20), unique=True, nullable=False)
    entry_date: Mapped[date] = mapped_column(Date, nullable=False)
    original_date: Mapped[date | None] = mapped_column(Date)             # set when the business date fell in a closed period
    period_id: Mapped[int] = mapped_column(ForeignKey("accounting_periods.id"), nullable=False, index=True)
    memo: Mapped[str] = mapped_column(String(300), nullable=False)
    kind: Mapped[str] = mapped_column(String(10), default="system", nullable=False)
    status: Mapped[str] = mapped_column(String(10), default="posted", nullable=False)
    source_type: Mapped[str | None] = mapped_column(String(30))
    source_id: Mapped[int | None] = mapped_column(Integer)
    event: Mapped[str | None] = mapped_column(String(30))
    reverses_id: Mapped[int | None] = mapped_column(ForeignKey("journal_entries.id"), unique=True)
    reason: Mapped[str | None] = mapped_column(Text)
    total: Mapped[Decimal] = mapped_column(Numeric(18, 2), default=0, nullable=False)       # sum of debits (= sum of credits)
    created_by: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    created_at: Mapped[datetime] = _created()
    posted_by: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    posted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    lines: Mapped[list["JournalLine"]] = relationship(back_populates="entry", cascade="all, delete-orphan", order_by="JournalLine.id")


class JournalLine(Base):
    __tablename__ = "journal_lines"
    __table_args__ = (
        CheckConstraint("debit >= 0 and credit >= 0 and (debit = 0 or credit = 0) and (debit + credit) > 0", name="one_side"),
        CheckConstraint("party_type is null or party_type in ('customer','supplier','distributor','employee','other')", name="party_valid"),
        Index("ix_journal_lines_account_date", "account_id", "entry_date"),
        Index("ix_journal_lines_party", "party_type", "party_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    entry_id: Mapped[int] = mapped_column(ForeignKey("journal_entries.id", ondelete="CASCADE"), nullable=False, index=True)
    account_id: Mapped[int] = mapped_column(ForeignKey("accounts.id"), nullable=False)
    debit: Mapped[Decimal] = mapped_column(Numeric(18, 2), default=0, nullable=False)
    credit: Mapped[Decimal] = mapped_column(Numeric(18, 2), default=0, nullable=False)
    memo: Mapped[str | None] = mapped_column(String(200))
    party_type: Mapped[str | None] = mapped_column(String(12))
    party_id: Mapped[int | None] = mapped_column(Integer)
    location_id: Mapped[int | None] = mapped_column(ForeignKey("locations.id"))
    entry_date: Mapped[date] = mapped_column(Date, nullable=False)          # copy of the entry date for fast reports
    reconciliation_id: Mapped[int | None] = mapped_column(ForeignKey("cash_reconciliations.id"))

    entry: Mapped[JournalEntry] = relationship(back_populates="lines")


class CashAccount(Base):
    """A till, a bank account or a clearing account (e.g. money held by couriers), each tied to one ledger account."""

    __tablename__ = "cash_accounts"
    __table_args__ = (CheckConstraint("kind in ('cash','bank','clearing')", name="kind_valid"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(120), unique=True, nullable=False)
    kind: Mapped[str] = mapped_column(String(10), nullable=False)
    account_id: Mapped[int] = mapped_column(ForeignKey("accounts.id"), unique=True, nullable=False)
    bank_name: Mapped[str | None] = mapped_column(String(120))
    account_ref: Mapped[str | None] = mapped_column(String(60))       # last digits or nickname only; never a full account number
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime] = _created()


class CashReconciliation(Base):
    __tablename__ = "cash_reconciliations"
    __table_args__ = (CheckConstraint("status in ('completed','voided')", name="status_valid"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    cash_account_id: Mapped[int] = mapped_column(ForeignKey("cash_accounts.id"), nullable=False, index=True)
    statement_date: Mapped[date] = mapped_column(Date, nullable=False)
    statement_balance: Mapped[Decimal] = mapped_column(Numeric(18, 2), nullable=False)
    cleared_balance: Mapped[Decimal] = mapped_column(Numeric(18, 2), nullable=False)    # book balance of everything cleared up to now
    status: Mapped[str] = mapped_column(String(10), default="completed", nullable=False)
    notes: Mapped[str | None] = mapped_column(Text)
    created_by: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    created_at: Mapped[datetime] = _created()
    voided_by: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    voided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    void_reason: Mapped[str | None] = mapped_column(Text)


class Expense(Base):
    __tablename__ = "expenses"
    __table_args__ = (
        CheckConstraint("amount > 0", name="amount_positive"),
        CheckConstraint("status in ('pending','posted','rejected','void')", name="status_valid"),
        Index("uq_expenses_idem", "idempotency_key", unique=True, postgresql_where=text("idempotency_key is not null")),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    ref: Mapped[str] = mapped_column(String(20), unique=True, nullable=False)
    expense_date: Mapped[date] = mapped_column(Date, nullable=False)
    account_id: Mapped[int] = mapped_column(ForeignKey("accounts.id"), nullable=False)
    cash_account_id: Mapped[int] = mapped_column(ForeignKey("cash_accounts.id"), nullable=False)    # paid from
    amount: Mapped[Decimal] = mapped_column(Numeric(18, 2), nullable=False)
    payee: Mapped[str | None] = mapped_column(String(160))
    description: Mapped[str] = mapped_column(String(300), nullable=False)
    receipt_ref: Mapped[str | None] = mapped_column(String(80))
    location_id: Mapped[int | None] = mapped_column(ForeignKey("locations.id"))
    status: Mapped[str] = mapped_column(String(10), default="pending", nullable=False)
    idempotency_key: Mapped[str | None] = mapped_column(String(60))
    created_by: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    created_at: Mapped[datetime] = _created()
    decided_by: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    decision_note: Mapped[str | None] = mapped_column(Text)
    voided_by: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    void_reason: Mapped[str | None] = mapped_column(Text)


class OpeningBatch(Base):
    """Opening balances as at a date. Lines must be individually verified and the batch must balance exactly;
    nothing is plugged. Approval by a different user posts one opening journal."""

    __tablename__ = "opening_batches"
    __table_args__ = (CheckConstraint("status in ('draft','posted','cancelled')", name="status_valid"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    ref: Mapped[str] = mapped_column(String(20), unique=True, nullable=False)
    as_of_date: Mapped[date] = mapped_column(Date, nullable=False)
    description: Mapped[str] = mapped_column(String(200), nullable=False)
    status: Mapped[str] = mapped_column(String(10), default="draft", nullable=False)
    created_by: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    created_at: Mapped[datetime] = _created()
    approved_by: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    journal_id: Mapped[int | None] = mapped_column(ForeignKey("journal_entries.id"))

    lines: Mapped[list["OpeningLine"]] = relationship(back_populates="batch", cascade="all, delete-orphan", order_by="OpeningLine.id")


class OpeningLine(Base):
    __tablename__ = "opening_lines"
    __table_args__ = (CheckConstraint("debit >= 0 and credit >= 0 and (debit = 0 or credit = 0) and (debit + credit) > 0", name="one_side"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    batch_id: Mapped[int] = mapped_column(ForeignKey("opening_batches.id", ondelete="CASCADE"), nullable=False, index=True)
    account_id: Mapped[int] = mapped_column(ForeignKey("accounts.id"), nullable=False)
    debit: Mapped[Decimal] = mapped_column(Numeric(18, 2), default=0, nullable=False)
    credit: Mapped[Decimal] = mapped_column(Numeric(18, 2), default=0, nullable=False)
    party_type: Mapped[str | None] = mapped_column(String(12))
    party_id: Mapped[int | None] = mapped_column(Integer)
    evidence: Mapped[str | None] = mapped_column(String(200))      # the register page, bank statement or count sheet it came from
    verified: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    note: Mapped[str | None] = mapped_column(String(200))

    batch: Mapped[OpeningBatch] = relationship(back_populates="lines")
