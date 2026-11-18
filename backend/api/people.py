"""Employees, staff discounts, employee purchases (router prefix /employees) and biometric attendance (router prefix /attendance).

Personnel data is restricted: employee records need employees.view; what an employee owes needs employees.purchase_view; the
staff taking a purchase at the counter (employees.purchase) get only a name list of active staff at their own locations; attendance
needs attendance.view and is never visible to accounts, order bookers or stock keepers by default. Out-of-scope records answer 404."""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from backend.api.common import commit_or_http
from backend.attendance import registry
from backend.db import get_db
from backend.deps import Principal, client_ip, ensure_location_access, require, require_any
from backend.models import (
    AttendanceCorrection, AttendanceDevice, AttendanceImport, AttendancePolicy, AttendancePunch, Category, Employee, EmployeeDiscountRule, EmployeePurchase,
    Location, ProductVariant, User,
)
from backend.schemas import people as S
from backend.services import attendance_service as att, employee_service as es
from backend.services.errors import BusinessRuleError, NotFoundError
from backend.services.scope import can_access_location, visible_location_ids

router = APIRouter(tags=["people"])


def _nf(fn):
    try:
        return fn()
    except NotFoundError as e:
        raise HTTPException(404, str(e))
    except BusinessRuleError as e:
        raise HTTPException(422, str(e))


def _names(db: Session, ids: set) -> dict[int, str]:
    ids = {i for i in ids if i}
    return {u.id: u.username for u in db.scalars(select(User).where(User.id.in_(ids))).all()} if ids else {}


def _scoped(q, p: Principal, col):
    ids = visible_location_ids(p.user)
    return q if ids is None else q.where(col.in_(ids))


# =============================================================================================================================
# Employees
# =============================================================================================================================
def _emp_out(db: Session, e: Employee, p: Principal, detail: bool = False) -> dict:
    out = {"id": e.id, "code": e.code, "full_name": e.full_name, "designation": e.designation, "location_id": e.location_id, "status": e.status,
           "device_user_id": e.device_user_id, "user_id": e.user_id, "joined_on": e.joined_on, "left_on": e.left_on, "phone": e.phone}
    if p.has("employees.purchase_view"):
        out["credit_limit"] = str(e.credit_limit)
        out["outstanding"] = str(es.outstanding(db, e.id))
    if detail:
        out["notes"] = e.notes
    return out


@router.get("/employees")
def list_employees(status: str | None = None, location_id: int | None = None, q: str | None = None, page: int = Query(1, ge=1),
                   page_size: int = Query(100, ge=1, le=500), p: Principal = Depends(require("employees.view")), db: Session = Depends(get_db)):
    stmt = _scoped(select(Employee), p, Employee.location_id)
    if status:
        stmt = stmt.where(Employee.status == status)
    if location_id:
        stmt = stmt.where(Employee.location_id == location_id)
    if q:
        like = f"%{q.strip().lower()}%"
        stmt = stmt.where(func.lower(Employee.full_name).like(like) | func.lower(Employee.code).like(like))
    total = db.scalar(select(func.count()).select_from(stmt.subquery())) or 0
    rows = db.scalars(stmt.order_by(Employee.full_name).offset((page - 1) * page_size).limit(page_size)).all()
    return {"items": [_emp_out(db, e, p) for e in rows], "total": total}


@router.post("/employees", status_code=201)
def create_employee(body: S.EmployeeIn, request: Request, p: Principal = Depends(require("employees.manage")), db: Session = Depends(get_db)):
    ensure_location_access(p, body.location_id)
    e = commit_or_http(db, lambda: es.create_employee(db, actor=p.user, values=body.model_dump(), ip=client_ip(request)))
    return _emp_out(db, e, p, True)


@router.get("/employees/lookup")
def lookup(p: Principal = Depends(require_any("employees.purchase", "employees.purchase_view", "employees.view")), db: Session = Depends(get_db)):
    """A name list of active staff at the caller's locations, for the counter. No personal or financial detail."""
    stmt = _scoped(select(Employee).where(Employee.status == "active"), p, Employee.location_id).order_by(Employee.full_name)
    return [{"id": e.id, "code": e.code, "full_name": e.full_name} for e in db.scalars(stmt).all()]


@router.get("/employees/settings")
def get_settings(p: Principal = Depends(require_any("employees.manage_discounts", "employees.purchase_view", "employees.purchase")), db: Session = Depends(get_db)):
    return es.settings(db)


@router.put("/employees/settings")
def put_settings(body: S.EmployeeSettingsIn, request: Request, p: Principal = Depends(require("employees.manage_discounts")), db: Session = Depends(get_db)):
    vals = body.model_dump(exclude={"reason"})
    return commit_or_http(db, lambda: es.set_settings(db, actor=p.user, values=vals, reason=body.reason, ip=client_ip(request)))


def _rule_out(db: Session, r: EmployeeDiscountRule, cats: dict) -> dict:
    return {"id": r.id, "name": r.name, "category_id": r.category_id, "category": cats.get(r.category_id) if r.category_id else "All products",
            "pct": str(r.pct), "is_active": r.is_active}


@router.get("/employees/discount-rules")
def list_rules(p: Principal = Depends(require_any("employees.manage_discounts", "employees.purchase_view", "employees.purchase")), db: Session = Depends(get_db)):
    cats = {c.id: c.name for c in db.scalars(select(Category)).all()}
    return [_rule_out(db, r, cats) for r in db.scalars(select(EmployeeDiscountRule).order_by(EmployeeDiscountRule.id)).all()]


@router.post("/employees/discount-rules", status_code=201)
def create_rule(body: S.RuleIn, request: Request, p: Principal = Depends(require("employees.manage_discounts")), db: Session = Depends(get_db)):
    r = commit_or_http(db, lambda: es.create_rule(db, actor=p.user, name=body.name, category_id=body.category_id, pct=body.pct, ip=client_ip(request)))
    return _rule_out(db, r, {c.id: c.name for c in db.scalars(select(Category)).all()})


@router.patch("/employees/discount-rules/{rule_id}")
def patch_rule(rule_id: int, body: S.RulePatch, request: Request, p: Principal = Depends(require("employees.manage_discounts")), db: Session = Depends(get_db)):
    vals = body.model_dump(exclude_unset=True, exclude={"reason"})
    r = commit_or_http(db, lambda: es.update_rule(db, actor=p.user, rule_id=rule_id, values=vals, reason=body.reason, ip=client_ip(request)))
    return _rule_out(db, r, {c.id: c.name for c in db.scalars(select(Category)).all()})


# ---- purchases ------------------------------------------------------------------------------------------------------------------
def _purchase_out(db: Session, pu: EmployeePurchase, detail: bool = True) -> dict:
    emp = db.get(Employee, pu.employee_id)
    names = _names(db, {pu.created_by, pu.decided_by, pu.voided_by})
    out = {"id": pu.id, "ref": pu.ref, "employee_id": pu.employee_id, "employee_code": emp.code, "employee": emp.full_name, "location_id": pu.location_id,
           "purchase_date": pu.purchase_date, "status": pu.status, "receipt_ref": pu.receipt_ref, "subtotal_list": str(pu.subtotal_list),
           "discount_total": str(pu.discount_total), "net": str(pu.net), "tax": str(pu.tax), "total": str(pu.total), "received_now": str(pu.down_payment),
           "paid": str(es.purchase_paid(pu)), "balance": str(es.purchase_balance(pu)), "created_by": names.get(pu.created_by),
           "decided_by": names.get(pu.decided_by), "voided_by": names.get(pu.voided_by), "decision_note": pu.decision_note, "void_reason": pu.void_reason}
    if detail:
        vids = {l.variant_id for l in pu.lines}
        vs = {v.id: v for v in db.scalars(select(ProductVariant).where(ProductVariant.id.in_(vids))).all()} if vids else {}
        out["lines"] = [{"variant_id": l.variant_id, "sku": vs[l.variant_id].sku, "name": f"{vs[l.variant_id].product.name} {vs[l.variant_id].name}".strip(),
                         "batch_id": l.batch_id, "qty": l.qty, "list_price": str(l.list_price), "unit_discount": str(l.unit_discount),
                         "unit_price": str(l.unit_price), "discount_source": l.discount_source, "line_total": str(Decimal(l.unit_price) * l.qty)} for l in pu.lines]
        out["payments"] = [{"id": x.id, "ref": x.ref, "method": x.method, "amount": str(x.amount), "paid_date": x.paid_date, "reference": x.reference,
                            "status": x.status, "reverse_reason": x.reverse_reason} for x in pu.payments]
        out["notes"] = pu.notes
    return out


def _can_see_purchase(p: Principal, pu: EmployeePurchase) -> bool:
    if not can_access_location(p.user, pu.location_id):
        return False
    return p.has("employees.purchase_view") or (p.has("employees.purchase") and pu.created_by == p.user.id)


@router.post("/employees/purchases/quote")
def quote(body: S.QuoteIn, p: Principal = Depends(require("employees.purchase")), db: Session = Depends(get_db)):
    q = _nf(lambda: es.quote(db, lines=[l.model_dump() for l in body.lines]))
    return {**{k: str(v) for k, v in q.items() if k != "lines"},
            "lines": [{k: (str(v) if isinstance(v, Decimal) else v) for k, v in l.items()} for l in q["lines"]]}


@router.get("/employees/purchases")
def list_purchases(status: str | None = None, employee_id: int | None = None, location_id: int | None = None, date_from: date | None = None,
                   date_to: date | None = None, outstanding_only: bool = False, page: int = Query(1, ge=1), page_size: int = Query(100, ge=1, le=500),
                   p: Principal = Depends(require_any("employees.purchase_view", "employees.purchase")), db: Session = Depends(get_db)):
    stmt = _scoped(select(EmployeePurchase), p, EmployeePurchase.location_id)
    if not p.has("employees.purchase_view"):
        stmt = stmt.where(EmployeePurchase.created_by == p.user.id)
    for col, val in ((EmployeePurchase.status, status), (EmployeePurchase.employee_id, employee_id), (EmployeePurchase.location_id, location_id)):
        if val:
            stmt = stmt.where(col == val)
    if date_from:
        stmt = stmt.where(EmployeePurchase.purchase_date >= date_from)
    if date_to:
        stmt = stmt.where(EmployeePurchase.purchase_date <= date_to)
    total = db.scalar(select(func.count()).select_from(stmt.subquery())) or 0
    rows = db.scalars(stmt.order_by(EmployeePurchase.id.desc()).offset((page - 1) * page_size).limit(page_size)).all()
    items = [_purchase_out(db, r, detail=False) for r in rows]
    if outstanding_only:
        items = [i for i in items if Decimal(i["balance"]) > 0]
    return {"items": items, "total": total}


@router.post("/employees/purchases", status_code=201)
def create_purchase(body: S.PurchaseIn, request: Request, p: Principal = Depends(require("employees.purchase")), db: Session = Depends(get_db)):
    ensure_location_access(p, body.location_id)
    emp = db.get(Employee, body.employee_id)
    if emp is None or not can_access_location(p.user, emp.location_id):
        raise HTTPException(404, "Employee not found")
    pu = commit_or_http(db, lambda: es.create_purchase(
        db, actor=p.user, employee_id=body.employee_id, location_id=body.location_id, purchase_date=body.purchase_date,
        lines=[l.model_dump() for l in body.lines], down_payment=body.received_now, method=body.method, reference=body.reference, receipt_ref=body.receipt_ref,
        notes=body.notes, idempotency_key=body.idempotency_key, ip=client_ip(request)))
    return _purchase_out(db, pu)


@router.get("/employees/purchases/{purchase_id}")
def get_purchase(purchase_id: int, p: Principal = Depends(require_any("employees.purchase_view", "employees.purchase")), db: Session = Depends(get_db)):
    pu = _nf(lambda: es.get_purchase(db, purchase_id))
    if not _can_see_purchase(p, pu):
        raise HTTPException(404, "Employee purchase not found")
    return _purchase_out(db, pu)


@router.post("/employees/purchases/{purchase_id}/decision")
def decide(purchase_id: int, body: S.DecisionIn, request: Request, p: Principal = Depends(require("employees.purchase_approve")), db: Session = Depends(get_db)):
    _nf(lambda: es.purchase_checked(db, p.user, purchase_id))
    pu = commit_or_http(db, lambda: es.decide_credit(db, actor=p.user, purchase_id=purchase_id, approve=body.approve, note=body.note, ip=client_ip(request)))
    return _purchase_out(db, pu)


@router.post("/employees/purchases/{purchase_id}/void")
def void(purchase_id: int, body: S.ReasonIn, request: Request, p: Principal = Depends(require("employees.purchase_approve")), db: Session = Depends(get_db)):
    _nf(lambda: es.purchase_checked(db, p.user, purchase_id))
    pu = commit_or_http(db, lambda: es.void_purchase(db, actor=p.user, purchase_id=purchase_id, reason=body.reason, ip=client_ip(request)))
    return _purchase_out(db, pu)


@router.post("/employees/purchases/{purchase_id}/payments", status_code=201)
def pay(purchase_id: int, body: S.PaymentIn, request: Request, p: Principal = Depends(require("employees.purchase_pay")), db: Session = Depends(get_db)):
    _nf(lambda: es.purchase_checked(db, p.user, purchase_id))
    commit_or_http(db, lambda: es.record_payment(db, actor=p.user, purchase_id=purchase_id, amount=body.amount, method=body.method, paid_date=body.paid_date,
                                                 reference=body.reference, idempotency_key=body.idempotency_key, ip=client_ip(request)))
    return _purchase_out(db, es.get_purchase(db, purchase_id))


@router.post("/employees/purchases/payments/{payment_id}/reverse")
def reverse_pay(payment_id: int, body: S.ReasonIn, request: Request, p: Principal = Depends(require("employees.purchase_pay")), db: Session = Depends(get_db)):
    pay_ = commit_or_http(db, lambda: es.reverse_payment(db, actor=p.user, payment_id=payment_id, reason=body.reason, ip=client_ip(request)))
    return _purchase_out(db, es.get_purchase(db, pay_.purchase_id))


@router.get("/employees/{employee_id}")
def get_employee(employee_id: int, p: Principal = Depends(require("employees.view")), db: Session = Depends(get_db)):
    return _emp_out(db, _nf(lambda: es.checked(db, p.user, employee_id)), p, True)


@router.patch("/employees/{employee_id}")
def patch_employee(employee_id: int, body: S.EmployeePatch, request: Request, p: Principal = Depends(require("employees.manage")), db: Session = Depends(get_db)):
    _nf(lambda: es.checked(db, p.user, employee_id))
    vals = body.model_dump(exclude_unset=True, exclude={"reason"})
    if vals.get("location_id") is not None:
        ensure_location_access(p, vals["location_id"])
    e = commit_or_http(db, lambda: es.update_employee(db, actor=p.user, employee_id=employee_id, values=vals, reason=body.reason, ip=client_ip(request)))
    return _emp_out(db, e, p, True)


# =============================================================================================================================
# Attendance
# =============================================================================================================================
att_router = APIRouter(prefix="/attendance", tags=["attendance"])
AV = Depends(require("attendance.view"))
AM = Depends(require("attendance.manage"))


def _device_out(d: AttendanceDevice) -> dict:
    return {"id": d.id, "name": d.name, "location_id": d.location_id, "brand": d.brand, "model": d.model, "protocol": d.protocol, "export_format": d.export_format,
            "adapter": d.adapter, "config": d.config, "is_active": d.is_active, "last_import_at": d.last_import_at}


@att_router.get("/adapters")
def adapters(p: Principal = AV):
    return registry.describe()


@att_router.get("/devices")
def devices(p: Principal = AV, db: Session = Depends(get_db)):
    return [_device_out(d) for d in db.scalars(_scoped(select(AttendanceDevice), p, AttendanceDevice.location_id).order_by(AttendanceDevice.name)).all()]


@att_router.post("/devices", status_code=201)
def create_device(body: S.DeviceIn, request: Request, p: Principal = AM, db: Session = Depends(get_db)):
    ensure_location_access(p, body.location_id)
    return _device_out(commit_or_http(db, lambda: att.create_device(db, actor=p.user, values=body.model_dump(), ip=client_ip(request))))


@att_router.patch("/devices/{device_id}")
def patch_device(device_id: int, body: S.DevicePatch, request: Request, p: Principal = AM, db: Session = Depends(get_db)):
    _nf(lambda: att.checked_device(db, p.user, device_id))
    vals = body.model_dump(exclude_unset=True, exclude={"reason"})
    return _device_out(commit_or_http(db, lambda: att.update_device(db, actor=p.user, device_id=device_id, values=vals, reason=body.reason, ip=client_ip(request))))


@att_router.post("/imports")
async def import_punches(request: Request, device_id: int, filename: str | None = None, dry_run: bool = False, p: Principal = AM, db: Session = Depends(get_db)):
    """The request body is the raw file. Use dry_run=true to see what would happen without saving anything."""
    _nf(lambda: att.checked_device(db, p.user, device_id))
    content = await request.body()
    if len(content) > att.MAX_FILE_BYTES:
        raise HTTPException(413, "The file is too large; export a shorter period.")
    if dry_run:
        return _nf(lambda: att.import_file(db, actor=p.user, device_id=device_id, filename=filename, content=content, dry_run=True, ip=client_ip(request)))
    return commit_or_http(db, lambda: att.import_file(db, actor=p.user, device_id=device_id, filename=filename, content=content, dry_run=False, ip=client_ip(request)))


@att_router.get("/imports")
def imports(limit: int = Query(50, ge=1, le=200), p: Principal = AV, db: Session = Depends(get_db)):
    q = select(AttendanceImport, AttendanceDevice).join(AttendanceDevice, AttendanceDevice.id == AttendanceImport.device_id)
    q = _scoped(q, p, AttendanceDevice.location_id).order_by(AttendanceImport.id.desc()).limit(limit)
    names = _names(db, {i.imported_by for i, _ in db.execute(q).all()})
    return [{"id": i.id, "device": d.name, "filename": i.filename, "created_at": i.created_at, "rows_total": i.rows_total, "rows_new": i.rows_new,
             "rows_duplicate": i.rows_duplicate, "rows_rejected": i.rows_rejected, "rows_unmatched": i.rows_unmatched, "first_punch_at": i.first_punch_at,
             "last_punch_at": i.last_punch_at, "imported_by": names.get(i.imported_by), "errors": i.errors} for i, d in db.execute(q).all()]


@att_router.get("/policies")
def policies(p: Principal = AV, db: Session = Depends(get_db)):
    ids = visible_location_ids(p.user)
    rows = db.scalars(select(AttendancePolicy).order_by(AttendancePolicy.location_id.nulls_first())).all()
    return [att.policy_dict(r) for r in rows if r.location_id is None or ids is None or r.location_id in ids]


@att_router.put("/policies")
def put_policy(body: S.PolicyIn, request: Request, p: Principal = AM, db: Session = Depends(get_db)):
    if body.location_id is None and visible_location_ids(p.user) is not None:
        raise HTTPException(403, "Only a user with company-wide access can set the default policy.")
    if body.location_id is not None:
        ensure_location_access(p, body.location_id)
    vals = body.model_dump(exclude={"reason", "location_id"})
    return att.policy_dict(commit_or_http(db, lambda: att.set_policy(db, actor=p.user, location_id=body.location_id, values=vals, reason=body.reason, ip=client_ip(request))))


class _Tz:
    """Local clock time for a location's policy time zone (cached per request)."""

    def __init__(self, db: Session):
        self.db, self.cache = db, {}

    def hm(self, location_id: int, dt) -> str | None:
        if dt is None:
            return None
        if location_id not in self.cache:
            pol = att.policy_for(self.db, location_id)
            self.cache[location_id] = att.ZoneInfo(pol.timezone) if pol else timezone.utc
        return dt.astimezone(self.cache[location_id]).strftime("%H:%M")


def _day_out(d, e: Employee, tz: _Tz) -> dict:
    return {"first_in_local": tz.hm(d.location_id, d.first_in), "last_out_local": tz.hm(d.location_id, d.last_out), "employee_id": e.id, "code": e.code, "name": e.full_name, "location_id": d.location_id, "work_date": d.work_date, "status": d.status,
            "first_in": d.first_in, "last_out": d.last_out, "punch_count": d.punch_count, "worked_minutes": d.worked_minutes, "late_minutes": d.late_minutes,
            "overtime_minutes": d.overtime_minutes, "exception": d.exception, "exception_text": att.EXCEPTIONS.get(d.exception), "corrected": d.corrected}


@att_router.get("/days")
def days(date_from: date, date_to: date, employee_id: int | None = None, location_id: int | None = None, status: str | None = None,
         p: Principal = AV, db: Session = Depends(get_db)):
    if (date_to - date_from).days > 120:
        raise HTTPException(422, "Ask for at most 120 days at a time.")
    tz = _Tz(db)
    return [_day_out(d, e, tz) for d, e in att.list_days(db, p.user, date_from=date_from, date_to=date_to, employee_id=employee_id, location_id=location_id, status=status)]


@att_router.get("/exceptions")
def exceptions(date_from: date, date_to: date, location_id: int | None = None, p: Principal = AV, db: Session = Depends(get_db)):
    days_ = att.list_days(db, p.user, date_from=date_from, date_to=date_to, location_id=location_id, only_exceptions=True)
    tz = _Tz(db)
    return {"days": [_day_out(d, e, tz) for d, e in days_], "unmatched_device_ids": att.unmatched_ids(db, p.user)}


@att_router.get("/summary")
def summary(date_from: date, date_to: date, location_id: int | None = None, p: Principal = AV, db: Session = Depends(get_db)):
    return att.period_summary(db, p.user, date_from=date_from, date_to=date_to, location_id=location_id)


@att_router.get("/punches")
def punches(employee_id: int, work_date: date, p: Principal = AV, db: Session = Depends(get_db)):
    e = _nf(lambda: es.checked(db, p.user, employee_id))
    pol = att.policy_for(db, e.location_id)
    if pol is None:
        return []
    tz = att.ZoneInfo(pol.timezone)
    lo, hi = att._day_bounds(work_date, tz)
    rows = db.scalars(select(AttendancePunch).where(AttendancePunch.employee_id == e.id, AttendancePunch.punched_at >= lo, AttendancePunch.punched_at < hi)
                      .order_by(AttendancePunch.punched_at)).all()
    return [{"id": r.id, "punched_at": r.punched_at, "local_time": r.punched_at.astimezone(tz).strftime("%H:%M:%S"), "direction": r.direction, "source": r.source,
             "status": r.status, "raw_ref": r.raw_ref} for r in rows]


@att_router.post("/rebuild")
def rebuild(body: S.RebuildIn, request: Request, p: Principal = AM, db: Session = Depends(get_db)):
    if body.location_id:
        ensure_location_access(p, body.location_id)
    ids = {body.location_id} if body.location_id else visible_location_ids(p.user)
    return commit_or_http(db, lambda: att.rebuild(db, date_from=body.date_from, date_to=body.date_to, location_ids=ids))


def _corr_out(db: Session, c: AttendanceCorrection, e: Employee, names: dict) -> dict:
    pol = att.policy_for(db, e.location_id)
    tz = att.ZoneInfo(pol.timezone) if pol else timezone.utc
    return {"id": c.id, "employee_id": e.id, "code": e.code, "name": e.full_name, "work_date": c.work_date, "kind": c.kind, "direction": c.direction,
            "local_time": c.punched_at.astimezone(tz).strftime("%H:%M") if c.punched_at else None, "punch_id": c.punch_id, "reason": c.reason, "status": c.status,
            "requested_by": names.get(c.requested_by), "requested_at": c.requested_at, "decided_by": names.get(c.decided_by), "decided_at": c.decided_at,
            "decision_note": c.decision_note}


@att_router.get("/corrections")
def corrections(status: str | None = None, employee_id: int | None = None, p: Principal = AV, db: Session = Depends(get_db)):
    q = _scoped(select(AttendanceCorrection, Employee).join(Employee, Employee.id == AttendanceCorrection.employee_id), p, Employee.location_id)
    if status:
        q = q.where(AttendanceCorrection.status == status)
    if employee_id:
        q = q.where(AttendanceCorrection.employee_id == employee_id)
    rows = db.execute(q.order_by(AttendanceCorrection.id.desc()).limit(300)).all()
    names = _names(db, {c.requested_by for c, _ in rows} | {c.decided_by for c, _ in rows})
    return [_corr_out(db, c, e, names) for c, e in rows]


@att_router.post("/corrections", status_code=201)
def request_correction(body: S.CorrectionIn, request: Request, p: Principal = AM, db: Session = Depends(get_db)):
    c = commit_or_http(db, lambda: att.request_correction(db, actor=p.user, employee_id=body.employee_id, work_date=body.work_date, kind=body.kind,
                                                          direction=body.direction, punch_time=body.punch_time, punch_id=body.punch_id, reason=body.reason,
                                                          ip=client_ip(request)))
    return _corr_out(db, c, db.get(Employee, c.employee_id), _names(db, {c.requested_by}))


@att_router.post("/corrections/{correction_id}/decision")
def decide_correction(correction_id: int, body: S.DecisionIn, request: Request, p: Principal = Depends(require("attendance.approve")), db: Session = Depends(get_db)):
    c = commit_or_http(db, lambda: att.decide_correction(db, actor=p.user, correction_id=correction_id, approve=body.approve, note=body.note, ip=client_ip(request)))
    return _corr_out(db, c, db.get(Employee, c.employee_id), _names(db, {c.requested_by, c.decided_by}))
