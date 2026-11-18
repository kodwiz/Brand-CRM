"""Supplier payments: partial payments, allocation to invoices, and reversal."""
from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from backend.models import PaymentAllocation, SupplierInvoice, SupplierPayment, User
from backend.models.purchasing import PAYMENT_METHODS
from backend.services import audit_service, invoice_service, ledger, supplier_service
from backend.services.errors import BusinessRuleError, ConflictError, NotFoundError, SelfApprovalError

q2 = supplier_service.q2


def get_payment(db: Session, payment_id: int, *, lock: bool = False) -> SupplierPayment:
    q = select(SupplierPayment).where(SupplierPayment.id == payment_id)
    if lock:
        q = q.with_for_update().execution_options(populate_existing=True)
    p = db.scalar(q)
    if p is None:
        raise NotFoundError("Payment not found")
    return p


def allocated(p: SupplierPayment) -> Decimal:
    return q2(sum((Decimal(a.amount) for a in p.allocations if a.released_at is None), Decimal(0)))


def unallocated(p: SupplierPayment) -> Decimal:
    return q2(Decimal(p.amount) - allocated(p)) if p.status == "posted" else Decimal(0)


def _apply(db: Session, payment: SupplierPayment, allocations: list[dict]) -> Decimal:
    """Lock the invoices (in id order, so concurrent payments cannot deadlock) and allocate. Each allocation may not exceed
    what is still outstanding, which is what stops two simultaneous payments both settling the same invoice."""
    if not allocations:
        return Decimal(0)
    ids = [a["invoice_id"] for a in allocations]
    if len(set(ids)) != len(ids):
        raise BusinessRuleError("The same invoice appears twice in the allocation.")
    locked = {i.id: i for i in db.scalars(select(SupplierInvoice).where(SupplierInvoice.id.in_(ids)).order_by(SupplierInvoice.id)
                                          .with_for_update().execution_options(populate_existing=True)).all()}
    total = Decimal(0)
    for a in allocations:
        inv = locked.get(a["invoice_id"])
        if inv is None:
            raise BusinessRuleError("One of the invoices was not found.")
        amt = q2(a["amount"])
        if amt <= 0:
            raise BusinessRuleError("Allocation amounts must be greater than zero.")
        if inv.supplier_id != payment.supplier_id:
            raise BusinessRuleError(f"{inv.ref} belongs to a different supplier.")
        if inv.status != "posted":
            raise BusinessRuleError(f"{inv.ref} is {inv.status}; payments can only be applied to posted invoices.")
        out = invoice_service.outstanding(db, inv)
        if amt > out:
            raise BusinessRuleError(f"{inv.ref}: {amt} is more than the {out} still outstanding.")
        payment.allocations.append(PaymentAllocation(invoice_id=inv.id, amount=amt))
        total += amt
    db.flush()
    return total


def create_payment(db: Session, *, actor: User, supplier_id: int, amount, method: str, paid_date: date,
                   reference: str | None, notes: str | None, allocations: list[dict], on_account: bool,
                   idempotency_key: str | None, ip: str | None) -> SupplierPayment:
    if idempotency_key:
        existing = db.scalar(select(SupplierPayment).where(SupplierPayment.idempotency_key == idempotency_key))
        if existing is not None:
            existing.replayed = True  # type: ignore[attr-defined]
            return existing
    supplier = supplier_service.get_supplier(db, supplier_id)
    amount = q2(amount)
    if amount <= 0:
        raise BusinessRuleError("The payment amount must be greater than zero.")
    if method not in PAYMENT_METHODS:
        raise BusinessRuleError(f"Payment method must be one of: {', '.join(PAYMENT_METHODS)}.")
    if paid_date > date.today():
        raise BusinessRuleError("The payment date cannot be in the future.")
    if method in ("cheque", "bank_transfer") and not (reference or "").strip():
        raise BusinessRuleError("Enter the cheque number or bank transaction reference.")
    if sum((q2(a["amount"]) for a in allocations), Decimal(0)) > amount:
        raise BusinessRuleError("The allocations add up to more than the payment amount.")
    pay = SupplierPayment(ref=ledger.next_ref(db, "supplier_payment_ref_seq", "SPY"), supplier_id=supplier.id, amount=amount,
                          method=method, paid_date=paid_date, reference=(reference or None), notes=notes,
                          idempotency_key=idempotency_key, created_by=actor.id)
    db.add(pay)
    db.flush()
    used = _apply(db, pay, allocations)
    if amount - used > 0 and not on_account:
        raise BusinessRuleError(f"{amount - used} of this payment is not allocated to any invoice. "
                                "Allocate it, or choose to keep the remainder on account.")
    supplier_service.add_ledger_entry(db, supplier_id=supplier.id, entry_type="payment", amount=-amount, entry_date=paid_date,
                                      source_type="supplier_payment", source_id=pay.id, ref=pay.ref,
                                      memo=f"Payment {method.replace('_', ' ')}", actor=actor)
    audit_service.record(db, actor=actor, action="supplier_payment.created", entity_type="supplier_payment", entity_id=pay.id,
                         after={"ref": pay.ref, "supplier": supplier.code, "amount": str(amount), "allocated": str(used),
                                "on_account": str(amount - used), "method": method}, ip=ip)
    pay.replayed = False  # type: ignore[attr-defined]
    return pay


def allocate_more(db: Session, *, actor: User, payment_id: int, allocations: list[dict], ip: str | None) -> SupplierPayment:
    pay = get_payment(db, payment_id, lock=True)
    if pay.status != "posted":
        raise ConflictError(f"{pay.ref} is {pay.status}.")
    if sum((q2(a["amount"]) for a in allocations), Decimal(0)) > unallocated(pay):
        raise BusinessRuleError(f"Only {unallocated(pay)} of {pay.ref} is still unallocated.")
    used = _apply(db, pay, allocations)
    audit_service.record(db, actor=actor, action="supplier_payment.allocated", entity_type="supplier_payment", entity_id=pay.id,
                         after={"ref": pay.ref, "allocated_now": str(used), "still_on_account": str(unallocated(pay))}, ip=ip)
    return pay


def reverse_payment(db: Session, *, actor: User, payment_id: int, reason: str, ip: str | None) -> SupplierPayment:
    if not (reason or "").strip():
        raise BusinessRuleError("A reason is required to reverse a payment.")
    pay = get_payment(db, payment_id, lock=True)
    if pay.status != "posted":
        raise ConflictError(f"{pay.ref} is already {pay.status}.")
    if pay.created_by == actor.id:
        raise SelfApprovalError("You cannot reverse a payment you recorded yourself. Ask another authorised user.")
    now = datetime.now(timezone.utc)
    ids = sorted({a.invoice_id for a in pay.allocations if a.released_at is None})
    if ids:  # lock the invoices the payment settled, in order, before releasing
        db.scalars(select(SupplierInvoice).where(SupplierInvoice.id.in_(ids)).order_by(SupplierInvoice.id).with_for_update()).all()
    for a in pay.allocations:
        if a.released_at is None:
            a.released_at = now
    pay.status = "reversed"
    pay.reversed_by, pay.reversed_at, pay.reverse_reason = actor.id, now, reason
    supplier_service.add_ledger_entry(db, supplier_id=pay.supplier_id, entry_type="payment_reversal", amount=Decimal(pay.amount),
                                      entry_date=date.today(), source_type="supplier_payment", source_id=pay.id, ref=pay.ref,
                                      memo=f"Reversal of {pay.ref}", actor=actor)
    db.flush()
    audit_service.record(db, actor=actor, action="supplier_payment.reversed", entity_type="supplier_payment", entity_id=pay.id,
                         reason=reason, after={"ref": pay.ref, "amount": str(pay.amount)}, ip=ip)
    return pay
