"""Customer returns: request -> approval -> physical receipt (stock enters `inspection`, never saleable) -> inspection
(each unit is restocked, damaged or expired) -> settlement (refund, store credit, or a reduction of what the customer owes).

Stock is never restored to saleable before inspection. The refund can never exceed what the customer has actually overpaid
after the returned goods are taken off the order total."""
from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from backend.models import Order, SalesReturn, SalesReturnLine, ShipmentLine, Shipment, User
from backend.services import audit_service, ledger, sales_service, settings_service
from backend.services.errors import BusinessRuleError, ConflictError, NotFoundError, SelfApprovalError
from backend.services.ledger import Spec

q2 = sales_service.q2
OPEN_RETURN = ("requested", "approved", "received", "inspected")


def get_return(db: Session, return_id: int, *, lock: bool = False) -> SalesReturn:
    q = select(SalesReturn).where(SalesReturn.id == return_id)
    if lock:
        q = q.with_for_update().execution_options(populate_existing=True)
    r = db.scalar(q)
    if r is None:
        raise NotFoundError("Return not found")
    return r


def _audit(db, actor, action, r: SalesReturn, ip, reason=None, **after):
    audit_service.record(db, actor=actor, action=action, entity_type="sales_return", entity_id=r.id, location_id=r.location_id,
                         reason=reason, after={"ref": r.ref, "status": r.status, **after}, ip=ip)


def _active_requested(db: Session, order_line_id: int, exclude_return: int | None = None) -> int:
    q = (select(func.coalesce(func.sum(SalesReturnLine.qty_requested), 0)).join(SalesReturn, SalesReturn.id == SalesReturnLine.sales_return_id)
         .where(SalesReturnLine.order_line_id == order_line_id, SalesReturn.status.in_(("requested", "approved", "received", "inspected", "settled"))))
    if exclude_return:
        q = q.where(SalesReturn.id != exclude_return)
    return int(db.scalar(q))


def request_return(db: Session, *, actor: User, order_id: int, lines: list[dict], reason: str, refund_mode: str,
                   refund_delivery_fee: bool, ip: str | None) -> SalesReturn:
    o = sales_service.get_order(db, order_id, lock=True)
    if not (reason or "").strip():
        raise BusinessRuleError("Say why the goods are being returned.")
    if refund_mode not in ("refund", "store_credit"):
        raise BusinessRuleError("Refund mode must be refund or store_credit.")
    if not lines:
        raise BusinessRuleError("Choose at least one line to return.")
    q = sales_service.line_quantities(o)
    by_id = {l.id: l for l in o.lines}
    window = int(settings_service.get(db, settings_service.RETURN_WINDOW_KEY) or 0)
    if window:
        last = max((s.delivered_at for s in o.shipments if s.status == "delivered" and s.delivered_at), default=None)
        if last is None or last.date() + timedelta(days=window) < date.today():
            raise BusinessRuleError(f"Returns are accepted for {window} days after delivery; this order is outside that window.")
    seen: set[int] = set()
    r = SalesReturn(ref=ledger.next_ref(db, "sales_return_ref_seq", "CRT"), order_id=o.id, customer_id=o.customer_id,
                    location_id=o.fulfilment_location_id, reason=reason.strip(), refund_mode=refund_mode,
                    refund_delivery_fee=bool(refund_delivery_fee), requested_by=actor.id)
    for ln in lines:
        l = by_id.get(ln["order_line_id"])
        if l is None:
            raise BusinessRuleError("A returned line does not belong to this order.")
        if l.id in seen:
            raise BusinessRuleError("The same line appears twice.")
        seen.add(l.id)
        n = int(ln["qty"])
        room = q[l.id]["delivered"] - _active_requested(db, l.id)
        if n <= 0 or n > room:
            raise BusinessRuleError(f"Line {l.variant_id}: only {max(room, 0)} delivered units can still be returned (asked for {n}).")
        r.lines.append(SalesReturnLine(order_line_id=l.id, variant_id=l.variant_id, qty_requested=n))
    if refund_delivery_fee:
        if Decimal(o.delivery_fee) == 0:
            raise BusinessRuleError("This order had no delivery fee to refund.")
        if db.scalar(select(func.count()).select_from(SalesReturn).where(
                SalesReturn.order_id == o.id, SalesReturn.refund_delivery_fee.is_(True), SalesReturn.status.in_(("requested", "approved", "received", "inspected", "settled")))):
            raise BusinessRuleError("The delivery fee is already part of another return on this order.")
    db.add(r)
    db.flush()
    sales_service.event(o, actor, "return_requested", f"{r.ref}: {reason}")
    db.flush()
    _audit(db, actor, "sales_return.requested", r, ip, reason=reason)
    return r


def decide(db: Session, *, actor: User, return_id: int, approve: bool, note: str | None, ip: str | None) -> SalesReturn:
    r = get_return(db, return_id, lock=True)
    if r.status != "requested":
        raise ConflictError(f"{r.ref} is {r.status}; it has already been decided.")
    if r.requested_by == actor.id:
        raise SelfApprovalError("You cannot approve a return you requested yourself.")
    if not approve and not (note or "").strip():
        raise BusinessRuleError("Say why the return is rejected.")
    r.status = "approved" if approve else "rejected"
    r.decided_by, r.decision_note = actor.id, (note or "").strip() or None
    r.decided_at = func.now()
    o = sales_service.get_order(db, r.order_id)
    sales_service.event(o, actor, f"return_{'approved' if approve else 'rejected'}", f"{r.ref}" + (f": {note}" if note else ""))
    db.flush()
    _audit(db, actor, f"sales_return.{'approved' if approve else 'rejected'}", r, ip, reason=note)
    return r


def cancel(db: Session, *, actor: User, return_id: int, reason: str, ip: str | None) -> SalesReturn:
    r = get_return(db, return_id, lock=True)
    if r.status not in ("requested", "approved"):
        raise ConflictError(f"{r.ref} is {r.status}; goods may already be back, so it cannot be cancelled.")
    if not (reason or "").strip():
        raise BusinessRuleError("Say why the return is cancelled.")
    r.status = "cancelled"
    o = sales_service.get_order(db, r.order_id)
    sales_service.event(o, actor, "return_cancelled", f"{r.ref}: {reason}")
    db.flush()
    _audit(db, actor, "sales_return.cancelled", r, ip, reason=reason)
    return r


def _sold_from(db: Session, order_line_id: int) -> dict[int | None, tuple[int, Decimal]]:
    """Delivered units and their cost per batch for an order line."""
    rows = db.execute(select(ShipmentLine.batch_id, func.sum(ShipmentLine.qty), func.sum(ShipmentLine.stock_value))
                      .join(Shipment, Shipment.id == ShipmentLine.shipment_id)
                      .where(ShipmentLine.order_line_id == order_line_id, Shipment.status == "delivered")
                      .group_by(ShipmentLine.batch_id)).all()
    return {r[0]: (int(r[1]), Decimal(r[2] or 0)) for r in rows}


def _already_received(db: Session, order_line_id: int, batch_id: int | None, exclude: int) -> int:
    q = (select(func.coalesce(func.sum(SalesReturnLine.qty_received), 0)).join(SalesReturn, SalesReturn.id == SalesReturnLine.sales_return_id)
         .where(SalesReturnLine.order_line_id == order_line_id, SalesReturn.id != exclude,
                SalesReturn.status.in_(("received", "inspected", "settled"))))
    q = q.where(SalesReturnLine.batch_id.is_(None)) if batch_id is None else q.where(SalesReturnLine.batch_id == batch_id)
    return int(db.scalar(q))


def receive(db: Session, *, actor: User, return_id: int, lines: list[dict], ip: str | None) -> SalesReturn:
    r = get_return(db, return_id, lock=True)
    o = sales_service.get_order(db, r.order_id, lock=True)
    if r.status != "approved":
        raise ConflictError(f"{r.ref} is {r.status}; goods can only be received for an approved return.")
    by_id = {l.id: l for l in r.lines}
    given = {ln["line_id"]: ln for ln in lines}
    if set(given) - set(by_id):
        raise BusinessRuleError("A received line does not belong to this return.")
    specs: list[Spec] = []
    any_received = False
    for l in r.lines:
        ln = given.get(l.id, {"qty_received": 0})
        n = int(ln.get("qty_received") or 0)
        if n < 0 or n > l.qty_requested:
            raise BusinessRuleError(f"Line {l.variant_id}: received {n}, but only {l.qty_requested} were approved for return.")
        l.qty_received = n
        if n == 0:
            continue
        any_received = True
        sold = _sold_from(db, l.order_line_id)
        batch = ln.get("batch_id")
        if batch is None:
            if len(sold) > 1:
                raise BusinessRuleError(f"Line {l.variant_id} was sold from several batches; say which batch these units belong to.")
            batch = next(iter(sold))
        if batch not in sold:
            raise BusinessRuleError(f"Line {l.variant_id}: that batch was not part of this order.")
        qty_sold, value = sold[batch]
        if n > qty_sold - _already_received(db, l.order_line_id, batch, r.id):
            raise BusinessRuleError(f"Line {l.variant_id}: more units than were delivered from that batch.")
        l.batch_id = batch
        l.unit_cost = ledger.q4(value / qty_sold) if qty_sold else Decimal(0)
        specs.append(Spec(r.location_id, l.variant_id, batch, "inspection", n, "in", value=ledger.q4(l.unit_cost * n)))
    if not any_received:
        raise BusinessRuleError("Enter the quantity that actually arrived on at least one line (or cancel the return).")
    txn = ledger.post(db, txn_type="return_intake", location_id=r.location_id, specs=specs, actor=actor, reason=f"Customer return {r.ref}",
                      source_type="sales_return", source_id=r.id)
    r.intake_txn_id, r.status, r.received_by, r.received_at = txn.id, "received", actor.id, func.now()
    sales_service.event(o, actor, "return_received", r.ref)
    db.flush()
    sales_service.refresh_status(db, o)
    _audit(db, actor, "sales_return.received", r, ip, stock_txn=txn.ref)
    return r


DISPOSITION = {"restock": "good", "damaged": "damaged", "expired": "expired"}


def inspect(db: Session, *, actor: User, return_id: int, lines: list[dict], ip: str | None) -> SalesReturn:
    r = get_return(db, return_id, lock=True)
    o = sales_service.get_order(db, r.order_id, lock=True)
    if r.status != "received":
        raise ConflictError(f"{r.ref} is {r.status}; only received goods can be inspected.")
    given = {ln["line_id"]: ln for ln in lines}
    by_id = {l.id: l for l in r.lines}
    if set(given) - set(by_id):
        raise BusinessRuleError("An inspected line does not belong to this return.")
    specs: list[Spec] = []
    value = Decimal(0)
    rate = Decimal(o.tax_rate_pct)
    ol = {l.id: l for l in o.lines}
    for l in r.lines:
        if l.qty_received == 0:
            continue
        ln = given.get(l.id)
        if ln is None:
            raise BusinessRuleError(f"Line {l.variant_id}: record what happened to each of the {l.qty_received} units received.")
        parts = {k: int(ln.get(k) or 0) for k in DISPOSITION}
        if min(parts.values()) < 0 or sum(parts.values()) != l.qty_received:
            raise BusinessRuleError(f"Line {l.variant_id}: restocked + damaged + expired must equal the {l.qty_received} units received.")
        refund_qty = int(ln["refund_qty"]) if ln.get("refund_qty") is not None else l.qty_received
        if refund_qty < 0 or refund_qty > l.qty_received:
            raise BusinessRuleError(f"Line {l.variant_id}: refund quantity must be between 0 and {l.qty_received}.")
        l.qty_restock, l.qty_damaged, l.qty_expired, l.refund_qty = parts["restock"], parts["damaged"], parts["expired"], refund_qty
        l.inspection_note = ln.get("note")
        for k, bucket in DISPOSITION.items():
            if parts[k]:
                specs.append(Spec(r.location_id, l.variant_id, l.batch_id, "inspection", parts[k], "move", to_bucket=bucket))
        unit = Decimal(ol[l.order_line_id].unit_price)
        value += (unit + q2(unit * rate / 100)) * refund_qty
    if r.refund_delivery_fee:
        value += Decimal(o.delivery_fee)
    if specs:
        ledger.post(db, txn_type="return_disposition", location_id=r.location_id, specs=specs, actor=actor,
                    reason=f"Inspection of {r.ref}", source_type="sales_return", source_id=r.id)
    r.status, r.inspected_by, r.inspected_at, r.return_value = "inspected", actor.id, func.now(), q2(value)
    sales_service.event(o, actor, "return_inspected", f"{r.ref}: value {r.return_value}")
    db.flush()
    _audit(db, actor, "sales_return.inspected", r, ip, return_value=str(r.return_value))
    return r


def settle(db: Session, *, actor: User, return_id: int, refund_amount, method: str | None, reference: str | None, ip: str | None) -> SalesReturn:
    r = get_return(db, return_id, lock=True)
    o = sales_service.get_order(db, r.order_id, lock=True)
    if r.status != "inspected":
        raise ConflictError(f"{r.ref} is {r.status}; it must be inspected before it can be settled.")
    if r.requested_by == actor.id:
        raise SelfApprovalError("You cannot settle a return you requested yourself.")
    # take the returned goods off what the customer owes, then see how much (if any) they have overpaid
    r.status, r.settled_by, r.settled_at = "settled", actor.id, func.now()
    db.flush()
    m = sales_service.money_state(db, o)
    amount = q2(refund_amount) if refund_amount is not None else m["overpaid"]
    if amount < 0 or amount > m["overpaid"]:
        raise BusinessRuleError(f"Only {m['overpaid']} can be given back; the rest of the return value reduces what the customer owes.")
    r.refund_paid = amount
    if amount > 0:
        pay_method = "store_credit" if r.refund_mode == "store_credit" else (method or "")
        if not pay_method:
            raise BusinessRuleError("Choose how the refund is paid (cash, bank transfer, card or other).")
        sales_service.refund(db, actor=actor, order_id=o.id, amount=amount, method=pay_method, reference=reference,
                             reason=f"Return {r.ref}", ip=ip, sales_return_id=r.id)
    sales_service.event(o, actor, "return_settled", f"{r.ref}: value {r.return_value}, paid back {amount}")
    db.flush()
    sales_service.refresh_status(db, o)
    _audit(db, actor, "sales_return.settled", r, ip, return_value=str(r.return_value), refund_paid=str(amount))
    return r
