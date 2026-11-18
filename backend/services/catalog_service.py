"""Product catalogue: categories, products, variants. Products hold no stock; stock lives in the ledger."""
from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation

from sqlalchemy import func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from backend.models import Category, Product, ProductVariant, StockMovement, User
from backend.services import audit_service
from backend.services.errors import BusinessRuleError, ConflictError, NotFoundError

STATUSES = ("active", "inactive", "discontinued")
PRICING = ("unpriced", "active", "suspended")
HEX_RE = re.compile(r"^#[0-9A-Fa-f]{6}$")
CODE_RE = re.compile(r"^[A-Z0-9][A-Z0-9._\-/]{0,38}[A-Z0-9]$|^[A-Z0-9]$")
PRICE_FIELDS = ("retail_price", "employee_price", "distributor_price")
PRICE_AUDIT_FIELDS = PRICE_FIELDS + ("purchase_cost", "pricing_status")
SORTS = {"name": Product.name, "code": Product.code, "brand": Product.brand, "created": Product.created_at,
         "updated": Product.updated_at}


def norm_code(value: str | None, label: str) -> str:
    v = (value or "").strip().upper()
    if not v:
        raise BusinessRuleError(f"{label} is required.")
    if not CODE_RE.match(v):
        raise BusinessRuleError(f"{label} '{v}' may only contain letters, numbers, dot, dash, underscore and slash.")
    return v


def parse_money(value, label: str, places: int, *, required: bool = False) -> Decimal | None:
    if value is None or (isinstance(value, str) and not value.strip()):
        if required:
            raise BusinessRuleError(f"{label} is required.")
        return None
    try:
        d = Decimal(str(value).replace(",", "").strip())
    except (InvalidOperation, ValueError):
        raise BusinessRuleError(f"{label} must be a number.")
    if not d.is_finite() or d < 0:
        raise BusinessRuleError(f"{label} must be zero or more.")
    if d != d.quantize(Decimal(1).scaleb(-places)):
        raise BusinessRuleError(f"{label} can have at most {places} decimal places.")
    return d.quantize(Decimal(1).scaleb(-places))


# ---- categories --------------------------------------------------------------------------------------
def list_categories(db: Session, include_inactive: bool = False) -> list[Category]:
    q = select(Category).order_by(Category.name)
    if not include_inactive:
        q = q.where(Category.is_active.is_(True))
    return list(db.scalars(q).all())


def create_category(db: Session, *, actor: User, name: str, ip: str | None) -> Category:
    name = (name or "").strip()
    if not name:
        raise BusinessRuleError("Category name is required.")
    if db.scalar(select(Category.id).where(func.lower(Category.name) == name.lower())):
        raise ConflictError(f"Category '{name}' already exists.")
    c = Category(name=name)
    db.add(c)
    db.flush()
    audit_service.record(db, actor=actor, action="category.created", entity_type="category", entity_id=c.id,
                         after={"name": name}, ip=ip)
    return c


def get_or_create_category(db: Session, actor: User, name: str, ip: str | None) -> Category:
    c = db.scalar(select(Category).where(func.lower(Category.name) == name.strip().lower()))
    return c or create_category(db, actor=actor, name=name, ip=ip)


# ---- products ----------------------------------------------------------------------------------------
def get_product(db: Session, product_id: int) -> Product:
    p = db.get(Product, product_id)
    if p is None:
        raise NotFoundError("Product not found")
    return p


def get_variant(db: Session, variant_id: int) -> ProductVariant:
    v = db.get(ProductVariant, variant_id)
    if v is None:
        raise NotFoundError("Variant not found")
    return v


def search_products(db: Session, *, q: str | None, category_id: int | None, brand: str | None, status: str | None,
                    pricing_status: str | None, sort: str, desc: bool, page: int, page_size: int):
    stmt = select(Product)
    if q:
        like = f"%{q.strip()}%"
        sub = select(ProductVariant.product_id).where(or_(
            ProductVariant.sku.ilike(like), ProductVariant.barcode.ilike(like), ProductVariant.name.ilike(like),
            ProductVariant.shade_name.ilike(like)))
        stmt = stmt.where(or_(Product.name.ilike(like), Product.code.ilike(like), Product.brand.ilike(like),
                              Product.id.in_(sub)))
    if category_id:
        stmt = stmt.where(Product.category_id == category_id)
    if brand:
        stmt = stmt.where(func.lower(Product.brand) == brand.lower())
    if status:
        stmt = stmt.where(Product.status == status)
    if pricing_status:
        stmt = stmt.where(Product.id.in_(select(ProductVariant.product_id).where(
            ProductVariant.pricing_status == pricing_status)))
    total = db.scalar(select(func.count()).select_from(stmt.subquery())) or 0
    col = SORTS.get(sort, Product.name)
    stmt = stmt.order_by(col.desc() if desc else col.asc(), Product.id).offset((page - 1) * page_size).limit(page_size)
    return list(db.scalars(stmt).unique().all()), total


def brands(db: Session) -> list[str]:
    return [b for b in db.scalars(select(Product.brand).where(Product.brand.is_not(None)).distinct().order_by(Product.brand)) if b]


def _snap_product(p: Product) -> dict:
    return {"code": p.code, "name": p.name, "brand": p.brand, "category": p.category.name if p.category else None,
            "status": p.status, "tracks_batches": p.tracks_batches, "tracks_expiry": p.tracks_expiry,
            "discount_eligible": p.discount_eligible}


def _snap_variant(v: ProductVariant, *, with_prices: bool = True) -> dict:
    d = {"sku": v.sku, "barcode": v.barcode, "name": v.name, "shade_name": v.shade_name, "shade_hex": v.shade_hex,
         "size": v.size, "status": v.status, "reorder_threshold": v.reorder_threshold}
    if with_prices:
        d.update({k: (str(getattr(v, k)) if getattr(v, k) is not None else None) for k in PRICE_AUDIT_FIELDS})
    return d


def _check_product_fields(code, name, status, tracks_batches, tracks_expiry):
    if not (name or "").strip():
        raise BusinessRuleError("Product name is required.")
    if status not in STATUSES:
        raise BusinessRuleError("Status must be active, inactive or discontinued.")
    if tracks_batches is False and tracks_expiry:
        pass  # expiry without batches is allowed: expiry then belongs to the receipt line


def create_product(db: Session, *, actor: User, data: dict, can_price: bool, ip: str | None) -> Product:
    code = norm_code(data.get("code"), "Product code")
    _check_product_fields(code, data.get("name"), data.get("status", "active"), data.get("tracks_batches"),
                          data.get("tracks_expiry"))
    cat = db.get(Category, data.get("category_id"))
    if cat is None:
        raise BusinessRuleError("Choose a category.")
    if db.scalar(select(Product.id).where(Product.code == code)):
        raise ConflictError(f"Product code {code} is already used.")
    p = Product(code=code, name=data["name"].strip(), brand=(data.get("brand") or None), category_id=cat.id,
                description=data.get("description") or None, status=data.get("status", "active"),
                tracks_batches=bool(data.get("tracks_batches")), tracks_expiry=bool(data.get("tracks_expiry")),
                discount_eligible=bool(data.get("discount_eligible", True)), created_by=actor.id)
    db.add(p)
    db.flush()
    for vd in data.get("variants") or []:
        add_variant(db, actor=actor, product=p, data=vd, can_price=can_price, ip=ip, audit=False)
    db.refresh(p)
    audit_service.record(db, actor=actor, action="product.created", entity_type="product", entity_id=p.id,
                         after={**_snap_product(p), "variants": len(p.variants)}, ip=ip)
    return p


def update_product(db: Session, *, actor: User, product_id: int, data: dict, ip: str | None) -> Product:
    p = get_product(db, product_id)
    before = _snap_product(p)
    if "name" in data and data["name"] is not None:
        if not data["name"].strip():
            raise BusinessRuleError("Product name is required.")
        p.name = data["name"].strip()
    for f in ("brand", "description"):
        if f in data:
            setattr(p, f, data[f] or None)
    if data.get("category_id") is not None:
        if db.get(Category, data["category_id"]) is None:
            raise BusinessRuleError("Choose a category.")
        p.category_id = data["category_id"]
    if data.get("status") is not None:
        if data["status"] not in STATUSES:
            raise BusinessRuleError("Status must be active, inactive or discontinued.")
        p.status = data["status"]
    if "discount_eligible" in data and data["discount_eligible"] is not None:
        p.discount_eligible = data["discount_eligible"]
    for f in ("tracks_batches", "tracks_expiry"):
        if data.get(f) is not None and data[f] != getattr(p, f):
            if _has_movements(db, p):
                raise ConflictError("Batch and expiry tracking cannot change once stock has been recorded for this product.")
            setattr(p, f, data[f])
    db.flush()
    db.refresh(p)
    after = _snap_product(p)
    if after != before:
        audit_service.record(db, actor=actor, action="product.updated", entity_type="product", entity_id=p.id,
                             before=before, after=after, ip=ip)
    return p


def _has_movements(db: Session, p: Product) -> bool:
    return bool(db.scalar(select(StockMovement.id).where(
        StockMovement.variant_id.in_(select(ProductVariant.id).where(ProductVariant.product_id == p.id))).limit(1)))


def add_variant(db: Session, *, actor: User, product: Product, data: dict, can_price: bool, ip: str | None,
                audit: bool = True) -> ProductVariant:
    sku = norm_code(data.get("sku"), "SKU")
    name = (data.get("name") or "").strip()
    if not name:
        raise BusinessRuleError(f"{sku}: a variant name is required.")
    if db.scalar(select(ProductVariant.id).where(ProductVariant.sku == sku)):
        raise ConflictError(f"SKU {sku} is already used.")
    barcode = (data.get("barcode") or "").strip() or None
    if barcode and db.scalar(select(ProductVariant.id).where(ProductVariant.barcode == barcode)):
        raise ConflictError(f"Barcode {barcode} is already used.")
    hexv = (data.get("shade_hex") or "").strip() or None
    if hexv and not HEX_RE.match(hexv):
        raise BusinessRuleError(f"{sku}: shade colour must look like #A1B2C3.")
    v = ProductVariant(product_id=product.id, sku=sku, barcode=barcode, name=name,
                       shade_name=(data.get("shade_name") or None), shade_hex=hexv, size=(data.get("size") or None),
                       attributes=data.get("attributes") or {}, status=data.get("status", "active"), pricing_status="unpriced",
                       reorder_threshold=int(data.get("reorder_threshold") or 0))
    if v.status not in STATUSES:
        raise BusinessRuleError(f"{sku}: invalid status.")
    if v.reorder_threshold < 0:
        raise BusinessRuleError(f"{sku}: the reorder threshold cannot be negative.")
    priced = any(data.get(k) not in (None, "") for k in PRICE_AUDIT_FIELDS)
    if priced and not can_price:
        raise BusinessRuleError("You do not have permission to set prices.")
    _apply_prices(v, data, sku)
    db.add(v)
    try:
        db.flush()
    except IntegrityError:
        db.rollback()
        raise ConflictError(f"SKU {sku} or its barcode is already used.")
    if audit:
        audit_service.record(db, actor=actor, action="variant.created", entity_type="variant", entity_id=v.id,
                             after=_snap_variant(v), ip=ip)
    return v


def _apply_prices(v: ProductVariant, data: dict, sku: str) -> None:
    for f in PRICE_FIELDS:
        if f in data:
            setattr(v, f, parse_money(data[f], f"{sku}: {f.replace('_', ' ')}", 2))
    if "purchase_cost" in data:
        v.purchase_cost = parse_money(data["purchase_cost"], f"{sku}: purchase cost", 4)
    ps = data.get("pricing_status")
    if ps is not None:
        if ps not in PRICING:
            raise BusinessRuleError(f"{sku}: invalid pricing status.")
        v.pricing_status = ps
    elif v.pricing_status == "unpriced" and v.retail_price is not None and "retail_price" in data:
        v.pricing_status = "active"
    if v.pricing_status == "active" and v.retail_price is None:
        raise BusinessRuleError(f"{sku}: a retail price is required before the price can be marked active.")
    for f in ("employee_price", "distributor_price"):
        val = getattr(v, f)
        if val is not None and v.retail_price is not None and val > v.retail_price:
            raise BusinessRuleError(f"{sku}: {f.replace('_', ' ')} is higher than the retail price; check it.")


def update_variant(db: Session, *, actor: User, variant_id: int, data: dict, can_price: bool, ip: str | None) -> ProductVariant:
    v = get_variant(db, variant_id)
    before = _snap_variant(v)
    if any(k in data for k in PRICE_AUDIT_FIELDS) and not can_price:
        raise BusinessRuleError("You do not have permission to change prices.")
    if data.get("name") is not None:
        if not data["name"].strip():
            raise BusinessRuleError("A variant name is required.")
        v.name = data["name"].strip()
    if "barcode" in data:
        bc = (data["barcode"] or "").strip() or None
        if bc and bc != v.barcode and db.scalar(select(ProductVariant.id).where(ProductVariant.barcode == bc)):
            raise ConflictError(f"Barcode {bc} is already used.")
        v.barcode = bc
    for f in ("shade_name", "size"):
        if f in data:
            setattr(v, f, data[f] or None)
    if "shade_hex" in data:
        hx = (data["shade_hex"] or "").strip() or None
        if hx and not HEX_RE.match(hx):
            raise BusinessRuleError("Shade colour must look like #A1B2C3.")
        v.shade_hex = hx
    if data.get("status") is not None:
        if data["status"] not in STATUSES:
            raise BusinessRuleError("Invalid status.")
        v.status = data["status"]
    if data.get("reorder_threshold") is not None:
        if int(data["reorder_threshold"]) < 0:
            raise BusinessRuleError("The reorder threshold cannot be negative.")
        v.reorder_threshold = int(data["reorder_threshold"])
    _apply_prices(v, data, v.sku)
    db.flush()
    after = _snap_variant(v)
    if after != before:
        priced_changed = any(before.get(k) != after.get(k) for k in PRICE_AUDIT_FIELDS)
        audit_service.record(db, actor=actor, action="variant.price_changed" if priced_changed else "variant.updated",
                             entity_type="variant", entity_id=v.id, before=before, after=after, ip=ip)
    return v


def set_image(db: Session, *, actor: User, product_id: int, path: str | None, ip: str | None) -> Product:
    p = get_product(db, product_id)
    before = p.image_path
    p.image_path = path
    db.flush()
    audit_service.record(db, actor=actor, action="product.image_changed", entity_type="product", entity_id=p.id,
                         before={"image": before}, after={"image": path}, ip=ip)
    return p
