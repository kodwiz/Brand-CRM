from __future__ import annotations

from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from backend.models import StockTransaction, SystemSetting, User
from backend.services import audit_service
from backend.services.errors import BusinessRuleError, ConflictError

COSTING_KEY = "inventory.costing_method"
SUPPORTED_COSTING = ("weighted_average",)
AUTO_APPROVE_KEY = "inventory.adjustment_auto_approve_value"
EXPIRY_WARN_KEY = "inventory.expiry_warning_days"

PO_SELF_APPROVE_KEY = "purchasing.po_self_approve_limit"      # PO total up to which the creator may approve their own PO
RECEIPT_TOL_KEY = "purchasing.receipt_over_tolerance_pct"     # % a receipt may exceed the ordered quantity
PRICE_TOL_KEY = "purchasing.invoice_price_tolerance_pct"      # % an invoice price may differ from the PO price
QTY_TOL_KEY = "purchasing.invoice_qty_tolerance_pct"          # % an invoice quantity may exceed the ordered quantity

DEFAULTS = {COSTING_KEY: "weighted_average", AUTO_APPROVE_KEY: "0", EXPIRY_WARN_KEY: 60,
            PO_SELF_APPROVE_KEY: "0", RECEIPT_TOL_KEY: "0", PRICE_TOL_KEY: "0", QTY_TOL_KEY: "0"}


def get(db: Session, key: str):
    row = db.get(SystemSetting, key)
    return row.value if row is not None else DEFAULTS.get(key)


def _set(db: Session, key: str, value, actor: User) -> None:
    row = db.get(SystemSetting, key)
    if row is None:
        db.add(SystemSetting(key=key, value=value, updated_by=actor.id))
    else:
        row.value = value
        row.updated_by = actor.id
    db.flush()


def live_transactions_exist(db: Session) -> bool:
    return (db.scalar(select(func.count()).select_from(StockTransaction)) or 0) > 0


def costing_method(db: Session) -> str:
    return get(db, COSTING_KEY)


def inventory_settings(db: Session) -> dict:
    return {
        "costing_method": costing_method(db),
        "costing_locked": live_transactions_exist(db),
        "supported_costing_methods": list(SUPPORTED_COSTING),
        "adjustment_auto_approve_value": str(get(db, AUTO_APPROVE_KEY)),
        "expiry_warning_days": int(get(db, EXPIRY_WARN_KEY)),
    }


def set_costing_method(db: Session, *, actor: User, method: str, reason: str | None, ip: str | None) -> dict:
    if method not in SUPPORTED_COSTING:
        raise BusinessRuleError(f"Unsupported costing method. Implemented: {', '.join(SUPPORTED_COSTING)}.")
    before = costing_method(db)
    if live_transactions_exist(db):
        raise ConflictError(
            "Live inventory transactions already exist. The costing method can no longer be changed here; "
            "a controlled data migration with accounting approval is required.")
    _set(db, COSTING_KEY, method, actor)
    audit_service.record(db, actor=actor, action="settings.costing_method_changed", entity_type="setting",
                         entity_id=COSTING_KEY, reason=reason, before={"value": before}, after={"value": method}, ip=ip)
    return inventory_settings(db)


def set_thresholds(db: Session, *, actor: User, auto_approve_value: Decimal | None, expiry_warning_days: int | None,
                   reason: str | None, ip: str | None) -> dict:
    before = inventory_settings(db)
    if auto_approve_value is not None:
        if auto_approve_value < 0:
            raise BusinessRuleError("The auto-approval limit cannot be negative.")
        _set(db, AUTO_APPROVE_KEY, str(auto_approve_value), actor)
    if expiry_warning_days is not None:
        if not 1 <= expiry_warning_days <= 730:
            raise BusinessRuleError("Expiry warning window must be between 1 and 730 days.")
        _set(db, EXPIRY_WARN_KEY, expiry_warning_days, actor)
    after = inventory_settings(db)
    audit_service.record(db, actor=actor, action="settings.inventory_thresholds_changed", entity_type="setting",
                         entity_id="inventory", reason=reason, before=before, after=after, ip=ip)
    return after


# ---- purchasing -----------------------------------------------------------------------------------
def purchasing_settings(db: Session) -> dict:
    return {
        "po_self_approve_limit": str(get(db, PO_SELF_APPROVE_KEY)),
        "receipt_over_tolerance_pct": str(get(db, RECEIPT_TOL_KEY)),
        "invoice_price_tolerance_pct": str(get(db, PRICE_TOL_KEY)),
        "invoice_qty_tolerance_pct": str(get(db, QTY_TOL_KEY)),
    }


def purchasing_value(db: Session, key: str) -> Decimal:
    return Decimal(str(get(db, key)))


def set_purchasing_settings(db: Session, *, actor: User, values: dict, reason: str | None, ip: str | None) -> dict:
    mapping = {"po_self_approve_limit": PO_SELF_APPROVE_KEY, "receipt_over_tolerance_pct": RECEIPT_TOL_KEY,
               "invoice_price_tolerance_pct": PRICE_TOL_KEY, "invoice_qty_tolerance_pct": QTY_TOL_KEY}
    before = purchasing_settings(db)
    for name, v in values.items():
        if v is None:
            continue
        d = Decimal(str(v))
        if d < 0 or (name.endswith("_pct") and d > 100):
            raise BusinessRuleError(f"{name.replace('_', ' ').capitalize()} must be between 0 and {'100' if name.endswith('_pct') else 'a positive amount'}.")
        _set(db, mapping[name], str(d), actor)
    after = purchasing_settings(db)
    audit_service.record(db, actor=actor, action="settings.purchasing_changed", entity_type="setting", entity_id="purchasing",
                         reason=reason, before=before, after=after, ip=ip)
    return after


# ---- sales ------------------------------------------------------------------------------------------------
DISCOUNT_LIMIT_KEY = "sales.max_manual_discount_pct"     # manual discount above this % of the promotional price needs approval
TAX_RATE_KEY = "sales.tax_rate_pct"                       # 0 = no tax charged
RETURN_WINDOW_KEY = "sales.return_window_days"            # 0 = no limit; counted from the delivery date
DEFAULTS.update({DISCOUNT_LIMIT_KEY: "0", TAX_RATE_KEY: "0", RETURN_WINDOW_KEY: 0})


def sales_settings(db: Session) -> dict:
    return {"max_manual_discount_pct": str(get(db, DISCOUNT_LIMIT_KEY)), "tax_rate_pct": str(get(db, TAX_RATE_KEY)),
            "return_window_days": int(get(db, RETURN_WINDOW_KEY))}


def sales_value(db: Session, key: str) -> Decimal:
    return Decimal(str(get(db, key)))


def set_sales_settings(db: Session, *, actor: User, values: dict, reason: str | None, ip: str | None) -> dict:
    before = sales_settings(db)
    for name, key in (("max_manual_discount_pct", DISCOUNT_LIMIT_KEY), ("tax_rate_pct", TAX_RATE_KEY)):
        v = values.get(name)
        if v is None:
            continue
        d = Decimal(str(v))
        if d < 0 or d > 100:
            raise BusinessRuleError(f"{name.replace('_', ' ').capitalize()} must be between 0 and 100.")
        _set(db, key, str(d), actor)
    if values.get("return_window_days") is not None:
        n = int(values["return_window_days"])
        if n < 0 or n > 3650:
            raise BusinessRuleError("The return window must be between 0 and 3650 days.")
        _set(db, RETURN_WINDOW_KEY, n, actor)
    after = sales_settings(db)
    audit_service.record(db, actor=actor, action="settings.sales_changed", entity_type="setting", entity_id="sales",
                         reason=reason, before=before, after=after, ip=ip)
    return after


# ---- distributors -----------------------------------------------------------------------------------------
DIST_BACKDATE_KEY = "distributors.backdate_days"                 # entries dated further back than this need approval
DIST_DISCOUNT_KEY = "distributors.max_extra_discount_pct"        # price below the agreed price by more than this % needs approval
DEFAULTS.update({DIST_BACKDATE_KEY: 7, DIST_DISCOUNT_KEY: "0"})


def distributor_settings(db: Session) -> dict:
    return {"backdate_days": int(get(db, DIST_BACKDATE_KEY)), "max_extra_discount_pct": str(get(db, DIST_DISCOUNT_KEY))}


def set_distributor_settings(db: Session, *, actor: User, values: dict, reason: str | None, ip: str | None) -> dict:
    before = distributor_settings(db)
    if values.get("backdate_days") is not None:
        n = int(values["backdate_days"])
        if n < 0 or n > 365:
            raise BusinessRuleError("The back-dating allowance must be between 0 and 365 days.")
        _set(db, DIST_BACKDATE_KEY, n, actor)
    if values.get("max_extra_discount_pct") is not None:
        d = Decimal(str(values["max_extra_discount_pct"]))
        if d < 0 or d > 100:
            raise BusinessRuleError("The extra discount limit must be between 0 and 100.")
        _set(db, DIST_DISCOUNT_KEY, str(d), actor)
    after = distributor_settings(db)
    audit_service.record(db, actor=actor, action="settings.distributors_changed", entity_type="setting", entity_id="distributors",
                         reason=reason, before=before, after=after, ip=ip)
    return after
