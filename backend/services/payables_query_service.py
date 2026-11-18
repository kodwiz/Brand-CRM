"""Read-side views for suppliers: balances, aging, statements, goods received not invoiced, and a consistency check."""
from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from backend.models import (
    PaymentAllocation, ProductVariant, PurchaseOrder, PurchaseOrderLine, Supplier, SupplierAdjustment, SupplierInvoice,
    SupplierLedgerEntry, SupplierPayment,
)
from backend.services import invoice_service, payment_service, purchase_service, supplier_service

q2 = supplier_service.q2
BUCKETS = ("not_due", "1_30", "31_60", "61_90", "over_90")


def _bucket(due: date, today: date) -> str:
    late = (today - due).days
    if late <= 0:
        return "not_due"
    return "1_30" if late <= 30 else "31_60" if late <= 60 else "61_90" if late <= 90 else "over_90"


def open_invoices(db: Session, supplier_id: int | None = None) -> list[tuple[SupplierInvoice, Decimal]]:
    q = select(SupplierInvoice).where(SupplierInvoice.status == "posted")
    if supplier_id:
        q = q.where(SupplierInvoice.supplier_id == supplier_id)
    out = []
    for inv in db.scalars(q.order_by(SupplierInvoice.due_date, SupplierInvoice.id)).all():
        o = invoice_service.outstanding(db, inv)
        if o > 0:
            out.append((inv, o))
    return out


def on_account(db: Session, supplier_id: int) -> Decimal:
    """Payments received by the supplier but not (yet) applied to an invoice."""
    pays = db.scalars(select(SupplierPayment).where(SupplierPayment.supplier_id == supplier_id,
                                                    SupplierPayment.status == "posted")).all()
    return q2(sum((payment_service.unallocated(p) for p in pays), Decimal(0)))


def unapplied_adjustments(db: Session, supplier_id: int) -> Decimal:
    """Approved adjustments not tied to an invoice: negative reduces the balance, positive increases it."""
    rows = db.scalars(select(SupplierAdjustment).where(
        SupplierAdjustment.supplier_id == supplier_id, SupplierAdjustment.status == "approved",
        SupplierAdjustment.invoice_id.is_(None))).all()
    return q2(sum((Decimal(a.amount) if a.kind == "increase_payable" else -Decimal(a.amount) for a in rows), Decimal(0)))


def opening_item(db: Session, supplier_id: int) -> tuple[date, Decimal] | None:
    """The balance owed before the system, as (due date, amount). It ages like an invoice, from its date plus the supplier's terms."""
    e = db.scalar(select(SupplierLedgerEntry).where(SupplierLedgerEntry.supplier_id == supplier_id, SupplierLedgerEntry.source_type == "supplier_opening"))
    if e is None:
        return None
    terms = supplier_service.get_supplier(db, supplier_id).payment_terms_days or 0
    return e.entry_date + timedelta(days=terms), q2(e.amount)


def _age_opening(db: Session, supplier_id: int, aging: dict, today: date) -> Decimal:
    o = opening_item(db, supplier_id)
    if o:
        aging[_bucket(o[0], today)] += o[1]
    return o[1] if o else Decimal(0)


def supplier_summary(db: Session, supplier_id: int, today: date | None = None) -> dict:
    today = today or date.today()
    inv = open_invoices(db, supplier_id)
    aging = {b: Decimal(0) for b in BUCKETS}
    for i, o in inv:
        aging[_bucket(i.due_date, today)] += o
    open_total = q2(sum((o for _, o in inv), Decimal(0)) + _age_opening(db, supplier_id, aging, today))
    overdue = q2(sum((o for b, o in aging.items() if b != "not_due"), Decimal(0)))
    pending = db.scalar(select(func.count()).select_from(SupplierAdjustment).where(
        SupplierAdjustment.supplier_id == supplier_id, SupplierAdjustment.status == "pending")) or 0
    held = db.scalar(select(func.count()).select_from(SupplierInvoice).where(
        SupplierInvoice.supplier_id == supplier_id, SupplierInvoice.status == "held")) or 0
    return {"balance": str(supplier_service.balance(db, supplier_id)), "open_invoices_total": str(open_total),
            "open_invoice_count": len(inv), "overdue": str(overdue), "on_account": str(on_account(db, supplier_id)),
            "unapplied_adjustments": str(unapplied_adjustments(db, supplier_id)),
            "pending_adjustments": pending, "held_invoices": held,
            "aging": {b: str(q2(v)) for b, v in aging.items()}}


def payables(db: Session, today: date | None = None) -> dict:
    today = today or date.today()
    rows = []
    totals = {b: Decimal(0) for b in BUCKETS}
    grand = Decimal(0)
    for s in db.scalars(select(Supplier).order_by(func.lower(Supplier.name))).all():
        bal = supplier_service.balance(db, s.id)
        inv = open_invoices(db, s.id)
        if bal == 0 and not inv:
            continue
        aging = {b: Decimal(0) for b in BUCKETS}
        for i, o in inv:
            aging[_bucket(i.due_date, today)] += o
        _age_opening(db, s.id, aging, today)
        for b in BUCKETS:
            totals[b] += aging[b]
        grand += bal
        rows.append({"supplier_id": s.id, "code": s.code, "name": s.name, "balance": str(bal),
                     "on_account": str(on_account(db, s.id)), "aging": {b: str(q2(v)) for b, v in aging.items()},
                     "overdue": str(q2(sum((v for b, v in aging.items() if b != "not_due"), Decimal(0))))})
    return {"as_of": today, "rows": rows, "total_balance": str(q2(grand)), "aging_totals": {b: str(q2(v)) for b, v in totals.items()}}


def statement(db: Session, supplier_id: int, date_from: date | None, date_to: date | None) -> dict:
    s = supplier_service.get_supplier(db, supplier_id)
    q = select(SupplierLedgerEntry).where(SupplierLedgerEntry.supplier_id == supplier_id)
    allrows = db.scalars(q.order_by(SupplierLedgerEntry.entry_date, SupplierLedgerEntry.id)).all()
    opening = Decimal(0)
    running = Decimal(0)
    out = []
    for e in allrows:
        if date_from and e.entry_date < date_from:
            opening += Decimal(e.amount)
            running = opening
            continue
        if date_to and e.entry_date > date_to:
            continue
        running += Decimal(e.amount)
        out.append({"date": e.entry_date, "type": e.entry_type, "ref": e.ref, "memo": e.memo, "amount": str(e.amount),
                    "balance": str(q2(running)), "source_type": e.source_type, "source_id": e.source_id})
    return {"supplier": {"id": s.id, "code": s.code, "name": s.name}, "opening_balance": str(q2(opening)), "entries": out,
            "closing_balance": str(q2(running))}


def goods_received_not_invoiced(db: Session, location_ids: set[int] | None = None) -> dict:
    """Goods we own that no supplier invoice covers yet. This is information, not a liability: the debt is only
    recognised when the invoice is posted."""
    rows = []
    total = Decimal(0)
    q = select(PurchaseOrder).where(PurchaseOrder.status.in_(("approved", "partially_received", "received", "closed")))
    if location_ids is not None:
        q = q.where(PurchaseOrder.location_id.in_(location_ids))
    for po in db.scalars(q.order_by(PurchaseOrder.id)).all():
        qty = purchase_service.line_quantities(db, po.id)
        for l in po.lines:
            d = qty[l.id]
            if d["not_yet_invoiced"] > 0:
                v = db.get(ProductVariant, l.variant_id)
                value = q2(Decimal(d["not_yet_invoiced"]) * Decimal(l.unit_cost))
                total += value
                rows.append({"po_id": po.id, "po_ref": po.ref, "supplier_id": po.supplier_id, "sku": v.sku,
                             "variant_name": v.name, "received": d["owned"], "invoiced": d["invoiced"],
                             "not_invoiced_qty": d["not_yet_invoiced"], "unit_cost": str(l.unit_cost), "value": str(value)})
    return {"rows": rows, "total_value": str(q2(total))}


def integrity_check(db: Session) -> list[str]:
    """Every supplier's ledger balance must equal what the documents say: posted invoices outstanding, plus approved
    increase adjustments, minus payments not yet applied and approved credits not tied to an invoice."""
    problems: list[str] = []
    for s in db.scalars(select(Supplier)).all():
        ledger_bal = supplier_service.balance(db, s.id)
        derived = (sum((o for _, o in open_invoices(db, s.id)), Decimal(0))
                   - on_account(db, s.id) + unapplied_adjustments(db, s.id))
        if q2(ledger_bal) != q2(derived):
            problems.append(f"{s.code}: ledger balance {q2(ledger_bal)} differs from documents {q2(derived)}")
    dupes = db.execute(select(SupplierLedgerEntry.source_type, SupplierLedgerEntry.source_id, SupplierLedgerEntry.entry_type,
                              func.count()).group_by(SupplierLedgerEntry.source_type, SupplierLedgerEntry.source_id,
                                                     SupplierLedgerEntry.entry_type).having(func.count() > 1)).all()
    for st, sid, et, n in dupes:
        problems.append(f"{et} for {st} {sid} appears {n} times in the supplier ledger")
    return problems
