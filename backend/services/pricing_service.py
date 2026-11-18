"""The pricing engine. List price = the variant's retail price. Promotions reduce it per unit; a manual (agreed) price can go
lower still, and anything beyond the configured limit needs approval (sales_service). Results are snapshotted on the order
line, so later price or promotion changes never alter an existing order.

Stacking: a promotion marked non-stackable excludes every other promotion; stackable promotions combine (applied one after
another, in id order). The line receives whichever is better for the customer: the best single non-stackable promotion, or
all applicable stackable ones together. A promotion's own cap (max_discount_pct) limits the total promotional discount."""
from __future__ import annotations

from datetime import date
from decimal import ROUND_HALF_UP, Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.models import Customer, Product, ProductVariant, Promotion, User
from backend.services import audit_service
from backend.services.errors import BusinessRuleError, NotFoundError

Q2 = Decimal("0.01")


def q2(x) -> Decimal:
    return Decimal(str(x)).quantize(Q2, rounding=ROUND_HALF_UP)


def list_price(variant: ProductVariant) -> Decimal:
    if variant.pricing_status != "active" or variant.retail_price is None:
        raise BusinessRuleError(f"{variant.sku} has no active retail price, so it cannot be sold. Set its price in Products first.")
    if variant.status == "inactive" or variant.product.status == "inactive":
        raise BusinessRuleError(f"{variant.sku} is inactive and cannot be sold.")
    return Decimal(variant.retail_price)


def _applies(p: Promotion, variant: ProductVariant, product: Product, customer: Customer, qty: int, order_value: Decimal, on: date) -> bool:
    if not p.is_active or p.starts_on > on or (p.ends_on is not None and p.ends_on < on):
        return False
    if p.variant_id is not None and p.variant_id != variant.id:
        return False
    if p.product_id is not None and p.product_id != product.id:
        return False
    if p.category_id is not None and p.category_id != product.category_id:
        return False
    if p.customer_category is not None and p.customer_category != customer.category:
        return False
    if qty < p.min_qty:
        return False
    if p.min_order_value is not None and order_value < Decimal(p.min_order_value):
        return False
    return True


def _off(p: Promotion, price: Decimal) -> Decimal:
    d = price * Decimal(p.value) / 100 if p.kind == "percent" else Decimal(p.value)
    return min(q2(d), price)


def promo_for_line(promos: list[Promotion], variant: ProductVariant, customer: Customer, qty: int, order_value: Decimal, on: date,
                   price: Decimal) -> tuple[Decimal, list[Promotion]]:
    product = variant.product
    ok = [p for p in promos if _applies(p, variant, product, customer, qty, order_value, on)]
    best_single: tuple[Decimal, list[Promotion]] = (Decimal(0), [])
    for p in ok:
        if not p.stackable:
            d = _off(p, price)
            if d > best_single[0]:
                best_single = (d, [p])
    running, used = price, []
    for p in sorted((x for x in ok if x.stackable), key=lambda x: x.id):
        d = _off(p, running)
        if d > 0:
            running -= d
            used.append(p)
    stacked = (price - running, used)
    discount, chosen = stacked if stacked[0] > best_single[0] else best_single
    caps = [Decimal(p.max_discount_pct) for p in chosen if p.max_discount_pct is not None]
    if caps:
        discount = min(discount, q2(price * min(caps) / 100))
    return q2(discount), chosen


def active_promotions(db: Session, on: date) -> list[Promotion]:
    return list(db.scalars(select(Promotion).where(Promotion.is_active.is_(True), Promotion.starts_on <= on)
                           .order_by(Promotion.id)).all())


def quote(db: Session, *, customer: Customer, lines: list[dict], on: date) -> list[dict]:
    """lines: [{variant_id, qty}] -> [{variant, list_price, promo_discount, promotions}]."""
    promos = active_promotions(db, on)
    variants: dict[int, ProductVariant] = {}
    for ln in lines:
        v = db.get(ProductVariant, ln["variant_id"])
        if v is None:
            raise NotFoundError(f"Item {ln['variant_id']} not found")
        variants[ln["variant_id"]] = v
    prices = {vid: list_price(v) for vid, v in variants.items()}
    order_value = sum((prices[ln["variant_id"]] * int(ln["qty"]) for ln in lines), Decimal(0))
    out = []
    for ln in lines:
        v = variants[ln["variant_id"]]
        disc, used = promo_for_line(promos, v, customer, int(ln["qty"]), order_value, on, prices[v.id])
        out.append({"variant": v, "list_price": prices[v.id], "promo_discount": disc, "promotions": used})
    return out


# ---- promotion administration (pricing.manage) --------------------------------------------------------------
def _validate(values: dict) -> None:
    if values["kind"] not in ("percent", "fixed_amount"):
        raise BusinessRuleError("Promotion type must be percent or fixed_amount.")
    v = Decimal(str(values["value"]))
    if v <= 0 or (values["kind"] == "percent" and v > 100):
        raise BusinessRuleError("A percentage must be between 0 and 100; an amount must be above zero.")
    if values.get("ends_on") and values["ends_on"] < values["starts_on"]:
        raise BusinessRuleError("The end date cannot be before the start date.")
    if int(values.get("min_qty") or 1) < 1:
        raise BusinessRuleError("Minimum quantity must be at least 1.")


def create_promotion(db: Session, *, actor: User, values: dict, ip: str | None) -> Promotion:
    values = {**values, "min_qty": values.get("min_qty") or 1}
    _validate(values)
    p = Promotion(**values, created_by=actor.id)
    db.add(p)
    db.flush()
    audit_service.record(db, actor=actor, action="promotion.created", entity_type="promotion", entity_id=p.id,
                         after={"name": p.name, "kind": p.kind, "value": str(p.value)}, ip=ip)
    return p


def update_promotion(db: Session, *, actor: User, promotion_id: int, values: dict, ip: str | None) -> Promotion:
    p = db.get(Promotion, promotion_id)
    if p is None:
        raise NotFoundError("Promotion not found")
    merged = {c: getattr(p, c) for c in ("kind", "value", "starts_on", "ends_on", "min_qty")}
    merged.update({k: v for k, v in values.items() if k in merged})
    _validate(merged)
    before = {"name": p.name, "value": str(p.value), "is_active": p.is_active}
    for k, v in values.items():
        setattr(p, k, v)
    db.flush()
    audit_service.record(db, actor=actor, action="promotion.updated", entity_type="promotion", entity_id=p.id, before=before,
                         after={"name": p.name, "value": str(p.value), "is_active": p.is_active}, ip=ip)
    return p
