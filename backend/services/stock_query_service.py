"""Read-side stock queries: balances, movements, valuation, alerts, integrity check."""
from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

from sqlalchemy import and_, func, select
from sqlalchemy.orm import Session

from backend.models import (
    Batch, Product, ProductVariant, StockBalance, StockCost, StockMovement, StockTransaction, User,
)
from backend.services import ledger, settings_service


def _scope(q, col, location_ids: set[int] | None, location_id: int | None):
    if location_id is not None:
        q = q.where(col == location_id)
    if location_ids is not None:
        q = q.where(col.in_(location_ids))
    return q


def stock_rows(db: Session, *, location_ids: set[int] | None, location_id: int | None, q: str | None,
               category_id: int | None, only_low: bool, only_issues: bool, include_zero: bool, show_cost: bool,
               page: int, page_size: int) -> tuple[list[dict], int]:
    """One row per (location, variant), summed across batches. Available = good - reserved, excluding expired batches."""
    today = date.today()
    expired_by_date = and_(Batch.expiry_date.is_not(None), Batch.expiry_date < today)
    good = func.sum(StockBalance.qty_good)
    reserved = func.sum(StockBalance.qty_reserved)
    stmt = (select(StockBalance.location_id, StockBalance.variant_id,
                   good.label("good"), reserved.label("reserved"),
                   func.sum(StockBalance.qty_damaged).label("damaged"), func.sum(StockBalance.qty_expired).label("expired"),
                   func.sum(StockBalance.qty_inspection).label("inspection"), func.sum(StockBalance.qty_transit).label("transit"),
                   func.sum(func.greatest(StockBalance.qty_good - StockBalance.qty_reserved, 0)).filter(
                       ~func.coalesce(expired_by_date, False)).label("available"),
                   func.sum(StockBalance.qty_good).filter(func.coalesce(expired_by_date, False)).label("date_expired"))
            .select_from(StockBalance).outerjoin(Batch, Batch.id == StockBalance.batch_id)
            .join(ProductVariant, ProductVariant.id == StockBalance.variant_id)
            .join(Product, Product.id == ProductVariant.product_id)
            .group_by(StockBalance.location_id, StockBalance.variant_id, ProductVariant.reorder_threshold, ProductVariant.sku))
    stmt = _scope(stmt, StockBalance.location_id, location_ids, location_id)
    if q:
        like = f"%{q.strip()}%"
        stmt = stmt.where(ProductVariant.sku.ilike(like) | ProductVariant.name.ilike(like) | Product.name.ilike(like)
                          | ProductVariant.barcode.ilike(like))
    if category_id:
        stmt = stmt.where(Product.category_id == category_id)
    if not include_zero:
        stmt = stmt.having(func.sum(StockBalance.qty_good + StockBalance.qty_damaged + StockBalance.qty_expired
                                    + StockBalance.qty_inspection + StockBalance.qty_transit) > 0)
    if only_low:
        avail_expr = func.coalesce(func.sum(func.greatest(StockBalance.qty_good - StockBalance.qty_reserved, 0)).filter(
            ~func.coalesce(expired_by_date, False)), 0)
        stmt = stmt.having(ProductVariant.reorder_threshold > 0, avail_expr <= ProductVariant.reorder_threshold)
    sub = stmt.subquery()
    total = db.scalar(select(func.count()).select_from(sub)) or 0
    rows = db.execute(stmt.order_by(ProductVariant.sku, StockBalance.location_id)
                      .offset((page - 1) * page_size).limit(page_size)).all()
    variants = {v.id: v for v in db.scalars(select(ProductVariant).where(ProductVariant.id.in_({r.variant_id for r in rows}))).all()} if rows else {}
    costs = {(c.location_id, c.variant_id): c for c in db.scalars(select(StockCost).where(
        StockCost.variant_id.in_({r.variant_id for r in rows}))).all()} if rows and show_cost else {}
    out = []
    for r in rows:
        v = variants[r.variant_id]
        avail = int(r.available or 0)
        low = v.reorder_threshold > 0 and avail <= v.reorder_threshold
        row = {"location_id": r.location_id, "variant_id": v.id, "sku": v.sku, "variant_name": v.name,
               "product_id": v.product_id, "product_name": v.product.name, "shade_hex": v.shade_hex,
               "shade_name": v.shade_name, "on_hand_good": int(r.good or 0), "reserved": int(r.reserved or 0),
               "available": avail, "damaged": int(r.damaged or 0), "expired": int(r.expired or 0) + int(r.date_expired or 0),
               "inspection": int(r.inspection or 0), "in_transit": int(r.transit or 0),
               "reorder_threshold": v.reorder_threshold, "is_low": low}
        if show_cost:
            c = costs.get((r.location_id, v.id))
            row["avg_cost"] = str(ledger.current_avg(c)) if c and ledger.current_avg(c) is not None else None
            row["value"] = str(ledger.q4(c.total_value)) if c else "0.0000"
        if only_low and not low:
            continue
        if only_issues and not (row["damaged"] or row["expired"] or row["inspection"]):
            continue
        out.append(row)
    return out, total


def batch_rows(db: Session, *, location_ids: set[int] | None, location_id: int | None, variant_id: int) -> list[dict]:
    stmt = (select(StockBalance, Batch).outerjoin(Batch, Batch.id == StockBalance.batch_id)
            .where(StockBalance.variant_id == variant_id).order_by(Batch.expiry_date.asc().nulls_last(), StockBalance.id))
    stmt = _scope(stmt, StockBalance.location_id, location_ids, location_id)
    out = []
    today = date.today()
    for bal, batch in db.execute(stmt).all():
        exp = batch.expiry_date if batch else None
        out.append({"location_id": bal.location_id, "batch_id": bal.batch_id, "batch_no": batch.batch_no if batch else None,
                    "expiry_date": exp, "good": bal.qty_good, "reserved": bal.qty_reserved,
                    "available": 0 if ledger.is_expired(exp, today) else max(0, bal.qty_good - bal.qty_reserved),
                    "damaged": bal.qty_damaged, "expired": bal.qty_expired, "inspection": bal.qty_inspection,
                    "in_transit": bal.qty_transit, "date_expired": ledger.is_expired(exp, today)})
    return out


def movements(db: Session, *, location_ids: set[int] | None, location_id: int | None, variant_id: int | None,
              txn_type: str | None, date_from: date | None, date_to: date | None, page: int, page_size: int,
              show_cost: bool) -> tuple[list[dict], int]:
    stmt = (select(StockMovement, StockTransaction).join(StockTransaction, StockTransaction.id == StockMovement.txn_id))
    stmt = _scope(stmt, StockMovement.location_id, location_ids, location_id)
    if variant_id:
        stmt = stmt.where(StockMovement.variant_id == variant_id)
    if txn_type:
        stmt = stmt.where(StockTransaction.txn_type == txn_type)
    if date_from:
        stmt = stmt.where(StockMovement.created_at >= date_from)
    if date_to:
        stmt = stmt.where(StockMovement.created_at < date_to + timedelta(days=1))
    total = db.scalar(select(func.count()).select_from(stmt.subquery())) or 0
    rows = db.execute(stmt.order_by(StockMovement.id.desc()).offset((page - 1) * page_size).limit(page_size)).all()
    vids = {m.variant_id for m, _ in rows}
    vmap = {v.id: v for v in db.scalars(select(ProductVariant).where(ProductVariant.id.in_(vids))).all()} if vids else {}
    bids = {m.batch_id for m, _ in rows if m.batch_id}
    bmap = {b.id: b for b in db.scalars(select(Batch).where(Batch.id.in_(bids))).all()} if bids else {}
    uids = {t.created_by for _, t in rows if t.created_by}
    umap = {u.id: u.username for u in db.scalars(select(User).where(User.id.in_(uids))).all()} if uids else {}
    out = []
    for m, t in rows:
        d = {"id": m.id, "txn_id": t.id, "ref": t.ref, "txn_type": t.txn_type, "txn_status": t.status,
             "location_id": m.location_id, "variant_id": m.variant_id, "sku": vmap[m.variant_id].sku,
             "variant_name": vmap[m.variant_id].name, "batch_no": bmap[m.batch_id].batch_no if m.batch_id in bmap else None,
             "bucket": m.bucket, "qty_delta": m.qty_delta, "reason": t.reason,
             "user": umap.get(t.created_by), "created_at": m.created_at, "reverses_txn_id": t.reverses_txn_id}
        if show_cost:
            d["unit_cost"] = str(m.unit_cost)
            d["value_delta"] = str(m.value_delta)
        out.append(d)
    return out, total


def valuation(db: Session, *, location_ids: set[int] | None, location_id: int | None) -> dict:
    """Owned stock value per location from the costing table, plus in-transit value held by destination."""
    stmt = select(StockCost.location_id, func.sum(StockCost.valued_qty), func.sum(StockCost.total_value)).group_by(StockCost.location_id)
    stmt = _scope(stmt, StockCost.location_id, location_ids, location_id)
    per = {r[0]: {"location_id": r[0], "units": int(r[1] or 0), "value": ledger.q4(r[2] or 0)} for r in db.execute(stmt).all()}
    t = (select(StockMovement.location_id, func.sum(StockMovement.value_delta)).where(StockMovement.bucket == "transit")
         .group_by(StockMovement.location_id))
    t = _scope(t, StockMovement.location_id, location_ids, location_id)
    transit = {r[0]: ledger.q4(r[1] or 0) for r in db.execute(t).all()}
    for loc in set(per) | set(transit):
        per.setdefault(loc, {"location_id": loc, "units": 0, "value": Decimal("0.0000")})
        per[loc]["in_transit_value"] = transit.get(loc, Decimal("0.0000"))
    rows = sorted(per.values(), key=lambda r: r["location_id"])
    return {"locations": [{**r, "value": str(r["value"]), "in_transit_value": str(r["in_transit_value"])} for r in rows],
            "total_value": str(sum((r["value"] for r in rows), Decimal("0"))),
            "total_units": sum(r["units"] for r in rows),
            "total_in_transit_value": str(sum((r["in_transit_value"] for r in rows), Decimal("0")))}


def alerts(db: Session, *, location_ids: set[int] | None, location_id: int | None) -> dict:
    today = date.today()
    warn_days = int(settings_service.get(db, settings_service.EXPIRY_WARN_KEY))
    rows, _ = stock_rows(db, location_ids=location_ids, location_id=location_id, q=None, category_id=None, only_low=True,
                         only_issues=False, include_zero=True, show_cost=False, page=1, page_size=500)
    low = [r for r in rows if r["is_low"]]
    stmt = (select(StockBalance, Batch).join(Batch, Batch.id == StockBalance.batch_id)
            .where(Batch.expiry_date.is_not(None), Batch.expiry_date <= today + timedelta(days=warn_days),
                   StockBalance.qty_good > 0).order_by(Batch.expiry_date))
    stmt = _scope(stmt, StockBalance.location_id, location_ids, location_id)
    exp = []
    for bal, b in db.execute(stmt).all():
        v = db.get(ProductVariant, bal.variant_id)
        exp.append({"location_id": bal.location_id, "variant_id": v.id, "sku": v.sku, "variant_name": v.name,
                    "batch_no": b.batch_no, "expiry_date": b.expiry_date, "qty": bal.qty_good,
                    "days_left": (b.expiry_date - today).days, "already_expired": b.expiry_date < today})
    return {"low_stock": low, "expiring": exp, "expiry_warning_days": warn_days}


def integrity_check(db: Session) -> list[str]:
    """Reconcile balances and costs with the immutable ledger. Returns a list of problems (empty = consistent)."""
    problems: list[str] = []
    attr = {"good": "qty_good", "damaged": "qty_damaged", "expired": "qty_expired", "inspection": "qty_inspection",
            "transit": "qty_transit"}
    sums = {(r[0], r[1], r[2] or 0, r[3]): int(r[4]) for r in db.execute(
        select(StockMovement.location_id, StockMovement.variant_id, StockMovement.batch_id, StockMovement.bucket,
               func.sum(StockMovement.qty_delta)).group_by(StockMovement.location_id, StockMovement.variant_id,
                                                            StockMovement.batch_id, StockMovement.bucket)).all()}
    for b in db.scalars(select(StockBalance)).all():
        for bucket, a in attr.items():
            have = getattr(b, a)
            want = sums.get((b.location_id, b.variant_id, b.batch_id or 0, bucket), 0)
            if have != want:
                problems.append(f"balance loc={b.location_id} var={b.variant_id} batch={b.batch_id} {bucket}: "
                                f"table={have} ledger={want}")
    for c in db.scalars(select(StockCost)).all():
        vq = db.scalar(select(func.coalesce(func.sum(StockMovement.qty_delta), 0)).where(
            StockMovement.location_id == c.location_id, StockMovement.variant_id == c.variant_id,
            StockMovement.bucket.in_(("good", "damaged", "expired", "inspection"))))
        vv = db.scalar(select(func.coalesce(func.sum(StockMovement.value_delta), 0)).where(
            StockMovement.location_id == c.location_id, StockMovement.variant_id == c.variant_id,
            StockMovement.bucket.in_(("good", "damaged", "expired", "inspection"))))
        if int(vq) != c.valued_qty:
            problems.append(f"cost loc={c.location_id} var={c.variant_id}: valued_qty={c.valued_qty} ledger={vq}")
        if Decimal(vv) != Decimal(c.total_value):
            problems.append(f"cost loc={c.location_id} var={c.variant_id}: value={c.total_value} ledger={vv}")
    return problems
