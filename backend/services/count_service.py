"""Physical stock counts. A count snapshots expected good-bucket quantities; submitting it turns the differences
into a `count_variance` change request that a different user must approve. Movements posted after the snapshot
(sales, receipts) are accounted for so a busy counter does not create false variances."""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from backend.models import StockBalance, StockCount, StockCountLine, StockMovement, User
from backend.services import audit_service, change_request_service, ledger
from backend.services.errors import BusinessRuleError, ConflictError, NotFoundError


def get_count(db: Session, count_id: int, *, lock: bool = False) -> StockCount:
    q = select(StockCount).where(StockCount.id == count_id)
    if lock:
        q = q.with_for_update().execution_options(populate_existing=True)
    c = db.scalar(q)
    if c is None:
        raise NotFoundError("Count not found")
    return c


def create(db: Session, *, actor: User, location_id: int, variant_ids: list[int] | None, notes: str | None,
           ip: str | None) -> StockCount:
    open_count = db.scalar(select(StockCount.id).where(StockCount.location_id == location_id,
                                                      StockCount.status == "in_progress").limit(1))
    if open_count:
        raise ConflictError("A count is already in progress at this location. Submit or cancel it first.")
    q = select(StockBalance).where(StockBalance.location_id == location_id, StockBalance.qty_good > 0)
    if variant_ids:
        q = q.where(StockBalance.variant_id.in_(variant_ids))
    balances = db.scalars(q.order_by(StockBalance.variant_id, StockBalance.batch_id)).all()
    if not balances:
        raise BusinessRuleError("There is no stock to count for this selection.")
    last = db.scalar(select(func.coalesce(func.max(StockMovement.id), 0)))
    c = StockCount(ref=ledger.next_ref(db, "stock_count_ref_seq", "CNT"), location_id=location_id, notes=notes,
                   snapshot_movement_id=last, created_by=actor.id)
    for b in balances:
        c.lines.append(StockCountLine(variant_id=b.variant_id, batch_id=b.batch_id, expected_qty=b.qty_good))
    db.add(c)
    db.flush()
    audit_service.record(db, actor=actor, action="count.created", entity_type="stock_count", entity_id=c.id,
                         location_id=location_id, after={"ref": c.ref, "lines": len(c.lines)}, ip=ip)
    return c


def enter_counts(db: Session, *, actor: User, count_id: int, entries: list[dict]) -> StockCount:
    c = get_count(db, count_id, lock=True)
    if c.status != "in_progress":
        raise ConflictError(f"{c.ref} is {c.status}.")
    by_id = {l.id: l for l in c.lines}
    for e in entries:
        ln = by_id.get(e["line_id"])
        if ln is None:
            raise BusinessRuleError("A line does not belong to this count.")
        q = e.get("counted_qty")
        if q is not None and int(q) < 0:
            raise BusinessRuleError("Counted quantity cannot be negative.")
        ln.counted_qty = int(q) if q is not None else None
        if "reason" in e:
            ln.reason = e["reason"]
    db.flush()
    return c


def _moved_since(db: Session, c: StockCount, ln: StockCountLine) -> int:
    q = select(func.coalesce(func.sum(StockMovement.qty_delta), 0)).where(
        StockMovement.id > c.snapshot_movement_id, StockMovement.location_id == c.location_id,
        StockMovement.variant_id == ln.variant_id, StockMovement.bucket == "good")
    q = q.where(StockMovement.batch_id.is_(None)) if ln.batch_id is None else q.where(StockMovement.batch_id == ln.batch_id)
    return int(db.scalar(q) or 0)


def variances(db: Session, c: StockCount) -> list[dict]:
    out = []
    for ln in c.lines:
        if ln.counted_qty is None:
            continue
        moved = _moved_since(db, c, ln)
        diff = ln.counted_qty - (ln.expected_qty + moved)
        out.append({"line": ln, "moved_since": moved, "variance": diff})
    return out


def submit(db: Session, *, actor: User, count_id: int, ip: str | None):
    c = get_count(db, count_id, lock=True)
    if c.status != "in_progress":
        raise ConflictError(f"{c.ref} is {c.status}.")
    if any(l.counted_qty is None for l in c.lines):
        raise BusinessRuleError("Every line must be counted (enter 0 if none is found) before submitting.")
    diffs = [v for v in variances(db, c) if v["variance"] != 0]
    for v in diffs:
        if not (v["line"].reason or "").strip():
            raise BusinessRuleError("Give a reason for every line with a difference.")
    req = None
    if diffs:
        req = change_request_service.create_request(
            db, actor=actor, kind="count_variance", location_id=c.location_id, count_id=c.id,
            reason=f"Stock count {c.ref}",
            lines=[{"variant_id": v["line"].variant_id, "batch_id": v["line"].batch_id, "bucket": "good",
                    "qty_delta": v["variance"]} for v in diffs], ip=ip)
    c.status = "submitted"
    c.submitted_at = datetime.now(timezone.utc)
    db.flush()
    audit_service.record(db, actor=actor, action="count.submitted", entity_type="stock_count", entity_id=c.id,
                         location_id=c.location_id,
                         after={"ref": c.ref, "variance_lines": len(diffs), "request": req.ref if req else None}, ip=ip)
    return c, req


def cancel(db: Session, *, actor: User, count_id: int, ip: str | None) -> StockCount:
    c = get_count(db, count_id, lock=True)
    if c.status != "in_progress":
        raise ConflictError(f"{c.ref} is {c.status}.")
    c.status = "cancelled"
    db.flush()
    audit_service.record(db, actor=actor, action="count.cancelled", entity_type="stock_count", entity_id=c.id,
                         location_id=c.location_id, after={"ref": c.ref}, ip=ip)
    return c
