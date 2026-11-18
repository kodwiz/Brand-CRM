"""Supplier records and balances."""
from __future__ import annotations

from decimal import Decimal

from sqlalchemy import func, or_, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from backend.models import PurchaseOrder, Supplier, SupplierLedgerEntry, User
from backend.services import audit_service
from backend.services.errors import BusinessRuleError, ConflictError, NotFoundError

EDITABLE = ("name", "contact_name", "phone", "email", "address", "city", "tax_number", "payment_terms_days", "notes")


def q2(x) -> Decimal:
    return Decimal(str(x)).quantize(Decimal("0.01"))


def get_supplier(db: Session, supplier_id: int, *, lock: bool = False) -> Supplier:
    q = select(Supplier).where(Supplier.id == supplier_id)
    if lock:
        q = q.with_for_update().execution_options(populate_existing=True)
    s = db.scalar(q)
    if s is None:
        raise NotFoundError("Supplier not found")
    return s


def _clean(data: dict) -> dict:
    out = {}
    for k in EDITABLE:
        if k in data and data[k] is not None:
            v = data[k]
            out[k] = v.strip() if isinstance(v, str) else v
            if isinstance(v, str) and not out[k] and k != "notes":
                out[k] = None
    return out


def _name_taken(db: Session, name: str, exclude_id: int | None = None) -> bool:
    q = select(Supplier.id).where(func.lower(Supplier.name) == name.lower())
    if exclude_id:
        q = q.where(Supplier.id != exclude_id)
    return db.scalar(q) is not None


def create_supplier(db: Session, *, actor: User, data: dict, ip: str | None) -> Supplier:
    d = _clean(data)
    if not d.get("name"):
        raise BusinessRuleError("A supplier name is required.")
    if _name_taken(db, d["name"]):
        raise ConflictError(f"A supplier called '{d['name']}' already exists.")
    code = (data.get("code") or "").strip().upper()
    if code:
        if db.scalar(select(Supplier.id).where(Supplier.code == code)):
            raise ConflictError(f"Supplier code {code} is already in use.")
    else:
        code = f"SUP-{db.scalar(text('select nextval(:s)'), {'s': 'supplier_code_seq'}):04d}"
    s = Supplier(code=code, created_by=actor.id, currency=(data.get("currency") or "PKR").upper(), **d)
    s.payment_terms_days = int(d.get("payment_terms_days") or 0)
    db.add(s)
    try:
        db.flush()
    except IntegrityError:
        raise ConflictError("A supplier with this name or code already exists.")
    audit_service.record(db, actor=actor, action="supplier.created", entity_type="supplier", entity_id=s.id,
                         after={"code": s.code, "name": s.name, "payment_terms_days": s.payment_terms_days}, ip=ip)
    return s


def update_supplier(db: Session, *, actor: User, supplier_id: int, data: dict, ip: str | None) -> Supplier:
    s = get_supplier(db, supplier_id, lock=True)
    d = _clean(data)
    before = {k: getattr(s, k) for k in EDITABLE} | {"is_active": s.is_active}
    if "name" in d:
        if not d["name"]:
            raise BusinessRuleError("A supplier name is required.")
        if _name_taken(db, d["name"], s.id):
            raise ConflictError(f"A supplier called '{d['name']}' already exists.")
    for k, v in d.items():
        setattr(s, k, v)
    if "payment_terms_days" in d and not 0 <= int(d["payment_terms_days"]) <= 365:
        raise BusinessRuleError("Payment terms must be between 0 and 365 days.")
    if data.get("is_active") is not None and bool(data["is_active"]) != s.is_active:
        if not data["is_active"]:
            if balance(db, s.id) != 0:
                raise BusinessRuleError("This supplier still has a balance. Settle or adjust it before deactivating.")
            open_po = db.scalar(select(func.count()).select_from(PurchaseOrder).where(
                PurchaseOrder.supplier_id == s.id, PurchaseOrder.status.in_(("draft", "approved", "partially_received"))))
            if open_po:
                raise BusinessRuleError("This supplier has open purchase orders. Close or cancel them first.")
        s.is_active = bool(data["is_active"])
    db.flush()
    after = {k: getattr(s, k) for k in EDITABLE} | {"is_active": s.is_active}
    if before != after:
        audit_service.record(db, actor=actor, action="supplier.updated", entity_type="supplier", entity_id=s.id,
                             before=before, after=after, ip=ip)
    return s


def list_suppliers(db: Session, *, q: str | None, active: bool | None, page: int, page_size: int) -> tuple[list[Supplier], int]:
    stmt = select(Supplier)
    if q:
        like = f"%{q.strip().lower()}%"
        stmt = stmt.where(or_(func.lower(Supplier.name).like(like), func.lower(Supplier.code).like(like),
                              func.lower(func.coalesce(Supplier.contact_name, "")).like(like),
                              func.lower(func.coalesce(Supplier.phone, "")).like(like)))
    if active is not None:
        stmt = stmt.where(Supplier.is_active == active)
    total = db.scalar(select(func.count()).select_from(stmt.subquery())) or 0
    rows = db.scalars(stmt.order_by(func.lower(Supplier.name)).offset((page - 1) * page_size).limit(page_size)).all()
    return list(rows), total


def balance(db: Session, supplier_id: int) -> Decimal:
    """What we owe the supplier: the sum of the append-only supplier ledger (negative = they owe us / prepayment)."""
    return q2(Decimal(db.scalar(select(func.coalesce(func.sum(SupplierLedgerEntry.amount), 0))
                                .where(SupplierLedgerEntry.supplier_id == supplier_id))))


def add_ledger_entry(db: Session, *, supplier_id: int, entry_type: str, amount: Decimal, entry_date, source_type: str,
                     source_id: int, ref: str, memo: str | None, actor: User) -> SupplierLedgerEntry:
    """The only way liability changes. The unique (type, source) constraint stops the same document being booked twice."""
    e = SupplierLedgerEntry(supplier_id=supplier_id, entry_type=entry_type, amount=q2(amount), entry_date=entry_date,
                            source_type=source_type, source_id=source_id, ref=ref, memo=memo, created_by=actor.id)
    db.add(e)
    try:
        db.flush()
    except IntegrityError:
        raise ConflictError(f"{ref} has already been posted to the supplier ledger.")
    return e


def post_opening(db: Session, *, actor: User, supplier_id: int, amount, as_of, note: str | None, ip: str | None) -> SupplierLedgerEntry:
    """What the company already owed this supplier before the system. One per supplier, never edited: a later correction is an adjustment.
    It posts Dr Opening balance equity / Cr Supplier payables, so it is cleared to owner capital with the other opening balances."""
    from datetime import date
    sup = get_supplier(db, supplier_id, lock=True)
    amt = q2(amount)
    if amt <= 0:
        raise BusinessRuleError("The opening payable must be greater than zero.")
    if as_of > date.today():
        raise BusinessRuleError("The opening balance date cannot be in the future.")
    if db.scalar(select(SupplierLedgerEntry.id).where(SupplierLedgerEntry.supplier_id == sup.id, SupplierLedgerEntry.source_type == "supplier_opening")):
        raise ConflictError(f"{sup.name} already has an opening balance. Use an adjustment to correct it.")
    e = add_ledger_entry(db, supplier_id=sup.id, entry_type="adjustment", amount=amt, entry_date=as_of, source_type="supplier_opening", source_id=sup.id,
                         ref=f"OPS-{sup.code}", memo=(note or "Opening balance")[:200], actor=actor)
    audit_service.record(db, actor=actor, action="supplier_opening.posted", entity_type="supplier", entity_id=sup.id,
                         after={"amount": str(amt), "as_of": as_of.isoformat(), "note": note}, ip=ip)
    return e
