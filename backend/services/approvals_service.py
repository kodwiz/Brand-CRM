"""The approvals inbox: every request that is waiting for a decision, in one list.

The inbox only LISTS. A decision is taken on the screen that owns the request, so each module's own rules (a different person must approve, limits, stock checks)
stay the single place where they are enforced. A user sees an item only when they hold the permission that module requires to decide it; `own` marks requests the
user raised themselves, which someone else must decide."""
from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.models import (
    AttendanceCorrection, DistributorAdjustment, DistributorInvoice, DistributorPayment, Employee, EmployeePurchase, Expense, JournalEntry, Order, OpeningBatch,
    PeriodRequest, SalesReturn, StockChangeRequest, SupplierAdjustment, SupplierInvoice, Transfer, User,
)
from backend.services.scope import visible_location_ids

# kind -> (label, screen the decision is taken on)
KINDS = {
    "stock_change": ("Stock adjustment or write-off", "inventory"), "transfer": ("Stock transfer", "transfers"), "transfer_shortage": ("Transfer shortage", "transfers"),
    "order_discount": ("Order discount", "orders"), "sales_return": ("Customer return", "orders"),
    "distributor_invoice": ("Distributor dispatch held", "distributors"), "distributor_payment": ("Distributor payment held", "distributors"),
    "distributor_adjustment": ("Distributor adjustment", "distributors"), "supplier_invoice": ("Supplier invoice held", "suppliers"), "supplier_adjustment": ("Supplier adjustment", "suppliers"),
    "expense": ("Expense", "accounting"), "period_request": ("Reopen or close a period", "accounting"), "journal": ("Manual journal", "accounting"),
    "opening_batch": ("Opening balances", "accounting"), "employee_credit": ("Employee credit purchase", "employees"), "attendance_correction": ("Attendance correction", "attendance"),
}


def _names(db: Session) -> dict[int, str]:
    return {u.id: u.full_name for u in db.scalars(select(User)).all()}


def _age(ts) -> int:
    if ts is None:
        return 0
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return max((datetime.now(timezone.utc) - ts).days, 0)


def inbox(db: Session, user: User) -> list[dict]:
    have = {p.permission for p in user.role.permissions}
    ids = visible_location_ids(user)
    names = _names(db)
    out: list[dict] = []

    def ok(loc) -> bool:
        return ids is None or loc is None or loc in ids

    def add(kind: str, ref, rid: int, loc, by, at, amount=None, text: str = "") -> None:
        if not ok(loc):
            return
        out.append({"kind": kind, "type": KINDS[kind][0], "screen": KINDS[kind][1], "ref": ref, "id": rid, "location_id": loc, "requested_by": names.get(by, ""), "requested_by_id": by,
                    "requested_at": at, "age_days": _age(at), "amount": str(amount) if amount is not None else None, "summary": text, "own": by == user.id})

    for r in (db.scalars(select(StockChangeRequest).where(StockChangeRequest.status == "pending")).all() if have & {"inventory.adjust_approve", "inventory.writeoff_approve"} else []):
        need = "inventory.writeoff_approve" if r.kind == "writeoff" else "inventory.adjust_approve"
        if need in have:
            add("stock_change", r.ref, r.id, r.location_id, r.requested_by, r.requested_at, r.estimated_value, f"{r.kind.replace('_', ' ').capitalize()}: {r.reason}")
    if "transfers.approve" in have:
        for t in db.scalars(select(Transfer).where(Transfer.status == "requested")).all():
            add("transfer", t.ref, t.id, t.from_location_id, t.requested_by, t.requested_at, None, "Waiting to be approved before dispatch")
    if "transfers.approve_discrepancy" in have:
        for t in db.scalars(select(Transfer).where(Transfer.status == "discrepancy_review")).all():
            add("transfer_shortage", t.ref, t.id, t.to_location_id, t.requested_by, t.requested_at, None, "Received quantities differ from what was dispatched")
    if "discounts.approve" in have:
        for o in db.scalars(select(Order).where(Order.discount_approval == "pending")).all():
            add("order_discount", o.ref, o.id, o.location_id, o.created_by, o.created_at, o.total, f"Manual discount of up to {o.manual_discount_pct}% on a line")
    if "orders.returns" in have:
        for r in db.scalars(select(SalesReturn).where(SalesReturn.status == "requested")).all():
            add("sales_return", r.ref, r.id, r.location_id, r.requested_by, r.requested_at, None, r.reason)
    if "distributors.approve" in have:
        for i in db.scalars(select(DistributorInvoice).where(DistributorInvoice.status == "held")).all():
            add("distributor_invoice", i.ref, i.id, i.location_id, i.created_by, i.created_at, i.total, "; ".join(i.hold_reasons or []) or "Held for approval")
        for p in db.scalars(select(DistributorPayment).where(DistributorPayment.status == "pending")).all():
            add("distributor_payment", p.ref, p.id, None, p.created_by, p.created_at, p.amount, p.hold_reason or "Held for approval")
        for a in db.scalars(select(DistributorAdjustment).where(DistributorAdjustment.status == "pending")).all():
            add("distributor_adjustment", a.ref, a.id, None, a.requested_by, a.requested_at, a.amount, f"{a.kind.replace('_', ' ').capitalize()}: {a.reason}")
    if "purchasing.approve_variance" in have:
        for i in db.scalars(select(SupplierInvoice).where(SupplierInvoice.status == "held")).all():
            add("supplier_invoice", i.ref, i.id, None, i.created_by, i.created_at, i.total, "Price or quantity differs from the order")
    if "purchasing.approve" in have:
        for a in db.scalars(select(SupplierAdjustment).where(SupplierAdjustment.status == "pending")).all():
            add("supplier_adjustment", a.ref, a.id, None, a.requested_by, a.created_at, a.amount, f"{a.kind.replace('_', ' ').capitalize()}: {a.reason}")
    if "accounting.approve" in have:
        for x in db.scalars(select(Expense).where(Expense.status == "pending")).all():
            add("expense", x.ref, x.id, x.location_id, x.created_by, x.created_at, x.amount, x.description)
        for r in db.scalars(select(PeriodRequest).where(PeriodRequest.status == "pending")).all():
            add("period_request", f"Period #{r.period_id}", r.id, None, r.requested_by, r.requested_at, None, f"{r.action}: {r.reason}")
        for j in db.scalars(select(JournalEntry).where(JournalEntry.status == "draft", JournalEntry.source_type.is_(None))).all():
            add("journal", j.ref, j.id, None, j.created_by, j.created_at, j.total, j.memo)
        for b in db.scalars(select(OpeningBatch).where(OpeningBatch.status == "draft")).all():
            add("opening_batch", b.ref, b.id, None, b.created_by, b.created_at, None, b.description)
    if "employees.purchase_approve" in have:
        for p in db.scalars(select(EmployeePurchase).where(EmployeePurchase.status == "pending_approval")).all():
            add("employee_credit", p.ref, p.id, p.location_id, p.created_by, p.created_at, p.total, "Credit purchase: goods are released only when approved")
    if "attendance.approve" in have:
        emps = {e.id: e for e in db.scalars(select(Employee)).all()}
        for c in db.scalars(select(AttendanceCorrection).where(AttendanceCorrection.status == "pending")).all():
            e = emps.get(c.employee_id)
            add("attendance_correction", f"#{c.id}", c.id, e.location_id if e else None, c.requested_by, c.requested_at, None,
                f"{e.full_name if e else ''} on {c.work_date}: {'add a punch' if c.kind == 'add_punch' else 'ignore a punch'}. {c.reason}")
    out.sort(key=lambda r: (-r["age_days"], r["type"], str(r["ref"])))
    return out


def counts(db: Session, user: User) -> dict:
    rows = inbox(db, user)
    by = Counter(r["type"] for r in rows)
    return {"total": len(rows), "raised_by_you": sum(1 for r in rows if r["own"]), "older_than_3_days": sum(1 for r in rows if r["age_days"] > 3), "by_type": dict(by)}
