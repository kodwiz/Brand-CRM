"""Inventory operations built on the ledger: receiving, opening stock, damage/expiry, disposition, returns, reversal."""
from __future__ import annotations

from datetime import date
from decimal import Decimal, InvalidOperation

from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.models import (
    Batch, ProductVariant, ReceiptCost, StockMovement, StockReceipt, StockReceiptLine, StockTransaction, User,
)
from backend.services import audit_service, ledger
from backend.services.errors import BusinessRuleError, ConflictError, NotFoundError, SelfApprovalError
from backend.services.ledger import Spec

REVERSIBLE_TYPES = {"receipt", "opening_stock", "adjustment", "count_variance", "damage", "expiry", "writeoff",
                    "return_intake", "return_disposition", "employee_purchase"}


def get_variant(db: Session, variant_id: int) -> ProductVariant:
    v = db.get(ProductVariant, variant_id)
    if v is None:
        raise NotFoundError(f"Variant {variant_id} not found")
    return v


def resolve_batch(db: Session, variant: ProductVariant, batch_no: str | None, expiry: date | None, *,
                  create: bool = True, quantity_accepted: bool = True) -> Batch | None:
    """Return the batch row for tracked products (creating it if allowed). Non-tracked products have no batch."""
    batch_no = (batch_no or "").strip() or None
    product = variant.product
    if not product.tracks_batches:
        if batch_no:
            raise BusinessRuleError(f"{variant.sku}: this product is not batch-tracked; leave the batch blank.")
        if product.tracks_expiry and expiry is None and quantity_accepted:
            raise BusinessRuleError(f"{variant.sku}: an expiry date is required.")
        return None
    if not batch_no:
        raise BusinessRuleError(f"{variant.sku}: a batch number is required for this product.")
    batch = db.scalar(select(Batch).where(Batch.variant_id == variant.id, Batch.batch_no == batch_no))
    if batch is not None:
        if expiry and batch.expiry_date and batch.expiry_date != expiry:
            raise BusinessRuleError(
                f"{variant.sku} batch {batch_no} already exists with expiry {batch.expiry_date}; got {expiry}.")
        if expiry and batch.expiry_date is None and create:
            batch.expiry_date = expiry
        return batch
    if product.tracks_expiry and expiry is None and quantity_accepted:
        raise BusinessRuleError(f"{variant.sku} batch {batch_no}: an expiry date is required.")
    if not create:
        return None
    batch = Batch(variant_id=variant.id, batch_no=batch_no, expiry_date=expiry)
    db.add(batch)
    db.flush()
    return batch


def _dec(value, field: str) -> Decimal:
    try:
        d = Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise BusinessRuleError(f"{field} must be a number.")
    if d < 0:
        raise BusinessRuleError(f"{field} cannot be negative.")
    return d


# ---- receiving -------------------------------------------------------------------------------------
def create_receipt(db: Session, *, actor: User, location_id: int, received_date: date, lines: list[dict],
                   purchase_reference: str | None, supplier_reference: str | None, notes: str | None,
                   ip: str | None, purchase_order_id: int | None = None, supplier_id: int | None = None,
                   additional_costs: list[dict] | None = None) -> StockReceipt:
    from backend.services import purchase_service, supplier_service  # local import: purchasing builds on receiving
    if not lines:
        raise BusinessRuleError("A receipt needs at least one line.")
    if received_date > date.today():
        raise BusinessRuleError("The received date cannot be in the future.")
    po = None
    if purchase_order_id:
        po, lines = purchase_service.prepare_receipt_for_po(db, purchase_order_id, location_id, lines)
        supplier_id = po.supplier_id
        purchase_reference = purchase_reference or po.ref
    elif supplier_id:
        supplier_service.get_supplier(db, supplier_id)
    rec = StockReceipt(ref=ledger.next_ref(db, "stock_receipt_ref_seq", "GRN"), location_id=location_id,
                       purchase_reference=purchase_reference, supplier_reference=supplier_reference,
                       received_date=received_date, notes=notes, created_by=actor.id, supplier_id=supplier_id,
                       purchase_order_id=po.id if po else None)
    for i, ln in enumerate(lines, 1):
        v = get_variant(db, ln["variant_id"])
        rcv, acc, dmg, rej = (int(ln["qty_received"]), int(ln["qty_accepted"]), int(ln.get("qty_damaged", 0)),
                              int(ln.get("qty_rejected", 0)))
        if min(rcv, acc, dmg, rej) < 0:
            raise BusinessRuleError(f"Line {i} ({v.sku}): quantities cannot be negative.")
        if rcv == 0:
            raise BusinessRuleError(f"Line {i} ({v.sku}): received quantity must be greater than zero.")
        if acc + dmg + rej != rcv:
            raise BusinessRuleError(f"Line {i} ({v.sku}): accepted + damaged + rejected must equal the quantity received.")
        cost = _dec(ln.get("unit_cost"), f"Line {i} ({v.sku}) unit cost") if ln.get("unit_cost") is not None else None
        if cost is None:
            raise BusinessRuleError(f"Line {i} ({v.sku}): a unit cost is required.")
        resolve_batch(db, v, ln.get("batch_no"), ln.get("expiry_date"), create=False, quantity_accepted=acc > 0)
        if ln.get("expiry_date") and ln["expiry_date"] < date.today() and acc > 0:
            raise BusinessRuleError(f"Line {i} ({v.sku}): the expiry date is in the past; record these units as damaged or rejected.")
        rec.lines.append(StockReceiptLine(
            variant_id=v.id, batch_no=(ln.get("batch_no") or None), expiry_date=ln.get("expiry_date"),
            qty_expected=ln.get("qty_expected"), qty_received=rcv, qty_accepted=acc, qty_damaged=dmg, qty_rejected=rej,
            unit_cost=cost, po_line_id=ln.get("po_line_id"), notes=ln.get("notes")))
    extra_total = Decimal(0)
    for c in additional_costs or []:
        amt = _dec(c.get("amount"), "Additional cost amount")
        if amt <= 0 or not (c.get("description") or "").strip():
            raise BusinessRuleError("Each additional cost needs a description and an amount above zero.")
        rec.costs.append(ReceiptCost(description=c["description"].strip(), amount=amt))
        extra_total += amt
    if extra_total:
        purchase_service.allocate_additional_costs(rec.lines, extra_total)
    db.add(rec)
    db.flush()
    if po is not None:
        purchase_service.check_over_receipt(db, po, rec)
    audit_service.record(db, actor=actor, action="receipt.created", entity_type="stock_receipt", entity_id=rec.id,
                         location_id=location_id, after={"ref": rec.ref, "lines": len(rec.lines),
                                                         "purchase_reference": purchase_reference,
                                                         "supplier_id": supplier_id, "additional_costs": str(extra_total)}, ip=ip)
    return rec


def get_receipt(db: Session, receipt_id: int, *, lock: bool = False) -> StockReceipt:
    q = select(StockReceipt).where(StockReceipt.id == receipt_id)
    if lock:
        q = q.with_for_update().execution_options(populate_existing=True)
    r = db.scalar(q)
    if r is None:
        raise NotFoundError("Receipt not found")
    return r


def post_receipt(db: Session, *, actor: User, receipt_id: int, ip: str | None) -> StockReceipt:
    from backend.services import purchase_service
    rec = get_receipt(db, receipt_id, lock=True)
    if rec.status != "draft":
        raise ConflictError(f"Receipt {rec.ref} is already {rec.status}; it cannot be posted again.")
    po = None
    if rec.purchase_order_id:  # serialise against other receipts/invoices on the same order
        po = purchase_service.get_po(db, rec.purchase_order_id, lock=True)
        if po.status not in ("approved", "partially_received", "received"):
            raise ConflictError(f"{po.ref} is {po.status.replace('_', ' ')}; goods can no longer be received against it.")
        purchase_service.check_over_receipt(db, po, rec)
    specs: list[Spec] = []
    for ln in rec.lines:
        v = get_variant(db, ln.variant_id)
        batch = resolve_batch(db, v, ln.batch_no, ln.expiry_date, create=True, quantity_accepted=ln.qty_accepted > 0)
        bid = batch.id if batch else None
        owned = ln.qty_accepted + ln.qty_damaged
        alloc = Decimal(ln.landed_alloc or 0)
        alloc_good = ledger.q4(alloc * ln.qty_accepted / owned) if owned else Decimal(0)
        alloc_dmg = alloc - alloc_good  # the remainder, so the whole allocated cost reaches stock exactly
        if ln.qty_accepted:
            specs.append(Spec(rec.location_id, v.id, bid, "good", ln.qty_accepted, "in",
                              value=ledger.q4(ln.qty_accepted * ln.unit_cost) + alloc_good))
        if ln.qty_damaged:  # owned but unsaleable; stays out of available stock
            specs.append(Spec(rec.location_id, v.id, bid, "damaged", ln.qty_damaged, "in",
                              value=ledger.q4(ln.qty_damaged * ln.unit_cost) + alloc_dmg))
        # rejected units never enter stock or the books
    txn = None
    if specs:
        txn = ledger.post(db, txn_type="receipt", location_id=rec.location_id, specs=specs, actor=actor,
                          reason=f"Goods receipt {rec.ref}", notes=rec.notes, source_type="receipt", source_id=rec.id)
    rec.status = "posted"
    rec.txn_id = txn.id if txn else None
    rec.posted_by = actor.id
    from datetime import datetime, timezone
    rec.posted_at = datetime.now(timezone.utc)
    db.flush()
    if po is not None:
        purchase_service.refresh_status(db, po.id)
    audit_service.record(db, actor=actor, action="receipt.posted", entity_type="stock_receipt", entity_id=rec.id,
                         location_id=rec.location_id, after={"ref": rec.ref, "txn": txn.ref if txn else None,
                                                             "accepted": sum(l.qty_accepted for l in rec.lines),
                                                             "damaged": sum(l.qty_damaged for l in rec.lines),
                                                             "rejected": sum(l.qty_rejected for l in rec.lines)}, ip=ip)
    return rec


def cancel_receipt(db: Session, *, actor: User, receipt_id: int, reason: str, ip: str | None) -> StockReceipt:
    rec = get_receipt(db, receipt_id, lock=True)
    if rec.status != "draft":
        raise ConflictError("Only draft receipts can be cancelled. Reverse a posted receipt instead.")
    rec.status = "cancelled"
    db.flush()
    audit_service.record(db, actor=actor, action="receipt.cancelled", entity_type="stock_receipt", entity_id=rec.id,
                         location_id=rec.location_id, reason=reason, ip=ip)
    return rec


# ---- opening stock ---------------------------------------------------------------------------------
def opening_stock(db: Session, *, actor: User, lines: list[dict], dry_run: bool, idempotency_key: str | None,
                  ip: str | None) -> dict:
    """Enter verified opening stock. Validates every line first; any error => nothing is written (all-or-nothing)."""
    errors: list[dict] = []
    seen: set[tuple] = set()
    prepared: list[tuple[int, Spec]] = []
    for i, ln in enumerate(lines, 1):
        def err(msg: str, i=i) -> None:
            errors.append({"line": i, "message": msg})
        try:
            v = get_variant(db, ln["variant_id"])
            qty = int(ln["qty"])
            if qty <= 0:
                err("Quantity must be greater than zero.")
                continue
            if ln.get("unit_cost") is None:
                err("A verified unit cost is required. Costs are never guessed.")
                continue
            cost = _dec(ln["unit_cost"], "Unit cost")
            batch = resolve_batch(db, v, ln.get("batch_no"), ln.get("expiry_date"), create=False)
            if ln.get("expiry_date") and ln["expiry_date"] < date.today():
                err("The expiry date is in the past.")
                continue
            key = (ln["location_id"], v.id, (ln.get("batch_no") or "").strip().upper())
            if key in seen:
                err("The same location, item and batch appear more than once.")
                continue
            seen.add(key)
            if batch is not None:
                dup = db.scalar(select(StockMovement.id).join(StockTransaction, StockTransaction.id == StockMovement.txn_id).where(
                    StockTransaction.txn_type == "opening_stock", StockTransaction.status == "posted",
                    StockMovement.location_id == ln["location_id"], StockMovement.variant_id == v.id,
                    StockMovement.batch_id == batch.id).limit(1))
                if dup:
                    err("Opening stock was already entered for this location, item and batch. Reverse it first to re-enter.")
                    continue
            elif not v.product.tracks_batches:
                dup = db.scalar(select(StockMovement.id).join(StockTransaction, StockTransaction.id == StockMovement.txn_id).where(
                    StockTransaction.txn_type == "opening_stock", StockTransaction.status == "posted",
                    StockMovement.location_id == ln["location_id"], StockMovement.variant_id == v.id,
                    StockMovement.batch_id.is_(None)).limit(1))
                if dup:
                    err("Opening stock was already entered for this location and item. Reverse it first to re-enter.")
                    continue
            prepared.append((i, Spec(ln["location_id"], v.id, None, "good", qty, "in", unit_cost=cost)))
        except (BusinessRuleError, NotFoundError) as e:
            err(str(e))
        except (KeyError, ValueError, TypeError):
            err("Missing or invalid field.")
    if errors or dry_run:
        return {"ok": not errors, "dry_run": dry_run, "errors": errors, "lines": len(lines), "transactions": []}

    # real run: create batches, then one transaction per location
    by_loc: dict[int, list[Spec]] = {}
    for (i, spec), ln in zip(prepared, lines):
        v = get_variant(db, spec.variant_id)
        batch = resolve_batch(db, v, ln.get("batch_no"), ln.get("expiry_date"), create=True)
        spec.batch_id = batch.id if batch else None
        by_loc.setdefault(spec.location_id, []).append(spec)
    refs = []
    for loc, specs in sorted(by_loc.items()):
        key = f"{idempotency_key}:{loc}" if idempotency_key else None
        txn = ledger.post(db, txn_type="opening_stock", location_id=loc, specs=specs, actor=actor,
                          reason="Verified opening stock", idempotency_key=key)
        refs.append(txn.ref)
        audit_service.record(db, actor=actor, action="stock.opening_entered", entity_type="stock_transaction",
                             entity_id=txn.id, location_id=loc, after={"ref": txn.ref, "lines": len(specs)}, ip=ip)
    return {"ok": True, "dry_run": False, "errors": [], "lines": len(lines), "transactions": refs}


# ---- bucket moves: damage, expiry, inspection disposition ---------------------------------------------
MOVE_RULES = {
    # (txn_type, from_bucket, to_bucket)
    "damage": ("damage", "good", "damaged"),
    "expiry": ("expiry", "good", "expired"),
    "inspection_restock": ("return_disposition", "inspection", "good"),
    "inspection_damaged": ("return_disposition", "inspection", "damaged"),
    "inspection_expired": ("return_disposition", "inspection", "expired"),
}


def move_bucket(db: Session, *, actor: User, action: str, location_id: int, variant_id: int, batch_id: int | None,
                qty: int, reason: str, notes: str | None, idempotency_key: str | None, ip: str | None) -> StockTransaction:
    if action not in MOVE_RULES:
        raise BusinessRuleError(f"Unknown action {action}")
    if qty <= 0:
        raise BusinessRuleError("Quantity must be greater than zero.")
    if not (reason or "").strip():
        raise BusinessRuleError("A reason is required.")
    txn_type, src, dst = MOVE_RULES[action]
    v = get_variant(db, variant_id)
    if v.product.tracks_batches and batch_id is None:
        raise BusinessRuleError(f"{v.sku}: choose the batch.")
    txn = ledger.post(db, txn_type=txn_type, location_id=location_id, actor=actor, reason=reason, notes=notes,
                      idempotency_key=idempotency_key,
                      specs=[Spec(location_id, variant_id, batch_id, src, qty, "move", to_bucket=dst)])
    if not getattr(txn, "replayed", False):
        audit_service.record(db, actor=actor, action=f"stock.{action}", entity_type="stock_transaction", entity_id=txn.id,
                             location_id=location_id, reason=reason,
                             after={"ref": txn.ref, "variant_id": variant_id, "qty": qty, "from": src, "to": dst}, ip=ip)
    return txn


def return_to_inspection(db: Session, *, actor: User, location_id: int, variant_id: int, batch_id: int | None, qty: int,
                         original_unit_cost: Decimal, source_type: str, source_id: int, reason: str,
                         ip: str | None = None) -> StockTransaction:
    """Called by the sales/distributor modules (Phases 4-5). The returned units enter `inspection` at the original cost
    of goods sold and are not saleable until a disposition is recorded."""
    if qty <= 0:
        raise BusinessRuleError("Quantity must be greater than zero.")
    txn = ledger.post(db, txn_type="return_intake", location_id=location_id, actor=actor, reason=reason,
                      source_type=source_type, source_id=source_id,
                      specs=[Spec(location_id, variant_id, batch_id, "inspection", qty, "in",
                                  unit_cost=_dec(original_unit_cost, "Original cost"))])
    audit_service.record(db, actor=actor, action="stock.return_intake", entity_type="stock_transaction", entity_id=txn.id,
                         location_id=location_id, reason=reason, after={"ref": txn.ref, "qty": qty}, ip=ip)
    return txn


# ---- reversal --------------------------------------------------------------------------------------
def reverse_transaction(db: Session, *, actor: User, txn_id: int, reason: str, ip: str | None) -> StockTransaction:
    if not (reason or "").strip():
        raise BusinessRuleError("A reason is required to reverse a transaction.")
    txn = db.scalar(select(StockTransaction).where(StockTransaction.id == txn_id).with_for_update()
                    .execution_options(populate_existing=True))
    if txn is None:
        raise NotFoundError("Transaction not found")
    if txn.status != "posted":
        raise ConflictError(f"{txn.ref} is already {txn.status}.")
    if txn.txn_type not in REVERSIBLE_TYPES:
        raise BusinessRuleError(
            f"{txn.ref} is a {txn.txn_type.replace('_', ' ')} and cannot be reversed directly. "
            "Transfers are corrected through the transfer discrepancy workflow.")
    if txn.created_by is not None and txn.created_by == actor.id:
        raise SelfApprovalError("You cannot reverse a transaction you posted yourself. Ask another authorised user.")
    specs = []
    for m in txn.movements:
        effect = {"in": "out", "out": "in", "none": "none"}[m.cost_effect]
        specs.append(Spec(m.location_id, m.variant_id, m.batch_id, m.bucket, -m.qty_delta, "raw",
                          value=-m.value_delta, cost_effect=effect))
    try:
        rev = ledger.post(db, txn_type="reversal", location_id=txn.location_id, specs=specs, actor=actor,
                          reason=reason, reverses_txn_id=txn.id, source_type="reversal", source_id=txn.id)
    except BusinessRuleError as e:
        raise BusinessRuleError(f"{txn.ref} cannot be reversed because the stock has since been used or moved: {e}")
    txn.status = "reversed"
    if txn.source_type == "receipt" and txn.source_id:
        r = db.get(StockReceipt, txn.source_id)
        if r is not None:
            r.status = "reversed"
            if r.purchase_order_id:
                db.flush()
                from backend.services import purchase_service
                purchase_service.refresh_status(db, r.purchase_order_id)
    db.flush()
    audit_service.record(db, actor=actor, action="stock.reversed", entity_type="stock_transaction", entity_id=txn.id,
                         location_id=txn.location_id, reason=reason, before={"ref": txn.ref, "status": "posted"},
                         after={"ref": txn.ref, "status": "reversed", "reversal_ref": rev.ref}, ip=ip)
    return rev
