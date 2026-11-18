"""Bulk product import from Excel/CSV: template -> validate (nothing saved) -> review -> commit (all or nothing)."""
from __future__ import annotations

import csv
import hashlib
import io
from decimal import Decimal

from openpyxl import Workbook, load_workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter
from openpyxl.worksheet.datavalidation import DataValidation
from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.models import Category, ImportJob, Product, ProductVariant, User
from backend.services import audit_service, catalog_service
from backend.services.errors import BusinessRuleError, ConflictError, NotFoundError

MAX_BYTES = 5 * 1024 * 1024
MAX_ROWS = 5000

# (key, header, required, help)
PRODUCT_COLS = [
    ("product_id", "product_id", True, "Your own unique key for the product (e.g. P001). Variants point to it. Not stored."),
    ("code", "product_code", True, "Unique product code. Letters, numbers, - _ . /"),
    ("name", "product_name", True, "Display name"),
    ("category", "category", True, "Category name. New categories are created on import."),
    ("brand", "brand", False, ""),
    ("description", "description", False, ""),
    ("status", "status", False, "active (default), inactive or discontinued"),
    ("tracks_batches", "tracks_batches", False, "yes/no. Default no. Needed for batch-level stock."),
    ("tracks_expiry", "tracks_expiry", False, "yes/no. Default no."),
    ("discount_eligible", "discount_eligible", False, "yes/no. Default yes."),
]
VARIANT_COLS = [
    ("product_id", "product_id", True, "Must match a product_id on the Products sheet"),
    ("sku", "sku", True, "Unique SKU for this variant"),
    ("name", "variant_name", True, "Label shown in pickers, e.g. 'Rosewood 3.5g'"),
    ("barcode", "barcode", False, "Unique. Leave blank if none: nothing is invented."),
    ("shade_name", "shade_name", False, ""),
    ("shade_hex", "shade_hex", False, "Swatch colour like #A1B2C3"),
    ("size", "size", False, ""),
    ("purchase_cost", "purchase_cost", False, "Reference cost only. Stock value comes from real receipts."),
    ("retail_price", "retail_price", False, "Needed before the item can be sold"),
    ("employee_price", "employee_price", False, ""),
    ("distributor_price", "distributor_price", False, ""),
    ("reorder_threshold", "reorder_threshold", False, "Whole number, default 0"),
    ("status", "status", False, "active (default), inactive or discontinued"),
]
YES = {"yes", "y", "true", "1", "x"}
NO = {"no", "n", "false", "0", ""}


# ---- template ----------------------------------------------------------------------------------------
def build_template_xlsx() -> bytes:
    wb = Workbook()
    ins = wb.active
    ins.title = "Instructions"
    lines = [
        ("Product import template", True),
        ("", False),
        ("1. Fill the Products sheet: one row per product. 2. Fill the Variants sheet: one row per variant (shade/size).", False),
        ("3. Link them with product_id: use the same value on both sheets. It is only a link inside this file.", False),
        ("4. Columns marked REQUIRED must be filled. Optional columns may be left blank.", False),
        ("5. Upload the file in the app. You will see a review of errors BEFORE anything is saved. Nothing is saved until you confirm.", False),
        ("6. If any row has an error, nothing is imported. Fix the file and upload again.", False),
        ("7. This template contains no products, prices, barcodes or stock. Leave a field blank rather than guessing.", False),
        ("8. Stock quantities are NOT imported here. Enter opening stock separately with verified counts and costs.", False),
        ("", False),
        ("Products sheet columns", True),
    ]
    for t, bold in lines:
        ins.append([t])
        if bold:
            ins.cell(ins.max_row, 1).font = Font(bold=True, size=12)
    for k, h, req, hlp in PRODUCT_COLS:
        ins.append([f"{h}  [{'REQUIRED' if req else 'optional'}]  {hlp}"])
    ins.append([""])
    ins.append(["Variants sheet columns"])
    ins.cell(ins.max_row, 1).font = Font(bold=True, size=12)
    for k, h, req, hlp in VARIANT_COLS:
        ins.append([f"{h}  [{'REQUIRED' if req else 'optional'}]  {hlp}"])
    ins.column_dimensions["A"].width = 130

    for title, cols in (("Products", PRODUCT_COLS), ("Variants", VARIANT_COLS)):
        ws = wb.create_sheet(title)
        ws.append([c[1] for c in cols])
        ws.append([("REQUIRED" if c[2] else "optional") for c in cols])
        for i, c in enumerate(cols, 1):
            head = ws.cell(1, i)
            head.font = Font(bold=True, color="FFFFFF")
            head.fill = PatternFill("solid", fgColor="6B4E5C" if c[2] else "8C8C8C")
            head.alignment = Alignment(horizontal="left")
            sub = ws.cell(2, i)
            sub.font = Font(italic=True, size=9, color="B03A2E" if c[2] else "6B6B6B")
            ws.column_dimensions[get_column_letter(i)].width = max(16, len(c[1]) + 4)
        ws.freeze_panes = "A3"
        if title == "Products":
            dv = DataValidation(type="list", formula1='"active,inactive,discontinued"', allow_blank=True)
            ws.add_data_validation(dv)
            dv.add("G3:G1000")
            yn = DataValidation(type="list", formula1='"yes,no"', allow_blank=True)
            ws.add_data_validation(yn)
            yn.add("H3:J1000")
        else:
            dv = DataValidation(type="list", formula1='"active,inactive,discontinued"', allow_blank=True)
            ws.add_data_validation(dv)
            dv.add("M3:M1000")
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def build_template_csv() -> bytes:
    cols = [c[1] for c in PRODUCT_COLS] + [c[1] for c in VARIANT_COLS if c[0] != "product_id"]
    out = io.StringIO()
    csv.writer(out).writerow(cols)
    return out.getvalue().encode("utf-8")


# ---- parsing -----------------------------------------------------------------------------------------
def _cell(v):
    if v is None:
        return None
    if isinstance(v, float) and v.is_integer():
        v = int(v)
    s = str(v).strip()
    return s or None


def _read_rows(ws, cols, errors, sheet) -> list[tuple[int, dict]]:
    rows = list(ws.iter_rows(values_only=True))
    if not rows:
        errors.append({"sheet": sheet, "row": 1, "field": None, "message": f"The {sheet} sheet is empty."})
        return []
    header = [(_cell(h) or "").lower() for h in rows[0]]
    idx = {}
    for key, h, req, _ in cols:
        if h.lower() in header:
            idx[key] = header.index(h.lower())
        elif req:
            errors.append({"sheet": sheet, "row": 1, "field": h, "message": f"Required column '{h}' is missing."})
    out = []
    for n, r in enumerate(rows[1:], start=2):
        rec = {k: _cell(r[i]) if i < len(r) else None for k, i in idx.items()}
        if all(v is None for v in rec.values()):
            continue
        # skip the REQUIRED/optional hint row from our template
        if n == 2 and all((v or "").lower() in ("required", "optional") for v in rec.values() if v):
            continue
        out.append((n, rec))
    return out


def parse_file(file_name: str, data: bytes) -> tuple[list[dict], list[dict]]:
    """Return (records, errors). records = [{'ref':product_id,'row':n,'product':{...},'variants':[{'row':n,...}]}]."""
    errors: list[dict] = []
    if len(data) > MAX_BYTES:
        raise BusinessRuleError("The file is larger than 5 MB.")
    name = file_name.lower()
    prod_rows: list[tuple[int, dict]] = []
    var_rows: list[tuple[int, dict]] = []
    if name.endswith((".xlsx", ".xlsm")):
        try:
            wb = load_workbook(io.BytesIO(data), read_only=True, data_only=True)
        except Exception:
            raise BusinessRuleError("This does not look like a valid Excel (.xlsx) file.")
        for need in ("Products", "Variants"):
            if need not in wb.sheetnames:
                raise BusinessRuleError(f"The workbook has no '{need}' sheet. Use the downloaded template.")
        prod_rows = _read_rows(wb["Products"], PRODUCT_COLS, errors, "Products")
        var_rows = _read_rows(wb["Variants"], VARIANT_COLS, errors, "Variants")
    elif name.endswith(".csv"):
        try:
            text = data.decode("utf-8-sig")
        except UnicodeDecodeError:
            raise BusinessRuleError("The CSV must be saved as UTF-8.")
        reader = csv.reader(io.StringIO(text))
        rows = list(reader)
        if not rows:
            raise BusinessRuleError("The file is empty.")
        header = [h.strip().lower() for h in rows[0]]
        pidx = {k: header.index(h.lower()) for k, h, req, _ in PRODUCT_COLS if h.lower() in header}
        vidx = {k: header.index(h.lower()) for k, h, req, _ in VARIANT_COLS if h.lower() in header and k != "product_id"}
        for k, h, req, _ in PRODUCT_COLS:
            if req and k not in pidx:
                errors.append({"sheet": "CSV", "row": 1, "field": h, "message": f"Required column '{h}' is missing."})
        for k, h, req, _ in VARIANT_COLS:
            if req and k not in vidx and k != "product_id":
                errors.append({"sheet": "CSV", "row": 1, "field": h, "message": f"Required column '{h}' is missing."})
        seen_products: set[str] = set()
        for n, r in enumerate(rows[1:], start=2):
            if not any((c or "").strip() for c in r):
                continue
            g = lambda ix, k: (r[ix[k]].strip() or None) if k in ix and ix[k] < len(r) else None
            prec = {k: g(pidx, k) for k in pidx}
            if prec.get("product_id") and prec["product_id"] not in seen_products:
                seen_products.add(prec["product_id"])
                prod_rows.append((n, prec))
            vrec = {k: g(vidx, k) for k in vidx}
            vrec["product_id"] = prec.get("product_id")
            var_rows.append((n, vrec))
    else:
        raise BusinessRuleError("Upload an .xlsx or .csv file.")

    if len(prod_rows) + len(var_rows) > MAX_ROWS:
        raise BusinessRuleError(f"Too many rows (limit {MAX_ROWS}). Split the file.")
    products: dict[str, dict] = {}
    for n, rec in prod_rows:
        ref = rec.get("product_id")
        if not ref:
            errors.append({"sheet": "Products", "row": n, "field": "product_id", "message": "product_id is required."})
            continue
        if ref in products:
            errors.append({"sheet": "Products", "row": n, "field": "product_id",
                           "message": f"product_id '{ref}' appears twice (first on row {products[ref]['row']})."})
            continue
        products[ref] = {"ref": ref, "row": n, "product": rec, "variants": []}
    for n, rec in var_rows:
        ref = rec.get("product_id")
        if not ref or ref not in products:
            errors.append({"sheet": "Variants", "row": n, "field": "product_id",
                           "message": f"product_id '{ref or ''}' does not match any row on the Products sheet."})
            continue
        products[ref]["variants"].append({"row": n, **rec})
    return list(products.values()), errors


# ---- validation --------------------------------------------------------------------------------------
def _bool(v, default: bool) -> bool | None:
    if v is None:
        return default
    s = v.strip().lower()
    if s in YES:
        return True
    if s in NO:
        return False
    return None


def validate_records(db: Session, records: list[dict], mode: str, can_price: bool) -> tuple[list[dict], list[dict], dict]:
    """Returns (normalised records, errors, summary). Never writes."""
    errors: list[dict] = []
    out: list[dict] = []
    seen_codes: dict[str, int] = {}
    seen_skus: dict[str, int] = {}
    seen_barcodes: dict[str, int] = {}
    new_p = upd_p = skip_p = new_v = upd_v = 0

    def err(sheet, row, field, msg):
        errors.append({"sheet": sheet, "row": row, "field": field, "message": msg})

    for rec in records:
        p, prow = rec["product"], rec["row"]
        norm: dict = {"ref": rec["ref"], "row": prow, "variants": []}
        ok = True
        try:
            code = catalog_service.norm_code(p.get("code"), "product_code")
        except BusinessRuleError as e:
            err("Products", prow, "product_code", str(e))
            code, ok = None, False
        if code:
            if code in seen_codes:
                err("Products", prow, "product_code", f"Duplicate product code {code} (also on row {seen_codes[code]}).")
                ok = False
            seen_codes[code] = prow
        if not p.get("name"):
            err("Products", prow, "product_name", "product_name is required.")
            ok = False
        if not p.get("category"):
            err("Products", prow, "category", "category is required.")
            ok = False
        status = (p.get("status") or "active").lower()
        if status not in catalog_service.STATUSES:
            err("Products", prow, "status", "status must be active, inactive or discontinued.")
            ok = False
        flags = {}
        for k, d in (("tracks_batches", False), ("tracks_expiry", False), ("discount_eligible", True)):
            b = _bool(p.get(k), d)
            if b is None:
                err("Products", prow, k, f"{k} must be yes or no.")
                ok = False
            flags[k] = b
        existing = db.scalar(select(Product).where(Product.code == code)) if code else None
        action = "create"
        if existing is not None:
            action = "skip" if mode == "skip" else "update"
        if not rec["variants"] and existing is None:
            err("Products", prow, "product_id", f"Product '{rec['ref']}' has no rows on the Variants sheet.")
            ok = False
        norm.update({"action": action, "existing_id": existing.id if existing else None,
                     "product": {"code": code, "name": p.get("name"), "brand": p.get("brand"),
                                 "category": (p.get("category") or "").strip(), "description": p.get("description"),
                                 "status": status, **flags}})
        for v in rec["variants"]:
            vrow = v["row"]
            vok = True
            nv = {"row": vrow}
            try:
                sku = catalog_service.norm_code(v.get("sku"), "sku")
            except BusinessRuleError as e:
                err("Variants", vrow, "sku", str(e))
                sku, vok = None, False
            if sku:
                if sku in seen_skus:
                    err("Variants", vrow, "sku", f"Duplicate SKU {sku} (also on row {seen_skus[sku]}).")
                    vok = False
                seen_skus[sku] = vrow
            if not v.get("name"):
                err("Variants", vrow, "variant_name", "variant_name is required.")
                vok = False
            bc = v.get("barcode")
            if bc:
                if bc in seen_barcodes:
                    err("Variants", vrow, "barcode", f"Duplicate barcode {bc} (also on row {seen_barcodes[bc]}).")
                    vok = False
                seen_barcodes[bc] = vrow
            hx = v.get("shade_hex")
            if hx and not catalog_service.HEX_RE.match(hx):
                err("Variants", vrow, "shade_hex", "shade_hex must look like #A1B2C3.")
                vok = False
            vstatus = (v.get("status") or "active").lower()
            if vstatus not in catalog_service.STATUSES:
                err("Variants", vrow, "status", "status must be active, inactive or discontinued.")
                vok = False
            nums = {}
            for f, places in (("purchase_cost", 4), ("retail_price", 2), ("employee_price", 2), ("distributor_price", 2)):
                try:
                    nums[f] = catalog_service.parse_money(v.get(f), f, places)
                except BusinessRuleError as e:
                    err("Variants", vrow, f, str(e))
                    vok = False
            if any(nums.get(f) is not None for f in nums) and not can_price:
                err("Variants", vrow, "retail_price", "You do not have permission to import prices. Remove price columns.")
                vok = False
            for f in ("employee_price", "distributor_price"):
                if nums.get(f) is not None and nums.get("retail_price") is not None and nums[f] > nums["retail_price"]:
                    err("Variants", vrow, f, f"{f} is higher than retail_price.")
                    vok = False
            thr = v.get("reorder_threshold")
            try:
                thr_i = int(Decimal(thr)) if thr not in (None, "") else 0
                if thr_i < 0 or (thr and Decimal(thr) != thr_i):
                    raise ValueError
            except Exception:
                err("Variants", vrow, "reorder_threshold", "reorder_threshold must be a whole number, zero or more.")
                vok = False
                thr_i = 0
            if sku:
                dup = db.scalar(select(ProductVariant).where(ProductVariant.sku == sku))
                if dup is not None and not (existing is not None and dup.product_id == existing.id):
                    err("Variants", vrow, "sku", f"SKU {sku} already belongs to another product in the catalogue.")
                    vok = False
                if dup is not None and existing is not None and dup.product_id == existing.id and action != "update":
                    nv["action"] = "skip"
            if bc:
                bdup = db.scalar(select(ProductVariant).where(ProductVariant.barcode == bc))
                if bdup is not None and bdup.sku != sku:
                    err("Variants", vrow, "barcode", f"Barcode {bc} is already used by SKU {bdup.sku}.")
                    vok = False
            nv.update({"sku": sku, "name": v.get("name"), "barcode": bc, "shade_name": v.get("shade_name"),
                       "shade_hex": hx, "size": v.get("size"), "status": vstatus, "reorder_threshold": thr_i,
                       **{k: (str(x) if x is not None else None) for k, x in nums.items()}})
            if sku:
                exv = db.scalar(select(ProductVariant.id).where(ProductVariant.sku == sku))
                nv.setdefault("action", "update" if exv else "create")
                if action == "skip":
                    nv["action"] = "skip"
            ok = ok and vok
            norm["variants"].append(nv)
        norm["valid"] = ok
        out.append(norm)
        if ok:
            if action == "create":
                new_p += 1
            elif action == "update":
                upd_p += 1
            else:
                skip_p += 1
            for nv in norm["variants"]:
                if nv.get("action") == "create":
                    new_v += 1
                elif nv.get("action") == "update":
                    upd_v += 1
    summary = {"products_total": len(records), "products_new": new_p, "products_updated": upd_p,
               "products_skipped": skip_p, "variants_new": new_v, "variants_updated": upd_v,
               "error_count": len(errors), "rows_with_errors": len({(e["sheet"], e["row"]) for e in errors})}
    return out, errors, summary


def validate_upload(db: Session, *, actor: User, file_name: str, data: bytes, mode: str, can_price: bool,
                    ip: str | None) -> ImportJob:
    if mode not in ("skip", "update"):
        raise BusinessRuleError("Mode must be 'skip' or 'update'.")
    records, perrors = parse_file(file_name, data)
    if not records and not perrors:
        raise BusinessRuleError("The file contains no products.")
    normalised, verrors, summary = validate_records(db, records, mode, can_price)
    errors = perrors + verrors
    summary["error_count"] = len(errors)
    summary["rows_with_errors"] = len({(e["sheet"], e["row"]) for e in errors})
    job = ImportJob(kind="products", file_name=file_name[:255], file_sha256=hashlib.sha256(data).hexdigest(),
                    status="validated", options={"mode": mode}, summary=summary, records=normalised, errors=errors,
                    created_by=actor.id)
    db.add(job)
    db.flush()
    audit_service.record(db, actor=actor, action="import.validated", entity_type="import_job", entity_id=job.id,
                         after={"file": file_name, "errors": len(errors), **summary}, ip=ip)
    return job


def get_job(db: Session, job_id: int, *, lock: bool = False) -> ImportJob:
    q = select(ImportJob).where(ImportJob.id == job_id)
    if lock:
        q = q.with_for_update().execution_options(populate_existing=True)
    j = db.scalar(q)
    if j is None:
        raise NotFoundError("Import not found")
    return j


def commit_job(db: Session, *, actor: User, job_id: int, can_price: bool, ip: str | None) -> ImportJob:
    """Apply a validated import in the caller's single transaction. Anything wrong => exception => full rollback."""
    job = get_job(db, job_id, lock=True)
    if job.status == "committed":
        raise ConflictError("This import was already committed.")
    if job.status != "validated":
        raise ConflictError(f"This import is {job.status}.")
    if job.errors:
        raise BusinessRuleError("This file has errors. Nothing was imported. Fix the file and upload it again.")
    # re-validate against the live catalogue: it may have changed since the review
    recs = [{"ref": r["ref"], "row": r["row"], "product": _denorm_product(r), "variants": [_denorm_var(v) for v in r["variants"]]}
            for r in job.records]
    _, errors, _ = validate_records(db, recs, job.options.get("mode", "skip"), can_price)
    if errors:
        raise ConflictError("The catalogue changed since the review and the file no longer validates. Upload it again.")

    created_p = updated_p = skipped_p = created_v = updated_v = 0
    for r in job.records:
        pr = r["product"]
        if r["action"] == "skip":
            skipped_p += 1
            continue
        cat = catalog_service.get_or_create_category(db, actor, pr["category"], ip)
        pdata = {**{k: pr[k] for k in ("name", "brand", "description", "status", "tracks_batches", "tracks_expiry",
                                         "discount_eligible")}, "category_id": cat.id}
        if r["action"] == "create":
            product = catalog_service.create_product(db, actor=actor, data={"code": pr["code"], **pdata}, can_price=can_price, ip=ip)
            created_p += 1
            created_v += len(r["variants"])
            # create_product only receives variants through its data; add them here
            for v in r["variants"]:
                vd = _variant_data(v)
                catalog_service.add_variant(db, actor=actor, product=product, data=vd, can_price=can_price, ip=ip)
            continue
        product = catalog_service.update_product(db, actor=actor, product_id=r["existing_id"], data={
            k: v for k, v in pdata.items() if k not in ("tracks_batches", "tracks_expiry")}, ip=ip)
        updated_p += 1
        for v in r["variants"]:
            existing = db.scalar(select(ProductVariant).where(ProductVariant.sku == v["sku"]))
            vd = _variant_data(v)
            if existing is None:
                catalog_service.add_variant(db, actor=actor, product=product, data=vd, can_price=can_price, ip=ip)
                created_v += 1
            else:
                vd.pop("sku", None)
                catalog_service.update_variant(db, actor=actor, variant_id=existing.id, data=vd, can_price=can_price, ip=ip)
                updated_v += 1
    # note: the create path above double counts variants only if create_product were given variants; it is not.
    job.status = "committed"
    from datetime import datetime, timezone
    job.committed_at = datetime.now(timezone.utc)
    job.result = {"products_created": created_p, "products_updated": updated_p, "products_skipped": skipped_p,
                  "variants_created": created_v, "variants_updated": updated_v}
    db.flush()
    audit_service.record(db, actor=actor, action="import.committed", entity_type="import_job", entity_id=job.id,
                         after={"file": job.file_name, **job.result}, ip=ip)
    return job


def _variant_data(v: dict) -> dict:
    keys = ("sku", "name", "barcode", "shade_name", "shade_hex", "size", "status", "reorder_threshold",
            "purchase_cost", "retail_price", "employee_price", "distributor_price")
    return {k: v.get(k) for k in keys if k in v}


def _denorm_product(r: dict) -> dict:
    p = r["product"]
    yn = lambda b: "yes" if b else "no"
    return {"product_id": r["ref"], "code": p["code"], "name": p["name"], "brand": p["brand"], "category": p["category"],
            "description": p["description"], "status": p["status"], "tracks_batches": yn(p["tracks_batches"]),
            "tracks_expiry": yn(p["tracks_expiry"]), "discount_eligible": yn(p["discount_eligible"])}


def _denorm_var(v: dict) -> dict:
    d = {k: v.get(k) for k in ("sku", "name", "barcode", "shade_name", "shade_hex", "size", "status",
                               "purchase_cost", "retail_price", "employee_price", "distributor_price")}
    d["reorder_threshold"] = str(v.get("reorder_threshold", 0))
    d["row"] = v["row"]
    return d
