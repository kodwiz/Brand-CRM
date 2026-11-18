"""Financial reports. Every figure is computed from posted journal lines (status posted or reversed); nothing is read from operational tables except the
inventory-valuation and reconciliation reports, which deliberately compare the ledger with the stock sub-ledger.

Report payload: {"title", "subtitle", "columns": [{key,label,align}], "rows": [...], "totals": {...}, "notes": [...]}."""
from __future__ import annotations

import calendar
from collections import defaultdict
from datetime import date
from decimal import Decimal

from sqlalchemy import case, func, select
from sqlalchemy.orm import Session

from backend.models import (
    Account, CashAccount, Customer, Distributor, Employee, JournalEntry, JournalLine, Location, ProductVariant, StockCost, Supplier,
)
from backend.services import accounting_service as acc
from backend.services import settings_service
from backend.services.accounting_service import q2
from backend.services.errors import BusinessRuleError, NotFoundError

ZERO = Decimal(0)
POSTED = ("posted", "reversed")


def _s(x) -> str:
    return str(q2(x))


def col(key: str, label: str, align: str = "left") -> dict:
    return {"key": key, "label": label, "align": align}


def _report(title: str, columns: list[dict], rows: list[dict], *, subtitle: str = "", totals: dict | None = None, notes: list[str] | None = None, **extra) -> dict:
    status = None
    return {"title": title, "subtitle": subtitle, "columns": columns, "rows": rows, "totals": totals or {}, "notes": notes or [], "ledger_status": status, **extra}


def _stamp(db: Session, rep: dict) -> dict:
    rep["ledger_status"] = settings_service.get(db, acc.LEDGER_STATUS_KEY)
    if rep["ledger_status"] != "official":
        rep["notes"].insert(0, "The ledger is not yet official: opening balances have not been reconciled and approved. Treat these figures as provisional.")
    return rep


def _base(date_from: date | None, date_to: date | None, location_id: int | None):
    c = [JournalEntry.status.in_(POSTED)]
    if date_from:
        c.append(JournalLine.entry_date >= date_from)
    if date_to:
        c.append(JournalLine.entry_date <= date_to)
    if location_id:
        c.append(JournalLine.location_id == location_id)
    return c


def _sums(db: Session, date_from, date_to, location_id=None) -> dict[int, tuple[Decimal, Decimal]]:
    rows = db.execute(select(JournalLine.account_id, func.coalesce(func.sum(JournalLine.debit), 0), func.coalesce(func.sum(JournalLine.credit), 0))
                      .join(JournalEntry, JournalEntry.id == JournalLine.entry_id).where(*_base(date_from, date_to, location_id)).group_by(JournalLine.account_id)).all()
    return {a: (Decimal(d), Decimal(c)) for a, d, c in rows}


def _accounts(db: Session) -> list[Account]:
    acc.ensure_chart(db)
    return list(db.scalars(select(Account).order_by(Account.code)).all())


# ---- general ledger -------------------------------------------------------------------------------------------------------------------------
def general_ledger(db: Session, *, account_id: int, date_from: date, date_to: date, location_id: int | None = None, party_type: str | None = None, party_id: int | None = None) -> dict:
    a = db.get(Account, account_id)
    if a is None:
        raise NotFoundError("Account not found")
    if date_from > date_to:
        raise BusinessRuleError("The start date is after the end date.")
    pf = []
    if party_type:
        pf.append(JournalLine.party_type == party_type)
    if party_id:
        pf.append(JournalLine.party_id == party_id)
    opening = Decimal(db.scalar(select(func.coalesce(func.sum(JournalLine.debit - JournalLine.credit), 0)).join(JournalEntry, JournalEntry.id == JournalLine.entry_id)
                                .where(JournalLine.account_id == a.id, *pf, *_base(None, date_from.fromordinal(date_from.toordinal() - 1), location_id))))
    rows = db.execute(select(JournalLine, JournalEntry).join(JournalEntry, JournalEntry.id == JournalLine.entry_id)
                      .where(JournalLine.account_id == a.id, *pf, *_base(date_from, date_to, location_id)).order_by(JournalLine.entry_date, JournalEntry.id, JournalLine.id)).all()
    sign = 1 if acc.normal_side(a.type) == "debit" else -1
    run, out, td, tc = opening, [], ZERO, ZERO
    out.append({"entry_date": None, "ref": "", "memo": "Opening balance", "debit": "", "credit": "", "balance": _s(opening * sign), "entry_id": None})
    for ln, e in rows:
        run += Decimal(ln.debit) - Decimal(ln.credit)
        td += Decimal(ln.debit)
        tc += Decimal(ln.credit)
        out.append({"entry_date": ln.entry_date, "ref": e.ref, "memo": ln.memo or e.memo, "debit": _s(ln.debit) if ln.debit else "", "credit": _s(ln.credit) if ln.credit else "",
                    "balance": _s(run * sign), "entry_id": e.id, "kind": e.kind, "party_type": ln.party_type, "party_id": ln.party_id})
    return _stamp(db, _report(f"General ledger: {a.code} {a.name}", [col("entry_date", "Date"), col("ref", "Entry"), col("memo", "Description"), col("debit", "Debit", "right"),
                                                                  col("credit", "Credit", "right"), col("balance", "Balance", "right")], out,
                              subtitle=f"{date_from:%d %b %Y} to {date_to:%d %b %Y}", totals={"debit": _s(td), "credit": _s(tc), "closing": _s(run * sign)}, account_id=a.id))


# ---- trial balance ------------------------------------------------------------------------------------------------------------------------------
def trial_balance(db: Session, *, as_of: date, location_id: int | None = None, include_zero: bool = False) -> dict:
    sums = _sums(db, None, as_of, location_id)
    rows, td, tc = [], ZERO, ZERO
    for a in _accounts(db):
        d, c = sums.get(a.id, (ZERO, ZERO))
        net = d - c
        if net == 0 and d == 0 and not include_zero:
            continue
        dr, cr = (net, ZERO) if net > 0 else (ZERO, -net)
        td += dr
        tc += cr
        rows.append({"code": a.code, "name": a.name, "type": a.type, "debit": _s(dr) if dr else "", "credit": _s(cr) if cr else "", "account_id": a.id})
    return _stamp(db, _report("Trial balance", [col("code", "Code"), col("name", "Account"), col("debit", "Debit", "right"), col("credit", "Credit", "right")], rows,
                              subtitle=f"As at {as_of:%d %b %Y}", totals={"debit": _s(td), "credit": _s(tc), "balanced": q2(td) == q2(tc)}))


# ---- income statement -------------------------------------------------------------------------------------------------------------------------
def _pl_lines(db: Session, date_from: date, date_to: date, location_id: int | None):
    sums = _sums(db, date_from, date_to, location_id)
    by = defaultdict(list)
    for a in _accounts(db):
        if a.type not in ("income", "contra_income", "expense"):
            continue
        d, c = sums.get(a.id, (ZERO, ZERO))
        amt = (c - d) if a.type == "income" else (d - c)
        if amt != 0:
            by[a.category].append((a, q2(amt)))
    return by


def income_statement(db: Session, *, date_from: date, date_to: date, location_id: int | None = None) -> dict:
    if date_from > date_to:
        raise BusinessRuleError("The start date is after the end date.")
    by = _pl_lines(db, date_from, date_to, location_id)
    rows: list[dict] = []

    def section(title: str, cats: list[str], sign: int = 1) -> Decimal:
        rows.append({"label": title, "amount": "", "kind": "head"})
        tot = ZERO
        for cat in cats:
            for a, amt in by.get(cat, []):
                v = amt * (-1 if a.type == "contra_income" else 1) if sign == 1 else amt
                rows.append({"label": f"{a.code}  {a.name}", "amount": _s(amt if a.type != "contra_income" else -amt), "kind": "line", "account_id": a.id})
                tot += v
        return tot

    revenue = section("Revenue", ["revenue"])
    rows.append({"label": "Net revenue", "amount": _s(revenue), "kind": "total"})
    cogs = section("Cost of sales", ["cost_of_sales"], sign=-1)
    rows.append({"label": "Total cost of sales", "amount": _s(cogs), "kind": "total"})
    gross = revenue - cogs
    rows.append({"label": "Gross profit", "amount": _s(gross), "kind": "total"})
    opex = section("Operating expenses", ["operating_expense"], sign=-1)
    rows.append({"label": "Total operating expenses", "amount": _s(opex), "kind": "total"})
    other = section("Other income", ["other_income"], sign=-1)
    net = gross - opex + other
    rows.append({"label": "Net profit", "amount": _s(net), "kind": "grand"})
    margin = f"{(gross / revenue * 100):.1f}%" if revenue else "n/a"
    return _stamp(db, _report("Profit and loss", [col("label", "Line"), col("amount", "Amount", "right")], rows, subtitle=f"{date_from:%d %b %Y} to {date_to:%d %b %Y}",
                              totals={"net_revenue": _s(revenue), "cost_of_sales": _s(cogs), "gross_profit": _s(gross), "gross_margin": margin, "operating_expenses": _s(opex),
                                      "other_income": _s(other), "net_profit": _s(net)},
                              notes=["Revenue is recognised at "
                                     + ("dispatch from the location." if settings_service.get(db, acc.REVENUE_KEY) == "dispatch" else "delivery to the customer.")]))


def net_profit(db: Session, date_from: date | None, date_to: date, location_id: int | None = None) -> Decimal:
    sums = _sums(db, date_from, date_to, location_id)
    tot = ZERO
    for a in _accounts(db):
        d, c = sums.get(a.id, (ZERO, ZERO))
        if a.type in ("income", "contra_income", "expense"):
            tot += c - d
    return q2(tot)


# ---- balance sheet --------------------------------------------------------------------------------------------------------------------------------
def balance_sheet(db: Session, *, as_of: date) -> dict:
    """Retained and current-year earnings are derived from the income and expense accounts; there are no year-end closing entries."""
    sums = _sums(db, None, as_of)
    fy = acc.fiscal_year_of(db, as_of)
    fy_start, _ = acc.fiscal_year_bounds(db, fy)
    party = _party_signs(db, as_of)
    assets, liabs, equity = [], [], []
    for a in _accounts(db):
        if a.type not in ("asset", "liability", "equity"):
            continue
        d, c = sums.get(a.id, (ZERO, ZERO))
        net = d - c
        if net == 0:
            continue
        if a.requires_party and a.id in party:
            pos, neg = party[a.id]  # pos: debit-balance parties; neg: credit-balance parties (both as positive numbers)
            if a.type == "asset":
                assets.append({"label": f"{a.code}  {a.name}", "amount": _s(pos), "kind": "line", "account_id": a.id})
                if neg:
                    liabs.append({"label": f"{a.code}  {a.name}: credit balances (customer advances, overpayments)", "amount": _s(neg), "kind": "line", "account_id": a.id})
            else:
                liabs.append({"label": f"{a.code}  {a.name}", "amount": _s(neg), "kind": "line", "account_id": a.id})
                if pos:
                    assets.append({"label": f"{a.code}  {a.name}: debit balances (supplier advances, overpayments)", "amount": _s(pos), "kind": "line", "account_id": a.id})
            continue
        (assets if a.type == "asset" else liabs if a.type == "liability" else equity).append(
            {"label": f"{a.code}  {a.name}", "amount": _s(net if a.type == "asset" else -net), "kind": "line", "account_id": a.id})
    prior = net_profit(db, None, fy_start.fromordinal(fy_start.toordinal() - 1))
    cur = net_profit(db, fy_start, as_of)
    if prior:
        equity.append({"label": "Retained earnings (prior years)", "amount": _s(prior), "kind": "line"})
    if cur:
        equity.append({"label": "Current-year earnings", "amount": _s(cur), "kind": "line"})
    ta = sum((Decimal(r["amount"]) for r in assets), ZERO)
    tl = sum((Decimal(r["amount"]) for r in liabs), ZERO)
    te = sum((Decimal(r["amount"]) for r in equity), ZERO)
    rows = [{"label": "Assets", "amount": "", "kind": "head"}, *assets, {"label": "Total assets", "amount": _s(ta), "kind": "total"},
            {"label": "Liabilities", "amount": "", "kind": "head"}, *liabs, {"label": "Total liabilities", "amount": _s(tl), "kind": "total"},
            {"label": "Equity", "amount": "", "kind": "head"}, *equity, {"label": "Total equity", "amount": _s(te), "kind": "total"},
            {"label": "Total liabilities and equity", "amount": _s(tl + te), "kind": "grand"}]
    return _stamp(db, _report("Balance sheet", [col("label", "Line"), col("amount", "Amount", "right")], rows, subtitle=f"As at {as_of:%d %b %Y}",
                              totals={"assets": _s(ta), "liabilities": _s(tl), "equity": _s(te), "balanced": q2(ta) == q2(tl + te)}))


def _party_signs(db: Session, as_of: date) -> dict[int, tuple[Decimal, Decimal]]:
    """For party-controlled accounts, the sum of debit balances and of credit balances across parties."""
    rows = db.execute(select(JournalLine.account_id, JournalLine.party_type, JournalLine.party_id, func.sum(JournalLine.debit - JournalLine.credit))
                      .join(JournalEntry, JournalEntry.id == JournalLine.entry_id).join(Account, Account.id == JournalLine.account_id)
                      .where(Account.requires_party.is_(True), JournalEntry.status.in_(POSTED), JournalLine.entry_date <= as_of)
                      .group_by(JournalLine.account_id, JournalLine.party_type, JournalLine.party_id)).all()
    out: dict[int, list[Decimal]] = defaultdict(lambda: [ZERO, ZERO])
    for aid, _t, _i, bal in rows:
        bal = Decimal(bal)
        if bal > 0:
            out[aid][0] += bal
        elif bal < 0:
            out[aid][1] += -bal
    return {k: (v[0], v[1]) for k, v in out.items()}


# ---- cash flow (direct method) ------------------------------------------------------------------------------------------------------------------
def cash_flow(db: Session, *, date_from: date, date_to: date) -> dict:
    amap = acc.accounts_map(db) if acc.ensure_chart(db) is None else {}
    cash_ids = {c.account_id for c in db.scalars(select(CashAccount)).all()}
    accts = {a.id: a for a in db.scalars(select(Account)).all()}
    opening = ZERO
    for i in cash_ids:
        opening += acc.account_balance(db, i, as_of=date_from.fromordinal(date_from.toordinal() - 1))
    entries = db.scalars(select(JournalEntry).join(JournalLine, JournalLine.entry_id == JournalEntry.id)
                         .where(JournalEntry.status.in_(POSTED), JournalLine.account_id.in_(cash_ids), JournalLine.entry_date >= date_from, JournalLine.entry_date <= date_to)
                         .distinct()).all()
    groups: dict[tuple[str, str], list[Decimal]] = defaultdict(lambda: [ZERO, ZERO])
    for e in entries:
        cash_net = sum((Decimal(ln.debit) - Decimal(ln.credit) for ln in e.lines if ln.account_id in cash_ids), ZERO)
        if cash_net == 0:
            continue
        others = [accts[ln.account_id] for ln in e.lines if ln.account_id not in cash_ids]
        types = {a.type for a in others}
        codes = {a.code for a in others}
        if "equity" in types and codes <= {"3000", "3100", "3200"}:
            sec, label = "Financing", "Owner capital introduced or withdrawn"
        elif codes <= {"1500"}:
            sec, label = "Investing", "Other assets bought or sold"
        elif "2300" in codes and codes <= {"2300"}:
            sec, label = "Financing", "Other liabilities (loans)"
        else:
            sec = "Operating"
            label = {"1100": "Received from customers", "1110": "Received from distributors", "2000": "Paid to suppliers", "4000": "Cash sales", "4010": "Cash sales",
                     "2100": "Customer advances and refunds", "1300": "Supplier deposits"}.get(next(iter(sorted(codes)), ""), "")
            if not label:
                label = "Operating expenses paid" if "expense" in types else ("Customer refunds and returns" if "contra_income" in types else "Other operating receipts and payments")
        g = groups[(sec, label)]
        g[0 if cash_net > 0 else 1] += abs(cash_net)
    rows, tot = [], {}
    net_all = ZERO
    for sec in ("Operating", "Investing", "Financing"):
        rows.append({"label": sec + " activities", "inflow": "", "outflow": "", "net": "", "kind": "head"})
        st = ZERO
        for (s, label), (i, o) in sorted(groups.items()):
            if s != sec:
                continue
            rows.append({"label": label, "inflow": _s(i), "outflow": _s(o), "net": _s(i - o), "kind": "line"})
            st += i - o
        rows.append({"label": f"Net cash from {sec.lower()} activities", "inflow": "", "outflow": "", "net": _s(st), "kind": "total"})
        tot[sec.lower()] = _s(st)
        net_all += st
    closing = opening + net_all
    check = sum((acc.account_balance(db, i, as_of=date_to) for i in cash_ids), ZERO)
    rows += [{"label": "Net change in cash and bank", "inflow": "", "outflow": "", "net": _s(net_all), "kind": "total"},
             {"label": "Opening cash and bank", "inflow": "", "outflow": "", "net": _s(opening), "kind": "line"},
             {"label": "Closing cash and bank", "inflow": "", "outflow": "", "net": _s(closing), "kind": "grand"}]
    return _stamp(db, _report("Cash flow", [col("label", "Line"), col("inflow", "Cash in", "right"), col("outflow", "Cash out", "right"), col("net", "Net", "right")], rows,
                              subtitle=f"{date_from:%d %b %Y} to {date_to:%d %b %Y}",
                              totals={**tot, "net_change": _s(net_all), "opening": _s(opening), "closing": _s(closing), "agrees_with_ledger": q2(check) == q2(closing)},
                              notes=["Direct method, built from the cash and bank accounts of the ledger. Transfers between your own cash and bank accounts net to zero."]))


# ---- ageing -----------------------------------------------------------------------------------------------------------------------------------------
BUCKETS = [(0, 30, "0-30"), (31, 60, "31-60"), (61, 90, "61-90"), (91, 10**6, "Over 90")]
PARTY_NAMES = {"customer": (Customer, "name"), "supplier": (Supplier, "name"), "distributor": (Distributor, "name"), "employee": (Employee, "full_name")}


def _party_name(db: Session, ptype: str, pid: int) -> str:
    m = PARTY_NAMES.get(ptype)
    obj = db.get(m[0], pid) if m else None
    return getattr(obj, m[1]) if obj else f"{ptype} #{pid}"


def aging(db: Session, *, kind: str, as_of: date) -> dict:
    """First-in-first-out ageing from the ledger. Receivables: customers + distributors. Payables: suppliers. Credit balances are listed separately, not netted."""
    acc.ensure_chart(db)
    amap = acc.accounts_map(db)
    if kind == "receivables":
        accounts, sign, title = [amap["1100"].id, amap["1110"].id, amap["1400"].id], 1, "Receivables ageing"
    elif kind == "payables":
        accounts, sign, title = [amap["2000"].id], -1, "Payables ageing"
    else:
        raise BusinessRuleError("Ageing is for receivables or payables.")
    lines = db.execute(select(JournalLine.party_type, JournalLine.party_id, JournalLine.entry_date, (JournalLine.debit - JournalLine.credit) * sign)
                       .join(JournalEntry, JournalEntry.id == JournalLine.entry_id)
                       .where(JournalLine.account_id.in_(accounts), JournalEntry.status.in_(POSTED), JournalLine.entry_date <= as_of, JournalLine.party_id.is_not(None))
                       .order_by(JournalLine.entry_date, JournalLine.id)).all()
    per: dict[tuple, list] = defaultdict(list)
    for pt, pid, d, amt in lines:
        per[(pt, pid)].append((d, Decimal(amt)))
    rows, tot = [], defaultdict(lambda: ZERO)
    for (pt, pid), items in per.items():
        charges: list[list] = []
        credit = ZERO
        for d, amt in items:
            if amt > 0:
                charges.append([d, amt])
            else:
                credit += -amt
        for ch in charges:  # oldest first
            take = min(ch[1], credit)
            ch[1] -= take
            credit -= take
        b = {lbl: ZERO for _, _, lbl in BUCKETS}
        for d, amt in charges:
            if amt <= 0:
                continue
            age = (as_of - d).days
            for lo, hi, lbl in BUCKETS:
                if lo <= age <= hi:
                    b[lbl] += amt
                    break
        owed = sum(b.values(), ZERO)
        if owed == 0 and credit == 0:
            continue
        rows.append({"party_type": pt, "party_id": pid, "party": _party_name(db, pt, pid), **{k: _s(v) if v else "" for k, v in b.items()}, "total": _s(owed),
                     "credit_balance": _s(credit) if credit else ""})
        for k, v in b.items():
            tot[k] += v
        tot["total"] += owed
        tot["credit"] += credit
    rows.sort(key=lambda r: -Decimal(r["total"] or 0))
    cols = [col("party", "Party"), col("party_type", "Type")] + [col(lbl, lbl, "right") for _, _, lbl in BUCKETS] + [col("total", "Owed", "right"), col("credit_balance", "Credit balance", "right")]
    return _stamp(db, _report(title, cols, rows, subtitle=f"As at {as_of:%d %b %Y}; payments are applied to the oldest charge first",
                              totals={**{k: _s(v) for k, v in tot.items()}}))


# ---- party statement -------------------------------------------------------------------------------------------------------------------------------
def party_statement(db: Session, *, party_type: str, party_id: int, date_from: date, date_to: date) -> dict:
    if party_type not in ("customer", "supplier", "distributor", "employee"):
        raise BusinessRuleError("Statements are for a customer, supplier, distributor or employee.")
    acc.ensure_chart(db)
    amap = acc.accounts_map(db)
    code = {"customer": "1100", "distributor": "1110", "supplier": "2000", "employee": "1400"}[party_type]
    sign = -1 if party_type == "supplier" else 1
    rep = general_ledger(db, account_id=amap[code].id, date_from=date_from, date_to=date_to, party_type=party_type, party_id=party_id)
    rep["title"] = f"Statement: {_party_name(db, party_type, party_id)}"
    rep["notes"].append("A supplier statement shows what we owe; a customer or distributor statement shows what they owe us." if sign else "")
    rep["notes"] = [n for n in rep["notes"] if n]
    return rep


# ---- sales, returns, discounts --------------------------------------------------------------------------------------------------------------------
def _monthly(db: Session, codes: list[str], date_from: date, date_to: date, location_id: int | None) -> dict[str, dict[str, Decimal]]:
    amap = acc.accounts_map(db)
    ids = {amap[c].id: c for c in codes}
    month = func.to_char(JournalLine.entry_date, "YYYY-MM")
    rows = db.execute(select(month, JournalLine.account_id, func.coalesce(func.sum(JournalLine.credit - JournalLine.debit), 0)).join(JournalEntry, JournalEntry.id == JournalLine.entry_id)
                      .where(JournalLine.account_id.in_(ids), *_base(date_from, date_to, location_id)).group_by(month, JournalLine.account_id).order_by(month)).all()
    out: dict[str, dict[str, Decimal]] = defaultdict(lambda: defaultdict(lambda: ZERO))
    for m, aid, amt in rows:
        out[m][ids[aid]] += Decimal(amt)
    return out


def sales_report(db: Session, *, date_from: date, date_to: date, location_id: int | None = None) -> dict:
    acc.ensure_chart(db)
    data = _monthly(db, ["4000", "4010", "4100", "4200"], date_from, date_to, location_id)
    rows, t = [], defaultdict(lambda: ZERO)
    for m in sorted(data):
        d = data[m]
        gross, fee, ret, disc = d["4000"], d["4010"], -d["4100"], -d["4200"]
        net = gross + fee - ret - disc
        rows.append({"month": m, "gross": _s(gross), "delivery": _s(fee), "returns": _s(ret), "discounts": _s(disc), "net": _s(net)})
        for k, v in (("gross", gross), ("delivery", fee), ("returns", ret), ("discounts", disc), ("net", net)):
            t[k] += v
    return _stamp(db, _report("Sales, returns and discounts", [col("month", "Month"), col("gross", "Product sales", "right"), col("delivery", "Delivery fees", "right"),
                                                               col("returns", "Returns", "right"), col("discounts", "Discounts", "right"), col("net", "Net sales", "right")], rows,
                              subtitle=f"{date_from:%d %b %Y} to {date_to:%d %b %Y}", totals={k: _s(v) for k, v in t.items()}))


def expense_summary(db: Session, *, date_from: date, date_to: date, location_id: int | None = None) -> dict:
    acc.ensure_chart(db)
    month = func.to_char(JournalLine.entry_date, "YYYY-MM")
    q = db.execute(select(Account.code, Account.name, month, func.coalesce(func.sum(JournalLine.debit - JournalLine.credit), 0)).join(Account, Account.id == JournalLine.account_id)
                   .join(JournalEntry, JournalEntry.id == JournalLine.entry_id)
                   .where(Account.type == "expense", Account.category == "operating_expense", *_base(date_from, date_to, location_id)).group_by(Account.code, Account.name, month)).all()
    by: dict[tuple, dict[str, Decimal]] = defaultdict(dict)
    months = sorted({m for *_x, m, _a in q})
    for code, name, m, amt in q:
        by[(code, name)][m] = Decimal(amt)
    rows, tot = [], ZERO
    mt = defaultdict(lambda: ZERO)
    for (code, name), md in sorted(by.items()):
        total = sum(md.values(), ZERO)
        if total == 0:
            continue
        rows.append({"code": code, "name": name, **{m: _s(md.get(m, ZERO)) for m in months}, "total": _s(total)})
        tot += total
        for m, v in md.items():
            mt[m] += v
    cols = [col("code", "Code"), col("name", "Expense")] + [col(m, m, "right") for m in months] + [col("total", "Total", "right")]
    return _stamp(db, _report("Expense summary", cols, rows, subtitle=f"{date_from:%d %b %Y} to {date_to:%d %b %Y}", totals={**{m: _s(v) for m, v in mt.items()}, "total": _s(tot)}))


def cogs_gross_profit(db: Session, *, date_from: date, date_to: date, location_id: int | None = None) -> dict:
    acc.ensure_chart(db)
    amap = acc.accounts_map(db)
    ids = {amap[c].id: c for c in ("4000", "4010", "4100", "4200", "5000", "5100", "5110")}
    month = func.to_char(JournalLine.entry_date, "YYYY-MM")
    q = db.execute(select(month, JournalLine.account_id, func.coalesce(func.sum(JournalLine.credit - JournalLine.debit), 0)).join(JournalEntry, JournalEntry.id == JournalLine.entry_id)
                   .where(JournalLine.account_id.in_(ids), *_base(date_from, date_to, location_id)).group_by(month, JournalLine.account_id)).all()
    data: dict[str, dict[str, Decimal]] = defaultdict(lambda: defaultdict(lambda: ZERO))
    for m, aid, amt in q:
        data[m][ids[aid]] += Decimal(amt)
    rows, t = [], defaultdict(lambda: ZERO)
    for m in sorted(data):
        d = data[m]
        rev = d["4000"] + d["4010"] + d["4100"] + d["4200"]   # contra accounts carry negative credit-minus-debit
        cogs = -d["5000"]
        loss = -(d["5100"] + d["5110"])
        gp = rev - cogs - loss
        rows.append({"month": m, "revenue": _s(rev), "cogs": _s(cogs), "losses": _s(loss), "gross_profit": _s(gp), "margin": f"{(gp / rev * 100):.1f}%" if rev else "n/a"})
        for k, v in (("revenue", rev), ("cogs", cogs), ("losses", loss), ("gross_profit", gp)):
            t[k] += v
    tt = {k: _s(v) for k, v in t.items()}
    tt["margin"] = f"{(t['gross_profit'] / t['revenue'] * 100):.1f}%" if t["revenue"] else "n/a"
    return _stamp(db, _report("Cost of goods sold and gross profit", [col("month", "Month"), col("revenue", "Net revenue", "right"), col("cogs", "COGS", "right"),
                                                                       col("losses", "Write-offs and variances", "right"), col("gross_profit", "Gross profit", "right"), col("margin", "Margin", "right")], rows,
                              subtitle=f"{date_from:%d %b %Y} to {date_to:%d %b %Y}", totals=tt,
                              notes=["Cost of goods sold uses weighted-average cost taken from the stock ledger at the moment of dispatch."]))


# ---- inventory valuation (ledger vs stock sub-ledger) ---------------------------------------------------------------------------------------------
def inventory_valuation(db: Session, *, location_id: int | None = None) -> dict:
    acc.ensure_chart(db)
    amap = acc.accounts_map(db)
    q = select(StockCost, ProductVariant.sku, ProductVariant.name, Location.name).join(ProductVariant, ProductVariant.id == StockCost.variant_id) \
        .join(Location, Location.id == StockCost.location_id).where(StockCost.valued_qty > 0)
    if location_id:
        q = q.where(StockCost.location_id == location_id)
    rows, tot = [], ZERO
    for sc, sku, vname, lname in db.execute(q.order_by(Location.name, ProductVariant.sku)).all():
        v = q2(sc.total_value)
        rows.append({"location": lname, "sku": sku, "item": vname, "qty": sc.valued_qty, "avg_cost": _s(Decimal(sc.total_value) / sc.valued_qty), "value": _s(v)})
        tot += v
    gl = q2(acc.account_balance(db, amap["1200"].id))
    totals = {"stock_ledger_value": _s(tot)}
    notes = ["Weighted-average cost. The stock ledger is the sub-ledger; the general ledger must agree with it."]
    if not location_id:
        totals.update({"general_ledger_value": _s(gl), "difference": _s(gl - tot), "agrees": q2(gl) == q2(tot)})
    return _stamp(db, _report("Inventory valuation", [col("location", "Location"), col("sku", "SKU"), col("item", "Item"), col("qty", "Qty", "right"),
                                                      col("avg_cost", "Average cost", "right"), col("value", "Value", "right")], rows, totals=totals, notes=notes))


# ---- monthly and annual summaries ------------------------------------------------------------------------------------------------------------------------
def monthly_summary(db: Session, *, fiscal_year: int, location_id: int | None = None) -> dict:
    start, end = acc.fiscal_year_bounds(db, fiscal_year)
    rows, cur = [], start
    cash_ids = [c.account_id for c in db.scalars(select(CashAccount)).all()]
    tot = defaultdict(lambda: ZERO)
    while cur <= end:
        me = date(cur.year, cur.month, calendar.monthrange(cur.year, cur.month)[1])
        s = _sums(db, cur, me, location_id)
        amap = acc.accounts_map(db)
        accts = {a.id: a for a in _accounts(db)}

        def cr(code):  # credit-normal movement
            d, c = s.get(amap[code].id, (ZERO, ZERO))
            return c - d
        rev = cr("4000") + cr("4010") + cr("4100") + cr("4200")
        cogs = -(cr("5000"))
        loss = -(cr("5100") + cr("5110"))
        opex = sum((s[i][0] - s[i][1] for i in s if accts[i].category == "operating_expense"), ZERO)
        other = cr("4900")
        net = rev - cogs - loss - opex + other
        cash = sum(((s.get(i, (ZERO, ZERO))[0] - s.get(i, (ZERO, ZERO))[1]) for i in cash_ids), ZERO) if not location_id else ZERO
        row = {"month": cur.strftime("%Y-%m"), "revenue": _s(rev), "cogs": _s(cogs + loss), "gross_profit": _s(rev - cogs - loss), "expenses": _s(opex), "net_profit": _s(net),
               "cash_change": _s(cash) if not location_id else ""}
        rows.append(row)
        for k, v in (("revenue", rev), ("cogs", cogs + loss), ("gross_profit", rev - cogs - loss), ("expenses", opex), ("net_profit", net), ("cash_change", cash)):
            tot[k] += v
        cur = date(cur.year + (cur.month == 12), (cur.month % 12) + 1, 1)
    return _stamp(db, _report(f"Monthly summary, fiscal year {fiscal_year}", [col("month", "Month"), col("revenue", "Net revenue", "right"), col("cogs", "Cost of sales", "right"),
                                                                              col("gross_profit", "Gross profit", "right"), col("expenses", "Expenses", "right"),
                                                                              col("net_profit", "Net profit", "right"), col("cash_change", "Cash change", "right")], rows,
                              subtitle=f"{start:%d %b %Y} to {end:%d %b %Y}", totals={k: _s(v) for k, v in tot.items()}))


def annual_summary(db: Session) -> dict:
    acc.ensure_chart(db)
    years = sorted({y for (y,) in db.execute(select(func.extract("year", JournalLine.entry_date)).distinct()).all()})
    fys = sorted({acc.fiscal_year_of(db, date(int(y), m, 1)) for y in years for m in (1, 12)})
    rows = []
    for fy in fys:
        start, end = acc.fiscal_year_bounds(db, fy)
        pl = income_statement(db, date_from=start, date_to=end)["totals"]
        rows.append({"fiscal_year": str(fy), "net_revenue": pl["net_revenue"], "gross_profit": pl["gross_profit"], "operating_expenses": pl["operating_expenses"], "net_profit": pl["net_profit"]})
    return _stamp(db, _report("Annual summary", [col("fiscal_year", "Fiscal year"), col("net_revenue", "Net revenue", "right"), col("gross_profit", "Gross profit", "right"),
                                                 col("operating_expenses", "Expenses", "right"), col("net_profit", "Net profit", "right")], rows))


REPORTS = {"general_ledger", "trial_balance", "income_statement", "balance_sheet", "cash_flow", "aging_receivables", "aging_payables", "party_statement", "sales", "expenses",
           "cogs", "inventory_valuation", "monthly", "annual"}
