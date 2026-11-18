"""Phase 7: employees, employee purchases and discounts, and biometric attendance.

Employee purchases are real sales and real stock movements (txn_type 'employee_purchase'); money owed by an employee is a
receivable (account 1400) that exists only when the company has approved employee credit. Attendance stores raw punches exactly
as they arrived plus a derived daily summary that can be rebuilt from the punches and approved corrections at any time.
"""
from __future__ import annotations

from datetime import date, datetime, time
from decimal import Decimal

from sqlalchemy import (
    JSON, Boolean, CheckConstraint, Date, DateTime, ForeignKey, Index, Integer, Numeric, String, Text, Time, UniqueConstraint, func, text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from backend.db import Base


def _created() -> Mapped[datetime]:
    return mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


# ---- employees ------------------------------------------------------------------------------------------------------------------------
class Employee(Base):
    """A member of staff. Not necessarily a system user (shop staff often are not); `user_id` links the two when they are,
    which lets the system refuse a person approving their own purchase."""

    __tablename__ = "employees"
    __table_args__ = (
        CheckConstraint("status in ('active','inactive')", name="status_valid"),
        CheckConstraint("credit_limit >= 0", name="credit_limit_nonneg"),
        Index("uq_employees_device_user", "device_user_id", unique=True, postgresql_where=text("device_user_id is not null")),
        Index("uq_employees_user", "user_id", unique=True, postgresql_where=text("user_id is not null")),
        Index("ix_employees_location", "location_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    code: Mapped[str] = mapped_column(String(20), unique=True, nullable=False)
    full_name: Mapped[str] = mapped_column(String(120), nullable=False)
    designation: Mapped[str | None] = mapped_column(String(80))
    phone: Mapped[str | None] = mapped_column(String(40))
    location_id: Mapped[int] = mapped_column(ForeignKey("locations.id"), nullable=False)
    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    device_user_id: Mapped[str | None] = mapped_column(String(40))          # the id enrolled on the biometric device
    joined_on: Mapped[date | None] = mapped_column(Date)
    left_on: Mapped[date | None] = mapped_column(Date)
    status: Mapped[str] = mapped_column(String(10), default="active", nullable=False)
    credit_limit: Mapped[Decimal] = mapped_column(Numeric(14, 2), default=0, nullable=False)   # only used if the company allows employee credit
    notes: Mapped[str | None] = mapped_column(Text)
    created_by: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    created_at: Mapped[datetime] = _created()
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)


class EmployeeDiscountRule(Base):
    """Centrally configured staff discount. category_id NULL = every product. A category rule beats the general rule. A variant that
    has its own employee price on the price list uses that price instead of a percentage."""

    __tablename__ = "employee_discount_rules"
    __table_args__ = (
        CheckConstraint("pct > 0 and pct <= 100", name="pct_valid"),
        Index("uq_employee_discount_scope", text("coalesce(category_id, 0)"), unique=True, postgresql_where=text("is_active")),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(80), nullable=False)
    category_id: Mapped[int | None] = mapped_column(ForeignKey("categories.id"))
    pct: Mapped[Decimal] = mapped_column(Numeric(5, 2), nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_by: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    created_at: Mapped[datetime] = _created()


# ---- employee purchases ---------------------------------------------------------------------------------------------------------------
class EmployeePurchase(Base):
    __tablename__ = "employee_purchases"
    __table_args__ = (
        CheckConstraint("status in ('pending_approval','completed','rejected','voided')", name="status_valid"),
        CheckConstraint("total >= 0 and down_payment >= 0 and down_payment <= total", name="amounts_valid"),
        Index("ix_employee_purchases_employee", "employee_id"),
        Index("ix_employee_purchases_loc_date", "location_id", "purchase_date"),
        Index("uq_employee_purchases_idem", "idempotency_key", unique=True, postgresql_where=text("idempotency_key is not null")),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    ref: Mapped[str] = mapped_column(String(20), unique=True, nullable=False)
    employee_id: Mapped[int] = mapped_column(ForeignKey("employees.id"), nullable=False)
    location_id: Mapped[int] = mapped_column(ForeignKey("locations.id"), nullable=False)
    purchase_date: Mapped[date] = mapped_column(Date, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    receipt_ref: Mapped[str | None] = mapped_column(String(60))              # the printed or manual receipt number
    subtotal_list: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False)
    discount_total: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False)
    net: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False)
    tax_rate_pct: Mapped[Decimal] = mapped_column(Numeric(5, 2), default=0, nullable=False)
    tax: Mapped[Decimal] = mapped_column(Numeric(14, 2), default=0, nullable=False)
    total: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False)
    down_payment: Mapped[Decimal] = mapped_column(Numeric(14, 2), default=0, nullable=False)
    down_payment_method: Mapped[str | None] = mapped_column(String(14))
    down_payment_reference: Mapped[str | None] = mapped_column(String(80))
    notes: Mapped[str | None] = mapped_column(Text)
    idempotency_key: Mapped[str | None] = mapped_column(String(60))
    stock_txn_id: Mapped[int | None] = mapped_column(ForeignKey("stock_transactions.id"))
    created_by: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    created_at: Mapped[datetime] = _created()
    decided_by: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))      # credit approval, or rejection
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    decision_note: Mapped[str | None] = mapped_column(Text)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    voided_by: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    voided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    void_reason: Mapped[str | None] = mapped_column(Text)

    lines: Mapped[list["EmployeePurchaseLine"]] = relationship(back_populates="purchase", cascade="all, delete-orphan", lazy="selectin",
                                                                order_by="EmployeePurchaseLine.id")
    payments: Mapped[list["EmployeePurchasePayment"]] = relationship(back_populates="purchase", lazy="selectin",
                                                                      order_by="EmployeePurchasePayment.id")


class EmployeePurchaseLine(Base):
    __tablename__ = "employee_purchase_lines"
    __table_args__ = (CheckConstraint("qty > 0", name="qty_positive"), Index("ix_employee_purchase_lines_purchase", "purchase_id"))

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    purchase_id: Mapped[int] = mapped_column(ForeignKey("employee_purchases.id", ondelete="CASCADE"), nullable=False)
    variant_id: Mapped[int] = mapped_column(ForeignKey("product_variants.id"), nullable=False)
    batch_id: Mapped[int | None] = mapped_column(ForeignKey("batches.id"))
    qty: Mapped[int] = mapped_column(Integer, nullable=False)
    list_price: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False)        # the retail price the sale is measured against
    unit_discount: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False)
    unit_price: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False)        # what the employee pays per unit
    discount_source: Mapped[str] = mapped_column(String(40), nullable=False)           # price_list | rule:<id> | none
    stock_value: Mapped[Decimal | None] = mapped_column(Numeric(18, 4))                # cost of the units, from the stock ledger

    purchase: Mapped[EmployeePurchase] = relationship(back_populates="lines")


class EmployeePurchasePayment(Base):
    """Money received from an employee against a purchase. Nothing is edited; a wrong entry is reversed."""

    __tablename__ = "employee_purchase_payments"
    __table_args__ = (
        CheckConstraint("amount > 0", name="amount_positive"),
        CheckConstraint("status in ('posted','reversed')", name="status_valid"),
        Index("ix_employee_purchase_payments_purchase", "purchase_id"),
        Index("uq_employee_purchase_payments_idem", "idempotency_key", unique=True, postgresql_where=text("idempotency_key is not null")),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    ref: Mapped[str] = mapped_column(String(20), unique=True, nullable=False)
    purchase_id: Mapped[int] = mapped_column(ForeignKey("employee_purchases.id"), nullable=False)
    employee_id: Mapped[int] = mapped_column(ForeignKey("employees.id"), nullable=False)
    location_id: Mapped[int] = mapped_column(ForeignKey("locations.id"), nullable=False)
    method: Mapped[str] = mapped_column(String(14), nullable=False)
    amount: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False)
    paid_date: Mapped[date] = mapped_column(Date, nullable=False)
    reference: Mapped[str | None] = mapped_column(String(80))
    status: Mapped[str] = mapped_column(String(10), default="posted", nullable=False)
    idempotency_key: Mapped[str | None] = mapped_column(String(60))
    created_by: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    created_at: Mapped[datetime] = _created()
    reversed_by: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    reversed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    reverse_reason: Mapped[str | None] = mapped_column(Text)
    accounting_status: Mapped[str] = mapped_column(String(10), default="pending", nullable=False)

    purchase: Mapped[EmployeePurchase] = relationship(back_populates="payments")


# ---- attendance -----------------------------------------------------------------------------------------------------------------------
class AttendanceDevice(Base):
    """A physical attendance device and the adapter used to read its exports. Brand, model and protocol are recorded as told by the
    company; the adapter code decides how a file or feed is parsed. No credentials are stored here."""

    __tablename__ = "attendance_devices"
    __table_args__ = (UniqueConstraint("name", name="uq_attendance_devices_name"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(80), nullable=False)
    location_id: Mapped[int] = mapped_column(ForeignKey("locations.id"), nullable=False)
    brand: Mapped[str | None] = mapped_column(String(80))
    model: Mapped[str | None] = mapped_column(String(80))
    protocol: Mapped[str | None] = mapped_column(String(80))
    export_format: Mapped[str | None] = mapped_column(String(80))
    adapter: Mapped[str] = mapped_column(String(40), nullable=False)
    config: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    last_import_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_by: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    created_at: Mapped[datetime] = _created()


class AttendanceImport(Base):
    __tablename__ = "attendance_imports"
    __table_args__ = (
        CheckConstraint("status in ('completed','failed')", name="status_valid"),
        Index("uq_attendance_imports_file", "device_id", "file_sha256", unique=True, postgresql_where=text("status = 'completed'")),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    device_id: Mapped[int] = mapped_column(ForeignKey("attendance_devices.id"), nullable=False)
    adapter: Mapped[str] = mapped_column(String(40), nullable=False)
    filename: Mapped[str | None] = mapped_column(String(200))
    file_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(10), nullable=False)
    rows_total: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    rows_new: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    rows_duplicate: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    rows_rejected: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    rows_unmatched: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    first_punch_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_punch_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    errors: Mapped[list] = mapped_column(JSON, default=list, nullable=False)       # first rejected rows with their reasons
    imported_by: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    created_at: Mapped[datetime] = _created()


class AttendancePunch(Base):
    """One raw punch. Never edited: an erroneous punch is ignored through an approved correction. The unique key makes importing the
    same punch twice (overlapping exports, re-reading a device) harmless."""

    __tablename__ = "attendance_punches"
    __table_args__ = (
        CheckConstraint("direction in ('in','out','unknown')", name="direction_valid"),
        CheckConstraint("status in ('ok','unmatched','ignored')", name="status_valid"),
        CheckConstraint("source in ('device','correction')", name="source_valid"),
        Index("uq_attendance_punch_device", "device_id", "device_user_id", "punched_at", unique=True, postgresql_where=text("device_id is not null")),
        Index("ix_attendance_punches_employee", "employee_id", "punched_at"),
        Index("ix_attendance_punches_unmatched", "device_user_id", postgresql_where=text("status = 'unmatched'")),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    device_id: Mapped[int | None] = mapped_column(ForeignKey("attendance_devices.id"))
    batch_id: Mapped[int | None] = mapped_column(ForeignKey("attendance_imports.id"))
    device_user_id: Mapped[str] = mapped_column(String(40), nullable=False)
    employee_id: Mapped[int | None] = mapped_column(ForeignKey("employees.id"))
    location_id: Mapped[int | None] = mapped_column(ForeignKey("locations.id"))
    punched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    direction: Mapped[str] = mapped_column(String(8), nullable=False)
    source: Mapped[str] = mapped_column(String(10), default="device", nullable=False)
    status: Mapped[str] = mapped_column(String(10), default="ok", nullable=False)
    raw_ref: Mapped[str | None] = mapped_column(Text)                 # the original line, or a reference to it
    correction_id: Mapped[int | None] = mapped_column(ForeignKey("attendance_corrections.id", use_alter=True, name="fk_attendance_punches_correction"))
    created_at: Mapped[datetime] = _created()


class AttendancePolicy(Base):
    """Company attendance rules. location_id NULL is the default for every location without its own policy. Nothing is assumed:
    until a policy exists, only worked time is calculated and no day is called late."""

    __tablename__ = "attendance_policies"
    __table_args__ = (
        CheckConstraint("direction_mode in ('device','first_last')", name="direction_mode_valid"),
        CheckConstraint("grace_minutes >= 0 and min_present_minutes >= 0 and overtime_after_minutes >= 0", name="minutes_nonneg"),
        Index("uq_attendance_policy_location", text("coalesce(location_id, 0)"), unique=True),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    location_id: Mapped[int | None] = mapped_column(ForeignKey("locations.id"))
    timezone: Mapped[str] = mapped_column(String(60), nullable=False)
    shift_start: Mapped[time] = mapped_column(Time, nullable=False)
    shift_end: Mapped[time] = mapped_column(Time, nullable=False)
    grace_minutes: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    min_present_minutes: Mapped[int] = mapped_column(Integer, default=0, nullable=False)        # below this the day is 'short'
    overtime_enabled: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    overtime_after_minutes: Mapped[int] = mapped_column(Integer, default=0, nullable=False)     # minutes beyond the scheduled shift before overtime counts
    weekly_off: Mapped[list] = mapped_column(JSON, default=list, nullable=False)                # weekday numbers, Monday = 0
    direction_mode: Mapped[str] = mapped_column(String(12), default="device", nullable=False)
    updated_by: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)


class AttendanceDay(Base):
    """Derived daily summary. Rebuilt from punches and approved corrections; never edited by hand."""

    __tablename__ = "attendance_days"
    __table_args__ = (
        UniqueConstraint("employee_id", "work_date", name="uq_attendance_days_employee_date"),
        CheckConstraint("status in ('present','late','short','incomplete','no_punch','weekly_off')", name="status_valid"),
        Index("ix_attendance_days_date", "work_date"),
        Index("ix_attendance_days_exception", "work_date", postgresql_where=text("exception is not null")),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    employee_id: Mapped[int] = mapped_column(ForeignKey("employees.id"), nullable=False)
    location_id: Mapped[int] = mapped_column(ForeignKey("locations.id"), nullable=False)
    work_date: Mapped[date] = mapped_column(Date, nullable=False)
    first_in: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_out: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    punch_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    worked_minutes: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    late_minutes: Mapped[int | None] = mapped_column(Integer)
    overtime_minutes: Mapped[int | None] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(12), nullable=False)
    exception: Mapped[str | None] = mapped_column(String(24))
    corrected: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    computed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class AttendanceCorrection(Base):
    """A requested change to a day's punches: add a missing punch, or ignore a wrong one. Applied only when a different person
    approves it; both steps are audited."""

    __tablename__ = "attendance_corrections"
    __table_args__ = (
        CheckConstraint("kind in ('add_punch','ignore_punch')", name="kind_valid"),
        CheckConstraint("status in ('pending','approved','rejected')", name="status_valid"),
        CheckConstraint("direction is null or direction in ('in','out')", name="direction_valid"),
        Index("ix_attendance_corrections_status", "status"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    employee_id: Mapped[int] = mapped_column(ForeignKey("employees.id"), nullable=False)
    work_date: Mapped[date] = mapped_column(Date, nullable=False)
    kind: Mapped[str] = mapped_column(String(14), nullable=False)
    direction: Mapped[str | None] = mapped_column(String(4))
    punched_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    punch_id: Mapped[int | None] = mapped_column(ForeignKey("attendance_punches.id"))
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(10), default="pending", nullable=False)
    requested_by: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    requested_at: Mapped[datetime] = _created()
    decided_by: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    decision_note: Mapped[str | None] = mapped_column(Text)
