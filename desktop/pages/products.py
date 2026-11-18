"""Product catalogue screens: list, detail with variants/stock/history, create/edit forms, bulk import wizard."""
from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QDialog, QFileDialog, QHBoxLayout, QLineEdit, QPlainTextEdit, QSpinBox, QStackedWidget,
    QTabWidget, QVBoxLayout, QWidget,
)

from desktop.pages.base import Page
from desktop.session import Session
from desktop.ui.forms import FormDialog, FormGrid
from desktop.ui.widgets import (
    Banner, Column, DataTable, PageHeader, Pager, StatePanel, button, format_money, label,
)
from desktop.ui.worker import run_async

STATUS_KIND = {"active": "ok", "inactive": "neutral", "discontinued": "warn"}
STATUSES = [("Active", "active"), ("Inactive", "inactive"), ("Discontinued", "discontinued")]


def _money(v):
    return format_money(v) if v not in (None, "") else "-"


# ------------------------------------------------------------------------------------------ forms
class ProductDialog(FormDialog):
    def __init__(self, session: Session, categories: list[dict], product: dict | None, parent=None):
        super().__init__("New product" if product is None else f"Edit {product['code']}",
                         "Create product" if product is None else "Save changes", parent, 560)
        self.session, self.product = session, product
        self.code, self.name, self.brand = QLineEdit(), QLineEdit(), QLineEdit()
        self.category = QComboBox()
        if product is None:
            self.category.addItem("Select a category…", None)
        for c in categories:
            self.category.addItem(c["name"], c["id"])
        self.new_category = QLineEdit()
        self.new_category.setPlaceholderText("or type a new category")
        self.status = QComboBox()
        for t, v in STATUSES:
            self.status.addItem(t, v)
        self.description = QPlainTextEdit()
        self.description.setFixedHeight(70)
        self.discount = QCheckBox("Eligible for discounts")
        self.discount.setChecked(True)
        self.batches = QCheckBox("Track batches")
        self.expiry = QCheckBox("Track expiry dates")

        self.body.addWidget(label("Identity", "sectionTitle"))
        g1 = FormGrid(2)
        g1.add("code", "Product code (unique)", self.code)
        g1.add("name", "Name", self.name)
        g1.add("brand", "Brand", self.brand)
        g1.add("status", "Status", self.status)
        self.body.addWidget(g1)
        self.body.addWidget(label("Classification", "sectionTitle"))
        g2 = FormGrid(2)
        g2.add("category", "Category", self.category)
        g2.add("new_category", "New category", self.new_category)
        g2.add("description", "Description", self.description, span=2)
        self.body.addWidget(g2)
        self.body.addWidget(label("Stock handling", "sectionTitle"))
        row = QHBoxLayout()
        for w in (self.discount, self.batches, self.expiry):
            row.addWidget(w)
        row.addStretch()
        self.body.addLayout(row)
        self.body.addWidget(label("Batch and expiry tracking cannot be changed once stock exists for the product.", "muted", wrap=True))
        self.form = g1
        self.cat_form = g2
        if product:
            self.code.setText(product["code"]); self.code.setEnabled(False)
            self.name.setText(product["name"]); self.brand.setText(product.get("brand") or "")
            self.status.setCurrentIndex(max(self.status.findData(product["status"]), 0))
            self.category.setCurrentIndex(max(self.category.findData(product["category_id"]), 0))
            self.description.setPlainText(product.get("description") or "")
            self.discount.setChecked(product["discount_eligible"])
            self.batches.setChecked(product["tracks_batches"]); self.expiry.setChecked(product["tracks_expiry"])

    def submit(self) -> None:
        self.form.clear_errors(); self.cat_form.clear_errors()
        bad = False
        if not self.code.text().strip() and self.product is None:
            self.form.set_error("code", "Enter a product code."); bad = True
        if not self.name.text().strip():
            self.form.set_error("name", "Enter a name."); bad = True
        new_cat = self.new_category.text().strip()
        if self.category.currentData() is None and not new_cat:
            self.cat_form.set_error("category", "Choose or type a category."); bad = True
        if bad:
            return self.reenable()
        c = self.session.client

        def work():
            cat_id = self.category.currentData()
            if new_cat:
                cat_id = c.post("/categories", {"name": new_cat})["id"]
            body = {"name": self.name.text().strip(), "brand": self.brand.text().strip() or None, "category_id": cat_id,
                    "description": self.description.toPlainText().strip() or None, "status": self.status.currentData(),
                    "discount_eligible": self.discount.isChecked(), "tracks_batches": self.batches.isChecked(),
                    "tracks_expiry": self.expiry.isChecked()}
            if self.product is None:
                return c.post("/products", {"code": self.code.text().strip(), **body})
            return c.patch(f"/products/{self.product['id']}", body)

        def done(r):
            self.result_product = r
            self.finish_ok()

        run_async(work, done, lambda e: self.finish_error(e.message))


class VariantDialog(FormDialog):
    def __init__(self, session: Session, product: dict, variant: dict | None, parent=None):
        super().__init__("Add variant" if variant is None else f"Edit {variant['sku']}",
                         "Add variant" if variant is None else "Save changes", parent, 560)
        self.session, self.product, self.variant = session, product, variant
        self.sku, self.name, self.barcode = QLineEdit(), QLineEdit(), QLineEdit()
        self.shade, self.hex, self.size = QLineEdit(), QLineEdit(), QLineEdit()
        self.hex.setPlaceholderText("#B76E79")
        self.threshold = QSpinBox(); self.threshold.setRange(0, 1_000_000)
        self.status = QComboBox()
        for t, v in STATUSES:
            self.status.addItem(t, v)
        self.retail, self.employee, self.distributor, self.cost = QLineEdit(), QLineEdit(), QLineEdit(), QLineEdit()
        self.body.addWidget(label(f"Product: {product['name']}", "muted"))
        g1 = FormGrid(2)
        g1.add("sku", "SKU (unique)", self.sku)
        g1.add("name", "Variant name", self.name)
        g1.add("shade", "Shade name", self.shade)
        g1.add("hex", "Shade colour", self.hex)
        g1.add("size", "Size", self.size)
        g1.add("barcode", "Barcode (optional)", self.barcode)
        g1.add("threshold", "Reorder threshold", self.threshold)
        g1.add("status", "Status", self.status)
        self.body.addWidget(g1)
        self.form = g1
        self.can_price = session.can("products.manage_prices")
        self.g2 = None
        if session.can("products.view_prices", "products.manage_prices"):
            self.body.addWidget(label("Prices", "sectionTitle"))
            g2 = FormGrid(2)
            g2.add("retail", "Retail price", self.retail)
            g2.add("employee", "Employee price", self.employee)
            g2.add("distributor", "Distributor price", self.distributor)
            if session.can("products.view_cost"):
                g2.add("cost", "Reference purchase cost", self.cost)
            self.body.addWidget(g2)
            self.g2 = g2
            for w in (self.retail, self.employee, self.distributor, self.cost):
                w.setEnabled(self.can_price)
            self.body.addWidget(label("Reference cost is for planning only; stock value always comes from real receipts."
                                      if self.can_price else "You can view prices but not change them.", "muted", wrap=True))
        if variant:
            self.sku.setText(variant["sku"]); self.sku.setEnabled(False)
            self.name.setText(variant["name"]); self.shade.setText(variant.get("shade_name") or "")
            self.hex.setText(variant.get("shade_hex") or ""); self.size.setText(variant.get("size") or "")
            self.barcode.setText(variant.get("barcode") or ""); self.threshold.setValue(variant["reorder_threshold"])
            self.status.setCurrentIndex(max(self.status.findData(variant["status"]), 0))
            self.retail.setText(variant.get("retail_price") or ""); self.employee.setText(variant.get("employee_price") or "")
            self.distributor.setText(variant.get("distributor_price") or ""); self.cost.setText(variant.get("purchase_cost") or "")

    def submit(self) -> None:
        self.form.clear_errors()
        if self.variant is None and not self.sku.text().strip():
            self.form.set_error("sku", "Enter a SKU."); return self.reenable()
        if not self.name.text().strip():
            self.form.set_error("name", "Enter a variant name."); return self.reenable()
        body = {"name": self.name.text().strip(), "shade_name": self.shade.text().strip() or None,
                "shade_hex": self.hex.text().strip() or None, "size": self.size.text().strip() or None,
                "barcode": self.barcode.text().strip() or None, "reorder_threshold": self.threshold.value(),
                "status": self.status.currentData()}
        if self.g2 is not None and self.can_price:
            body.update(retail_price=self.retail.text().strip() or None, employee_price=self.employee.text().strip() or None,
                        distributor_price=self.distributor.text().strip() or None)
            if self.session.can("products.view_cost"):
                body["purchase_cost"] = self.cost.text().strip() or None
        c = self.session.client
        if self.variant is None:
            run_async(lambda: c.post(f"/products/{self.product['id']}/variants", {"sku": self.sku.text().strip(), **body}),
                      lambda _: self.finish_ok(), lambda e: self.finish_error(e.message))
        else:
            run_async(lambda: c.patch(f"/variants/{self.variant['id']}", body),
                      lambda _: self.finish_ok(), lambda e: self.finish_error(e.message))


# ------------------------------------------------------------------------------------------ detail
class ProductDetailDialog(QDialog):
    def __init__(self, session: Session, product_id: int, categories: list[dict], parent=None):
        super().__init__(parent)
        self.session, self.product_id, self.categories, self.product = session, product_id, categories, {}
        self.changed = False
        self.setWindowTitle("Product")
        self.setMinimumSize(1020, 580)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(20, 16, 20, 14)
        top = QHBoxLayout()
        self.image = label("No image", "muted")
        self.image.setFixedSize(96, 96)
        self.image.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.image.setStyleSheet("border: 1px solid #E3DCDA; background: #F2EFEC;")
        top.addWidget(self.image)
        meta = QVBoxLayout()
        self.title = label("", "pageTitle"); self.sub = label("", "muted"); self.flags = label("", "muted", wrap=True)
        meta.addWidget(self.title); meta.addWidget(self.sub); meta.addWidget(self.flags); meta.addStretch()
        top.addLayout(meta, 1)
        can = session.can("products.manage")
        for text, fn in (("Change image", self._image), ("Edit product", self._edit)):
            b = button(text, on_click=fn); b.setVisible(can); top.addWidget(b, 0, Qt.AlignmentFlag.AlignTop)
        lay.addLayout(top)
        self.banner = Banner(); lay.addWidget(self.banner)
        self.tabs = QTabWidget()
        lay.addWidget(self.tabs, 1)

        v = QWidget(); vl = QVBoxLayout(v); vl.setContentsMargins(0, 10, 0, 0)
        bar = QHBoxLayout(); bar.addStretch()
        self.add_v = button("Add variant", "primary", self._add_variant); self.add_v.setVisible(can)
        self.edit_v = button("Edit variant", on_click=self._edit_variant); self.edit_v.setVisible(can)
        bar.addWidget(self.edit_v); bar.addWidget(self.add_v); vl.addLayout(bar)
        cols = [Column("sku", "SKU", 122), Column("name", "Variant", swatch_key="swatch"), Column("barcode", "Barcode", 118),
                Column("retail_price", "Retail", 92, "right", fmt=_money)]
        if session.can("products.view_prices"):
            cols += [Column("employee_price", "Employee", 92, "right", fmt=_money), Column("distributor_price", "Distrib.", 92, "right", fmt=_money)]
        if session.can("products.view_cost"):
            cols.append(Column("purchase_cost", "Ref. cost", 92, "right", fmt=_money))
        cols += [Column("reorder_threshold", "Reorder", 66, "right"),
                 Column("pricing_status", "Pricing", 76, status=lambda v: "ok" if v == "active" else "warn", fmt=lambda v: str(v).capitalize()),
                 Column("status", "Status", 96, status=lambda v: STATUS_KIND.get(v, "neutral"), fmt=lambda v: str(v).capitalize())]
        self.vtable = DataTable(cols)
        self.vtable.row_activated.connect(lambda _: self._edit_variant())
        vl.addWidget(self.vtable, 1)
        self.tabs.addTab(v, "Variants")

        self.stock_table = DataTable([Column("sku", "SKU", 122), Column("variant_name", "Variant"), Column("location", "Location", 110),
                                      Column("on_hand_good", "On hand", 80, "right"), Column("reserved", "Reserved", 80, "right"),
                                      Column("available", "Available", 80, "right"), Column("damaged", "Damaged", 80, "right"),
                                      Column("expired", "Expired", 80, "right"), Column("in_transit", "In transit", 80, "right")])
        self.stock_panel = StatePanel(self.stock_table, "No stock has been recorded for this product at any location you can see.")
        self.tabs.addTab(self.stock_panel, "Stock by location")
        self.move_table = DataTable([Column("created_at", "When", 140, fmt=lambda v: (v or "")[:16].replace("T", " ")),
                                     Column("ref", "Ref", 108), Column("txn_type", "Type", 130, fmt=lambda v: str(v).replace("_", " ")),
                                     Column("sku", "SKU", 118), Column("batch_no", "Batch", 76), Column("bucket", "Bucket", 76),
                                     Column("qty_delta", "Qty", 70, "right"), Column("reason", "Reason"), Column("user", "By", 100)])
        self.move_panel = StatePanel(self.move_table, "No stock movements yet.")
        self.tabs.addTab(self.move_panel, "Movement history")
        close = button("Close", on_click=self.accept)
        row = QHBoxLayout(); row.addStretch(); row.addWidget(close); lay.addLayout(row)
        self.load()

    def load(self) -> None:
        c = self.session.client
        run_async(lambda: c.get(f"/products/{self.product_id}"), self._loaded, lambda e: self.banner.show_message(e.message, "error"))

    def _loaded(self, p: dict) -> None:
        self.product = p
        self.title.setText(p["name"])
        self.sub.setText(f"{p['code']}  ·  {p.get('brand') or 'No brand'}  ·  {p['category']}")
        flags = [p["status"].capitalize()]
        if p["tracks_batches"]: flags.append("batch tracked")
        if p["tracks_expiry"]: flags.append("expiry tracked")
        if not p["discount_eligible"]: flags.append("not discount eligible")
        self.flags.setText("  ·  ".join(flags) + (f"\n{p['description']}" if p.get("description") else ""))
        for v in p["variants"]:
            v["swatch"] = [v["shade_hex"]] if v.get("shade_hex") else []
        self.vtable.set_rows(p["variants"])
        # shade column: show the hex value text; swatches shown in the list view
        if p["has_image"]:
            c = self.session.client
            run_async(lambda: c.get_bytes(f"/products/{self.product_id}/image"), self._image_loaded, lambda e: None)
        if self.session.can("inventory.view"):
            self._load_stock()
        else:
            self.stock_panel.show_empty("You do not have access to stock figures.")
            self.move_panel.show_empty("You do not have access to stock movements.")

    def _image_loaded(self, data: bytes) -> None:
        pm = QPixmap(); pm.loadFromData(data)
        if not pm.isNull():
            self.image.setPixmap(pm.scaled(94, 94, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation))

    def _load_stock(self) -> None:
        c, s = self.session.client, self.session
        names = {l["id"]: l["code"] for l in s.locations}
        ids = {v["id"] for v in self.product["variants"]}

        def work():
            rows = c.get("/inventory/stock", q=self.product["name"], include_zero="true", page_size=200,
                         location_id=s.current_location_id)["items"]
            rows = [r for r in rows if r["variant_id"] in ids]
            mv = []
            for vid in list(ids)[:30]:
                mv += c.get("/inventory/movements", variant_id=vid, page_size=25, location_id=s.current_location_id)["items"]
            mv.sort(key=lambda m: m["id"], reverse=True)
            return rows, mv[:100]

        def done(r):
            rows, mv = r
            for x in rows:
                x["location"] = names.get(x["location_id"], f"#{x['location_id']}")
            self.stock_table.set_rows(rows)
            self.stock_panel.show_content() if rows else self.stock_panel.show_empty()
            self.move_table.set_rows(mv)
            self.move_panel.show_content() if mv else self.move_panel.show_empty()

        run_async(work, done, lambda e: (self.stock_panel.show_error(e.message, e.offline), self.move_panel.show_error(e.message, e.offline)))

    def _edit(self) -> None:
        d = ProductDialog(self.session, self.categories, self.product, self)
        if d.exec():
            self.changed = True; self.banner.show_message("Product saved.", "ok"); self.load()

    def _add_variant(self) -> None:
        if VariantDialog(self.session, self.product, None, self).exec():
            self.changed = True; self.banner.show_message("Variant added.", "ok"); self.load()

    def _edit_variant(self) -> None:
        row = self.vtable.selected_row()
        if row and self.session.can("products.manage") and VariantDialog(self.session, self.product, row, self).exec():
            self.changed = True; self.banner.show_message("Variant saved.", "ok"); self.load()

    def _image(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "Choose product image", "", "Images (*.png *.jpg *.jpeg *.webp)")
        if not path:
            return
        c = self.session.client
        run_async(lambda: c.upload(f"/products/{self.product_id}/image", path, method="PUT"),
                  lambda _: (self.banner.show_message("Image updated.", "ok"), self.load()),
                  lambda e: self.banner.show_message(e.message, "error"))


# ------------------------------------------------------------------------------------------ import
class ImportWizard(QDialog):
    """1 Choose file -> 2 Review (nothing saved yet) -> 3 Result."""

    def __init__(self, session: Session, parent=None):
        super().__init__(parent)
        self.session, self.job, self.path = session, None, None
        self.setWindowTitle("Import products")
        self.setMinimumSize(860, 560)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(20, 16, 20, 14)
        lay.addWidget(label("Import products from Excel or CSV", "sectionTitle"))
        self.banner = Banner(); lay.addWidget(self.banner)
        self.stack = QStackedWidget(); lay.addWidget(self.stack, 1)

        # step 1
        s1 = QWidget(); l1 = QVBoxLayout(s1)
        l1.addWidget(label("1. Download the blank template and fill it in. Required columns are marked REQUIRED.\n"
                           "Products and variants are on separate sheets, linked by product_id.\n"
                           "The template contains no products, prices, barcodes or stock. Nothing is saved until you confirm in step 2.",
                           "muted", wrap=True))
        r = QHBoxLayout()
        r.addWidget(button("Download Excel template", on_click=lambda: self._template("xlsx")))
        r.addWidget(button("Download CSV template", on_click=lambda: self._template("csv")))
        r.addStretch(); l1.addLayout(r)
        l1.addSpacing(10)
        l1.addWidget(label("2. Choose your completed file", "fieldLabel"))
        r2 = QHBoxLayout()
        self.file_lbl = label("No file chosen", "muted")
        r2.addWidget(button("Choose file…", on_click=self._choose)); r2.addWidget(self.file_lbl, 1); l1.addLayout(r2)
        l1.addWidget(label("If a product code already exists", "fieldLabel"))
        self.mode = QComboBox()
        self.mode.addItem("Skip it (leave the existing product unchanged)", "skip")
        self.mode.addItem("Update it from the file", "update")
        self.mode.setMaximumWidth(420)
        l1.addWidget(self.mode)
        l1.addStretch()
        row = QHBoxLayout(); row.addStretch()
        row.addWidget(button("Cancel", on_click=self.reject))
        self.validate_btn = button("Check file", "primary", self._validate); self.validate_btn.setEnabled(False)
        row.addWidget(self.validate_btn); l1.addLayout(row)
        self.stack.addWidget(s1)

        # step 2
        s2 = QWidget(); l2 = QVBoxLayout(s2)
        self.summary = label("", "sectionTitle", wrap=True)
        l2.addWidget(self.summary)
        self.review_tabs = QTabWidget()
        self.err_table = DataTable([Column("sheet", "Sheet", 90), Column("row", "Row", 60, "right"), Column("field", "Column", 140), Column("message", "Problem")])
        self.rec_table = DataTable([Column("row", "Row", 60, "right"), Column("code", "Code", 140), Column("name", "Name"),
                                    Column("variants", "Variants", 80, "right"),
                                    Column("action", "Will", 110, fmt=lambda v: {"create": "Create", "update": "Update", "skip": "Skip"}.get(v, v)),
                                    Column("valid", "Valid", 80, status=lambda v: "ok" if v else "danger", fmt=lambda v: "Yes" if v else "No")])
        self.review_tabs.addTab(self.err_table, "Problems")
        self.review_tabs.addTab(self.rec_table, "Products in file")
        l2.addWidget(self.review_tabs, 1)
        row2 = QHBoxLayout(); row2.addStretch()
        row2.addWidget(button("Back", on_click=lambda: self.stack.setCurrentIndex(0)))
        self.commit_btn = button("Import now", "primary", self._commit)
        row2.addWidget(self.commit_btn); l2.addLayout(row2)
        self.stack.addWidget(s2)

        # step 3
        s3 = QWidget(); l3 = QVBoxLayout(s3)
        self.result = label("", "sectionTitle", wrap=True)
        l3.addWidget(self.result); l3.addStretch()
        r3 = QHBoxLayout(); r3.addStretch(); r3.addWidget(button("Done", "primary", self.accept)); l3.addLayout(r3)
        self.stack.addWidget(s3)

    def _template(self, fmt: str) -> None:
        name = f"product-import-template.{fmt}"
        path, _ = QFileDialog.getSaveFileName(self, "Save template", name)
        if not path:
            return
        c = self.session.client

        def done(data):
            with open(path, "wb") as f:
                f.write(data)
            self.banner.show_message(f"Template saved to {path}", "ok")

        run_async(lambda: c.get_bytes("/imports/template", format=fmt), done, lambda e: self.banner.show_message(e.message, "error"))

    def _choose(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "Choose file", "", "Spreadsheets (*.xlsx *.csv)")
        if path:
            self.path = path
            self.file_lbl.setText(path)
            self.validate_btn.setEnabled(True)

    def _validate(self) -> None:
        self.banner.clear(); self.validate_btn.setEnabled(False)
        c, path, mode = self.session.client, self.path, self.mode.currentData()

        def done(j):
            self.job = j
            s = j["summary"]
            if j["error_count"]:
                self.summary.setText(f"{j['error_count']} problem(s) found in {s['rows_with_errors']} row(s). Nothing can be imported until the file is fixed.")
            else:
                self.summary.setText(f"Ready: {s['products_new']} new, {s['products_updated']} updated, {s['products_skipped']} skipped products; "
                                     f"{s['variants_new']} new and {s['variants_updated']} updated variants. Nothing is saved yet.")
            self.err_table.set_rows(j["errors"])
            self.rec_table.set_rows(j["records"])
            self.review_tabs.setCurrentIndex(0 if j["error_count"] else 1)
            self.commit_btn.setEnabled(j["error_count"] == 0)
            self.stack.setCurrentIndex(1)
            self.validate_btn.setEnabled(True)

        def fail(e):
            self.banner.show_message(e.message, "error"); self.validate_btn.setEnabled(True)

        run_async(lambda: c.upload("/imports/validate", path, data={"mode": mode}), done, fail)

    def _commit(self) -> None:
        self.commit_btn.setEnabled(False)
        c, jid = self.session.client, self.job["id"]

        def done(j):
            r = j["result"]
            self.result.setText(f"Import complete.\n{r['products_created']} products created, {r['products_updated']} updated, "
                                f"{r['products_skipped']} skipped.\n{r['variants_created']} variants created, {r['variants_updated']} updated.")
            self.stack.setCurrentIndex(2)

        def fail(e):
            self.banner.show_message(f"Nothing was imported. {e.message}", "error"); self.commit_btn.setEnabled(True)

        run_async(lambda: c.post(f"/imports/{jid}/commit", {"confirm": True}), done, fail)


# ------------------------------------------------------------------------------------------ page
class ProductsPage(Page):
    title = "Products"

    def __init__(self, session: Session):
        super().__init__(session)
        self.categories: list[dict] = []
        head = PageHeader("Products", "Catalogue, variants and prices. Stock is tracked separately under Inventory.")
        if session.can("products.import"):
            head.add_action(button("Import from Excel / CSV", on_click=self._import))
        if session.can("products.manage"):
            head.add_action(button("New product", "primary", self._new))
        self.root.addWidget(head)
        self.status = Banner(); self.root.addWidget(self.status)

        bar = QHBoxLayout()
        self.search = QLineEdit(); self.search.setPlaceholderText("Search name, code, brand, SKU or barcode"); self.search.setMinimumWidth(320)
        self.search.returnPressed.connect(self._reload)
        self.category = QComboBox(); self.category.addItem("All categories", None); self.category.currentIndexChanged.connect(self._reload)
        self.state = QComboBox(); self.state.addItem("Any status", None)
        for t, v in STATUSES:
            self.state.addItem(t, v)
        self.state.currentIndexChanged.connect(self._reload)
        self.pricing = QComboBox(); self.pricing.addItem("Any pricing", None)
        self.pricing.addItem("Has unpriced variants", "unpriced"); self.pricing.currentIndexChanged.connect(self._reload)
        self.sort = QComboBox()
        for t, v in (("Sort: name", "name"), ("Sort: code", "code"), ("Sort: brand", "brand"), ("Sort: recently changed", "updated")):
            self.sort.addItem(t, v)
        self.sort.currentIndexChanged.connect(self._reload)
        for w in (self.search, self.category, self.state, self.pricing, self.sort):
            bar.addWidget(w)
        bar.addStretch()
        self.root.addLayout(bar)

        self.table = DataTable([
            Column("name", "Product", None, swatch_key="swatches"), Column("code", "Code", 130), Column("brand", "Brand", 140),
            Column("category", "Category", 150), Column("variant_count", "Variants", 80, "right"),
            Column("unpriced_variants", "Pricing", 120, status=lambda v: "warn" if v else "ok",
                   fmt=lambda v: f"{v} unpriced" if v else "Priced"),
            Column("status", "Status", 120, status=lambda v: STATUS_KIND.get(v, "neutral"), fmt=lambda v: str(v).capitalize()),
        ])
        self.table.row_activated.connect(self._open)
        self.panel = StatePanel(self.table, "No products yet. Add one with New product, or import a spreadsheet.")
        self.panel.retry.connect(self.refresh)
        self.root.addWidget(self.panel, 1)
        self.pager = Pager(50); self.pager.changed.connect(self.refresh)
        self.root.addWidget(self.pager)

    def _reload(self) -> None:
        self.pager.reset(); self.refresh()

    def refresh(self) -> None:
        self.panel.show_loading()
        c = self.session.client
        params = dict(q=self.search.text().strip() or None, category_id=self.category.currentData(),
                      status=self.state.currentData(), pricing_status=self.pricing.currentData(),
                      sort=self.sort.currentData(), page=self.pager.page, page_size=self.pager.page_size)

        def work():
            return c.get("/products", **params), c.get("/categories")

        def done(r):
            data, cats = r
            if cats != self.categories:
                self.categories = cats
                cur = self.category.currentData()
                self.category.blockSignals(True); self.category.clear(); self.category.addItem("All categories", None)
                for x in cats:
                    self.category.addItem(x["name"], x["id"])
                self.category.setCurrentIndex(max(self.category.findData(cur), 0)); self.category.blockSignals(False)
            self.table.set_rows(data["items"])
            self.pager.update_total(data["total"])
            filtered = any(v for k, v in params.items() if k in ("q", "category_id", "status", "pricing_status"))
            if data["items"]:
                self.panel.show_content()
            else:
                self.panel.show_empty("No products match these filters." if filtered else None)

        run_async(work, done, lambda e: self.panel.show_error(e.message, e.offline))

    def _open(self, row: dict) -> None:
        d = ProductDetailDialog(self.session, row["id"], self.categories, self)
        d.exec()
        if d.changed:
            self.refresh()

    def _new(self) -> None:
        d = ProductDialog(self.session, self.categories, None, self)
        if d.exec():
            self.status.show_message("Product created. Add its variants (shades or sizes) next.", "ok")
            self.refresh()
            self._open(d.result_product)

    def _import(self) -> None:
        if ImportWizard(self.session, self).exec():
            self.status.show_message("Import complete.", "ok")
        self.refresh()
