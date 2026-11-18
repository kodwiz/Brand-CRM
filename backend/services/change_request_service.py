"""Controlled stock changes: adjustments, write-offs and count variances.

Nothing touches stock until a *different* authorised user approves it (or the value is below the configured
auto-approve limit, which defaults to zero = never). Posted changes are never edited; they are reversed.
"""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.models import StockChangeLine, StockChangeRequest, StockCost, User
from backend.services import audit_service, inventory_service, ledger, settings_service
from backend.services.errors import BusinessRuleError, ConflictError, NotFoundError, SelfApprovalError
from backend.services.ledger import Spec

KINDS = ("adjustment", "writeoff", "count_variance")
PHYSICAL = ("good", "damaged", "expired", "inspection")


def _estimate(db: Session, location_id: int, lines: list[StockChangeLine]) -> Decimal:
    """Absolute estimated value of the change at current average cost (information for approvers)."""
    total = Decimal("0")
    for ln in lines:
        cost = db.scalar(
            select(StockCost).where(StockCost.location_id == location_id, StockCost.variant_id == ln.variant_id))
        avg = ledger.current_avg(cost)
        unit = ln.unit_cost if ln.unit_cost is not None else avg
        if unit is None:
            continue
        total += abs(ln.qty_delta) * Decimal(unit)
    return ledger.q4(total)


def create_request(db: Session, *, actor: User, kind: str, location_id: int, lines: list[dict], reason: str,
                   ip: str | None, count_id: int | None = None) -> StockChangeRequest:
    if kind not in KINDS:
        raise BusinessRuleError("Unknown request type.")
    if not (reason or "").strip():
        raise BusinessRuleError("A reason is required.")
    if not lines:
        raise BusinessRuleError("Add at least one line.")
    req = StockChangeRequest(
        ref=ledger.next_ref(db, "stock_change_ref_seq", "REQ"), kind=kind, location_id=location_id,
        reason=reason.strip(), requested_by=actor.id, count_id=count_id)
    for i, ln in enumerate(lines, 1):
        v = inventory_service.get_variant(db, ln["variant_id"])
        qty = int(ln["qty_delta"])
        bucket = ln.get("bucket", "good")
        if qty == 0:
            raise BusinessRuleError(f"Line {i} ({v.sku}): the change cannot be zero.")
        if bucket not in PHYSICAL:
            raise BusinessRuleError(f"Line {i} ({v.sku}): unknown stock bucket '{bucket}'.")
        if kind == "writeoff" and qty > 0:
            raise BusinessRuleError(f"Line {i} ({v.sku}): a write-off must remove stock.")
        if v.product.tracks_batches and ln.get("batch_id") is None:
            raise BusinessRuleError(f"Line {i} ({v.sku}): choose the batch.")
        if qty > 0 and ln.get("unit_cost") is not None and Decimal(str(ln["unit_cost"])) < 0:
            raise BusinessRuleError(f"Line {i} ({v.sku}): cost cannot be negative.")
        req.lines.append(StockChangeLine(
            variant_id=v.id, batch_id=ln.get("batch_id"), bucket=bucket, qty_delta=qty,
            unit_cost=Decimal(str(ln["unit_cost"])) if ln.get("unit_cost") is not None else None))
    req.estimated_value = _estimate(db, location_id, req.lines)
    db.add(req)
    db.flush()
    audit_service.record(db, actor=actor, action=f"stock_request.created", entity_type="stock_change_request",
                         entity_id=req.id, location_id=location_id, reason=reason,
                         after={"ref": req.ref, "kind": kind, "lines": len(req.lines),
                                "estimated_value": str(req.estimated_value)}, ip=ip)
    limit = Decimal(str(settings_service.get(db, settings_service.AUTO_APPROVE_KEY)))
    if kind == "adjustment" and limit > 0 and req.estimated_value <= limit:
        _apply(db, req, actor=actor, auto=True, note="Auto-approved: below the configured limit", ip=ip)
    return req


def get_request(db: Session, request_id: int, *, lock: bool = False) -> StockChangeRequest:
    q = select(StockChangeRequest).where(StockChangeRequest.id == request_id)
    if lock:
        q = q.with_for_update().execution_options(populate_existing=True)
    r = db.scalar(q)
    if r is None:
        raise NotFoundError("Request not found")
    return r


def _apply(db: Session, req: StockChangeRequest, *, actor: User, auto: bool, note: str | None, ip: str | None) -> None:
    specs: list[Spec] = []
    for ln in req.lines:
        n = abs(ln.qty_delta)
        if ln.qty_delta < 0:
            specs.append(Spec(req.location_id, ln.variant_id, ln.batch_id, ln.bucket, -n, "out"))
        else:
            unit = ln.unit_cost
            if unit is None:
                cost = db.scalar(select(StockCost).where(StockCost.location_id == req.location_id,
                                                        StockCost.variant_id == ln.variant_id))
                unit = ledger.current_avg(cost)
            if unit is None:
                v = inventory_service.get_variant(db, ln.variant_id)
                raise BusinessRuleError(
                    f"{v.sku}: no cost is known for this item at this location. Enter a unit cost for the gain.")
            specs.append(Spec(req.location_id, ln.variant_id, ln.batch_id, ln.bucket, n, "in", unit_cost=Decimal(unit)))
    txn_type = {"adjustment": "adjustment", "writeoff": "writeoff", "count_variance": "count_variance"}[req.kind]
    txn = ledger.post(db, txn_type=txn_type, location_id=req.location_id, specs=specs, actor=actor,
                      reason=req.reason, notes=note, source_type="change_request", source_id=req.id)
    req.status = "approved"
    req.decided_by = actor.id
    req.decided_at = datetime.now(timezone.utc)
    req.decision_note = note
    req.txn_id = txn.id
    db.flush()
    audit_service.record(db, actor=actor, action="stock_request.auto_approved" if auto else "stock_request.approved",
                         entity_type="stock_change_request", entity_id=req.id, location_id=req.location_id,
                         reason=note, after={"ref": req.ref, "txn": txn.ref, "kind": req.kind}, ip=ip)


def approve(db: Session, *, actor: User, request_id: int, note: str | None, ip: str | None) -> StockChangeRequest:
    req = get_request(db, request_id, lock=True)
    if req.status != "pending":
        raise ConflictError(f"{req.ref} is already {req.status}.")
    if req.requested_by == actor.id:
        raise SelfApprovalError("You cannot approve a stock change you requested yourself.")
    _apply(db, req, actor=actor, auto=False, note=note, ip=ip)
    return req


def reject(db: Session, *, actor: User, request_id: int, note: str, ip: str | None) -> StockChangeRequest:
    if not (note or "").strip():
        raise BusinessRuleError("Say why the request is rejected.")
    req = get_request(db, request_id, lock=True)
    if req.status != "pending":
        raise ConflictError(f"{req.ref} is already {req.status}.")
    if req.requested_by == actor.id:
        raise SelfApprovalError("Use cancel to withdraw your own request.")
    req.status = "rejected"
    req.decided_by = actor.id
    req.decided_at = datetime.now(timezone.utc)
    req.decision_note = note
    db.flush()
    audit_service.record(db, actor=actor, action="stock_request.rejected", entity_type="stock_change_request",
                         entity_id=req.id, location_id=req.location_id, reason=note, after={"ref": req.ref}, ip=ip)
    return req


def cancel(db: Session, *, actor: User, request_id: int, ip: str | None) -> StockChangeRequest:
    req = get_request(db, request_id, lock=True)
    if req.status != "pending":
        raise ConflictError(f"{req.ref} is already {req.status}.")
    if req.requested_by != actor.id:
        raise BusinessRuleError("Only the person who made the request can cancel it.")
    req.status = "cancelled"
    db.flush()
    audit_service.record(db, actor=actor, action="stock_request.cancelled", entity_type="stock_change_request",
                         entity_id=req.id, location_id=req.location_id, after={"ref": req.ref}, ip=ip)
    return req
