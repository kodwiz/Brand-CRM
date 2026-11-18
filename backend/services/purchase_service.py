"""Purchase orders, and everything that ties receipts and invoices back to them (quantities, matching, status)."""
from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from backend.models import (
    Location, ProductVariant, PurchaseOrder, PurchaseOrderLine, StockReceipt, StockReceiptLine, SupplierInvoice,
    SupplierInvoiceLine, User,
)
from backend.services import audit_service, ledger, settings_service as cfg, supplier_service
from backend.services.errors import BusinessRuleError, ConflictError, NotFoundError, SelfApprovalError

OPEN_STATUSES = ("approved", "partially_received", "received")


def q4(x) -> Decimal:
    return ledger.q4(Decimal(str(x)))


def q2(x) -> Decimal:
    return supplier_service.q2(x)


def get_po(db: Session, po_id: int, *, lock: bool = False) -> PurchaseOrder:
    q = select(PurchaseOrder).where(PurchaseOrder.id == po_id)
    if lock:
        q = q.with_for_update().execution_options(populate_existing=True)
    po = db.scalar(q)
    if po is None:
        raise NotFoundError("Purchase order not found")
    return po


def po_total(po: PurchaseOrder) -> Decimal:
    return q2(sum((Decimal(l.qty_ordered) * Decimal(l.unit_cost) for l in po.lines), Decimal(0)))


def _prepare_lines(db: Session, lines: list[dict]) -> list[PurchaseOrderLine]:
    if not lines:
        raise BusinessRuleError("A purchase order needs at least one line.")
    seen: set[int] = set()
    out = []
    for i, ln in enumerate(lines, 1):
        v = db.get(ProductVariant, ln["variant_id"])
        if v is None:
            raise BusinessRuleError(f"Line {i}: item not found.")
        if v.id in seen:
            raise BusinessRuleError(f"Line {i} ({v.sku}): the same item appears twice. Combine the quantities on one line.")
        seen.add(v.id)
        if getattr(v, "status", "active") == "inactive":
            raise BusinessRuleError(f"Line {i} ({v.sku}): this item is inactive and cannot be ordered.")
        qty = int(ln["qty_ordered"])
        if qty <= 0:
            raise BusinessRuleError(f"Line {i} ({v.sku}): the quantity must be greater than zero.")
        if ln.get("unit_cost") is None:
            raise BusinessRuleError(f"Line {i} ({v.sku}): a unit cost is required. Costs are never guessed.")
        cost = Decimal(str(ln["unit_cost"]))
        if cost < 0:
            raise BusinessRuleError(f"Line {i} ({v.sku}): the unit cost cannot be negative.")
        out.append(PurchaseOrderLine(variant_id=v.id, qty_ordered=qty, unit_cost=q4(cost), notes=ln.get("notes")))
    return out


def _check_header(db: Session, supplier_id: int, location_id: int, order_date: date, expected_date: date | None):
    s = supplier_service.get_supplier(db, supplier_id)
    if not s.is_active:
        raise BusinessRuleError("This supplier is inactive. Reactivate it before ordering.")
    loc = db.get(Location, location_id)
    if loc is None or not loc.is_active:
        raise BusinessRuleError("Choose an active delivery location.")
    if expected_date and expected_date < order_date:
        raise BusinessRuleError("The expected delivery date cannot be before the order date.")
    return s


def create_po(db: Session, *, actor: User, supplier_id: int, location_id: int, order_date: date, expected_date: date | None,
              lines: list[dict], notes: str | None, supplier_reference: str | None, ip: str | None) -> PurchaseOrder:
    s = _check_header(db, supplier_id, location_id, order_date, expected_date)
    po = PurchaseOrder(ref=ledger.next_ref(db, "purchase_order_ref_seq", "PO"), supplier_id=s.id, location_id=location_id,
                       order_date=order_date, expected_date=expected_date, payment_terms_days=s.payment_terms_days,
                       notes=notes, supplier_reference=supplier_reference, created_by=actor.id)
    po.lines = _prepare_lines(db, lines)
    db.add(po)
    db.flush()
    audit_service.record(db, actor=actor, action="po.created", entity_type="purchase_order", entity_id=po.id,
                         location_id=location_id, after={"ref": po.ref, "supplier": s.code, "lines": len(po.lines),
                                                         "total": str(po_total(po))}, ip=ip)
    return po


def update_po(db: Session, *, actor: User, po_id: int, supplier_id: int | None, location_id: int | None,
              order_date: date | None, expected_date: date | None, lines: list[dict] | None, notes: str | None,
              supplier_reference: str | None, ip: str | None) -> PurchaseOrder:
    po = get_po(db, po_id, lock=True)
    if po.status != "draft":
        raise ConflictError(f"{po.ref} is {po.status.replace('_', ' ')}; only draft orders can be edited. "
                            "Cancel it and raise a new one, or record the difference on the invoice.")
    before = {"total": str(po_total(po)), "lines": len(po.lines)}
    sid = supplier_id or po.supplier_id
    s = _check_header(db, sid, location_id or po.location_id, order_date or po.order_date,
                      expected_date if expected_date is not None else po.expected_date)
    if sid != po.supplier_id:
        po.supplier_id = sid
        po.payment_terms_days = s.payment_terms_days
    po.location_id = location_id or po.location_id
    po.order_date = order_date or po.order_date
    if expected_date is not None:
        po.expected_date = expected_date
    if notes is not None:
        po.notes = notes
    if supplier_reference is not None:
        po.supplier_reference = supplier_reference
    if lines is not None:
        new = _prepare_lines(db, lines)
        po.lines.clear()
        db.flush()
        po.lines.extend(new)
    db.flush()
    audit_service.record(db, actor=actor, action="po.updated", entity_type="purchase_order", entity_id=po.id,
                         location_id=po.location_id, before=before,
                         after={"total": str(po_total(po)), "lines": len(po.lines)}, ip=ip)
    return po


def approve_po(db: Session, *, actor: User, po_id: int, ip: str | None) -> PurchaseOrder:
    po = get_po(db, po_id, lock=True)
    if po.status != "draft":
        raise ConflictError(f"{po.ref} is already {po.status.replace('_', ' ')}.")
    total = po_total(po)
    limit = cfg.purchasing_value(db, cfg.PO_SELF_APPROVE_KEY)
    if po.created_by == actor.id and total > limit:
        raise SelfApprovalError(
            "You cannot approve a purchase order you created yourself"
            + (f" above {limit}" if limit > 0 else "") + ". Ask another authorised user to approve it.")
    po.status = "approved"
    po.approved_by = actor.id
    po.approved_at = datetime.now(timezone.utc)
    db.flush()
    audit_service.record(db, actor=actor, action="po.approved", entity_type="purchase_order", entity_id=po.id,
                         location_id=po.location_id, after={"ref": po.ref, "total": str(total)}, ip=ip)
    return po


def _has_activity(db: Session, po_id: int) -> bool:
    rec = db.scalar(select(func.count()).select_from(StockReceipt).where(
        StockReceipt.purchase_order_id == po_id, StockReceipt.status.in_(("draft", "posted"))))
    inv = db.scalar(select(func.count()).select_from(SupplierInvoice).where(
        SupplierInvoice.purchase_order_id == po_id, SupplierInvoice.status.in_(("draft", "held", "posted"))))
    return bool(rec or inv)


def cancel_po(db: Session, *, actor: User, po_id: int, reason: str, ip: str | None) -> PurchaseOrder:
    if not (reason or "").strip():
        raise BusinessRuleError("A reason is required to cancel a purchase order.")
    po = get_po(db, po_id, lock=True)
    if po.status not in ("draft", "approved"):
        raise ConflictError(f"{po.ref} is {po.status.replace('_', ' ')} and cannot be cancelled. "
                            "Close it instead if the remaining quantity will not arrive.")
    if _has_activity(db, po.id):
        raise ConflictError(f"{po.ref} already has receipts or invoices. Close it instead of cancelling.")
    po.status = "cancelled"
    po.closed_by, po.closed_at, po.close_reason = actor.id, datetime.now(timezone.utc), reason
    db.flush()
    audit_service.record(db, actor=actor, action="po.cancelled", entity_type="purchase_order", entity_id=po.id,
                         location_id=po.location_id, reason=reason, ip=ip)
    return po


def close_po(db: Session, *, actor: User, po_id: int, reason: str, ip: str | None) -> PurchaseOrder:
    """Close an order whose remaining quantity will not be delivered. Nothing already received or invoiced changes."""
    if not (reason or "").strip():
        raise BusinessRuleError("A reason is required to close a purchase order.")
    po = get_po(db, po_id, lock=True)
    if po.status not in OPEN_STATUSES:
        raise ConflictError(f"{po.ref} is {po.status.replace('_', ' ')}; only approved orders can be closed.")
    pending = db.scalar(select(func.count()).select_from(StockReceipt).where(
        StockReceipt.purchase_order_id == po.id, StockReceipt.status == "draft"))
    if pending:
        raise ConflictError("There are draft receipts against this order. Post or cancel them first.")
    po.status = "closed"
    po.closed_by, po.closed_at, po.close_reason = actor.id, datetime.now(timezone.utc), reason
    db.flush()
    audit_service.record(db, actor=actor, action="po.closed", entity_type="purchase_order", entity_id=po.id,
                         location_id=po.location_id, reason=reason, ip=ip)
    return po


# ---- quantities and matching ---------------------------------------------------------------------------
def line_quantities(db: Session, po_id: int) -> dict[int, dict]:
    """Per PO line: ordered / received (posted receipts) / invoiced (posted + held invoices). Always derived, never stored."""
    po = get_po(db, po_id)
    out = {l.id: {"ordered": l.qty_ordered, "received": 0, "accepted": 0, "damaged": 0, "rejected": 0, "invoiced": 0,
                  "invoiced_value": Decimal(0)} for l in po.lines}
    rows = db.execute(
        select(StockReceiptLine.po_line_id, func.sum(StockReceiptLine.qty_received), func.sum(StockReceiptLine.qty_accepted),
               func.sum(StockReceiptLine.qty_damaged), func.sum(StockReceiptLine.qty_rejected))
        .join(StockReceipt, StockReceipt.id == StockReceiptLine.receipt_id)
        .where(StockReceipt.purchase_order_id == po_id, StockReceipt.status == "posted")
        .group_by(StockReceiptLine.po_line_id)).all()
    for lid, rcv, acc, dmg, rej in rows:
        if lid in out:
            out[lid].update(received=int(rcv), accepted=int(acc), damaged=int(dmg), rejected=int(rej))
    inv = db.execute(
        select(SupplierInvoiceLine.po_line_id, func.sum(SupplierInvoiceLine.qty), func.sum(SupplierInvoiceLine.line_total))
        .join(SupplierInvoice, SupplierInvoice.id == SupplierInvoiceLine.invoice_id)
        .where(SupplierInvoice.status.in_(("posted", "held")), SupplierInvoiceLine.po_line_id.in_(list(out) or [0]))
        .group_by(SupplierInvoiceLine.po_line_id)).all()
    for lid, qty, val in inv:
        out[lid].update(invoiced=int(qty), invoiced_value=Decimal(val))
    for d in out.values():
        d["owned"] = d["accepted"] + d["damaged"]          # goods we took ownership of (billable)
        d["remaining_to_receive"] = max(d["ordered"] - d["owned"], 0)
        d["not_yet_invoiced"] = d["owned"] - d["invoiced"]  # > 0: received, awaiting invoice; < 0: invoiced ahead of receipt
    return out


def refresh_status(db: Session, po_id: int) -> None:
    """Recompute received/partially received after a receipt is posted or reversed."""
    po = get_po(db, po_id, lock=True)
    if po.status not in OPEN_STATUSES:
        return
    qty = line_quantities(db, po_id)
    if all(q["owned"] >= q["ordered"] for q in qty.values()):
        new = "received"
    elif any(q["received"] > 0 for q in qty.values()):
        new = "partially_received"
    else:
        new = "approved"
    if new != po.status:
        po.status = new
        db.flush()


def matching(db: Session, po: PurchaseOrder) -> list[dict]:
    """Ordered vs received vs invoiced, with the discrepancy shown per line."""
    qty = line_quantities(db, po.id)
    vm = {v.id: v for v in db.scalars(select(ProductVariant).where(ProductVariant.id.in_([l.variant_id for l in po.lines]))).all()}
    rows = []
    for l in po.lines:
        q = qty[l.id]
        flags = []
        if q["owned"] < q["ordered"] and po.status in OPEN_STATUSES:
            flags.append("short_received")
        if q["owned"] > q["ordered"]:
            flags.append("over_received")
        if q["invoiced"] > q["owned"]:
            flags.append("invoiced_ahead_of_receipt")
        elif q["invoiced"] < q["owned"]:
            flags.append("received_not_invoiced")
        if q["invoiced"] > q["ordered"]:
            flags.append("over_invoiced")
        if q["rejected"]:
            flags.append("rejected_on_receipt")
        rows.append({"po_line_id": l.id, "variant_id": l.variant_id, "sku": vm[l.variant_id].sku,
                     "variant_name": vm[l.variant_id].name, "unit_cost": str(l.unit_cost), **{k: (str(v) if isinstance(v, Decimal) else v) for k, v in q.items()},
                     "flags": flags})
    return rows


# ---- hooks used by goods receiving ---------------------------------------------------------------------
def prepare_receipt_for_po(db: Session, po_id: int, location_id: int, lines: list[dict]) -> tuple[PurchaseOrder, list[dict]]:
    """Validate a receipt against its purchase order and fill in what the order already knows (PO line, unit cost)."""
    po = get_po(db, po_id)
    if po.status not in ("approved", "partially_received", "received"):
        raise BusinessRuleError(f"{po.ref} is {po.status.replace('_', ' ')}; goods can only be received against an approved order.")
    if po.location_id != location_id:
        raise BusinessRuleError(f"{po.ref} is for a different delivery location than this receipt.")
    by_variant = {l.variant_id: l for l in po.lines}
    out = []
    for i, ln in enumerate(lines, 1):
        pl = by_variant.get(ln["variant_id"])
        if pl is None:
            raise BusinessRuleError(f"Line {i}: this item is not on {po.ref}. Add it to a new order or receive it without an order.")
        out.append({**ln, "po_line_id": pl.id, "unit_cost": pl.unit_cost})
    return po, out


def check_over_receipt(db: Session, po: PurchaseOrder, receipt: StockReceipt) -> None:
    """Cumulative owned quantity (accepted + damaged) may not exceed the ordered quantity beyond the tolerance."""
    tol = cfg.purchasing_value(db, cfg.RECEIPT_TOL_KEY)
    qty = line_quantities(db, po.id)  # posted receipts only; this one is added below
    per_line: dict[int, int] = {}
    for ln in receipt.lines:
        per_line[ln.po_line_id] = per_line.get(ln.po_line_id, 0) + ln.qty_accepted + ln.qty_damaged
    vm = {l.id: l for l in po.lines}
    for lid, add in per_line.items():
        if lid is None:
            continue
        ordered = vm[lid].qty_ordered
        allowed = int((Decimal(ordered) * (Decimal(100) + tol) / Decimal(100)).to_integral_value(rounding="ROUND_FLOOR"))
        if qty[lid]["owned"] + add > allowed:
            sku = db.get(ProductVariant, vm[lid].variant_id).sku
            raise BusinessRuleError(
                f"{sku}: receiving {add} more would bring the total to {qty[lid]['owned'] + add} but only {ordered} were ordered"
                + (f" (tolerance {tol}%)" if tol else "") + ". Reduce the quantity, or record the excess as rejected.")


def allocate_additional_costs(lines: list[StockReceiptLine], total_cost: Decimal) -> None:
    """Spread freight/duty/handling over the lines that enter stock, in proportion to their value (then to quantity when
    all values are zero). The last line takes the rounding remainder, so the allocated total always equals the cost."""
    base = [l for l in lines if (l.qty_accepted + l.qty_damaged) > 0]
    if total_cost <= 0:
        return
    if not base:
        raise BusinessRuleError("Additional costs cannot be added to a receipt where every unit was rejected.")
    weights = [Decimal(l.qty_accepted + l.qty_damaged) * Decimal(l.unit_cost) for l in base]
    if sum(weights) == 0:
        weights = [Decimal(l.qty_accepted + l.qty_damaged) for l in base]
    wsum = sum(weights)
    left = total_cost
    for i, (l, w) in enumerate(zip(base, weights)):
        share = left if i == len(base) - 1 else q4(total_cost * w / wsum)
        l.landed_alloc = share
        left -= share
