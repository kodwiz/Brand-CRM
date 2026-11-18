"""Customer records. Phone numbers are normalised for duplicate detection; order bookers only reach the customers they
created or have taken orders for (company-wide visibility needs customers.view_all)."""
from __future__ import annotations

import re
from decimal import Decimal

from sqlalchemy import exists, func, or_, select
from sqlalchemy.orm import Session

from backend.models import Customer, CustomerAddress, CustomerCreditEntry, Order, User
from backend.services import audit_service, ledger
from backend.services.errors import BusinessRuleError, ConflictError, NotFoundError

Q2 = Decimal("0.01")


def normalise_phone(raw: str) -> str:
    digits = re.sub(r"\D", "", raw or "")
    if digits.startswith("0092"):
        digits = digits[2:]
    if digits.startswith("92") and len(digits) == 12:
        digits = "0" + digits[2:]
    if not 7 <= len(digits) <= 15:
        raise BusinessRuleError("Enter a valid phone number (7 to 15 digits).")
    return digits


def visible_filter(user: User, can_view_all: bool):
    """SQL condition limiting customers to those this user may see."""
    if can_view_all:
        return None
    return or_(Customer.created_by == user.id,
               exists().where(Order.customer_id == Customer.id, Order.created_by == user.id))


def get_customer(db: Session, customer_id: int, *, lock: bool = False) -> Customer:
    q = select(Customer).where(Customer.id == customer_id)
    if lock:
        q = q.with_for_update().execution_options(populate_existing=True)
    c = db.scalar(q)
    if c is None:
        raise NotFoundError("Customer not found")
    return c


def check_visible(db: Session, user: User, customer_id: int, can_view_all: bool) -> Customer:
    c = get_customer(db, customer_id)
    cond = visible_filter(user, can_view_all)
    if cond is not None and db.scalar(select(Customer.id).where(Customer.id == customer_id, cond)) is None:
        raise NotFoundError("Customer not found")
    return c


def _set_addresses(c: Customer, addresses: list[dict]) -> None:
    c.addresses.clear()
    defaults: set[str] = set()
    for a in addresses:
        kind = a.get("kind", "delivery")
        if kind not in ("delivery", "billing"):
            raise BusinessRuleError("Address kind must be delivery or billing.")
        if not (a.get("line") or "").strip():
            raise BusinessRuleError("An address needs its street/area line.")
        is_default = bool(a.get("is_default")) and kind not in defaults
        if is_default:
            defaults.add(kind)
        c.addresses.append(CustomerAddress(kind=kind, label=a.get("label"), line=a["line"].strip(), city=a.get("city"),
                                           is_default=is_default))
    for kind in ("delivery", "billing"):
        first = next((x for x in c.addresses if x.kind == kind), None)
        if first is not None and kind not in defaults:
            first.is_default = True


def create_customer(db: Session, *, actor: User, name: str, phone: str, email: str | None, category: str, notes: str | None,
                    addresses: list[dict], ip: str | None) -> Customer:
    name = (name or "").strip()
    if not name:
        raise BusinessRuleError("The customer's name is required.")
    norm = normalise_phone(phone)
    dup = db.scalar(select(Customer).where(Customer.phone_norm == norm))
    if dup is not None:
        # do not reveal whose record it is; the code lets a manager find it
        raise ConflictError(f"A customer with this phone number is already registered ({dup.code}).")
    c = Customer(code=ledger.next_ref(db, "customer_code_seq", "CUS"), name=name, phone=phone.strip(), phone_norm=norm,
                 email=(email or None), category=(category or "retail").strip().lower(), notes=notes, created_by=actor.id)
    _set_addresses(c, addresses or [])
    db.add(c)
    db.flush()
    audit_service.record(db, actor=actor, action="customer.created", entity_type="customer", entity_id=c.id,
                         after={"code": c.code, "name": c.name}, ip=ip)
    return c


def update_customer(db: Session, *, actor: User, customer: Customer, values: dict, ip: str | None) -> Customer:
    before = {"name": customer.name, "phone": customer.phone, "status": customer.status, "category": customer.category}
    if values.get("name") is not None:
        if not values["name"].strip():
            raise BusinessRuleError("The customer's name is required.")
        customer.name = values["name"].strip()
    if values.get("phone") is not None:
        norm = normalise_phone(values["phone"])
        dup = db.scalar(select(Customer).where(Customer.phone_norm == norm, Customer.id != customer.id))
        if dup is not None:
            raise ConflictError(f"A customer with this phone number is already registered ({dup.code}).")
        customer.phone, customer.phone_norm = values["phone"].strip(), norm
    for f in ("email", "notes"):
        if f in values and values[f] is not None:
            setattr(customer, f, values[f] or None)
    if values.get("category"):
        customer.category = values["category"].strip().lower()
    if values.get("status"):
        if values["status"] not in ("active", "inactive"):
            raise BusinessRuleError("Status must be active or inactive.")
        customer.status = values["status"]
    if values.get("addresses") is not None:
        _set_addresses(customer, values["addresses"])
    db.flush()
    audit_service.record(db, actor=actor, action="customer.updated", entity_type="customer", entity_id=customer.id, before=before,
                         after={"name": customer.name, "phone": customer.phone, "status": customer.status, "category": customer.category}, ip=ip)
    return customer


def credit_balance(db: Session, customer_id: int) -> Decimal:
    return Decimal(db.scalar(select(func.coalesce(func.sum(CustomerCreditEntry.amount), 0))
                             .where(CustomerCreditEntry.customer_id == customer_id))).quantize(Q2)


def add_credit_entry(db: Session, *, customer_id: int, entry_type: str, amount: Decimal, source_type: str, source_id: int,
                     memo: str | None, actor: User) -> CustomerCreditEntry:
    e = CustomerCreditEntry(customer_id=customer_id, entry_type=entry_type, amount=amount, source_type=source_type,
                            source_id=source_id, memo=memo, created_by=actor.id)
    db.add(e)
    db.flush()
    return e
