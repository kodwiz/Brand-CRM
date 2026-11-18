"""Returning goods to a supplier. Dispatch takes stock out of the ledger at the current average cost; the credit the supplier
grants arrives separately as an approved 'reduce payable' adjustment linked to the return."""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.models import Location, ProductVariant, PurchaseOrder, StockMovement, StockReceipt, SupplierReturn, SupplierReturnLine, User
from backend.services import audit_service, ledger, supplier_service
from backend.services.errors import BusinessRuleError, ConflictError, NotFoundError
from backend.services.ledger import Spec


def get_return(db: Session, return_id: int, *, lock: bool = False) -> SupplierReturn:
    q = select(SupplierReturn).where(SupplierReturn.id == return_id)
    if lock:
        q = q.with_for_update().execution_options(populate_existing=True)
    r = db.scalar(q)
    if r is None:
        raise NotFoundError("Supplier return not found")
    return r


def create_return(db: Session, *, actor: User, supplier_id: int, location_id: int, reason: str, notes: str | None,
                  purchase_order_id: int | None, receipt_id: int | None, lines: list[dict], ip: str | None) -> SupplierReturn:
    supplier = supplier_service.get_supplier(db, supplier_id)
    loc = db.get(Location, location_id)
    if loc is None or not loc.is_active:
        raise BusinessRuleError("Choose an active location.")
    if not (reason or "").strip():
        raise BusinessRuleError("A reason is required.")
    if not lines:
        raise BusinessRuleError("A return needs at least one line.")
    if purchase_order_id:
        po = db.get(PurchaseOrder, purchase_order_id)
        if po is None or po.supplier_id != supplier_id:
            raise BusinessRuleError("That purchase order does not belong to this supplier.")
    if receipt_id:
        rc = db.get(StockReceipt, receipt_id)
        if rc is None or rc.supplier_id != supplier_id:
            raise BusinessRuleError("That goods receipt does not belong to this supplier.")
    r = SupplierReturn(ref=ledger.next_ref(db, "supplier_return_ref_seq", "SRT"), supplier_id=supplier.id, location_id=location_id,
                       purchase_order_id=purchase_order_id, receipt_id=receipt_id, reason=reason.strip(), notes=notes,
                       created_by=actor.id)
    for i, ln in enumerate(lines, 1):
        v = db.get(ProductVariant, ln["variant_id"])
        if v is None:
            raise BusinessRuleError(f"Line {i}: item not found.")
        qty = int(ln["qty"])
        if qty <= 0:
            raise BusinessRuleError(f"Line {i} ({v.sku}): quantity must be above zero.")
        bucket = ln.get("bucket") or "damaged"
        if bucket not in ("good", "damaged", "expired", "inspection"):
            raise BusinessRuleError(f"Line {i} ({v.sku}): choose the stock bucket the goods are in.")
        r.lines.append(SupplierReturnLine(variant_id=v.id, batch_id=ln.get("batch_id"), bucket=bucket, qty=qty))
    db.add(r)
    db.flush()
    audit_service.record(db, actor=actor, action="supplier_return.created", entity_type="supplier_return", entity_id=r.id,
                         location_id=location_id, reason=reason, after={"ref": r.ref, "supplier": supplier.code,
                                                                        "lines": len(r.lines)}, ip=ip)
    return r


def dispatch(db: Session, *, actor: User, return_id: int, ip: str | None) -> SupplierReturn:
    r = get_return(db, return_id, lock=True)
    if r.status != "draft":
        raise ConflictError(f"{r.ref} is already {r.status}; it cannot be dispatched again.")
    specs = [Spec(r.location_id, ln.variant_id, ln.batch_id, ln.bucket, -ln.qty, "out") for ln in r.lines]
    txn = ledger.post(db, txn_type="supplier_return", location_id=r.location_id, specs=specs, actor=actor,
                      reason=f"Return to supplier {r.ref}", source_type="supplier_return", source_id=r.id)
    movements = db.scalars(select(StockMovement).where(StockMovement.txn_id == txn.id).order_by(StockMovement.id)).all()
    for ln, m in zip(r.lines, movements):
        ln.stock_value = -Decimal(m.value_delta)
    r.status, r.txn_id = "dispatched", txn.id
    r.dispatched_by, r.dispatched_at = actor.id, datetime.now(timezone.utc)
    db.flush()
    audit_service.record(db, actor=actor, action="supplier_return.dispatched", entity_type="supplier_return", entity_id=r.id,
                         location_id=r.location_id, after={"ref": r.ref, "txn": txn.ref,
                                                           "stock_value": str(sum(l.stock_value or 0 for l in r.lines))}, ip=ip)
    return r


def cancel(db: Session, *, actor: User, return_id: int, reason: str, ip: str | None) -> SupplierReturn:
    r = get_return(db, return_id, lock=True)
    if r.status != "draft":
        raise ConflictError("Only a draft return can be cancelled; stock has already left once it is dispatched.")
    r.status = "cancelled"
    db.flush()
    audit_service.record(db, actor=actor, action="supplier_return.cancelled", entity_type="supplier_return", entity_id=r.id,
                         location_id=r.location_id, reason=reason, after={"ref": r.ref}, ip=ip)
    return r
