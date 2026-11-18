"""Automatic accounting postings.

The operational modules (stock, suppliers, sales, distributors) write their own records and never touch the ledger.
This engine reads those records and creates balanced journal entries from them. It is idempotent: each entry is keyed by
(source_type, source_id, event), which is unique in the database, so running it twice, retrying after a failure, or
re-importing cannot double-post. A source that cannot be posted (for example a missing account) stays pending and is
reported; nothing is guessed.

Policies (see docs/accounting.md):
  * Inventory is carried at weighted-average cost taken from the stock ledger's own value, never recomputed here.
  * Goods received are Dr Inventory / Cr Goods-received-not-invoiced; a supplier invoice clears it against Supplier payables.
  * Revenue is recognised when a parcel is dispatched (default) or delivered (setting); cost of goods sold follows the same event.
  * Distributor dispatches create a receivable and revenue; goods issued are never treated as cash.
  * Customer money is kept per customer in Customer receivables; a customer with a credit balance is shown as an advance at reporting time.
"""
from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Iterator

from sqlalchemy import and_, exists, func, select
from sqlalchemy.orm import Session

from backend.models import (
    DistributorAdjustment, DistributorInvoice, EmployeePurchase, EmployeePurchasePayment, DistributorLedgerEntry, JournalEntry, Order, OrderPayment, SalesReturn, Shipment, StockMovement,
    StockTransaction, SupplierAdjustment, SupplierPayment, DistributorPayment, SupplierInvoice, SupplierLedgerEntry, User,
)
from backend.models.inventory import VALUED_BUCKETS
from backend.services import accounting_service as acc
from backend.services import settings_service
from backend.services.accounting_service import L, q2
from backend.services.errors import BusinessRuleError

SKIP = "skip"


class Unmapped(Exception):
    """The source cannot be posted yet (for example a type with no accounting rule)."""


def _d(ts) -> date:
    return ts.date() if isinstance(ts, datetime) else ts


def _dr_cr(account: str, amount: Decimal, memo: str | None = None, **kw) -> L:
    """Debit when positive, credit when negative."""
    return L(account=account, debit=amount if amount > 0 else 0, credit=-amount if amount < 0 else 0, memo=memo, **kw)


def _policy(db: Session) -> str:
    return settings_service.get(db, acc.REVENUE_KEY)


# ---- stock transactions ----------------------------------------------------------------------------------------------------------
COUNTER = {"opening_stock": "3200", "receipt": "2050", "supplier_return": "2050", "distributor_dispatch": "5000", "employee_purchase": "5000", "writeoff": "5100",
           "transfer_shortage": "5100", "adjustment": "5110", "count_variance": "5110", "transfer_out": "1210"}
ZERO_SUM_TYPES = ("return_disposition", "damage", "expiry", "transfer_transit")


def _stock_txn(db: Session, t: StockTransaction):
    if t.reverses_txn_id:
        orig = acc.find_by_source(db, "stock_txn", t.reverses_txn_id, "posted")
        if orig is None:
            o = db.get(StockTransaction, t.reverses_txn_id)
            if o is not None and o.accounting_status == "skipped":
                return SKIP
            raise Unmapped(f"the transaction it reverses (#{t.reverses_txn_id}) has not been posted yet")
        lines = acc.mirror_lines(orig)
        return _d(t.created_at), f"{t.ref} reversal: {t.reason or ''}", lines
    moves = list(t.movements)
    valued = sum((Decimal(m.value_delta) for m in moves if m.bucket in VALUED_BUCKETS), Decimal(0))
    transit = sum((Decimal(m.value_delta) for m in moves if m.bucket == "transit"), Decimal(0))
    kind = t.txn_type
    if kind in ZERO_SUM_TYPES:
        return SKIP
    if kind == "customer_sale":
        counter = "1220" if _policy(db) == "delivery" else "5000"
    elif kind == "return_intake":
        counter = "1220" if (t.source_type == "shipment_return" and _policy(db) == "delivery") else "5000"
    elif kind in COUNTER:
        counter = COUNTER[kind]
    elif kind == "transfer_in":
        counter = None
    else:
        raise Unmapped(f"no accounting rule for stock transaction type '{kind}'")
    v, tr = q2(valued), q2(transit)
    lines: list[L] = []
    if v != 0:
        lines.append(_dr_cr("1200", v, location_id=t.location_id))
    if tr != 0:
        lines.append(_dr_cr("1210", tr, location_id=t.location_id))
    rest = -(v + tr)
    if counter is not None and rest != 0:
        lines.append(_dr_cr(counter, rest, location_id=t.location_id))
    if not lines or (v == 0 and tr == 0):
        return SKIP
    return _d(t.created_at), f"{t.ref} {kind.replace('_', ' ')}" + (f": {t.reason}" if t.reason else ""), lines


# ---- supplier ledger -------------------------------------------------------------------------------------------------------------------
def _supplier_entry(db: Session, e: SupplierLedgerEntry):
    amt = Decimal(e.amount)
    sup = {"party_type": "supplier", "party_id": e.supplier_id}
    memo = f"{e.ref} {e.entry_type.replace('_', ' ')}" + (f": {e.memo}" if e.memo else "")
    if e.entry_type in ("invoice", "invoice_void"):
        inv = db.get(SupplierInvoice, e.source_id)
        tax = q2(inv.tax) if inv else Decimal(0)
        sign = 1 if amt > 0 else -1
        goods = q2(abs(amt)) - tax
        lines = [_dr_cr("2050", sign * goods), _dr_cr("2000", -sign * q2(abs(amt)), **sup)]
        if tax:
            lines.append(_dr_cr("2200", sign * tax))
        return e.entry_date, memo, [ln for ln in lines if (ln.debit or ln.credit)]
    if e.entry_type in ("payment", "payment_reversal"):
        pay = db.get(SupplierPayment, e.source_id)
        cash = acc.cash_account_for_method(db, pay.method)
        return e.entry_date, memo, [L(account="2000", debit=-amt if amt < 0 else 0, credit=amt if amt > 0 else 0, **sup),
                                    L(account=cash.account_id, debit=amt if amt > 0 else 0, credit=-amt if amt < 0 else 0)]
    if e.entry_type == "adjustment" and e.source_type == "supplier_opening":
        return e.entry_date, memo, [L(account="3200", debit=amt if amt > 0 else 0, credit=-amt if amt < 0 else 0),
                                    L(account="2000", debit=-amt if amt < 0 else 0, credit=amt if amt > 0 else 0, **sup)]
    if e.entry_type == "adjustment":
        a = db.get(SupplierAdjustment, e.source_id)
        if a is not None and a.document_no:
            memo += f" (doc {a.document_no})"
        return e.entry_date, memo, [L(account="2000", debit=-amt if amt < 0 else 0, credit=amt if amt > 0 else 0, **sup),
                                    L(account="2050", debit=amt if amt > 0 else 0, credit=-amt if amt < 0 else 0)]
    raise Unmapped(f"no accounting rule for supplier ledger entry type '{e.entry_type}'")


# ---- distributor ledger -----------------------------------------------------------------------------------------------------------------
def _distributor_entry(db: Session, e: DistributorLedgerEntry):
    amt = Decimal(e.amount)
    dist = {"party_type": "distributor", "party_id": e.distributor_id}
    memo = f"{e.ref} {e.entry_type.replace('_', ' ')}" + (f": {e.memo}" if e.memo else "")
    if e.entry_type in ("invoice", "invoice_void"):
        inv = db.get(DistributorInvoice, e.source_id)
        sign = 1 if amt > 0 else -1
        total = q2(abs(amt))
        if inv is not None and inv.kind == "opening":
            lines = [_dr_cr("1110", sign * total, **dist), _dr_cr("3200", -sign * total)]
        elif inv is not None and inv.kind == "debit_note":
            lines = [_dr_cr("1110", sign * total, **dist), _dr_cr("4900", -sign * total)]
        else:
            gross = q2(sum((Decimal(l.base_price) * l.qty for l in (inv.lines if inv else [])), Decimal(0)))
            if gross < total:
                gross = total
            lines = [_dr_cr("1110", sign * total, **dist), _dr_cr("4000", -sign * gross)]
            if gross > total:
                lines.append(_dr_cr("4200", sign * (gross - total)))
        loc = inv.location_id if inv is not None else None
        for ln in lines:
            ln.location_id = loc if ln.account in ("4000", "4200") else None
        return e.entry_date, memo, lines
    if e.entry_type in ("payment", "payment_reversal"):
        pay = db.get(DistributorPayment, e.source_id)
        cash = acc.cash_account_for_method(db, pay.method)
        return e.entry_date, memo, [L(account=cash.account_id, debit=-amt if amt < 0 else 0, credit=amt if amt > 0 else 0),
                                    L(account="1110", debit=amt if amt > 0 else 0, credit=-amt if amt < 0 else 0, **dist)]
    if e.entry_type == "credit_note":
        return e.entry_date, memo, [L(account="4100", debit=-amt), L(account="1110", credit=-amt, **dist)]
    if e.entry_type == "adjustment":
        a = db.get(DistributorAdjustment, e.source_id)
        expense = "5800" if (a is not None and a.kind == "write_off") else "4200"
        return e.entry_date, memo, [L(account=expense, debit=-amt), L(account="1110", credit=-amt, **dist)]
    raise Unmapped(f"no accounting rule for distributor ledger entry type '{e.entry_type}'")


# ---- customer payments, shipments, returns -----------------------------------------------------------------------------------------------
def _order_payment(db: Session, p: OrderPayment, event: str):
    if p.method == "store_credit":
        return SKIP         # store credit already sits on the customer's account; applying or issuing it moves no money
    cash = acc.cash_account_for_method(db, p.method)
    o = db.get(Order, p.order_id)
    amt = q2(p.amount)
    cust = {"party_type": "customer", "party_id": p.customer_id}
    if event == "reversed":
        orig = acc.find_by_source(db, "order_payment", p.id, "posted")
        if orig is None:
            raise Unmapped("the original receipt has not been posted")
        return _d(p.reversed_at) if p.reversed_at else date.today(), f"{p.ref} reversed: {p.reverse_reason or ''}", acc.mirror_lines(orig)
    loc = o.location_id
    if p.kind == "receipt":
        lines = [L(account=cash.account_id, debit=amt, location_id=loc), L(account="1100", credit=amt, location_id=loc, **cust)]
    else:
        lines = [L(account="1100", debit=amt, location_id=loc, **cust), L(account=cash.account_id, credit=amt, location_id=loc)]
    return p.paid_date, f"{p.ref} {p.kind} {o.ref} by {p.method}", lines


def _shipment_sale(db: Session, s: Shipment, o: Order):
    ol = {l.id: l for l in o.lines}
    net = gross = Decimal(0)
    for sl in s.lines:
        l = ol[sl.order_line_id]
        net += Decimal(l.unit_price) * sl.qty
        gross += Decimal(l.list_price) * sl.qty
    net, gross = q2(net), q2(gross)
    tax = q2(net * Decimal(o.tax_rate_pct) / 100)
    first = db.scalar(select(func.min(Shipment.id)).where(Shipment.order_id == o.id, Shipment.dispatched_at.is_not(None)))
    fee = q2(o.delivery_fee) if first == s.id else Decimal(0)
    total = net + tax + fee
    lines = [L(account="1100", debit=total, party_type="customer", party_id=o.customer_id, location_id=s.location_id),
             L(account="4000", credit=gross, location_id=s.location_id)]
    if gross > net:
        lines.append(L(account="4200", debit=gross - net, location_id=s.location_id))
    if tax:
        lines.append(L(account="2200", credit=tax))
    if fee:
        lines.append(L(account="4010", credit=fee, location_id=s.location_id))
    return lines


def _sales_return(db: Session, r: SalesReturn):
    o = db.get(Order, r.order_id)
    ol = {l.id: l for l in o.lines}
    rate = Decimal(o.tax_rate_pct)
    net = tax = Decimal(0)
    for l in r.lines:
        if not l.refund_qty:
            continue
        unit = Decimal(ol[l.order_line_id].unit_price)
        net += unit * l.refund_qty
        tax += q2(unit * rate / 100) * l.refund_qty
    fee = q2(o.delivery_fee) if r.refund_delivery_fee else Decimal(0)
    net, tax = q2(net), q2(tax)
    value = q2(r.return_value or 0)
    net += value - (net + tax + fee)          # any rounding difference stays inside the returned goods, never in cash
    if value <= 0:
        return SKIP
    lines = [L(account="4100", debit=net, location_id=r.location_id), L(account="1100", credit=value, party_type="customer", party_id=r.customer_id,
                                                                    location_id=r.location_id)]
    if tax:
        lines.append(L(account="2200", debit=tax))
    if fee:
        lines.append(L(account="4010", debit=fee, location_id=r.location_id))
    return _d(r.settled_at) if r.settled_at else date.today(), f"{r.ref} customer return settled ({o.ref})", lines


# ---- employee purchases ------------------------------------------------------------------------------------------------------------------
def _employee_sale(db: Session, p: EmployeePurchase, event: str):
    """Goods handed over to an employee are a sale at list price with the staff discount shown separately; what the employee owes sits in
    Employee purchase receivables until paid. The cost of the goods is posted from the stock transaction (Dr Cost of goods sold, Cr Inventory)."""
    emp = {"party_type": "employee", "party_id": p.employee_id}
    if event == "sale_reversal":
        orig = acc.find_by_source(db, "employee_purchase", p.id, "sale")
        if orig is None:
            raise Unmapped("the original sale has not been posted")
        return _d(p.voided_at), f"{p.ref} voided: {p.void_reason or ''}", acc.mirror_lines(orig)
    gross, disc, tax, total = q2(p.subtotal_list), q2(p.discount_total), q2(p.tax), q2(p.total)
    lines = [L(account="1400", debit=total, location_id=p.location_id, **emp), L(account="4000", credit=gross, location_id=p.location_id)]
    if disc:
        lines.append(L(account="4200", debit=disc, location_id=p.location_id))
    if tax:
        lines.append(L(account="2200", credit=tax))
    return p.purchase_date, f"{p.ref} employee purchase", lines


def _employee_payment(db: Session, pay: EmployeePurchasePayment, event: str):
    cash = acc.cash_account_for_method(db, pay.method)
    amt = q2(pay.amount)
    emp = {"party_type": "employee", "party_id": pay.employee_id}
    if event == "reversed":
        orig = acc.find_by_source(db, "employee_payment", pay.id, "posted")
        if orig is None:
            raise Unmapped("the original payment has not been posted")
        return _d(pay.reversed_at) if pay.reversed_at else date.today(), f"{pay.ref} reversed: {pay.reverse_reason or ''}", acc.mirror_lines(orig)
    return pay.paid_date, f"{pay.ref} payment on employee purchase", [L(account=cash.account_id, debit=amt, location_id=pay.location_id),
                                                                       L(account="1400", credit=amt, location_id=pay.location_id, **emp)]


# ---- queue -------------------------------------------------------------------------------------------------------------------------------------
Item = tuple[str, object, str]       # (kind, source object, event)


def iter_pending(db: Session, up_to: date | None = None) -> Iterator[Item]:
    ok = lambda d: up_to is None or d <= up_to  # noqa: E731
    for t in db.scalars(select(StockTransaction).where(StockTransaction.accounting_status == "pending").order_by(StockTransaction.id)).all():
        if ok(_d(t.created_at)):
            yield "stock_txn", t, "posted"
    for e in db.scalars(select(SupplierLedgerEntry).where(SupplierLedgerEntry.accounting_status == "pending").order_by(SupplierLedgerEntry.id)).all():
        if ok(e.entry_date):
            yield "supplier_ledger", e, "posted"
    for e in db.scalars(select(DistributorLedgerEntry).where(DistributorLedgerEntry.accounting_status == "pending").order_by(DistributorLedgerEntry.id)).all():
        if ok(e.entry_date):
            yield "distributor_ledger", e, "posted"
    policy = _policy(db)
    sale_event = "sale"
    jsrc = JournalEntry.source_type == "shipment"
    done = lambda ev: exists().where(jsrc, JournalEntry.source_id == Shipment.id, JournalEntry.event == ev)  # noqa: E731
    if policy == "delivery":
        q = select(Shipment).where(Shipment.delivered_at.is_not(None), ~done("sale")).order_by(Shipment.id)
    else:
        q = select(Shipment).where(Shipment.dispatched_at.is_not(None), ~done("sale")).order_by(Shipment.id)
    for s in db.scalars(q).all():
        if ok(_d(s.delivered_at if policy == "delivery" else s.dispatched_at)):
            yield "shipment", s, sale_event
    if policy == "delivery":
        for s in db.scalars(select(Shipment).where(Shipment.delivered_at.is_not(None), ~done("cogs")).order_by(Shipment.id)).all():
            if ok(_d(s.delivered_at)):
                yield "shipment", s, "cogs"
    else:
        for s in db.scalars(select(Shipment).where(Shipment.status == "received_back", Shipment.dispatched_at.is_not(None), done("sale"),
                                                   ~done("sale_reversal")).order_by(Shipment.id)).all():
            if ok(_d(s.received_back_at or s.dispatched_at)):
                yield "shipment", s, "sale_reversal"
    for r in db.scalars(select(SalesReturn).where(SalesReturn.status == "settled", ~exists().where(
            JournalEntry.source_type == "sales_return", JournalEntry.source_id == SalesReturn.id, JournalEntry.event == "settled")).order_by(SalesReturn.id)).all():
        if ok(_d(r.settled_at) if r.settled_at else date.today()):
            yield "sales_return", r, "settled"
    ejsrc = JournalEntry.source_type == "employee_purchase"
    edone = lambda ev: exists().where(ejsrc, JournalEntry.source_id == EmployeePurchase.id, JournalEntry.event == ev)  # noqa: E731
    for ep in db.scalars(select(EmployeePurchase).where(EmployeePurchase.completed_at.is_not(None), ~edone("sale")).order_by(EmployeePurchase.id)).all():
        if ok(ep.purchase_date):
            yield "employee_purchase", ep, "sale"
    for ep in db.scalars(select(EmployeePurchase).where(EmployeePurchase.status == "voided", edone("sale"), ~edone("sale_reversal"))
                         .order_by(EmployeePurchase.id)).all():
        if ok(_d(ep.voided_at)):
            yield "employee_purchase", ep, "sale_reversal"
    for ap in db.scalars(select(EmployeePurchasePayment).where((EmployeePurchasePayment.accounting_status == "pending") | (
            (EmployeePurchasePayment.status == "reversed") & (EmployeePurchasePayment.accounting_status != "reversed")))
            .order_by(EmployeePurchasePayment.id)).all():
        if ap.accounting_status == "pending" and ok(ap.paid_date):
            yield "employee_payment", ap, "posted"
        if ap.status == "reversed" and ok(_d(ap.reversed_at) if ap.reversed_at else date.today()):
            yield "employee_payment", ap, "reversed"
    for p in db.scalars(select(OrderPayment).where((OrderPayment.accounting_status == "pending") | (
            (OrderPayment.status == "reversed") & (OrderPayment.accounting_status != "reversed"))).order_by(OrderPayment.id)).all():
        if p.accounting_status == "pending" and ok(p.paid_date):
            yield "order_payment", p, "posted"
        if p.status == "reversed" and ok(_d(p.reversed_at) if p.reversed_at else date.today()):
            yield "order_payment", p, "reversed"


def pending_count(db: Session, up_to: date | None = None) -> int:
    return sum(1 for _ in iter_pending(db, up_to))


def pending_summary(db: Session) -> dict:
    out: dict[str, int] = {}
    for kind, _, _ in iter_pending(db):
        out[kind] = out.get(kind, 0) + 1
    return {"total": sum(out.values()), "by_source": out}


def _build(db: Session, kind: str, obj, event: str):
    if kind == "stock_txn":
        return _stock_txn(db, obj)
    if kind == "supplier_ledger":
        return _supplier_entry(db, obj)
    if kind == "distributor_ledger":
        return _distributor_entry(db, obj)
    if kind == "order_payment":
        return _order_payment(db, obj, event)
    if kind == "sales_return":
        return _sales_return(db, obj)
    if kind == "employee_purchase":
        return _employee_sale(db, obj, event)
    if kind == "employee_payment":
        return _employee_payment(db, obj, event)
    if kind == "shipment":
        o = db.get(Order, obj.order_id)
        if event == "sale":
            when = obj.delivered_at if _policy(db) == "delivery" else obj.dispatched_at
            return _d(when), f"{obj.ref} {'delivered' if _policy(db) == 'delivery' else 'dispatched'} ({o.ref})", _shipment_sale(db, obj, o)
        if event == "sale_reversal":
            orig = acc.find_by_source(db, "shipment", obj.id, "sale")
            return _d(obj.received_back_at or obj.dispatched_at), f"{obj.ref} received back; sale reversed ({o.ref})", acc.mirror_lines(orig)
        if event == "cogs":
            cost = q2(sum((Decimal(sl.stock_value or 0) for sl in obj.lines), Decimal(0)))
            if cost <= 0:
                return SKIP
            return _d(obj.delivered_at), f"{obj.ref} delivered: cost of goods sold", [L(account="5000", debit=cost, location_id=obj.location_id),
                                                                                       L(account="1220", credit=cost, location_id=obj.location_id)]
    raise Unmapped(f"unknown source kind {kind}")


def _mark(kind: str, obj, event: str, state: str) -> None:
    if kind in ("stock_txn", "supplier_ledger", "distributor_ledger"):
        obj.accounting_status = state
    elif kind in ("order_payment", "employee_payment"):
        obj.accounting_status = "reversed" if (event == "reversed") else state


def post_pending(db: Session, *, actor: User, limit: int | None = None) -> dict:
    """Post every pending source. Each one runs in its own savepoint: a failure leaves that source pending and is reported."""
    acc.ensure_chart(db)
    posted = skipped = 0
    errors: list[dict] = []
    for n, (kind, obj, event) in enumerate(list(iter_pending(db))):
        if limit is not None and n >= limit:
            break
        try:
            with db.begin_nested():
                built = _build(db, kind, obj, event)
                if built == SKIP:
                    _mark(kind, obj, event, "skipped")
                    skipped += 1
                    continue
                entry_date, memo, lines = built
                lines = [ln for ln in lines if ln is not None]
                acc.create_entry(db, entry_date=entry_date, memo=memo, lines=lines, kind="system", actor=actor,
                                 source=(_SRC[kind], obj.id, event), shift_if_closed=True)
                _mark(kind, obj, event, "posted")
                posted += 1
        except (Unmapped, BusinessRuleError) as e:
            errors.append({"source": kind, "id": obj.id, "event": event, "ref": getattr(obj, "ref", None), "error": str(e)})
    db.flush()
    return {"posted": posted, "skipped": skipped, "errors": errors, "remaining": pending_count(db)}


_SRC = {"stock_txn": "stock_txn", "supplier_ledger": "supplier_ledger", "distributor_ledger": "distributor_ledger", "shipment": "shipment",
        "sales_return": "sales_return", "order_payment": "order_payment", "employee_purchase": "employee_purchase", "employee_payment": "employee_payment"}
