"""Inventory endpoints: stock, movements, receiving, opening stock, damage/expiry, change requests, reversal,
transfers, counts, alerts, valuation and inventory settings. Every route checks permission AND location."""
from __future__ import annotations

from datetime import date

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.api.common import commit_or_http
from backend.db import get_db
from backend.deps import Principal, client_ip, ensure_location_access, require, require_any
from backend.models import (
    Batch, Location, ProductVariant, StockChangeRequest, StockCount, StockReceipt, StockTransaction, Transfer, User,
)
from backend.schemas import inventory as S
from backend.services import (
    change_request_service as crs, count_service, inventory_service, settings_service, stock_query_service as sq,
    transfer_service as ts,
)
from backend.services.errors import NotFoundError
from backend.services.scope import visible_location_ids

router = APIRouter(tags=["inventory"])


def _scope(p: Principal, location_id: int | None) -> tuple[set[int] | None, int | None]:
    if location_id is not None:
        ensure_location_access(p, location_id)
    return visible_location_ids(p.user), location_id


def _nf(fn):
    try:
        return fn()
    except NotFoundError as e:
        raise HTTPException(404, str(e))


def _names(db: Session, ids: set[int]) -> dict[int, str]:
    ids = {i for i in ids if i}
    if not ids:
        return {}
    return {u.id: u.username for u in db.scalars(select(User).where(User.id.in_(ids))).all()}


def _lines_out(db: Session, lines, fields: list[str]) -> list[dict]:
    vids = {l.variant_id for l in lines}
    vm = {v.id: v for v in db.scalars(select(ProductVariant).where(ProductVariant.id.in_(vids))).all()} if vids else {}
    bids = {getattr(l, "batch_id", None) for l in lines} - {None}
    bm = {b.id: b for b in db.scalars(select(Batch).where(Batch.id.in_(bids))).all()} if bids else {}
    out = []
    for l in lines:
        d = {"id": l.id, "variant_id": l.variant_id, "sku": vm[l.variant_id].sku, "variant_name": vm[l.variant_id].name}
        bid = getattr(l, "batch_id", None)
        if bid:
            d["batch_id"] = bid
            d["batch_no"] = bm[bid].batch_no
            d["expiry_date"] = bm[bid].expiry_date
        for f in fields:
            val = getattr(l, f)
            d[f] = str(val) if hasattr(val, "quantize") else val
        out.append(d)
    return out


# ---- stock views -------------------------------------------------------------------------------------------
@router.get("/inventory/stock")
def stock(location_id: int | None = None, q: str | None = None, category_id: int | None = None, low_only: bool = False,
          issues_only: bool = False, include_zero: bool = False, page: int = Query(1, ge=1),
          page_size: int = Query(50, ge=1, le=200), p: Principal = Depends(require("inventory.view")),
          db: Session = Depends(get_db)):
    ids, loc = _scope(p, location_id)
    items, total = sq.stock_rows(db, location_ids=ids, location_id=loc, q=q, category_id=category_id, only_low=low_only,
                                 only_issues=issues_only, include_zero=include_zero,
                                 show_cost=p.has("inventory.view_valuation"), page=page, page_size=page_size)
    return {"items": items, "total": total, "page": page, "page_size": page_size}


@router.get("/inventory/stock/{variant_id}/batches")
def stock_batches(variant_id: int, location_id: int | None = None, p: Principal = Depends(require("inventory.view")),
                  db: Session = Depends(get_db)):
    ids, loc = _scope(p, location_id)
    return sq.batch_rows(db, location_ids=ids, location_id=loc, variant_id=variant_id)


@router.get("/inventory/batches")
def list_batches(variant_id: int, p: Principal = Depends(require_any("inventory.view", "transfers.view")),
                 db: Session = Depends(get_db)):
    rows = db.scalars(select(Batch).where(Batch.variant_id == variant_id).order_by(Batch.expiry_date.nulls_last())).all()
    return [{"id": b.id, "batch_no": b.batch_no, "expiry_date": b.expiry_date} for b in rows]


@router.get("/inventory/movements")
def movements(location_id: int | None = None, variant_id: int | None = None, txn_type: str | None = None,
              date_from: date | None = None, date_to: date | None = None, page: int = Query(1, ge=1),
              page_size: int = Query(50, ge=1, le=200), p: Principal = Depends(require("inventory.view")),
              db: Session = Depends(get_db)):
    ids, loc = _scope(p, location_id)
    items, total = sq.movements(db, location_ids=ids, location_id=loc, variant_id=variant_id, txn_type=txn_type,
                                date_from=date_from, date_to=date_to, page=page, page_size=page_size,
                                show_cost=p.has("inventory.view_valuation"))
    return {"items": items, "total": total, "page": page, "page_size": page_size}


@router.get("/inventory/valuation")
def valuation(location_id: int | None = None, p: Principal = Depends(require("inventory.view_valuation")),
              db: Session = Depends(get_db)):
    ids, loc = _scope(p, location_id)
    return sq.valuation(db, location_ids=ids, location_id=loc)


@router.get("/inventory/alerts")
def alerts(location_id: int | None = None, p: Principal = Depends(require("inventory.view")), db: Session = Depends(get_db)):
    ids, loc = _scope(p, location_id)
    return sq.alerts(db, location_ids=ids, location_id=loc)


@router.get("/inventory/integrity")
def integrity(p: Principal = Depends(require("settings.manage")), db: Session = Depends(get_db)):
    problems = sq.integrity_check(db)
    return {"consistent": not problems, "problems": problems[:200]}


# ---- receiving ---------------------------------------------------------------------------------------------
def _receipt_out(db: Session, r: StockReceipt, show_cost: bool) -> dict:
    fields = ["batch_no", "expiry_date", "qty_expected", "qty_received", "qty_accepted", "qty_damaged", "qty_rejected", "notes"]
    lines = []
    vm = {v.id: v for v in db.scalars(select(ProductVariant).where(ProductVariant.id.in_([l.variant_id for l in r.lines]))).all()}
    for l in r.lines:
        d = {"id": l.id, "variant_id": l.variant_id, "sku": vm[l.variant_id].sku, "variant_name": vm[l.variant_id].name}
        for f in fields:
            d[f] = getattr(l, f)
        if show_cost:
            d["unit_cost"] = str(l.unit_cost)
            d["landed_alloc"] = str(l.landed_alloc)
        d["po_line_id"] = l.po_line_id
        lines.append(d)
    names = _names(db, {r.created_by, r.posted_by})
    extra = {"supplier_id": r.supplier_id, "purchase_order_id": r.purchase_order_id,
             "additional_costs": ([{"description": c.description, "amount": str(c.amount)} for c in r.costs] if show_cost else [])}
    return extra | {"id": r.id, "ref": r.ref, "location_id": r.location_id, "purchase_reference": r.purchase_reference,
            "supplier_reference": r.supplier_reference, "received_date": r.received_date, "status": r.status,
            "notes": r.notes, "created_by": names.get(r.created_by), "posted_by": names.get(r.posted_by),
            "posted_at": r.posted_at, "lines": lines,
            "totals": {"received": sum(l.qty_received for l in r.lines), "accepted": sum(l.qty_accepted for l in r.lines),
                       "damaged": sum(l.qty_damaged for l in r.lines), "rejected": sum(l.qty_rejected for l in r.lines)}}


@router.get("/inventory/receipts")
def list_receipts(location_id: int | None = None, status: str | None = None, page: int = Query(1, ge=1),
                  page_size: int = Query(50, ge=1, le=200), p: Principal = Depends(require("inventory.view")),
                  db: Session = Depends(get_db)):
    ids, loc = _scope(p, location_id)
    q = select(StockReceipt)
    if loc:
        q = q.where(StockReceipt.location_id == loc)
    if ids is not None:
        q = q.where(StockReceipt.location_id.in_(ids))
    if status:
        q = q.where(StockReceipt.status == status)
    from sqlalchemy import func
    total = db.scalar(select(func.count()).select_from(q.subquery())) or 0
    rows = db.scalars(q.order_by(StockReceipt.id.desc()).offset((page - 1) * page_size).limit(page_size)).all()
    sv = p.has("inventory.view_valuation")
    return {"items": [_receipt_out(db, r, sv) for r in rows], "total": total, "page": page, "page_size": page_size}


@router.get("/inventory/receipts/{receipt_id}")
def get_receipt(receipt_id: int, p: Principal = Depends(require("inventory.view")), db: Session = Depends(get_db)):
    r = _nf(lambda: inventory_service.get_receipt(db, receipt_id))
    ensure_location_access(p, r.location_id)
    return _receipt_out(db, r, p.has("inventory.view_valuation"))


@router.post("/inventory/receipts", status_code=201)
def create_receipt(body: S.ReceiptIn, request: Request, p: Principal = Depends(require("inventory.receive")),
                   db: Session = Depends(get_db)):
    ensure_location_access(p, body.location_id)

    def run():
        r = inventory_service.create_receipt(
            db, actor=p.user, location_id=body.location_id, received_date=body.received_date,
            lines=[l.model_dump() for l in body.lines], purchase_reference=body.purchase_reference,
            supplier_reference=body.supplier_reference, notes=body.notes, ip=client_ip(request),
            purchase_order_id=body.purchase_order_id, supplier_id=body.supplier_id,
            additional_costs=[c.model_dump() for c in body.additional_costs])
        if body.post_now:
            inventory_service.post_receipt(db, actor=p.user, receipt_id=r.id, ip=client_ip(request))
        return r

    r = commit_or_http(db, run)
    return _receipt_out(db, r, p.has("inventory.view_valuation"))


@router.post("/inventory/receipts/{receipt_id}/post")
def post_receipt(receipt_id: int, request: Request, p: Principal = Depends(require("inventory.receive")),
                 db: Session = Depends(get_db)):
    r0 = _nf(lambda: inventory_service.get_receipt(db, receipt_id))
    ensure_location_access(p, r0.location_id)
    r = commit_or_http(db, lambda: inventory_service.post_receipt(db, actor=p.user, receipt_id=receipt_id, ip=client_ip(request)))
    return _receipt_out(db, r, p.has("inventory.view_valuation"))


@router.post("/inventory/receipts/{receipt_id}/cancel")
def cancel_receipt(receipt_id: int, body: S.ReasonIn, request: Request, p: Principal = Depends(require("inventory.receive")),
                   db: Session = Depends(get_db)):
    r0 = _nf(lambda: inventory_service.get_receipt(db, receipt_id))
    ensure_location_access(p, r0.location_id)
    r = commit_or_http(db, lambda: inventory_service.cancel_receipt(db, actor=p.user, receipt_id=receipt_id,
                                                                    reason=body.reason, ip=client_ip(request)))
    return _receipt_out(db, r, p.has("inventory.view_valuation"))


# ---- opening stock, bucket moves, reversal ---------------------------------------------------------------------
@router.post("/inventory/opening-stock")
def opening_stock(body: S.OpeningIn, request: Request, p: Principal = Depends(require("inventory.opening_stock")),
                  db: Session = Depends(get_db)):
    for loc in {l.location_id for l in body.lines}:
        ensure_location_access(p, loc)
    return commit_or_http(db, lambda: inventory_service.opening_stock(
        db, actor=p.user, lines=[l.model_dump() for l in body.lines], dry_run=body.dry_run,
        idempotency_key=body.idempotency_key, ip=client_ip(request)))


def _txn_out(t: StockTransaction) -> dict:
    return {"id": t.id, "ref": t.ref, "txn_type": t.txn_type, "status": t.status, "location_id": t.location_id,
            "accounting_status": t.accounting_status}


@router.post("/inventory/moves", status_code=201)
def move(body: S.MoveIn, request: Request, p: Principal = Depends(require("inventory.adjust")),
         db: Session = Depends(get_db)):
    """Damage, expiry, or an inspection disposition. Moves stock between buckets at the current average cost."""
    ensure_location_access(p, body.location_id)
    t = commit_or_http(db, lambda: inventory_service.move_bucket(
        db, actor=p.user, action=body.action, location_id=body.location_id, variant_id=body.variant_id,
        batch_id=body.batch_id, qty=body.qty, reason=body.reason, notes=body.notes,
        idempotency_key=body.idempotency_key, ip=client_ip(request)))
    return _txn_out(t)


@router.post("/inventory/transactions/{txn_id}/reverse")
def reverse(txn_id: int, body: S.ReasonIn, request: Request, p: Principal = Depends(require("inventory.reverse")),
            db: Session = Depends(get_db)):
    t = db.get(StockTransaction, txn_id)
    if t is None:
        raise HTTPException(404, "Transaction not found")
    ensure_location_access(p, t.location_id)
    return _txn_out(commit_or_http(db, lambda: inventory_service.reverse_transaction(
        db, actor=p.user, txn_id=txn_id, reason=body.reason, ip=client_ip(request))))


# ---- change requests -----------------------------------------------------------------------------------------
def _req_out(db: Session, r: StockChangeRequest, show_cost: bool) -> dict:
    names = _names(db, {r.requested_by, r.decided_by})
    d = {"id": r.id, "ref": r.ref, "kind": r.kind, "location_id": r.location_id, "status": r.status, "reason": r.reason,
         "requested_by": names.get(r.requested_by), "requested_by_id": r.requested_by, "requested_at": r.requested_at,
         "decided_by": names.get(r.decided_by), "decided_at": r.decided_at, "decision_note": r.decision_note,
         "txn_id": r.txn_id, "count_id": r.count_id, "lines": _lines_out(db, r.lines, ["bucket", "qty_delta"])}
    if show_cost:
        d["estimated_value"] = str(r.estimated_value) if r.estimated_value is not None else None
    return d


def _approve_perm(p: Principal, kind: str) -> None:
    need = "inventory.writeoff_approve" if kind == "writeoff" else "inventory.adjust_approve"
    if not p.has(need):
        raise HTTPException(403, "You do not have permission to perform this action")


@router.get("/inventory/requests")
def list_requests(location_id: int | None = None, status: str | None = None, page: int = Query(1, ge=1),
                  page_size: int = Query(50, ge=1, le=200), p: Principal = Depends(require("inventory.view")),
                  db: Session = Depends(get_db)):
    ids, loc = _scope(p, location_id)
    q = select(StockChangeRequest)
    if loc:
        q = q.where(StockChangeRequest.location_id == loc)
    if ids is not None:
        q = q.where(StockChangeRequest.location_id.in_(ids))
    if status:
        q = q.where(StockChangeRequest.status == status)
    from sqlalchemy import func
    total = db.scalar(select(func.count()).select_from(q.subquery())) or 0
    rows = db.scalars(q.order_by(StockChangeRequest.id.desc()).offset((page - 1) * page_size).limit(page_size)).all()
    sv = p.has("inventory.view_valuation")
    return {"items": [_req_out(db, r, sv) for r in rows], "total": total, "page": page, "page_size": page_size}


@router.post("/inventory/requests", status_code=201)
def create_request(body: S.ChangeRequestIn, request: Request, p: Principal = Depends(require("inventory.adjust")),
                   db: Session = Depends(get_db)):
    ensure_location_access(p, body.location_id)
    if body.kind not in ("adjustment", "writeoff"):
        raise HTTPException(422, "Kind must be adjustment or writeoff.")
    r = commit_or_http(db, lambda: crs.create_request(
        db, actor=p.user, kind=body.kind, location_id=body.location_id, reason=body.reason,
        lines=[l.model_dump() for l in body.lines], ip=client_ip(request)))
    return _req_out(db, r, p.has("inventory.view_valuation"))


def _load_request(db, p, request_id) -> StockChangeRequest:
    r = _nf(lambda: crs.get_request(db, request_id))
    ensure_location_access(p, r.location_id)
    return r


@router.post("/inventory/requests/{request_id}/approve")
def approve_request(request_id: int, body: S.NoteIn, request: Request, p: Principal = Depends(require("inventory.view")),
                    db: Session = Depends(get_db)):
    r0 = _load_request(db, p, request_id)
    _approve_perm(p, r0.kind)
    r = commit_or_http(db, lambda: crs.approve(db, actor=p.user, request_id=request_id, note=body.note, ip=client_ip(request)))
    return _req_out(db, r, p.has("inventory.view_valuation"))


@router.post("/inventory/requests/{request_id}/reject")
def reject_request(request_id: int, body: S.RequiredNoteIn, request: Request, p: Principal = Depends(require("inventory.view")),
                   db: Session = Depends(get_db)):
    r0 = _load_request(db, p, request_id)
    _approve_perm(p, r0.kind)
    r = commit_or_http(db, lambda: crs.reject(db, actor=p.user, request_id=request_id, note=body.note, ip=client_ip(request)))
    return _req_out(db, r, p.has("inventory.view_valuation"))


@router.post("/inventory/requests/{request_id}/cancel")
def cancel_request(request_id: int, request: Request, p: Principal = Depends(require("inventory.adjust")),
                   db: Session = Depends(get_db)):
    _load_request(db, p, request_id)
    r = commit_or_http(db, lambda: crs.cancel(db, actor=p.user, request_id=request_id, ip=client_ip(request)))
    return _req_out(db, r, p.has("inventory.view_valuation"))


# ---- transfers ----------------------------------------------------------------------------------------------
def _transfer_out(db: Session, t: Transfer, show_cost: bool) -> dict:
    names = _names(db, {t.requested_by, t.approved_by, t.dispatched_by, t.shortage_closed_by})
    fields = ["qty_requested", "qty_dispatched", "qty_received_good", "qty_received_damaged", "qty_short"]
    lines = _lines_out(db, t.lines, fields)
    if show_cost:
        for d, l in zip(lines, t.lines):
            d["dispatch_value"] = str(l.dispatch_value)
    return {"id": t.id, "ref": t.ref, "from_location_id": t.from_location_id, "to_location_id": t.to_location_id,
            "status": t.status, "notes": t.notes, "requested_by": names.get(t.requested_by),
            "requested_by_id": t.requested_by, "requested_at": t.requested_at, "approved_by": names.get(t.approved_by),
            "approved_at": t.approved_at, "dispatched_by": names.get(t.dispatched_by), "dispatched_at": t.dispatched_at,
            "completed_at": t.completed_at, "decision_reason": t.decision_reason, "shortage_reason": t.shortage_reason,
            "lines": lines,
            "events": [{"event": e.event, "by": e.actor_username, "note": e.note, "at": e.created_at} for e in t.events]}


def _transfer_access(p: Principal, t: Transfer, *, side: str) -> None:
    """side: 'from', 'to' or 'either' -- which end the caller must be assigned to for this action."""
    from backend.services.scope import can_access_location
    f, to = can_access_location(p.user, t.from_location_id), can_access_location(p.user, t.to_location_id)
    ok = {"from": f, "to": to, "either": f or to}[side]
    if not ok:
        raise HTTPException(404, "Not found")


def _load_transfer(db, transfer_id) -> Transfer:
    return _nf(lambda: ts.get_transfer(db, transfer_id))


@router.get("/transfers/locations")
def transfer_locations(p: Principal = Depends(require("transfers.view")), db: Session = Depends(get_db)):
    """Names of every active location, so a transfer can name a destination the user is not assigned to.
    Exposes id, code, name and city only; nothing else about those locations."""
    rows = db.scalars(select(Location).where(Location.is_active.is_(True)).order_by(Location.name)).all()
    return [{"id": l.id, "code": l.code, "name": l.name, "city": l.city} for l in rows]


@router.get("/transfers")
def list_transfers(status: str | None = None, location_id: int | None = None, page: int = Query(1, ge=1),
                   page_size: int = Query(50, ge=1, le=200), p: Principal = Depends(require("transfers.view")),
                   db: Session = Depends(get_db)):
    ids, loc = _scope(p, location_id)
    q = select(Transfer)
    if loc:
        q = q.where((Transfer.from_location_id == loc) | (Transfer.to_location_id == loc))
    if ids is not None:
        q = q.where(Transfer.from_location_id.in_(ids) | Transfer.to_location_id.in_(ids))
    if status:
        q = q.where(Transfer.status == status)
    from sqlalchemy import func
    total = db.scalar(select(func.count()).select_from(q.subquery())) or 0
    rows = db.scalars(q.order_by(Transfer.id.desc()).offset((page - 1) * page_size).limit(page_size)).all()
    sv = p.has("inventory.view_valuation")
    return {"items": [_transfer_out(db, t, sv) for t in rows], "total": total, "page": page, "page_size": page_size}


@router.get("/transfers/{transfer_id}")
def get_transfer(transfer_id: int, p: Principal = Depends(require("transfers.view")), db: Session = Depends(get_db)):
    t = _load_transfer(db, transfer_id)
    _transfer_access(p, t, side="either")
    return _transfer_out(db, t, p.has("inventory.view_valuation"))


@router.post("/transfers", status_code=201)
def create_transfer(body: S.TransferIn, request: Request, p: Principal = Depends(require("transfers.request")),
                    db: Session = Depends(get_db)):
    # the requester must be assigned to the location that needs the stock or the one sending it
    from backend.services.scope import can_access_location
    if not (can_access_location(p.user, body.from_location_id) or can_access_location(p.user, body.to_location_id)):
        raise HTTPException(404, "Not found")
    for lid in (body.from_location_id, body.to_location_id):
        if db.get(Location, lid) is None or not db.get(Location, lid).is_active:
            raise HTTPException(422, "Choose active locations.")
    t = commit_or_http(db, lambda: ts.create(db, actor=p.user, from_location_id=body.from_location_id,
                                             to_location_id=body.to_location_id, lines=[l.model_dump() for l in body.lines],
                                             notes=body.notes, ip=client_ip(request)))
    return _transfer_out(db, t, p.has("inventory.view_valuation"))




@router.post("/transfers/{transfer_id}/approve")
def approve_transfer(transfer_id: int, body: S.NoteIn, request: Request, p: Principal = Depends(require("transfers.approve")),
                     db: Session = Depends(get_db)):
    _transfer_access(p, _load_transfer(db, transfer_id), side="from")
    t = commit_or_http(db, lambda: ts.approve(db, actor=p.user, transfer_id=transfer_id, note=body.note, ip=client_ip(request)))
    return _transfer_out(db, t, p.has("inventory.view_valuation"))


@router.post("/transfers/{transfer_id}/reject")
def reject_transfer(transfer_id: int, body: S.ReasonIn, request: Request, p: Principal = Depends(require("transfers.approve")),
                    db: Session = Depends(get_db)):
    _transfer_access(p, _load_transfer(db, transfer_id), side="from")
    t = commit_or_http(db, lambda: ts.reject(db, actor=p.user, transfer_id=transfer_id, reason=body.reason, ip=client_ip(request)))
    return _transfer_out(db, t, p.has("inventory.view_valuation"))


@router.post("/transfers/{transfer_id}/cancel")
def cancel_transfer(transfer_id: int, body: S.ReasonIn, request: Request, p: Principal = Depends(require("transfers.request")),
                    db: Session = Depends(get_db)):
    _transfer_access(p, _load_transfer(db, transfer_id), side="either")
    t = commit_or_http(db, lambda: ts.cancel(db, actor=p.user, transfer_id=transfer_id, reason=body.reason, ip=client_ip(request)))
    return _transfer_out(db, t, p.has("inventory.view_valuation"))


@router.post("/transfers/{transfer_id}/dispatch")
def dispatch_transfer(transfer_id: int, request: Request, p: Principal = Depends(require("transfers.dispatch")),
                      db: Session = Depends(get_db)):
    _transfer_access(p, _load_transfer(db, transfer_id), side="from")
    t = commit_or_http(db, lambda: ts.dispatch(db, actor=p.user, transfer_id=transfer_id, ip=client_ip(request)))
    return _transfer_out(db, t, p.has("inventory.view_valuation"))


@router.post("/transfers/{transfer_id}/receive")
def receive_transfer(transfer_id: int, body: S.TransferReceiveIn, request: Request,
                     p: Principal = Depends(require("transfers.receive")), db: Session = Depends(get_db)):
    _transfer_access(p, _load_transfer(db, transfer_id), side="to")
    t = commit_or_http(db, lambda: ts.receive(db, actor=p.user, transfer_id=transfer_id,
                                              lines=[l.model_dump() for l in body.lines], notes=body.notes, ip=client_ip(request)))
    return _transfer_out(db, t, p.has("inventory.view_valuation"))


@router.post("/transfers/{transfer_id}/flag-discrepancy")
def flag_discrepancy(transfer_id: int, body: S.ReasonIn, request: Request, p: Principal = Depends(require("transfers.receive")),
                     db: Session = Depends(get_db)):
    _transfer_access(p, _load_transfer(db, transfer_id), side="to")
    t = commit_or_http(db, lambda: ts.flag_shortage(db, actor=p.user, transfer_id=transfer_id, reason=body.reason, ip=client_ip(request)))
    return _transfer_out(db, t, p.has("inventory.view_valuation"))


@router.post("/transfers/{transfer_id}/resolve-discrepancy")
def resolve_discrepancy(transfer_id: int, body: S.RequiredNoteIn, request: Request,
                        p: Principal = Depends(require("transfers.approve_discrepancy")), db: Session = Depends(get_db)):
    _transfer_access(p, _load_transfer(db, transfer_id), side="either")
    t = commit_or_http(db, lambda: ts.resolve_discrepancy(db, actor=p.user, transfer_id=transfer_id, note=body.note, ip=client_ip(request)))
    return _transfer_out(db, t, p.has("inventory.view_valuation"))


# ---- counts --------------------------------------------------------------------------------------------------
def _count_out(db: Session, c: StockCount) -> dict:
    vm = {v.id: v for v in db.scalars(select(ProductVariant).where(ProductVariant.id.in_([l.variant_id for l in c.lines]))).all()}
    bm = {b.id: b for b in db.scalars(select(Batch).where(Batch.id.in_([l.batch_id for l in c.lines if l.batch_id]))).all()}
    var = {v["line"].id: v for v in count_service.variances(db, c)} if c.status == "in_progress" else {}
    lines = [{"id": l.id, "variant_id": l.variant_id, "sku": vm[l.variant_id].sku, "variant_name": vm[l.variant_id].name,
              "batch_no": bm[l.batch_id].batch_no if l.batch_id else None, "expected_qty": l.expected_qty,
              "counted_qty": l.counted_qty, "reason": l.reason,
              "moved_since_snapshot": var[l.id]["moved_since"] if l.id in var else None,
              "variance": var[l.id]["variance"] if l.id in var else None} for l in c.lines]
    return {"id": c.id, "ref": c.ref, "location_id": c.location_id, "status": c.status, "notes": c.notes,
            "created_at": c.created_at, "submitted_at": c.submitted_at, "lines": lines}


@router.get("/inventory/counts")
def list_counts(location_id: int | None = None, p: Principal = Depends(require("inventory.count")), db: Session = Depends(get_db)):
    ids, loc = _scope(p, location_id)
    q = select(StockCount)
    if loc:
        q = q.where(StockCount.location_id == loc)
    if ids is not None:
        q = q.where(StockCount.location_id.in_(ids))
    return [{"id": c.id, "ref": c.ref, "location_id": c.location_id, "status": c.status, "created_at": c.created_at,
             "lines": len(c.lines)} for c in db.scalars(q.order_by(StockCount.id.desc()).limit(100)).all()]


@router.post("/inventory/counts", status_code=201)
def create_count(body: S.CountCreateIn, request: Request, p: Principal = Depends(require("inventory.count")),
                 db: Session = Depends(get_db)):
    ensure_location_access(p, body.location_id)
    c = commit_or_http(db, lambda: count_service.create(db, actor=p.user, location_id=body.location_id,
                                                        variant_ids=body.variant_ids, notes=body.notes, ip=client_ip(request)))
    return _count_out(db, c)


def _load_count(db, p, count_id) -> StockCount:
    c = _nf(lambda: count_service.get_count(db, count_id))
    ensure_location_access(p, c.location_id)
    return c


@router.get("/inventory/counts/{count_id}")
def get_count(count_id: int, p: Principal = Depends(require("inventory.count")), db: Session = Depends(get_db)):
    return _count_out(db, _load_count(db, p, count_id))


@router.put("/inventory/counts/{count_id}/entries")
def enter_counts(count_id: int, body: S.CountEntriesIn, p: Principal = Depends(require("inventory.count")),
                 db: Session = Depends(get_db)):
    _load_count(db, p, count_id)
    c = commit_or_http(db, lambda: count_service.enter_counts(db, actor=p.user, count_id=count_id,
                                                              entries=[e.model_dump(exclude_unset=True) for e in body.entries]))
    return _count_out(db, c)


@router.post("/inventory/counts/{count_id}/submit")
def submit_count(count_id: int, request: Request, p: Principal = Depends(require("inventory.count")),
                 db: Session = Depends(get_db)):
    _load_count(db, p, count_id)
    c, req = commit_or_http(db, lambda: count_service.submit(db, actor=p.user, count_id=count_id, ip=client_ip(request)))
    return {**_count_out(db, c), "request_ref": req.ref if req else None}


@router.post("/inventory/counts/{count_id}/cancel")
def cancel_count(count_id: int, request: Request, p: Principal = Depends(require("inventory.count")),
                 db: Session = Depends(get_db)):
    _load_count(db, p, count_id)
    return _count_out(db, commit_or_http(db, lambda: count_service.cancel(db, actor=p.user, count_id=count_id, ip=client_ip(request))))


# ---- settings -------------------------------------------------------------------------------------------------
@router.get("/inventory/settings")
def get_settings_(p: Principal = Depends(require("inventory.view")), db: Session = Depends(get_db)):
    return settings_service.inventory_settings(db)


@router.put("/inventory/settings/costing")
def set_costing(body: S.CostingIn, request: Request, p: Principal = Depends(require("settings.manage")),
                db: Session = Depends(get_db)):
    return commit_or_http(db, lambda: settings_service.set_costing_method(
        db, actor=p.user, method=body.method, reason=body.reason, ip=client_ip(request)))


@router.put("/inventory/settings/thresholds")
def set_thresholds(body: S.ThresholdsIn, request: Request, p: Principal = Depends(require("settings.manage")),
                   db: Session = Depends(get_db)):
    return commit_or_http(db, lambda: settings_service.set_thresholds(
        db, actor=p.user, auto_approve_value=body.adjustment_auto_approve_value,
        expiry_warning_days=body.expiry_warning_days, reason=body.reason, ip=client_ip(request)))
