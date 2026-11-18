"""Orders: drafting and pricing, discount approval, confirmation with stock reservation, shipments (partial deliveries),
delivery outcomes, customer receipts and refunds.

Rules enforced here (docs/orders.md):
* Order status is derived from shipments and returns (`refresh_status`), never typed in; a cancelled order cannot ship
  unless it is reopened (a controlled, audited step); payment status is derived from posted receipts and refunds.
* Confirming reserves stock atomically (all lines or none). Stock leaves the ledger when a parcel is dispatched, at the
  weighted-average cost of the moment, and that cost is stored on the shipment line.
* A manual discount above the configured limit needs approval by a different person with discounts.approve.
* Money is never edited: receipts and refunds are rows; reversal is a flag plus a reason.
"""
from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import ROUND_HALF_UP, Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from backend.models import (
    Customer, Order, OrderEvent, OrderLine, OrderPayment, ProductVariant, SalesChannel, SalesReturn, SalesReturnLine, Shipment, ShipmentLine,
    StockMovement, User,
)
from backend.models.location import Location
from backend.services import audit_service, customer_service, inventory_service, ledger, pricing_service, reservation_service, settings_service
from backend.services.errors import BusinessRuleError, ConflictError, InsufficientStockError, NotFoundError, SelfApprovalError
from backend.services.ledger import Spec

Q2 = Decimal("0.01")
COMMITTED = ("packed", "dispatched", "delivered", "failed", "refused")   # shipment states that hold the goods
ACTIVE_RETURN = ("requested", "approved", "received", "inspected", "settled")
SHIPPABLE = ("confirmed", "packed", "dispatched", "partially_delivered", "delivery_failed", "refused")


def q2(x) -> Decimal:
    return Decimal(str(x)).quantize(Q2, rounding=ROUND_HALF_UP)


def get_order(db: Session, order_id: int, *, lock: bool = False) -> Order:
    q = select(Order).where(Order.id == order_id)
    if lock:
        q = q.with_for_update().execution_options(populate_existing=True)
    o = db.scalar(q)
    if o is None:
        raise NotFoundError("Order not found")
    return o


def get_shipment(db: Session, shipment_id: int, *, lock: bool = False) -> Shipment:
    q = select(Shipment).where(Shipment.id == shipment_id)
    if lock:
        q = q.with_for_update().execution_options(populate_existing=True)
    s = db.scalar(q)
    if s is None:
        raise NotFoundError("Shipment not found")
    return s


def event(o: Order, actor: User | None, name: str, note: str | None = None) -> None:
    o.events.append(OrderEvent(event=name, actor_user_id=actor.id if actor else None,
                               actor_username=actor.username if actor else None, note=note))


def _audit(db: Session, actor: User, action: str, o: Order, ip: str | None, reason: str | None = None, **after) -> None:
    audit_service.record(db, actor=actor, action=action, entity_type="order", entity_id=o.id, location_id=o.fulfilment_location_id,
                         reason=reason, after={"ref": o.ref, "status": o.status, **after}, ip=ip)


# ---- quantities and money, always derived ---------------------------------------------------------------------
def line_quantities(o: Order) -> dict[int, dict]:
    """Per order line: ordered, committed (packed or with the courier or delivered), delivered, still to ship."""
    out = {l.id: {"ordered": l.qty, "packed": 0, "dispatched": 0, "delivered": 0, "failed": 0} for l in o.lines}
    for s in o.shipments:
        for sl in s.lines:
            d = out[sl.order_line_id]
            if s.status == "packed":
                d["packed"] += sl.qty
            elif s.status == "dispatched":
                d["dispatched"] += sl.qty
            elif s.status == "delivered":
                d["delivered"] += sl.qty
            elif s.status in ("failed", "refused"):
                d["failed"] += sl.qty
    for d in out.values():
        d["committed"] = d["packed"] + d["dispatched"] + d["delivered"] + d["failed"]
        d["to_ship"] = d["ordered"] - d["committed"]
    return out


def returned_qty(db: Session, order_id: int) -> dict[int, int]:
    rows = db.execute(select(SalesReturnLine.order_line_id, func.coalesce(func.sum(SalesReturnLine.qty_received), 0))
                      .join(SalesReturn, SalesReturn.id == SalesReturnLine.sales_return_id)
                      .where(SalesReturn.order_id == order_id, SalesReturn.status.in_(("received", "inspected", "settled")))
                      .group_by(SalesReturnLine.order_line_id)).all()
    return {r[0]: int(r[1]) for r in rows}


def totals(db: Session, o: Order, rate: Decimal | None = None) -> None:
    gross = sum((Decimal(l.list_price) * l.qty for l in o.lines), Decimal(0))
    net = sum((Decimal(l.unit_price) * l.qty for l in o.lines), Decimal(0))
    rate = Decimal(o.tax_rate_pct if rate is None else rate)
    o.subtotal_list = q2(gross)
    o.discount_total = q2(gross - net)
    o.tax_total = q2(net * rate / 100)
    o.total = q2(net + o.tax_total + Decimal(o.delivery_fee))


def money_state(db: Session, o: Order) -> dict:
    """Receipts, refunds, returns settled and the resulting position. The single source for payment status."""
    rows = db.execute(select(OrderPayment.kind, func.coalesce(func.sum(OrderPayment.amount), 0)).where(
        OrderPayment.order_id == o.id, OrderPayment.status == "posted").group_by(OrderPayment.kind)).all()
    sums = {k: Decimal(v) for k, v in rows}
    received, refunded = sums.get("receipt", Decimal(0)), sums.get("refund", Decimal(0))
    settled = Decimal(db.scalar(select(func.coalesce(func.sum(SalesReturn.return_value), 0)).where(
        SalesReturn.order_id == o.id, SalesReturn.status == "settled")))
    net_payable = Decimal(0) if o.status == "cancelled" else max(Decimal(o.total) - settled, Decimal(0))
    paid = received - refunded
    if o.status == "draft":
        status = "unpaid"
    elif net_payable == 0 and paid <= 0:
        status = "settled" if (received > 0 or o.status in ("cancelled", "returned")) else "unpaid"
    elif paid <= 0:
        status = "unpaid"
    elif paid < net_payable:
        status = "partially_paid"
    elif paid == net_payable:
        status = "paid"
    else:
        status = "overpaid"
    return {"received": q2(received), "refunded": q2(refunded), "returns_settled": q2(settled), "net_payable": q2(net_payable),
            "paid": q2(paid), "balance_due": q2(max(net_payable - paid, Decimal(0))), "overpaid": q2(max(paid - net_payable, Decimal(0))),
            "payment_status": status}


def refresh_status(db: Session, o: Order) -> None:
    if o.status in ("draft", "cancelled"):
        return
    total = sum(l.qty for l in o.lines)
    sh = list(o.shipments)
    delivered = sum(sl.qty for s in sh if s.status == "delivered" for sl in s.lines)
    returned = sum(returned_qty(db, o.id).values())
    in_transit = any(s.status == "dispatched" for s in sh)
    new = o.status
    if returned > 0 and delivered > 0:
        new = "returned" if returned >= delivered else "partially_returned"
    elif in_transit:
        new = "dispatched"
    elif delivered >= total:
        new = "delivered"
    elif delivered > 0:
        new = "partially_delivered"
    elif any(s.status in ("failed", "refused") for s in sh):
        last = [s for s in sh if s.status in ("failed", "refused")][-1]
        new = "refused" if last.status == "refused" else "delivery_failed"
    elif any(s.status == "packed" for s in sh):
        new = "packed"
    elif any(s.status == "received_back" for s in sh):
        last = [s for s in sh if s.status == "received_back"][-1]
        new = "refused" if last.outcome == "refused" else "delivery_failed"
    else:
        new = "confirmed"
    if new != o.status:
        event(o, None, f"status_{new}")
        o.status = new
        db.flush()


# ---- drafting ---------------------------------------------------------------------------------------------------
def _validate_header(db: Session, customer_id: int, channel_id: int, location_id: int, fulfilment_location_id: int, payment_terms: str):
    c = customer_service.get_customer(db, customer_id)
    if c.status != "active":
        raise BusinessRuleError(f"Customer {c.code} is inactive. Reactivate the record before taking an order.")
    ch = db.get(SalesChannel, channel_id)
    if ch is None or not ch.is_active:
        raise BusinessRuleError("Choose an active sales channel.")
    for lid in {location_id, fulfilment_location_id}:
        loc = db.get(Location, lid)
        if loc is None or not loc.is_active:
            raise BusinessRuleError("Choose an active location.")
    if payment_terms not in ("prepaid", "cod", "credit"):
        raise BusinessRuleError("Payment terms must be prepaid, cod or credit.")
    return c


def _apply_lines(db: Session, o: Order, customer: Customer, lines: list[dict]) -> None:
    """Replace the order's lines with freshly priced ones. `unit_price` (agreed price) is optional; absent means list - promotions."""
    if not lines:
        raise BusinessRuleError("An order needs at least one line.")
    seen: set[int] = set()
    for i, ln in enumerate(lines, 1):
        if int(ln["qty"]) <= 0:
            raise BusinessRuleError(f"Line {i}: quantity must be greater than zero.")
        if ln["variant_id"] in seen:
            raise BusinessRuleError(f"Line {i}: the same item appears twice. Combine the quantities.")
        seen.add(ln["variant_id"])
    quoted = pricing_service.quote(db, customer=customer, lines=lines, on=o.order_date)
    o.lines.clear()
    db.flush()
    worst = Decimal(0)
    for ln, q in zip(lines, quoted):
        lp, promo = q["list_price"], q["promo_discount"]
        agreed = Decimal(str(ln["unit_price"])) if ln.get("unit_price") is not None else lp - promo
        agreed = pricing_service.q2(agreed)
        if agreed < 0:
            raise BusinessRuleError(f"{q['variant'].sku}: the price cannot be negative.")
        if agreed > lp:
            raise BusinessRuleError(f"{q['variant'].sku}: the agreed price {agreed} is above the list price {lp}. Prices above list are not supported.")
        promo = min(promo, lp - agreed)
        base = lp - promo
        manual = (base - agreed)
        if manual > 0 and base > 0:
            worst = max(worst, (manual / base * 100).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))
        o.lines.append(OrderLine(
            variant_id=q["variant"].id, qty=int(ln["qty"]), list_price=lp, promo_discount=promo, unit_price=agreed,
            promotion_ids=",".join(str(p.id) for p in q["promotions"]) or None,
            promotion_names="; ".join(p.name for p in q["promotions"])[:300] or None, notes=ln.get("notes")))
    o.manual_discount_pct = worst
    limit = settings_service.sales_value(db, settings_service.DISCOUNT_LIMIT_KEY)
    o.discount_approval = "pending" if worst > limit else "none"
    o.discount_approved_by = None
    o.discount_approval_note = None
    db.flush()
    totals(db, o, settings_service.sales_value(db, settings_service.TAX_RATE_KEY))


def create_order(db: Session, *, actor: User, customer_id: int, channel_id: int, location_id: int, fulfilment_location_id: int | None,
                 order_date: date, payment_terms: str, delivery: dict, delivery_fee, notes: str | None, lines: list[dict],
                 idempotency_key: str | None, ip: str | None) -> Order:
    if idempotency_key:
        existing = db.scalar(select(Order).where(Order.idempotency_key == idempotency_key))
        if existing is not None:
            existing.replayed = True  # type: ignore[attr-defined]
            return existing
    fulfil = fulfilment_location_id or location_id
    c = _validate_header(db, customer_id, channel_id, location_id, fulfil, payment_terms)
    if order_date > date.today():
        raise BusinessRuleError("The order date cannot be in the future.")
    fee = Decimal(str(delivery_fee or 0))
    if fee < 0:
        raise BusinessRuleError("The delivery fee cannot be negative.")
    o = Order(ref=ledger.next_ref(db, "order_ref_seq", "ORD"), customer_id=c.id, channel_id=channel_id, location_id=location_id,
              fulfilment_location_id=fulfil, order_date=order_date, payment_terms=payment_terms, delivery_fee=fee, notes=notes,
              idempotency_key=idempotency_key, created_by=actor.id, status="draft",
              delivery_name=delivery.get("name") or c.name, delivery_phone=delivery.get("phone") or c.phone,
              delivery_address=delivery.get("address"), delivery_city=delivery.get("city"), billing_address=delivery.get("billing_address"),
              courier=delivery.get("courier"))
    db.add(o)
    db.flush()
    _apply_lines(db, o, c, lines)
    event(o, actor, "created", notes)
    db.flush()
    _audit(db, actor, "order.created", o, ip, total=str(o.total))
    o.replayed = False  # type: ignore[attr-defined]
    return o


def update_draft(db: Session, *, actor: User, order_id: int, values: dict, lines: list[dict] | None, ip: str | None) -> Order:
    o = get_order(db, order_id, lock=True)
    if o.status != "draft":
        raise ConflictError(f"{o.ref} is {o.status.replace('_', ' ')}; only drafts can be edited. Cancel and re-enter, or reopen a cancelled order.")
    c = customer_service.get_customer(db, values.get("customer_id") or o.customer_id)
    for f in ("customer_id", "channel_id", "location_id", "fulfilment_location_id", "order_date", "payment_terms", "notes", "courier"):
        if values.get(f) is not None:
            setattr(o, f, values[f])
    d = values.get("delivery") or {}
    for src, dst in (("name", "delivery_name"), ("phone", "delivery_phone"), ("address", "delivery_address"), ("city", "delivery_city"),
                     ("billing_address", "billing_address")):
        if src in d and d[src] is not None:
            setattr(o, dst, d[src])
    if values.get("delivery_fee") is not None:
        fee = Decimal(str(values["delivery_fee"]))
        if fee < 0:
            raise BusinessRuleError("The delivery fee cannot be negative.")
        o.delivery_fee = fee
    _validate_header(db, o.customer_id, o.channel_id, o.location_id, o.fulfilment_location_id, o.payment_terms)
    if o.order_date > date.today():
        raise BusinessRuleError("The order date cannot be in the future.")
    if lines is not None:
        _apply_lines(db, o, c, lines)
    else:
        # nothing about the lines changed, but the delivery fee may have
        totals(db, o, settings_service.sales_value(db, settings_service.TAX_RATE_KEY))
    event(o, actor, "edited")
    db.flush()
    _audit(db, actor, "order.edited", o, ip, total=str(o.total))
    return o


def decide_discount(db: Session, *, actor: User, order_id: int, approve: bool, note: str, ip: str | None) -> Order:
    o = get_order(db, order_id, lock=True)
    if o.status != "draft" or o.discount_approval != "pending":
        raise ConflictError(f"{o.ref} has no discount waiting for approval.")
    if o.created_by == actor.id:
        raise SelfApprovalError("You cannot approve the discount on your own order.")
    if not (note or "").strip():
        raise BusinessRuleError("Say why you are approving or rejecting this discount.")
    o.discount_approval = "approved" if approve else "rejected"
    o.discount_approved_by = actor.id
    o.discount_approval_note = note.strip()
    event(o, actor, "discount_approved" if approve else "discount_rejected", note)
    db.flush()
    _audit(db, actor, "order.discount_approved" if approve else "order.discount_rejected", o, ip, reason=note,
           manual_discount_pct=str(o.manual_discount_pct))
    return o


# ---- confirmation, cancellation, reopening ----------------------------------------------------------------------
def confirm(db: Session, *, actor: User, order_id: int, ip: str | None) -> Order:
    o = get_order(db, order_id, lock=True)
    if o.status != "draft":
        raise ConflictError(f"{o.ref} is {o.status.replace('_', ' ')}; only a draft can be confirmed.")
    if o.discount_approval == "pending":
        raise BusinessRuleError(f"{o.ref} has a manual discount of {o.manual_discount_pct}% above the limit and must be approved first.")
    if o.discount_approval == "rejected":
        raise BusinessRuleError(f"The discount on {o.ref} was rejected. Change the prices and save the order again.")
    c = _validate_header(db, o.customer_id, o.channel_id, o.location_id, o.fulfilment_location_id, o.payment_terms)
    # prices may have moved since the draft was saved: never confirm silently at stale numbers
    quoted = pricing_service.quote(db, customer=c, lines=[{"variant_id": l.variant_id, "qty": l.qty} for l in o.lines], on=o.order_date)
    stale = []
    for l, q in zip(o.lines, quoted):
        followed_promo = Decimal(l.unit_price) == Decimal(l.list_price) - Decimal(l.promo_discount)   # no manual price on top
        if Decimal(l.list_price) != q["list_price"] or (followed_promo and Decimal(l.promo_discount) != q["promo_discount"]):
            stale.append(q["variant"].sku)
    if stale:
        raise ConflictError(f"Prices changed since this draft was saved ({', '.join(stale)}). Open the order and save it again to re-price.")
    rate = settings_service.sales_value(db, settings_service.TAX_RATE_KEY)
    o.tax_rate_pct = rate
    totals(db, o, rate)
    for l in sorted(o.lines, key=lambda x: x.variant_id):
        try:
            reservation_service.reserve(db, location_id=o.fulfilment_location_id, variant_id=l.variant_id, qty=l.qty, ref_type="order",
                                        ref_id=str(o.id), actor=actor)
        except InsufficientStockError as e:
            raise BusinessRuleError(f"Cannot confirm {o.ref}: {db.get(ProductVariant, l.variant_id).sku} - {e}") from e
    o.status = "confirmed"
    o.confirmed_at = datetime.now(timezone.utc)
    event(o, actor, "confirmed", "Stock reserved")
    db.flush()
    _audit(db, actor, "order.confirmed", o, ip, total=str(o.total))
    return o


def cancel(db: Session, *, actor: User, order_id: int, reason: str, ip: str | None) -> Order:
    o = get_order(db, order_id, lock=True)
    if not (reason or "").strip():
        raise BusinessRuleError("Say why the order is being cancelled.")
    if o.status == "cancelled":
        raise ConflictError(f"{o.ref} is already cancelled.")
    held = [s for s in o.shipments if s.status in ("dispatched", "delivered", "failed", "refused")]
    if o.status not in ("draft", "confirmed", "packed") or held:
        raise ConflictError(f"{o.ref} has goods dispatched or delivered; it cannot be cancelled. Use delivery outcomes and returns instead.")
    for s in o.shipments:
        if s.status == "packed":
            s.status = "cancelled"
    reservation_service.release(db, "order", str(o.id))
    o.status, o.cancel_reason = "cancelled", reason.strip()
    event(o, actor, "cancelled", reason)
    db.flush()
    _audit(db, actor, "order.cancelled", o, ip, reason=reason)
    return o


def reopen(db: Session, *, actor: User, order_id: int, reason: str, ip: str | None) -> Order:
    """The controlled way back from cancelled: the order returns to draft, must be re-checked and confirmed again."""
    o = get_order(db, order_id, lock=True)
    if o.status != "cancelled":
        raise ConflictError(f"{o.ref} is not cancelled.")
    if not (reason or "").strip():
        raise BusinessRuleError("Say why the order is being reopened.")
    o.status, o.cancel_reason = "draft", None
    o.discount_approval = "pending" if o.manual_discount_pct > settings_service.sales_value(db, settings_service.DISCOUNT_LIMIT_KEY) else "none"
    event(o, actor, "reopened", reason)
    db.flush()
    _audit(db, actor, "order.reopened", o, ip, reason=reason)
    return o


def reserve_remaining(db: Session, *, actor: User, order_id: int, ip: str | None) -> Order:
    """After goods came back from a failed or refused delivery and were inspected back into stock, reserve what is still to ship."""
    o = get_order(db, order_id, lock=True)
    if o.status not in SHIPPABLE:
        raise ConflictError(f"{o.ref} is {o.status.replace('_', ' ')}; nothing can be reserved.")
    q = line_quantities(o)
    reserved = {}
    for r in reservation_service.active_for(db, "order", str(o.id)):
        reserved[r.variant_id] = reserved.get(r.variant_id, 0) + r.qty
    packed_by_variant: dict[int, int] = {}
    for l in o.lines:
        packed_by_variant[l.variant_id] = q[l.id]["packed"]
    done = []
    for l in sorted(o.lines, key=lambda x: x.variant_id):
        need = q[l.id]["to_ship"] + q[l.id]["packed"] - reserved.get(l.variant_id, 0)
        if need > 0:
            try:
                reservation_service.reserve(db, location_id=o.fulfilment_location_id, variant_id=l.variant_id, qty=need, ref_type="order",
                                            ref_id=str(o.id), actor=actor, top_up=True)
            except InsufficientStockError as e:
                raise BusinessRuleError(f"Cannot reserve {db.get(ProductVariant, l.variant_id).sku}: {e}") from e
            done.append((l.variant_id, need))
    if not done:
        raise BusinessRuleError("Everything still to ship is already reserved.")
    event(o, actor, "reserved_remaining", ", ".join(f"{v}x{n}" for v, n in done))
    db.flush()
    _audit(db, actor, "order.reserved_remaining", o, ip)
    return o


# ---- shipments --------------------------------------------------------------------------------------------------
def pack(db: Session, *, actor: User, order_id: int, lines: list[dict], notes: str | None, ip: str | None) -> Shipment:
    o = get_order(db, order_id, lock=True)
    if o.status not in SHIPPABLE:
        raise ConflictError(f"{o.ref} is {o.status.replace('_', ' ')}; goods cannot be packed for it.")
    if not lines:
        raise BusinessRuleError("Choose at least one line to pack.")
    q = line_quantities(o)
    by_id = {l.id: l for l in o.lines}
    reserved: dict[int, int] = {}
    for r in reservation_service.active_for(db, "order", str(o.id)):
        reserved[r.variant_id] = reserved.get(r.variant_id, 0) + r.qty
    seen: set[int] = set()
    s = Shipment(ref=ledger.next_ref(db, "shipment_ref_seq", "SHP"), order_id=o.id, location_id=o.fulfilment_location_id,
                 packed_by=actor.id, courier=o.courier)
    for ln in lines:
        l = by_id.get(ln["order_line_id"])
        if l is None:
            raise BusinessRuleError("A packed line does not belong to this order.")
        if l.id in seen:
            raise BusinessRuleError("The same line appears twice.")
        seen.add(l.id)
        n = int(ln["qty"])
        if n <= 0:
            raise BusinessRuleError("Pack quantities must be greater than zero.")
        if n > q[l.id]["to_ship"]:
            raise BusinessRuleError(f"{db.get(ProductVariant, l.variant_id).sku}: only {q[l.id]['to_ship']} still to ship on {o.ref} (asked for {n}).")
        free = reserved.get(l.variant_id, 0) - q[l.id]["packed"]
        if n > free:
            raise BusinessRuleError(f"{db.get(ProductVariant, l.variant_id).sku}: only {max(free, 0)} reserved and unpacked. Reserve the remaining stock first.")
        s.lines.append(ShipmentLine(order_line_id=l.id, variant_id=l.variant_id, qty=n))
    db.add(s)
    o.shipments.append(s)
    db.flush()
    event(o, actor, "packed", f"{s.ref}" + (f": {notes}" if notes else ""))
    refresh_status(db, o)
    _audit(db, actor, "shipment.packed", o, ip, shipment=s.ref)
    return s


def cancel_packed(db: Session, *, actor: User, shipment_id: int, ip: str | None) -> Shipment:
    s = get_shipment(db, shipment_id, lock=True)
    o = get_order(db, s.order_id, lock=True)
    if s.status != "packed":
        raise ConflictError(f"{s.ref} is {s.status}; only a packed parcel that has not left can be unpacked.")
    s.status = "cancelled"
    event(o, actor, "unpacked", s.ref)
    db.flush()
    refresh_status(db, o)
    _audit(db, actor, "shipment.cancelled", o, ip, shipment=s.ref)
    return s


def dispatch(db: Session, *, actor: User, shipment_id: int, courier: str | None, tracking_no: str | None, ip: str | None) -> Shipment:
    s = get_shipment(db, shipment_id, lock=True)
    o = get_order(db, s.order_id, lock=True)
    if s.status != "packed":
        raise ConflictError(f"{s.ref} is {s.status}; it cannot be dispatched (already sent, or not packed).")
    if o.status == "cancelled":
        raise ConflictError(f"{o.ref} is cancelled; goods cannot be dispatched unless the order is reopened.")
    if o.payment_terms == "prepaid":
        m = money_state(db, o)
        if m["paid"] < m["net_payable"]:
            raise BusinessRuleError(f"{o.ref} is prepaid and still has {m['balance_due']} unpaid; record the payment before dispatch.")
    # consume reservations item by item, then post one outflow transaction (balances are locked in a global order by the ledger)
    keys: list[tuple[int, int | None, int, int]] = []   # (variant, batch, qty, order_line_id)
    for sl in sorted(s.lines, key=lambda x: x.variant_id):
        for batch_id, n in reservation_service.consume_qty(db, "order", str(o.id), sl.variant_id, sl.qty):
            keys.append((sl.variant_id, batch_id, n, sl.order_line_id))
    specs = [Spec(s.location_id, v, b, "good", -n, "out") for v, b, n, _ in keys]
    txn = ledger.post(db, txn_type="customer_sale", location_id=s.location_id, specs=specs, actor=actor, reason=f"Dispatch {s.ref} for {o.ref}",
                      source_type="shipment", source_id=s.id, ref_prefix="SAL")
    moves = db.scalars(select(StockMovement).where(StockMovement.txn_id == txn.id)).all()
    values = {}
    for m in moves:
        k = (m.variant_id, m.batch_id)
        values[k] = values.get(k, Decimal(0)) + (-Decimal(m.value_delta))
    s.lines.clear()
    db.flush()
    for v, b, n, ol in keys:
        s.lines.append(ShipmentLine(order_line_id=ol, variant_id=v, batch_id=b, qty=n, stock_value=values[(v, b)]))
    s.status, s.dispatched_by, s.dispatched_at, s.attempts = "dispatched", actor.id, datetime.now(timezone.utc), 1
    s.stock_txn_id = txn.id
    s.courier = courier or s.courier or o.courier
    s.tracking_no = tracking_no or s.tracking_no
    o.courier, o.tracking_no = s.courier or o.courier, s.tracking_no or o.tracking_no
    event(o, actor, "dispatched", f"{s.ref}" + (f" via {s.courier}" if s.courier else "") + (f", tracking {s.tracking_no}" if s.tracking_no else ""))
    db.flush()
    refresh_status(db, o)
    _audit(db, actor, "shipment.dispatched", o, ip, shipment=s.ref, stock_txn=txn.ref)
    return s


def outcome(db: Session, *, actor: User, shipment_id: int, result: str, reason: str | None, ip: str | None) -> Shipment:
    s = get_shipment(db, shipment_id, lock=True)
    o = get_order(db, s.order_id, lock=True)
    if result not in ("delivered", "failed", "refused"):
        raise BusinessRuleError("The outcome must be delivered, failed or refused.")
    if s.status != "dispatched":
        raise ConflictError(f"{s.ref} is {s.status}; an outcome can only be recorded for a parcel that is with the courier.")
    if result != "delivered" and not (reason or "").strip():
        raise BusinessRuleError("Say why the delivery failed or was refused.")
    s.status = result
    s.outcome_reason = (reason or "").strip() or None
    if result == "delivered":
        s.delivered_at = datetime.now(timezone.utc)
    else:
        s.outcome = result
    event(o, actor, f"shipment_{result}", f"{s.ref}" + (f": {reason}" if reason else ""))
    db.flush()
    refresh_status(db, o)
    _audit(db, actor, f"shipment.{result}", o, ip, reason=reason, shipment=s.ref)
    return s


def redispatch(db: Session, *, actor: User, shipment_id: int, tracking_no: str | None, ip: str | None) -> Shipment:
    s = get_shipment(db, shipment_id, lock=True)
    o = get_order(db, s.order_id, lock=True)
    if s.status != "failed":
        raise ConflictError(f"{s.ref} is {s.status}; only a parcel whose delivery failed can be sent out again.")
    s.status, s.outcome, s.attempts = "dispatched", None, s.attempts + 1
    s.tracking_no = tracking_no or s.tracking_no
    event(o, actor, "redispatched", f"{s.ref}, attempt {s.attempts}")
    db.flush()
    refresh_status(db, o)
    _audit(db, actor, "shipment.redispatched", o, ip, shipment=s.ref)
    return s


def receive_back(db: Session, *, actor: User, shipment_id: int, ip: str | None) -> Shipment:
    """The parcel of a failed/refused delivery is back at the location. Units enter `inspection` at their original cost;
    they are not saleable until an inspection disposition is recorded in Inventory."""
    s = get_shipment(db, shipment_id, lock=True)
    o = get_order(db, s.order_id, lock=True)
    if s.status not in ("failed", "refused"):
        raise ConflictError(f"{s.ref} is {s.status}; only parcels with a failed or refused delivery can be received back.")
    specs = [Spec(s.location_id, sl.variant_id, sl.batch_id, "inspection", sl.qty, "in",
                  value=Decimal(sl.stock_value)) for sl in sorted(s.lines, key=lambda x: (x.variant_id, x.batch_id or 0))]
    txn = ledger.post(db, txn_type="return_intake", location_id=s.location_id, specs=specs, actor=actor,
                      reason=f"{s.ref} received back ({s.status})", source_type="shipment_return", source_id=s.id)
    s.status, s.received_back_by, s.received_back_at, s.return_txn_id = "received_back", actor.id, datetime.now(timezone.utc), txn.id
    event(o, actor, "received_back", s.ref)
    db.flush()
    refresh_status(db, o)
    _audit(db, actor, "shipment.received_back", o, ip, shipment=s.ref, stock_txn=txn.ref)
    return s


# ---- receipts and refunds -----------------------------------------------------------------------------------------
def _method(method: str) -> str:
    if method not in ("cash", "bank_transfer", "cod_courier", "card", "store_credit", "other"):
        raise BusinessRuleError("Unknown payment method.")
    return method


def record_receipt(db: Session, *, actor: User, order_id: int, amount, method: str, paid_date: date, reference: str | None,
                   notes: str | None, idempotency_key: str | None, ip: str | None) -> OrderPayment:
    if idempotency_key:
        ex = db.scalar(select(OrderPayment).where(OrderPayment.idempotency_key == idempotency_key))
        if ex is not None:
            return ex
    o = get_order(db, order_id, lock=True)
    if o.status in ("draft", "cancelled"):
        raise BusinessRuleError(f"{o.ref} is {o.status}; payments can only be recorded against a confirmed order.")
    amt = q2(amount)
    if amt <= 0:
        raise BusinessRuleError("The amount must be greater than zero.")
    if paid_date > date.today():
        raise BusinessRuleError("The payment date cannot be in the future.")
    method = _method(method)
    if method in ("bank_transfer", "card") and not (reference or "").strip():
        raise BusinessRuleError("A transaction reference is required for bank transfers and cards.")
    m = money_state(db, o)
    room = m["net_payable"] - m["paid"]
    if amt > room:
        raise BusinessRuleError(f"{amt} is more than the {max(room, Decimal(0))} still due on {o.ref}. Record only what is owed.")
    pay = OrderPayment(ref=ledger.next_ref(db, "order_payment_ref_seq", "RCP"), order_id=o.id, customer_id=o.customer_id, kind="receipt",
                       method=method, amount=amt, paid_date=paid_date, reference=reference, notes=notes,
                       idempotency_key=idempotency_key, created_by=actor.id)
    db.add(pay)
    db.flush()
    if method == "store_credit":
        customer_service.get_customer(db, o.customer_id, lock=True)
        if customer_service.credit_balance(db, o.customer_id) < amt:
            raise BusinessRuleError("The customer does not have enough store credit.")
        customer_service.add_credit_entry(db, customer_id=o.customer_id, entry_type="applied", amount=-amt, source_type="order_payment",
                                          source_id=pay.id, memo=f"Applied to {o.ref}", actor=actor)
    event(o, actor, "payment_received", f"{pay.ref} {amt} by {method}")
    db.flush()
    _audit(db, actor, "order_payment.recorded", o, ip, payment=pay.ref, amount=str(amt))
    return pay


def reverse_payment(db: Session, *, actor: User, payment_id: int, reason: str, ip: str | None) -> OrderPayment:
    pay = db.scalar(select(OrderPayment).where(OrderPayment.id == payment_id).with_for_update().execution_options(populate_existing=True))
    if pay is None:
        raise NotFoundError("Payment not found")
    o = get_order(db, pay.order_id, lock=True)
    if pay.status != "posted":
        raise ConflictError(f"{pay.ref} is already reversed.")
    if pay.kind != "receipt":
        raise BusinessRuleError("A refund that has been paid out cannot be reversed here.")
    if pay.created_by == actor.id:
        raise SelfApprovalError("You cannot reverse a payment you recorded yourself.")
    if not (reason or "").strip():
        raise BusinessRuleError("Say why the payment is being reversed.")
    m = money_state(db, o)
    if m["paid"] - pay.amount < 0:
        raise BusinessRuleError("Part of this payment has already been refunded; the refund must be settled first.")
    pay.status, pay.reversed_by, pay.reversed_at, pay.reverse_reason = "reversed", actor.id, datetime.now(timezone.utc), reason.strip()
    if pay.method == "store_credit":
        customer_service.add_credit_entry(db, customer_id=o.customer_id, entry_type="applied_reversal", amount=Decimal(pay.amount),
                                          source_type="order_payment", source_id=pay.id, memo=f"Reversal of {pay.ref}", actor=actor)
    event(o, actor, "payment_reversed", f"{pay.ref}: {reason}")
    db.flush()
    _audit(db, actor, "order_payment.reversed", o, ip, reason=reason, payment=pay.ref)
    return pay


def refund(db: Session, *, actor: User, order_id: int, amount, method: str, reference: str | None, reason: str, ip: str | None,
           sales_return_id: int | None = None) -> OrderPayment:
    """Give back money the customer has overpaid (after a return is settled or an order is cancelled). Cannot exceed the overpayment."""
    o = get_order(db, order_id, lock=True)
    amt = q2(amount)
    if amt <= 0:
        raise BusinessRuleError("The refund must be greater than zero.")
    if not (reason or "").strip():
        raise BusinessRuleError("Say why the money is being refunded.")
    method = _method(method)
    if method == "store_credit":
        pass
    elif method in ("bank_transfer", "card") and not (reference or "").strip():
        raise BusinessRuleError("A transaction reference is required for bank transfers and cards.")
    m = money_state(db, o)
    if amt > m["overpaid"]:
        raise BusinessRuleError(f"Only {m['overpaid']} has been overpaid on {o.ref}; {amt} cannot be refunded.")
    pay = OrderPayment(ref=ledger.next_ref(db, "order_payment_ref_seq", "RCP"), order_id=o.id, customer_id=o.customer_id, kind="refund",
                       method=method, amount=amt, paid_date=date.today(), reference=reference, notes=reason,
                       sales_return_id=sales_return_id, created_by=actor.id)
    db.add(pay)
    db.flush()
    if method == "store_credit":
        customer_service.get_customer(db, o.customer_id, lock=True)
        customer_service.add_credit_entry(db, customer_id=o.customer_id, entry_type="return_credit", amount=amt, source_type="order_payment",
                                          source_id=pay.id, memo=f"Credit from {o.ref}", actor=actor)
    event(o, actor, "refund_issued", f"{pay.ref} {amt} by {method}")
    db.flush()
    _audit(db, actor, "order_payment.refund", o, ip, reason=reason, payment=pay.ref, amount=str(amt))
    return pay
