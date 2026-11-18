"""Role-specific dashboards with date and location filters.

The view a user gets follows their permissions, never a role name, and every section is shown only if the user holds the permission for its figures:
  management  (reports.operational + reports.summaries): CEO and manager. The CEO also sees ledger balances (reports.financial).
  accounts    (reports.financial without reports.operational): the financial dashboard.
  operations  (inventory and fulfilment work): the stock keeper's assigned locations.
  booker      (anyone else with orders.view_own): their own orders and performance.
Money figures come from posted journals and carry a group label so revenue, cash collected, receivables, stock value and profit are never mixed up."""
from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from backend.models import (
    AttendanceDay, AttendancePunch, CashAccount, Distributor, DistributorPayment, Order, OrderPayment, ProductVariant, SalesReturn, StockBalance, StockChangeRequest, StockCost, StockReceipt,
    Transfer, User,
)
from backend.services import accounting_ops_service as ops
from backend.services import accounting_posting as posting
from backend.services import accounting_reports as ar
from backend.services import accounting_service as acc
from backend.services import approvals_service, report_service as rs
from backend.services.accounting_service import q2
from backend.services.scope import visible_location_ids

ZERO = Decimal(0)
DELAYED_DAYS = 2            # an order confirmed but not dispatched for longer than this is shown as delayed
TRANSIT_DAYS = 3            # a transfer on the road for longer than this is shown as slow
GROUPS = {"revenue": "Revenue", "profit": "Profit", "cash": "Cash collected", "owed": "Receivables and payables", "stock": "Stock value", "balances": "Cash and bank balances",
          "volume": "Activity", "quality": "Stock quality", "people": "People"}


def _m(key: str, label: str, value, group: str, unit: str = "money", hint: str = "") -> dict:
    return {"key": key, "label": label, "value": str(q2(value)) if unit == "money" else value, "unit": unit, "group": group, "group_label": GROUPS[group], "hint": hint}


def _table(columns: list[tuple], rows: list[dict]) -> dict:
    return {"columns": [{"key": k, "label": l, "align": a} for k, l, a in columns], "rows": rows}


def _section(key: str, title: str, *, metrics: list[dict] | None = None, table: dict | None = None, note: str = "") -> dict:
    return {"key": key, "title": title, "metrics": metrics or [], "table": table, "note": note}


def _have(user: User) -> set[str]:
    return {p.permission for p in user.role.permissions}


def pick_view(have: set[str]) -> str:
    if "reports.operational" in have and "reports.summaries" in have:
        return "management"
    if "reports.financial" in have and "reports.operational" not in have:
        return "accounts"
    if have & {"inventory.receive", "transfers.dispatch", "transfers.receive", "orders.fulfil", "inventory.count"}:
        return "operations"
    return "booker"


def _scope(user: User, location_id: int | None) -> set[int] | None:
    return rs._allowed(user, location_id)


def _in(col, ids):
    return col.in_(ids) if ids is not None else True


# ---- shared building blocks ----------------------------------------------------------------------------------------------------------------
def _money_sections(db: Session, user: User, have: set[str], f: date, t: date, loc: int | None, ids) -> list[dict]:
    out = []
    unrestricted = ids is None or loc is not None
    if "reports.summaries" in have and unrestricted:
        sales = ar.sales_report(db, date_from=f, date_to=t, location_id=loc)["totals"] or {}
        ms = [_m("net_sales", "Net sales", Decimal(sales.get("net", 0)), "revenue", hint="Product sales + delivery fees - returns - discounts"),
              _m("returns", "Returns", Decimal(sales.get("returns", 0)), "revenue"), _m("discounts", "Discounts", Decimal(sales.get("discounts", 0)), "revenue")]
        note = "From posted journals. Revenue is what was sold; it is not cash."
        if "reports.financial" in have:                 # profit is a financial statement: managers see sales, not profit
            pl = ar.income_statement(db, date_from=f, date_to=t, location_id=loc)["totals"]
            ms += [_m("gross_profit", "Gross profit", Decimal(pl["gross_profit"]), "profit", hint=f"Margin {pl['gross_margin']}"),
                   _m("expenses", "Operating expenses", Decimal(pl["operating_expenses"]), "profit"), _m("net_profit", "Net profit", Decimal(pl["net_profit"]), "profit")]
            note = "From posted journals. Revenue is what was sold; profit is what is left after cost and expenses; neither is cash."
        out.append(_section("revenue_profit", "Revenue and profit" if "reports.financial" in have else "Revenue", note=note, metrics=ms))
    return out


def _cash_collected(db: Session, f: date, t: date, ids) -> list[dict]:
    """Money actually received in the period, by source. It is a different thing from sales and from receivables."""
    cust = db.scalar(select(func.coalesce(func.sum(OrderPayment.amount), 0)).join(Order, Order.id == OrderPayment.order_id)
                     .where(OrderPayment.kind == "receipt", OrderPayment.status == "posted", OrderPayment.paid_date >= f, OrderPayment.paid_date <= t, _in(Order.location_id, ids))) or 0
    ref = db.scalar(select(func.coalesce(func.sum(OrderPayment.amount), 0)).join(Order, Order.id == OrderPayment.order_id)
                    .where(OrderPayment.kind == "refund", OrderPayment.status == "posted", OrderPayment.paid_date >= f, OrderPayment.paid_date <= t, _in(Order.location_id, ids))) or 0
    dist = db.scalar(select(func.coalesce(func.sum(DistributorPayment.amount), 0)).where(DistributorPayment.status == "posted", DistributorPayment.paid_date >= f, DistributorPayment.paid_date <= t)) or 0 \
        if ids is None else 0
    ms = [_m("cust_receipts", "Received from customers", Decimal(cust), "cash"), _m("cust_refunds", "Refunded to customers", Decimal(ref), "cash")]
    if ids is None:
        ms.append(_m("dist_receipts", "Received from distributors", Decimal(dist), "cash"))
    return ms


def _owed(db: Session, have: set[str], t: date) -> list[dict]:
    ms = []
    rec = ar.aging(db, kind="receivables", as_of=t)
    pay = ar.aging(db, kind="payables", as_of=t)
    by_type = defaultdict(lambda: ZERO)
    for r in rec["rows"]:
        by_type[r["party_type"]] += Decimal(r["total"] or 0)
    ms += [_m("recv_customers", "Customers owe", by_type["customer"], "owed"), _m("recv_distributors", "Distributors owe", by_type["distributor"], "owed"),
           _m("recv_employees", "Employees owe", by_type["employee"], "owed"), _m("payables", "Owed to suppliers", Decimal(pay["totals"].get("total", 0)), "owed")]
    return ms


def _overdue_distributors(db: Session, t: date) -> tuple[list[dict], Decimal]:
    rec = ar.aging(db, kind="receivables", as_of=t)
    terms = {d.id: d.payment_terms_days for d in db.scalars(select(Distributor)).all()}
    bounds = {lbl: lo for lo, _hi, lbl in ar.BUCKETS}
    rows, total = [], ZERO
    for r in rec["rows"]:
        if r["party_type"] != "distributor":
            continue
        days = terms.get(r["party_id"], 30)
        over = sum((Decimal(r.get(lbl) or 0) for lbl, lo in bounds.items() if lo > days), ZERO)
        if over > 0:
            rows.append({"party": r["party"], "owed": r["total"], "overdue": str(q2(over)), "terms": f"{days} days"})
            total += over
    rows.sort(key=lambda x: -Decimal(x["overdue"]))
    return rows, total


def _stock_quality(db: Session, ids) -> tuple[int, int, Decimal]:
    dmg, exp = db.execute(select(func.coalesce(func.sum(StockBalance.qty_damaged), 0), func.coalesce(func.sum(StockBalance.qty_expired), 0)).where(_in(StockBalance.location_id, ids))).one()
    val = ZERO
    costs = {(c.location_id, c.variant_id): c for c in db.scalars(select(StockCost)).all()}
    for b in db.scalars(select(StockBalance).where(_in(StockBalance.location_id, ids), or_(StockBalance.qty_damaged > 0, StockBalance.qty_expired > 0))).all():
        c = costs.get((b.location_id, b.variant_id))
        if c and c.valued_qty:
            val += Decimal(c.total_value) / c.valued_qty * (b.qty_damaged + b.qty_expired)
    return int(dmg), int(exp), q2(val)


def _low_stock(db: Session, ids) -> list[dict]:
    """Items whose good stock across the visible locations is at or below the item's reorder threshold."""
    q = (select(ProductVariant.sku, ProductVariant.name, ProductVariant.reorder_threshold, func.coalesce(func.sum(StockBalance.qty_good - StockBalance.qty_reserved), 0))
         .outerjoin(StockBalance, (StockBalance.variant_id == ProductVariant.id) & (_in(StockBalance.location_id, ids) if ids is not None else True))
         .where(ProductVariant.reorder_threshold > 0, ProductVariant.status == "active").group_by(ProductVariant.id).order_by(ProductVariant.sku))
    return [{"sku": sku, "item": name, "available": int(avail), "threshold": int(th), "short_by": int(th) - int(avail)} for sku, name, th, avail in db.execute(q).all() if int(avail) <= int(th)]


def _delayed_orders(db: Session, user: User, ids, have: set[str]) -> list[dict]:
    cut = datetime.now(timezone.utc) - timedelta(days=DELAYED_DAYS)
    q = select(Order).where(Order.status.in_(("confirmed", "packed")), Order.confirmed_at.is_not(None), Order.confirmed_at < cut, _in(Order.fulfilment_location_id, ids))
    if "orders.view_all" not in have:
        q = q.where(Order.created_by == user.id)
    return [{"ref": o.ref, "status": o.status.capitalize(), "since": o.confirmed_at.date().isoformat(), "days": (datetime.now(timezone.utc) - o.confirmed_at).days, "total": str(q2(o.total))}
            for o in db.scalars(q.order_by(Order.confirmed_at)).all()]


def _slow_transfers(db: Session, ids) -> list[dict]:
    cut = datetime.now(timezone.utc) - timedelta(days=TRANSIT_DAYS)
    q = select(Transfer).where(Transfer.status.in_(("dispatched", "partially_received")), Transfer.dispatched_at.is_not(None), Transfer.dispatched_at < cut)
    if ids is not None:
        q = q.where(or_(Transfer.from_location_id.in_(ids), Transfer.to_location_id.in_(ids)))
    names = rs._loc_names(db)
    return [{"ref": t.ref, "from": names.get(t.from_location_id), "to": names.get(t.to_location_id), "days": (datetime.now(timezone.utc) - t.dispatched_at).days} for t in db.scalars(q).all()]


def _attendance_today(db: Session, ids) -> dict:
    last = db.scalar(select(func.max(AttendanceDay.work_date)).where(AttendanceDay.status.in_(("present", "late", "short", "incomplete")), _in(AttendanceDay.location_id, ids)))
    if last is None:
        return {"date": None, "counts": {}}
    rows = db.execute(select(AttendanceDay.status, func.count()).where(AttendanceDay.work_date == last, _in(AttendanceDay.location_id, ids)).group_by(AttendanceDay.status)).all()
    return {"date": last, "counts": dict(rows)}


def _alerts(db: Session, user: User, have: set[str], ids, t: date, view: str) -> list[dict]:
    out: list[dict] = []

    def add(sev: str, text: str, screen: str, n: int | None = None):
        out.append({"severity": sev, "text": text, "screen": screen, "count": n})
    if have & {"inventory.view"} and view != "booker":
        low = _low_stock(db, ids)
        if low:
            add("warn", f"{len(low)} item(s) at or below their reorder level", "inventory", len(low))
        dmg, exp, _v = _stock_quality(db, ids)
        if exp:
            add("error", f"{exp} unit(s) of expired stock still on the books", "inventory", exp)
        if dmg:
            add("warn", f"{dmg} damaged unit(s) waiting to be written off or returned", "inventory", dmg)
    if "orders.view_all" in have or view == "booker":
        late = _delayed_orders(db, user, ids, have)
        if late:
            add("warn", f"{len(late)} order(s) confirmed more than {DELAYED_DAYS} days ago and not yet dispatched", "orders", len(late))
    if "transfers.view" in have:
        slow = _slow_transfers(db, ids)
        if slow:
            add("warn", f"{len(slow)} transfer(s) in transit for more than {TRANSIT_DAYS} days", "transfers", len(slow))
    if "reports.financial" in have and ids is None:
        rows, total = _overdue_distributors(db, t)
        if rows:
            add("error", f"{len(rows)} distributor(s) are past their payment terms ({q2(total):,} overdue)", "distributors", len(rows))
        pend = posting.pending_count(db)
        if pend:
            add("info", f"{pend} record(s) are waiting to be posted to the ledger", "accounting", pend)
        sub = ops.subledger_reconciliation(db)
        bad = [r["name"] for r in sub["rows"] if not r["agrees"]]
        if bad:
            add("error", "The ledger does not agree with: " + ", ".join(bad), "accounting", len(bad))
    if "attendance.view" in have:
        unm = db.scalar(select(func.count(func.distinct(AttendancePunch.device_user_id))).where(AttendancePunch.status == "unmatched", _in(AttendancePunch.location_id, ids) if ids is not None else True)) or 0
        if unm:
            add("warn", f"{unm} device number(s) have punches but no matching employee", "attendance", unm)
        inc = db.scalar(select(func.count()).select_from(AttendanceDay).where(AttendanceDay.exception.is_not(None), AttendanceDay.corrected.is_(False), AttendanceDay.work_date >= t - timedelta(days=7),
                                                                              _in(AttendanceDay.location_id, ids))) or 0
        if inc:
            add("warn", f"{inc} attendance day(s) in the last 7 days have a missing or odd punch", "attendance", inc)
    ap = approvals_service.counts(db, user)
    if ap["total"]:
        add("warn" if ap["older_than_3_days"] else "info", f"{ap['total']} request(s) waiting for approval" + (f", {ap['older_than_3_days']} older than 3 days" if ap["older_than_3_days"] else ""), "approvals", ap["total"])
    return out


# ---- the views ------------------------------------------------------------------------------------------------------------------------------
def management(db, user, have, f, t, loc, ids) -> list[dict]:
    secs = _money_sections(db, user, have, f, t, loc, ids)
    secs.append(_section("cash_collected", "Cash collected", note="Money received in the period. It is not revenue (what was sold) and not receivables (what is still owed).", metrics=_cash_collected(db, f, t, ids)))
    if "reports.financial" in have and ids is None:
        secs.append(_section("owed", "Receivables and payables", note=f"As of {t:%d %b %Y}, from the ledger.", metrics=_owed(db, have, t)))
        iv = ar.inventory_valuation(db)["totals"]
        cash = [ops.cash_account_view(db, c) for c in db.scalars(select(CashAccount).where(CashAccount.is_active.is_(True))).all()]
        secs.append(_section("stock_cash", "Stock value and cash balances", note="Stock at weighted-average cost; balances are what the ledger holds for each cash and bank account.",
                             metrics=[_m("stock_value", "Stock value (ledger)", Decimal(iv.get("general_ledger_value", iv["stock_ledger_value"])), "stock", hint="Agrees with the stock sub-ledger" if iv.get("agrees", True) else "Does NOT agree with the stock sub-ledger")]
                             + [_m(f"cash_{c['id']}", c["name"], Decimal(c["balance"]), "balances") for c in cash]))
    sales = rs.sales_by_date_location(db, user, date_from=f, date_to=t, location_id=loc, group="month") if "reports.summaries" in have else None
    if sales:
        by_loc, by_chan = defaultdict(lambda: ZERO), defaultdict(lambda: ZERO)
        for r in sales["rows"]:
            by_loc[r["location"]] += Decimal(r["net"])
            by_chan[r["channel"]] += Decimal(r["net"])
        secs.append(_section("sales_location", "Net sales by location", table=_table([("location", "Location", "left"), ("net", "Net sales", "right")],
                                                                                    [{"location": k, "net": str(q2(v))} for k, v in sorted(by_loc.items(), key=lambda kv: -kv[1])])))
        secs.append(_section("sales_channel", "Net sales by channel", table=_table([("channel", "Channel", "left"), ("net", "Net sales", "right")],
                                                                                   [{"channel": k, "net": str(q2(v))} for k, v in sorted(by_chan.items(), key=lambda kv: -kv[1])])))
        pp = rs.product_performance(db, user, date_from=f, date_to=t, location_id=loc)["rows"]
        secs.append(_section("best_products", "Best-selling products", table=_table([("item", "Item", "left"), ("units", "Units", "right"), ("revenue", "Net revenue", "right")], pp[:5])))
        sold = [r for r in pp if r["units"] > 0]
        secs.append(_section("worst_products", "Slowest-selling products", note="Items that sold at least once in the period, lowest first.", table=_table([("item", "Item", "left"), ("units", "Units", "right"), ("revenue", "Net revenue", "right")], list(reversed(sold))[:5])))
    dmg, exp, val = _stock_quality(db, ids)
    secs.append(_section("quality", "Damaged and expired stock", metrics=[_m("dmg", "Damaged units", dmg, "quality", "count"), _m("exp", "Expired units", exp, "quality", "count"), _m("risk_value", "Value of both (at cost)", val, "quality")]))
    ret = db.execute(select(func.count(), func.coalesce(func.sum(SalesReturn.return_value), 0)).where(SalesReturn.status.in_(("inspected", "settled")), func.date(SalesReturn.requested_at) >= f,
                                                                                                         func.date(SalesReturn.requested_at) <= t, _in(SalesReturn.location_id, ids))).one()
    secs.append(_section("returns", "Returns and refunds", metrics=[_m("ret_n", "Customer returns accepted", int(ret[0]), "volume", "count"), _m("ret_v", "Value of those returns", Decimal(ret[1]), "volume")]))
    if "reports.financial" in have and ids is None:
        rows, total = _overdue_distributors(db, t)
        secs.append(_section("dist_overdue", "Distributor overdue balances", note="Balances older than each distributor's payment terms.", table=_table([("party", "Distributor", "left"), ("owed", "Owes", "right"), ("overdue", "Overdue", "right"), ("terms", "Terms", "left")], rows)))
    if "reports.operational" in have:
        ob = rs.order_booker_performance(db, user, date_from=f, date_to=t, location_id=loc)["rows"]
        secs.append(_section("bookers", "Order booker performance", table=_table([("booker", "Order booker", "left"), ("orders", "Orders", "right"), ("delivered_value", "Delivered value", "right"), ("delivery_rate", "Delivery success", "right")], ob[:8])))
        secs.append(_section("shortages", "Stock shortages", note="Items at or below their reorder level (available = good stock - reserved).", table=_table([("sku", "SKU", "left"), ("item", "Item", "left"), ("available", "Available", "right"), ("threshold", "Reorder level", "right")], _low_stock(db, ids)[:10])))
        secs.append(_section("delayed", "Delayed orders", note=f"Confirmed more than {DELAYED_DAYS} days ago and not dispatched.", table=_table([("ref", "Order", "left"), ("status", "Status", "left"), ("since", "Since", "left"), ("days", "Days", "right"), ("total", "Total", "right")], _delayed_orders(db, user, ids, have)[:10])))
        pend_t = db.scalar(select(func.count()).select_from(Transfer).where(Transfer.status == "requested", or_(_in(Transfer.from_location_id, ids), _in(Transfer.to_location_id, ids)))) or 0
        pend_a = db.scalar(select(func.count()).select_from(StockChangeRequest).where(StockChangeRequest.status == "pending", _in(StockChangeRequest.location_id, ids))) or 0
        secs.append(_section("tasks", "Daily tasks", metrics=[_m("t_req", "Transfer requests waiting", pend_t, "volume", "count"), _m("t_adj", "Stock adjustments waiting", pend_a, "volume", "count"),
                                                              _m("t_slow", "Slow transfers", len(_slow_transfers(db, ids)), "volume", "count")]))
    if "attendance.view" in have:
        a = _attendance_today(db, ids)
        c = a["counts"]
        secs.append(_section("attendance", "Employee attendance", note=(f"Latest day with punches: {a['date']:%d %b %Y}" if a["date"] else "No attendance imported yet."), metrics=[
            _m("a_pres", "Present", c.get("present", 0), "people", "count"), _m("a_late", "Late", c.get("late", 0), "people", "count"), _m("a_short", "Short day", c.get("short", 0), "people", "count"),
            _m("a_inc", "Incomplete", c.get("incomplete", 0), "people", "count"), _m("a_none", "No punches", c.get("no_punch", 0), "people", "count")]))
    return secs


def accounts_view(db, user, have, f, t, loc, ids) -> list[dict]:
    secs = _money_sections(db, user, have, f, t, loc, ids)
    secs.append(_section("cash_collected", "Cash collected", note="Money received in the period; not revenue and not receivables.", metrics=_cash_collected(db, f, t, ids)))
    if ids is None:
        secs.append(_section("owed", "Receivables and payables", note=f"As of {t:%d %b %Y}, from the ledger.", metrics=_owed(db, have, t)))
        cash = [ops.cash_account_view(db, c) for c in db.scalars(select(CashAccount).where(CashAccount.is_active.is_(True))).all()]
        secs.append(_section("cash", "Cash and bank balances", table=_table([("name", "Account", "left"), ("balance", "Ledger balance", "right"), ("unreconciled_lines", "Unreconciled lines", "right"), ("last_reconciled_to", "Reconciled to", "left")],
                                                                              [{**c, "last_reconciled_to": str(c["last_reconciled_to"] or "never")} for c in cash])))
        iv = ar.inventory_valuation(db)["totals"]
        secs.append(_section("stock", "Stock value", metrics=[_m("stock_value", "Stock value (ledger)", Decimal(iv["general_ledger_value"]), "stock", hint="Agrees with the stock sub-ledger" if iv.get("agrees") else "Does NOT agree with the stock sub-ledger")]))
        rows, total = _overdue_distributors(db, t)
        secs.append(_section("dist_overdue", "Distributor overdue balances", table=_table([("party", "Distributor", "left"), ("owed", "Owes", "right"), ("overdue", "Overdue", "right"), ("terms", "Terms", "left")], rows)))
        sub = ops.subledger_reconciliation(db)
        secs.append(_section("integrity", "Does the ledger agree with the records?", table=_table([("name", "Check", "left"), ("difference", "Difference", "right"), ("agrees", "Agrees", "left")], [{**r, "agrees": "Yes" if r["agrees"] else "NO"} for r in sub["rows"]])))
    return secs


def operations_view(db, user, have, f, t, loc, ids) -> list[dict]:
    secs = []
    rec = db.scalar(select(func.count()).select_from(StockReceipt).where(StockReceipt.status == "posted", func.date(StockReceipt.created_at) >= f, func.date(StockReceipt.created_at) <= t, _in(StockReceipt.location_id, ids))) or 0
    to_dispatch = db.scalar(select(func.count()).select_from(Transfer).where(Transfer.status == "approved", _in(Transfer.from_location_id, ids))) or 0
    to_receive = db.scalar(select(func.count()).select_from(Transfer).where(Transfer.status.in_(("dispatched", "partially_received")), _in(Transfer.to_location_id, ids))) or 0
    pend_adj = db.scalar(select(func.count()).select_from(StockChangeRequest).where(StockChangeRequest.status == "pending", _in(StockChangeRequest.location_id, ids))) or 0
    to_ship = db.scalar(select(func.count()).select_from(Order).where(Order.status.in_(("confirmed", "packed")), _in(Order.fulfilment_location_id, ids))) or 0
    secs.append(_section("tasks", "Work waiting at your locations", metrics=[_m("receipts", "Goods receipts posted", rec, "volume", "count"), _m("to_pack", "Orders to pack or dispatch", to_ship, "volume", "count"),
                                                                           _m("to_dispatch", "Transfers to dispatch", to_dispatch, "volume", "count"), _m("to_receive", "Transfers to receive", to_receive, "volume", "count"),
                                                                           _m("pend_adj", "Adjustments waiting for approval", pend_adj, "volume", "count")]))
    secs.append(_section("shortages", "Stock shortages", table=_table([("sku", "SKU", "left"), ("item", "Item", "left"), ("available", "Available", "right"), ("threshold", "Reorder level", "right")], _low_stock(db, ids)[:10])))
    dmg, exp, val = _stock_quality(db, ids)
    secs.append(_section("quality", "Damaged and expired stock", metrics=[_m("dmg", "Damaged units", dmg, "quality", "count"), _m("exp", "Expired units", exp, "quality", "count")]))
    secs.append(_section("delayed", "Delayed orders", table=_table([("ref", "Order", "left"), ("status", "Status", "left"), ("since", "Since", "left"), ("days", "Days", "right")], _delayed_orders(db, user, ids, have)[:10])))
    if "distributors.view" in have:
        pays = db.scalar(select(func.coalesce(func.sum(DistributorPayment.amount), 0)).where(DistributorPayment.status == "posted", DistributorPayment.paid_date >= f, DistributorPayment.paid_date <= t,
                                                                                         DistributorPayment.created_by == user.id)) or 0
        secs.append(_section("dist", "Distributors", note="Payments you recorded in the period. Distributor balances are in the Distributors screen.", metrics=[_m("pays", "Payments you recorded", Decimal(pays), "cash")]))
    return secs


def booker_view(db, user, have, f, t, loc, ids) -> list[dict]:
    q = select(Order.status, func.count(), func.coalesce(func.sum(Order.total), 0)).where(Order.created_by == user.id, Order.order_date >= f, Order.order_date <= t, Order.status != "draft").group_by(Order.status)
    per = {s: (n, Decimal(v)) for s, n, v in db.execute(q).all()}
    n_all = sum(n for n, _ in per.values())
    delivered = sum(per.get(s, (0, ZERO))[0] for s in ("delivered", "partially_delivered", "partially_returned", "returned"))
    failed = sum(per.get(s, (0, ZERO))[0] for s in ("delivery_failed", "refused"))
    secs = [_section("my_orders", "My orders", metrics=[_m("n", "Orders taken", n_all, "volume", "count"), _m("val", "Value of my orders", sum((v for _, v in per.values()), ZERO), "volume", hint="Order totals; not revenue until dispatched"),
                                                         _m("deliv", "Delivered", delivered, "volume", "count"), _m("fail", "Failed or refused", failed, "volume", "count"),
                                                         _m("canc", "Cancelled", per.get("cancelled", (0, ZERO))[0], "volume", "count")]),
            _section("by_status", "My orders by status", table=_table([("status", "Status", "left"), ("n", "Orders", "right"), ("value", "Value", "right")],
                                                                      [{"status": s.replace("_", " ").capitalize(), "n": n, "value": str(q2(v))} for s, (n, v) in sorted(per.items(), key=lambda kv: -kv[1][0])]))]
    perf = rs.order_booker_performance(db, user, date_from=f, date_to=t, location_id=loc)["rows"]
    if perf:
        p = perf[0]
        secs.append(_section("perf", "My performance", metrics=[_m("dv", "Delivered value", Decimal(p["delivered_value"]), "volume"), _m("rate", "Delivery success", p["delivery_rate"], "volume", "text"), _m("ret", "Returns on my orders", Decimal(p["returns"]), "volume")]))
    late = _delayed_orders(db, user, ids, have)
    secs.append(_section("delayed", "My delayed orders", table=_table([("ref", "Order", "left"), ("status", "Status", "left"), ("since", "Since", "left"), ("days", "Days", "right")], late[:10])))
    return secs


VIEWS = {"management": ("Company overview", management), "accounts": ("Financial dashboard", accounts_view), "operations": ("Operations dashboard", operations_view), "booker": ("My dashboard", booker_view)}


def build(db: Session, user: User, *, date_from: date | None, date_to: date | None, location_id: int | None) -> dict:
    t = date_to or date.today()
    f = date_from or date(t.year, t.month, 1)
    acc.ensure_chart(db)
    have = _have(user)
    ids = _scope(user, location_id)
    view = pick_view(have)
    title, fn = VIEWS[view]
    names = rs._loc_names(db)
    scope = names.get(location_id) if location_id else ("All locations" if visible_location_ids(user) is None else "All my locations")
    return {"view": view, "title": title, "period": {"from": f.isoformat(), "to": t.isoformat()}, "scope": scope, "sections": fn(db, user, have, f, t, location_id, ids),
            "alerts": _alerts(db, user, have, ids, t, view), "approvals": approvals_service.counts(db, user) if "approvals.view" in have else None}
