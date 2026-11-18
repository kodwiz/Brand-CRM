"""Distributor credit: records and terms, dispatches on credit, returns and exchanges, payments and allocations, adjustments,
receivable aging and monthly statements. Distributors are not users; their records are internal.

Visibility: managers, the CEO and accounts see every distributor; a stock keeper sees those assigned to them (and unassigned
ones). Dispatches and returns are also bound to the user's locations. Out-of-scope records answer 404."""
from __future__ import annotations

from datetime import date
from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from backend.api.common import commit_or_http
from backend.db import get_db
from backend.deps import Principal, client_ip, ensure_location_access, require, require_any
from backend.models import (
    Distributor, DistributorAdjustment, DistributorInvoice, DistributorPayment, DistributorPrice, DistributorReturn, ProductVariant, User,
)
from backend.schemas import distributors as S
from backend.services import (
    distributor_dispatch_service as dds, distributor_payment_service as dps, distributor_return_service as drs, distributor_service as ds,
    settings_service,
)
from backend.services.distributor_return_service import _committed as committed_return_qty
from backend.services.errors import BusinessRuleError, NotFoundError
from backend.services.scope import can_access_location, visible_location_ids

router = APIRouter(tags=["distributors"])


def _names(db: Session, ids: set) -> dict[int, str]:
    ids = {i for i in ids if i}
    return {u.id: u.username for u in db.scalars(select(User).where(User.id.in_(ids))).all()} if ids else {}


def _nf(fn):
    try:
        return fn()
    except NotFoundError as e:
        raise HTTPException(404, str(e))
    except BusinessRuleError as e:
        raise HTTPException(422, str(e))


def _dist(db: Session, p: Principal, distributor_id: int) -> Distributor:
    return _nf(lambda: ds.checked(db, p.user, distributor_id))


def _page(q, db, page, size, order):
    total = db.scalar(select(func.count()).select_from(q.subquery())) or 0
    return db.scalars(q.order_by(order).offset((page - 1) * size).limit(size)).all(), total


def _dist_out(db: Session, d: Distributor, detail: bool = False) -> dict:
    bal = ds.balance(db, d.id)
    out = {"id": d.id, "code": d.code, "name": d.name, "city": d.city, "phone": d.phone, "discount_pct": str(d.discount_pct),
           "credit_limit": str(ds.q2(Decimal(d.credit_limit))), "payment_terms_days": d.payment_terms_days, "is_active": d.is_active,
           "assigned_user_id": d.assigned_user_id, "balance": str(bal), "available_credit": str(ds.q2(Decimal(d.credit_limit) - bal))}
    if detail:
        ag = ds.aging(db, d.id)
        out.update(contact_name=d.contact_name, email=d.email, address=d.address, notes=d.notes,
                   assigned_user=_names(db, {d.assigned_user_id}).get(d.assigned_user_id), overdue=ag["overdue"],
                   unallocated_credit=ag["unallocated_credit"])
    else:
        out["overdue"] = ds.aging(db, d.id)["overdue"]
    return out


# ---- settings --------------------------------------------------------------------------------------------------------
@router.get("/distributors/settings")
def get_settings(p: Principal = Depends(require_any("distributors.view", "settings.manage")), db: Session = Depends(get_db)):
    return settings_service.distributor_settings(db)


@router.put("/distributors/settings")
def put_settings(body: S.DistributorSettingsIn, request: Request, p: Principal = Depends(require("settings.manage")), db: Session = Depends(get_db)):
    return commit_or_http(db, lambda: settings_service.set_distributor_settings(db, actor=p.user, values=body.model_dump(), reason=None, ip=client_ip(request)))


@router.get("/distributors/integrity")
def integrity(p: Principal = Depends(require_any("settings.manage", "distributors.approve")), db: Session = Depends(get_db)):
    problems = ds.integrity(db)
    return {"consistent": not problems, "problems": problems[:200]}


# ---- records ---------------------------------------------------------------------------------------------------------------
@router.get("/distributors")
def list_distributors(q: str | None = None, active: bool | None = None, page: int = Query(1, ge=1), page_size: int = Query(50, ge=1, le=200),
                      p: Principal = Depends(require("distributors.view")), db: Session = Depends(get_db)):
    stmt = select(Distributor)
    cond = ds.visible_filter(p.user)
    if cond is not None:
        stmt = stmt.where(cond)
    if active is not None:
        stmt = stmt.where(Distributor.is_active.is_(active))
    if q:
        like = f"%{q.lower()}%"
        stmt = stmt.where(func.lower(Distributor.name).like(like) | func.lower(Distributor.code).like(like))
    rows, total = _page(stmt, db, page, page_size, func.lower(Distributor.name))
    return {"items": [_dist_out(db, d) for d in rows], "total": total, "page": page, "page_size": page_size}


@router.post("/distributors", status_code=201)
def create_distributor(body: S.DistributorIn, request: Request, p: Principal = Depends(require("distributors.manage")), db: Session = Depends(get_db)):
    return commit_or_http(db, lambda: _dist_out(db, ds.create_distributor(db, actor=p.user, data=body.model_dump(), ip=client_ip(request)), True))


@router.get("/distributors/{distributor_id}")
def get_distributor(distributor_id: int, p: Principal = Depends(require("distributors.view")), db: Session = Depends(get_db)):
    return _dist_out(db, _dist(db, p, distributor_id), True)


@router.put("/distributors/{distributor_id}")
def update_distributor(distributor_id: int, body: S.DistributorUpdate, request: Request, p: Principal = Depends(require("distributors.manage")),
                       db: Session = Depends(get_db)):
    _dist(db, p, distributor_id)
    data = body.model_dump(exclude_unset=True)
    active = data.pop("is_active", None)

    def go():
        d = ds.update_distributor(db, actor=p.user, distributor_id=distributor_id, data=data, is_active=active, ip=client_ip(request))
        return _dist_out(db, d, True)
    return commit_or_http(db, go)


@router.get("/distributors/{distributor_id}/prices")
def list_prices(distributor_id: int, p: Principal = Depends(require_any("distributors.view")), db: Session = Depends(get_db)):
    d = _dist(db, p, distributor_id)
    rows = db.execute(select(DistributorPrice, ProductVariant).join(ProductVariant, ProductVariant.id == DistributorPrice.variant_id)
                      .where(DistributorPrice.distributor_id == d.id).order_by(ProductVariant.sku)).all()
    return [{"variant_id": v.id, "sku": v.sku, "name": v.name, "price": str(pr.price),
             "standard_price": str(v.distributor_price) if v.distributor_price is not None else None} for pr, v in rows]


@router.put("/distributors/{distributor_id}/prices/{variant_id}")
def put_price(distributor_id: int, variant_id: int, body: S.PriceIn, request: Request, p: Principal = Depends(require("distributors.manage")),
              db: Session = Depends(get_db)):
    _dist(db, p, distributor_id)
    return commit_or_http(db, lambda: ds.set_price(db, actor=p.user, distributor_id=distributor_id, variant_id=variant_id, price=body.price, ip=client_ip(request)))


@router.post("/distributors/{distributor_id}/opening-balance", status_code=201)
def opening_balance(distributor_id: int, body: S.OpeningIn, request: Request, p: Principal = Depends(require("distributors.manage", "distributors.approve")),
                    db: Session = Depends(get_db)):
    _dist(db, p, distributor_id)
    return commit_or_http(db, lambda: _invoice_out(db, p, dds.post_opening(db, actor=p.user, distributor_id=distributor_id, amount=body.amount,
                                                                         as_of=body.as_of, note=body.note, ip=client_ip(request))))


@router.get("/distributors/{distributor_id}/aging")
def get_aging(distributor_id: int, as_of: date | None = None, p: Principal = Depends(require("distributors.view")), db: Session = Depends(get_db)):
    d = _dist(db, p, distributor_id)
    return ds.aging(db, d.id, as_of)


@router.get("/distributors/{distributor_id}/statement")
def get_statement(distributor_id: int, month: str | None = None, p: Principal = Depends(require("distributors.view")), db: Session = Depends(get_db)):
    d = _dist(db, p, distributor_id)
    month = month or date.today().strftime("%Y-%m")
    out = _nf(lambda: ds.statement(db, d.id, month))
    out["distributor"] = {"id": d.id, "code": d.code, "name": d.name, "payment_terms_days": d.payment_terms_days}
    return out


@router.post("/distributors/{distributor_id}/allocate")
def allocate(distributor_id: int, body: S.AllocateIn, request: Request, p: Principal = Depends(require("distributors.receive_payment")),
             db: Session = Depends(get_db)):
    _dist(db, p, distributor_id)
    return commit_or_http(db, lambda: {"allocated": str(dps.allocate_more(
        db, actor=p.user, distributor_id=distributor_id, source_type=body.source_type, source_id=body.source_id,
        allocations=[a.model_dump() for a in body.allocations], ip=client_ip(request)))})


# ---- dispatches --------------------------------------------------------------------------------------------------------------------
def _invoice_out(db: Session, p: Principal, i: DistributorInvoice) -> dict:
    d = db.get(Distributor, i.distributor_id)
    names = _names(db, {i.created_by, i.posted_by, i.decided_by})
    vm = {v.id: v for v in db.scalars(select(ProductVariant).where(ProductVariant.id.in_([l.variant_id for l in i.lines] or [0]))).all()}
    show_cost = p.has("products.view_cost")
    lines = []
    for l in i.lines:
        row = {"id": l.id, "variant_id": l.variant_id, "sku": vm[l.variant_id].sku, "variant_name": vm[l.variant_id].name, "batch_id": l.batch_id,
               "qty": l.qty, "base_price": str(l.base_price), "discount_pct": str(l.discount_pct), "agreed_price": str(l.agreed_price),
               "unit_price": str(l.unit_price), "line_total": str(ds.q2(Decimal(l.unit_price) * l.qty))}
        if i.status == "posted" and i.kind == "dispatch":
            row["returnable"] = max(l.qty - committed_return_qty(db, l.id), 0)
        if show_cost:
            row["stock_value"] = str(l.stock_value) if l.stock_value is not None else None
        lines.append(row)
    out = ds.allocated_to(db, i.id)
    return {"id": i.id, "ref": i.ref, "kind": i.kind, "distributor": {"id": d.id, "code": d.code, "name": d.name}, "location_id": i.location_id,
            "invoice_date": i.invoice_date, "due_date": i.due_date, "status": i.status, "subtotal": str(i.subtotal),
            "discount_total": str(i.discount_total), "total": str(i.total), "allocated": str(out),
            "outstanding": str(ds.outstanding(db, i)), "hold_reasons": i.hold_reasons or [], "decision_note": i.decision_note,
            "decided_by": names.get(i.decided_by), "created_by": names.get(i.created_by), "posted_by": names.get(i.posted_by),
            "exchange_return_id": i.exchange_return_id, "notes": i.notes, "lines": lines}


def _invoice_checked(db: Session, p: Principal, invoice_id: int) -> DistributorInvoice:
    i = db.get(DistributorInvoice, invoice_id)
    if i is None:
        raise HTTPException(404, "Dispatch not found")
    _dist(db, p, i.distributor_id)
    if i.location_id is not None and not can_access_location(p.user, i.location_id):
        raise HTTPException(404, "Dispatch not found")
    return i


@router.get("/distributor-dispatches")
def list_dispatches(distributor_id: int | None = None, status: str | None = None, kind: str | None = None, page: int = Query(1, ge=1),
                    page_size: int = Query(50, ge=1, le=200), p: Principal = Depends(require("distributors.view")), db: Session = Depends(get_db)):
    stmt = select(DistributorInvoice)
    cond = ds.visible_filter(p.user)
    if cond is not None:
        stmt = stmt.where(DistributorInvoice.distributor_id.in_(select(Distributor.id).where(cond)))
    ids = visible_location_ids(p.user)
    if ids is not None:
        stmt = stmt.where((DistributorInvoice.location_id.is_(None)) | DistributorInvoice.location_id.in_(ids))
    if distributor_id:
        stmt = stmt.where(DistributorInvoice.distributor_id == distributor_id)
    if status:
        stmt = stmt.where(DistributorInvoice.status == status)
    if kind:
        stmt = stmt.where(DistributorInvoice.kind == kind)
    rows, total = _page(stmt, db, page, page_size, DistributorInvoice.id.desc())
    return {"items": [_invoice_out(db, p, i) for i in rows], "total": total, "page": page, "page_size": page_size}


@router.post("/distributor-dispatches", status_code=201)
def create_dispatch(body: S.DispatchIn, request: Request, p: Principal = Depends(require("distributors.transact")), db: Session = Depends(get_db)):
    _dist(db, p, body.distributor_id)
    ensure_location_access(p, body.location_id)

    def go():
        i = dds.create_dispatch(db, actor=p.user, distributor_id=body.distributor_id, location_id=body.location_id,
                                invoice_date=body.invoice_date or date.today(), lines=[l.model_dump() for l in body.lines], notes=body.notes,
                                exchange_return_id=body.exchange_return_id, idempotency_key=body.idempotency_key, ip=client_ip(request))
        if body.post_now and i.status == "draft":
            i = dds.post(db, actor=p.user, invoice_id=i.id, ip=client_ip(request))
        return _invoice_out(db, p, i)
    return commit_or_http(db, go)


@router.get("/distributor-dispatches/{invoice_id}")
def get_dispatch(invoice_id: int, p: Principal = Depends(require("distributors.view")), db: Session = Depends(get_db)):
    return _invoice_out(db, p, _invoice_checked(db, p, invoice_id))


@router.put("/distributor-dispatches/{invoice_id}")
def update_dispatch(invoice_id: int, body: S.DispatchUpdate, request: Request, p: Principal = Depends(require("distributors.transact")),
                    db: Session = Depends(get_db)):
    _invoice_checked(db, p, invoice_id)
    if body.location_id is not None:
        ensure_location_access(p, body.location_id)
    return commit_or_http(db, lambda: _invoice_out(db, p, dds.update_draft(
        db, actor=p.user, invoice_id=invoice_id, location_id=body.location_id, invoice_date=body.invoice_date,
        lines=[l.model_dump() for l in body.lines] if body.lines is not None else None, notes=body.notes, ip=client_ip(request))))


@router.post("/distributor-dispatches/{invoice_id}/post")
def post_dispatch(invoice_id: int, request: Request, p: Principal = Depends(require("distributors.transact")), db: Session = Depends(get_db)):
    i = _invoice_checked(db, p, invoice_id)
    ensure_location_access(p, i.location_id)
    return commit_or_http(db, lambda: _invoice_out(db, p, dds.post(db, actor=p.user, invoice_id=invoice_id, ip=client_ip(request))))


@router.post("/distributor-dispatches/{invoice_id}/decide")
def decide_dispatch(invoice_id: int, body: S.DecisionIn, request: Request, p: Principal = Depends(require("distributors.approve")),
                    db: Session = Depends(get_db)):
    _invoice_checked(db, p, invoice_id)
    return commit_or_http(db, lambda: _invoice_out(db, p, dds.decide(db, actor=p.user, invoice_id=invoice_id, approve=body.approve,
                                                                      note=body.note, ip=client_ip(request))))


@router.post("/distributor-dispatches/{invoice_id}/cancel")
def cancel_dispatch(invoice_id: int, body: S.ReasonIn, request: Request, p: Principal = Depends(require("distributors.transact")),
                    db: Session = Depends(get_db)):
    _invoice_checked(db, p, invoice_id)
    return commit_or_http(db, lambda: _invoice_out(db, p, dds.cancel(db, actor=p.user, invoice_id=invoice_id, reason=body.reason, ip=client_ip(request))))


# ---- returns -----------------------------------------------------------------------------------------------------------------------------
def _return_out(db: Session, p: Principal, r: DistributorReturn) -> dict:
    d = db.get(Distributor, r.distributor_id)
    names = _names(db, {r.requested_by, r.received_by, r.inspected_by, r.decided_by})
    vm = {v.id: v for v in db.scalars(select(ProductVariant).where(ProductVariant.id.in_([l.variant_id for l in r.lines] or [0]))).all()}
    return {"id": r.id, "ref": r.ref, "distributor": {"id": d.id, "code": d.code, "name": d.name}, "location_id": r.location_id,
            "return_date": r.return_date, "status": r.status, "reason": r.reason,
            "credit_value": str(r.credit_value) if r.credit_value is not None else None, "decision_note": r.decision_note,
            "requested_by": names.get(r.requested_by), "received_by": names.get(r.received_by), "inspected_by": names.get(r.inspected_by),
            "decided_by": names.get(r.decided_by),
            "lines": [{"id": l.id, "invoice_line_id": l.invoice_line_id, "variant_id": l.variant_id, "sku": vm[l.variant_id].sku,
                       "variant_name": vm[l.variant_id].name, "batch_id": l.batch_id, "qty_requested": l.qty_requested, "qty_received": l.qty_received,
                       "qty_restock": l.qty_restock, "qty_damaged": l.qty_damaged, "qty_expired": l.qty_expired, "credit_qty": l.credit_qty,
                       "unit_price": str(l.unit_price), "inspection_note": l.inspection_note} for l in r.lines]}


def _return_checked(db: Session, p: Principal, return_id: int) -> DistributorReturn:
    r = db.get(DistributorReturn, return_id)
    if r is None:
        raise HTTPException(404, "Return not found")
    _dist(db, p, r.distributor_id)
    if not can_access_location(p.user, r.location_id):
        raise HTTPException(404, "Return not found")
    return r


@router.get("/distributor-returns")
def list_returns(distributor_id: int | None = None, status: str | None = None, page: int = Query(1, ge=1), page_size: int = Query(50, ge=1, le=200),
                 p: Principal = Depends(require("distributors.view")), db: Session = Depends(get_db)):
    stmt = select(DistributorReturn)
    cond = ds.visible_filter(p.user)
    if cond is not None:
        stmt = stmt.where(DistributorReturn.distributor_id.in_(select(Distributor.id).where(cond)))
    ids = visible_location_ids(p.user)
    if ids is not None:
        stmt = stmt.where(DistributorReturn.location_id.in_(ids))
    if distributor_id:
        stmt = stmt.where(DistributorReturn.distributor_id == distributor_id)
    if status:
        stmt = stmt.where(DistributorReturn.status == status)
    rows, total = _page(stmt, db, page, page_size, DistributorReturn.id.desc())
    return {"items": [_return_out(db, p, r) for r in rows], "total": total, "page": page, "page_size": page_size}


@router.post("/distributor-returns", status_code=201)
def request_return(body: S.ReturnIn, request: Request, p: Principal = Depends(require("distributors.transact")), db: Session = Depends(get_db)):
    _dist(db, p, body.distributor_id)
    ensure_location_access(p, body.location_id)
    return commit_or_http(db, lambda: _return_out(db, p, drs.request_return(
        db, actor=p.user, distributor_id=body.distributor_id, location_id=body.location_id, lines=[l.model_dump() for l in body.lines],
        reason=body.reason, return_date=body.return_date or date.today(), ip=client_ip(request))))


@router.get("/distributor-returns/{return_id}")
def get_return(return_id: int, p: Principal = Depends(require("distributors.view")), db: Session = Depends(get_db)):
    return _return_out(db, p, _return_checked(db, p, return_id))


@router.post("/distributor-returns/{return_id}/receive")
def receive_return(return_id: int, body: S.ReceiveReturnIn, request: Request, p: Principal = Depends(require("distributors.transact")),
                   db: Session = Depends(get_db)):
    _return_checked(db, p, return_id)
    return commit_or_http(db, lambda: _return_out(db, p, drs.receive(db, actor=p.user, return_id=return_id, lines=[l.model_dump() for l in body.lines],
                                                                      ip=client_ip(request))))


@router.post("/distributor-returns/{return_id}/inspect")
def inspect_return(return_id: int, body: S.InspectIn, request: Request, p: Principal = Depends(require("distributors.transact")),
                   db: Session = Depends(get_db)):
    _return_checked(db, p, return_id)
    return commit_or_http(db, lambda: _return_out(db, p, drs.inspect(db, actor=p.user, return_id=return_id, lines=[l.model_dump() for l in body.lines],
                                                                      ip=client_ip(request))))


@router.post("/distributor-returns/{return_id}/decide")
def decide_return(return_id: int, body: S.DecisionIn, request: Request, p: Principal = Depends(require("distributors.approve")),
                  db: Session = Depends(get_db)):
    _return_checked(db, p, return_id)
    return commit_or_http(db, lambda: _return_out(db, p, drs.decide(db, actor=p.user, return_id=return_id, approve=body.approve, note=body.note,
                                                                     ip=client_ip(request))))


@router.post("/distributor-returns/{return_id}/cancel")
def cancel_return(return_id: int, body: S.ReasonIn, request: Request, p: Principal = Depends(require("distributors.transact")),
                  db: Session = Depends(get_db)):
    _return_checked(db, p, return_id)
    return commit_or_http(db, lambda: _return_out(db, p, drs.cancel(db, actor=p.user, return_id=return_id, reason=body.reason, ip=client_ip(request))))


# ---- payments ------------------------------------------------------------------------------------------------------------------------------
def _payment_out(db: Session, p: Principal, x: DistributorPayment) -> dict:
    d = db.get(Distributor, x.distributor_id)
    names = _names(db, {x.created_by, x.decided_by, x.reversed_by})
    got = dps.allocated(db, x) if x.status == "posted" else Decimal(0)
    return {"id": x.id, "ref": x.ref, "distributor": {"id": d.id, "code": d.code, "name": d.name}, "amount": str(x.amount), "method": x.method,
            "paid_date": x.paid_date, "reference": x.reference, "status": x.status, "hold_reason": x.hold_reason,
            "allocated": str(got), "unallocated": str(ds.q2(Decimal(x.amount) - got)) if x.status == "posted" else "0.00",
            "created_by": names.get(x.created_by), "decided_by": names.get(x.decided_by), "decision_note": x.decision_note,
            "reversed_by": names.get(x.reversed_by), "reverse_reason": x.reverse_reason, "notes": x.notes}


def _payment_checked(db: Session, p: Principal, payment_id: int) -> DistributorPayment:
    x = db.get(DistributorPayment, payment_id)
    if x is None:
        raise HTTPException(404, "Payment not found")
    _dist(db, p, x.distributor_id)
    return x


@router.get("/distributor-payments")
def list_payments(distributor_id: int | None = None, status: str | None = None, page: int = Query(1, ge=1), page_size: int = Query(50, ge=1, le=200),
                  p: Principal = Depends(require("distributors.view")), db: Session = Depends(get_db)):
    stmt = select(DistributorPayment)
    cond = ds.visible_filter(p.user)
    if cond is not None:
        stmt = stmt.where(DistributorPayment.distributor_id.in_(select(Distributor.id).where(cond)))
    if distributor_id:
        stmt = stmt.where(DistributorPayment.distributor_id == distributor_id)
    if status:
        stmt = stmt.where(DistributorPayment.status == status)
    rows, total = _page(stmt, db, page, page_size, DistributorPayment.id.desc())
    return {"items": [_payment_out(db, p, x) for x in rows], "total": total, "page": page, "page_size": page_size}


@router.post("/distributor-payments", status_code=201)
def record_payment(body: S.PaymentIn, request: Request, p: Principal = Depends(require("distributors.receive_payment")), db: Session = Depends(get_db)):
    _dist(db, p, body.distributor_id)
    return commit_or_http(db, lambda: _payment_out(db, p, dps.record_payment(
        db, actor=p.user, distributor_id=body.distributor_id, amount=body.amount, method=body.method, paid_date=body.paid_date or date.today(),
        reference=body.reference, allocations=[a.model_dump() for a in body.allocations], auto_allocate=body.auto_allocate, notes=body.notes,
        idempotency_key=body.idempotency_key, ip=client_ip(request))))


@router.get("/distributor-payments/{payment_id}")
def get_payment(payment_id: int, p: Principal = Depends(require("distributors.view")), db: Session = Depends(get_db)):
    return _payment_out(db, p, _payment_checked(db, p, payment_id))


@router.post("/distributor-payments/{payment_id}/decide")
def decide_payment(payment_id: int, body: S.DecisionIn, request: Request, p: Principal = Depends(require("distributors.approve")),
                   db: Session = Depends(get_db)):
    _payment_checked(db, p, payment_id)
    return commit_or_http(db, lambda: _payment_out(db, p, dps.decide_payment(db, actor=p.user, payment_id=payment_id, approve=body.approve,
                                                                              note=body.note, ip=client_ip(request))))


@router.post("/distributor-payments/{payment_id}/reverse")
def reverse_payment(payment_id: int, body: S.ReasonIn, request: Request, p: Principal = Depends(require("distributors.approve")),
                    db: Session = Depends(get_db)):
    _payment_checked(db, p, payment_id)
    return commit_or_http(db, lambda: _payment_out(db, p, dps.reverse_payment(db, actor=p.user, payment_id=payment_id, reason=body.reason,
                                                                               ip=client_ip(request))))


# ---- adjustments --------------------------------------------------------------------------------------------------------------------------------
def _adjustment_out(db: Session, a: DistributorAdjustment) -> dict:
    d = db.get(Distributor, a.distributor_id)
    names = _names(db, {a.requested_by, a.decided_by})
    return {"id": a.id, "ref": a.ref, "distributor": {"id": d.id, "code": d.code, "name": d.name}, "kind": a.kind, "amount": str(a.amount),
            "entry_date": a.entry_date, "invoice_id": a.invoice_id, "reason": a.reason, "status": a.status,
            "requested_by": names.get(a.requested_by), "decided_by": names.get(a.decided_by), "decision_note": a.decision_note}


@router.get("/distributor-adjustments")
def list_adjustments(distributor_id: int | None = None, status: str | None = None, page: int = Query(1, ge=1), page_size: int = Query(50, ge=1, le=200),
                     p: Principal = Depends(require("distributors.view")), db: Session = Depends(get_db)):
    stmt = select(DistributorAdjustment)
    cond = ds.visible_filter(p.user)
    if cond is not None:
        stmt = stmt.where(DistributorAdjustment.distributor_id.in_(select(Distributor.id).where(cond)))
    if distributor_id:
        stmt = stmt.where(DistributorAdjustment.distributor_id == distributor_id)
    if status:
        stmt = stmt.where(DistributorAdjustment.status == status)
    rows, total = _page(stmt, db, page, page_size, DistributorAdjustment.id.desc())
    return {"items": [_adjustment_out(db, a) for a in rows], "total": total, "page": page, "page_size": page_size}


@router.post("/distributor-adjustments", status_code=201)
def request_adjustment(body: S.AdjustmentIn, request: Request, p: Principal = Depends(require("distributors.transact")), db: Session = Depends(get_db)):
    _dist(db, p, body.distributor_id)
    return commit_or_http(db, lambda: _adjustment_out(db, dps.request_adjustment(
        db, actor=p.user, distributor_id=body.distributor_id, kind=body.kind, amount=body.amount, entry_date=body.entry_date or date.today(),
        invoice_id=body.invoice_id, reason=body.reason, ip=client_ip(request))))


@router.post("/distributor-adjustments/{adjustment_id}/decide")
def decide_adjustment(adjustment_id: int, body: S.DecisionIn, request: Request, p: Principal = Depends(require("distributors.approve")),
                      db: Session = Depends(get_db)):
    a = db.get(DistributorAdjustment, adjustment_id)
    if a is None:
        raise HTTPException(404, "Adjustment not found")
    _dist(db, p, a.distributor_id)
    return commit_or_http(db, lambda: _adjustment_out(db, dps.decide_adjustment(db, actor=p.user, adjustment_id=adjustment_id, approve=body.approve,
                                                                                 note=body.note, ip=client_ip(request))))
