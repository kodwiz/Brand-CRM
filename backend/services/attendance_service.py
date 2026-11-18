"""Biometric attendance: devices and adapters, punch import with de-duplication, daily summaries, exceptions and approved corrections.

Raw punches are evidence and are never edited (a database trigger enforces it). The daily summary is derived from the punches and
approved corrections and can be rebuilt at any time. Nothing is assumed about the company's rules: until an attendance policy
(time zone, shift, grace period, weekly off days) exists, no summary is built, because without a time zone the day a punch belongs
to is not known.

Known limits (see docs/attendance.md): shifts are within one calendar day (no night shifts across midnight), leave and public
holidays are not modelled (a day without punches is 'no punch', not 'absent'), and one policy applies per location.
"""
from __future__ import annotations

import hashlib
from collections import defaultdict
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import and_, func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from backend.attendance import registry
from backend.attendance.base import RawPunch
from backend.models import (
    AttendanceCorrection, AttendanceDay, AttendanceDevice, AttendanceImport, AttendancePolicy, AttendancePunch, Employee, Location, User,
)
from backend.services import audit_service
from backend.services.errors import BusinessRuleError, ConflictError, NotFoundError, SelfApprovalError
from backend.services.scope import can_access_location

MAX_FILE_BYTES = 10 * 1024 * 1024
SECRET_WORDS = ("password", "passwd", "secret", "token", "apikey", "api_key", "private")
EXCEPTIONS = {"missing_out": "Check-in without a check-out", "missing_in": "Check-out without a check-in",
              "single_punch": "Only one punch in the day", "unclassified_punch": "Punches without in/out information"}


# ---- devices ----------------------------------------------------------------------------------------------------------------------------
def get_device(db: Session, device_id: int) -> AttendanceDevice:
    d = db.get(AttendanceDevice, device_id)
    if d is None:
        raise NotFoundError("Attendance device not found")
    return d


def checked_device(db: Session, user: User, device_id: int) -> AttendanceDevice:
    d = get_device(db, device_id)
    if not can_access_location(user, d.location_id):
        raise NotFoundError("Attendance device not found")
    return d


def _no_secrets(config: dict) -> None:
    def walk(o, path=""):
        if isinstance(o, dict):
            for k, v in o.items():
                if any(w in str(k).lower() for w in SECRET_WORDS):
                    raise BusinessRuleError(f"Do not store credentials in the device settings ('{path}{k}'). Keep them in the environment of whatever "
                                            "connector runs the device, not in the database.")
                walk(v, f"{path}{k}.")
    walk(config)


def create_device(db: Session, *, actor: User, values: dict, ip: str | None) -> AttendanceDevice:
    name = (values.get("name") or "").strip()
    if not name:
        raise BusinessRuleError("Give the device a name.")
    if db.get(Location, values["location_id"]) is None:
        raise NotFoundError("Location not found")
    if db.scalar(select(AttendanceDevice).where(func.lower(AttendanceDevice.name) == name.lower())) is not None:
        raise ConflictError(f"A device called '{name}' already exists.")
    adapter = registry.get_adapter(values["adapter"])
    _no_secrets(values.get("config") or {})
    cfg = adapter.validate_config(values.get("config") or {})
    d = AttendanceDevice(name=name, location_id=values["location_id"], brand=(values.get("brand") or "").strip() or None,
                         model=(values.get("model") or "").strip() or None, protocol=(values.get("protocol") or "").strip() or None,
                         export_format=(values.get("export_format") or "").strip() or None, adapter=adapter.code, config=cfg, created_by=actor.id)
    db.add(d)
    db.flush()
    audit_service.record(db, actor=actor, action="attendance_device.created", entity_type="attendance_device", entity_id=d.id, location_id=d.location_id,
                         after={"name": d.name, "adapter": d.adapter, "brand": d.brand, "model": d.model, "config": cfg}, ip=ip)
    return d


def update_device(db: Session, *, actor: User, device_id: int, values: dict, reason: str | None, ip: str | None) -> AttendanceDevice:
    d = get_device(db, device_id)
    before = {"name": d.name, "brand": d.brand, "model": d.model, "is_active": d.is_active, "config": d.config}
    for k in ("brand", "model", "protocol", "export_format"):
        if k in values:
            setattr(d, k, (values[k] or "").strip() or None)
    if values.get("name"):
        d.name = values["name"].strip()
    if values.get("config") is not None:
        _no_secrets(values["config"])
        d.config = registry.get_adapter(d.adapter).validate_config(values["config"])
    if values.get("is_active") is not None:
        d.is_active = values["is_active"]
    db.flush()
    audit_service.record(db, actor=actor, action="attendance_device.updated", entity_type="attendance_device", entity_id=d.id, location_id=d.location_id,
                         reason=reason, before=before, after={"name": d.name, "brand": d.brand, "model": d.model, "is_active": d.is_active, "config": d.config}, ip=ip)
    return d


# ---- policy -----------------------------------------------------------------------------------------------------------------------------
def policy_for(db: Session, location_id: int) -> AttendancePolicy | None:
    p = db.scalar(select(AttendancePolicy).where(AttendancePolicy.location_id == location_id))
    return p or db.scalar(select(AttendancePolicy).where(AttendancePolicy.location_id.is_(None)))


def set_policy(db: Session, *, actor: User, location_id: int | None, values: dict, reason: str | None, ip: str | None) -> AttendancePolicy:
    if location_id is not None and db.get(Location, location_id) is None:
        raise NotFoundError("Location not found")
    tz = (values.get("timezone") or "").strip()
    try:
        ZoneInfo(tz)
    except (ZoneInfoNotFoundError, ValueError):
        raise BusinessRuleError(f"'{tz}' is not a known time zone (for example Asia/Karachi).")
    start, end = values["shift_start"], values["shift_end"]
    if end <= start:
        raise BusinessRuleError("The shift must end after it starts, on the same day. Night shifts across midnight are not supported yet.")
    off = sorted(set(int(x) for x in values.get("weekly_off") or []))
    if any(x < 0 or x > 6 for x in off):
        raise BusinessRuleError("Weekly off days are numbers 0 (Monday) to 6 (Sunday).")
    mode = values.get("direction_mode") or "device"
    if mode not in ("device", "first_last"):
        raise BusinessRuleError("Direction mode must be 'device' or 'first_last'.")
    nums = {k: int(values.get(k) or 0) for k in ("grace_minutes", "min_present_minutes", "overtime_after_minutes")}
    if any(v < 0 for v in nums.values()):
        raise BusinessRuleError("Minutes cannot be negative.")
    p = db.scalar(select(AttendancePolicy).where(AttendancePolicy.location_id.is_(None) if location_id is None else AttendancePolicy.location_id == location_id))
    before = None
    if p is None:
        p = AttendancePolicy(location_id=location_id, timezone=tz, shift_start=start, shift_end=end)
        db.add(p)
    else:
        before = _policy_dict(p)
    p.timezone, p.shift_start, p.shift_end = tz, start, end
    p.overtime_enabled, p.weekly_off, p.direction_mode = bool(values.get("overtime_enabled")), off, mode
    p.grace_minutes, p.min_present_minutes, p.overtime_after_minutes = nums["grace_minutes"], nums["min_present_minutes"], nums["overtime_after_minutes"]
    p.updated_by = actor.id
    db.flush()
    audit_service.record(db, actor=actor, action="attendance_policy.set", entity_type="attendance_policy", entity_id=p.id, location_id=location_id, reason=reason,
                         before=before, after=_policy_dict(p), ip=ip)
    _rebuild_everything(db, location_id)                                                     # summaries depend on the policy
    return p


def _rebuild_everything(db: Session, location_id: int | None) -> None:
    """After a policy change, recompute from the earliest punch (or the last 31 days if there are none) up to today, 400 days at a time."""
    q = select(func.min(AttendancePunch.punched_at))
    first = db.scalar(q)
    start = (first.date() - timedelta(days=1)) if first else date.today() - timedelta(days=31)
    ids = None if location_id is None else {location_id}
    d = start
    while d <= date.today():
        end = min(d + timedelta(days=399), date.today())
        rebuild(db, date_from=d, date_to=end, location_ids=ids)
        d = end + timedelta(days=1)


def _policy_dict(p: AttendancePolicy) -> dict:
    return {"location_id": p.location_id, "timezone": p.timezone, "shift_start": p.shift_start.isoformat(timespec="minutes"),
            "shift_end": p.shift_end.isoformat(timespec="minutes"), "grace_minutes": p.grace_minutes, "min_present_minutes": p.min_present_minutes,
            "overtime_enabled": p.overtime_enabled, "overtime_after_minutes": p.overtime_after_minutes, "weekly_off": list(p.weekly_off),
            "direction_mode": p.direction_mode}


policy_dict = _policy_dict


# ---- import -----------------------------------------------------------------------------------------------------------------------------
def import_file(db: Session, *, actor: User, device_id: int, filename: str | None, content: bytes, dry_run: bool, ip: str | None) -> dict:
    d = get_device(db, device_id)
    if not d.is_active:
        raise BusinessRuleError(f"{d.name} is switched off. Switch it on before importing.")
    if not content:
        raise BusinessRuleError("The file is empty.")
    if len(content) > MAX_FILE_BYTES:
        raise BusinessRuleError(f"The file is larger than {MAX_FILE_BYTES // (1024 * 1024)} MB. Export a shorter period.")
    sha = hashlib.sha256(content).hexdigest()
    prior = db.scalar(select(AttendanceImport).where(AttendanceImport.device_id == d.id, AttendanceImport.file_sha256 == sha,
                                                     AttendanceImport.status == "completed"))
    if prior is not None and not dry_run:
        raise ConflictError(f"This exact file was already imported on {prior.created_at:%Y-%m-%d %H:%M} (import #{prior.id}). Nothing was added.")
    parsed = registry.get_adapter(d.adapter).parse(content, filename, d.config)
    punches = parsed.punches
    employees = {e.device_user_id: e for e in db.scalars(select(Employee).where(Employee.device_user_id.is_not(None))).all()}
    existing: set[tuple[str, datetime]] = set()
    if punches:
        lo, hi = min(p.punched_at for p in punches), max(p.punched_at for p in punches)
        existing = {(u, t) for u, t in db.execute(select(AttendancePunch.device_user_id, AttendancePunch.punched_at).where(
            AttendancePunch.device_id == d.id, AttendancePunch.punched_at >= lo, AttendancePunch.punched_at <= hi)).all()}
    fresh: list[RawPunch] = []
    seen: set[tuple[str, datetime]] = set()
    duplicates = 0
    for p in punches:
        key = (p.device_user_id, p.punched_at)
        if key in existing or key in seen:
            duplicates += 1
            continue
        seen.add(key)
        fresh.append(p)
    unmatched = sum(1 for p in fresh if p.device_user_id not in employees)
    summary = {"device": d.name, "filename": filename, "rows_total": parsed.rows_total, "rows_new": len(fresh), "rows_duplicate": duplicates,
               "rows_rejected": len(parsed.rejects), "rows_unmatched": unmatched,
               "rejects": [{"line": r.line, "reason": r.reason, "raw": r.raw} for r in parsed.rejects[:50]],
               "unmatched_ids": sorted({p.device_user_id for p in fresh if p.device_user_id not in employees})[:50],
               "first_punch_at": min((p.punched_at for p in fresh), default=None), "last_punch_at": max((p.punched_at for p in fresh), default=None),
               "dry_run": dry_run}
    if dry_run:
        return summary
    if parsed.rows_total == 0 or (not fresh and not duplicates):
        raise BusinessRuleError("No punches could be read from this file. " + (f"First problem: line {parsed.rejects[0].line}: {parsed.rejects[0].reason}."
                                                                             if parsed.rejects else "Check the device settings."))
    imp = AttendanceImport(device_id=d.id, adapter=d.adapter, filename=(filename or "")[:200] or None, file_sha256=sha, status="completed",
                           rows_total=parsed.rows_total, rows_new=len(fresh), rows_duplicate=duplicates, rows_rejected=len(parsed.rejects),
                           rows_unmatched=unmatched, first_punch_at=summary["first_punch_at"], last_punch_at=summary["last_punch_at"],
                           errors=summary["rejects"], imported_by=actor.id)
    db.add(imp)
    db.flush()
    inserted = 0
    for i in range(0, len(fresh), 500):
        chunk = fresh[i:i + 500]
        rows = [{"device_id": d.id, "batch_id": imp.id, "device_user_id": p.device_user_id, "employee_id": employees[p.device_user_id].id if p.device_user_id in employees else None,
                 "location_id": d.location_id, "punched_at": p.punched_at, "direction": p.direction, "source": "device",
                 "status": "ok" if p.device_user_id in employees else "unmatched", "raw_ref": p.raw_ref} for p in chunk]
        stmt = pg_insert(AttendancePunch).values(rows).on_conflict_do_nothing(index_elements=["device_id", "device_user_id", "punched_at"],
                                                                              index_where=AttendancePunch.device_id.is_not(None))
        inserted += len(db.execute(stmt.returning(AttendancePunch.id)).all())
    imp.rows_duplicate += len(fresh) - inserted           # a concurrent import may have added some of them first
    imp.rows_new = inserted
    summary.update(rows_new=inserted, rows_duplicate=imp.rows_duplicate, import_id=imp.id)
    d.last_import_at = datetime.now(timezone.utc)
    audit_service.record(db, actor=actor, action="attendance.imported", entity_type="attendance_import", entity_id=imp.id, location_id=d.location_id,
                         after={"device": d.name, "file": filename, "sha256": sha[:16], "new": inserted, "duplicates": imp.rows_duplicate,
                                "rejected": len(parsed.rejects), "unmatched": unmatched}, ip=ip)
    db.flush()
    if fresh:
        ids = {employees[p.device_user_id].id for p in fresh if p.device_user_id in employees}
        if ids:
            rebuild(db, employee_ids=ids, date_from=summary["first_punch_at"].date() - timedelta(days=1), date_to=summary["last_punch_at"].date() + timedelta(days=1))
    return summary


def match_unmatched(db: Session, *, actor: User, device_user_id: str, ip: str | None) -> int:
    """Attach earlier punches with this device id to the employee who now owns it."""
    e = db.scalar(select(Employee).where(Employee.device_user_id == device_user_id))
    if e is None:
        raise NotFoundError(f"No employee has device id {device_user_id}.")
    rows = db.scalars(select(AttendancePunch).where(AttendancePunch.device_user_id == device_user_id, AttendancePunch.status == "unmatched")
                      .with_for_update()).all()
    for r in rows:
        r.employee_id, r.status = e.id, "ok"
    db.flush()
    if rows:
        audit_service.record(db, actor=actor, action="attendance.matched", entity_type="employee", entity_id=e.id, location_id=e.location_id,
                             after={"device_user_id": device_user_id, "punches": len(rows)}, ip=ip)
        rebuild(db, employee_ids={e.id}, date_from=min(r.punched_at for r in rows).date() - timedelta(days=1),
                date_to=max(r.punched_at for r in rows).date() + timedelta(days=1))
    return len(rows)


def unmatched_ids(db: Session, user: User) -> list[dict]:
    q = (select(AttendancePunch.device_user_id, func.count(), func.min(AttendancePunch.punched_at), func.max(AttendancePunch.punched_at), AttendancePunch.location_id)
         .where(AttendancePunch.status == "unmatched").group_by(AttendancePunch.device_user_id, AttendancePunch.location_id).order_by(AttendancePunch.device_user_id))
    return [{"device_user_id": u, "punches": n, "first": a, "last": b, "location_id": loc} for u, n, a, b, loc in db.execute(q).all()
            if loc is None or can_access_location(user, loc)]


# ---- daily summaries ---------------------------------------------------------------------------------------------------------------------
def _local(dt: datetime, tz: ZoneInfo) -> datetime:
    return dt.astimezone(tz)


def _day_bounds(d: date, tz: ZoneInfo) -> tuple[datetime, datetime]:
    return (datetime.combine(d, time.min, tzinfo=tz).astimezone(timezone.utc), datetime.combine(d + timedelta(days=1), time.min, tzinfo=tz).astimezone(timezone.utc))


def _pair(punches: list[AttendancePunch], mode: str) -> tuple[list[tuple[datetime, datetime]], str | None]:
    """Turn a day's punches into (in, out) pairs and name the exception, if any."""
    if not punches:
        return [], None
    ordered = sorted(punches, key=lambda p: p.punched_at)
    if mode == "first_last":
        if len(ordered) == 1:
            return [], "single_punch"
        return [(ordered[0].punched_at, ordered[-1].punched_at)], None
    if any(p.direction == "unknown" for p in ordered):
        return [], "unclassified_punch"
    pairs, open_in, problem = [], None, None
    for p in ordered:
        if p.direction == "in":
            if open_in is not None:
                problem = problem or "missing_out"
            open_in = p.punched_at
        else:
            if open_in is None:
                problem = problem or "missing_in"
            else:
                pairs.append((open_in, p.punched_at))
                open_in = None
    if open_in is not None:
        problem = problem or "missing_out"
    return pairs, problem


def compute_day(emp: Employee, d: date, punches: list[AttendancePunch], pol: AttendancePolicy, tz: ZoneInfo) -> dict:
    pairs, problem = _pair(punches, pol.direction_mode)
    worked = sum(int((o - i).total_seconds() // 60) for i, o in pairs)
    first_in = pairs[0][0] if pairs else None
    last_out = pairs[-1][1] if pairs else None
    if first_in is None:                                  # an incomplete day still shows the check-in that exists (never a guess for unknown-direction punches)
        first_in = next((p.punched_at for p in sorted(punches, key=lambda p: p.punched_at) if p.direction == "in"), None)
    if not punches:
        status = "weekly_off" if d.weekday() in (pol.weekly_off or []) else "no_punch"
        return {"status": status, "punch_count": 0, "worked_minutes": 0, "first_in": None, "last_out": None, "late_minutes": None,
                "overtime_minutes": None, "exception": None}
    # lateness is judged on the first check-in even when the day is incomplete
    first_in_any = first_in or next((p.punched_at for p in sorted(punches, key=lambda p: p.punched_at) if p.direction in ("in", "unknown")), None)
    late = None
    if first_in_any is not None:
        fi = _local(first_in_any, tz)
        start = datetime.combine(fi.date(), pol.shift_start, tzinfo=tz)
        late = int((fi - start).total_seconds() // 60) if fi > start + timedelta(minutes=pol.grace_minutes) else 0
    overtime = None
    if pol.overtime_enabled and not problem:
        scheduled = (datetime.combine(d, pol.shift_end) - datetime.combine(d, pol.shift_start)).seconds // 60
        overtime = max(0, worked - scheduled - pol.overtime_after_minutes)
    if problem:
        status = "incomplete"
    elif worked < pol.min_present_minutes:
        status = "short"
    elif late:
        status = "late"
    else:
        status = "present"
    return {"status": status, "punch_count": len(punches), "worked_minutes": worked, "first_in": first_in, "last_out": last_out,
            "late_minutes": late, "overtime_minutes": overtime, "exception": problem}


def rebuild(db: Session, *, date_from: date, date_to: date, employee_ids: set[int] | None = None, location_ids: set[int] | None = None) -> dict:
    """Recompute the daily summaries for the range. Days of employees whose location has no attendance policy are left out and counted."""
    if date_to < date_from:
        raise BusinessRuleError("The end date is before the start date.")
    if (date_to - date_from).days > 400:
        raise BusinessRuleError("Rebuild at most 400 days at a time.")
    q = select(Employee)
    if employee_ids is not None:
        q = q.where(Employee.id.in_(employee_ids))
    if location_ids is not None:
        q = q.where(Employee.location_id.in_(location_ids))
    built = skipped = 0
    for e in db.scalars(q).all():
        pol = policy_for(db, e.location_id)
        if pol is None:
            skipped += 1
            continue
        tz = ZoneInfo(pol.timezone)
        today_local = datetime.now(tz).date()
        lo, hi = _day_bounds(date_from, tz)[0], _day_bounds(date_to, tz)[1]
        by_day: dict[date, list[AttendancePunch]] = defaultdict(list)
        corrected: set[date] = set()
        for pu in db.scalars(select(AttendancePunch).where(AttendancePunch.employee_id == e.id, AttendancePunch.punched_at >= lo, AttendancePunch.punched_at < hi)).all():
            ld = _local(pu.punched_at, tz).date()
            if pu.status == "ignored":
                if pu.correction_id:
                    corrected.add(ld)
                continue
            if pu.status == "ok":
                by_day[ld].append(pu)
                if pu.source == "correction":
                    corrected.add(ld)
        db.query(AttendanceDay).filter(AttendanceDay.employee_id == e.id, AttendanceDay.work_date >= date_from, AttendanceDay.work_date <= date_to).delete(synchronize_session=False)
        d = date_from
        while d <= min(date_to, today_local):
            active = (e.joined_on is None or e.joined_on <= d) and (e.left_on is None or e.left_on >= d)
            if active or by_day.get(d):
                v = compute_day(e, d, by_day.get(d, []), pol, tz)
                db.add(AttendanceDay(employee_id=e.id, location_id=e.location_id, work_date=d, corrected=d in corrected, **v))
                built += 1
            d += timedelta(days=1)
    db.flush()
    return {"days_built": built, "employees_without_policy": skipped}


# ---- queries ------------------------------------------------------------------------------------------------------------------------------
def _scope(q, user: User, col):
    from backend.services.scope import visible_location_ids
    ids = visible_location_ids(user)
    return q if ids is None else q.where(col.in_(ids))


def list_days(db: Session, user: User, *, date_from: date, date_to: date, employee_id: int | None = None, location_id: int | None = None,
              only_exceptions: bool = False, status: str | None = None, limit: int = 2000) -> list[tuple[AttendanceDay, Employee]]:
    q = select(AttendanceDay, Employee).join(Employee, Employee.id == AttendanceDay.employee_id).where(
        AttendanceDay.work_date >= date_from, AttendanceDay.work_date <= date_to)
    q = _scope(q, user, AttendanceDay.location_id)
    if employee_id:
        q = q.where(AttendanceDay.employee_id == employee_id)
    if location_id:
        q = q.where(AttendanceDay.location_id == location_id)
    if only_exceptions:
        q = q.where(AttendanceDay.exception.is_not(None))
    if status:
        q = q.where(AttendanceDay.status == status)
    return list(db.execute(q.order_by(AttendanceDay.work_date.desc(), Employee.full_name).limit(limit)).all())


def period_summary(db: Session, user: User, *, date_from: date, date_to: date, location_id: int | None = None) -> list[dict]:
    q = select(Employee.id, Employee.code, Employee.full_name, Employee.location_id,
               func.count().filter(AttendanceDay.status.in_(("present", "late", "short"))),
               func.count().filter(AttendanceDay.status == "late"), func.count().filter(AttendanceDay.status == "short"),
               func.count().filter(AttendanceDay.status == "incomplete"), func.count().filter(AttendanceDay.status == "no_punch"),
               func.coalesce(func.sum(AttendanceDay.worked_minutes), 0), func.coalesce(func.sum(AttendanceDay.late_minutes), 0),
               func.coalesce(func.sum(AttendanceDay.overtime_minutes), 0)).join(AttendanceDay, AttendanceDay.employee_id == Employee.id).where(
        AttendanceDay.work_date >= date_from, AttendanceDay.work_date <= date_to).group_by(Employee.id)
    q = _scope(q, user, Employee.location_id)
    if location_id:
        q = q.where(Employee.location_id == location_id)
    return [{"employee_id": i, "code": c, "name": n, "location_id": loc, "days_present": pr, "days_late": la, "days_short": sh, "days_incomplete": inc,
             "days_no_punch": npn, "worked_minutes": int(w), "late_minutes": int(lm), "overtime_minutes": int(ot)}
            for i, c, n, loc, pr, la, sh, inc, npn, w, lm, ot in db.execute(q.order_by(Employee.full_name)).all()]


# ---- corrections ----------------------------------------------------------------------------------------------------------------------------
def get_correction(db: Session, correction_id: int, *, lock: bool = False) -> AttendanceCorrection:
    q = select(AttendanceCorrection).where(AttendanceCorrection.id == correction_id)
    if lock:
        q = q.with_for_update().execution_options(populate_existing=True)
    c = db.scalar(q)
    if c is None:
        raise NotFoundError("Correction not found")
    return c


def _no_self(actor: User, e: Employee, what: str) -> None:
    if e.user_id is not None and e.user_id == actor.id:
        raise SelfApprovalError(f"You cannot {what} your own attendance.")


def request_correction(db: Session, *, actor: User, employee_id: int, work_date: date, kind: str, direction: str | None, punch_time: time | None,
                       punch_id: int | None, reason: str, ip: str | None) -> AttendanceCorrection:
    e = db.get(Employee, employee_id)
    if e is None or not can_access_location(actor, e.location_id):
        raise NotFoundError("Employee not found")
    _no_self(actor, e, "correct")
    if len((reason or "").strip()) < 5:
        raise BusinessRuleError("Say why the correction is needed (at least a few words).")
    if work_date > date.today():
        raise BusinessRuleError("A correction cannot be for a future date.")
    pol = policy_for(db, e.location_id)
    if pol is None:
        raise BusinessRuleError("Set the attendance policy for this location first; the day a punch belongs to depends on its time zone.")
    tz = ZoneInfo(pol.timezone)
    at = None
    if kind == "add_punch":
        if direction not in ("in", "out") or punch_time is None:
            raise BusinessRuleError("Say whether the missing punch is a check-in or check-out, and at what time.")
        at = datetime.combine(work_date, punch_time, tzinfo=tz).astimezone(timezone.utc)
        dup = db.scalar(select(AttendanceCorrection).where(AttendanceCorrection.employee_id == e.id, AttendanceCorrection.status == "pending",
                                                           AttendanceCorrection.kind == kind, AttendanceCorrection.punched_at == at))
        if dup is not None:
            raise ConflictError("The same correction is already waiting for a decision.")
    elif kind == "ignore_punch":
        pu = db.get(AttendancePunch, punch_id) if punch_id else None
        if pu is None or pu.employee_id != e.id or pu.status != "ok" or _local(pu.punched_at, tz).date() != work_date:
            raise BusinessRuleError("Choose one of this employee's punches on that day.")
        dup = db.scalar(select(AttendanceCorrection).where(AttendanceCorrection.punch_id == pu.id, AttendanceCorrection.status == "pending"))
        if dup is not None:
            raise ConflictError("That punch already has a correction waiting for a decision.")
    else:
        raise BusinessRuleError("A correction either adds a missing punch or ignores a wrong one.")
    c = AttendanceCorrection(employee_id=e.id, work_date=work_date, kind=kind, direction=direction if kind == "add_punch" else None, punched_at=at,
                             punch_id=punch_id if kind == "ignore_punch" else None, reason=reason.strip(), requested_by=actor.id)
    db.add(c)
    db.flush()
    audit_service.record(db, actor=actor, action="attendance_correction.requested", entity_type="attendance_correction", entity_id=c.id, location_id=e.location_id,
                         reason=reason, after={"employee": e.code, "date": work_date.isoformat(), "kind": kind, "direction": direction,
                                               "punched_at": at.isoformat() if at else None, "punch_id": punch_id}, ip=ip)
    return c


def decide_correction(db: Session, *, actor: User, correction_id: int, approve: bool, note: str | None, ip: str | None) -> AttendanceCorrection:
    c = get_correction(db, correction_id, lock=True)
    if c.status != "pending":
        raise ConflictError(f"This correction is already {c.status}.")
    e = db.get(Employee, c.employee_id)
    if not can_access_location(actor, e.location_id):
        raise NotFoundError("Correction not found")
    if c.requested_by == actor.id:
        raise SelfApprovalError("You cannot decide on a correction you requested yourself. Ask another authorised person.")
    _no_self(actor, e, "approve a correction of")
    if not approve and not (note or "").strip():
        raise BusinessRuleError("Say why the correction is being rejected.")
    c.status, c.decided_by, c.decided_at, c.decision_note = ("approved" if approve else "rejected"), actor.id, datetime.now(timezone.utc), (note or "").strip() or None
    before = None
    if approve:
        if c.kind == "add_punch":
            exists = db.scalar(select(AttendancePunch).where(AttendancePunch.employee_id == e.id, AttendancePunch.punched_at == c.punched_at,
                                                             AttendancePunch.status == "ok"))
            if exists is not None:
                raise ConflictError("The employee already has a punch at exactly that time.")
            db.add(AttendancePunch(device_id=None, device_user_id=e.device_user_id or e.code, employee_id=e.id, location_id=e.location_id, punched_at=c.punched_at,
                                   direction=c.direction, source="correction", status="ok", raw_ref=f"approved correction #{c.id}", correction_id=c.id))
        else:
            pu = db.scalar(select(AttendancePunch).where(AttendancePunch.id == c.punch_id).with_for_update())
            if pu is None or pu.status != "ok":
                raise ConflictError("That punch is no longer active; the correction cannot be applied.")
            before = {"punch_id": pu.id, "punched_at": pu.punched_at.isoformat(), "direction": pu.direction, "status": pu.status}
            pu.status, pu.correction_id = "ignored", c.id
        db.flush()
        rebuild(db, employee_ids={e.id}, date_from=c.work_date - timedelta(days=1), date_to=c.work_date + timedelta(days=1))
    audit_service.record(db, actor=actor, action="attendance_correction.approved" if approve else "attendance_correction.rejected", entity_type="attendance_correction",
                         entity_id=c.id, location_id=e.location_id, reason=note, before=before,
                         after={"employee": e.code, "date": c.work_date.isoformat(), "kind": c.kind, "status": c.status}, ip=ip)
    return c
