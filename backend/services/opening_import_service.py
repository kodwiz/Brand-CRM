"""Opening-balance import from the paper registers: ONE workbook, checked as a whole, then entered through the same services the screens use.

Rules (they come from the brief and are enforced, not suggested):
  * Only VERIFIED balances with an EVIDENCE reference are imported. An unverified or undocumented row is an error: it is left out of the file until it has been checked.
  * Nothing is invented and nothing is plugged. The owner's capital is a figure the owner DECLARES; if it does not equal assets minus liabilities, the import is refused
    and the difference is shown. It is never made to match silently.
  * All or nothing. A check run (dry_run) executes the real services and then rolls everything back, so the check cannot disagree with the real run.
  * Opening stock, distributor balances and supplier payables are entered in their own modules (their sub-ledgers must agree with the ledger); customer receivables, cash and bank,
    other assets and liabilities, and the owner's capital are entered as ONE draft opening batch that a different person must approve.
  * A file that was already imported cannot be imported twice."""
from __future__ import annotations

import hashlib
import io
from collections import defaultdict
from datetime import date, datetime
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

from openpyxl import Workbook, load_workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter
from openpyxl.worksheet.datavalidation import DataValidation
from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.models import Account, AuditLog, CashAccount, Customer, Distributor, Location, ProductVariant, Supplier, User
from backend.services import accounting_ops_service as ops
from backend.services import audit_service, distributor_dispatch_service as dds, inventory_service, supplier_service
from backend.services import accounting_service as acc
from backend.services.errors import BusinessRuleError, ConflictError, NotFoundError, SelfApprovalError

MAX_BYTES = 5 * 1024 * 1024
MAX_ROWS = 5000
ZERO = Decimal(0)
Q2, Q4 = Decimal("0.01"), Decimal("0.0001")
YES = {"yes", "y", "true", "1", "x"}

COMMON = [("evidence", "evidence", True, "Where this figure comes from: register page, count sheet number, bank statement date."),
          ("verified", "verified", True, "yes only after someone has checked it against the document. Rows that are not 'yes' are refused.")]
SHEETS = {
    "Opening stock": [("location", "location_code", True, "Code of the location holding the stock"), ("sku", "sku", True, "SKU of the item"),
                      ("qty", "qty", True, "Counted quantity, whole units"), ("unit_cost", "unit_cost", True, "Verified cost of one unit. Never guessed: no cost, no row."),
                      ("batch_no", "batch_no", False, "Needed for items that track batches"), ("expiry", "expiry_date", False, "YYYY-MM-DD; needed for items that track expiry")] + COMMON,
    "Distributors": [("code", "distributor_code", True, "Code of an existing distributor"), ("amount", "amount", True, "Amount the distributor owed on the opening date")] + COMMON,
    "Suppliers": [("code", "supplier_code", True, "Code of an existing supplier"), ("amount", "amount", True, "Amount the company owed the supplier on the opening date")] + COMMON,
    "Customers": [("code", "customer_code", True, "Code of an existing customer"), ("amount", "amount", True, "Amount the customer owed on the opening date")] + COMMON,
    "Cash and bank": [("account", "cash_account", True, "Exact name of a cash or bank account set up under Accounting > Cash and bank"),
                      ("amount", "amount", True, "Counted cash or bank-statement balance")] + COMMON,
    "Other balances": [("account", "account_code", True, "Ledger account code (an asset or a liability; not stock, suppliers, distributors, customers or equity)"),
                       ("debit", "debit", False, "Fill debit for an asset (money owed to the company, equipment), or credit for a liability"), ("credit", "credit", False, ""),
                       ("note", "note", False, "What it is")] + COMMON,
    "Owner's capital": [("amount", "owner_capital", True, "Opening equity the owner declares. It must equal assets minus liabilities or the import is refused.")] + COMMON,
}
ORDER = list(SHEETS)


# ---- template ----------------------------------------------------------------------------------------------------------------
def build_template_xlsx() -> bytes:
    wb = Workbook()
    ins = wb.active
    ins.title = "Instructions"
    for t, bold in [("Opening balances: paper registers to the system", True), ("", False),
                    ("1. Fill only the sheets you need. Every row needs an EVIDENCE reference and 'yes' in the verified column. Anything else is refused.", False),
                    ("2. Make sure locations, products, distributors, suppliers, customers and cash/bank accounts already exist: this file never creates them.", False),
                    ("3. Choose the opening date when you upload. All balances are as at that date. It cannot be in the future.", False),
                    ("4. 'Check file' enters everything for real and then rolls it back, so you see exactly what would happen. Nothing is saved by a check.", False),
                    ("5. If any row has an error, NOTHING is imported. Fix the file and check again.", False),
                    ("6. Owner's capital is what the owner declares. It must equal assets minus liabilities. If it does not, you are shown the difference; the system never adjusts it.", False),
                    ("7. Stock, distributor and supplier balances take effect when 'Post pending records' runs in Accounting. Receivables, cash, bank, other balances and capital become a DRAFT "
                     "opening batch which someone other than you must approve.", False),
                    ("8. This template contains no balances. Leave a row out rather than guess a number.", False), ("", False), ("Sheet columns", True)]:
        ins.append([t])
        if bold:
            ins.cell(ins.max_row, 1).font = Font(bold=True, size=12)
    for name, cols in SHEETS.items():
        ins.append([""])
        ins.append([name])
        ins.cell(ins.max_row, 1).font = Font(bold=True)
        for _k, h, req, hlp in cols:
            ins.append([f"   {h}  [{'REQUIRED' if req else 'optional'}]  {hlp}"])
    ins.column_dimensions["A"].width = 140
    for name, cols in SHEETS.items():
        ws = wb.create_sheet(name)
        ws.append([c[1] for c in cols])
        ws.append([("REQUIRED" if c[2] else "optional") for c in cols])
        for i, c in enumerate(cols, 1):
            head = ws.cell(1, i)
            head.font = Font(bold=True, color="FFFFFF")
            head.fill = PatternFill("solid", fgColor="6B4E5C" if c[2] else "8C8C8C")
            head.alignment = Alignment(horizontal="left")
            ws.cell(2, i).font = Font(italic=True, size=9, color="B03A2E" if c[2] else "6B6B6B")
            ws.column_dimensions[get_column_letter(i)].width = max(16, len(c[1]) + 4)
        ws.freeze_panes = "A3"
        col = [c[0] for c in cols].index("verified") + 1
        dv = DataValidation(type="list", formula1='"yes,no"', allow_blank=True)
        ws.add_data_validation(dv)
        dv.add(f"{get_column_letter(col)}3:{get_column_letter(col)}1000")
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


# ---- parsing -------------------------------------------------------------------------------------------------------------------
def _cell(v):
    if v is None:
        return None
    if isinstance(v, float) and v.is_integer():
        v = int(v)
    if isinstance(v, datetime):
        return v.date().isoformat()
    s = str(v).strip()
    return s or None


def _money(text, field: str, positive: bool = True) -> Decimal:
    try:
        d = Decimal(str(text).replace(",", ""))
    except (InvalidOperation, ValueError):
        raise BusinessRuleError(f"{field} is not a number.")
    if not d.is_finite() or d.as_tuple().exponent < -2:
        raise BusinessRuleError(f"{field} must be a plain amount with at most two decimals.")
    if positive and d <= 0:
        raise BusinessRuleError(f"{field} must be greater than zero.")
    return d.quantize(Q2)


def _read(wb, name: str, errors: list) -> list[tuple[int, dict]]:
    if name not in wb.sheetnames:
        return []
    cols = SHEETS[name]
    rows = list(wb[name].iter_rows(values_only=True))
    if not rows:
        return []
    header = [(_cell(h) or "").lower() for h in rows[0]]
    idx, bad = {}, False
    for key, h, req, _ in cols:
        if h in header:
            idx[key] = header.index(h)
        elif req:
            errors.append({"sheet": name, "row": 1, "message": f"Missing required column '{h}'."})
            bad = True
    if bad:
        return []
    out = []
    for n, r in enumerate(rows[1:], 2):
        d = {k: _cell(r[i]) if i < len(r) else None for k, i in idx.items()}
        if n == 2 and (d.get("evidence") or "").upper() == "REQUIRED":
            continue                                   # the hint row of the template
        if not any(v for v in d.values()):
            continue
        out.append((n, d))
    if len(out) > MAX_ROWS:
        errors.append({"sheet": name, "row": None, "message": f"More than {MAX_ROWS} rows. Split the file."})
        return []
    return out


def _gate(sheet: str, n: int, d: dict, errors: list) -> bool:
    """Every row must be verified and carry its evidence. Returns False (and records why) if not."""
    ok = True
    if (d.get("verified") or "").lower() not in YES:
        errors.append({"sheet": sheet, "row": n, "message": "Not verified. Only checked balances are imported: check it against the document and write 'yes', or take the row out."})
        ok = False
    if not d.get("evidence"):
        errors.append({"sheet": sheet, "row": n, "message": "No evidence reference (register page, count sheet, bank statement). Every opening balance must point to a document."})
        ok = False
    return ok


# ---- the import ----------------------------------------------------------------------------------------------------------------------
def _run(db: Session, actor: User, content: bytes, as_of: date, ip: str | None) -> dict:
    errors: list[dict] = []
    summary: dict[str, dict] = {}
    try:
        wb = load_workbook(io.BytesIO(content), data_only=True, read_only=True)
    except Exception:
        raise BusinessRuleError("This is not a readable Excel (.xlsx) file. Download the template and fill that in.")
    data = {name: _read(wb, name, errors) for name in ORDER}
    if errors:
        return {"errors": errors, "summary": summary, "totals": None}
    if not any(data.values()):
        raise BusinessRuleError("The workbook has no balances in it.")

    acc.ensure_chart(db)
    amap = acc.accounts_map(db)
    totals = {"stock": ZERO, "distributors": ZERO, "suppliers": ZERO, "customers": ZERO, "cash": ZERO, "other_assets": ZERO, "other_liabilities": ZERO}
    batch_lines: list[dict] = []

    def row_try(sheet, n, fn):
        """Run one row inside a savepoint so a refused row cannot poison the rest; collect its message."""
        try:
            with db.begin_nested():
                return fn()
        except (BusinessRuleError, ConflictError, NotFoundError, SelfApprovalError) as e:
            errors.append({"sheet": sheet, "row": n, "message": str(e)})
        except (KeyError, ValueError, TypeError):
            errors.append({"sheet": sheet, "row": n, "message": "Missing or invalid field."})
        return None

    # opening stock -------------------------------------------------------------------------------------------
    rows = data["Opening stock"]
    if rows:
        locs = {l.code.upper(): l for l in db.scalars(select(Location)).all()}
        skus = {v.sku.upper(): v for v in db.scalars(select(ProductVariant)).all()}
        lines, by_loc = [], defaultdict(lambda: ZERO)
        ok_rows = 0
        for n, d in rows:
            if not _gate("Opening stock", n, d, errors):
                continue

            def prep(n=n, d=d):
                loc = locs.get((d["location"] or "").upper())
                if loc is None:
                    raise NotFoundError(f"Location '{d['location']}' does not exist.")
                v = skus.get((d["sku"] or "").upper())
                if v is None:
                    raise NotFoundError(f"SKU '{d['sku']}' does not exist.")
                qty = int(Decimal(str(d["qty"]))) if d.get("qty") and Decimal(str(d["qty"])) == int(Decimal(str(d["qty"]))) else None
                if not qty or qty <= 0:
                    raise BusinessRuleError("Quantity must be a whole number greater than zero.")
                if not d.get("unit_cost"):
                    raise BusinessRuleError("A verified unit cost is required. Costs are never guessed.")
                try:
                    cost = Decimal(str(d["unit_cost"]).replace(",", ""))
                except InvalidOperation:
                    raise BusinessRuleError("Unit cost is not a number.")
                if cost < 0 or not cost.is_finite():
                    raise BusinessRuleError("Unit cost cannot be negative.")
                exp = None
                if d.get("expiry"):
                    try:
                        exp = date.fromisoformat(d["expiry"])
                    except ValueError:
                        raise BusinessRuleError("Expiry date must be YYYY-MM-DD.")
                lines.append({"location_id": loc.id, "variant_id": v.id, "batch_no": d.get("batch_no"), "expiry_date": exp, "qty": qty, "unit_cost": cost, "_row": n})
                by_loc[loc.id] += (Decimal(qty) * cost).quantize(Q4, rounding=ROUND_HALF_UP)
            row_try("Opening stock", n, prep)
        if lines:
            res = inventory_service.opening_stock(db, actor=actor, lines=[{k: v for k, v in l.items() if k != "_row"} for l in lines], dry_run=False, idempotency_key=None, ip=ip) \
                if not errors else inventory_service.opening_stock(db, actor=actor, lines=[{k: v for k, v in l.items() if k != "_row"} for l in lines], dry_run=True, idempotency_key=None, ip=ip)
            for e in res.get("errors", []):
                errors.append({"sheet": "Opening stock", "row": lines[e["line"] - 1]["_row"], "message": e["message"]})
        totals["stock"] = sum((v.quantize(Q2, rounding=ROUND_HALF_UP) for v in by_loc.values()), ZERO)
        summary["Opening stock"] = {"rows": len(lines), "amount": str(totals["stock"])}

    # distributors and suppliers (own modules, own sub-ledgers) ------------------------------------------------------------------
    for sheet, model, key, post in (("Distributors", Distributor, "distributors", "dist"), ("Suppliers", Supplier, "suppliers", "sup")):
        rows = data[sheet]
        if not rows:
            continue
        party = {p.code.upper(): p for p in db.scalars(select(model)).all()}
        seen, n_ok = set(), 0
        for n, d in rows:
            if not _gate(sheet, n, d, errors):
                continue

            def go(n=n, d=d):
                p = party.get((d["code"] or "").upper())
                if p is None:
                    raise NotFoundError(f"'{d['code']}' does not exist. Create it first.")
                if p.id in seen:
                    raise BusinessRuleError(f"{p.name} appears more than once.")
                seen.add(p.id)
                amt = _money(d["amount"], "Amount")
                if post == "dist":
                    dds.post_opening(db, actor=actor, distributor_id=p.id, amount=amt, as_of=as_of, note=d["evidence"], ip=ip)
                else:
                    supplier_service.post_opening(db, actor=actor, supplier_id=p.id, amount=amt, as_of=as_of, note=d["evidence"], ip=ip)
                totals[key] += amt
                return True
            if row_try(sheet, n, go):
                n_ok += 1
        summary[sheet] = {"rows": n_ok, "amount": str(totals[key])}

    # customers, cash and bank, other balances: one draft opening batch --------------------------------------------------------------
    rows = data["Customers"]
    if rows:
        cust = {c.code.upper(): c for c in db.scalars(select(Customer)).all()}
        seen, n_ok = set(), 0
        for n, d in rows:
            if not _gate("Customers", n, d, errors):
                continue

            def go(n=n, d=d):
                c = cust.get((d["code"] or "").upper())
                if c is None:
                    raise NotFoundError(f"Customer '{d['code']}' does not exist. Create it first.")
                if c.id in seen:
                    raise BusinessRuleError(f"{c.name} appears more than once.")
                seen.add(c.id)
                amt = _money(d["amount"], "Amount")
                batch_lines.append({"account": "1100", "debit": amt, "credit": 0, "party_type": "customer", "party_id": c.id, "evidence": d["evidence"], "verified": True,
                                    "note": f"{c.code} {c.name}"})
                totals["customers"] += amt
                return True
            if row_try("Customers", n, go):
                n_ok += 1
        summary["Customers"] = {"rows": n_ok, "amount": str(totals["customers"])}

    rows = data["Cash and bank"]
    if rows:
        cash = {c.name.lower(): c for c in db.scalars(select(CashAccount)).all()}
        seen, n_ok = set(), 0
        for n, d in rows:
            if not _gate("Cash and bank", n, d, errors):
                continue

            def go(n=n, d=d):
                c = cash.get((d["account"] or "").lower())
                if c is None or not c.is_active:
                    raise NotFoundError(f"Cash or bank account '{d['account']}' does not exist or is inactive. Set it up under Accounting > Cash and bank.")
                if c.id in seen:
                    raise BusinessRuleError(f"{c.name} appears more than once.")
                seen.add(c.id)
                amt = _money(d["amount"], "Amount")
                batch_lines.append({"account_id": c.account_id, "debit": amt, "credit": 0, "evidence": d["evidence"], "verified": True, "note": c.name})
                totals["cash"] += amt
                return True
            if row_try("Cash and bank", n, go):
                n_ok += 1
        summary["Cash and bank"] = {"rows": n_ok, "amount": str(totals["cash"])}

    rows = data["Other balances"]
    if rows:
        cash_ledger_ids = {c.account_id for c in db.scalars(select(CashAccount)).all()}
        n_ok = 0
        for n, d in rows:
            if not _gate("Other balances", n, d, errors):
                continue

            def go(n=n, d=d):
                a = amap.get(str(d["account"]))
                if a is None:
                    raise NotFoundError(f"Account '{d['account']}' does not exist.")
                if a.type not in ("asset", "liability"):
                    raise BusinessRuleError(f"{a.code} {a.name} is not an asset or a liability. Opening balances are balance-sheet items; equity goes on the Owner's capital sheet.")
                if a.is_control and a.code != "1100":
                    raise BusinessRuleError(f"{a.code} {a.name} is filled from its own module. Use that sheet.")
                if a.code == "1100":
                    raise BusinessRuleError("Customer receivables go on the Customers sheet, one row per customer.")
                if a.id in cash_ledger_ids:
                    raise BusinessRuleError(f"{a.code} {a.name} is a cash or bank account. Use the Cash and bank sheet.")
                dr = _money(d["debit"], "Debit", positive=True) if d.get("debit") else ZERO
                cr = _money(d["credit"], "Credit", positive=True) if d.get("credit") else ZERO
                if (dr > 0) == (cr > 0):
                    raise BusinessRuleError("Enter either a debit or a credit amount.")
                if a.requires_party:
                    raise BusinessRuleError(f"{a.code} {a.name} needs a named party; enter it as a draft opening batch in Accounting instead.")
                batch_lines.append({"account": a.code, "debit": dr, "credit": cr, "evidence": d["evidence"], "verified": True, "note": d.get("note")})
                if a.type == "asset":
                    totals["other_assets"] += dr - cr
                else:
                    totals["other_liabilities"] += cr - dr
                return True
            if row_try("Other balances", n, go):
                n_ok += 1
        summary["Other balances"] = {"rows": n_ok, "amount": str(totals["other_assets"] - totals["other_liabilities"])}

    # the owner's capital: declared, then proved ----------------------------------------------------------------------------------
    cap_rows = data["Owner's capital"]
    declared = None
    if len(cap_rows) > 1:
        errors.append({"sheet": "Owner's capital", "row": cap_rows[1][0], "message": "Only one row: the owner declares one opening capital figure."})
    elif cap_rows:
        n, d = cap_rows[0]
        if _gate("Owner's capital", n, d, errors):
            try:
                declared = _money(d["amount"], "Owner's capital", positive=False)
            except BusinessRuleError as e:
                errors.append({"sheet": "Owner's capital", "row": n, "message": str(e)})
    else:
        errors.append({"sheet": "Owner's capital", "row": None, "message": "Owner's capital is missing. The owner must declare the opening equity so the books can be proved."})
    assets = totals["stock"] + totals["distributors"] + totals["customers"] + totals["cash"] + totals["other_assets"]
    liabilities = totals["suppliers"] + totals["other_liabilities"]
    expected = (assets - liabilities).quantize(Q2)
    out_totals = {"assets": str(assets), "liabilities": str(liabilities), "expected_capital": str(expected), "declared_capital": str(declared) if declared is not None else None,
                  "difference": str((declared - expected).quantize(Q2)) if declared is not None else None, "stock": str(totals["stock"]), "distributors": str(totals["distributors"]),
                  "suppliers": str(totals["suppliers"]), "customers": str(totals["customers"]), "cash": str(totals["cash"])}
    if declared is not None and declared != expected:
        errors.append({"sheet": "Owner's capital", "row": cap_rows[0][0],
                       "message": f"Declared capital {declared:,.2f} does not equal assets {assets:,.2f} minus liabilities {liabilities:,.2f} = {expected:,.2f} "
                                  f"(difference {declared - expected:,.2f}). The system does not adjust it: find the missing or wrong figure in the registers."})
    summary["Owner's capital"] = {"rows": 1 if declared is not None else 0, "amount": str(declared) if declared is not None else "0.00"}

    batch_ref = None
    if not errors and declared is not None:
        module_net = (totals["stock"] + totals["distributors"] - totals["suppliers"]).quantize(Q2)
        lines = list(batch_lines)
        if module_net != 0:
            lines.append({"account": "3200", "debit": module_net if module_net > 0 else 0, "credit": -module_net if module_net < 0 else 0, "verified": True,
                          "evidence": "Clears opening balance equity: the stock, distributor and supplier openings entered by this same file", "note": "Clearing"})
        lines.append({"account": "3000", "debit": 0, "credit": declared, "verified": True, "evidence": "Owner's capital as declared on the Owner's capital sheet", "note": "Declared by the owner"})
        # customers + cash + other balance a batch only together with the module-driven clearing and the capital line
        try:
            with db.begin_nested():
                b = ops.create_batch(db, actor=actor, as_of_date=as_of, description="Opening balances from the paper registers (workbook import)", lines=lines, ip=ip)
                batch_ref = b.ref
        except (BusinessRuleError, ConflictError, NotFoundError) as e:
            errors.append({"sheet": "Owner's capital", "row": None, "message": str(e)})
    return {"errors": errors, "summary": summary, "totals": out_totals, "batch_ref": batch_ref}


def run(db: Session, *, actor: User, content: bytes, as_of: date, dry_run: bool, ip: str | None) -> dict:
    """Check (dry_run) or enter the workbook. A check enters everything for real and rolls back; the caller commits only a successful, non-dry run."""
    if not content or len(content) > MAX_BYTES:
        raise BusinessRuleError("The file is empty or larger than 5 MB.")
    if as_of > date.today():
        raise BusinessRuleError("Opening balances cannot be dated in the future.")
    sha = hashlib.sha256(content).hexdigest()
    if not dry_run:
        prev = db.scalar(select(AuditLog.id).where(AuditLog.action == "opening.import", AuditLog.after["sha256"].astext == sha))
        if prev:
            raise ConflictError("This exact file has already been imported. Correct a mistake with an adjustment or reversal, not by importing again.")
    res = _run(db, actor, content, as_of, ip)
    ok = not res["errors"]
    res.update({"ok": ok, "dry_run": dry_run, "sha256": sha, "as_of": as_of.isoformat()})
    if not ok or dry_run:
        db.rollback()                                  # nothing from a check or a refused file survives
        res["batch_ref"] = res["batch_ref"] if ok else None
        return res
    audit_service.record(db, actor=actor, action="opening.import", entity_type="opening_import", entity_id=sha[:12],
                         after={"sha256": sha, "as_of": as_of.isoformat(), "summary": res["summary"], "totals": res["totals"], "batch": res["batch_ref"]}, ip=ip)
    return res
