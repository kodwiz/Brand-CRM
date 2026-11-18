"""Employees, staff discount rules and employee purchases.

An employee purchase is a sale and a stock movement. It is recorded in one step when it is paid in full; when part of it is on credit
it waits for a second person (credit must be allowed by the company, within the employee's own limit) and the goods are only
issued once that approval is given. Money later received is a separate payment record. Nothing here writes to the ledger: the
posting engine reads these records (see accounting_posting). Separation of duties:

  * nobody records, approves, pays or voids a purchase of their own (an employee linked to a user account is that user);
  * credit is approved by someone other than the person who recorded it;
  * a purchase and a payment are reversed by someone other than the person who recorded them.
"""
from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import ROUND_HALF_UP, Decimal

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from backend.models import (
    Batch, Category, Employee, EmployeeDiscountRule, EmployeePurchase, EmployeePurchaseLine, EmployeePurchasePayment, Location, Product,
    ProductVariant, StockBalance, StockMovement, User,
)
from backend.services import audit_service, inventory_service, ledger, pricing_service, settings_service
from backend.services.errors import BusinessRuleError, ConflictError, NotFoundError, SelfApprovalError
from backend.services.ledger import Spec
from backend.services.scope import can_access_location

METHODS = ("cash", "bank_transfer", "card", "other")
Q2 = Decimal("0.01")
CREDIT_ALLOWED_KEY = "employees.credit_allowed"          # "true" / "false": may an employee buy on credit at all?
MONTHLY_CAP_KEY = "employees.monthly_purchase_cap"       # most an employee may buy in a calendar month (0 = no cap)
settings_service.DEFAULTS.update({CREDIT_ALLOWED_KEY: "false", MONTHLY_CAP_KEY: "0"})


def q2(x) -> Decimal:
    return Decimal(str(x)).quantize(Q2, rounding=ROUND_HALF_UP)


# ---- settings ---------------------------------------------------------------------------------------------------------------------------
def settings(db: Session) -> dict:
    return {"credit_allowed": str(settings_service.get(db, CREDIT_ALLOWED_KEY)).lower() == "true",
            "monthly_purchase_cap": str(settings_service.get(db, MONTHLY_CAP_KEY))}


def set_settings(db: Session, *, actor: User, values: dict, reason: str | None, ip: str | None) -> dict:
    before = settings(db)
    if values.get("credit_allowed") is not None:
        settings_service._set(db, CREDIT_ALLOWED_KEY, "true" if values["credit_allowed"] else "false", actor)
    if values.get("monthly_purchase_cap") is not None:
        cap = Decimal(str(values["monthly_purchase_cap"]))
        if cap < 0:
            raise BusinessRuleError("The monthly cap cannot be negative (use 0 for no cap).")
        settings_service._set(db, MONTHLY_CAP_KEY, str(q2(cap)), actor)
    after = settings(db)
    audit_service.record(db, actor=actor, action="employee_settings.changed", entity_type="setting", entity_id="employees",
                         reason=reason, before=before, after=after, ip=ip)
    return after


# ---- employees --------------------------------------------------------------------------------------------------------------------------
def get_employee(db: Session, employee_id: int, *, lock: bool = False) -> Employee:
    q = select(Employee).where(Employee.id == employee_id)
    if lock:
        q = q.with_for_update().execution_options(populate_existing=True)
    e = db.scalar(q)
    if e is None:
        raise NotFoundError("Employee not found")
    return e


def checked(db: Session, user: User, employee_id: int) -> Employee:
    """Employees outside the caller's locations answer 'not found'."""
    e = get_employee(db, employee_id)
    if not can_access_location(user, e.location_id):
        raise NotFoundError("Employee not found")
    return e


def _clean(v: str | None) -> str | None:
    v = (v or "").strip()
    return v or None


def _check_links(db: Session, *, location_id: int, user_id: int | None, device_user_id: str | None, own_id: int | None) -> None:
    if db.get(Location, location_id) is None:
        raise NotFoundError("Location not found")
    if user_id is not None:
        if db.get(User, user_id) is None:
            raise NotFoundError("User account not found")
        other = db.scalar(select(Employee).where(Employee.user_id == user_id, Employee.id != (own_id or 0)))
        if other is not None:
            raise ConflictError(f"That user account is already linked to {other.code} {other.full_name}.")
    if device_user_id:
        other = db.scalar(select(Employee).where(Employee.device_user_id == device_user_id, Employee.id != (own_id or 0)))
        if other is not None:
            raise ConflictError(f"Attendance-device id {device_user_id} already belongs to {other.code} {other.full_name}.")


def create_employee(db: Session, *, actor: User, values: dict, ip: str | None) -> Employee:
    name = (values.get("full_name") or "").strip()
    if not name:
        raise BusinessRuleError("Enter the employee's name.")
    device = _clean(values.get("device_user_id"))
    _check_links(db, location_id=values["location_id"], user_id=values.get("user_id"), device_user_id=device, own_id=None)
    limit = q2(values.get("credit_limit") or 0)
    if limit < 0:
        raise BusinessRuleError("The credit limit cannot be negative.")
    e = Employee(code=ledger.next_ref(db, "employee_code_seq", "EMP"), full_name=name, designation=_clean(values.get("designation")),
                 phone=_clean(values.get("phone")), location_id=values["location_id"], user_id=values.get("user_id"), device_user_id=device,
                 joined_on=values.get("joined_on"), credit_limit=limit, notes=_clean(values.get("notes")), created_by=actor.id)
    db.add(e)
    db.flush()
    audit_service.record(db, actor=actor, action="employee.created", entity_type="employee", entity_id=e.id, location_id=e.location_id,
                         after={"code": e.code, "name": e.full_name, "location_id": e.location_id, "device_user_id": e.device_user_id,
                                "credit_limit": str(e.credit_limit)}, ip=ip)
    if device:                                                   # punches already stored for this device number now belong to the new employee
        from backend.services import attendance_service       # late import: attendance depends on employees
        attendance_service.match_unmatched(db, actor=actor, device_user_id=device, ip=ip)
    return e


def update_employee(db: Session, *, actor: User, employee_id: int, values: dict, reason: str | None, ip: str | None) -> Employee:
    e = get_employee(db, employee_id, lock=True)
    before = {"name": e.full_name, "location_id": e.location_id, "device_user_id": e.device_user_id, "user_id": e.user_id,
              "status": e.status, "credit_limit": str(e.credit_limit), "designation": e.designation}
    new_loc = values.get("location_id", e.location_id)
    new_user = values["user_id"] if "user_id" in values else e.user_id
    new_dev = _clean(values["device_user_id"]) if "device_user_id" in values else e.device_user_id
    _check_links(db, location_id=new_loc, user_id=new_user, device_user_id=new_dev, own_id=e.id)
    if values.get("status") not in (None, "active", "inactive"):
        raise BusinessRuleError("Status must be active or inactive.")
    if values.get("credit_limit") is not None:
        if Decimal(str(values["credit_limit"])) < 0:
            raise BusinessRuleError("The credit limit cannot be negative.")
        e.credit_limit = q2(values["credit_limit"])
    for k in ("full_name", "designation", "phone", "notes"):
        if k in values and values[k] is not None:
            setattr(e, k, values[k].strip() if k == "full_name" else _clean(values[k]))
    if not e.full_name:
        raise BusinessRuleError("The employee's name cannot be empty.")
    e.location_id, e.user_id, e.device_user_id = new_loc, new_user, new_dev
    for k in ("joined_on", "left_on", "status"):
        if k in values:
            setattr(e, k, values[k])
    if e.status == "inactive" and e.left_on is None:
        e.left_on = date.today()
    if e.status == "active":
        e.left_on = None
    db.flush()
    audit_service.record(db, actor=actor, action="employee.updated", entity_type="employee", entity_id=e.id, location_id=e.location_id, reason=reason,
                         before=before, after={"name": e.full_name, "location_id": e.location_id, "device_user_id": e.device_user_id, "user_id": e.user_id,
                                               "status": e.status, "credit_limit": str(e.credit_limit), "designation": e.designation}, ip=ip)
    if new_dev != before["device_user_id"] and new_dev:
        from backend.services import attendance_service       # late import: attendance depends on employees
        attendance_service.match_unmatched(db, actor=actor, device_user_id=new_dev, ip=ip)
    return e


def outstanding(db: Session, employee_id: int) -> Decimal:
    """What the employee owes: completed purchases less payments that have not been reversed. Derived, never stored."""
    owed = db.scalar(select(func.coalesce(func.sum(EmployeePurchase.total), 0)).where(EmployeePurchase.employee_id == employee_id,
                                                                                         EmployeePurchase.status == "completed"))
    paid = db.scalar(select(func.coalesce(func.sum(EmployeePurchasePayment.amount), 0))
                     .join(EmployeePurchase, EmployeePurchase.id == EmployeePurchasePayment.purchase_id)
                     .where(EmployeePurchasePayment.employee_id == employee_id, EmployeePurchasePayment.status == "posted",
                            EmployeePurchase.status == "completed"))
    return q2(Decimal(owed) - Decimal(paid))


def purchase_paid(p: EmployeePurchase) -> Decimal:
    return q2(sum((Decimal(x.amount) for x in p.payments if x.status == "posted"), Decimal(0)))


def purchase_balance(p: EmployeePurchase) -> Decimal:
    return q2(Decimal(p.total) - purchase_paid(p)) if p.status == "completed" else Decimal(0)


# ---- discount rules ---------------------------------------------------------------------------------------------------------------------
def _validate_pct(pct) -> Decimal:
    d = Decimal(str(pct))
    if d <= 0 or d > 100:
        raise BusinessRuleError("A discount must be above 0 and at most 100 percent.")
    return q2(d)


def create_rule(db: Session, *, actor: User, name: str, category_id: int | None, pct, ip: str | None) -> EmployeeDiscountRule:
    if not (name or "").strip():
        raise BusinessRuleError("Give the rule a name.")
    if category_id is not None and db.get(Category, category_id) is None:
        raise NotFoundError("Category not found")
    p = _validate_pct(pct)
    exists = db.scalar(select(EmployeeDiscountRule).where(EmployeeDiscountRule.is_active.is_(True),
                                                          func.coalesce(EmployeeDiscountRule.category_id, 0) == (category_id or 0)))
    if exists is not None:
        raise ConflictError(f"'{exists.name}' already applies to {'this category' if category_id else 'all products'}. Change or switch it off first.")
    r = EmployeeDiscountRule(name=name.strip(), category_id=category_id, pct=p, created_by=actor.id)
    db.add(r)
    db.flush()
    audit_service.record(db, actor=actor, action="employee_discount.created", entity_type="employee_discount_rule", entity_id=r.id,
                         after={"name": r.name, "category_id": category_id, "pct": str(p)}, ip=ip)
    return r


def update_rule(db: Session, *, actor: User, rule_id: int, values: dict, reason: str | None, ip: str | None) -> EmployeeDiscountRule:
    r = db.get(EmployeeDiscountRule, rule_id)
    if r is None:
        raise NotFoundError("Rule not found")
    before = {"name": r.name, "pct": str(r.pct), "is_active": r.is_active}
    if values.get("pct") is not None:
        r.pct = _validate_pct(values["pct"])
    if values.get("name"):
        r.name = values["name"].strip()
    if values.get("is_active") is not None:
        if values["is_active"] and not r.is_active:
            clash = db.scalar(select(EmployeeDiscountRule).where(EmployeeDiscountRule.is_active.is_(True), EmployeeDiscountRule.id != r.id,
                                                                 func.coalesce(EmployeeDiscountRule.category_id, 0) == (r.category_id or 0)))
            if clash is not None:
                raise ConflictError(f"'{clash.name}' already applies to the same products.")
        r.is_active = values["is_active"]
    db.flush()
    audit_service.record(db, actor=actor, action="employee_discount.updated", entity_type="employee_discount_rule", entity_id=r.id, reason=reason,
                         before=before, after={"name": r.name, "pct": str(r.pct), "is_active": r.is_active}, ip=ip)
    return r


def _discount_for(variant: ProductVariant, rules: list[EmployeeDiscountRule]) -> tuple[Decimal, str]:
    """(discount per unit, source). The price list wins when the variant has its own, lower, employee price; otherwise the most
    specific active percentage rule; otherwise none."""
    lp = pricing_service.list_price(variant)
    if variant.employee_price is not None and Decimal(variant.employee_price) < lp:
        return q2(lp - Decimal(variant.employee_price)), "price_list"
    by_cat = {r.category_id: r for r in rules if r.is_active}
    rule = by_cat.get(variant.product.category_id) or by_cat.get(None)
    if rule is not None:
        return min(q2(lp * Decimal(rule.pct) / 100), lp), f"rule:{rule.id}"
    return Decimal(0), "none"


def quote(db: Session, *, lines: list[dict]) -> dict:
    if not lines:
        raise BusinessRuleError("Add at least one item.")
    rules = list(db.scalars(select(EmployeeDiscountRule).where(EmployeeDiscountRule.is_active.is_(True))).all())
    rate = settings_service.sales_value(db, settings_service.TAX_RATE_KEY)
    out, seen = [], set()
    for ln in lines:
        qty = int(ln["qty"])
        if qty <= 0:
            raise BusinessRuleError("Quantities must be above zero.")
        v = db.get(ProductVariant, ln["variant_id"])
        if v is None:
            raise NotFoundError(f"Item {ln['variant_id']} not found")
        if ln["variant_id"] in seen:
            raise BusinessRuleError(f"{v.sku} is listed twice; combine the quantities.")
        seen.add(ln["variant_id"])
        disc, source = _discount_for(v, rules)
        lp = pricing_service.list_price(v)
        out.append({"variant_id": v.id, "sku": v.sku, "name": f"{v.product.name} {v.name}".strip(), "qty": qty, "list_price": lp, "unit_discount": disc,
                    "unit_price": lp - disc, "discount_source": source, "line_total": (lp - disc) * qty})
    subtotal = q2(sum((l["list_price"] * l["qty"] for l in out), Decimal(0)))
    net = q2(sum((l["line_total"] for l in out), Decimal(0)))
    tax = q2(net * rate / 100)
    return {"lines": out, "subtotal_list": subtotal, "discount_total": subtotal - net, "net": net, "tax_rate_pct": rate, "tax": tax, "total": net + tax}


# ---- purchases --------------------------------------------------------------------------------------------------------------------------
def get_purchase(db: Session, purchase_id: int, *, lock: bool = False) -> EmployeePurchase:
    q = select(EmployeePurchase).where(EmployeePurchase.id == purchase_id)
    if lock:
        q = q.with_for_update().execution_options(populate_existing=True)
    p = db.scalar(q)
    if p is None:
        raise NotFoundError("Employee purchase not found")
    return p


def purchase_checked(db: Session, user: User, purchase_id: int, *, lock: bool = False) -> EmployeePurchase:
    p = get_purchase(db, purchase_id, lock=lock)
    if not can_access_location(user, p.location_id):
        raise NotFoundError("Employee purchase not found")
    return p


def _no_self(actor: User, e: Employee, what: str) -> None:
    if e.user_id is not None and e.user_id == actor.id:
        raise SelfApprovalError(f"You cannot {what} your own employee purchase.")


def _method(method: str | None, reference: str | None) -> str:
    if method not in METHODS:
        raise BusinessRuleError(f"Payment method must be one of: {', '.join(METHODS)}.")
    if method in ("bank_transfer", "card") and not (reference or "").strip():
        raise BusinessRuleError("A transaction reference is required for bank transfers and cards.")
    return method


def _month_total(db: Session, employee_id: int, on: date, exclude_id: int | None = None) -> Decimal:
    start = on.replace(day=1)
    end = date(start.year + (start.month == 12), (start.month % 12) + 1, 1)
    return Decimal(db.scalar(select(func.coalesce(func.sum(EmployeePurchase.total), 0)).where(
        EmployeePurchase.employee_id == employee_id, EmployeePurchase.status.in_(("completed", "pending_approval")),
        EmployeePurchase.purchase_date >= start, EmployeePurchase.purchase_date < end, EmployeePurchase.id != (exclude_id or 0))))


def _check_limits(db: Session, e: Employee, total: Decimal, down: Decimal, on: date, exclude_id: int | None = None) -> None:
    cap = Decimal(str(settings_service.get(db, MONTHLY_CAP_KEY)))
    if cap > 0 and _month_total(db, e.id, on, exclude_id) + total > cap:
        raise BusinessRuleError(f"This would take {e.full_name} above the monthly purchase cap of {cap}.")
    credit = total - down
    if credit > 0:
        if not settings(db)["credit_allowed"]:
            raise BusinessRuleError("Employee credit is not allowed. Receive the full amount now, or ask the CEO to allow employee credit in the settings.")
        owed = outstanding(db, e.id)
        if Decimal(e.credit_limit) <= 0:
            raise BusinessRuleError(f"{e.full_name} has no credit limit set.")
        if owed + credit > Decimal(e.credit_limit):
            raise BusinessRuleError(f"{e.full_name} owes {owed} and has a credit limit of {e.credit_limit}; {credit} more would exceed it.")


def _availability(db: Session, location_id: int, variant: ProductVariant, qty: int, on: date) -> list[tuple[int | None, int]]:
    """First-expiring-first-out allocation of good, unreserved, unexpired stock. Returns [(batch_id, qty)]. Raises if not enough."""
    rows = db.execute(select(StockBalance, Batch.expiry_date).outerjoin(Batch, Batch.id == StockBalance.batch_id)
                      .where(StockBalance.location_id == location_id, StockBalance.variant_id == variant.id)).all()
    usable = []
    for bal, expiry in rows:
        free = bal.qty_good - bal.qty_reserved
        if free > 0 and not (expiry is not None and expiry < on):
            usable.append((bal.batch_id, free, expiry))
    usable.sort(key=lambda r: (r[2] is None, r[2] or date.max, r[0] or 0))
    out, need = [], qty
    for batch_id, free, _ in usable:
        take = min(free, need)
        out.append((batch_id, take))
        need -= take
        if need == 0:
            break
    if need:
        have = qty - need
        raise BusinessRuleError(f"Not enough unreserved, unexpired stock of {variant.sku} at this location: {have} available, {qty} requested.")
    return out


def _issue(db: Session, p: EmployeePurchase, actor: User) -> None:
    """Take the goods out of stock and make the purchase final."""
    emp = get_employee(db, p.employee_id)
    allocated: list[tuple[EmployeePurchaseLine, int | None, int]] = []
    for ln in list(p.lines):
        v = db.get(ProductVariant, ln.variant_id)
        for batch_id, n in _availability(db, p.location_id, v, ln.qty, p.purchase_date):
            allocated.append((ln, batch_id, n))
    specs = [Spec(p.location_id, ln.variant_id, b, "good", -n, "out") for ln, b, n in sorted(allocated, key=lambda t: (t[0].variant_id, t[1] or 0))]
    txn = ledger.post(db, txn_type="employee_purchase", location_id=p.location_id, specs=specs, actor=actor,
                      reason=f"Employee purchase {p.ref} ({emp.code})", source_type="employee_purchase", source_id=p.id, ref_prefix="EPS")
    values: dict[tuple[int, int | None], Decimal] = {}
    for m in db.scalars(select(StockMovement).where(StockMovement.txn_id == txn.id)).all():
        k = (m.variant_id, m.batch_id)
        values[k] = values.get(k, Decimal(0)) + (-Decimal(m.value_delta))
    originals = {ln.id: ln for ln in p.lines}
    p.lines.clear()
    db.flush()
    for ln, b, n in allocated:
        o = originals[ln.id]
        p.lines.append(EmployeePurchaseLine(variant_id=o.variant_id, batch_id=b, qty=n, list_price=o.list_price, unit_discount=o.unit_discount,
                                            unit_price=o.unit_price, discount_source=o.discount_source, stock_value=values[(o.variant_id, b)]))
    p.stock_txn_id, p.status, p.completed_at = txn.id, "completed", datetime.now(timezone.utc)
    db.flush()
    if Decimal(p.down_payment) > 0:
        pay = EmployeePurchasePayment(ref=ledger.next_ref(db, "employee_payment_ref_seq", "EPY"), purchase_id=p.id, employee_id=p.employee_id,
                                      location_id=p.location_id, method=p.down_payment_method, amount=p.down_payment, paid_date=p.purchase_date,
                                      reference=p.down_payment_reference, created_by=p.created_by)
        p.payments.append(pay)                      # the session does not autoflush or expire, so keep the loaded collection current
        db.flush()


def create_purchase(db: Session, *, actor: User, employee_id: int, location_id: int, purchase_date: date, lines: list[dict], down_payment,
                    method: str | None, reference: str | None, receipt_ref: str | None, notes: str | None, idempotency_key: str | None,
                    ip: str | None) -> EmployeePurchase:
    if idempotency_key:
        ex = db.scalar(select(EmployeePurchase).where(EmployeePurchase.idempotency_key == idempotency_key))
        if ex is not None:
            ex.replayed = True  # type: ignore[attr-defined]
            return ex
    e = get_employee(db, employee_id, lock=True)
    if e.status != "active":
        raise BusinessRuleError(f"{e.full_name} is not an active employee.")
    _no_self(actor, e, "record")
    if db.get(Location, location_id) is None:
        raise NotFoundError("Location not found")
    if purchase_date > date.today():
        raise BusinessRuleError("The purchase date cannot be in the future.")
    qt = quote(db, lines=lines)
    total = q2(qt["total"])
    down = q2(down_payment or 0)
    if down < 0 or down > total:
        raise BusinessRuleError("The amount received now cannot be negative or more than the total.")
    if down > 0:
        method = _method(method, reference)
    _check_limits(db, e, total, down, purchase_date)
    for ln in qt["lines"]:                                         # fail early when stock is short (the real check is when goods are issued)
        _availability(db, location_id, db.get(ProductVariant, ln["variant_id"]), ln["qty"], purchase_date)
    p = EmployeePurchase(ref=ledger.next_ref(db, "employee_purchase_ref_seq", "EPU"), employee_id=e.id, location_id=location_id,
                         purchase_date=purchase_date, status="pending_approval", receipt_ref=_clean(receipt_ref), subtotal_list=qt["subtotal_list"],
                         discount_total=qt["discount_total"], net=qt["net"], tax_rate_pct=qt["tax_rate_pct"], tax=qt["tax"], total=total,
                         down_payment=down, down_payment_method=method if down > 0 else None, down_payment_reference=_clean(reference) if down > 0 else None,
                         notes=_clean(notes), idempotency_key=idempotency_key, created_by=actor.id)
    for ln in qt["lines"]:
        p.lines.append(EmployeePurchaseLine(variant_id=ln["variant_id"], qty=ln["qty"], list_price=ln["list_price"], unit_discount=ln["unit_discount"],
                                            unit_price=ln["unit_price"], discount_source=ln["discount_source"]))
    db.add(p)
    try:
        db.flush()
    except IntegrityError:
        db.rollback()
        raise ConflictError("This purchase was already recorded (duplicate submission).")
    if down == total:
        _issue(db, p, actor)
    audit_service.record(db, actor=actor, action="employee_purchase.recorded", entity_type="employee_purchase", entity_id=p.id, location_id=location_id,
                         after={"ref": p.ref, "employee": e.code, "total": str(total), "discount": str(p.discount_total), "received_now": str(down),
                                "on_credit": str(total - down), "status": p.status, "receipt_ref": p.receipt_ref}, ip=ip)
    p.replayed = False  # type: ignore[attr-defined]
    return p


def decide_credit(db: Session, *, actor: User, purchase_id: int, approve: bool, note: str | None, ip: str | None) -> EmployeePurchase:
    p = get_purchase(db, purchase_id, lock=True)
    if p.status != "pending_approval":
        raise ConflictError(f"{p.ref} is {p.status.replace('_', ' ')}; it is not waiting for a decision.")
    e = get_employee(db, p.employee_id, lock=True)
    if p.created_by == actor.id:
        raise SelfApprovalError("You cannot decide on a purchase you recorded yourself. Ask another authorised person.")
    _no_self(actor, e, "decide on")
    if not approve and not (note or "").strip():
        raise BusinessRuleError("Say why the purchase is being rejected.")
    p.decided_by, p.decided_at, p.decision_note = actor.id, datetime.now(timezone.utc), _clean(note)
    if approve:
        if e.status != "active":
            raise BusinessRuleError(f"{e.full_name} is no longer an active employee.")
        _check_limits(db, e, Decimal(p.total), Decimal(p.down_payment), p.purchase_date, exclude_id=p.id)
        _issue(db, p, actor)
    else:
        p.status = "rejected"
    db.flush()
    audit_service.record(db, actor=actor, action="employee_purchase.approved" if approve else "employee_purchase.rejected", entity_type="employee_purchase",
                         entity_id=p.id, location_id=p.location_id, reason=note, after={"ref": p.ref, "status": p.status, "total": str(p.total)}, ip=ip)
    return p


def record_payment(db: Session, *, actor: User, purchase_id: int, amount, method: str, paid_date: date, reference: str | None,
                   idempotency_key: str | None, ip: str | None) -> EmployeePurchasePayment:
    if idempotency_key:
        ex = db.scalar(select(EmployeePurchasePayment).where(EmployeePurchasePayment.idempotency_key == idempotency_key))
        if ex is not None:
            return ex
    p = get_purchase(db, purchase_id, lock=True)
    e = get_employee(db, p.employee_id)
    if p.status != "completed":
        raise BusinessRuleError(f"{p.ref} is {p.status.replace('_', ' ')}; payments can only be recorded on a completed purchase.")
    _no_self(actor, e, "record a payment for")
    amt = q2(amount)
    if amt <= 0:
        raise BusinessRuleError("The amount must be above zero.")
    if paid_date > date.today():
        raise BusinessRuleError("The payment date cannot be in the future.")
    method = _method(method, reference)
    bal = purchase_balance(p)
    if amt > bal:
        raise BusinessRuleError(f"{amt} is more than the {bal} still owed on {p.ref}. Record only what is owed.")
    pay = EmployeePurchasePayment(ref=ledger.next_ref(db, "employee_payment_ref_seq", "EPY"), purchase_id=p.id, employee_id=p.employee_id,
                                  location_id=p.location_id, method=method, amount=amt, paid_date=paid_date, reference=_clean(reference),
                                  idempotency_key=idempotency_key, created_by=actor.id)
    p.payments.append(pay)
    db.flush()
    audit_service.record(db, actor=actor, action="employee_payment.recorded", entity_type="employee_purchase", entity_id=p.id, location_id=p.location_id,
                         after={"payment": pay.ref, "amount": str(amt), "method": method, "balance_after": str(bal - amt)}, ip=ip)
    return pay


def _reverse_payment_row(pay: EmployeePurchasePayment, actor: User, reason: str) -> None:
    pay.status, pay.reversed_by, pay.reversed_at, pay.reverse_reason = "reversed", actor.id, datetime.now(timezone.utc), reason


def reverse_payment(db: Session, *, actor: User, payment_id: int, reason: str, ip: str | None) -> EmployeePurchasePayment:
    pay = db.scalar(select(EmployeePurchasePayment).where(EmployeePurchasePayment.id == payment_id).with_for_update()
                    .execution_options(populate_existing=True))
    if pay is None:
        raise NotFoundError("Payment not found")
    p = get_purchase(db, pay.purchase_id, lock=True)
    if pay.status != "posted":
        raise ConflictError(f"{pay.ref} is already reversed.")
    if pay.created_by == actor.id:
        raise SelfApprovalError("You cannot reverse a payment you recorded yourself.")
    if not (reason or "").strip():
        raise BusinessRuleError("Say why the payment is being reversed.")
    _reverse_payment_row(pay, actor, reason.strip())
    db.flush()
    audit_service.record(db, actor=actor, action="employee_payment.reversed", entity_type="employee_purchase", entity_id=p.id, location_id=p.location_id,
                         reason=reason, after={"payment": pay.ref, "amount": str(pay.amount)}, ip=ip)
    return pay


def void_purchase(db: Session, *, actor: User, purchase_id: int, reason: str, ip: str | None) -> EmployeePurchase:
    """Cancel a completed purchase as a whole: the goods come back into stock, every payment is reversed in the books, and the sale
    is reversed by the posting engine. (The money itself has to be handed back to the employee outside the system.)"""
    p = get_purchase(db, purchase_id, lock=True)
    e = get_employee(db, p.employee_id)
    if p.status != "completed":
        raise ConflictError(f"{p.ref} is {p.status.replace('_', ' ')}; only a completed purchase can be voided.")
    if p.created_by == actor.id:
        raise SelfApprovalError("You cannot void a purchase you recorded yourself. Ask another authorised person.")
    _no_self(actor, e, "void")
    if not (reason or "").strip():
        raise BusinessRuleError("Say why the purchase is being voided.")
    inventory_service.reverse_transaction(db, actor=actor, txn_id=p.stock_txn_id, reason=f"Void {p.ref}: {reason.strip()}", ip=ip)
    for pay in p.payments:
        if pay.status == "posted":
            _reverse_payment_row(pay, actor, f"Purchase {p.ref} voided")
    p.status, p.voided_by, p.voided_at, p.void_reason = "voided", actor.id, datetime.now(timezone.utc), reason.strip()
    db.flush()
    audit_service.record(db, actor=actor, action="employee_purchase.voided", entity_type="employee_purchase", entity_id=p.id, location_id=p.location_id,
                         reason=reason, after={"ref": p.ref, "total": str(p.total)}, ip=ip)
    return p
