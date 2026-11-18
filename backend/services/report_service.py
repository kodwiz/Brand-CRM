"""The report catalogue. Every report returns the same payload as the accounting reports:
{"title", "subtitle", "columns": [{key,label,align}], "rows": [...], "totals": {...}, "notes": [...], "reconciliation": [...]}.

* Money figures come from posted journal lines wherever a ledger figure exists, so they reconcile by construction.
* Operational reports (units, orders, stock movement) come from the operational records and, where a ledger figure covers the same thing,
  carry a `reconciliation` block that shows both numbers and the difference. A difference is shown, never hidden.
* A report is available to a user only when they hold every permission it lists, and it is limited to the locations they may see.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

from sqlalchemy import and_, case, func, or_, select
from sqlalchemy.orm import Session

from backend.models import (
    AttendanceDay, AuditLog, Batch, Customer, Distributor, DistributorInvoice, DistributorInvoiceLine, Employee, EmployeePurchase, EmployeePurchaseLine,
    JournalEntry, JournalLine, Location, Order, OrderLine, OrderPayment, Product, ProductVariant, SalesReturn, SalesReturnLine, Shipment, ShipmentLine,
    StockBalance, StockCost, StockMovement, StockTransaction, Transfer, TransferLine, User,
)
from backend.services import accounting_reports as ar
from backend.services import accounting_service as acc
from backend.services import attendance_service as att
from backend.services import settings_service
from backend.services.accounting_service import q2
from backend.services.errors import BusinessRuleError, NotFoundError
from backend.services.scope import visible_location_ids

ZERO = Decimal(0)
col = ar.col
_s = ar._s
POSTED = ("posted", "reversed")


# ---- helpers --------------------------------------------------------------------------------------------------------------------------------
def _report(title: str, columns: list[dict], rows: list[dict], *, subtitle: str = "", totals: dict | None = None, notes: list[str] | None = None,
            reconciliation: list[dict] | None = None) -> dict:
    return {"title": title, "subtitle": subtitle, "columns": columns, "rows": rows, "totals": totals or {}, "notes": notes or [], "reconciliation": reconciliation or []}


def _span(f: date, t: date) -> str:
    return f"{f:%d %b %Y} to {t:%d %b %Y}"


def _allowed(user: User, location_id: int | None) -> set[int] | None:
    """The set of location ids a query may use: None = no restriction."""
    ids = visible_location_ids(user)
    if location_id:
        if ids is not None and location_id not in ids:
            raise NotFoundError("Location not found")
        return {location_id}
    return ids


def _in(column, ids: set[int] | None):
    return column.in_(ids) if ids is not None else True


def _loc_names(db: Session) -> dict[int, str]:
    return {l.id: l.name for l in db.scalars(select(Location)).all()}


def _user_names(db: Session) -> dict[int, str]:
    return {u.id: u.full_name for u in db.scalars(select(User)).all()}


def _day(ts) -> date:
    return ts.date() if isinstance(ts, datetime) else ts


def _recon(label: str, report_value: Decimal, ledger_value: Decimal, note: str = "") -> dict:
    d = q2(report_value) - q2(ledger_value)
    return {"label": label, "report": _s(report_value), "ledger": _s(ledger_value), "difference": _s(d), "agrees": d == 0, "note": note}


def _recognised_shipments(db: Session):
    """Shipments whose sale the ledger recognises under the current policy (dispatch or delivery), with the date it is recognised."""
    policy = settings_service.get(db, acc.REVENUE_KEY)
    if policy == "delivery":
        return Shipment.status == "delivered", Shipment.delivered_at
    return Shipment.status.in_(("dispatched", "delivered")), Shipment.dispatched_at


# ---- ledger-based sales ---------------------------------------------------------------------------------------------------------------------
CHANNEL = {"shipment": "Customer orders", "sales_return": "Customer orders", "distributor_ledger": "Distributors", "employee_purchase": "Staff purchases"}


def sales_by_date_location(db: Session, user: User, *, date_from: date, date_to: date, location_id: int | None = None, group: str = "day") -> dict:
    acc.ensure_chart(db)
    amap = acc.accounts_map(db)
    ids = _allowed(user, location_id)
    codes = {amap[c].id: c for c in ("4000", "4010", "4100", "4200")}
    q = (select(JournalLine.entry_date, JournalLine.location_id, JournalEntry.source_type, JournalLine.account_id, func.sum(JournalLine.credit - JournalLine.debit))
         .join(JournalEntry, JournalEntry.id == JournalLine.entry_id)
         .where(JournalEntry.status.in_(POSTED), JournalLine.account_id.in_(list(codes)), JournalLine.entry_date >= date_from, JournalLine.entry_date <= date_to, _in(JournalLine.location_id, ids))
         .group_by(JournalLine.entry_date, JournalLine.location_id, JournalEntry.source_type, JournalLine.account_id))
    names = _loc_names(db)
    cells: dict[tuple, dict[str, Decimal]] = defaultdict(lambda: defaultdict(lambda: ZERO))
    for d, loc, src, aid, net in db.execute(q).all():
        key = (d.strftime("%Y-%m") if group == "month" else d.isoformat(), names.get(loc, "No location"), CHANNEL.get(src, "Other"))
        cells[key][codes[aid]] += Decimal(net)
    rows, t = [], defaultdict(lambda: ZERO)
    for (period, loc, chan), v in sorted(cells.items()):
        gross, fee, ret, disc = v["4000"], v["4010"], -v["4100"], -v["4200"]
        net = gross + fee - ret - disc
        rows.append({"period": period, "location": loc, "channel": chan, "gross": _s(gross), "discounts": _s(disc), "returns": _s(ret), "delivery": _s(fee), "net": _s(net)})
        for k, x in (("gross", gross), ("discounts", disc), ("returns", ret), ("delivery", fee), ("net", net)):
            t[k] += x
    led = ar.sales_report(db, date_from=date_from, date_to=date_to, location_id=location_id)["totals"] if ids is None or location_id else None
    rec = [_recon("Net sales", t["net"], Decimal(led["net"]), "Sales report from the ledger for the same period")] if led else []
    return _report("Sales by date and location", [col("period", "Month" if group == "month" else "Date"), col("location", "Location"), col("channel", "Channel"),
                                                   col("gross", "Product sales", "right"), col("discounts", "Discounts", "right"), col("returns", "Returns", "right"),
                                                   col("delivery", "Delivery fees", "right"), col("net", "Net sales", "right")], rows, subtitle=_span(date_from, date_to),
                   totals={k: _s(v) for k, v in t.items()}, reconciliation=rec,
                   notes=["Net sales = product sales + delivery fees - returns - discounts, from posted journals. Revenue is recognised at "
                          f"{settings_service.get(db, acc.REVENUE_KEY)}. Records not yet posted do not appear until posted."])


# ---- product performance --------------------------------------------------------------------------------------------------------------------
SALE_TYPES = ("customer_sale", "distributor_dispatch", "employee_purchase")


def product_performance(db: Session, user: User, *, date_from: date, date_to: date, location_id: int | None = None, order: str = "revenue") -> dict:
    ids = _allowed(user, location_id)
    cond, when = _recognised_shipments(db)
    rev: dict[int, Decimal] = defaultdict(lambda: ZERO)
    units: dict[int, int] = defaultdict(int)

    def add(variant_id, qty, amount):
        units[variant_id] += qty
        rev[variant_id] += amount
    for vid, qty, amt in db.execute(select(ShipmentLine.variant_id, ShipmentLine.qty, OrderLine.unit_price * ShipmentLine.qty).join(Shipment, Shipment.id == ShipmentLine.shipment_id)
                                    .join(OrderLine, OrderLine.id == ShipmentLine.order_line_id)
                                    .where(cond, func.date(when) >= date_from, func.date(when) <= date_to, _in(Shipment.location_id, ids))).all():
        add(vid, qty, Decimal(amt))
    for vid, qty, amt in db.execute(select(DistributorInvoiceLine.variant_id, DistributorInvoiceLine.qty, DistributorInvoiceLine.unit_price * DistributorInvoiceLine.qty)
                                    .join(DistributorInvoice, DistributorInvoice.id == DistributorInvoiceLine.invoice_id)
                                    .where(DistributorInvoice.status == "posted", DistributorInvoice.kind.notin_(("opening", "debit_note")),
                                           DistributorInvoice.invoice_date >= date_from, DistributorInvoice.invoice_date <= date_to, _in(DistributorInvoice.location_id, ids))).all():
        add(vid, qty, Decimal(amt))
    for vid, qty, amt in db.execute(select(EmployeePurchaseLine.variant_id, EmployeePurchaseLine.qty, EmployeePurchaseLine.unit_price * EmployeePurchaseLine.qty)
                                    .join(EmployeePurchase, EmployeePurchase.id == EmployeePurchaseLine.purchase_id)
                                    .where(EmployeePurchase.status == "completed", EmployeePurchase.purchase_date >= date_from, EmployeePurchase.purchase_date <= date_to,
                                           _in(EmployeePurchase.location_id, ids))).all():
        add(vid, qty, Decimal(amt))
    ret_units: dict[int, int] = defaultdict(int)
    ret_value: dict[int, Decimal] = defaultdict(lambda: ZERO)
    for vid, rq, uv in db.execute(select(OrderLine.variant_id, SalesReturnLine.refund_qty, OrderLine.unit_price * SalesReturnLine.refund_qty)
                                  .join(SalesReturn, SalesReturn.id == SalesReturnLine.sales_return_id).join(OrderLine, OrderLine.id == SalesReturnLine.order_line_id)
                                  .where(SalesReturn.status == "settled", func.date(SalesReturn.settled_at) >= date_from, func.date(SalesReturn.settled_at) <= date_to,
                                         _in(SalesReturn.location_id, ids))).all():
        ret_units[vid] += rq
        ret_value[vid] += Decimal(uv)
    cost: dict[int, Decimal] = defaultdict(lambda: ZERO)
    q = (select(StockMovement.variant_id, func.sum(StockMovement.value_delta)).join(StockTransaction, StockTransaction.id == StockMovement.txn_id)
         .where(StockTransaction.txn_type.in_(SALE_TYPES + ("return_intake",)), StockMovement.bucket.in_(("good", "damaged", "expired", "inspection")),
                func.date(StockTransaction.created_at) >= date_from, func.date(StockTransaction.created_at) <= date_to, _in(StockMovement.location_id, ids)).group_by(StockMovement.variant_id))
    for vid, v in db.execute(q).all():
        cost[vid] = -Decimal(v)
    vids = set(units) | set(ret_units) | set(cost)
    info = {v.id: v for v in db.scalars(select(ProductVariant).where(ProductVariant.id.in_(vids))).all()} if vids else {}
    rows = []
    for vid in vids:
        v = info[vid]
        net = rev[vid] - ret_value[vid]
        c = q2(cost[vid])
        rows.append({"sku": v.sku, "item": f"{v.product.name} - {v.name}", "category": v.product.category.name, "units": units[vid] - ret_units[vid], "returned": ret_units[vid],
                     "revenue": _s(net), "cost": _s(c), "margin": _s(net - c), "margin_pct": f"{(q2((net - c) * 100 / net) if net else ZERO)}%"})
    rows.sort(key=lambda r: Decimal(r["revenue"]) if order == "revenue" else r["units"], reverse=True)
    tot_net = sum((Decimal(r["revenue"]) for r in rows), ZERO)
    tot_cost = sum((Decimal(r["cost"]) for r in rows), ZERO)
    rec = []
    if ids is None or location_id:
        led = ar.sales_report(db, date_from=date_from, date_to=date_to, location_id=location_id)["totals"]
        rec.append(_recon("Product sales net of discounts and returns (excludes delivery fees)", tot_net, Decimal(led.get("gross", 0)) - Decimal(led.get("discounts", 0)) - Decimal(led.get("returns", 0)),
                          "Ledger: product sales - discounts - returns. A difference usually means records not yet posted, or return timing."))
    return _report("Product performance", [col("sku", "SKU"), col("item", "Item"), col("category", "Category"), col("units", "Units sold (net)", "right"),
                                           col("returned", "Units returned", "right"), col("revenue", "Net revenue", "right"), col("cost", "Cost of goods", "right"),
                                           col("margin", "Gross margin", "right"), col("margin_pct", "Margin %", "right")], rows, subtitle=_span(date_from, date_to),
                   totals={"revenue": _s(tot_net), "cost": _s(tot_cost), "margin": _s(tot_net - tot_cost), "units": sum(r["units"] for r in rows)}, reconciliation=rec,
                   notes=["Revenue is at the price charged, across customer orders, distributors and staff purchases. Cost comes from the stock movements.",
                          "Units sold are net of customer returns that were settled in the period. Distributor returns are not netted here."])


# ---- inventory ------------------------------------------------------------------------------------------------------------------------------
def inventory_movement(db: Session, user: User, *, date_from: date, date_to: date, location_id: int | None = None, q: str | None = None) -> dict:
    ids = _allowed(user, location_id)
    stmt = (select(ProductVariant.sku, ProductVariant.name, Product.name, StockTransaction.txn_type,
                   func.coalesce(func.sum(case((StockMovement.qty_delta > 0, StockMovement.qty_delta), else_=0)), 0),
                   func.coalesce(func.sum(case((StockMovement.qty_delta < 0, -StockMovement.qty_delta), else_=0)), 0), func.coalesce(func.sum(StockMovement.value_delta), 0))
            .join(StockTransaction, StockTransaction.id == StockMovement.txn_id).join(ProductVariant, ProductVariant.id == StockMovement.variant_id)
            .join(Product, Product.id == ProductVariant.product_id)
            .where(StockMovement.bucket != "transit", func.date(StockTransaction.created_at) >= date_from, func.date(StockTransaction.created_at) <= date_to, _in(StockMovement.location_id, ids))
            .group_by(ProductVariant.sku, ProductVariant.name, Product.name, StockTransaction.txn_type).order_by(ProductVariant.sku, StockTransaction.txn_type))
    if q:
        like = f"%{q.strip().lower()}%"
        stmt = stmt.where(func.lower(ProductVariant.sku).like(like) | func.lower(Product.name).like(like))
    rows, tin, tout, tval = [], 0, 0, ZERO
    for sku, vname, pname, typ, qin, qout, val in db.execute(stmt).all():
        rows.append({"sku": sku, "item": f"{pname} - {vname}", "type": typ.replace("_", " ").capitalize(), "qty_in": int(qin), "qty_out": int(qout), "net": int(qin) - int(qout), "value": _s(val)})
        tin, tout, tval = tin + int(qin), tout + int(qout), tval + Decimal(val)
    return _report("Inventory movement", [col("sku", "SKU"), col("item", "Item"), col("type", "Movement"), col("qty_in", "Units in", "right"), col("qty_out", "Units out", "right"),
                                          col("net", "Net units", "right"), col("value", "Value", "right")], rows, subtitle=_span(date_from, date_to),
                   totals={"qty_in": tin, "qty_out": tout, "net": tin - tout, "value": _s(tval)},
                   notes=["Every stock movement that changed a physical bucket, grouped by item and movement type. Stock in transit between locations is not counted until it arrives."])


def stock_valuation(db: Session, user: User, *, location_id: int | None = None) -> dict:
    _allowed(user, location_id)
    ids = visible_location_ids(user)
    if ids is not None and not location_id:
        raise BusinessRuleError("Choose one of your locations: the company-wide stock valuation is for users with access to every location.")
    return ar.inventory_valuation(db, location_id=location_id)


def expiry_damage(db: Session, user: User, *, location_id: int | None = None, days: int = 60) -> dict:
    ids = _allowed(user, location_id)
    today = date.today()
    horizon = today + timedelta(days=days)
    names = _loc_names(db)
    q = (select(StockBalance, ProductVariant, Product, Batch).join(ProductVariant, ProductVariant.id == StockBalance.variant_id).join(Product, Product.id == ProductVariant.product_id)
         .outerjoin(Batch, Batch.id == StockBalance.batch_id).where(_in(StockBalance.location_id, ids),
                                                                    or_(StockBalance.qty_damaged > 0, StockBalance.qty_expired > 0,
                                                                        and_(StockBalance.qty_good > 0, Batch.expiry_date.is_not(None), Batch.expiry_date <= horizon)))
         .order_by(StockBalance.location_id, ProductVariant.sku))
    costs = {(c.location_id, c.variant_id): c for c in db.scalars(select(StockCost)).all()}
    rows, tq, tv = [], ZERO, ZERO
    for b, v, p, bt in db.execute(q).all():
        c = costs.get((b.location_id, b.variant_id))
        avg = (Decimal(c.total_value) / c.valued_qty) if c and c.valued_qty else ZERO
        exp = bt.expiry_date if bt else None
        if b.qty_expired > 0 or (exp and exp < today and b.qty_good > 0):
            state = "Expired"
        elif b.qty_damaged > 0:
            state = "Damaged"
        else:
            state = f"Expires within {days} days"
        at_risk = (b.qty_damaged + b.qty_expired + (b.qty_good if exp and exp <= horizon else 0))
        val = q2(avg * at_risk)
        rows.append({"location": names.get(b.location_id), "sku": v.sku, "item": f"{p.name} - {v.name}", "batch": bt.batch_no if bt else "", "expiry": exp.isoformat() if exp else "",
                     "days_left": (exp - today).days if exp else "", "good": b.qty_good, "damaged": b.qty_damaged, "expired": b.qty_expired, "state": state, "value": _s(val)})
        tq += at_risk
        tv += val
    return _report("Expiry and damage", [col("location", "Location"), col("sku", "SKU"), col("item", "Item"), col("batch", "Batch"), col("expiry", "Expiry"), col("days_left", "Days left", "right"),
                                         col("good", "Good", "right"), col("damaged", "Damaged", "right"), col("expired", "Expired", "right"), col("state", "State"), col("value", "Value at risk", "right")],
                   rows, subtitle=f"As of {today:%d %b %Y}; expiry horizon {days} days", totals={"units_at_risk": int(tq), "value_at_risk": _s(tv)},
                   notes=["Value uses the current weighted-average cost of the item at that location."])


def transfer_reconciliation(db: Session, user: User, *, date_from: date, date_to: date, location_id: int | None = None) -> dict:
    ids = _allowed(user, location_id)
    names = _loc_names(db)
    q = select(Transfer).where(func.date(Transfer.requested_at) >= date_from, func.date(Transfer.requested_at) <= date_to)
    if ids is not None:
        q = q.where(or_(Transfer.from_location_id.in_(ids), Transfer.to_location_id.in_(ids)))
    rows, tot = [], defaultdict(lambda: ZERO)
    for t in db.scalars(q.order_by(Transfer.id)).all():
        ls = db.scalars(select(TransferLine).where(TransferLine.transfer_id == t.id)).all()
        disp, good, dmg, short = (sum(getattr(l, a) for l in ls) for a in ("qty_dispatched", "qty_received_good", "qty_received_damaged", "qty_short"))
        transit = disp - good - dmg - short
        rows.append({"ref": t.ref, "date": t.requested_at.date().isoformat(), "from": names.get(t.from_location_id), "to": names.get(t.to_location_id), "status": t.status.replace("_", " ").capitalize(),
                     "dispatched": disp, "received_good": good, "received_damaged": dmg, "short": short, "in_transit": transit, "value": _s(sum((Decimal(l.dispatch_value) for l in ls), ZERO))})
        for k, v in (("dispatched", disp), ("received_good", good), ("received_damaged", dmg), ("short", short), ("in_transit", transit)):
            tot[k] += v
    open_n = sum(1 for r in rows if r["in_transit"] > 0)
    return _report("Transfer reconciliation", [col("ref", "Transfer"), col("date", "Requested"), col("from", "From"), col("to", "To"), col("status", "Status"), col("dispatched", "Dispatched", "right"),
                                               col("received_good", "Received good", "right"), col("received_damaged", "Received damaged", "right"), col("short", "Short", "right"),
                                               col("in_transit", "Still in transit", "right"), col("value", "Dispatch value", "right")], rows, subtitle=_span(date_from, date_to),
                   totals={**{k: int(v) for k, v in tot.items()}, "transfers_with_goods_in_transit": open_n},
                   notes=["Still in transit = dispatched - received good - received damaged - short. It must be zero once a transfer is complete."])


# ---- customer orders ------------------------------------------------------------------------------------------------------------------------
def customer_orders_returns(db: Session, user: User, *, date_from: date, date_to: date, location_id: int | None = None, status: str | None = None) -> dict:
    ids = _allowed(user, location_id)
    q = (select(Order, Customer.name).join(Customer, Customer.id == Order.customer_id).where(Order.order_date >= date_from, Order.order_date <= date_to, Order.status != "draft",
                                                                                              _in(Order.location_id, ids)))
    if not user.role.scope_all_locations and not any(p.permission == "orders.view_all" for p in user.role.permissions):
        q = q.where(Order.created_by == user.id)
    if status:
        q = q.where(Order.status == status)
    orders = db.execute(q.order_by(Order.id)).all()
    oids = [o.id for o, _ in orders]
    paid = dict(db.execute(select(OrderPayment.order_id, func.sum(case((OrderPayment.kind == "receipt", OrderPayment.amount), else_=-OrderPayment.amount)))
                           .where(OrderPayment.order_id.in_(oids), OrderPayment.status == "posted").group_by(OrderPayment.order_id)).all()) if oids else {}
    rets = dict(db.execute(select(SalesReturn.order_id, func.sum(func.coalesce(SalesReturn.return_value, 0))).where(SalesReturn.order_id.in_(oids), SalesReturn.status.in_(("inspected", "settled")))
                           .group_by(SalesReturn.order_id)).all()) if oids else {}
    users, locs = _user_names(db), _loc_names(db)
    rows, t = [], defaultdict(lambda: ZERO)
    for o, cname in orders:
        total, p, r = Decimal(o.total), Decimal(paid.get(o.id, 0)), Decimal(rets.get(o.id, 0))
        rows.append({"ref": o.ref, "date": o.order_date.isoformat(), "customer": cname, "location": locs.get(o.location_id), "booker": users.get(o.created_by), "status": o.status.replace("_", " ").capitalize(),
                     "terms": o.payment_terms.upper() if o.payment_terms != "credit" else "Credit", "total": _s(total), "paid": _s(p), "returns": _s(r), "balance": _s(total - r - p)})
        t["total"] += total
        t["paid"] += p
        t["returns"] += r
        t["balance"] += total - r - p
    return _report("Customer orders and returns", [col("ref", "Order"), col("date", "Date"), col("customer", "Customer"), col("location", "Location"), col("booker", "Order booker"), col("status", "Status"),
                                                   col("terms", "Terms"), col("total", "Order total", "right"), col("paid", "Received (net of refunds)", "right"), col("returns", "Returns", "right"),
                                                   col("balance", "Balance", "right")], rows, subtitle=_span(date_from, date_to), totals={k: _s(v) for k, v in t.items()},
                   notes=["Balance = order total - returns inspected or settled - money received net of refunds. Orders that were never dispatched still show their full total; cancelled orders are included so nothing is hidden."])


# ---- people ---------------------------------------------------------------------------------------------------------------------------------
def order_booker_performance(db: Session, user: User, *, date_from: date, date_to: date, location_id: int | None = None) -> dict:
    ids = _allowed(user, location_id)
    users = _user_names(db)
    q = select(Order.created_by, Order.status, func.count(), func.coalesce(func.sum(Order.total), 0)).where(Order.order_date >= date_from, Order.order_date <= date_to, Order.status != "draft",
                                                                                                        _in(Order.location_id, ids)).group_by(Order.created_by, Order.status)
    if not user.role.scope_all_locations and not any(p.permission == "orders.view_all" for p in user.role.permissions):
        q = q.where(Order.created_by == user.id)
    per: dict[int, dict] = defaultdict(lambda: defaultdict(lambda: ZERO))
    for uid, st, n, tot in db.execute(q).all():
        d = per[uid]
        d["orders"] += n
        d["value"] += Decimal(tot)
        if st in ("delivered", "partially_delivered", "partially_returned", "returned"):
            d["delivered"] += n
            d["delivered_value"] += Decimal(tot)
        elif st in ("delivery_failed", "refused"):
            d["failed"] += n
        elif st == "cancelled":
            d["cancelled"] += n
    ret = dict(db.execute(select(Order.created_by, func.sum(func.coalesce(SalesReturn.return_value, 0))).join(Order, Order.id == SalesReturn.order_id)
                          .where(SalesReturn.status.in_(("inspected", "settled")), Order.order_date >= date_from, Order.order_date <= date_to).group_by(Order.created_by)).all())
    rows, t = [], defaultdict(lambda: ZERO)
    for uid, d in sorted(per.items(), key=lambda kv: kv[1]["delivered_value"], reverse=True):
        attempted = d["delivered"] + d["failed"]
        rate = q2(d["delivered"] * 100 / attempted) if attempted else None
        r = Decimal(ret.get(uid, 0))
        rows.append({"booker": users.get(uid, f"User {uid}"), "orders": int(d["orders"]), "delivered": int(d["delivered"]), "failed": int(d["failed"]), "cancelled": int(d["cancelled"]),
                     "value": _s(d["value"]), "delivered_value": _s(d["delivered_value"]), "returns": _s(r), "delivery_rate": f"{rate}%" if rate is not None else "-"})
        for k in ("orders", "delivered", "failed", "cancelled", "value", "delivered_value"):
            t[k] += d[k]
        t["returns"] += r
    return _report("Order booker performance", [col("booker", "Order booker"), col("orders", "Orders", "right"), col("delivered", "Delivered", "right"), col("failed", "Failed or refused", "right"),
                                                col("cancelled", "Cancelled", "right"), col("value", "Order value", "right"), col("delivered_value", "Delivered value", "right"),
                                                col("returns", "Returns", "right"), col("delivery_rate", "Delivery success", "right")], rows, subtitle=_span(date_from, date_to),
                   totals={k: (int(v) if k in ("orders", "delivered", "failed", "cancelled") else _s(v)) for k, v in t.items()},
                   notes=["Delivery success = delivered / (delivered + failed or refused). Orders still in progress are counted in Orders but in neither group.",
                          "Order value is the order total including tax and delivery fee; it is an operational measure, not revenue (see Sales by date and location)."])


def employee_performance(db: Session, user: User, *, date_from: date, date_to: date, location_id: int | None = None) -> dict:
    ids = _allowed(user, location_id)
    perms = {p.permission for p in user.role.permissions}
    emps = db.scalars(select(Employee).where(_in(Employee.location_id, ids), Employee.status == "active").order_by(Employee.full_name)).all()
    days = {}
    for eid, st, n, w, lm in db.execute(select(AttendanceDay.employee_id, AttendanceDay.status, func.count(), func.coalesce(func.sum(AttendanceDay.worked_minutes), 0),
                                               func.coalesce(func.sum(AttendanceDay.late_minutes), 0)).where(AttendanceDay.work_date >= date_from, AttendanceDay.work_date <= date_to)
                                        .group_by(AttendanceDay.employee_id, AttendanceDay.status)).all():
        d = days.setdefault(eid, defaultdict(int))
        d[st] += n
        d["worked"] += int(w)
        d["late_min"] += int(lm)
    show_money = "employees.purchase_view" in perms
    spend = dict(db.execute(select(EmployeePurchase.employee_id, func.sum(EmployeePurchase.total)).where(EmployeePurchase.status == "completed", EmployeePurchase.purchase_date >= date_from,
                                                                                                    EmployeePurchase.purchase_date <= date_to).group_by(EmployeePurchase.employee_id)).all()) if show_money else {}
    locs = _loc_names(db)
    rows = []
    for e in emps:
        d = days.get(e.id, defaultdict(int))
        counted = d["present"] + d["late"] + d["short"] + d["incomplete"] + d["no_punch"]
        row = {"code": e.code, "name": e.full_name, "location": locs.get(e.location_id), "days": counted, "present": d["present"], "late": d["late"], "short": d["short"],
               "incomplete": d["incomplete"], "no_punch": d["no_punch"], "worked": f"{d['worked'] // 60}h {d['worked'] % 60:02d}m", "late_min": d["late_min"]}
        if show_money:
            row["purchases"] = _s(Decimal(spend.get(e.id, 0)))
        rows.append(row)
    cols = [col("code", "Code"), col("name", "Employee"), col("location", "Location"), col("days", "Days counted", "right"), col("present", "Present", "right"), col("late", "Late", "right"),
            col("short", "Short", "right"), col("incomplete", "Incomplete", "right"), col("no_punch", "No punches", "right"), col("worked", "Hours worked", "right"), col("late_min", "Minutes late", "right")]
    if show_money:
        cols.append(col("purchases", "Staff purchases", "right"))
    return _report("Employee performance", cols, rows, subtitle=_span(date_from, date_to),
                   totals={"employees": len(rows), "late_days": sum(r["late"] for r in rows), "incomplete_days": sum(r["incomplete"] for r in rows)},
                   notes=["Attendance is shown as recorded: days off for leave or public holidays are not modelled and count as 'No punches'. Sales performance of order bookers is in the Order booker performance report."])


def attendance_report(db: Session, user: User, *, date_from: date, date_to: date, location_id: int | None = None) -> dict:
    _allowed(user, location_id)
    data = att.period_summary(db, user, date_from=date_from, date_to=date_to, location_id=location_id)
    rows = [{"code": d["code"], "name": d["name"], "present": d["days_present"], "late": d["days_late"], "short": d["days_short"], "incomplete": d["days_incomplete"], "no_punch": d["days_no_punch"],
             "worked": f"{d['worked_minutes'] // 60}h {d['worked_minutes'] % 60:02d}m", "late_time": f"{d['late_minutes'] // 60}h {d['late_minutes'] % 60:02d}m",
             "overtime": f"{d['overtime_minutes'] // 60}h {d['overtime_minutes'] % 60:02d}m"} for d in data]
    return _report("Attendance", [col("code", "Code"), col("name", "Employee"), col("present", "Present days", "right"), col("late", "Late days", "right"), col("short", "Short days", "right"),
                                  col("incomplete", "Incomplete", "right"), col("no_punch", "No punches", "right"), col("worked", "Worked", "right"), col("late_time", "Late time", "right"),
                                  col("overtime", "Overtime", "right")], rows, subtitle=_span(date_from, date_to),
                   notes=["From the daily summaries. Weekly-off days are not counted as absent; leave and public holidays are not modelled."])


# ---- discounts ------------------------------------------------------------------------------------------------------------------------------
def discounts_promotions(db: Session, user: User, *, date_from: date, date_to: date, location_id: int | None = None) -> dict:
    ids = _allowed(user, location_id)
    cond, when = _recognised_shipments(db)
    rows, total = [], ZERO
    q = (select(OrderLine.promotion_names, func.count(), func.sum(ShipmentLine.qty), func.sum((OrderLine.list_price - OrderLine.unit_price) * ShipmentLine.qty))
         .join(OrderLine, OrderLine.id == ShipmentLine.order_line_id).join(Shipment, Shipment.id == ShipmentLine.shipment_id)
         .where(cond, func.date(when) >= date_from, func.date(when) <= date_to, _in(Shipment.location_id, ids), OrderLine.list_price > OrderLine.unit_price).group_by(OrderLine.promotion_names))
    for names, n, units, disc in db.execute(q).all():
        rows.append({"source": "Customer orders", "name": names or "No promotion (manual discount or agreed price)", "lines": n, "units": int(units), "discount": _s(disc)})
        total += Decimal(disc)
    q = (select(func.count(), func.coalesce(func.sum(DistributorInvoice.discount_total), 0)).where(DistributorInvoice.status == "posted", DistributorInvoice.kind.notin_(("opening", "debit_note")),
                                                                                                    DistributorInvoice.invoice_date >= date_from, DistributorInvoice.invoice_date <= date_to,
                                                                                                    _in(DistributorInvoice.location_id, ids), DistributorInvoice.discount_total > 0))
    n, disc = db.execute(q).one()
    if n:
        rows.append({"source": "Distributors", "name": "Distributor terms and agreed prices", "lines": n, "units": "", "discount": _s(disc)})
        total += Decimal(disc)
    q = select(func.count(), func.coalesce(func.sum(EmployeePurchase.discount_total), 0)).where(EmployeePurchase.status == "completed", EmployeePurchase.purchase_date >= date_from,
                                                                                               EmployeePurchase.purchase_date <= date_to, _in(EmployeePurchase.location_id, ids), EmployeePurchase.discount_total > 0)
    n, disc = db.execute(q).one()
    if n:
        rows.append({"source": "Staff purchases", "name": "Staff discount rules and staff prices", "lines": n, "units": "", "discount": _s(disc)})
        total += Decimal(disc)
    rows.sort(key=lambda r: Decimal(r["discount"]), reverse=True)
    pending = db.scalar(select(func.count()).select_from(Order).where(Order.discount_approval == "pending", _in(Order.location_id, ids))) or 0
    rec = []
    if ids is None or location_id:
        rec.append(_recon("Discounts given", total, Decimal(ar.sales_report(db, date_from=date_from, date_to=date_to, location_id=location_id)["totals"]["discounts"]),
                          "Ledger discounts (account 4200). Rows are counted per line, so a difference can come from timing of posting."))
    return _report("Discounts and promotions", [col("source", "Source"), col("name", "Promotion or rule"), col("lines", "Lines", "right"), col("units", "Units", "right"), col("discount", "Discount given", "right")],
                   rows, subtitle=_span(date_from, date_to), totals={"discount": _s(total), "orders_waiting_for_discount_approval": pending}, reconciliation=rec,
                   notes=["Discount = list price - price charged, for goods dispatched (or delivered) in the period."])


# ---- audit ----------------------------------------------------------------------------------------------------------------------------------
def audit_history(db: Session, user: User, *, date_from: date, date_to: date, location_id: int | None = None, action: str | None = None, actor: str | None = None,
                  entity_type: str | None = None) -> dict:
    ids = _allowed(user, location_id)
    q = select(AuditLog).where(func.date(AuditLog.created_at) >= date_from, func.date(AuditLog.created_at) <= date_to)
    if ids is not None:
        q = q.where(or_(AuditLog.location_id.in_(ids), AuditLog.location_id.is_(None)))
    for c, v in ((AuditLog.action, action), (AuditLog.actor_username, actor), (AuditLog.entity_type, entity_type)):
        if v:
            q = q.where(func.lower(c).like(f"%{v.strip().lower()}%"))
    total = db.scalar(select(func.count()).select_from(q.subquery())) or 0
    names = _loc_names(db)
    rows = [{"time": a.created_at.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M"), "user": a.actor_username or "", "action": a.action, "entity": f"{a.entity_type} {a.entity_id or ''}".strip(),
             "location": names.get(a.location_id, ""), "reason": a.reason or ""} for a in db.scalars(q.order_by(AuditLog.id.desc()).limit(5000)).all()]
    notes = ["Times are UTC. The before and after values of each change are in the audit log on screen and are not printed here."]
    if total > 5000:
        notes.append(f"Showing the latest 5,000 of {total:,} entries. Narrow the dates or filters to see the rest.")
    return _report("Audit history", [col("time", "Time (UTC)"), col("user", "User"), col("action", "Action"), col("entity", "Record"), col("location", "Location"), col("reason", "Reason")],
                   rows, subtitle=_span(date_from, date_to), totals={"entries": total}, notes=notes)


# ---- wrappers around the accounting reports -----------------------------------------------------------------------------------------------
def _fit(fn, p: dict) -> dict:
    """Pass a report function only the filters it understands (a date range means nothing to a point-in-time balance)."""
    import inspect
    sig = inspect.signature(fn).parameters
    if any(v.kind is inspect.Parameter.VAR_KEYWORD for v in sig.values()):
        return p
    return {k: v for k, v in p.items() if k in sig}


def _fin(fn):
    def run(db, user, **p):
        if visible_location_ids(user) is not None:
            raise BusinessRuleError("Company-wide financial reports are for users with access to every location.")
        return fn(db, **_fit(fn, p))
    return run


def _cogs(db, user, **p):
    _allowed(user, p.get("location_id"))
    return ar.cogs_gross_profit(db, **p)


# ---- the registry --------------------------------------------------------------------------------------------------------------------------
RANGE, ASOF, LOC = "range", "as_of", "location"
REPORTS: dict[str, dict] = {}


def _reg(key: str, title: str, group: str, needs: tuple[str, ...], params: tuple, fn, help: str = "") -> None:
    REPORTS[key] = {"key": key, "title": title, "group": group, "needs": needs, "params": params, "fn": fn, "help": help}


_reg("sales_by_date_location", "Sales by date and location", "Sales", ("reports.summaries",), (RANGE, LOC, "group"), sales_by_date_location, "Net sales from posted journals, by day or month, location and channel.")
_reg("product_performance", "Product performance", "Sales", ("reports.summaries",), (RANGE, LOC, "order"), product_performance, "Units, revenue, cost and margin per item, best sellers first.")
_reg("discounts_promotions", "Discounts and promotions", "Sales", ("reports.summaries",), (RANGE, LOC), discounts_promotions, "What was discounted, through which promotion or rule.")
_reg("customer_orders_returns", "Customer orders and returns", "Sales", ("reports.operational",), (RANGE, LOC, "status"), customer_orders_returns, "Each order with what was received and returned.")
_reg("order_booker_performance", "Order booker performance", "People", ("reports.operational",), (RANGE, LOC), order_booker_performance, "Orders, deliveries and returns per order booker.")
_reg("employee_performance", "Employee performance", "People", ("employees.view", "attendance.view"), (RANGE, LOC), employee_performance, "Attendance and staff purchases per employee.")
_reg("attendance", "Attendance", "People", ("attendance.view",), (RANGE, LOC), attendance_report, "Days present, late and missing punches per employee.")
_reg("inventory_movement", "Inventory movement", "Stock", ("reports.operational", "inventory.view"), (RANGE, LOC, "q"), inventory_movement, "Units in and out by item and type of movement.")
_reg("stock_valuation", "Stock valuation", "Stock", ("reports.financial", "inventory.view_valuation"), (LOC,), stock_valuation, "Stock at weighted-average cost, checked against the ledger.")
_reg("expiry_damage", "Expiry and damage", "Stock", ("reports.operational", "inventory.view"), (LOC, "days"), expiry_damage, "Damaged, expired and soon-to-expire stock and its value.")
_reg("transfer_reconciliation", "Transfer reconciliation", "Stock", ("reports.operational", "transfers.view"), (RANGE, LOC), transfer_reconciliation, "Dispatched, received, short and still in transit.")
_reg("receivables_aging", "Receivables ageing", "Money", ("reports.financial",), (ASOF,), _fin(lambda db, as_of, **_: ar.aging(db, kind="receivables", as_of=as_of)), "Who owes the company and for how long.")
_reg("payables_aging", "Payables ageing and supplier balances", "Money", ("reports.financial",), (ASOF,), _fin(lambda db, as_of, **_: ar.aging(db, kind="payables", as_of=as_of)), "What the company owes each supplier, by age.")
_reg("party_statement", "Customer, distributor, supplier or employee statement", "Money", ("reports.financial",), (RANGE, "party"), _fin(lambda db, date_from, date_to, party_type, party_id, **_: ar.party_statement(db, party_type=party_type, party_id=party_id, date_from=date_from, date_to=date_to)), "Every posted entry for one party with a running balance.")
_reg("expenses", "Expenses", "Money", ("reports.financial",), (RANGE, LOC), lambda db, user, **p: ar.expense_summary(db, **{k: v for k, v in p.items() if k in ("date_from", "date_to", "location_id")}), "Expenses by account for a period.")
_reg("cash_movements", "Cash and bank movements", "Money", ("reports.financial",), (RANGE,), _fin(lambda db, date_from, date_to, **_: ar.cash_flow(db, date_from=date_from, date_to=date_to)), "Direct cash flow for cash and bank accounts.")
_reg("profit_and_loss", "Profit and loss", "Statements", ("reports.financial",), (RANGE, LOC), lambda db, user, **p: ar.income_statement(db, **{k: v for k, v in p.items() if k in ("date_from", "date_to", "location_id")}), "Income statement from posted journals.")
_reg("balance_sheet", "Balance sheet", "Statements", ("reports.financial",), (ASOF,), _fin(lambda db, as_of, **_: ar.balance_sheet(db, as_of=as_of)), "Assets, liabilities and equity as of a date.")
_reg("trial_balance", "Trial balance", "Statements", ("reports.financial",), (ASOF, LOC), lambda db, user, as_of, location_id=None, **_: ar.trial_balance(db, as_of=as_of, location_id=location_id), "Every account's balance; debits must equal credits.")
_reg("audit_history", "Audit history", "Control", ("audit.view",), (RANGE, LOC, "action", "actor", "entity_type"), audit_history, "Who did what and when, with the reason given.")


def available(user: User) -> list[dict]:
    have = {p.permission for p in user.role.permissions}
    return [{k: v for k, v in r.items() if k != "fn"} for r in REPORTS.values() if all(n in have for n in r["needs"])]


def run(db: Session, user: User, key: str, params: dict) -> dict:
    r = REPORTS.get(key)
    have = {p.permission for p in user.role.permissions}
    if r is None or not all(n in have for n in r["needs"]):
        raise NotFoundError("Report not found")        # not available to this user: indistinguishable from a report that does not exist
    today = date.today()
    p = dict(params)
    p["date_to"] = p.get("date_to") or today
    p["date_from"] = p.get("date_from") or date(p["date_to"].year, p["date_to"].month, 1)
    p["as_of"] = p.get("as_of") or today
    if p["date_to"] < p["date_from"]:
        raise BusinessRuleError("The end date is before the start date.")
    if (p["date_to"] - p["date_from"]).days > 800:
        raise BusinessRuleError("Choose a period of at most about two years.")
    if key == "party_statement" and not (p.get("party_type") and p.get("party_id")):
        raise BusinessRuleError("Choose whose statement you want.")
    accepted = {"date_from", "date_to", "as_of", "location_id", "group", "order", "status", "q", "days", "party_type", "party_id", "action", "actor", "entity_type"}
    kw = {k: v for k, v in p.items() if k in accepted and v is not None}
    if "location_id" in kw:
        _allowed(user, kw["location_id"])
    rep = r["fn"](db, user, **_fit(r["fn"], kw))
    rep.setdefault("reconciliation", [])
    rep["key"] = key
    rep["generated_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    rep["generated_by"] = user.full_name
    return rep
