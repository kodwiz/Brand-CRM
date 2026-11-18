"""Supplier debit/credit adjustments. They change what we owe, so they need an independent approver."""
from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.models import SupplierAdjustment, SupplierInvoice, SupplierReturn, User
from backend.services import audit_service, invoice_service, ledger, supplier_service
from backend.services.errors import BusinessRuleError, ConflictError, NotFoundError, SelfApprovalError

q2 = supplier_service.q2


def get_adjustment(db: Session, adj_id: int, *, lock: bool = False) -> SupplierAdjustment:
    q = select(SupplierAdjustment).where(SupplierAdjustment.id == adj_id)
    if lock:
        q = q.with_for_update().execution_options(populate_existing=True)
    a = db.scalar(q)
    if a is None:
        raise NotFoundError("Adjustment not found")
    return a


def request_adjustment(db: Session, *, actor: User, supplier_id: int, kind: str, amount, adjustment_date: date,
                       document_no: str | None, reason: str, invoice_id: int | None, supplier_return_id: int | None,
                       ip: str | None) -> SupplierAdjustment:
    supplier = supplier_service.get_supplier(db, supplier_id)
    if kind not in ("reduce_payable", "increase_payable"):
        raise BusinessRuleError("Choose whether this reduces or increases what we owe the supplier.")
    amount = q2(amount)
    if amount <= 0:
        raise BusinessRuleError("The amount must be greater than zero.")
    if not (reason or "").strip():
        raise BusinessRuleError("A reason is required.")
    if adjustment_date > date.today():
        raise BusinessRuleError("The date cannot be in the future.")
    if invoice_id:
        inv = db.get(SupplierInvoice, invoice_id)
        if inv is None or inv.supplier_id != supplier_id or inv.status != "posted":
            raise BusinessRuleError("Choose a posted invoice from this supplier.")
        if kind != "reduce_payable":
            raise BusinessRuleError("Only a credit from the supplier can be applied against an invoice.")
    if supplier_return_id:
        ret = db.get(SupplierReturn, supplier_return_id)
        if ret is None or ret.supplier_id != supplier_id or ret.status != "dispatched":
            raise BusinessRuleError("Choose a dispatched return for this supplier that is still waiting for its credit.")
        if kind != "reduce_payable":
            raise BusinessRuleError("A return credit reduces what we owe.")
    a = SupplierAdjustment(ref=ledger.next_ref(db, "supplier_adjustment_ref_seq", "SAD"), supplier_id=supplier_id, kind=kind,
                           amount=amount, adjustment_date=adjustment_date, document_no=document_no, reason=reason.strip(),
                           invoice_id=invoice_id, supplier_return_id=supplier_return_id, requested_by=actor.id)
    db.add(a)
    db.flush()
    audit_service.record(db, actor=actor, action="supplier_adjustment.requested", entity_type="supplier_adjustment",
                         entity_id=a.id, reason=reason, after={"ref": a.ref, "supplier": supplier.code, "kind": kind,
                                                               "amount": str(amount)}, ip=ip)
    return a


def approve(db: Session, *, actor: User, adj_id: int, note: str | None, ip: str | None) -> SupplierAdjustment:
    a = get_adjustment(db, adj_id, lock=True)
    if a.status != "pending":
        raise ConflictError(f"{a.ref} is already {a.status}.")
    if a.requested_by == actor.id:
        raise SelfApprovalError("You cannot approve an adjustment you requested yourself. Ask another authorised user.")
    if a.invoice_id:
        inv = db.scalar(select(SupplierInvoice).where(SupplierInvoice.id == a.invoice_id).with_for_update()
                        .execution_options(populate_existing=True))
        out = invoice_service.outstanding(db, inv)
        if Decimal(a.amount) > out:
            raise BusinessRuleError(f"{inv.ref} has only {out} outstanding; the credit of {a.amount} no longer fits.")
    signed = -Decimal(a.amount) if a.kind == "reduce_payable" else Decimal(a.amount)
    supplier_service.add_ledger_entry(db, supplier_id=a.supplier_id, entry_type="adjustment", amount=signed,
                                      entry_date=a.adjustment_date, source_type="supplier_adjustment", source_id=a.id, ref=a.ref,
                                      memo=(a.document_no and f"Supplier note {a.document_no}") or a.reason[:100], actor=actor)
    a.status, a.decided_by, a.decided_at, a.decision_note = "approved", actor.id, datetime.now(timezone.utc), note
    if a.supplier_return_id:
        ret = db.scalar(select(SupplierReturn).where(SupplierReturn.id == a.supplier_return_id).with_for_update())
        ret.status = "credited"
    db.flush()
    audit_service.record(db, actor=actor, action="supplier_adjustment.approved", entity_type="supplier_adjustment", entity_id=a.id,
                         reason=note, after={"ref": a.ref, "amount": str(a.amount), "kind": a.kind}, ip=ip)
    return a


def reject(db: Session, *, actor: User, adj_id: int, note: str, ip: str | None) -> SupplierAdjustment:
    if not (note or "").strip():
        raise BusinessRuleError("A reason is required to reject an adjustment.")
    a = get_adjustment(db, adj_id, lock=True)
    if a.status != "pending":
        raise ConflictError(f"{a.ref} is already {a.status}.")
    if a.requested_by == actor.id:
        raise SelfApprovalError("You cannot reject your own request; cancel it instead.")
    a.status, a.decided_by, a.decided_at, a.decision_note = "rejected", actor.id, datetime.now(timezone.utc), note
    db.flush()
    audit_service.record(db, actor=actor, action="supplier_adjustment.rejected", entity_type="supplier_adjustment", entity_id=a.id,
                         reason=note, after={"ref": a.ref}, ip=ip)
    return a


def cancel(db: Session, *, actor: User, adj_id: int, ip: str | None) -> SupplierAdjustment:
    a = get_adjustment(db, adj_id, lock=True)
    if a.status != "pending":
        raise ConflictError(f"{a.ref} is already {a.status}.")
    if a.requested_by != actor.id:
        raise BusinessRuleError("Only the person who requested an adjustment can cancel it.")
    a.status = "cancelled"
    db.flush()
    audit_service.record(db, actor=actor, action="supplier_adjustment.cancelled", entity_type="supplier_adjustment",
                         entity_id=a.id, after={"ref": a.ref}, ip=ip)
    return a
