"""Suppliers and purchasing endpoints: suppliers, purchase orders, supplier invoices, payments, adjustments, returns,
payables reports. Every route checks permission; location-bound documents (orders, returns) also check the location."""
from __future__ import annotations

from datetime import date

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from backend.api.common import commit_or_http
from backend.db import get_db
from backend.deps import Principal, client_ip, ensure_location_access, require, require_any
from backend.models import (
    ProductVariant, PurchaseOrder, Supplier, SupplierAdjustment, SupplierInvoice, SupplierPayment, SupplierReturn, User,
)
from backend.schemas import purchasing as S
from backend.services import (
    adjustment_service, invoice_service, payables_query_service as pq, payment_service, purchase_service, settings_service,
    supplier_return_service, supplier_service,
)
from backend.services.errors import NotFoundError
from backend.services.scope import visible_location_ids

router = APIRouter(tags=["purchasing"])


def _nf(fn):
    try:
        return fn()
    except NotFoundError as e:
        raise HTTPException(404, str(e))


def _names(db: Session, ids: set) -> dict[int, str]:
    ids = {i for i in ids if i}
    if not ids:
        return {}
    return {u.id: u.username for u in db.scalars(select(User).where(User.id.in_(ids))).all()}


def _sup_brief(db: Session, ids: set[int]) -> dict[int, dict]:
    ids = {i for i in ids if i}
    if not ids:
        return {}
    return {s.id: {"id": s.id, "code": s.code, "name": s.name} for s in db.scalars(select(Supplier).where(Supplier.id.in_(ids))).all()}


def _page(q, db: Session, page: int, page_size: int, order):
    total = db.scalar(select(func.count()).select_from(q.subquery())) or 0
    return db.scalars(q.order_by(order).offset((page - 1) * page_size).limit(page_size)).all(), total


# ---- suppliers ------------------------------------------------------------------------------------------
def _supplier_out(s: Supplier, bal=None) -> dict:
    d = {"id": s.id, "code": s.code, "name": s.name, "contact_name": s.contact_name, "phone": s.phone, "email": s.email,
         "address": s.address, "city": s.city, "tax_number": s.tax_number, "payment_terms_days": s.payment_terms_days,
         "currency": s.currency, "notes": s.notes, "is_active": s.is_active}
    if bal is not None:
        d["balance"] = str(bal)
    return d


@router.get("/suppliers")
def list_suppliers(q: str | None = None, active: bool | None = None, page: int = Query(1, ge=1),
                   page_size: int = Query(50, ge=1, le=200), p: Principal = Depends(require_any("suppliers.view", "purchasing.view")),
                   db: Session = Depends(get_db)):
    rows, total = supplier_service.list_suppliers(db, q=q, active=active, page=page, page_size=page_size)
    fin = p.has("suppliers.view")
    return {"items": [_supplier_out(s, supplier_service.balance(db, s.id) if fin else None) for s in rows],
            "total": total, "page": page, "page_size": page_size}


@router.post("/suppliers", status_code=201)
def create_supplier(body: S.SupplierIn, request: Request, p: Principal = Depends(require("suppliers.manage")),
                    db: Session = Depends(get_db)):
    s = commit_or_http(db, lambda: supplier_service.create_supplier(db, actor=p.user, data=body.model_dump(), ip=client_ip(request)))
    return _supplier_out(s, 0 if p.has("suppliers.view") else None)


@router.post("/suppliers/{supplier_id}/opening-balance", status_code=201)
def supplier_opening_balance(supplier_id: int, body: S.SupplierOpeningIn, request: Request, p: Principal = Depends(require("accounting.opening_balances")),
                             db: Session = Depends(get_db)):
    """What we already owed this supplier before the system (from the paper register). Once per supplier; corrections are adjustments."""
    e = commit_or_http(db, lambda: supplier_service.post_opening(db, actor=p.user, supplier_id=supplier_id, amount=body.amount, as_of=body.as_of, note=body.note, ip=client_ip(request)))
    return {"ref": e.ref, "amount": str(e.amount), "as_of": e.entry_date, "balance": str(supplier_service.balance(db, supplier_id))}


@router.get("/suppliers/{supplier_id}")
def get_supplier(supplier_id: int, p: Principal = Depends(require("suppliers.view")), db: Session = Depends(get_db)):
    s = _nf(lambda: supplier_service.get_supplier(db, supplier_id))
    return _supplier_out(s, supplier_service.balance(db, s.id)) | {"summary": pq.supplier_summary(db, s.id)}


@router.put("/suppliers/{supplier_id}")
def update_supplier(supplier_id: int, body: S.SupplierUpdate, request: Request,
                    p: Principal = Depends(require("suppliers.manage")), db: Session = Depends(get_db)):
    s = commit_or_http(db, lambda: supplier_service.update_supplier(
        db, actor=p.user, supplier_id=supplier_id, data=body.model_dump(exclude_unset=True), ip=client_ip(request)))
    return _supplier_out(s, supplier_service.balance(db, s.id) if p.has("suppliers.view") else None)


@router.get("/suppliers/{supplier_id}/statement")
def statement(supplier_id: int, date_from: date | None = None, date_to: date | None = None,
              p: Principal = Depends(require("suppliers.view")), db: Session = Depends(get_db)):
    return _nf(lambda: pq.statement(db, supplier_id, date_from, date_to))


# ---- purchase orders ------------------------------------------------------------------------------------
def _po_out(db: Session, po: PurchaseOrder, show_cost: bool, detail: bool = True) -> dict:
    vm = {v.id: v for v in db.scalars(select(ProductVariant).where(ProductVariant.id.in_([l.variant_id for l in po.lines]))).all()}
    qty = purchase_service.line_quantities(db, po.id)
    lines = []
    for l in po.lines:
        q = qty[l.id]
        d = {"id": l.id, "variant_id": l.variant_id, "sku": vm[l.variant_id].sku, "variant_name": vm[l.variant_id].name,
             "qty_ordered": l.qty_ordered, "qty_received": q["received"], "qty_accepted": q["accepted"],
             "qty_damaged": q["damaged"], "qty_rejected": q["rejected"], "qty_owned": q["owned"],
             "qty_remaining": q["remaining_to_receive"], "notes": l.notes}
        if show_cost:
            d["unit_cost"] = str(l.unit_cost)
            d["qty_invoiced"] = q["invoiced"]
        lines.append(d)
    names = _names(db, {po.created_by, po.approved_by, po.closed_by})
    out = {"id": po.id, "ref": po.ref, "supplier": _sup_brief(db, {po.supplier_id}).get(po.supplier_id),
           "location_id": po.location_id, "status": po.status, "order_date": po.order_date, "expected_date": po.expected_date,
           "payment_terms_days": po.payment_terms_days, "supplier_reference": po.supplier_reference, "notes": po.notes,
           "created_by": names.get(po.created_by), "approved_by": names.get(po.approved_by), "approved_at": po.approved_at,
           "closed_by": names.get(po.closed_by), "close_reason": po.close_reason, "lines": lines,
           "units_ordered": sum(l.qty_ordered for l in po.lines), "units_owned": sum(q["owned"] for q in qty.values())}
    if show_cost:
        out["total"] = str(purchase_service.po_total(po))
    return out


@router.get("/purchase-orders")
def list_pos(supplier_id: int | None = None, location_id: int | None = None, status: str | None = None, q: str | None = None,
             page: int = Query(1, ge=1), page_size: int = Query(50, ge=1, le=200),
             p: Principal = Depends(require("purchasing.view")), db: Session = Depends(get_db)):
    if location_id is not None:
        ensure_location_access(p, location_id)
    stmt = select(PurchaseOrder)
    ids = visible_location_ids(p.user)
    if ids is not None:
        stmt = stmt.where(PurchaseOrder.location_id.in_(ids))
    if location_id:
        stmt = stmt.where(PurchaseOrder.location_id == location_id)
    if supplier_id:
        stmt = stmt.where(PurchaseOrder.supplier_id == supplier_id)
    if status:
        stmt = stmt.where(PurchaseOrder.status == status)
    if q:
        stmt = stmt.where(or_(func.lower(PurchaseOrder.ref).like(f"%{q.lower()}%"),
                              func.lower(func.coalesce(PurchaseOrder.supplier_reference, "")).like(f"%{q.lower()}%")))
    rows, total = _page(stmt, db, page, page_size, PurchaseOrder.id.desc())
    sc = p.has("products.view_cost")
    return {"items": [_po_out(db, po, sc) for po in rows], "total": total, "page": page, "page_size": page_size}


@router.post("/purchase-orders", status_code=201)
def create_po(body: S.POIn, request: Request, p: Principal = Depends(require("purchasing.manage", "products.view_cost")),
              db: Session = Depends(get_db)):
    ensure_location_access(p, body.location_id)

    def run():
        po = purchase_service.create_po(
            db, actor=p.user, supplier_id=body.supplier_id, location_id=body.location_id, order_date=body.order_date,
            expected_date=body.expected_date, lines=[l.model_dump() for l in body.lines], notes=body.notes,
            supplier_reference=body.supplier_reference, ip=client_ip(request))
        if body.approve_now:
            if not p.has("purchasing.approve"):
                raise HTTPException(403, "You do not have permission to approve purchase orders")
            purchase_service.approve_po(db, actor=p.user, po_id=po.id, ip=client_ip(request))
        return po

    return _po_out(db, commit_or_http(db, run), True)


def _po_checked(db: Session, p: Principal, po_id: int, lock: bool = False) -> PurchaseOrder:
    po = _nf(lambda: purchase_service.get_po(db, po_id))
    ensure_location_access(p, po.location_id)
    return po


@router.get("/purchase-orders/{po_id}")
def get_po(po_id: int, p: Principal = Depends(require("purchasing.view")), db: Session = Depends(get_db)):
    po = _po_checked(db, p, po_id)
    return _po_out(db, po, p.has("products.view_cost"))


@router.get("/purchase-orders/{po_id}/matching")
def po_matching(po_id: int, p: Principal = Depends(require("purchasing.view")), db: Session = Depends(get_db)):
    po = _po_checked(db, p, po_id)
    rows = purchase_service.matching(db, po)
    if not p.has("products.view_cost"):
        for r in rows:
            r.pop("unit_cost", None)
            r.pop("invoiced_value", None)
    return {"po": _po_out(db, po, p.has("products.view_cost")), "lines": rows}


@router.put("/purchase-orders/{po_id}")
def update_po(po_id: int, body: S.POUpdate, request: Request, p: Principal = Depends(require("purchasing.manage", "products.view_cost")),
              db: Session = Depends(get_db)):
    po = _po_checked(db, p, po_id)
    if body.location_id:
        ensure_location_access(p, body.location_id)
    d = body.model_dump(exclude_unset=True)
    po = commit_or_http(db, lambda: purchase_service.update_po(
        db, actor=p.user, po_id=po_id, supplier_id=d.get("supplier_id"), location_id=d.get("location_id"),
        order_date=d.get("order_date"), expected_date=d.get("expected_date"),
        lines=[l.model_dump() for l in body.lines] if body.lines is not None else None, notes=d.get("notes"),
        supplier_reference=d.get("supplier_reference"), ip=client_ip(request)))
    return _po_out(db, po, True)


@router.post("/purchase-orders/{po_id}/approve")
def approve_po(po_id: int, request: Request, p: Principal = Depends(require("purchasing.approve")), db: Session = Depends(get_db)):
    _po_checked(db, p, po_id)
    po = commit_or_http(db, lambda: purchase_service.approve_po(db, actor=p.user, po_id=po_id, ip=client_ip(request)))
    return _po_out(db, po, p.has("products.view_cost"))


@router.post("/purchase-orders/{po_id}/cancel")
def cancel_po(po_id: int, body: S.ReasonIn, request: Request, p: Principal = Depends(require("purchasing.manage")),
              db: Session = Depends(get_db)):
    _po_checked(db, p, po_id)
    po = commit_or_http(db, lambda: purchase_service.cancel_po(db, actor=p.user, po_id=po_id, reason=body.reason, ip=client_ip(request)))
    return _po_out(db, po, p.has("products.view_cost"))


@router.post("/purchase-orders/{po_id}/close")
def close_po(po_id: int, body: S.ReasonIn, request: Request, p: Principal = Depends(require("purchasing.manage")),
             db: Session = Depends(get_db)):
    _po_checked(db, p, po_id)
    po = commit_or_http(db, lambda: purchase_service.close_po(db, actor=p.user, po_id=po_id, reason=body.reason, ip=client_ip(request)))
    return _po_out(db, po, p.has("products.view_cost"))


# ---- supplier invoices ----------------------------------------------------------------------------------
def _invoice_out(db: Session, inv: SupplierInvoice) -> dict:
    vm = {v.id: v for v in db.scalars(select(ProductVariant).where(
        ProductVariant.id.in_([l.variant_id for l in inv.lines if l.variant_id]))).all()}
    names = _names(db, {inv.created_by, inv.posted_by, inv.decided_by, inv.voided_by})
    po = db.get(PurchaseOrder, inv.purchase_order_id) if inv.purchase_order_id else None
    lines = [{"id": l.id, "po_line_id": l.po_line_id, "variant_id": l.variant_id,
              "sku": vm[l.variant_id].sku if l.variant_id else None, "description": l.description or (vm[l.variant_id].name if l.variant_id else None),
              "qty": l.qty, "unit_price": str(l.unit_price), "line_total": str(l.line_total)} for l in inv.lines]
    paid = invoice_service.paid_amount(db, inv.id)
    return {"id": inv.id, "ref": inv.ref, "supplier": _sup_brief(db, {inv.supplier_id}).get(inv.supplier_id),
            "invoice_no": inv.invoice_no, "invoice_date": inv.invoice_date, "due_date": inv.due_date, "status": inv.status,
            "purchase_order_id": inv.purchase_order_id, "po_ref": po.ref if po else None,
            "subtotal": str(inv.subtotal), "charges_total": str(inv.charges_total), "discount": str(inv.discount),
            "tax": str(inv.tax), "total": str(inv.total), "paid": str(paid),
            "credited": str(invoice_service.credited_amount(db, inv.id)),
            "outstanding": str(invoice_service.outstanding(db, inv)), "payment_status": invoice_service.payment_status(db, inv),
            "overdue": inv.status == "posted" and inv.due_date < date.today() and invoice_service.outstanding(db, inv) > 0,
            "match_status": inv.match_status, "variances": inv.variances or [], "notes": inv.notes, "lines": lines,
            "charges": [{"description": c.description, "amount": str(c.amount)} for c in inv.charges],
            "created_by": names.get(inv.created_by), "posted_by": names.get(inv.posted_by), "posted_at": inv.posted_at,
            "decided_by": names.get(inv.decided_by), "decision_note": inv.decision_note, "void_reason": inv.void_reason}


@router.get("/supplier-invoices")
def list_invoices(supplier_id: int | None = None, purchase_order_id: int | None = None, status: str | None = None,
                  unpaid_only: bool = False, overdue_only: bool = False, q: str | None = None, page: int = Query(1, ge=1),
                  page_size: int = Query(50, ge=1, le=200), p: Principal = Depends(require("suppliers.view")),
                  db: Session = Depends(get_db)):
    stmt = select(SupplierInvoice)
    if supplier_id:
        stmt = stmt.where(SupplierInvoice.supplier_id == supplier_id)
    if purchase_order_id:
        stmt = stmt.where(SupplierInvoice.purchase_order_id == purchase_order_id)
    if status:
        stmt = stmt.where(SupplierInvoice.status == status)
    if q:
        stmt = stmt.where(or_(func.lower(SupplierInvoice.invoice_no).like(f"%{q.lower()}%"),
                              func.lower(SupplierInvoice.ref).like(f"%{q.lower()}%")))
    if unpaid_only or overdue_only:
        stmt = stmt.where(SupplierInvoice.status == "posted")
        if overdue_only:
            stmt = stmt.where(SupplierInvoice.due_date < date.today())
        ids_ = [i.id for i, _ in pq.open_invoices(db, supplier_id)]
        stmt = stmt.where(SupplierInvoice.id.in_(ids_ or [0]))
    rows, total = _page(stmt, db, page, page_size, SupplierInvoice.id.desc())
    return {"items": [_invoice_out(db, i) for i in rows], "total": total, "page": page, "page_size": page_size}


@router.post("/supplier-invoices", status_code=201)
def create_invoice(body: S.InvoiceIn, request: Request, p: Principal = Depends(require("purchasing.manage", "suppliers.view")),
                   db: Session = Depends(get_db)):
    inv = commit_or_http(db, lambda: invoice_service.create_invoice(
        db, actor=p.user, supplier_id=body.supplier_id, invoice_no=body.invoice_no, invoice_date=body.invoice_date,
        due_date=body.due_date, purchase_order_id=body.purchase_order_id, lines=[l.model_dump() for l in body.lines],
        charges=[c.model_dump() for c in body.charges], discount=body.discount, tax=body.tax, notes=body.notes,
        post_now=body.post_now, ip=client_ip(request)))
    return _invoice_out(db, inv)


@router.get("/supplier-invoices/{invoice_id}")
def get_invoice(invoice_id: int, p: Principal = Depends(require("suppliers.view")), db: Session = Depends(get_db)):
    return _invoice_out(db, _nf(lambda: invoice_service.get_invoice(db, invoice_id)))


@router.post("/supplier-invoices/{invoice_id}/post")
def post_invoice(invoice_id: int, request: Request, p: Principal = Depends(require("purchasing.manage")), db: Session = Depends(get_db)):
    inv = commit_or_http(db, lambda: invoice_service.post_invoice(db, actor=p.user, invoice_id=invoice_id, ip=client_ip(request)))
    return _invoice_out(db, inv)


@router.post("/supplier-invoices/{invoice_id}/decide")
def decide_invoice(invoice_id: int, body: S.DecisionIn, request: Request,
                   p: Principal = Depends(require("purchasing.approve_variance")), db: Session = Depends(get_db)):
    inv = commit_or_http(db, lambda: invoice_service.decide_invoice(
        db, actor=p.user, invoice_id=invoice_id, approve=body.approve, note=body.note, ip=client_ip(request)))
    return _invoice_out(db, inv)


@router.post("/supplier-invoices/{invoice_id}/cancel")
def cancel_invoice(invoice_id: int, body: S.ReasonIn, request: Request, p: Principal = Depends(require("purchasing.manage")),
                   db: Session = Depends(get_db)):
    inv = commit_or_http(db, lambda: invoice_service.cancel_draft(db, actor=p.user, invoice_id=invoice_id, reason=body.reason,
                                                                   ip=client_ip(request)))
    return _invoice_out(db, inv)


@router.post("/supplier-invoices/{invoice_id}/void")
def void_invoice(invoice_id: int, body: S.ReasonIn, request: Request, p: Principal = Depends(require("purchasing.reverse")),
                 db: Session = Depends(get_db)):
    inv = commit_or_http(db, lambda: invoice_service.void_invoice(db, actor=p.user, invoice_id=invoice_id, reason=body.reason,
                                                                   ip=client_ip(request)))
    return _invoice_out(db, inv)


# ---- payments -------------------------------------------------------------------------------------------
def _payment_out(db: Session, pay: SupplierPayment) -> dict:
    inv = {i.id: i for i in db.scalars(select(SupplierInvoice).where(SupplierInvoice.id.in_([a.invoice_id for a in pay.allocations]))).all()} \
        if pay.allocations else {}
    names = _names(db, {pay.created_by, pay.reversed_by})
    return {"id": pay.id, "ref": pay.ref, "supplier": _sup_brief(db, {pay.supplier_id}).get(pay.supplier_id), "amount": str(pay.amount),
            "method": pay.method, "paid_date": pay.paid_date, "reference": pay.reference, "notes": pay.notes, "status": pay.status,
            "allocated": str(payment_service.allocated(pay)), "unallocated": str(payment_service.unallocated(pay)),
            "allocations": [{"invoice_id": a.invoice_id, "invoice_ref": inv[a.invoice_id].ref, "invoice_no": inv[a.invoice_id].invoice_no,
                             "amount": str(a.amount), "released": a.released_at is not None} for a in pay.allocations],
            "created_by": names.get(pay.created_by), "reversed_by": names.get(pay.reversed_by), "reverse_reason": pay.reverse_reason}


@router.get("/supplier-payments")
def list_payments(supplier_id: int | None = None, status: str | None = None, page: int = Query(1, ge=1),
                  page_size: int = Query(50, ge=1, le=200), p: Principal = Depends(require("suppliers.view")),
                  db: Session = Depends(get_db)):
    stmt = select(SupplierPayment)
    if supplier_id:
        stmt = stmt.where(SupplierPayment.supplier_id == supplier_id)
    if status:
        stmt = stmt.where(SupplierPayment.status == status)
    rows, total = _page(stmt, db, page, page_size, SupplierPayment.id.desc())
    return {"items": [_payment_out(db, x) for x in rows], "total": total, "page": page, "page_size": page_size}


@router.post("/supplier-payments", status_code=201)
def create_payment(body: S.PaymentIn, request: Request, p: Principal = Depends(require("purchasing.pay", "suppliers.view")),
                   db: Session = Depends(get_db)):
    pay = commit_or_http(db, lambda: payment_service.create_payment(
        db, actor=p.user, supplier_id=body.supplier_id, amount=body.amount, method=body.method, paid_date=body.paid_date,
        reference=body.reference, notes=body.notes, allocations=[a.model_dump() for a in body.allocations],
        on_account=body.on_account, idempotency_key=body.idempotency_key, ip=client_ip(request)))
    return _payment_out(db, pay)


@router.get("/supplier-payments/{payment_id}")
def get_payment(payment_id: int, p: Principal = Depends(require("suppliers.view")), db: Session = Depends(get_db)):
    return _payment_out(db, _nf(lambda: payment_service.get_payment(db, payment_id)))


@router.post("/supplier-payments/{payment_id}/allocate")
def allocate_payment(payment_id: int, body: S.AllocateIn, request: Request, p: Principal = Depends(require("purchasing.pay")),
                     db: Session = Depends(get_db)):
    pay = commit_or_http(db, lambda: payment_service.allocate_more(
        db, actor=p.user, payment_id=payment_id, allocations=[a.model_dump() for a in body.allocations], ip=client_ip(request)))
    return _payment_out(db, pay)


@router.post("/supplier-payments/{payment_id}/reverse")
def reverse_payment(payment_id: int, body: S.ReasonIn, request: Request, p: Principal = Depends(require("purchasing.reverse")),
                    db: Session = Depends(get_db)):
    pay = commit_or_http(db, lambda: payment_service.reverse_payment(db, actor=p.user, payment_id=payment_id, reason=body.reason,
                                                                      ip=client_ip(request)))
    return _payment_out(db, pay)


# ---- adjustments ----------------------------------------------------------------------------------------
def _adj_out(db: Session, a: SupplierAdjustment) -> dict:
    names = _names(db, {a.requested_by, a.decided_by})
    inv = db.get(SupplierInvoice, a.invoice_id) if a.invoice_id else None
    ret = db.get(SupplierReturn, a.supplier_return_id) if a.supplier_return_id else None
    return {"id": a.id, "ref": a.ref, "supplier": _sup_brief(db, {a.supplier_id}).get(a.supplier_id), "kind": a.kind,
            "amount": str(a.amount), "adjustment_date": a.adjustment_date, "document_no": a.document_no, "reason": a.reason,
            "invoice_id": a.invoice_id, "invoice_ref": inv.ref if inv else None,
            "supplier_return_id": a.supplier_return_id, "return_ref": ret.ref if ret else None, "status": a.status,
            "requested_by": names.get(a.requested_by), "decided_by": names.get(a.decided_by), "decision_note": a.decision_note}


@router.get("/supplier-adjustments")
def list_adjustments(supplier_id: int | None = None, status: str | None = None, page: int = Query(1, ge=1),
                     page_size: int = Query(50, ge=1, le=200), p: Principal = Depends(require("suppliers.view")),
                     db: Session = Depends(get_db)):
    stmt = select(SupplierAdjustment)
    if supplier_id:
        stmt = stmt.where(SupplierAdjustment.supplier_id == supplier_id)
    if status:
        stmt = stmt.where(SupplierAdjustment.status == status)
    rows, total = _page(stmt, db, page, page_size, SupplierAdjustment.id.desc())
    return {"items": [_adj_out(db, a) for a in rows], "total": total, "page": page, "page_size": page_size}


@router.post("/supplier-adjustments", status_code=201)
def request_adjustment(body: S.AdjustmentIn, request: Request, p: Principal = Depends(require("purchasing.manage", "suppliers.view")),
                       db: Session = Depends(get_db)):
    a = commit_or_http(db, lambda: adjustment_service.request_adjustment(
        db, actor=p.user, supplier_id=body.supplier_id, kind=body.kind, amount=body.amount, adjustment_date=body.adjustment_date,
        document_no=body.document_no, reason=body.reason, invoice_id=body.invoice_id,
        supplier_return_id=body.supplier_return_id, ip=client_ip(request)))
    return _adj_out(db, a)


@router.post("/supplier-adjustments/{adj_id}/approve")
def approve_adjustment(adj_id: int, body: S.NoteIn, request: Request, p: Principal = Depends(require("purchasing.approve", "suppliers.view")),
                       db: Session = Depends(get_db)):
    a = commit_or_http(db, lambda: adjustment_service.approve(db, actor=p.user, adj_id=adj_id, note=body.note, ip=client_ip(request)))
    return _adj_out(db, a)


@router.post("/supplier-adjustments/{adj_id}/reject")
def reject_adjustment(adj_id: int, body: S.ReasonIn, request: Request, p: Principal = Depends(require("purchasing.approve", "suppliers.view")),
                      db: Session = Depends(get_db)):
    a = commit_or_http(db, lambda: adjustment_service.reject(db, actor=p.user, adj_id=adj_id, note=body.reason, ip=client_ip(request)))
    return _adj_out(db, a)


@router.post("/supplier-adjustments/{adj_id}/cancel")
def cancel_adjustment(adj_id: int, request: Request, p: Principal = Depends(require("purchasing.manage")), db: Session = Depends(get_db)):
    a = commit_or_http(db, lambda: adjustment_service.cancel(db, actor=p.user, adj_id=adj_id, ip=client_ip(request)))
    return _adj_out(db, a)


# ---- supplier returns -----------------------------------------------------------------------------------
def _return_out(db: Session, r: SupplierReturn, show_value: bool) -> dict:
    vm = {v.id: v for v in db.scalars(select(ProductVariant).where(ProductVariant.id.in_([l.variant_id for l in r.lines]))).all()}
    names = _names(db, {r.created_by, r.dispatched_by})
    lines = []
    for l in r.lines:
        d = {"id": l.id, "variant_id": l.variant_id, "sku": vm[l.variant_id].sku, "variant_name": vm[l.variant_id].name,
             "batch_id": l.batch_id, "bucket": l.bucket, "qty": l.qty}
        if show_value:
            d["stock_value"] = str(l.stock_value) if l.stock_value is not None else None
        lines.append(d)
    adj = db.scalar(select(SupplierAdjustment).where(SupplierAdjustment.supplier_return_id == r.id))
    return {"id": r.id, "ref": r.ref, "supplier": _sup_brief(db, {r.supplier_id}).get(r.supplier_id), "location_id": r.location_id,
            "status": r.status, "reason": r.reason, "notes": r.notes, "purchase_order_id": r.purchase_order_id,
            "receipt_id": r.receipt_id, "lines": lines, "created_by": names.get(r.created_by),
            "dispatched_by": names.get(r.dispatched_by), "dispatched_at": r.dispatched_at,
            "credit_ref": adj.ref if adj else None, "credit_status": adj.status if adj else None}


@router.get("/supplier-returns")
def list_returns(supplier_id: int | None = None, location_id: int | None = None, status: str | None = None,
                 page: int = Query(1, ge=1), page_size: int = Query(50, ge=1, le=200),
                 p: Principal = Depends(require("purchasing.view")), db: Session = Depends(get_db)):
    if location_id is not None:
        ensure_location_access(p, location_id)
    stmt = select(SupplierReturn)
    ids = visible_location_ids(p.user)
    if ids is not None:
        stmt = stmt.where(SupplierReturn.location_id.in_(ids))
    if location_id:
        stmt = stmt.where(SupplierReturn.location_id == location_id)
    if supplier_id:
        stmt = stmt.where(SupplierReturn.supplier_id == supplier_id)
    if status:
        stmt = stmt.where(SupplierReturn.status == status)
    rows, total = _page(stmt, db, page, page_size, SupplierReturn.id.desc())
    sv = p.has("inventory.view_valuation")
    return {"items": [_return_out(db, r, sv) for r in rows], "total": total, "page": page, "page_size": page_size}


@router.post("/supplier-returns", status_code=201)
def create_return(body: S.ReturnIn, request: Request, p: Principal = Depends(require("purchasing.manage")), db: Session = Depends(get_db)):
    ensure_location_access(p, body.location_id)

    def run():
        r = supplier_return_service.create_return(
            db, actor=p.user, supplier_id=body.supplier_id, location_id=body.location_id, reason=body.reason, notes=body.notes,
            purchase_order_id=body.purchase_order_id, receipt_id=body.receipt_id, lines=[l.model_dump() for l in body.lines],
            ip=client_ip(request))
        if body.dispatch_now:
            supplier_return_service.dispatch(db, actor=p.user, return_id=r.id, ip=client_ip(request))
        return r

    return _return_out(db, commit_or_http(db, run), p.has("inventory.view_valuation"))


def _ret_checked(db: Session, p: Principal, return_id: int) -> SupplierReturn:
    r = _nf(lambda: supplier_return_service.get_return(db, return_id))
    ensure_location_access(p, r.location_id)
    return r


@router.get("/supplier-returns/{return_id}")
def get_return(return_id: int, p: Principal = Depends(require("purchasing.view")), db: Session = Depends(get_db)):
    return _return_out(db, _ret_checked(db, p, return_id), p.has("inventory.view_valuation"))


@router.post("/supplier-returns/{return_id}/dispatch")
def dispatch_return(return_id: int, request: Request, p: Principal = Depends(require("purchasing.manage")), db: Session = Depends(get_db)):
    _ret_checked(db, p, return_id)
    r = commit_or_http(db, lambda: supplier_return_service.dispatch(db, actor=p.user, return_id=return_id, ip=client_ip(request)))
    return _return_out(db, r, p.has("inventory.view_valuation"))


@router.post("/supplier-returns/{return_id}/cancel")
def cancel_return(return_id: int, body: S.ReasonIn, request: Request, p: Principal = Depends(require("purchasing.manage")),
                  db: Session = Depends(get_db)):
    _ret_checked(db, p, return_id)
    r = commit_or_http(db, lambda: supplier_return_service.cancel(db, actor=p.user, return_id=return_id, reason=body.reason,
                                                                   ip=client_ip(request)))
    return _return_out(db, r, p.has("inventory.view_valuation"))


# ---- payables, GRNI, settings, integrity ----------------------------------------------------------------
@router.get("/purchasing/payables")
def payables(p: Principal = Depends(require("suppliers.view")), db: Session = Depends(get_db)):
    return pq.payables(db)


@router.get("/purchasing/grni")
def grni(p: Principal = Depends(require("suppliers.view", "purchasing.view")), db: Session = Depends(get_db)):
    return pq.goods_received_not_invoiced(db, visible_location_ids(p.user))


@router.get("/purchasing/integrity")
def purchasing_integrity(p: Principal = Depends(require("settings.manage")), db: Session = Depends(get_db)):
    problems = pq.integrity_check(db)
    return {"consistent": not problems, "problems": problems[:200]}


@router.get("/purchasing/settings")
def get_purchasing_settings(p: Principal = Depends(require_any("purchasing.view", "suppliers.view")), db: Session = Depends(get_db)):
    return settings_service.purchasing_settings(db)


@router.put("/purchasing/settings")
def set_purchasing_settings(body: S.PurchasingSettingsIn, request: Request, p: Principal = Depends(require("settings.manage")),
                            db: Session = Depends(get_db)):
    vals = body.model_dump(exclude={"reason"})
    return commit_or_http(db, lambda: settings_service.set_purchasing_settings(db, actor=p.user, values=vals, reason=body.reason,
                                                                                ip=client_ip(request)))
