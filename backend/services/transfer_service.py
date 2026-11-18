"""Inter-location stock transfers: Requested -> Approved -> Dispatched -> (Partially received) -> Received.

* Approval reserves stock at the source (no oversell); rejection/cancellation releases it.
* Dispatch removes stock from the source at its weighted-average cost and records that value as in-transit at the
  destination. The value is fixed at dispatch and is *not* part of the destination average until goods are received.
* Receipt moves in-transit units into good/damaged at the destination at the dispatch cost (the destination average
  absorbs it then). Partial receipts are allowed; every line can only be received once up to what was dispatched.
* A shortage (dispatched but never arrived) must be closed by a different user with transfers.approve_discrepancy; the
  missing units are written off from in-transit at their dispatch value.
"""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.models import Transfer, TransferEvent, TransferLine, User
from backend.services import audit_service, inventory_service, ledger, reservation_service
from backend.services.errors import BusinessRuleError, ConflictError, NotFoundError, SelfApprovalError
from backend.services.ledger import Spec

OPEN = ("requested", "approved", "dispatched", "partially_received", "discrepancy_review")


def get_transfer(db: Session, transfer_id: int, *, lock: bool = False) -> Transfer:
    q = select(Transfer).where(Transfer.id == transfer_id)
    if lock:
        q = q.with_for_update().execution_options(populate_existing=True)
    t = db.scalar(q)
    if t is None:
        raise NotFoundError("Transfer not found")
    return t


def _event(t: Transfer, actor: User, event: str, note: str | None = None) -> None:
    t.events.append(TransferEvent(event=event, actor_user_id=actor.id, actor_username=actor.username, note=note))


def _audit(db, actor, action, t, ip, reason=None, **after):
    audit_service.record(db, actor=actor, action=action, entity_type="transfer", entity_id=t.id,
                         location_id=t.from_location_id, reason=reason, after={"ref": t.ref, "status": t.status, **after}, ip=ip)


def create(db: Session, *, actor: User, from_location_id: int, to_location_id: int, lines: list[dict],
           notes: str | None, ip: str | None) -> Transfer:
    if from_location_id == to_location_id:
        raise BusinessRuleError("The source and destination must be different locations.")
    if not lines:
        raise BusinessRuleError("Add at least one line.")
    seen = set()
    t = Transfer(ref=ledger.next_ref(db, "transfer_ref_seq", "TRF"), from_location_id=from_location_id,
                 to_location_id=to_location_id, notes=notes, requested_by=actor.id)
    for i, ln in enumerate(lines, 1):
        v = inventory_service.get_variant(db, ln["variant_id"])
        qty = int(ln["qty"])
        if qty <= 0:
            raise BusinessRuleError(f"Line {i} ({v.sku}): quantity must be greater than zero.")
        if v.status == "inactive" or v.product.status == "inactive":
            raise BusinessRuleError(f"Line {i} ({v.sku}) is inactive and cannot be transferred.")
        key = (v.id, ln.get("batch_id"))
        if key in seen:
            raise BusinessRuleError(f"Line {i} ({v.sku}): the same item and batch appear twice.")
        seen.add(key)
        t.lines.append(TransferLine(variant_id=v.id, batch_id=ln.get("batch_id"), qty_requested=qty))
    _event(t, actor, "requested", notes)
    db.add(t)
    db.flush()
    _audit(db, actor, "transfer.requested", t, ip, lines=len(t.lines), to=to_location_id)
    return t


def _ref_id(t: Transfer) -> str:
    return str(t.id)


def approve(db: Session, *, actor: User, transfer_id: int, note: str | None, ip: str | None) -> Transfer:
    t = get_transfer(db, transfer_id, lock=True)
    if t.status != "requested":
        raise ConflictError(f"{t.ref} is {t.status}; it cannot be approved.")
    if t.requested_by == actor.id:
        raise SelfApprovalError("You cannot approve a transfer you requested yourself.")
    # reserve at source; a shortage here raises InsufficientStockError and nothing is saved
    resolved: list[TransferLine] = []
    for ln in sorted(t.lines, key=lambda x: (x.variant_id, x.batch_id or 0)):
        res = reservation_service.reserve(db, location_id=t.from_location_id, variant_id=ln.variant_id, qty=ln.qty_requested,
                                          ref_type="transfer", ref_id=_ref_id(t), batch_id=ln.batch_id, actor=actor)
        resolved.append(ln)
        # when the line named no batch, FEFO may have split it; record the first batch for display only
        if ln.batch_id is None and len(res) == 1:
            ln.batch_id = res[0].batch_id
    t.status = "approved"
    t.approved_by = actor.id
    t.approved_at = datetime.now(timezone.utc)
    _event(t, actor, "approved", note)
    db.flush()
    _audit(db, actor, "transfer.approved", t, ip, reason=note)
    return t


def reject(db: Session, *, actor: User, transfer_id: int, reason: str, ip: str | None) -> Transfer:
    if not (reason or "").strip():
        raise BusinessRuleError("Say why the transfer is rejected.")
    t = get_transfer(db, transfer_id, lock=True)
    if t.status != "requested":
        raise ConflictError(f"{t.ref} is {t.status}; only requested transfers can be rejected.")
    t.status = "rejected"
    t.decision_reason = reason
    _event(t, actor, "rejected", reason)
    db.flush()
    _audit(db, actor, "transfer.rejected", t, ip, reason=reason)
    return t


def cancel(db: Session, *, actor: User, transfer_id: int, reason: str, ip: str | None) -> Transfer:
    t = get_transfer(db, transfer_id, lock=True)
    if t.status not in ("requested", "approved"):
        raise ConflictError(f"{t.ref} is {t.status}; it can no longer be cancelled. Stock already left the source.")
    reservation_service.release(db, "transfer", _ref_id(t))
    t.status = "cancelled"
    t.decision_reason = reason
    _event(t, actor, "cancelled", reason)
    db.flush()
    _audit(db, actor, "transfer.cancelled", t, ip, reason=reason)
    return t


def dispatch(db: Session, *, actor: User, transfer_id: int, ip: str | None) -> Transfer:
    t = get_transfer(db, transfer_id, lock=True)
    if t.status != "approved":
        raise ConflictError(f"{t.ref} is {t.status}; it cannot be dispatched (already sent, or not approved).")
    reservations = reservation_service.active_for(db, "transfer", _ref_id(t))
    if not reservations:
        raise BusinessRuleError("No stock is reserved for this transfer.")
    # consume reservations first, then post the outflow (balance rows are locked in id order inside ledger.post)
    reservation_service.consume(db, "transfer", _ref_id(t))
    out_specs: list[Spec] = []
    for r in reservations:
        out_specs.append(Spec(t.from_location_id, r.variant_id, r.batch_id, "good", -r.qty, "out"))
    txn = ledger.post(db, txn_type="transfer_out", location_id=t.from_location_id, specs=out_specs, actor=actor,
                      reason=f"Dispatch {t.ref}", source_type="transfer", source_id=t.id)
    # fix the value of each dispatched batch and place it in transit at the destination
    out_lines = [m for m in txn.movements]
    transit_specs: list[Spec] = []
    by_key: dict[tuple[int, int | None], tuple[int, Decimal]] = {}
    for m in out_lines:
        qty, val = by_key.get((m.variant_id, m.batch_id), (0, Decimal("0")))
        by_key[(m.variant_id, m.batch_id)] = (qty + abs(m.qty_delta), val + abs(m.value_delta))
    for (variant_id, batch_id), (qty, val) in sorted(by_key.items(), key=lambda kv: (kv[0][0], kv[0][1] or 0)):
        transit_specs.append(Spec(t.to_location_id, variant_id, batch_id, "transit", qty, "transit", value=val))
    ledger.post(db, txn_type="transfer_transit", location_id=t.to_location_id, specs=transit_specs, actor=actor,
                reason=f"In transit {t.ref}", source_type="transfer", source_id=t.id)
    # one transfer line per dispatched (variant, batch). Reserved quantity == requested quantity, so each new line's
    # requested quantity equals what was dispatched (FEFO may have split a line across batches).
    for old in list(t.lines):
        t.lines.remove(old)
        db.delete(old)
    db.flush()
    for (variant_id, batch_id), (qty, val) in sorted(by_key.items(), key=lambda kv: (kv[0][0], kv[0][1] or 0)):
        t.lines.append(TransferLine(variant_id=variant_id, batch_id=batch_id, qty_requested=qty,
                                    qty_dispatched=qty, dispatch_value=val))
    t.status = "dispatched"
    t.dispatched_by = actor.id
    t.dispatched_at = datetime.now(timezone.utc)
    _event(t, actor, "dispatched")
    db.flush()
    _audit(db, actor, "transfer.dispatched", t, ip, txn=txn.ref, units=sum(l.qty_dispatched for l in t.lines))
    return t


def receive(db: Session, *, actor: User, transfer_id: int, lines: list[dict], notes: str | None, ip: str | None) -> Transfer:
    """lines: [{line_id, qty_good, qty_damaged}] -- may be partial; call again later for the remainder."""
    t = get_transfer(db, transfer_id, lock=True)
    if t.status not in ("dispatched", "partially_received"):
        raise ConflictError(f"{t.ref} is {t.status}; it cannot be received (not dispatched, or already complete).")
    by_id = {l.id: l for l in t.lines}
    specs: list[Spec] = []
    for item in lines:
        ln = by_id.get(item["line_id"])
        if ln is None:
            raise BusinessRuleError("A line does not belong to this transfer.")
        good, dmg = int(item.get("qty_good", 0)), int(item.get("qty_damaged", 0))
        if good < 0 or dmg < 0:
            raise BusinessRuleError("Quantities cannot be negative.")
        if good + dmg == 0:
            continue
        outstanding = ln.qty_dispatched - ln.qty_received_good - ln.qty_received_damaged - ln.qty_short
        if good + dmg > outstanding:
            v = inventory_service.get_variant(db, ln.variant_id)
            raise BusinessRuleError(f"{v.sku}: only {outstanding} units are still expected; you entered {good + dmg}.")
        unit = (ln.dispatch_value / ln.qty_dispatched) if ln.qty_dispatched else Decimal("0")
        settle = ledger.q4(unit * (good + dmg))
        remaining_value = ln.dispatch_value - ln.value_settled
        if good + dmg == outstanding:
            settle = remaining_value  # last receipt takes the exact remainder; no rounding drift
        specs.append(Spec(t.to_location_id, ln.variant_id, ln.batch_id, "transit", -(good + dmg), "transit", value=settle))
        if good:
            gv = ledger.q4(settle * good / (good + dmg)) if dmg else settle
            specs.append(Spec(t.to_location_id, ln.variant_id, ln.batch_id, "good", good, "in", value=gv))
            if dmg:
                specs.append(Spec(t.to_location_id, ln.variant_id, ln.batch_id, "damaged", dmg, "in", value=settle - gv))
        else:
            specs.append(Spec(t.to_location_id, ln.variant_id, ln.batch_id, "damaged", dmg, "in", value=settle))
        ln.qty_received_good += good
        ln.qty_received_damaged += dmg
        ln.value_settled += settle
    if not specs:
        raise BusinessRuleError("Enter the quantities received.")
    txn = ledger.post(db, txn_type="transfer_in", location_id=t.to_location_id, specs=specs, actor=actor,
                      reason=f"Receipt {t.ref}", notes=notes, source_type="transfer", source_id=t.id)
    _settle_status(t)
    _event(t, actor, "received" if t.status == "received" else "partially_received", notes)
    if t.status == "received":
        t.completed_at = datetime.now(timezone.utc)
    db.flush()
    _audit(db, actor, "transfer.received", t, ip, txn=txn.ref)
    return t


def _settle_status(t: Transfer) -> None:
    outstanding = sum(l.qty_dispatched - l.qty_received_good - l.qty_received_damaged - l.qty_short for l in t.lines)
    received = sum(l.qty_received_good + l.qty_received_damaged for l in t.lines)
    if outstanding == 0:
        t.status = "received"
    elif received > 0:
        t.status = "partially_received"


def flag_shortage(db: Session, *, actor: User, transfer_id: int, reason: str, ip: str | None) -> Transfer:
    """Receiver reports that the remaining units will not arrive; routes the transfer to discrepancy review."""
    if not (reason or "").strip():
        raise BusinessRuleError("Describe the discrepancy.")
    t = get_transfer(db, transfer_id, lock=True)
    if t.status not in ("dispatched", "partially_received"):
        raise ConflictError(f"{t.ref} is {t.status}.")
    t.status = "discrepancy_review"
    t.shortage_reason = reason
    t.shortage_closed_by = None
    _event(t, actor, "discrepancy_flagged", reason)
    db.flush()
    _audit(db, actor, "transfer.discrepancy_flagged", t, ip, reason=reason)
    return t


def resolve_discrepancy(db: Session, *, actor: User, transfer_id: int, note: str, ip: str | None) -> Transfer:
    """A different authorised user accepts the shortage: missing units are written off from in-transit."""
    t = get_transfer(db, transfer_id, lock=True)
    if t.status != "discrepancy_review":
        raise ConflictError(f"{t.ref} is {t.status}; there is no discrepancy to resolve.")
    flagged = next((e for e in reversed(t.events) if e.event == "discrepancy_flagged"), None)
    if t.requested_by == actor.id or (flagged and flagged.actor_user_id == actor.id) or t.dispatched_by == actor.id:
        raise SelfApprovalError("A shortage must be approved by someone who was not involved in the transfer.")
    if not (note or "").strip():
        raise BusinessRuleError("A note is required.")
    specs: list[Spec] = []
    for ln in t.lines:
        missing = ln.qty_dispatched - ln.qty_received_good - ln.qty_received_damaged - ln.qty_short
        if missing <= 0:
            continue
        value = ln.dispatch_value - ln.value_settled
        specs.append(Spec(t.to_location_id, ln.variant_id, ln.batch_id, "transit", -missing, "transit", value=value))
        ln.qty_short += missing
        ln.value_settled += value
    if specs:
        ledger.post(db, txn_type="transfer_shortage", location_id=t.to_location_id, specs=specs, actor=actor,
                    reason=f"Approved shortage on {t.ref}", notes=note, source_type="transfer", source_id=t.id)
    t.status = "received"
    t.shortage_closed_by = actor.id
    t.completed_at = datetime.now(timezone.utc)
    _event(t, actor, "discrepancy_approved", note)
    db.flush()
    _audit(db, actor, "transfer.discrepancy_approved", t, ip, reason=note,
           short_units=sum(l.qty_short for l in t.lines))
    return t
