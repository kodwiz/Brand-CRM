"""Stock reservations. Reserving locks the balance rows, so concurrent reservations can never oversell."""
from __future__ import annotations

from datetime import date, datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.models import Batch, Product, ProductVariant, StockBalance, StockReservation, User
from backend.services import ledger
from backend.services.errors import BusinessRuleError, ConflictError, InsufficientStockError, NotFoundError


def available_qty(bal: StockBalance, expiry: date | None, today: date | None = None) -> int:
    """Saleable now: good minus reserved; zero if the batch is past its expiry date (even if not yet marked expired)."""
    if ledger.is_expired(expiry, today):
        return 0
    return max(0, bal.qty_good - bal.qty_reserved)


def reserve(db: Session, *, location_id: int, variant_id: int, qty: int, ref_type: str, ref_id: str,
            batch_id: int | None = None, actor: User | None = None, today: date | None = None,
            top_up: bool = False) -> list[StockReservation]:
    """Reserve `qty` for a document (an order, a transfer). Without a batch, allocates first-expiry-first-out across
    non-expired batches. Raises InsufficientStockError when not enough is available."""
    if qty <= 0:
        raise BusinessRuleError("Reservation quantity must be positive.")
    variant = db.get(ProductVariant, variant_id)
    if variant is None:
        raise NotFoundError("Variant not found")
    if variant.status == "inactive" or variant.product.status == "inactive":
        raise BusinessRuleError(f"{variant.sku} is inactive and cannot be allocated to new orders or transfers.")

    q = (select(StockBalance, Batch.expiry_date).outerjoin(Batch, Batch.id == StockBalance.batch_id)
         .where(StockBalance.location_id == location_id, StockBalance.variant_id == variant_id))
    if batch_id is not None:
        q = q.where(StockBalance.batch_id == batch_id)
    rows = db.execute(q.order_by(Batch.expiry_date.asc().nulls_last(), StockBalance.id)).all()
    candidate_ids = sorted(r[0].id for r in rows)
    if not candidate_ids:
        raise InsufficientStockError("No stock is held for this item at this location.")
    # lock in id order (consistent order => no deadlocks), then re-read the quantities under the lock
    locked = {b.id: b for b in db.scalars(
        select(StockBalance).where(StockBalance.id.in_(candidate_ids)).order_by(StockBalance.id)
        .with_for_update().execution_options(populate_existing=True)).all()}
    expiry_of = {r[0].id: r[1] for r in rows}
    order = [r[0].id for r in rows]  # FEFO order
    total = sum(available_qty(locked[i], expiry_of[i], today) for i in order)
    if total < qty:
        raise InsufficientStockError(f"Only {total} available (requested {qty}). Reserved, damaged, expired and in-transit stock is excluded.")

    out: list[StockReservation] = []
    remaining = qty
    for i in order:
        if remaining == 0:
            break
        bal = locked[i]
        take = min(remaining, available_qty(bal, expiry_of[i], today))
        if take <= 0:
            continue
        dup = db.scalar(select(StockReservation.id).where(
            StockReservation.ref_type == ref_type, StockReservation.ref_id == ref_id,
            StockReservation.location_id == location_id, StockReservation.variant_id == variant_id,
            StockReservation.batch_id.is_(None) if bal.batch_id is None else StockReservation.batch_id == bal.batch_id,
            StockReservation.status == "active"))
        if dup and not top_up:
            raise ConflictError("This item is already reserved for this document.")
        bal.qty_reserved += take
        if dup:  # topping up an existing reservation of the same document and batch
            existing = db.get(StockReservation, dup)
            existing.qty += take
            out.append(existing)
            remaining -= take
            continue
        res = StockReservation(location_id=location_id, variant_id=variant_id, batch_id=bal.batch_id, qty=take,
                               ref_type=ref_type, ref_id=ref_id, created_by=actor.id if actor else None)
        db.add(res)
        out.append(res)
        remaining -= take
    db.flush()
    return out


def _close(db: Session, res: StockReservation, status: str) -> None:
    bal = ledger.lock_balance(db, res.location_id, res.variant_id, res.batch_id)
    bal.qty_reserved -= res.qty
    res.status = status
    res.closed_at = datetime.now(timezone.utc)
    db.flush()


def active_for(db: Session, ref_type: str, ref_id: str) -> list[StockReservation]:
    return list(db.scalars(select(StockReservation).where(
        StockReservation.ref_type == ref_type, StockReservation.ref_id == ref_id, StockReservation.status == "active")
        .order_by(StockReservation.variant_id, StockReservation.batch_id, StockReservation.id).with_for_update()).all())


def release(db: Session, ref_type: str, ref_id: str) -> int:
    """Release every active reservation of a document (cancelled order, rejected transfer). Idempotent."""
    n = 0
    for res in active_for(db, ref_type, ref_id):
        _close(db, res, "released")
        n += 1
    return n


def consume(db: Session, ref_type: str, ref_id: str) -> int:
    """Turn reservations into issued stock: frees the reserved count so the caller can post the outflow."""
    n = 0
    for res in active_for(db, ref_type, ref_id):
        _close(db, res, "consumed")
        n += 1
    return n


def consume_qty(db: Session, ref_type: str, ref_id: str, variant_id: int, qty: int) -> list[tuple[int | None, int]]:
    """Turn `qty` units of ONE item of a document's reservations into issued stock (earliest expiry first) and return
    [(batch_id, qty)]. Used for partial shipments: the rest of the reservation stays active."""
    rows = [r for r in active_for(db, ref_type, ref_id) if r.variant_id == variant_id]
    if sum(r.qty for r in rows) < qty:
        raise BusinessRuleError("Not enough stock is reserved for this item on this order.")

    def expiry(r: StockReservation):
        b = db.get(Batch, r.batch_id) if r.batch_id else None
        exp = b.expiry_date if b is not None else None
        return (exp is None, exp or date.max, r.id)

    out: list[tuple[int | None, int]] = []
    need = qty
    for res in sorted(rows, key=expiry):
        if need == 0:
            break
        take = min(res.qty, need)
        bal = ledger.lock_balance(db, res.location_id, res.variant_id, res.batch_id)
        bal.qty_reserved -= take
        if take == res.qty:
            res.status, res.closed_at = "consumed", datetime.now(timezone.utc)
        else:
            res.qty -= take
            db.add(StockReservation(location_id=res.location_id, variant_id=res.variant_id, batch_id=res.batch_id, qty=take,
                                    status="consumed", ref_type=res.ref_type, ref_id=res.ref_id, created_by=res.created_by,
                                    closed_at=datetime.now(timezone.utc)))
        out.append((res.batch_id, take))
        need -= take
    db.flush()
    return out
