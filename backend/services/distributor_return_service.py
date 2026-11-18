"""Goods coming back from a distributor.

Request -> receive (stock goes to INSPECTION, never to saleable stock) -> inspect (restock / damaged / expired and how many
units earn credit) -> credit approved by someone else (ledger credit note at the price originally charged, applied to the
dispatch it came from). An exchange is a return plus a replacement dispatch that points at it; history is never edited.
"""
from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal

from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from backend.models import (
    Distributor, DistributorInvoice, DistributorInvoiceLine, DistributorReturn, DistributorReturnLine, Location, User,
)
from backend.services import audit_service, distributor_service as ds, ledger
from backend.services.errors import BusinessRuleError, ConflictError, NotFoundError, SelfApprovalError
from backend.services.ledger import Spec

q2, q4 = ds.q2, ds.q4
DISPOSITION = {"restock": "good", "damaged": "damaged", "expired": "expired"}


def get_return(db: Session, return_id: int, *, lock: bool = False) -> DistributorReturn:
    q = select(DistributorReturn).where(DistributorReturn.id == return_id)
    if lock:
        q = q.with_for_update().execution_options(populate_existing=True)
    r = db.scalar(q)
    if r is None:
        raise NotFoundError("Return not found")
    return r


def _audit(db, actor, action, r: DistributorReturn, ip, reason=None, **after):
    audit_service.record(db, actor=actor, action=action, entity_type="distributor_return", entity_id=r.id, location_id=r.location_id,
                         reason=reason, after={"ref": r.ref, "status": r.status, **after}, ip=ip)


def _committed(db: Session, invoice_line_id: int, exclude: int | None = None) -> int:
    """Units of a dispatch line already on live returns: the requested quantity until goods arrive, then what arrived."""
    q = select(DistributorReturnLine, DistributorReturn.status).join(DistributorReturn, DistributorReturn.id == DistributorReturnLine.return_id).where(
        DistributorReturnLine.invoice_line_id == invoice_line_id, DistributorReturn.status.in_(("requested", "received", "inspected", "credited")))
    if exclude:
        q = q.where(DistributorReturn.id != exclude)
    return sum(l.qty_requested if st == "requested" else l.qty_received for l, st in db.execute(q).all())


def request_return(db: Session, *, actor: User, distributor_id: int, location_id: int, lines: list[dict], reason: str, return_date: date,
                   ip: str | None) -> DistributorReturn:
    d = ds.get_distributor(db, distributor_id, lock=True)
    if not (reason or "").strip():
        raise BusinessRuleError("Say why the goods are coming back.")
    loc = db.get(Location, location_id)
    if loc is None or not loc.is_active:
        raise BusinessRuleError("Choose an active location to receive the goods at.")
    if return_date > date.today():
        raise BusinessRuleError("The return date cannot be in the future.")
    if not lines:
        raise BusinessRuleError("Choose at least one line to return.")
    seen: set[int] = set()
    rows = []
    for ln in lines:
        il = db.get(DistributorInvoiceLine, ln["invoice_line_id"])
        inv = db.get(DistributorInvoice, il.invoice_id) if il else None
        if il is None or inv is None or inv.distributor_id != d.id or inv.status != "posted" or inv.kind != "dispatch":
            raise BusinessRuleError("A returned line does not belong to a posted dispatch of this distributor.")
        if il.id in seen:
            raise BusinessRuleError("The same dispatch line appears twice.")
        seen.add(il.id)
        n = int(ln["qty"])
        room = il.qty - _committed(db, il.id)
        if n <= 0 or n > room:
            raise BusinessRuleError(f"{inv.ref}: only {max(room, 0)} units can still be returned on this line (asked for {n}).")
        rows.append(DistributorReturnLine(invoice_line_id=il.id, variant_id=il.variant_id, batch_id=il.batch_id, qty_requested=n,
                                          unit_price=il.unit_price, unit_cost=q4(Decimal(il.stock_value) / il.qty) if il.stock_value is not None else None))
    r = DistributorReturn(ref=f"DRT-{db.scalar(text('select nextval(:s)'), {'s': 'distributor_return_ref_seq'}):06d}", distributor_id=d.id,
                          location_id=location_id, return_date=return_date, status="requested", reason=reason.strip(), requested_by=actor.id)
    r.lines = rows
    db.add(r)
    db.flush()
    _audit(db, actor, "distributor_return.requested", r, ip)
    return r


def cancel(db: Session, *, actor: User, return_id: int, reason: str, ip: str | None) -> DistributorReturn:
    r = get_return(db, return_id, lock=True)
    if r.status != "requested":
        raise ConflictError(f"{r.ref} is {r.status}; goods may already be back, so it cannot be cancelled.")
    if not (reason or "").strip():
        raise BusinessRuleError("Say why the return is cancelled.")
    r.status, r.decision_note = "cancelled", reason.strip()
    db.flush()
    _audit(db, actor, "distributor_return.cancelled", r, ip, reason=reason)
    return r


def receive(db: Session, *, actor: User, return_id: int, lines: list[dict], ip: str | None) -> DistributorReturn:
    r = get_return(db, return_id, lock=True)
    if r.status != "requested":
        raise ConflictError(f"{r.ref} is {r.status}; goods can only be received for a return that was requested.")
    given = {ln["line_id"]: int(ln["qty_received"]) for ln in lines}
    by_id = {l.id: l for l in r.lines}
    if set(given) - set(by_id):
        raise BusinessRuleError("A received line does not belong to this return.")
    specs, any_in = [], False
    for l in r.lines:
        n = given.get(l.id, 0)
        if n < 0 or n > l.qty_requested:
            raise BusinessRuleError(f"Line {l.variant_id}: received {n}, but only {l.qty_requested} were expected.")
        l.qty_received = n
        if n:
            any_in = True
            cost = Decimal(l.unit_cost) if l.unit_cost is not None else Decimal(0)
            specs.append(Spec(r.location_id, l.variant_id, l.batch_id, "inspection", n, "in", value=ledger.q4(cost * n)))
    if not any_in:
        raise BusinessRuleError("Enter the quantity that actually arrived on at least one line (or cancel the return).")
    txn = ledger.post(db, txn_type="return_intake", location_id=r.location_id, specs=specs, actor=actor,
                      reason=f"Distributor return {r.ref}", source_type="distributor_return", source_id=r.id)
    r.intake_txn_id, r.status, r.received_by, r.received_at = txn.id, "received", actor.id, func.now()
    db.flush()
    _audit(db, actor, "distributor_return.received", r, ip, stock_txn=txn.ref)
    return r


def inspect(db: Session, *, actor: User, return_id: int, lines: list[dict], ip: str | None) -> DistributorReturn:
    r = get_return(db, return_id, lock=True)
    if r.status != "received":
        raise ConflictError(f"{r.ref} is {r.status}; only received goods can be inspected.")
    given = {ln["line_id"]: ln for ln in lines}
    by_id = {l.id: l for l in r.lines}
    if set(given) - set(by_id):
        raise BusinessRuleError("An inspected line does not belong to this return.")
    specs: list[Spec] = []
    value = Decimal(0)
    for l in r.lines:
        if l.qty_received == 0:
            continue
        ln = given.get(l.id)
        if ln is None:
            raise BusinessRuleError(f"Line {l.variant_id}: record what happened to each of the {l.qty_received} units received.")
        parts = {k: int(ln.get(k) or 0) for k in DISPOSITION}
        if min(parts.values()) < 0 or sum(parts.values()) != l.qty_received:
            raise BusinessRuleError(f"Line {l.variant_id}: restocked + damaged + expired must equal the {l.qty_received} units received.")
        credit_qty = int(ln["credit_qty"]) if ln.get("credit_qty") is not None else l.qty_received
        if credit_qty < 0 or credit_qty > l.qty_received:
            raise BusinessRuleError(f"Line {l.variant_id}: credited units must be between 0 and {l.qty_received}.")
        l.qty_restock, l.qty_damaged, l.qty_expired, l.credit_qty = parts["restock"], parts["damaged"], parts["expired"], credit_qty
        l.inspection_note = ln.get("note")
        for k, bucket in DISPOSITION.items():
            if parts[k]:
                specs.append(Spec(r.location_id, l.variant_id, l.batch_id, "inspection", parts[k], "move", to_bucket=bucket))
        value += Decimal(l.unit_price) * credit_qty
    if specs:
        ledger.post(db, txn_type="return_disposition", location_id=r.location_id, specs=specs, actor=actor,
                    reason=f"Inspection of {r.ref}", source_type="distributor_return", source_id=r.id)
    r.status, r.inspected_by, r.inspected_at, r.credit_value = "inspected", actor.id, func.now(), q2(value)
    db.flush()
    _audit(db, actor, "distributor_return.inspected", r, ip, credit_value=str(r.credit_value))
    return r


def decide(db: Session, *, actor: User, return_id: int, approve: bool, note: str, ip: str | None) -> DistributorReturn:
    """Approve (credit note) or reject (no credit). Not by the person who requested or inspected it."""
    r = get_return(db, return_id, lock=True)
    d = ds.get_distributor(db, r.distributor_id, lock=True)
    r = get_return(db, return_id, lock=True)
    if r.status != "inspected":
        raise ConflictError(f"{r.ref} is {r.status}; credit is decided after inspection.")
    if actor.id in (r.requested_by, r.inspected_by):
        raise SelfApprovalError("You cannot decide the credit for a return you requested or inspected yourself.")
    if not (note or "").strip():
        raise BusinessRuleError("Say why you are approving or rejecting the credit.")
    r.decided_by, r.decided_at, r.decision_note = actor.id, datetime.now(timezone.utc), note.strip()
    if not approve or Decimal(r.credit_value or 0) == 0:
        r.status = "rejected"
        db.flush()
        _audit(db, actor, "distributor_return.rejected", r, ip, reason=note)
        return r
    r.status = "credited"
    db.flush()
    ds.add_ledger_entry(db, distributor_id=d.id, entry_type="credit_note", amount=-Decimal(r.credit_value), entry_date=date.today(),
                        source_type="distributor_return", source_id=r.id, ref=r.ref, memo=f"Credit for returned goods: {r.reason}"[:200], actor=actor)
    # apply the credit to the dispatch(es) the goods came from, as far as they are still owed
    plan: dict[int, Decimal] = {}
    for l in r.lines:
        if l.credit_qty:
            inv_id = db.get(DistributorInvoiceLine, l.invoice_line_id).invoice_id
            plan[inv_id] = plan.get(inv_id, Decimal(0)) + Decimal(l.unit_price) * l.credit_qty
    allocs = []
    for inv_id, amt in sorted(plan.items()):
        inv = db.get(DistributorInvoice, inv_id)
        take = min(q2(amt), ds.outstanding(db, inv))
        if take > 0:
            allocs.append({"invoice_id": inv_id, "amount": take})
    ds.allocate(db, actor=actor, distributor_id=d.id, source_type="credit_note", source_id=r.id, allocations=allocs)
    _audit(db, actor, "distributor_return.credited", r, ip, reason=note, credit_value=str(r.credit_value))
    return r
