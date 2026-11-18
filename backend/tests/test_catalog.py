import io

from openpyxl import Workbook, load_workbook

from backend.tests.conftest import login, make_user


def test_product_and_variant_crud_and_validation(client, auth, catalog):
    h = auth["ceo"]
    p = client.get(f"/products/{catalog['plain']['id']}", headers=h).json()
    assert p["code"] == "TST-LIP" and len(p["variants"]) == 2
    # duplicate product code (case-insensitive) and duplicate SKU / barcode are refused
    r = client.post("/products", headers=h, json={"code": "tst-lip", "name": "X", "category_id": catalog["cat"]["id"]})
    assert r.status_code == 409
    r = client.post(f"/products/{p['id']}/variants", headers=h, json={"sku": "tst-lip-r", "name": "Dup"})
    assert r.status_code == 409
    r = client.post(f"/products/{p['id']}/variants", headers=h, json={"sku": "NEW1", "name": "N", "barcode": "9000000000011"})
    assert r.status_code == 409
    # invalid prices
    for bad in ("-1", "abc", "10.999"):
        r = client.post(f"/products/{p['id']}/variants", headers=h, json={"sku": "BAD", "name": "N", "retail_price": bad})
        assert r.status_code == 422, bad
    r = client.post(f"/products/{p['id']}/variants", headers=h,
                    json={"sku": "BAD", "name": "N", "retail_price": "10", "employee_price": "20"})
    assert r.status_code == 422
    assert client.post(f"/products/{p['id']}/variants", headers=h, json={"sku": "BAD", "name": "N", "shade_hex": "red"}).status_code == 422
    # missing required fields
    assert client.post("/products", headers=h, json={"code": "A", "category_id": catalog["cat"]["id"]}).status_code == 422
    assert client.post("/products", headers=h, json={"code": "A", "name": "A", "category_id": 99999}).status_code == 422
    # edit + status
    r = client.patch(f"/products/{p['id']}", headers=h, json={"status": "discontinued", "brand": "B2"})
    assert r.status_code == 200 and r.json()["status"] == "discontinued"
    r = client.patch(f"/variants/{catalog['rose']}", headers=h, json={"retail_price": "120.00"})
    assert r.json()["retail_price"] == "120.00"


def test_search_sort_filter_pagination(client, auth, catalog):
    h = auth["ceo"]
    assert client.get("/products?q=serum", headers=h).json()["total"] == 1
    assert client.get("/products?q=TST-LIP-N", headers=h).json()["total"] == 1  # variant SKU finds the product
    assert client.get("/products?q=9000000000011", headers=h).json()["total"] == 1  # barcode
    assert client.get("/products?brand=testbrand", headers=h).json()["total"] == 1
    r = client.get("/products?page_size=1&sort=name&desc=true", headers=h).json()
    assert r["total"] == 2 and r["items"][0]["name"] == "Test Serum"
    assert client.get("/products?pricing_status=active", headers=h).json()["total"] == 2
    assert client.post(f"/products/{catalog['plain']['id']}/variants", headers=h,
                       json={"sku": "NOPRICE", "name": "x"}).json()["pricing_status"] == "unpriced"
    assert client.get("/products?pricing_status=unpriced", headers=h).json()["total"] == 1


def test_price_visibility_and_edit_permissions(client, db, auth, catalog, locations):
    # order booker may see products and retail price but not cost/employee/distributor prices, and cannot edit
    h = auth["booker"]
    v = client.get(f"/products/{catalog['plain']['id']}", headers=h).json()["variants"][0]
    assert "retail_price" in v and "purchase_cost" not in v and "employee_price" not in v
    assert client.post("/products", headers=h, json={"code": "Z", "name": "Z", "category_id": 1}).status_code == 403
    assert client.patch(f"/variants/{catalog['rose']}", headers=h, json={"retail_price": "1"}).status_code == 403
    # a user who can manage products but not prices cannot set or change prices
    from backend.models import Role, RolePermission
    role = db.query(Role).filter_by(code="stock_keeper").one()
    db.add(RolePermission(role_id=role.id, permission="products.manage")); db.commit()
    hs = login(client, "stock")
    r = client.patch(f"/variants/{catalog['rose']}", headers=hs, json={"retail_price": "1"})
    assert r.status_code == 422
    r = client.post(f"/products/{catalog['plain']['id']}/variants", headers=hs, json={"sku": "Q1", "name": "Q"})
    assert r.status_code == 201  # no prices -> allowed, stays unpriced
    assert r.json()["pricing_status"] == "unpriced"


def test_catalogue_changes_are_audited(client, auth, catalog):
    h = auth["ceo"]
    client.patch(f"/variants/{catalog['rose']}", headers=h, json={"retail_price": "111.00"})
    rows = client.get("/audit?entity_type=variant&page_size=50", headers=h).json()["items"]
    changed = [a for a in rows if a["action"] == "variant.price_changed"]
    assert changed and changed[0]["before"]["retail_price"] == "100.00" and changed[0]["after"]["retail_price"] == "111.00"
    assert any(a["action"] == "product.created" for a in client.get("/audit?entity_type=product", headers=h).json()["items"])


def test_image_upload_validation(client, auth, catalog, tmp_path, monkeypatch):
    from backend.config import get_settings
    monkeypatch.setattr(get_settings(), "media_dir", str(tmp_path))
    h = auth["ceo"]
    pid = catalog["plain"]["id"]
    png = b"\x89PNG\r\n\x1a\n" + b"0" * 50
    ok = client.put(f"/products/{pid}/image", headers=h, files={"file": ("a.png", png, "image/png")})
    assert ok.status_code == 200
    assert client.get(f"/products/{pid}/image", headers=h).content == png
    bad = client.put(f"/products/{pid}/image", headers=h, files={"file": ("a.png", b"<html>not an image", "image/png")})
    assert bad.status_code == 422
    big = client.put(f"/products/{pid}/image", headers=h, files={"file": ("a.png", png + b"0" * (3 * 1024 * 1024), "image/png")})
    assert big.status_code == 413


# ---- import ---------------------------------------------------------------------------------------------
def _xlsx(products, variants):
    wb = Workbook()
    ws = wb.active; ws.title = "Products"
    ws.append(["product_id", "product_code", "product_name", "category", "brand", "description", "status",
               "tracks_batches", "tracks_expiry", "discount_eligible"])
    for r in products: ws.append(r)
    wv = wb.create_sheet("Variants")
    wv.append(["product_id", "sku", "variant_name", "barcode", "shade_name", "shade_hex", "size", "purchase_cost",
               "retail_price", "employee_price", "distributor_price", "reorder_threshold", "status"])
    for r in variants: wv.append(r)
    b = io.BytesIO(); wb.save(b); return b.getvalue()


XLSX = ("import.xlsx", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")


def test_template_download_is_blank_and_marked(client, auth):
    r = client.get("/imports/template", headers=auth["ceo"])
    assert r.status_code == 200
    wb = load_workbook(io.BytesIO(r.content))
    assert wb.sheetnames == ["Instructions", "Products", "Variants"]
    assert [c.value for c in wb["Products"][1]][:3] == ["product_id", "product_code", "product_name"]
    assert wb["Products"].max_row == 2 and wb["Variants"].max_row == 2  # header + REQUIRED/optional row only
    assert wb["Products"]["A2"].value == "REQUIRED" and wb["Products"]["E2"].value == "optional"
    assert client.get("/imports/template?format=csv", headers=auth["ceo"]).status_code == 200


def test_import_validate_review_commit(client, auth, catalog):
    h = auth["ceo"]
    data = _xlsx([["P1", "imp-1", "Imported One", "New Cat", "B", None, None, "no", "no", "yes"]],
                 [["P1", "imp-1-a", "Alpha", None, "Alpha", "#112233", None, None, "50", None, None, 3, None],
                  ["P1", "imp-1-b", "Beta", None, None, None, "5ml", None, "55", None, None, None, None]])
    v = client.post("/imports/validate", headers=h, files={"file": (*XLSX[:1], data, XLSX[1])}, data={"mode": "skip"})
    assert v.status_code == 201, v.text
    j = v.json()
    assert j["error_count"] == 0 and j["summary"]["products_new"] == 1 and j["summary"]["variants_new"] == 2
    # validation alone saves nothing
    assert client.get("/products?q=imp-1", headers=h).json()["total"] == 0
    assert client.post(f"/imports/{j['id']}/commit", headers=h, json={"confirm": False}).status_code == 422
    c = client.post(f"/imports/{j['id']}/commit", headers=h, json={"confirm": True})
    assert c.status_code == 200 and c.json()["result"]["products_created"] == 1
    assert client.get("/products?q=imp-1", headers=h).json()["total"] == 1
    # committing twice is refused
    assert client.post(f"/imports/{j['id']}/commit", headers=h, json={"confirm": True}).status_code == 409
    # re-uploading the same file in skip mode skips the existing product
    v2 = client.post("/imports/validate", headers=h, files={"file": (*XLSX[:1], data, XLSX[1])}, data={"mode": "skip"}).json()
    assert v2["summary"]["products_skipped"] == 1 and v2["error_count"] == 0


def test_import_with_errors_is_all_or_nothing(client, auth, catalog):
    h = auth["ceo"]
    data = _xlsx(
        [["P1", "ok-1", "Fine", "C", None, None, None, None, None, None],
         ["P2", "tst-lip", "Dup code is fine (skip mode)", "C", None, None, None, None, None, None],
         ["P3", "bad-3", "", "C", None, None, "weird", None, None, None]],
        [["P1", "ok-1-a", "A", None, None, None, None, None, "10", None, None, None, None],
         ["P1", "TST-LIP-R", "Existing sku other product", None, None, None, None, None, "10", None, None, None, None],
         ["P1", "ok-1-a", "Duplicate in file", None, None, None, None, None, "10", None, None, None, None],
         ["P3", "bad-3-a", "A", "9000000000011", None, "nothex", None, None, "-5", None, None, None, None],
         ["P9", "orphan", "Orphan", None, None, None, None, None, None, None, None, None, None]])
    j = client.post("/imports/validate", headers=h, files={"file": (*XLSX[:1], data, XLSX[1])}).json()
    assert j["error_count"] >= 7
    msgs = " | ".join(e["message"] for e in j["errors"])
    for needle in ("already belongs to another product", "Duplicate SKU", "already used by SKU", "shade_hex",
                   "retail_price", "does not match any row", "product_name is required", "status must be"):
        assert needle in msgs, needle
    c = client.post(f"/imports/{j['id']}/commit", headers=h, json={"confirm": True})
    assert c.status_code == 422
    # nothing at all was imported, not even the valid product
    assert client.get("/products?q=ok-1", headers=h).json()["total"] == 0


def test_import_rolls_back_if_catalogue_changes_after_review(client, auth, catalog):
    h = auth["ceo"]
    data = _xlsx([["P1", "race-1", "Race", "C", None, None, None, None, None, None]],
                 [["P1", "race-1-a", "A", None, None, None, None, None, "10", None, None, None, None]])
    j = client.post("/imports/validate", headers=h, files={"file": (*XLSX[:1], data, XLSX[1])}).json()
    assert j["error_count"] == 0
    # someone creates the same SKU between review and commit
    cat = catalog["cat"]["id"]
    client.post("/products", headers=h, json={"code": "OTHER", "name": "O", "category_id": cat,
                                              "variants": [{"sku": "RACE-1-A", "name": "x"}]})
    c = client.post(f"/imports/{j['id']}/commit", headers=h, json={"confirm": True})
    assert c.status_code == 409
    codes = [i["code"] for i in client.get("/products?q=race", headers=h).json()["items"]]
    assert codes == ["OTHER"]  # the imported RACE-1 product was not created


def test_import_requires_permission_and_price_permission(client, auth):
    assert client.get("/imports/template", headers=auth["booker"]).status_code == 403
    assert client.post("/imports/validate", headers=auth["booker"], files={"file": ("a.csv", b"x", "text/csv")}).status_code == 403


def test_bad_files_rejected(client, auth):
    h = auth["ceo"]
    assert client.post("/imports/validate", headers=h, files={"file": ("a.xlsx", b"not excel", "x")}).status_code == 422
    assert client.post("/imports/validate", headers=h, files={"file": ("a.txt", b"x", "x")}).status_code == 422
    csv = b"product_id,product_code,product_name,category,sku,variant_name,retail_price\nP1,C1,Name,Cat,S1,V1,10\nP1,C1,Name,Cat,S2,V2,12\n"
    j = client.post("/imports/validate", headers=h, files={"file": ("a.csv", csv, "text/csv")}).json()
    assert j["error_count"] == 0 and j["summary"]["variants_new"] == 2
