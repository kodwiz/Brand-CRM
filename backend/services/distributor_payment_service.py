"""Distributor payments, allocations and adjustments (write-offs, credits, debits).

* A payment reduces the receivable the moment it is posted, whether or not it has been applied to specific dispatches;
  the part not applied is a credit on account that can be allocated later.
* A payment dated further back than the allowance waits for approval (status pending): no ledger entry, no allocation.
* Reversal and every adjustment need someone other than the person who entered them, with distributors.approve.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from backend.models import DistributorAdjustment, DistributorInvoice, DistributorPayment, User
from backend.models.distributors import PAYMENT_METHODS
from backend.services import audit_service, distributor_dispatch_service as dds, distributor_service as ds, settings_service
from backend.services.errors import BusinessRuleError, ConflictError, NotFoundError, SelfApprovalError

q2 = ds.q2


def get_payment(db: Session, payment_id: int, *, lock: bool = False) -> DistributorPayment:
    q = select(DistributorPayment).where(DistributorPayment.id == payment_id)
    if lock:
        q = q.with_for_update().execution_options(populate_existing=True)
    p = db.scalar(q)
    if p is None:
        raise NotFoundError("Payment not found")
    return p


def _audit(db, actor, action, p, ip, reason=None, **after):
    audit_service.record(db, actor=actor, action=action, entity_type="distributor_payment", entity_id=p.id, reason=reason,
                         after={"ref": p.ref, "status": p.status, "amount": str(p.amount), **after}, ip=ip)


def allocated(db: Session, p: DistributorPayment) -> Decimal:
    return ds.source_allocated(db, "payment", p.id)


def _post(db: Session, actor: User, p: DistributorPayment, d, ip) -> None:
    plan = p.pending_allocations or []
    ds.add_ledger_entry(db, distributor_id=d.id, entry_type="payment", amount=-Decimal(p.amount), entry_date=p.paid_date,
                        source_type="distributor_payment", source_id=p.id, ref=p.ref, memo=f"Payment received ({p.method})", actor=actor)
    p.status, p.pending_allocations, p.hold_reason = "posted", None, None
    db.flush()
    ds.allocate(db, actor=actor, distributor_id=d.id, source_type="payment", source_id=p.id,
                allocations=[{"invoice_id": a["invoice_id"], "amount": Decimal(str(a["amount"]))} for a in plan])


def record_payment(db: Session, *, actor: User, distributor_id: int, amount, method: str, paid_date: date, reference: str | None,
                   allocations: list[dict] | None, auto_allocate: bool, notes: str | None, idempotency_key: str | None, ip: str | None) -> DistributorPayment:
    d = ds.get_distributor(db, distributor_id, lock=True)
    if idempotency_key:
        prev = db.scalar(select(DistributorPayment).where(DistributorPayment.distributor_id == d.id, DistributorPayment.idempotency_key == idempotency_key))
        if prev is not None:
            return prev
    amt = q2(amount)
    if amt <= 0:
        raise BusinessRuleError("The amount must be greater than zero.")
    if method not in PAYMENT_METHODS:
        raise BusinessRuleError("Unknown payment method.")
    if method in ("bank_transfer", "cheque", "card") and not (reference or "").strip():
        raise BusinessRuleError("A transaction or cheque reference is required for this payment method.")
    if paid_date > date.today():
        raise BusinessRuleError("The payment date cannot be in the future.")
    plan = [{"invoice_id": a["invoice_id"], "amount": q2(a["amount"])} for a in (allocations or [])]
    if sum((a["amount"] for a in plan), Decimal(0)) > amt:
        raise BusinessRuleError("The allocations add up to more than the payment.")
    if auto_allocate and not plan:
        plan = ds.oldest_first(db, d.id, amt)
    allow = int(settings_service.get(db, settings_service.DIST_BACKDATE_KEY))
    p = DistributorPayment(ref=f"DPY-{db.scalar(text('select nextval(:s)'), {'s': 'distributor_payment_ref_seq'}):06d}", distributor_id=d.id,
                           amount=amt, method=method, paid_date=paid_date, reference=(reference or None) and reference.strip(), notes=notes,
                           idempotency_key=idempotency_key, created_by=actor.id, status="posted",
                           pending_allocations=[{"invoice_id": a["invoice_id"], "amount": str(a["amount"])} for a in plan])
    db.add(p)
    db.flush()
    if paid_date < date.today() - timedelta(days=allow):
        p.status, p.hold_reason = "pending", f"backdated: dated {paid_date}, more than {allow} days ago"
        db.flush()
        _audit(db, actor, "distributor_payment.held", p, ip, reason=p.hold_reason)
        return p
    _post(db, actor, p, d, ip)
    _audit(db, actor, "distributor_payment.posted", p, ip)
    return p


def decide_payment(db: Session, *, actor: User, payment_id: int, approve: bool, note: str, ip: str | None) -> DistributorPayment:
    p = get_payment(db, payment_id, lock=True)
    d = ds.get_distributor(db, p.distributor_id, lock=True)
    p = get_payment(db, payment_id, lock=True)
    if p.status != "pending":
        raise ConflictError(f"{p.ref} is {p.status}; nothing is waiting for approval.")
    if p.created_by == actor.id:
        raise SelfApprovalError("You cannot approve a payment you entered yourself.")
    if not (note or "").strip():
        raise BusinessRuleError("Say why you are approving or rejecting this payment.")
    p.decided_by, p.decided_at, p.decision_note = actor.id, datetime.now(timezone.utc), note.strip()
    if not approve:
        p.status = "rejected"
        db.flush()
        _audit(db, actor, "distributor_payment.rejected", p, ip, reason=note)
        return p
    _post(db, actor, p, d, ip)
    _audit(db, actor, "distributor_payment.approved", p, ip, reason=note)
    return p


def allocate_more(db: Session, *, actor: User, distributor_id: int, source_type: str, source_id: int, allocations: list[dict], ip: str | None) -> Decimal:
    d = ds.get_distributor(db, distributor_id, lock=True)
    if source_type not in ds.CREDIT_SOURCES:
        raise BusinessRuleError("Unknown source to allocate.")
    total = ds.allocate(db, actor=actor, distributor_id=d.id, source_type=source_type, source_id=source_id,
                        allocations=[{"invoice_id": a["invoice_id"], "amount": q2(a["amount"])} for a in allocations])
    audit_service.record(db, actor=actor, action="distributor.allocated", entity_type="distributor", entity_id=d.id,
                         after={"source": f"{source_type}:{source_id}", "amount": str(total)}, ip=ip)
    return total


def reverse_payment(db: Session, *, actor: User, payment_id: int, reason: str, ip: str | None) -> DistributorPayment:
    p = get_payment(db, payment_id, lock=True)
    d = ds.get_distributor(db, p.distributor_id, lock=True)
    p = get_payment(db, payment_id, lock=True)
    if p.status != "posted":
        raise ConflictError(f"{p.ref} is {p.status}; only a posted payment can be reversed.")
    if p.created_by == actor.id:
        raise SelfApprovalError("You cannot reverse a payment you entered yourself.")
    if not (reason or "").strip():
        raise BusinessRuleError("Say why the payment is being reversed.")
    ds.release_allocations(db, "payment", p.id)
    p.status, p.reversed_by, p.reversed_at, p.reverse_reason = "reversed", actor.id, datetime.now(timezone.utc), reason.strip()
    db.flush()
    ds.add_ledger_entry(db, distributor_id=d.id, entry_type="payment_reversal", amount=Decimal(p.amount), entry_date=date.today(),
                        source_type="distributor_payment", source_id=p.id, ref=p.ref, memo=f"Payment reversed: {reason}"[:200], actor=actor)
    _audit(db, actor, "distributor_payment.reversed", p, ip, reason=reason)
    return p


# ---- adjustments ---------------------------------------------------------------------------------------------------------------
def request_adjustment(db: Session, *, actor: User, distributor_id: int, kind: str, amount, entry_date: date, invoice_id: int | None,
                       reason: str, ip: str | None) -> DistributorAdjustment:
    d = ds.get_distributor(db, distributor_id, lock=True)
    amt = q2(amount)
    if kind not in ("write_off", "credit", "debit"):
        raise BusinessRuleError("An adjustment is a write-off, a credit or a debit.")
    if amt <= 0:
        raise BusinessRuleError("The amount must be greater than zero.")
    if not (reason or "").strip():
        raise BusinessRuleError("Say why the adjustment is needed.")
    if entry_date > date.today():
        raise BusinessRuleError("The date cannot be in the future.")
    if invoice_id is not None:
        inv = db.get(DistributorInvoice, invoice_id)
        if inv is None or inv.distributor_id != d.id or inv.status != "posted":
            raise BusinessRuleError("Choose a posted dispatch of this distributor.")
        if kind != "debit" and amt > ds.outstanding(db, inv):
            raise BusinessRuleError(f"{inv.ref}: only {ds.outstanding(db, inv)} is still outstanding.")
    elif kind == "write_off":
        raise BusinessRuleError("A write-off must say which dispatch it clears.")
    a = DistributorAdjustment(ref=f"DAD-{db.scalar(text('select nextval(:s)'), {'s': 'distributor_adjustment_ref_seq'}):06d}", distributor_id=d.id,
                              kind=kind, amount=amt, entry_date=entry_date, invoice_id=invoice_id if kind != "debit" else None,
                              reason=reason.strip(), requested_by=actor.id)
    db.add(a)
    db.flush()
    audit_service.record(db, actor=actor, action="distributor_adjustment.requested", entity_type="distributor_adjustment", entity_id=a.id,
                         after={"ref": a.ref, "kind": kind, "amount": str(amt)}, reason=reason, ip=ip)
    return a


def decide_adjustment(db: Session, *, actor: User, adjustment_id: int, approve: bool, note: str, ip: str | None) -> DistributorAdjustment:
    a = db.scalar(select(DistributorAdjustment).where(DistributorAdjustment.id == adjustment_id).with_for_update().execution_options(populate_existing=True))
    if a is None:
        raise NotFoundError("Adjustment not found")
    d = ds.get_distributor(db, a.distributor_id, lock=True)
    db.refresh(a)
    if a.status != "pending":
        raise ConflictError(f"{a.ref} is {a.status}; it has already been decided.")
    if a.requested_by == actor.id:
        raise SelfApprovalError("You cannot approve an adjustment you requested yourself.")
    if not (note or "").strip():
        raise BusinessRuleError("Say why you are approving or rejecting this adjustment.")
    a.decided_by, a.decided_at, a.decision_note = actor.id, datetime.now(timezone.utc), note.strip()
    if not approve:
        a.status = "rejected"
        db.flush()
        audit_service.record(db, actor=actor, action="distributor_adjustment.rejected", entity_type="distributor_adjustment", entity_id=a.id,
                             after={"ref": a.ref}, reason=note, ip=ip)
        return a
    a.status = "approved"
    db.flush()
    if a.kind == "debit":
        inv = DistributorInvoice(ref=dds._ref(db, "DBN"), distributor_id=d.id, kind="debit_note", invoice_date=a.entry_date,
                                 due_date=a.entry_date + timedelta(days=d.payment_terms_days), status="posted", subtotal=a.amount,
                                 discount_total=0, total=a.amount, notes=a.reason, created_by=a.requested_by, posted_by=actor.id,
                                 posted_at=datetime.now(timezone.utc))
        db.add(inv)
        db.flush()
        a.result_invoice_id = inv.id
        ds.add_ledger_entry(db, distributor_id=d.id, entry_type="invoice", amount=Decimal(a.amount), entry_date=a.entry_date,
                            source_type="distributor_invoice", source_id=inv.id, ref=inv.ref, memo=f"Debit note: {a.reason}"[:200], actor=actor)
    else:
        ds.add_ledger_entry(db, distributor_id=d.id, entry_type="adjustment", amount=-Decimal(a.amount), entry_date=a.entry_date,
                            source_type="distributor_adjustment", source_id=a.id, ref=a.ref,
                            memo=f"{'Write-off' if a.kind == 'write_off' else 'Credit'}: {a.reason}"[:200], actor=actor)
        if a.invoice_id is not None:
            ds.allocate(db, actor=actor, distributor_id=d.id, source_type="adjustment", source_id=a.id,
                        allocations=[{"invoice_id": a.invoice_id, "amount": Decimal(a.amount)}])
    audit_service.record(db, actor=actor, action="distributor_adjustment.approved", entity_type="distributor_adjustment", entity_id=a.id,
                         after={"ref": a.ref, "kind": a.kind, "amount": str(a.amount)}, reason=note, ip=ip)
    return a
