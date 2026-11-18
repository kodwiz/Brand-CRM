"""Distributor records and the receivable.

The receivable is never typed in: it is the sum of the append-only `distributor_ledger`. Documents (dispatches, credit
notes, payments, adjustments) create ledger rows once each (unique source constraint), and *allocations* say which dispatch
a payment or credit settles, which drives outstanding amounts, aging and statements.

    closing receivable = opening + charges - approved credit notes/adjustments - payments allocated or on account
"""
from __future__ import annotations

import calendar
from datetime import date, timedelta
from decimal import Decimal

from sqlalchemy import func, or_, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from backend.models import (
    Distributor, DistributorAdjustment, DistributorAllocation, DistributorInvoice, DistributorLedgerEntry, DistributorPayment,
    DistributorPrice, DistributorReturn, ProductVariant, User,
)
from backend.services import audit_service
from backend.services.errors import BusinessRuleError, ConflictError, NotFoundError

EDITABLE = ("name", "contact_name", "phone", "email", "address", "city", "discount_pct", "credit_limit", "payment_terms_days",
            "assigned_user_id", "notes")
CREDIT_SOURCES = ("payment", "credit_note", "adjustment")


def q2(x) -> Decimal:
    return Decimal(str(x)).quantize(Decimal("0.01"))


def q4(x) -> Decimal:
    return Decimal(str(x)).quantize(Decimal("0.0001"))


# ---- records ------------------------------------------------------------------------------------------------
def get_distributor(db: Session, distributor_id: int, *, lock: bool = False) -> Distributor:
    q = select(Distributor).where(Distributor.id == distributor_id)
    if lock:
        q = q.with_for_update().execution_options(populate_existing=True)
    d = db.scalar(q)
    if d is None:
        raise NotFoundError("Distributor not found")
    return d


def can_access(user: User, d: Distributor) -> bool:
    """Managers, the CEO and accounts (all-location roles) see every distributor; a stock keeper sees those assigned to them
    and those not yet assigned to anyone."""
    return bool(user.role.scope_all_locations) or d.assigned_user_id in (None, user.id)


def visible_filter(user: User):
    if user.role.scope_all_locations:
        return None
    return or_(Distributor.assigned_user_id.is_(None), Distributor.assigned_user_id == user.id)


def checked(db: Session, user: User, distributor_id: int, *, lock: bool = False) -> Distributor:
    d = get_distributor(db, distributor_id, lock=lock)
    if not can_access(user, d):
        raise NotFoundError("Distributor not found")
    return d


def _clean(data: dict) -> dict:
    out = {}
    for k in EDITABLE:
        if k in data and data[k] is not None:
            v = data[k]
            out[k] = v.strip() if isinstance(v, str) else v
            if isinstance(v, str) and not out[k] and k != "notes":
                out[k] = None
    return out


def _validate(db: Session, d: dict) -> None:
    if "discount_pct" in d and not (Decimal(str(d["discount_pct"])) >= 0 and Decimal(str(d["discount_pct"])) <= 100):
        raise BusinessRuleError("The discount must be between 0 and 100 percent.")
    if "credit_limit" in d and Decimal(str(d["credit_limit"])) < 0:
        raise BusinessRuleError("The credit limit cannot be negative.")
    if "payment_terms_days" in d and not (0 <= int(d["payment_terms_days"]) <= 365):
        raise BusinessRuleError("Payment terms must be between 0 and 365 days.")
    if d.get("assigned_user_id") is not None:
        u = db.get(User, d["assigned_user_id"])
        if u is None or not u.is_active:
            raise BusinessRuleError("The assigned stock keeper must be an active user.")


def create_distributor(db: Session, *, actor: User, data: dict, ip: str | None) -> Distributor:
    d = _clean(data)
    if not d.get("name"):
        raise BusinessRuleError("A distributor name is required.")
    _validate(db, d)
    if db.scalar(select(Distributor.id).where(func.lower(Distributor.name) == d["name"].lower())):
        raise ConflictError(f"A distributor called '{d['name']}' already exists.")
    code = f"DST-{db.scalar(text('select nextval(:s)'), {'s': 'distributor_code_seq'}):04d}"
    row = Distributor(code=code, created_by=actor.id, **d)
    db.add(row)
    try:
        db.flush()
    except IntegrityError:
        raise ConflictError("A distributor with this name already exists.")
    audit_service.record(db, actor=actor, action="distributor.created", entity_type="distributor", entity_id=row.id,
                         after={"code": row.code, "name": row.name, "credit_limit": str(row.credit_limit)}, ip=ip)
    return row


def update_distributor(db: Session, *, actor: User, distributor_id: int, data: dict, is_active: bool | None, ip: str | None) -> Distributor:
    row = get_distributor(db, distributor_id, lock=True)
    d = _clean(data)
    if "assigned_user_id" in data and data["assigned_user_id"] is None:
        d["assigned_user_id"] = None
    _validate(db, d)
    if "name" in d:
        if not d["name"]:
            raise BusinessRuleError("A distributor name is required.")
        if db.scalar(select(Distributor.id).where(func.lower(Distributor.name) == d["name"].lower(), Distributor.id != row.id)):
            raise ConflictError(f"A distributor called '{d['name']}' already exists.")
    before = {k: str(getattr(row, k)) for k in EDITABLE} | {"is_active": row.is_active}
    for k, v in d.items():
        setattr(row, k, v)
    if is_active is not None and is_active != row.is_active:
        if not is_active and balance(db, row.id) != 0:
            raise BusinessRuleError("This distributor still has a balance. Settle or write it off before deactivating.")
        row.is_active = is_active
    db.flush()
    audit_service.record(db, actor=actor, action="distributor.updated", entity_type="distributor", entity_id=row.id, before=before,
                         after={k: str(getattr(row, k)) for k in EDITABLE} | {"is_active": row.is_active}, ip=ip)
    return row


def set_price(db: Session, *, actor: User, distributor_id: int, variant_id: int, price, ip: str | None) -> dict:
    d = get_distributor(db, distributor_id, lock=True)
    v = db.get(ProductVariant, variant_id)
    if v is None:
        raise NotFoundError("Item not found")
    row = db.scalar(select(DistributorPrice).where(DistributorPrice.distributor_id == d.id, DistributorPrice.variant_id == variant_id))
    if price is None:
        if row:
            db.delete(row)
        db.flush()
        audit_service.record(db, actor=actor, action="distributor.price_removed", entity_type="distributor", entity_id=d.id,
                             after={"variant": v.sku}, ip=ip)
        return {"variant_id": variant_id, "sku": v.sku, "price": None}
    p = q4(price)
    if p < 0:
        raise BusinessRuleError("A price cannot be negative.")
    before = str(row.price) if row else None
    if row:
        row.price = p
    else:
        db.add(DistributorPrice(distributor_id=d.id, variant_id=variant_id, price=p))
    db.flush()
    audit_service.record(db, actor=actor, action="distributor.price_set", entity_type="distributor", entity_id=d.id,
                         before={"price": before}, after={"variant": v.sku, "price": str(p)}, ip=ip)
    return {"variant_id": variant_id, "sku": v.sku, "price": str(p)}


def base_price(db: Session, d: Distributor, v: ProductVariant) -> Decimal:
    """The agreed price for this distributor, else the variant's standard distributor price. Never a retail price."""
    row = db.scalar(select(DistributorPrice).where(DistributorPrice.distributor_id == d.id, DistributorPrice.variant_id == v.id))
    if row is not None:
        return Decimal(row.price)
    if v.distributor_price is None:
        raise BusinessRuleError(f"{v.sku} has no distributor price. Set one in Products, or agree a price with {d.name}.")
    if v.status == "inactive" or v.product.status == "inactive":
        raise BusinessRuleError(f"{v.sku} is inactive and cannot be dispatched.")
    return Decimal(v.distributor_price)


# ---- the ledger and balances -----------------------------------------------------------------------------------
def balance(db: Session, distributor_id: int, before: date | None = None) -> Decimal:
    q = select(func.coalesce(func.sum(DistributorLedgerEntry.amount), 0)).where(DistributorLedgerEntry.distributor_id == distributor_id)
    if before is not None:
        q = q.where(DistributorLedgerEntry.entry_date < before)
    return q2(Decimal(db.scalar(q)))


def add_ledger_entry(db: Session, *, distributor_id: int, entry_type: str, amount: Decimal, entry_date: date, source_type: str,
                     source_id: int, ref: str, memo: str | None, actor: User) -> DistributorLedgerEntry:
    """The only way the receivable changes. The unique (type, source) constraint stops one document being booked twice."""
    e = DistributorLedgerEntry(distributor_id=distributor_id, entry_type=entry_type, amount=q2(amount), entry_date=entry_date,
                               source_type=source_type, source_id=source_id, ref=ref, memo=(memo or None) and memo[:200], created_by=actor.id)
    db.add(e)
    try:
        db.flush()
    except IntegrityError:
        raise ConflictError(f"{ref} has already been booked to the distributor's account.")
    return e


def allocated_to(db: Session, invoice_id: int) -> Decimal:
    return q2(Decimal(db.scalar(select(func.coalesce(func.sum(DistributorAllocation.amount), 0)).where(
        DistributorAllocation.invoice_id == invoice_id, DistributorAllocation.released_at.is_(None)))))


def outstanding(db: Session, inv: DistributorInvoice) -> Decimal:
    return q2(Decimal(inv.total) - allocated_to(db, inv.id)) if inv.status == "posted" else Decimal(0)


def source_info(db: Session, source_type: str, source_id: int) -> tuple[int, Decimal, str]:
    """(distributor_id, amount available to allocate, ref) for a payment, credit note (return) or adjustment."""
    if source_type == "payment":
        p = db.get(DistributorPayment, source_id)
        if p is None or p.status != "posted":
            raise BusinessRuleError("That payment is not posted, so it cannot be allocated.")
        return p.distributor_id, Decimal(p.amount), p.ref
    if source_type == "credit_note":
        r = db.get(DistributorReturn, source_id)
        if r is None or r.status != "credited":
            raise BusinessRuleError("That credit note has not been approved, so it cannot be allocated.")
        return r.distributor_id, Decimal(r.credit_value or 0), r.ref
    a = db.get(DistributorAdjustment, source_id)
    if a is None or a.status != "approved" or a.kind == "debit":
        raise BusinessRuleError("That adjustment cannot be allocated.")
    return a.distributor_id, Decimal(a.amount), a.ref


def source_allocated(db: Session, source_type: str, source_id: int) -> Decimal:
    return q2(Decimal(db.scalar(select(func.coalesce(func.sum(DistributorAllocation.amount), 0)).where(
        DistributorAllocation.source_type == source_type, DistributorAllocation.source_id == source_id,
        DistributorAllocation.released_at.is_(None)))))


def allocate(db: Session, *, actor: User, distributor_id: int, source_type: str, source_id: int, allocations: list[dict]) -> Decimal:
    """Apply part of a payment / credit to specific dispatches. Locks the invoices in id order (no deadlocks between
    simultaneous payments) and refuses to allocate more than a dispatch still owes or more than the source holds."""
    if not allocations:
        return Decimal(0)
    dist, amount, ref = source_info(db, source_type, source_id)
    if dist != distributor_id:
        raise BusinessRuleError(f"{ref} belongs to a different distributor.")
    ids = [a["invoice_id"] for a in allocations]
    if len(set(ids)) != len(ids):
        raise BusinessRuleError("The same dispatch appears twice in the allocation.")
    locked = {i.id: i for i in db.scalars(select(DistributorInvoice).where(DistributorInvoice.id.in_(ids)).order_by(DistributorInvoice.id)
                                          .with_for_update().execution_options(populate_existing=True)).all()}
    total = Decimal(0)
    for a in allocations:
        inv = locked.get(a["invoice_id"])
        if inv is None or inv.distributor_id != distributor_id:
            raise BusinessRuleError("One of the dispatches was not found for this distributor.")
        amt = q2(a["amount"])
        if amt <= 0:
            raise BusinessRuleError("Allocation amounts must be greater than zero.")
        if inv.status != "posted":
            raise BusinessRuleError(f"{inv.ref} is {inv.status}; only posted dispatches can be settled.")
        out = outstanding(db, inv)
        if amt > out:
            raise BusinessRuleError(f"{inv.ref}: {amt} is more than the {out} still outstanding.")
        total += amt
        db.add(DistributorAllocation(source_type=source_type, source_id=source_id, invoice_id=inv.id, amount=amt, created_by=actor.id))
        db.flush()
    held = source_allocated(db, source_type, source_id)
    if held > amount:
        raise BusinessRuleError(f"{ref}: only {q2(amount - (held - total))} is left to allocate.")
    return total


def oldest_first(db: Session, distributor_id: int, amount: Decimal) -> list[dict]:
    """Allocation plan that settles the oldest dispatches first, up to `amount`."""
    plan, left = [], q2(amount)
    for inv in db.scalars(select(DistributorInvoice).where(DistributorInvoice.distributor_id == distributor_id,
                                                           DistributorInvoice.status == "posted")
                          .order_by(DistributorInvoice.due_date, DistributorInvoice.id)).all():
        if left <= 0:
            break
        out = outstanding(db, inv)
        if out > 0:
            take = min(out, left)
            plan.append({"invoice_id": inv.id, "amount": take})
            left -= take
    return plan


def release_allocations(db: Session, source_type: str, source_id: int) -> None:
    for a in db.scalars(select(DistributorAllocation).where(DistributorAllocation.source_type == source_type, DistributorAllocation.source_id == source_id,
                                                            DistributorAllocation.released_at.is_(None))).all():
        a.released_at = func.now()
    db.flush()


def unallocated_total(db: Session, distributor_id: int) -> Decimal:
    """Money and credits received but not yet applied to a specific dispatch (a credit on account)."""
    paid = db.scalar(select(func.coalesce(func.sum(DistributorPayment.amount), 0)).where(
        DistributorPayment.distributor_id == distributor_id, DistributorPayment.status == "posted"))
    credits = db.scalar(select(func.coalesce(func.sum(DistributorReturn.credit_value), 0)).where(
        DistributorReturn.distributor_id == distributor_id, DistributorReturn.status == "credited"))
    adj = db.scalar(select(func.coalesce(func.sum(DistributorAdjustment.amount), 0)).where(
        DistributorAdjustment.distributor_id == distributor_id, DistributorAdjustment.status == "approved",
        DistributorAdjustment.kind.in_(("credit", "write_off"))))
    used = db.scalar(select(func.coalesce(func.sum(DistributorAllocation.amount), 0)).join(
        DistributorInvoice, DistributorInvoice.id == DistributorAllocation.invoice_id).where(
        DistributorInvoice.distributor_id == distributor_id, DistributorAllocation.released_at.is_(None)))
    return q2(Decimal(paid) + Decimal(credits) + Decimal(adj) - Decimal(used))


# ---- aging, statements, integrity ------------------------------------------------------------------------------------
BUCKETS = (("current", None, 0), ("1_30", 1, 30), ("31_60", 31, 60), ("61_90", 61, 90), ("over_90", 91, None))


def aging(db: Session, distributor_id: int, as_of: date | None = None) -> dict:
    today = as_of or date.today()
    out = {b[0]: Decimal(0) for b in BUCKETS}
    rows = []
    for inv in db.scalars(select(DistributorInvoice).where(DistributorInvoice.distributor_id == distributor_id,
                                                           DistributorInvoice.status == "posted").order_by(DistributorInvoice.due_date, DistributorInvoice.id)).all():
        o = outstanding(db, inv)
        if o <= 0:
            continue
        late = (today - inv.due_date).days
        key = "current" if late <= 0 else "1_30" if late <= 30 else "31_60" if late <= 60 else "61_90" if late <= 90 else "over_90"
        out[key] += o
        rows.append({"invoice_id": inv.id, "ref": inv.ref, "kind": inv.kind, "invoice_date": inv.invoice_date, "due_date": inv.due_date,
                     "total": str(inv.total), "outstanding": str(o), "days_overdue": max(late, 0), "bucket": key})
    overdue = sum((v for k, v in out.items() if k != "current"), Decimal(0))
    return {"as_of": today, "buckets": {k: str(q2(v)) for k, v in out.items()}, "overdue": str(q2(overdue)),
            "outstanding_invoices": str(q2(sum(out.values(), Decimal(0)))), "unallocated_credit": str(unallocated_total(db, distributor_id)),
            "balance": str(balance(db, distributor_id)), "invoices": rows}


def month_bounds(month: str) -> tuple[date, date]:
    try:
        y, m = (int(x) for x in month.split("-"))
        first = date(y, m, 1)
    except Exception:
        raise BusinessRuleError("Month must look like 2026-11.")
    return first, date(y, m, calendar.monthrange(y, m)[1])


def statement(db: Session, distributor_id: int, month: str) -> dict:
    """One month: opening (everything before it, carried forward automatically), dated entries with a running balance,
    totals by kind and the closing balance, with the reconciliation shown."""
    first, last = month_bounds(month)
    opening = balance(db, distributor_id, before=first)
    rows = db.scalars(select(DistributorLedgerEntry).where(
        DistributorLedgerEntry.distributor_id == distributor_id, DistributorLedgerEntry.entry_date >= first,
        DistributorLedgerEntry.entry_date <= last).order_by(DistributorLedgerEntry.entry_date, DistributorLedgerEntry.id)).all()
    run = opening
    entries, charges, credits, payments = [], Decimal(0), Decimal(0), Decimal(0)
    for e in rows:
        amt = Decimal(e.amount)
        run += amt
        if e.entry_type == "invoice":
            charges += amt
        elif e.entry_type in ("credit_note", "adjustment"):
            credits += -amt
        else:                                    # payment or payment_reversal
            payments += -amt
        entries.append({"date": e.entry_date, "ref": e.ref, "type": e.entry_type, "memo": e.memo,
                        "charge": str(q2(amt)) if amt > 0 and e.entry_type == "invoice" else None,
                        "credit": str(q2(-amt)) if amt < 0 and e.entry_type in ("credit_note", "adjustment") else None,
                        "payment": str(q2(-amt)) if e.entry_type in ("payment", "payment_reversal") else None,
                        "amount": str(q2(amt)), "balance": str(q2(run))})
    closing = q2(run)
    check = q2(opening + charges - credits - payments)
    return {"month": month, "from": first, "to": last, "opening_balance": str(opening), "charges": str(q2(charges)),
            "credits": str(q2(credits)), "payments": str(q2(payments)), "closing_balance": str(closing), "entries": entries,
            "reconciles": check == closing and closing == balance(db, distributor_id, before=last + timedelta(days=1))}


def integrity(db: Session) -> list[str]:
    """Re-derive each balance from the documents and compare with the ledger; check allocations never exceed what they come from."""
    problems: list[str] = []
    for d in db.scalars(select(Distributor)).all():
        led = balance(db, d.id)
        charged = db.scalar(select(func.coalesce(func.sum(DistributorInvoice.total), 0)).where(
            DistributorInvoice.distributor_id == d.id, DistributorInvoice.status == "posted"))
        credited = db.scalar(select(func.coalesce(func.sum(DistributorReturn.credit_value), 0)).where(
            DistributorReturn.distributor_id == d.id, DistributorReturn.status == "credited"))
        paid = db.scalar(select(func.coalesce(func.sum(DistributorPayment.amount), 0)).where(
            DistributorPayment.distributor_id == d.id, DistributorPayment.status == "posted"))
        adj = db.scalar(select(func.coalesce(func.sum(DistributorAdjustment.amount), 0)).where(
            DistributorAdjustment.distributor_id == d.id, DistributorAdjustment.status == "approved",
            DistributorAdjustment.kind.in_(("credit", "write_off"))))
        derived = q2(Decimal(charged) - Decimal(credited) - Decimal(paid) - Decimal(adj))
        if derived != led:
            problems.append(f"{d.code}: ledger balance {led} differs from the documents {derived}")
        out = sum((outstanding(db, i) for i in db.scalars(select(DistributorInvoice).where(
            DistributorInvoice.distributor_id == d.id, DistributorInvoice.status == "posted")).all()), Decimal(0))
        if q2(out - unallocated_total(db, d.id)) != led:
            problems.append(f"{d.code}: open dispatches less unapplied credit does not equal the balance {led}")
        for i in db.scalars(select(DistributorInvoice).where(DistributorInvoice.distributor_id == d.id, DistributorInvoice.status == "posted")).all():
            if allocated_to(db, i.id) > Decimal(i.total):
                problems.append(f"{i.ref}: more allocated than the dispatch total")
    for st, sid in db.execute(select(DistributorAllocation.source_type, DistributorAllocation.source_id).where(
            DistributorAllocation.released_at.is_(None)).distinct()).all():
        try:
            _, amount, ref = source_info(db, st, sid)
        except BusinessRuleError:
            problems.append(f"{st} #{sid}: allocations exist for a document that is not in a postable state")
            continue
        if source_allocated(db, st, sid) > amount:
            problems.append(f"{ref}: allocated more than it is worth")
    return problems
