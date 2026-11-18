"""Goods issued to distributors on credit.

* Prices come from the server: the distributor's agreed price (or the variant's standard distributor price) less the
  distributor's terms discount. A line may be priced lower only inside `distributors.max_extra_discount_pct` (default 0),
  never higher. Every price is copied onto the line, so later price changes never alter a posted dispatch.
* Posting moves stock out at average cost (the cost is stored on the line) and books the charge in the receivable ledger,
  once. A dispatch that exceeds the credit limit, is back-dated beyond the allowance, or carries an extra discount is
  HELD: nothing moves and nothing is owed until someone else with distributors.approve approves it.
* Opening balances and debit notes are charges without goods.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from backend.models import (
    DistributorInvoice, DistributorInvoiceLine, DistributorReturn, Location, ProductVariant, StockMovement, User,
)
from backend.services import audit_service, distributor_service as ds, ledger, reservation_service, settings_service
from backend.services.errors import BusinessRuleError, ConflictError, NotFoundError, SelfApprovalError
from backend.services.ledger import Spec

q2, q4 = ds.q2, ds.q4


def get_invoice(db: Session, invoice_id: int, *, lock: bool = False) -> DistributorInvoice:
    q = select(DistributorInvoice).where(DistributorInvoice.id == invoice_id)
    if lock:
        q = q.with_for_update().execution_options(populate_existing=True)
    i = db.scalar(q)
    if i is None:
        raise NotFoundError("Dispatch not found")
    return i


def _audit(db, actor, action, inv: DistributorInvoice, ip, reason=None, **after):
    audit_service.record(db, actor=actor, action=action, entity_type="distributor_invoice", entity_id=inv.id, location_id=inv.location_id,
                         reason=reason, after={"ref": inv.ref, "status": inv.status, "total": str(inv.total), **after}, ip=ip)


def _ref(db: Session, prefix: str) -> str:
    return f"{prefix}-{db.scalar(text('select nextval(:s)'), {'s': 'distributor_invoice_ref_seq'}):06d}"


def _price_lines(db: Session, d, lines: list[dict]) -> tuple[list[DistributorInvoiceLine], Decimal, Decimal]:
    if not lines:
        raise BusinessRuleError("A dispatch needs at least one line.")
    seen, out = set(), []
    subtotal = net = Decimal(0)
    for i, l in enumerate(lines, 1):
        n = int(l["qty"])
        if n <= 0:
            raise BusinessRuleError(f"Line {i}: quantity must be greater than zero.")
        v = db.get(ProductVariant, l["variant_id"])
        if v is None:
            raise BusinessRuleError(f"Line {i}: item not found.")
        key = (v.id, l.get("batch_id"))
        if key in seen:
            raise BusinessRuleError(f"Line {i}: the same item appears twice. Combine the quantities.")
        seen.add(key)
        base = q4(ds.base_price(db, d, v))
        agreed = q4(base * (Decimal(100) - Decimal(d.discount_pct)) / 100)
        price = q4(l["unit_price"]) if l.get("unit_price") not in (None, "") else agreed
        if price < 0:
            raise BusinessRuleError(f"{v.sku}: the price cannot be negative.")
        if price > agreed:
            raise BusinessRuleError(f"{v.sku}: the price {price} is above the agreed price {agreed}. Prices above the agreement are not supported.")
        out.append(DistributorInvoiceLine(variant_id=v.id, batch_id=l.get("batch_id"), qty=n, base_price=base,
                                          discount_pct=Decimal(d.discount_pct), agreed_price=agreed, unit_price=price))
        subtotal += base * n
        net += price * n
    return out, q2(subtotal), q2(net)


def _extra_discount_pct(l: DistributorInvoiceLine) -> Decimal:
    return q2((Decimal(l.agreed_price) - Decimal(l.unit_price)) / Decimal(l.agreed_price) * 100) if Decimal(l.agreed_price) > 0 else Decimal(0)


def create_dispatch(db: Session, *, actor: User, distributor_id: int, location_id: int, invoice_date: date, lines: list[dict],
                    notes: str | None, exchange_return_id: int | None, idempotency_key: str | None, ip: str | None) -> DistributorInvoice:
    d = ds.get_distributor(db, distributor_id, lock=True)
    if idempotency_key:
        prev = db.scalar(select(DistributorInvoice).where(DistributorInvoice.distributor_id == d.id, DistributorInvoice.idempotency_key == idempotency_key))
        if prev is not None:
            return prev
    if not d.is_active:
        raise BusinessRuleError(f"{d.name} is inactive. Reactivate the distributor before dispatching goods.")
    loc = db.get(Location, location_id)
    if loc is None or not loc.is_active:
        raise BusinessRuleError("Choose an active location to dispatch from.")
    if invoice_date > date.today():
        raise BusinessRuleError("The dispatch date cannot be in the future.")
    if exchange_return_id is not None:
        r = db.get(DistributorReturn, exchange_return_id)
        if r is None or r.distributor_id != d.id or r.status in ("cancelled", "rejected"):
            raise BusinessRuleError("The exchange must refer to a return from this distributor that has not been cancelled or rejected.")
    rows, subtotal, net = _price_lines(db, d, lines)
    inv = DistributorInvoice(ref=_ref(db, "DSP"), distributor_id=d.id, kind="dispatch", location_id=location_id, invoice_date=invoice_date,
                             due_date=invoice_date + timedelta(days=d.payment_terms_days), status="draft", subtotal=subtotal,
                             discount_total=q2(subtotal - net), total=net, exchange_return_id=exchange_return_id,
                             idempotency_key=idempotency_key, notes=notes, created_by=actor.id)
    inv.lines = rows
    db.add(inv)
    db.flush()
    _audit(db, actor, "distributor_dispatch.created", inv, ip)
    return inv


def update_draft(db: Session, *, actor: User, invoice_id: int, location_id: int | None, invoice_date: date | None, lines: list[dict] | None,
                 notes: str | None, ip: str | None) -> DistributorInvoice:
    inv = get_invoice(db, invoice_id, lock=True)
    if inv.kind != "dispatch" or inv.status != "draft":
        raise ConflictError(f"{inv.ref} is {inv.status}; only draft dispatches can be edited.")
    d = ds.get_distributor(db, inv.distributor_id, lock=True)
    if location_id:
        inv.location_id = location_id
    if invoice_date:
        if invoice_date > date.today():
            raise BusinessRuleError("The dispatch date cannot be in the future.")
        inv.invoice_date = invoice_date
        inv.due_date = invoice_date + timedelta(days=d.payment_terms_days)
    if notes is not None:
        inv.notes = notes
    if lines is not None:
        rows, subtotal, net = _price_lines(db, d, lines)
        inv.lines.clear()
        db.flush()
        inv.lines = rows
        inv.subtotal, inv.total, inv.discount_total = subtotal, net, q2(subtotal - net)
    db.flush()
    _audit(db, actor, "distributor_dispatch.updated", inv, ip)
    return inv


def holds_for(db: Session, d, inv: DistributorInvoice) -> list[str]:
    reasons: list[str] = []
    exposure = ds.balance(db, d.id) + Decimal(inv.total)
    if exposure > Decimal(d.credit_limit):
        reasons.append(f"credit_limit: balance would be {q2(exposure)} against a limit of {q2(d.credit_limit)}")
    allow = int(settings_service.get(db, settings_service.DIST_BACKDATE_KEY))
    if inv.invoice_date < date.today() - timedelta(days=allow):
        reasons.append(f"backdated: dated {inv.invoice_date}, more than {allow} days ago")
    limit = Decimal(str(settings_service.get(db, settings_service.DIST_DISCOUNT_KEY)))
    worst = max((_extra_discount_pct(l) for l in inv.lines), default=Decimal(0))
    if worst > limit:
        reasons.append(f"discount: a line is {worst}% below the agreed price (limit {limit}%)")
    return reasons


def _execute_post(db: Session, actor: User, inv: DistributorInvoice, d, ip: str | None) -> None:
    """Move the stock out and book the charge. Called exactly once per dispatch (status gates it)."""
    keys: list[tuple[DistributorInvoiceLine, int | None, int]] = []
    for l in sorted(inv.lines, key=lambda x: (x.variant_id, x.batch_id or 0)):
        reservation_service.reserve(db, location_id=inv.location_id, variant_id=l.variant_id, qty=l.qty, ref_type="distributor_dispatch",
                                    ref_id=str(inv.id), batch_id=l.batch_id, actor=actor)
        for batch_id, n in reservation_service.consume_qty(db, "distributor_dispatch", str(inv.id), l.variant_id, l.qty):
            keys.append((l, batch_id, n))
    specs = [Spec(inv.location_id, l.variant_id, b, "good", -n, "out") for l, b, n in keys]
    txn = ledger.post(db, txn_type="distributor_dispatch", location_id=inv.location_id, specs=specs, actor=actor,
                      reason=f"Dispatch {inv.ref} to {d.name}", source_type="distributor_invoice", source_id=inv.id, ref_prefix="DIS")
    values: dict[tuple, Decimal] = {}
    for m in db.scalars(select(StockMovement).where(StockMovement.txn_id == txn.id)).all():
        k = (m.variant_id, m.batch_id)
        values[k] = values.get(k, Decimal(0)) + (-Decimal(m.value_delta))
    originals = list(inv.lines)
    inv.lines.clear()
    db.flush()
    for l, b, n in keys:
        inv.lines.append(DistributorInvoiceLine(variant_id=l.variant_id, batch_id=b, qty=n, base_price=l.base_price, discount_pct=l.discount_pct,
                                                agreed_price=l.agreed_price, unit_price=l.unit_price, stock_value=values[(l.variant_id, b)]))
    inv.status, inv.posted_by, inv.posted_at, inv.stock_txn_id = "posted", actor.id, datetime.now(timezone.utc), txn.id
    db.flush()
    ds.add_ledger_entry(db, distributor_id=d.id, entry_type="invoice", amount=Decimal(inv.total), entry_date=inv.invoice_date,
                        source_type="distributor_invoice", source_id=inv.id, ref=inv.ref, memo=f"Goods dispatched, due {inv.due_date}", actor=actor)
    _audit(db, actor, "distributor_dispatch.posted", inv, ip, stock_txn=txn.ref)


def post(db: Session, *, actor: User, invoice_id: int, ip: str | None) -> DistributorInvoice:
    inv = get_invoice(db, invoice_id, lock=True)
    d = ds.get_distributor(db, inv.distributor_id, lock=True)      # serialises credit-limit checks per distributor
    inv = get_invoice(db, invoice_id, lock=True)
    if inv.kind != "dispatch" or inv.status != "draft":
        raise ConflictError(f"{inv.ref} is {inv.status}; only a draft dispatch can be posted.")
    if not d.is_active:
        raise BusinessRuleError(f"{d.name} is inactive.")
    reasons = holds_for(db, d, inv)
    if reasons:
        inv.status, inv.hold_reasons = "held", reasons
        db.flush()
        _audit(db, actor, "distributor_dispatch.held", inv, ip, reasons=reasons)
        return inv
    _execute_post(db, actor, inv, d, ip)
    return inv


def decide(db: Session, *, actor: User, invoice_id: int, approve: bool, note: str, ip: str | None) -> DistributorInvoice:
    inv = get_invoice(db, invoice_id, lock=True)
    d = ds.get_distributor(db, inv.distributor_id, lock=True)
    inv = get_invoice(db, invoice_id, lock=True)
    if inv.status != "held":
        raise ConflictError(f"{inv.ref} is {inv.status}; there is nothing waiting for approval.")
    if inv.created_by == actor.id:
        raise SelfApprovalError("You cannot approve a dispatch you entered yourself.")
    if not (note or "").strip():
        raise BusinessRuleError("Say why you are approving or rejecting this dispatch.")
    inv.decided_by, inv.decided_at, inv.decision_note = actor.id, datetime.now(timezone.utc), note.strip()
    if not approve:
        inv.status = "rejected"
        db.flush()
        _audit(db, actor, "distributor_dispatch.rejected", inv, ip, reason=note)
        return inv
    _execute_post(db, actor, inv, d, ip)
    _audit(db, actor, "distributor_dispatch.approved", inv, ip, reason=note)
    return inv


def cancel(db: Session, *, actor: User, invoice_id: int, reason: str, ip: str | None) -> DistributorInvoice:
    inv = get_invoice(db, invoice_id, lock=True)
    if inv.status not in ("draft", "held") or inv.kind != "dispatch":
        raise ConflictError(f"{inv.ref} is {inv.status}; goods have left or it is already closed. Use a return to take goods back.")
    if not (reason or "").strip():
        raise BusinessRuleError("Say why the dispatch is being cancelled.")
    inv.status, inv.void_reason = "cancelled", reason.strip()
    db.flush()
    _audit(db, actor, "distributor_dispatch.cancelled", inv, ip, reason=reason)
    return inv


def post_opening(db: Session, *, actor: User, distributor_id: int, amount, as_of: date, note: str | None, ip: str | None) -> DistributorInvoice:
    """The balance the distributor owed before this system. One per distributor; it settles like any other charge."""
    d = ds.get_distributor(db, distributor_id, lock=True)
    amt = q2(amount)
    if amt <= 0:
        raise BusinessRuleError("The opening balance must be greater than zero.")
    if as_of > date.today():
        raise BusinessRuleError("The opening balance date cannot be in the future.")
    if db.scalar(select(DistributorInvoice.id).where(DistributorInvoice.distributor_id == d.id, DistributorInvoice.kind == "opening",
                                                     DistributorInvoice.status.in_(("posted", "held")))):
        raise ConflictError(f"{d.name} already has an opening balance. Use an adjustment to correct it.")
    inv = DistributorInvoice(ref=_ref(db, "OPN"), distributor_id=d.id, kind="opening", invoice_date=as_of, due_date=as_of + timedelta(days=d.payment_terms_days),
                             status="posted", subtotal=amt, discount_total=0, total=amt, notes=note, created_by=actor.id,
                             posted_by=actor.id, posted_at=datetime.now(timezone.utc))
    db.add(inv)
    db.flush()
    ds.add_ledger_entry(db, distributor_id=d.id, entry_type="invoice", amount=amt, entry_date=as_of, source_type="distributor_invoice",
                        source_id=inv.id, ref=inv.ref, memo="Opening balance", actor=actor)
    _audit(db, actor, "distributor_opening.posted", inv, ip)
    return inv
