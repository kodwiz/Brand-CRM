"""Catalogue endpoints: categories, products, variants, images, bulk import."""
from __future__ import annotations

import uuid
from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, Request, Response, UploadFile
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session

from backend.api.common import commit_or_http
from backend.config import get_settings
from backend.db import get_db
from backend.deps import Principal, client_ip, require
from backend.models import Product, ProductVariant
from backend.schemas.inventory import CategoryIn, ImportCommitIn, ProductIn, ProductUpdate, VariantIn, VariantUpdate
from backend.services import catalog_service, import_service
from backend.services.errors import BusinessRuleError, NotFoundError

router = APIRouter(tags=["catalogue"])

MAGIC = {b"\x89PNG\r\n\x1a\n": ".png", b"\xff\xd8\xff": ".jpg", b"RIFF": ".webp"}
MAX_IMAGE = 2 * 1024 * 1024


def variant_out(v: ProductVariant, p: Principal) -> dict:
    d = {"id": v.id, "product_id": v.product_id, "sku": v.sku, "barcode": v.barcode, "name": v.name,
         "shade_name": v.shade_name, "shade_hex": v.shade_hex, "size": v.size, "status": v.status,
         "pricing_status": v.pricing_status, "reorder_threshold": v.reorder_threshold,
         "retail_price": str(v.retail_price) if v.retail_price is not None else None}
    if p.has("products.view_prices"):
        d["employee_price"] = str(v.employee_price) if v.employee_price is not None else None
        d["distributor_price"] = str(v.distributor_price) if v.distributor_price is not None else None
    if p.has("products.view_cost"):
        d["purchase_cost"] = str(v.purchase_cost) if v.purchase_cost is not None else None
    return d


def product_out(pr: Product, p: Principal, *, detail: bool = True) -> dict:
    d = {"id": pr.id, "code": pr.code, "name": pr.name, "brand": pr.brand, "category_id": pr.category_id,
         "category": pr.category.name, "status": pr.status, "has_image": bool(pr.image_path),
         "tracks_batches": pr.tracks_batches, "tracks_expiry": pr.tracks_expiry,
         "discount_eligible": pr.discount_eligible, "variant_count": len(pr.variants),
         "unpriced_variants": sum(1 for v in pr.variants if v.pricing_status != "active"),
         "swatches": [v.shade_hex for v in pr.variants if v.shade_hex][:6], "updated_at": pr.updated_at}
    if detail:
        d["description"] = pr.description
        d["variants"] = [variant_out(v, p) for v in pr.variants]
    return d


@router.get("/categories")
def categories(include_inactive: bool = False, p: Principal = Depends(require("products.view")), db: Session = Depends(get_db)):
    return [{"id": c.id, "name": c.name, "is_active": c.is_active} for c in catalog_service.list_categories(db, include_inactive)]


@router.post("/categories", status_code=201)
def create_category(body: CategoryIn, request: Request, p: Principal = Depends(require("products.manage")),
                    db: Session = Depends(get_db)):
    c = commit_or_http(db, lambda: catalog_service.create_category(db, actor=p.user, name=body.name, ip=client_ip(request)))
    return {"id": c.id, "name": c.name, "is_active": c.is_active}


@router.get("/products")
def list_products(q: str | None = None, category_id: int | None = None, brand: str | None = None,
                  status: str | None = None, pricing_status: str | None = None, sort: str = "name", desc: bool = False,
                  page: int = Query(1, ge=1), page_size: int = Query(50, ge=1, le=200),
                  p: Principal = Depends(require("products.view")), db: Session = Depends(get_db)):
    items, total = catalog_service.search_products(db, q=q, category_id=category_id, brand=brand, status=status,
                                                   pricing_status=pricing_status, sort=sort, desc=desc, page=page,
                                                   page_size=page_size)
    return {"items": [product_out(i, p, detail=False) for i in items], "total": total, "page": page, "page_size": page_size}


@router.get("/products/brands")
def product_brands(p: Principal = Depends(require("products.view")), db: Session = Depends(get_db)):
    return catalog_service.brands(db)


@router.post("/products", status_code=201)
def create_product(body: ProductIn, request: Request, p: Principal = Depends(require("products.manage")),
                   db: Session = Depends(get_db)):
    data = body.model_dump()
    pr = commit_or_http(db, lambda: catalog_service.create_product(
        db, actor=p.user, data=data, can_price=p.has("products.manage_prices"), ip=client_ip(request)))
    return product_out(pr, p)


@router.get("/products/{product_id}")
def get_product(product_id: int, p: Principal = Depends(require("products.view")), db: Session = Depends(get_db)):
    try:
        return product_out(catalog_service.get_product(db, product_id), p)
    except NotFoundError as e:
        raise HTTPException(404, "Product not found") from e


@router.patch("/products/{product_id}")
def update_product(product_id: int, body: ProductUpdate, request: Request,
                   p: Principal = Depends(require("products.manage")), db: Session = Depends(get_db)):
    pr = commit_or_http(db, lambda: catalog_service.update_product(
        db, actor=p.user, product_id=product_id, data=body.model_dump(exclude_unset=True), ip=client_ip(request)))
    return product_out(pr, p)


@router.post("/products/{product_id}/variants", status_code=201)
def add_variant(product_id: int, body: VariantIn, request: Request, p: Principal = Depends(require("products.manage")),
                db: Session = Depends(get_db)):
    def run():
        pr = catalog_service.get_product(db, product_id)
        return catalog_service.add_variant(db, actor=p.user, product=pr, data=body.model_dump(),
                                           can_price=p.has("products.manage_prices"), ip=client_ip(request))
    return variant_out(commit_or_http(db, run), p)


@router.patch("/variants/{variant_id}")
def update_variant(variant_id: int, body: VariantUpdate, request: Request,
                   p: Principal = Depends(require("products.manage")), db: Session = Depends(get_db)):
    v = commit_or_http(db, lambda: catalog_service.update_variant(
        db, actor=p.user, variant_id=variant_id, data=body.model_dump(exclude_unset=True),
        can_price=p.has("products.manage_prices"), ip=client_ip(request)))
    return variant_out(v, p)


@router.get("/variants/lookup")
def lookup_variants(q: str = Query(min_length=1), limit: int = Query(20, ge=1, le=50),
                    p: Principal = Depends(require("products.view")), db: Session = Depends(get_db)):
    """Picker search by SKU, barcode or name."""
    from sqlalchemy import or_, select
    like = f"%{q.strip()}%"
    rows = db.scalars(select(ProductVariant).where(or_(
        ProductVariant.sku.ilike(like), ProductVariant.barcode.ilike(like), ProductVariant.name.ilike(like)))
        .order_by(ProductVariant.sku).limit(limit)).all()
    return [{"id": v.id, "sku": v.sku, "name": v.name, "product_name": v.product.name, "shade_hex": v.shade_hex,
             "tracks_batches": v.product.tracks_batches, "tracks_expiry": v.product.tracks_expiry, "status": v.status}
            for v in rows]


# ---- images ----------------------------------------------------------------------------------------------
def _media_dir() -> Path:
    d = Path(get_settings().media_dir) / "products"
    d.mkdir(parents=True, exist_ok=True)
    return d


@router.put("/products/{product_id}/image")
async def upload_image(product_id: int, request: Request, file: UploadFile = File(...),
                       p: Principal = Depends(require("products.manage")), db: Session = Depends(get_db)):
    data = await file.read(MAX_IMAGE + 1)
    if len(data) > MAX_IMAGE:
        raise HTTPException(413, "The image is larger than 2 MB.")
    ext = next((e for m, e in MAGIC.items() if data.startswith(m)), None)
    if ext is None or (ext == ".webp" and data[8:12] != b"WEBP"):
        raise HTTPException(422, "Use a PNG, JPEG or WebP image.")
    name = f"{product_id}-{uuid.uuid4().hex[:12]}{ext}"
    path = _media_dir() / name
    path.write_bytes(data)
    try:
        commit_or_http(db, lambda: catalog_service.set_image(db, actor=p.user, product_id=product_id, path=name, ip=client_ip(request)))
    except Exception:
        path.unlink(missing_ok=True)
        raise
    return {"ok": True}


@router.get("/products/{product_id}/image")
def get_image(product_id: int, p: Principal = Depends(require("products.view")), db: Session = Depends(get_db)):
    pr = db.get(Product, product_id)
    if pr is None or not pr.image_path:
        raise HTTPException(404, "No image")
    f = _media_dir() / Path(pr.image_path).name  # basename only: no path traversal
    if not f.exists():
        raise HTTPException(404, "No image")
    return FileResponse(f)


# ---- import ----------------------------------------------------------------------------------------------
@router.get("/imports/template")
def import_template(format: str = "xlsx", p: Principal = Depends(require("products.import"))):
    if format == "csv":
        return Response(import_service.build_template_csv(), media_type="text/csv",
                        headers={"Content-Disposition": 'attachment; filename="product-import-template.csv"'})
    return Response(import_service.build_template_xlsx(),
                    media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    headers={"Content-Disposition": 'attachment; filename="product-import-template.xlsx"'})


def job_out(j, *, full: bool = False) -> dict:
    d = {"id": j.id, "file_name": j.file_name, "status": j.status, "mode": j.options.get("mode"),
         "summary": j.summary, "error_count": len(j.errors), "created_at": j.created_at,
         "committed_at": j.committed_at, "result": j.result}
    if full:
        d["errors"] = j.errors[:1000]
        d["errors_truncated"] = len(j.errors) > 1000
        d["records"] = [{"ref": r["ref"], "row": r["row"], "action": r["action"], "valid": r["valid"],
                         "code": r["product"]["code"], "name": r["product"]["name"], "variants": len(r["variants"]),
                         "variant_skus": [v["sku"] for v in r["variants"]][:20]} for r in j.records]
    return d


@router.post("/imports/validate", status_code=201)
async def validate_import(request: Request, file: UploadFile = File(...), mode: str = Form("skip"),
                          p: Principal = Depends(require("products.import")), db: Session = Depends(get_db)):
    data = await file.read(import_service.MAX_BYTES + 1)
    job = commit_or_http(db, lambda: import_service.validate_upload(
        db, actor=p.user, file_name=file.filename or "upload", data=data, mode=mode,
        can_price=p.has("products.manage_prices"), ip=client_ip(request)))
    return job_out(job, full=True)


@router.get("/imports/{job_id}")
def get_import(job_id: int, p: Principal = Depends(require("products.import")), db: Session = Depends(get_db)):
    return job_out(commit_or_http(db, lambda: import_service.get_job(db, job_id)), full=True)


@router.post("/imports/{job_id}/commit")
def commit_import(job_id: int, body: ImportCommitIn, request: Request, p: Principal = Depends(require("products.import")),
                  db: Session = Depends(get_db)):
    if not body.confirm:
        raise HTTPException(422, "Confirm the import to proceed.")
    job = commit_or_http(db, lambda: import_service.commit_job(
        db, actor=p.user, job_id=job_id, can_price=p.has("products.manage_prices"), ip=client_ip(request)))
    return job_out(job)
