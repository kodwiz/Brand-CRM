"""Customers, pricing, orders, shipments, customer payments and returns.

Visibility: order bookers reach only their own orders and the customers they created or sold to; managers and accounts see
everything in their location scope; stock keepers see the orders to be fulfilled at their locations with prices and payments
removed. Out-of-scope records answer 404."""
from __future__ import annotations

from datetime import date

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy import and_, func, or_, select, true
from sqlalchemy.orm import Session

from backend.api.common import commit_or_http
from backend.db import get_db
from backend.deps import Principal, client_ip, ensure_location_access, require, require_any
from backend.models import (
    Customer, Order, OrderPayment, ProductVariant, Promotion, SalesChannel, SalesReturn, Shipment, User,
)
from backend.schemas import sales as S
from backend.services import (
    customer_service, pricing_service, sales_return_service as rs, sales_service as ss, settings_service,
)
from backend.services.errors import BusinessRuleError, NotFoundError
from backend.services.scope import can_access_location, visible_location_ids

router = APIRouter(tags=["sales"])


def _nf(fn):
    try:
        return fn()
    except NotFoundError as e:
        raise HTTPException(404, str(e))


def _names(db: Session, ids: set) -> dict[int, str]:
    ids = {i for i in ids if i}
    return {u.id: u.username for u in db.scalars(select(User).where(User.id.in_(ids))).all()} if ids else {}


def _page(q, db: Session, page: int, page_size: int, order):
    total = db.scalar(select(func.count()).select_from(q.subquery())) or 0
    return db.scalars(q.order_by(order).offset((page - 1) * page_size).limit(page_size)).all(), total


# ---- sales channels and settings ---------------------------------------------------------------------------------
@router.get("/sales/channels")
def channels(all: bool = False, p: Principal = Depends(require_any("orders.create", "orders.view_all", "orders.view_own", "customers.view", "settings.manage")),
             db: Session = Depends(get_db)):
    q = select(SalesChannel).order_by(SalesChannel.sort_order, SalesChannel.id)
    if not (all and p.has("settings.manage")):
        q = q.where(SalesChannel.is_active.is_(True))
    return [{"id": c.id, "code": c.code, "name": c.name, "is_active": c.is_active} for c in db.scalars(q).all()]


@router.post("/sales/channels", status_code=201)
def create_channel(body: S.ChannelIn, p: Principal = Depends(require("settings.manage")), db: Session = Depends(get_db)):
    def go():
        if db.scalar(select(SalesChannel).where(SalesChannel.code == body.code)):
            raise BusinessRuleError(f"A channel with code {body.code} already exists.")
        c = SalesChannel(code=body.code, name=body.name, is_active=body.is_active, sort_order=100)
        db.add(c)
        db.flush()
        return {"id": c.id, "code": c.code, "name": c.name, "is_active": c.is_active}
    return commit_or_http(db, go)


@router.put("/sales/channels/{channel_id}")
def update_channel(channel_id: int, body: S.ChannelUpdate, p: Principal = Depends(require("settings.manage")), db: Session = Depends(get_db)):
    def go():
        c = db.get(SalesChannel, channel_id)
        if c is None:
            raise NotFoundError("Channel not found")
        if body.name is not None:
            c.name = body.name
        if body.is_active is not None:
            c.is_active = body.is_active
        db.flush()
        return {"id": c.id, "code": c.code, "name": c.name, "is_active": c.is_active}
    return commit_or_http(db, go)


@router.get("/sales/settings")
def get_sales_settings(p: Principal = Depends(require_any("orders.create", "orders.view_all", "settings.manage", "pricing.manage")),
                       db: Session = Depends(get_db)):
    return settings_service.sales_settings(db)


@router.put("/sales/settings")
def put_sales_settings(body: S.SalesSettingsIn, request: Request, p: Principal = Depends(require("settings.manage")), db: Session = Depends(get_db)):
    return commit_or_http(db, lambda: settings_service.set_sales_settings(
        db, actor=p.user, values=body.model_dump(), reason=None, ip=client_ip(request)))


# ---- customers ---------------------------------------------------------------------------------------------------
def _customer_out(db: Session, c: Customer, p: Principal, detail: bool = False) -> dict:
    d = {"id": c.id, "code": c.code, "name": c.name, "phone": c.phone, "email": c.email, "category": c.category, "status": c.status,
         "created_at": c.created_at}
    if detail:
        d["notes"] = c.notes
        d["addresses"] = [{"id": a.id, "kind": a.kind, "label": a.label, "line": a.line, "city": a.city, "is_default": a.is_default}
                          for a in c.addresses]
        d["created_by"] = _names(db, {c.created_by}).get(c.created_by)
        if p.has("orders.view_all") or p.has("orders.payments"):
            d["store_credit"] = str(customer_service.credit_balance(db, c.id))
    return d


def _can_all(p: Principal) -> bool:
    return p.has("customers.view_all")


@router.get("/customers")
def list_customers(q: str | None = None, status: str | None = None, page: int = Query(1, ge=1), page_size: int = Query(50, ge=1, le=200),
                   p: Principal = Depends(require("customers.view")), db: Session = Depends(get_db)):
    stmt = select(Customer)
    cond = customer_service.visible_filter(p.user, _can_all(p))
    if cond is not None:
        stmt = stmt.where(cond)
    if status:
        stmt = stmt.where(Customer.status == status)
    if q:
        like = f"%{q.lower()}%"
        digits = "".join(ch for ch in q if ch.isdigit())
        conds = [func.lower(Customer.name).like(like), func.lower(Customer.code).like(like), func.lower(func.coalesce(Customer.email, "")).like(like)]
        if digits:
            conds.append(Customer.phone_norm.like(f"%{digits}%"))
        stmt = stmt.where(or_(*conds))
    rows, total = _page(stmt, db, page, page_size, func.lower(Customer.name))
    return {"items": [_customer_out(db, c, p) for c in rows], "total": total, "page": page, "page_size": page_size}


@router.post("/customers", status_code=201)
def create_customer(body: S.CustomerIn, request: Request, p: Principal = Depends(require("customers.manage")), db: Session = Depends(get_db)):
    def go():
        c = customer_service.create_customer(db, actor=p.user, name=body.name, phone=body.phone, email=body.email, category=body.category,
                                             notes=body.notes, addresses=[a.model_dump() for a in body.addresses], ip=client_ip(request))
        return _customer_out(db, c, p, True)
    return commit_or_http(db, go)


@router.get("/customers/{customer_id}")
def get_customer(customer_id: int, p: Principal = Depends(require("customers.view")), db: Session = Depends(get_db)):
    c = _nf(lambda: customer_service.check_visible(db, p.user, customer_id, _can_all(p)))
    return _customer_out(db, c, p, True)


@router.put("/customers/{customer_id}")
def update_customer(customer_id: int, body: S.CustomerUpdate, request: Request, p: Principal = Depends(require("customers.manage")),
                    db: Session = Depends(get_db)):
    def go():
        c = customer_service.check_visible(db, p.user, customer_id, _can_all(p))
        vals = body.model_dump(exclude_unset=True)
        if "addresses" in vals and vals["addresses"] is not None:
            vals["addresses"] = [a.model_dump() if hasattr(a, "model_dump") else a for a in body.addresses]
        customer_service.update_customer(db, actor=p.user, customer=c, values=vals, ip=client_ip(request))
        return _customer_out(db, c, p, True)
    return commit_or_http(db, go)


@router.get("/customers/{customer_id}/orders")
def customer_orders(customer_id: int, p: Principal = Depends(require("customers.view")), db: Session = Depends(get_db)):
    c = _nf(lambda: customer_service.check_visible(db, p.user, customer_id, _can_all(p)))
    stmt = select(Order).where(Order.customer_id == c.id).where(_order_scope(p, include_fulfil=False))
    rows = db.scalars(stmt.order_by(Order.id.desc()).limit(200)).all()
    return {"items": [_order_row(db, o, p) for o in rows]}


# ---- promotions ----------------------------------------------------------------------------------------------------
def _promo_out(pr: Promotion) -> dict:
    return {"id": pr.id, "name": pr.name, "kind": pr.kind, "value": str(pr.value), "starts_on": pr.starts_on, "ends_on": pr.ends_on,
            "variant_id": pr.variant_id, "product_id": pr.product_id, "category_id": pr.category_id,
            "customer_category": pr.customer_category, "min_qty": pr.min_qty,
            "min_order_value": str(pr.min_order_value) if pr.min_order_value is not None else None,
            "max_discount_pct": str(pr.max_discount_pct) if pr.max_discount_pct is not None else None,
            "stackable": pr.stackable, "is_active": pr.is_active}


@router.get("/promotions")
def list_promotions(p: Principal = Depends(require_any("pricing.manage", "orders.create", "orders.view_all")), db: Session = Depends(get_db)):
    return [_promo_out(x) for x in db.scalars(select(Promotion).order_by(Promotion.id.desc())).all()]


@router.post("/promotions", status_code=201)
def create_promotion(body: S.PromotionIn, request: Request, p: Principal = Depends(require("pricing.manage")), db: Session = Depends(get_db)):
    return commit_or_http(db, lambda: _promo_out(pricing_service.create_promotion(db, actor=p.user, values=body.model_dump(), ip=client_ip(request))))


@router.put("/promotions/{promotion_id}")
def update_promotion(promotion_id: int, body: S.PromotionUpdate, request: Request, p: Principal = Depends(require("pricing.manage")),
                     db: Session = Depends(get_db)):
    return commit_or_http(db, lambda: _promo_out(pricing_service.update_promotion(
        db, actor=p.user, promotion_id=promotion_id, values=body.model_dump(exclude_unset=True), ip=client_ip(request))))


# ---- order visibility ----------------------------------------------------------------------------------------------
def _order_scope(p: Principal, include_fulfil: bool = True):
    """SQL condition: the orders this user may see at all."""
    ids = visible_location_ids(p.user)
    parts = []
    if p.has("orders.view_all"):
        parts.append(true() if ids is None else or_(Order.location_id.in_(ids), Order.fulfilment_location_id.in_(ids)))
    if p.has("orders.view_own"):
        parts.append(Order.created_by == p.user.id)
    if include_fulfil and p.has("orders.fulfil"):
        parts.append(and_(Order.status != "draft", true() if ids is None else Order.fulfilment_location_id.in_(ids)))
    return or_(*parts) if parts else Order.id == -1


def _level(p: Principal, o: Order) -> str | None:
    """'full' (prices, payments, customer), 'fulfil' (goods and delivery only) or None (not allowed)."""
    ids = visible_location_ids(p.user)
    in_scope = ids is None or o.location_id in ids or o.fulfilment_location_id in ids
    if (p.has("orders.view_all") and in_scope) or (p.has("orders.view_own") and o.created_by == p.user.id):
        return "full"
    if p.has("orders.fulfil") and o.status != "draft" and (ids is None or o.fulfilment_location_id in ids):
        return "fulfil"
    return None


def _order_checked(db: Session, p: Principal, order_id: int, need: str = "any") -> tuple[Order, str]:
    o = db.get(Order, order_id)
    lvl = _level(p, o) if o else None
    if lvl is None or (need == "full" and lvl != "full"):
        raise HTTPException(404, "Order not found")
    return o, lvl


def _is_owner_or_manager(p: Principal, o: Order) -> bool:
    return o.created_by == p.user.id or (p.has("orders.view_all") and p.has("orders.create"))


def _order_row(db: Session, o: Order, p: Principal) -> dict:
    cust = db.get(Customer, o.customer_id)
    ch = db.get(SalesChannel, o.channel_id)
    m = ss.money_state(db, o)
    return {"id": o.id, "ref": o.ref, "customer": {"id": cust.id, "code": cust.code, "name": cust.name}, "channel": ch.name,
            "status": o.status, "payment_status": m["payment_status"], "payment_terms": o.payment_terms, "order_date": o.order_date,
            "total": str(o.total), "balance_due": str(m["balance_due"]), "location_id": o.location_id,
            "fulfilment_location_id": o.fulfilment_location_id, "discount_approval": o.discount_approval,
            "created_by": _names(db, {o.created_by}).get(o.created_by), "tracking_no": o.tracking_no,
            "units": sum(l.qty for l in o.lines)}


def _shipment_out(db: Session, s: Shipment, show_cost: bool, names: dict) -> dict:
    vm = {v.id: v for v in db.scalars(select(ProductVariant).where(ProductVariant.id.in_([l.variant_id for l in s.lines]))).all()}
    lines = []
    for l in s.lines:
        d = {"id": l.id, "order_line_id": l.order_line_id, "variant_id": l.variant_id, "sku": vm[l.variant_id].sku,
             "variant_name": vm[l.variant_id].name, "batch_id": l.batch_id, "qty": l.qty}
        if show_cost:
            d["stock_value"] = str(l.stock_value) if l.stock_value is not None else None
        lines.append(d)
    return {"id": s.id, "ref": s.ref, "order_id": s.order_id, "location_id": s.location_id, "status": s.status, "courier": s.courier,
            "tracking_no": s.tracking_no, "attempts": s.attempts, "outcome": s.outcome, "outcome_reason": s.outcome_reason,
            "packed_by": names.get(s.packed_by), "packed_at": s.packed_at, "dispatched_by": names.get(s.dispatched_by),
            "dispatched_at": s.dispatched_at, "delivered_at": s.delivered_at, "received_back_at": s.received_back_at, "lines": lines}


def _payment_out(pay: OrderPayment, names: dict) -> dict:
    return {"id": pay.id, "ref": pay.ref, "kind": pay.kind, "method": pay.method, "amount": str(pay.amount), "paid_date": pay.paid_date,
            "reference": pay.reference, "status": pay.status, "created_by": names.get(pay.created_by),
            "reversed_by": names.get(pay.reversed_by), "reverse_reason": pay.reverse_reason, "sales_return_id": pay.sales_return_id}


def _order_out(db: Session, o: Order, p: Principal, level: str) -> dict:
    full = level == "full"
    vm = {v.id: v for v in db.scalars(select(ProductVariant).where(ProductVariant.id.in_([l.variant_id for l in o.lines]))).all()}
    q = ss.line_quantities(o)
    ret = ss.returned_qty(db, o.id)
    cust = db.get(Customer, o.customer_id)
    names = _names(db, {o.created_by, o.discount_approved_by} | {s.packed_by for s in o.shipments} | {s.dispatched_by for s in o.shipments}
                   | {e.actor_user_id for e in o.events})
    pay_names = {}
    lines = []
    for l in o.lines:
        d = {"id": l.id, "variant_id": l.variant_id, "sku": vm[l.variant_id].sku, "variant_name": vm[l.variant_id].name, "qty": l.qty,
             "packed": q[l.id]["packed"], "dispatched": q[l.id]["dispatched"], "delivered": q[l.id]["delivered"],
             "failed": q[l.id]["failed"], "to_ship": q[l.id]["to_ship"], "returned": ret.get(l.id, 0), "notes": l.notes}
        if full:
            d.update(list_price=str(l.list_price), promo_discount=str(l.promo_discount), unit_price=str(l.unit_price),
                     line_total=str(pricing_service.q2(Decimal_(l.unit_price) * l.qty)), promotions=l.promotion_names)
        lines.append(d)
    out = {"id": o.id, "ref": o.ref, "status": o.status, "order_date": o.order_date, "location_id": o.location_id,
           "fulfilment_location_id": o.fulfilment_location_id, "channel": db.get(SalesChannel, o.channel_id).name, "channel_id": o.channel_id,
           "payment_terms": o.payment_terms, "delivery": {"name": o.delivery_name, "phone": o.delivery_phone, "address": o.delivery_address,
                                                           "city": o.delivery_city, "courier": o.courier, "tracking_no": o.tracking_no},
           "created_by": names.get(o.created_by), "confirmed_at": o.confirmed_at, "cancel_reason": o.cancel_reason, "lines": lines,
           "shipments": [_shipment_out(db, s, p.has("products.view_cost") and full, names) for s in o.shipments],
           "events": [{"event": e.event, "by": e.actor_username, "note": e.note, "at": e.created_at} for e in o.events]}
    if full:
        m = ss.money_state(db, o)
        out.update(customer={"id": cust.id, "code": cust.code, "name": cust.name, "phone": cust.phone, "category": cust.category},
                   customer_id=o.customer_id, billing_address=o.billing_address, notes=o.notes, delivery_fee=str(o.delivery_fee),
                   subtotal_list=str(o.subtotal_list), discount_total=str(o.discount_total), tax_rate_pct=str(o.tax_rate_pct),
                   tax_total=str(o.tax_total), total=str(o.total), manual_discount_pct=str(o.manual_discount_pct),
                   discount_approval=o.discount_approval, discount_approved_by=names.get(o.discount_approved_by),
                   discount_approval_note=o.discount_approval_note, money=_money_out(m), payment_status=m["payment_status"])
        if p.has("orders.payments") or p.has("orders.refund") or p.has("orders.view_all") or o.created_by == p.user.id:
            pays = db.scalars(select(OrderPayment).where(OrderPayment.order_id == o.id).order_by(OrderPayment.id)).all()
            pn = _names(db, {x.created_by for x in pays} | {x.reversed_by for x in pays})
            out["payments"] = [_payment_out(x, pn) for x in pays]
        rets = db.scalars(select(SalesReturn).where(SalesReturn.order_id == o.id).order_by(SalesReturn.id)).all()
        out["returns"] = [{"id": r.id, "ref": r.ref, "status": r.status, "reason": r.reason,
                           "return_value": str(r.return_value) if r.return_value is not None else None} for r in rets]
    else:
        out["customer"] = {"name": cust.name}
    return out


def Decimal_(x):
    from decimal import Decimal
    return Decimal(str(x))


def _money_out(m: dict) -> dict:
    return {k: (str(v) if k != "payment_status" else v) for k, v in m.items()}


# ---- orders ------------------------------------------------------------------------------------------------------------
@router.post("/orders/quote")
def quote(body: S.QuoteIn, p: Principal = Depends(require("orders.create")), db: Session = Depends(get_db)):
    def go():
        c = customer_service.check_visible(db, p.user, body.customer_id, _can_all(p))
        on = body.order_date or date.today()
        qs = pricing_service.quote(db, customer=c, lines=[{"variant_id": l.variant_id, "qty": l.qty} for l in body.lines], on=on)
        limit = settings_service.sales_value(db, settings_service.DISCOUNT_LIMIT_KEY)
        rate = settings_service.sales_value(db, settings_service.TAX_RATE_KEY)
        net = gross = pricing_service.q2(0)
        rows, worst = [], pricing_service.q2(0)
        for l, q in zip(body.lines, qs):
            base = q["list_price"] - q["promo_discount"]
            agreed = pricing_service.q2(l.unit_price) if l.unit_price is not None else base
            if agreed > q["list_price"]:
                raise BusinessRuleError(f"{q['variant'].sku}: the agreed price is above the list price.")
            manual = max(base - agreed, 0)
            pct = pricing_service.q2(manual / base * 100) if base > 0 and manual > 0 else pricing_service.q2(0)
            worst = max(worst, pct)
            gross += q["list_price"] * l.qty
            net += agreed * l.qty
            rows.append({"variant_id": l.variant_id, "sku": q["variant"].sku, "qty": l.qty, "list_price": str(q["list_price"]),
                         "promo_discount": str(q["promo_discount"]), "unit_price": str(agreed),
                         "promotions": [x.name for x in q["promotions"]], "manual_discount_pct": str(pct),
                         "line_total": str(pricing_service.q2(agreed * l.qty))})
        tax = pricing_service.q2(net * rate / 100)
        return {"lines": rows, "subtotal_list": str(gross), "discount_total": str(gross - net), "tax_total": str(tax),
                "delivery_fee": str(body.delivery_fee), "total": str(net + tax + body.delivery_fee),
                "approval_required": worst > limit, "manual_discount_limit_pct": str(limit)}
    return commit_or_http(db, go)


@router.get("/orders")
def list_orders(status: str | None = None, customer_id: int | None = None, channel_id: int | None = None, location_id: int | None = None,
                created_by: int | None = None, date_from: date | None = None, date_to: date | None = None, q: str | None = None,
                page: int = Query(1, ge=1), page_size: int = Query(50, ge=1, le=200),
                p: Principal = Depends(require_any("orders.view_all", "orders.view_own", "orders.fulfil")), db: Session = Depends(get_db)):
    if location_id is not None:
        ensure_location_access(p, location_id)
    stmt = select(Order).where(_order_scope(p))
    if status:
        stmt = stmt.where(Order.status == status)
    if customer_id:
        stmt = stmt.where(Order.customer_id == customer_id)
    if channel_id:
        stmt = stmt.where(Order.channel_id == channel_id)
    if location_id:
        stmt = stmt.where(or_(Order.location_id == location_id, Order.fulfilment_location_id == location_id))
    if created_by:
        stmt = stmt.where(Order.created_by == created_by)
    if date_from:
        stmt = stmt.where(Order.order_date >= date_from)
    if date_to:
        stmt = stmt.where(Order.order_date <= date_to)
    if q:
        like = f"%{q.lower()}%"
        stmt = stmt.where(or_(func.lower(Order.ref).like(like), func.lower(func.coalesce(Order.tracking_no, "")).like(like),
                              Order.customer_id.in_(select(Customer.id).where(or_(func.lower(Customer.name).like(like),
                                                                                  Customer.phone_norm.like(f"%{''.join(c for c in q if c.isdigit()) or 'x'}%"))))))
    rows, total = _page(stmt, db, page, page_size, Order.id.desc())
    items = []
    for o in rows:
        r = _order_row(db, o, p)
        if _level(p, o) != "full":
            for k in ("total", "balance_due", "payment_status", "discount_approval"):
                r.pop(k, None)
            r["customer"] = {"name": r["customer"]["name"]}
        items.append(r)
    return {"items": items, "total": total, "page": page, "page_size": page_size}


@router.get("/orders/my-summary")
def my_summary(date_from: date | None = None, date_to: date | None = None,
               p: Principal = Depends(require_any("orders.view_own", "orders.view_all")), db: Session = Depends(get_db)):
    """Counts and values of the caller's own orders by status, for the booker's personal performance view."""
    stmt = select(Order.status, func.count(), func.coalesce(func.sum(Order.total), 0)).where(Order.created_by == p.user.id)
    if date_from:
        stmt = stmt.where(Order.order_date >= date_from)
    if date_to:
        stmt = stmt.where(Order.order_date <= date_to)
    rows = db.execute(stmt.group_by(Order.status)).all()
    return {"by_status": [{"status": r[0], "orders": r[1], "value": str(r[2])} for r in rows],
            "orders": sum(r[1] for r in rows), "value_excluding_cancelled": str(sum(r[2] for r in rows if r[0] not in ("cancelled", "draft")))}


@router.post("/orders", status_code=201)
def create_order(body: S.OrderIn, request: Request, p: Principal = Depends(require("orders.create")), db: Session = Depends(get_db)):
    ensure_location_access(p, body.location_id)

    def go():
        customer_service.check_visible(db, p.user, body.customer_id, _can_all(p))
        o = ss.create_order(db, actor=p.user, customer_id=body.customer_id, channel_id=body.channel_id, location_id=body.location_id,
                            fulfilment_location_id=body.fulfilment_location_id, order_date=body.order_date or date.today(),
                            payment_terms=body.payment_terms, delivery=body.delivery.model_dump(), delivery_fee=body.delivery_fee,
                            notes=body.notes, lines=[l.model_dump() for l in body.lines], idempotency_key=body.idempotency_key,
                            ip=client_ip(request))
        if body.confirm_now and o.status == "draft":
            ss.confirm(db, actor=p.user, order_id=o.id, ip=client_ip(request))
        return _order_out(db, o, p, "full")
    return commit_or_http(db, go)


@router.get("/orders/{order_id}")
def get_order(order_id: int, p: Principal = Depends(require_any("orders.view_all", "orders.view_own", "orders.fulfil")), db: Session = Depends(get_db)):
    o, lvl = _order_checked(db, p, order_id)
    return _order_out(db, o, p, lvl)


def _owned_draft(db: Session, p: Principal, order_id: int) -> Order:
    o, lvl = _order_checked(db, p, order_id, "full")
    if not _is_owner_or_manager(p, o):
        raise HTTPException(404, "Order not found")
    return o


@router.put("/orders/{order_id}")
def update_order(order_id: int, body: S.OrderUpdate, request: Request, p: Principal = Depends(require("orders.create")), db: Session = Depends(get_db)):
    _owned_draft(db, p, order_id)
    if body.location_id is not None:
        ensure_location_access(p, body.location_id)

    def go():
        vals = body.model_dump(exclude_unset=True, exclude={"lines"})
        if body.delivery is not None:
            vals["delivery"] = body.delivery.model_dump(exclude_unset=True)
        o = ss.update_draft(db, actor=p.user, order_id=order_id, values=vals,
                            lines=[l.model_dump() for l in body.lines] if body.lines is not None else None, ip=client_ip(request))
        return _order_out(db, o, p, "full")
    return commit_or_http(db, go)


@router.post("/orders/{order_id}/confirm")
def confirm_order(order_id: int, request: Request, p: Principal = Depends(require("orders.create")), db: Session = Depends(get_db)):
    _owned_draft(db, p, order_id)
    return commit_or_http(db, lambda: _order_out(db, ss.confirm(db, actor=p.user, order_id=order_id, ip=client_ip(request)), p, "full"))


@router.post("/orders/{order_id}/discount-decision")
def discount_decision(order_id: int, body: S.DecisionIn, request: Request, p: Principal = Depends(require("discounts.approve")),
                      db: Session = Depends(get_db)):
    _order_checked(db, p, order_id, "full")
    return commit_or_http(db, lambda: _order_out(db, ss.decide_discount(db, actor=p.user, order_id=order_id, approve=body.approve,
                                                                         note=body.note, ip=client_ip(request)), p, "full"))


@router.post("/orders/{order_id}/cancel")
def cancel_order(order_id: int, body: S.ReasonIn, request: Request, p: Principal = Depends(require("orders.update_status")),
                 db: Session = Depends(get_db)):
    o, _ = _order_checked(db, p, order_id, "full")
    if not _is_owner_or_manager(p, o) and not p.has("orders.view_all"):
        raise HTTPException(404, "Order not found")
    return commit_or_http(db, lambda: _order_out(db, ss.cancel(db, actor=p.user, order_id=order_id, reason=body.reason, ip=client_ip(request)), p, "full"))


@router.post("/orders/{order_id}/reopen")
def reopen_order(order_id: int, body: S.ReasonIn, request: Request, p: Principal = Depends(require("orders.view_all", "orders.update_status")),
                 db: Session = Depends(get_db)):
    _order_checked(db, p, order_id, "full")
    return commit_or_http(db, lambda: _order_out(db, ss.reopen(db, actor=p.user, order_id=order_id, reason=body.reason, ip=client_ip(request)), p, "full"))


@router.post("/orders/{order_id}/reserve-remaining")
def reserve_remaining(order_id: int, request: Request, p: Principal = Depends(require("orders.update_status")), db: Session = Depends(get_db)):
    o, lvl = _order_checked(db, p, order_id)
    if lvl != "full" and not p.has("orders.fulfil"):
        raise HTTPException(404, "Order not found")
    return commit_or_http(db, lambda: _order_out(db, ss.reserve_remaining(db, actor=p.user, order_id=order_id, ip=client_ip(request)), p, lvl))


# ---- shipments -----------------------------------------------------------------------------------------------------------
@router.post("/orders/{order_id}/shipments", status_code=201)
def pack_order(order_id: int, body: S.PackIn, request: Request, p: Principal = Depends(require("orders.fulfil")), db: Session = Depends(get_db)):
    o, lvl = _order_checked(db, p, order_id)
    ensure_location_access(p, o.fulfilment_location_id)

    def go():
        s = ss.pack(db, actor=p.user, order_id=order_id, lines=[l.model_dump() for l in body.lines], notes=body.notes, ip=client_ip(request))
        return _shipment_out(db, s, False, _names(db, {s.packed_by}))
    return commit_or_http(db, go)


def _ship_checked(db: Session, p: Principal, shipment_id: int) -> Shipment:
    s = db.get(Shipment, shipment_id)
    if s is None:
        raise HTTPException(404, "Shipment not found")
    ensure_location_access(p, s.location_id)
    return s


@router.get("/shipments")
def list_shipments(status: str | None = None, location_id: int | None = None, page: int = Query(1, ge=1), page_size: int = Query(50, ge=1, le=200),
                   p: Principal = Depends(require("orders.fulfil")), db: Session = Depends(get_db)):
    """The fulfilment queue: parcels at the user's locations, with the order's delivery details but no prices."""
    if location_id is not None:
        ensure_location_access(p, location_id)
    stmt = select(Shipment)
    ids = visible_location_ids(p.user)
    if ids is not None:
        stmt = stmt.where(Shipment.location_id.in_(ids))
    if location_id:
        stmt = stmt.where(Shipment.location_id == location_id)
    if status:
        stmt = stmt.where(Shipment.status == status)
    rows, total = _page(stmt, db, page, page_size, Shipment.id.desc())
    items = []
    for s in rows:
        o = db.get(Order, s.order_id)
        d = _shipment_out(db, s, False, _names(db, {s.packed_by, s.dispatched_by}))
        d.update(order_ref=o.ref, order_status=o.status, delivery={"name": o.delivery_name, "phone": o.delivery_phone,
                                                                    "address": o.delivery_address, "city": o.delivery_city},
                 payment_terms=o.payment_terms)
        items.append(d)
    return {"items": items, "total": total, "page": page, "page_size": page_size}


def _ship_out(db, p, s):
    return _shipment_out(db, s, p.has("products.view_cost"), _names(db, {s.packed_by, s.dispatched_by}))


@router.post("/shipments/{shipment_id}/dispatch")
def dispatch(shipment_id: int, body: S.DispatchIn, request: Request, p: Principal = Depends(require("orders.fulfil")), db: Session = Depends(get_db)):
    _ship_checked(db, p, shipment_id)
    return commit_or_http(db, lambda: _ship_out(db, p, ss.dispatch(db, actor=p.user, shipment_id=shipment_id, courier=body.courier,
                                                                    tracking_no=body.tracking_no, ip=client_ip(request))))


@router.post("/shipments/{shipment_id}/unpack")
def unpack(shipment_id: int, request: Request, p: Principal = Depends(require("orders.fulfil")), db: Session = Depends(get_db)):
    _ship_checked(db, p, shipment_id)
    return commit_or_http(db, lambda: _ship_out(db, p, ss.cancel_packed(db, actor=p.user, shipment_id=shipment_id, ip=client_ip(request))))


@router.post("/shipments/{shipment_id}/outcome")
def shipment_outcome(shipment_id: int, body: S.OutcomeIn, request: Request,
                     p: Principal = Depends(require_any("orders.fulfil", "orders.update_status")), db: Session = Depends(get_db)):
    s = db.get(Shipment, shipment_id)
    if s is None:
        raise HTTPException(404, "Shipment not found")
    o = db.get(Order, s.order_id)
    lvl = _level(p, o)
    allowed = (p.has("orders.fulfil") and can_access_location(p.user, s.location_id)) or (p.has("orders.update_status") and lvl == "full")
    if not allowed:
        raise HTTPException(404, "Shipment not found")
    return commit_or_http(db, lambda: _ship_out(db, p, ss.outcome(db, actor=p.user, shipment_id=shipment_id, result=body.result,
                                                                   reason=body.reason, ip=client_ip(request))))


@router.post("/shipments/{shipment_id}/redispatch")
def redispatch(shipment_id: int, body: S.DispatchIn, request: Request, p: Principal = Depends(require("orders.fulfil")), db: Session = Depends(get_db)):
    _ship_checked(db, p, shipment_id)
    return commit_or_http(db, lambda: _ship_out(db, p, ss.redispatch(db, actor=p.user, shipment_id=shipment_id, tracking_no=body.tracking_no,
                                                                      ip=client_ip(request))))


@router.post("/shipments/{shipment_id}/receive-back")
def receive_back(shipment_id: int, request: Request, p: Principal = Depends(require("orders.fulfil", "returns.receive")), db: Session = Depends(get_db)):
    _ship_checked(db, p, shipment_id)
    return commit_or_http(db, lambda: _ship_out(db, p, ss.receive_back(db, actor=p.user, shipment_id=shipment_id, ip=client_ip(request))))


# ---- customer payments -----------------------------------------------------------------------------------------------------
@router.post("/orders/{order_id}/payments", status_code=201)
def record_payment(order_id: int, body: S.ReceiptIn, request: Request, p: Principal = Depends(require("orders.payments")), db: Session = Depends(get_db)):
    _order_checked(db, p, order_id, "full")

    def go():
        pay = ss.record_receipt(db, actor=p.user, order_id=order_id, amount=body.amount, method=body.method,
                                paid_date=body.paid_date or date.today(), reference=body.reference, notes=body.notes,
                                idempotency_key=body.idempotency_key, ip=client_ip(request))
        return _payment_out(pay, _names(db, {pay.created_by}))
    return commit_or_http(db, go)


@router.post("/order-payments/{payment_id}/reverse")
def reverse_order_payment(payment_id: int, body: S.ReasonIn, request: Request, p: Principal = Depends(require("orders.refund")),
                          db: Session = Depends(get_db)):
    pay = db.get(OrderPayment, payment_id)
    if pay is None:
        raise HTTPException(404, "Payment not found")
    _order_checked(db, p, pay.order_id, "full")

    def go():
        x = ss.reverse_payment(db, actor=p.user, payment_id=payment_id, reason=body.reason, ip=client_ip(request))
        return _payment_out(x, _names(db, {x.created_by, x.reversed_by}))
    return commit_or_http(db, go)


@router.post("/orders/{order_id}/refunds", status_code=201)
def refund_overpayment(order_id: int, body: S.RefundIn, request: Request, p: Principal = Depends(require("orders.refund")), db: Session = Depends(get_db)):
    _order_checked(db, p, order_id, "full")

    def go():
        x = ss.refund(db, actor=p.user, order_id=order_id, amount=body.amount, method=body.method, reference=body.reference,
                      reason=body.reason, ip=client_ip(request))
        return _payment_out(x, _names(db, {x.created_by}))
    return commit_or_http(db, go)


# ---- customer returns ----------------------------------------------------------------------------------------------------------
def _return_out(db: Session, r: SalesReturn, p: Principal) -> dict:
    vm = {v.id: v for v in db.scalars(select(ProductVariant).where(ProductVariant.id.in_([l.variant_id for l in r.lines]))).all()}
    names = _names(db, {r.requested_by, r.decided_by, r.received_by, r.inspected_by, r.settled_by})
    o = db.get(Order, r.order_id)
    cust = db.get(Customer, r.customer_id)
    full = _level(p, o) == "full"
    out = {"id": r.id, "ref": r.ref, "order_id": r.order_id, "order_ref": o.ref, "customer": {"id": cust.id, "name": cust.name},
           "location_id": r.location_id, "status": r.status, "reason": r.reason, "refund_mode": r.refund_mode,
           "refund_delivery_fee": r.refund_delivery_fee, "requested_by": names.get(r.requested_by), "requested_at": r.requested_at,
           "decided_by": names.get(r.decided_by), "decision_note": r.decision_note, "received_by": names.get(r.received_by),
           "inspected_by": names.get(r.inspected_by), "settled_by": names.get(r.settled_by),
           "lines": [{"id": l.id, "order_line_id": l.order_line_id, "variant_id": l.variant_id, "sku": vm[l.variant_id].sku,
                      "variant_name": vm[l.variant_id].name, "batch_id": l.batch_id, "qty_requested": l.qty_requested,
                      "qty_received": l.qty_received, "qty_restock": l.qty_restock, "qty_damaged": l.qty_damaged,
                      "qty_expired": l.qty_expired, "refund_qty": l.refund_qty, "inspection_note": l.inspection_note}
                     for l in r.lines]}
    if full:
        out["return_value"] = str(r.return_value) if r.return_value is not None else None
        out["refund_paid"] = str(r.refund_paid) if r.refund_paid is not None else None
    return out


def _return_checked(db: Session, p: Principal, return_id: int, mode: str) -> SalesReturn:
    r = db.get(SalesReturn, return_id)
    if r is None:
        raise HTTPException(404, "Return not found")
    o = db.get(Order, r.order_id)
    if mode == "order":                     # needs access to the order itself
        if _level(p, o) != "full":
            raise HTTPException(404, "Return not found")
    elif mode == "location":                # warehouse work
        ensure_location_access(p, r.location_id)
    else:
        if _level(p, o) != "full" and not (p.has("returns.receive") and can_access_location(p.user, r.location_id)):
            raise HTTPException(404, "Return not found")
    return r


@router.get("/sales-returns")
def list_returns(status: str | None = None, order_id: int | None = None, page: int = Query(1, ge=1), page_size: int = Query(50, ge=1, le=200),
                 p: Principal = Depends(require_any("orders.view_all", "orders.view_own", "returns.receive")), db: Session = Depends(get_db)):
    ids = visible_location_ids(p.user)
    parts = []
    if p.has("orders.view_all") or p.has("orders.view_own"):
        parts.append(SalesReturn.order_id.in_(select(Order.id).where(_order_scope(p, include_fulfil=False))))
    if p.has("returns.receive"):
        parts.append(true() if ids is None else SalesReturn.location_id.in_(ids))
    stmt = select(SalesReturn).where(or_(*parts))
    if status:
        stmt = stmt.where(SalesReturn.status == status)
    if order_id:
        stmt = stmt.where(SalesReturn.order_id == order_id)
    rows, total = _page(stmt, db, page, page_size, SalesReturn.id.desc())
    return {"items": [_return_out(db, r, p) for r in rows], "total": total, "page": page, "page_size": page_size}


@router.get("/sales-returns/{return_id}")
def get_return(return_id: int, p: Principal = Depends(require_any("orders.view_all", "orders.view_own", "returns.receive")), db: Session = Depends(get_db)):
    return _return_out(db, _return_checked(db, p, return_id, "any"), p)


@router.post("/orders/{order_id}/returns", status_code=201)
def request_return(order_id: int, body: S.ReturnIn, request: Request,
                   p: Principal = Depends(require_any("orders.update_status", "orders.returns")), db: Session = Depends(get_db)):
    _order_checked(db, p, order_id, "full")
    return commit_or_http(db, lambda: _return_out(db, rs.request_return(
        db, actor=p.user, order_id=order_id, lines=[l.model_dump() for l in body.lines], reason=body.reason, refund_mode=body.refund_mode,
        refund_delivery_fee=body.refund_delivery_fee, ip=client_ip(request)), p))


@router.post("/sales-returns/{return_id}/decide")
def decide_return(return_id: int, body: S.ReturnDecisionIn, request: Request, p: Principal = Depends(require("orders.returns")),
                  db: Session = Depends(get_db)):
    _return_checked(db, p, return_id, "order")
    return commit_or_http(db, lambda: _return_out(db, rs.decide(db, actor=p.user, return_id=return_id, approve=body.approve, note=body.note,
                                                                 ip=client_ip(request)), p))


@router.post("/sales-returns/{return_id}/cancel")
def cancel_return(return_id: int, body: S.ReasonIn, request: Request, p: Principal = Depends(require_any("orders.update_status", "orders.returns")),
                  db: Session = Depends(get_db)):
    r = _return_checked(db, p, return_id, "order")
    if r.requested_by != p.user.id and not p.has("orders.returns"):
        raise HTTPException(404, "Return not found")
    return commit_or_http(db, lambda: _return_out(db, rs.cancel(db, actor=p.user, return_id=return_id, reason=body.reason, ip=client_ip(request)), p))


@router.post("/sales-returns/{return_id}/receive")
def receive_return(return_id: int, body: S.ReceiveReturnIn, request: Request, p: Principal = Depends(require("returns.receive")),
                   db: Session = Depends(get_db)):
    _return_checked(db, p, return_id, "location")
    return commit_or_http(db, lambda: _return_out(db, rs.receive(db, actor=p.user, return_id=return_id,
                                                                 lines=[l.model_dump() for l in body.lines], ip=client_ip(request)), p))


@router.post("/sales-returns/{return_id}/inspect")
def inspect_return(return_id: int, body: S.InspectIn, request: Request, p: Principal = Depends(require("returns.receive")),
                   db: Session = Depends(get_db)):
    _return_checked(db, p, return_id, "location")
    return commit_or_http(db, lambda: _return_out(db, rs.inspect(db, actor=p.user, return_id=return_id,
                                                                 lines=[l.model_dump() for l in body.lines], ip=client_ip(request)), p))


@router.post("/sales-returns/{return_id}/settle")
def settle_return(return_id: int, body: S.SettleIn, request: Request, p: Principal = Depends(require("orders.refund")), db: Session = Depends(get_db)):
    _return_checked(db, p, return_id, "order")
    return commit_or_http(db, lambda: _return_out(db, rs.settle(db, actor=p.user, return_id=return_id, refund_amount=body.refund_amount,
                                                                 method=body.method, reference=body.reference, ip=client_ip(request)), p))


# ---- integrity ------------------------------------------------------------------------------------------------------------------
@router.get("/sales/integrity")
def sales_integrity(p: Principal = Depends(require("settings.manage")), db: Session = Depends(get_db)):
    """Re-derive every order's totals and every customer's store credit and report differences."""
    problems: list[str] = []
    for o in db.scalars(select(Order).where(Order.status != "draft")).all():
        gross = sum((l.list_price * l.qty for l in o.lines), 0)
        net = sum((l.unit_price * l.qty for l in o.lines), 0)
        from decimal import Decimal
        expected = pricing_service.q2(Decimal(net) + pricing_service.q2(Decimal(net) * Decimal(o.tax_rate_pct) / 100) + Decimal(o.delivery_fee))
        if expected != Decimal(o.total):
            problems.append(f"{o.ref}: stored total {o.total} differs from lines {expected}")
        q = ss.line_quantities(o)
        for l in o.lines:
            if q[l.id]["committed"] > l.qty:
                problems.append(f"{o.ref}: more units shipped than ordered on line {l.id}")
        m = ss.money_state(db, o)
        if m["paid"] > m["net_payable"] and o.status not in ("cancelled", "refused", "delivery_failed"):
            pass  # overpayment is visible on the order and refundable; not an inconsistency
    for c in db.scalars(select(Customer)).all():
        if customer_service.credit_balance(db, c.id) < 0:
            problems.append(f"{c.code}: store credit balance is negative")
    return {"consistent": not problems, "problems": problems[:200]}
