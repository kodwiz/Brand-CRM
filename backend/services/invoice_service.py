"""Supplier invoices: entry, three-way matching against the purchase order and goods receipts, and posting.

The supplier liability is created in exactly one place, `_book`, when an invoice is posted. Goods receipts never
create a liability, so receiving goods and then matching the invoice cannot count the same debt twice. See docs/purchasing.md.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from backend.models import (
    PaymentAllocation, ProductVariant, PurchaseOrder, PurchaseOrderLine, SupplierAdjustment, SupplierInvoice,
    SupplierInvoiceCharge, SupplierInvoiceLine, SupplierPayment, User,
)
from backend.services import audit_service, ledger, purchase_service, settings_service as cfg, supplier_service
from backend.services.errors import BusinessRuleError, ConflictError, NotFoundError, SelfApprovalError

q2 = supplier_service.q2


def get_invoice(db: Session, invoice_id: int, *, lock: bool = False) -> SupplierInvoice:
    q = select(SupplierInvoice).where(SupplierInvoice.id == invoice_id)
    if lock:
        q = q.with_for_update().execution_options(populate_existing=True)
    inv = db.scalar(q)
    if inv is None:
        raise NotFoundError("Supplier invoice not found")
    return inv


def paid_amount(db: Session, invoice_id: int) -> Decimal:
    return Decimal(db.scalar(select(func.coalesce(func.sum(PaymentAllocation.amount), 0)).where(
        PaymentAllocation.invoice_id == invoice_id, PaymentAllocation.released_at.is_(None))))


def credited_amount(db: Session, invoice_id: int) -> Decimal:
    return Decimal(db.scalar(select(func.coalesce(func.sum(SupplierAdjustment.amount), 0)).where(
        SupplierAdjustment.invoice_id == invoice_id, SupplierAdjustment.status == "approved")))


def outstanding(db: Session, inv: SupplierInvoice) -> Decimal:
    if inv.status != "posted":
        return Decimal(0)
    return q2(Decimal(inv.total) - paid_amount(db, inv.id) - credited_amount(db, inv.id))


def payment_status(db: Session, inv: SupplierInvoice) -> str:
    if inv.status != "posted":
        return inv.status
    out = outstanding(db, inv)
    if out <= 0:
        return "paid"
    if out < Decimal(inv.total):
        return "partially_paid"
    return "unpaid"


def create_invoice(db: Session, *, actor: User, supplier_id: int, invoice_no: str, invoice_date: date, due_date: date | None,
                   purchase_order_id: int | None, lines: list[dict], charges: list[dict] | None, discount, tax,
                   notes: str | None, post_now: bool, ip: str | None) -> SupplierInvoice:
    supplier = supplier_service.get_supplier(db, supplier_id)
    invoice_no = (invoice_no or "").strip()
    if not invoice_no:
        raise BusinessRuleError("The supplier's invoice number is required.")
    if invoice_date > date.today():
        raise BusinessRuleError("The invoice date cannot be in the future.")
    if not lines:
        raise BusinessRuleError("An invoice needs at least one line.")
    dup = db.scalar(select(SupplierInvoice).where(
        SupplierInvoice.supplier_id == supplier_id, func.lower(SupplierInvoice.invoice_no) == invoice_no.lower(),
        SupplierInvoice.status.in_(("draft", "held", "posted"))))
    if dup is not None:
        raise ConflictError(f"{supplier.name} invoice {invoice_no} is already recorded as {dup.ref}. "
                            "The same invoice cannot be entered twice.")

    po = None
    terms = supplier.payment_terms_days
    if purchase_order_id:
        # lock before inserting the invoice: the FK would otherwise take a shared lock first and two writers deadlock
        po = purchase_service.get_po(db, purchase_order_id, lock=True)
        if po.supplier_id != supplier_id:
            raise BusinessRuleError(f"{po.ref} belongs to a different supplier.")
        if po.status in ("draft", "cancelled"):
            raise BusinessRuleError(f"{po.ref} is {po.status}; invoices can only be matched to an approved order.")
        terms = po.payment_terms_days
    due = due_date or (invoice_date + timedelta(days=terms))
    if due < invoice_date:
        raise BusinessRuleError("The due date cannot be before the invoice date.")

    po_lines = {l.id: l for l in po.lines} if po else {}
    inv = SupplierInvoice(ref=ledger.next_ref(db, "supplier_invoice_ref_seq", "SIN"), supplier_id=supplier_id,
                          purchase_order_id=po.id if po else None, invoice_no=invoice_no, invoice_date=invoice_date,
                          due_date=due, notes=notes, created_by=actor.id, subtotal=0, total=0)
    subtotal = Decimal(0)
    for i, ln in enumerate(lines, 1):
        qty = int(ln["qty"])
        price = Decimal(str(ln["unit_price"]))
        if qty <= 0 or price < 0:
            raise BusinessRuleError(f"Line {i}: quantity must be above zero and the price cannot be negative.")
        pl_id = ln.get("po_line_id")
        variant_id = ln.get("variant_id")
        if pl_id:
            pl = po_lines.get(pl_id)
            if pl is None:
                raise BusinessRuleError(f"Line {i}: that order line does not belong to {po.ref if po else 'a purchase order'}.")
            variant_id = pl.variant_id
        elif variant_id and db.get(ProductVariant, variant_id) is None:
            raise BusinessRuleError(f"Line {i}: item not found.")
        desc = (ln.get("description") or "").strip() or None
        if not variant_id and not desc:
            raise BusinessRuleError(f"Line {i}: describe what is being billed.")
        total = q2(qty * price)
        subtotal += total
        inv.lines.append(SupplierInvoiceLine(po_line_id=pl_id, variant_id=variant_id, description=desc, qty=qty,
                                             unit_price=ledger.q4(price), line_total=total))
    charges_total = Decimal(0)
    for c in charges or []:
        amt = Decimal(str(c["amount"]))
        if amt <= 0 or not (c.get("description") or "").strip():
            raise BusinessRuleError("Each extra charge needs a description and an amount above zero.")
        inv.charges.append(SupplierInvoiceCharge(description=c["description"].strip(), amount=q2(amt)))
        charges_total += q2(amt)
    disc, tx = q2(discount or 0), q2(tax or 0)
    if disc < 0 or tx < 0:
        raise BusinessRuleError("Discount and tax cannot be negative.")
    if disc > subtotal + charges_total:
        raise BusinessRuleError("The discount cannot exceed the invoice amount.")
    inv.subtotal, inv.charges_total, inv.discount, inv.tax = q2(subtotal), q2(charges_total), disc, tx
    inv.total = q2(subtotal + charges_total + tx - disc)
    if inv.total <= 0:
        raise BusinessRuleError("The invoice total must be greater than zero.")
    db.add(inv)
    db.flush()
    audit_service.record(db, actor=actor, action="supplier_invoice.created", entity_type="supplier_invoice", entity_id=inv.id,
                         after={"ref": inv.ref, "supplier": supplier.code, "invoice_no": invoice_no, "total": str(inv.total),
                                "po": po.ref if po else None}, ip=ip)
    if post_now:
        post_invoice(db, actor=actor, invoice_id=inv.id, ip=ip)
    return inv


# ---- matching ------------------------------------------------------------------------------------------
def evaluate_match(db: Session, inv: SupplierInvoice) -> tuple[str, list[dict], bool]:
    """Returns (match_status, variances, hold). `hold` means a person must approve before a liability is created."""
    if inv.purchase_order_id is None:
        return "no_po", [], False
    price_tol = cfg.purchasing_value(db, cfg.PRICE_TOL_KEY)
    qty_tol = cfg.purchasing_value(db, cfg.QTY_TOL_KEY)
    qty = purchase_service.line_quantities(db, inv.purchase_order_id)  # posted + held invoices, EXCLUDING this draft
    pls = {l.id: l for l in db.scalars(select(PurchaseOrderLine).where(PurchaseOrderLine.po_id == inv.purchase_order_id)).all()}
    variances: list[dict] = []
    hold = False
    this: dict[int, int] = {}
    for ln in inv.lines:
        label = db.get(ProductVariant, ln.variant_id).sku if ln.variant_id else (ln.description or "line")
        if ln.po_line_id is None:
            variances.append({"type": "not_on_po", "item": label, "message": f"{label} is not on the purchase order."})
            hold = True
            continue
        pl = pls[ln.po_line_id]
        this[pl.id] = this.get(pl.id, 0) + ln.qty
        allowed_diff = Decimal(pl.unit_cost) * price_tol / Decimal(100)
        if abs(Decimal(ln.unit_price) - Decimal(pl.unit_cost)) > allowed_diff:
            variances.append({"type": "price_variance", "item": label, "ordered_price": str(pl.unit_cost),
                              "invoiced_price": str(ln.unit_price),
                              "message": f"{label}: invoiced at {ln.unit_price}, ordered at {pl.unit_cost}."})
            hold = True
    informational = False
    for lid, add in this.items():
        pl = pls[lid]
        q = qty[lid]
        label = db.get(ProductVariant, pl.variant_id).sku
        total_invoiced = q["invoiced"] + add
        max_qty = int((Decimal(pl.qty_ordered) * (Decimal(100) + qty_tol) / Decimal(100)).to_integral_value(rounding="ROUND_FLOOR"))
        if total_invoiced > max_qty:
            variances.append({"type": "over_invoiced", "item": label, "ordered": pl.qty_ordered, "invoiced_total": total_invoiced,
                              "message": f"{label}: {total_invoiced} invoiced in total but only {pl.qty_ordered} ordered."})
            hold = True
        if total_invoiced > q["owned"]:
            informational = True
            variances.append({"type": "awaiting_receipt", "item": label, "received": q["owned"], "invoiced_total": total_invoiced,
                              "message": f"{label}: invoiced {total_invoiced}, received so far {q['owned']}."})
    status = "variance" if hold else ("awaiting_receipt" if informational else "matched")
    return status, variances, hold


# ---- posting -------------------------------------------------------------------------------------------
def _book(db: Session, inv: SupplierInvoice, actor: User) -> None:
    """Create the liability. The only place an invoice touches the supplier ledger."""
    supplier_service.add_ledger_entry(
        db, supplier_id=inv.supplier_id, entry_type="invoice", amount=Decimal(inv.total), entry_date=inv.invoice_date,
        source_type="supplier_invoice", source_id=inv.id, ref=inv.ref, memo=f"Invoice {inv.invoice_no}", actor=actor)
    inv.status = "posted"
    inv.posted_by = actor.id
    inv.posted_at = datetime.now(timezone.utc)


def post_invoice(db: Session, *, actor: User, invoice_id: int, ip: str | None) -> SupplierInvoice:
    inv = get_invoice(db, invoice_id, lock=True)
    if inv.status != "draft":
        raise ConflictError(f"{inv.ref} is already {inv.status}; it cannot be posted again.")
    if inv.purchase_order_id:  # serialise with receipts and other invoices on the same order
        purchase_service.get_po(db, inv.purchase_order_id, lock=True)
    status, variances, hold = evaluate_match(db, inv)
    inv.match_status, inv.variances = status, variances or None
    if hold:
        inv.status = "held"
        action = "supplier_invoice.held"
    else:
        _book(db, inv, actor)
        action = "supplier_invoice.posted"
    db.flush()
    audit_service.record(db, actor=actor, action=action, entity_type="supplier_invoice", entity_id=inv.id,
                         after={"ref": inv.ref, "total": str(inv.total), "match": status,
                                "variances": [v["message"] for v in variances]}, ip=ip)
    return inv


def decide_invoice(db: Session, *, actor: User, invoice_id: int, approve: bool, note: str | None,
                   ip: str | None) -> SupplierInvoice:
    inv = get_invoice(db, invoice_id, lock=True)
    if inv.status != "held":
        raise ConflictError(f"{inv.ref} is {inv.status}; only held invoices need a decision.")
    if inv.created_by == actor.id:
        raise SelfApprovalError("You cannot decide an invoice you entered yourself. Ask another authorised user.")
    if not approve and not (note or "").strip():
        raise BusinessRuleError("A reason is required to reject an invoice.")
    if approve and not (note or "").strip():
        raise BusinessRuleError("Say why the variance is acceptable.")
    if inv.purchase_order_id:
        purchase_service.get_po(db, inv.purchase_order_id, lock=True)
    inv.decided_by, inv.decided_at, inv.decision_note = actor.id, datetime.now(timezone.utc), note
    if approve:
        _book(db, inv, actor)
    else:
        inv.status = "rejected"
    db.flush()
    audit_service.record(db, actor=actor, action="supplier_invoice.variance_approved" if approve else "supplier_invoice.rejected",
                         entity_type="supplier_invoice", entity_id=inv.id, reason=note,
                         after={"ref": inv.ref, "total": str(inv.total)}, ip=ip)
    return inv


def cancel_draft(db: Session, *, actor: User, invoice_id: int, reason: str, ip: str | None) -> SupplierInvoice:
    inv = get_invoice(db, invoice_id, lock=True)
    if inv.status != "draft":
        raise ConflictError("Only draft invoices can be cancelled. Void a posted invoice instead.")
    inv.status = "cancelled"
    db.flush()
    audit_service.record(db, actor=actor, action="supplier_invoice.cancelled", entity_type="supplier_invoice", entity_id=inv.id,
                         reason=reason, after={"ref": inv.ref}, ip=ip)
    return inv


def void_invoice(db: Session, *, actor: User, invoice_id: int, reason: str, ip: str | None) -> SupplierInvoice:
    if not (reason or "").strip():
        raise BusinessRuleError("A reason is required to void an invoice.")
    inv = get_invoice(db, invoice_id, lock=True)
    if inv.status != "posted":
        raise ConflictError(f"{inv.ref} is {inv.status}; only posted invoices can be voided.")
    if inv.posted_by == actor.id:
        raise SelfApprovalError("You cannot void an invoice you posted yourself. Ask another authorised user.")
    if paid_amount(db, inv.id) > 0 or credited_amount(db, inv.id) > 0:
        raise BusinessRuleError(f"{inv.ref} has payments or credits applied. Reverse those first, then void the invoice.")
    supplier_service.add_ledger_entry(
        db, supplier_id=inv.supplier_id, entry_type="invoice_void", amount=-Decimal(inv.total), entry_date=date.today(),
        source_type="supplier_invoice", source_id=inv.id, ref=inv.ref, memo=f"Void of invoice {inv.invoice_no}", actor=actor)
    inv.status = "void"
    inv.voided_by, inv.voided_at, inv.void_reason = actor.id, datetime.now(timezone.utc), reason
    db.flush()
    audit_service.record(db, actor=actor, action="supplier_invoice.voided", entity_type="supplier_invoice", entity_id=inv.id,
                         reason=reason, after={"ref": inv.ref, "total": str(inv.total)}, ip=ip)
    return inv
