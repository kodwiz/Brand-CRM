"""Accounting core: chart of accounts, periods, journals (double entry), settings.

Services never commit. Every posted entry balances (checked here and by a deferred database trigger) and is immutable;
corrections are new, opposite entries. System entries are created by `accounting_posting` from source documents and are
keyed by (source_type, source_id, event), which is unique, so a document can never be posted twice.
"""
from __future__ import annotations

import calendar
import json
from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import ROUND_HALF_UP, Decimal

from sqlalchemy import func, select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from backend.models import (
    Account, AccountingPeriod, CashAccount, JournalEntry, JournalLine, PeriodRequest, User,
)
from backend.models.accounting import ACCOUNT_TYPES, DEBIT_NORMAL
from backend.services import audit_service, settings_service
from backend.services.errors import BusinessRuleError, ConflictError, NotFoundError, SelfApprovalError

Q2 = Decimal("0.01")
ZERO = Decimal("0")

FISCAL_START_KEY = "accounting.fiscal_year_start_month"
REVENUE_KEY = "accounting.revenue_recognition"          # dispatch | delivery
METHODS_KEY = "accounting.method_accounts"              # payment method -> cash account id
EXPENSE_LIMIT_KEY = "accounting.expense_approval_limit"  # expenses up to this amount post without a second approver
LEDGER_STATUS_KEY = "accounting.ledger_status"          # preparing | official
GO_LIVE_KEY = "accounting.go_live"
settings_service.DEFAULTS.update({FISCAL_START_KEY: 1, REVENUE_KEY: "dispatch", METHODS_KEY: {}, EXPENSE_LIMIT_KEY: "0",
                                  LEDGER_STATUS_KEY: "preparing", GO_LIVE_KEY: None})

# (code, name, type, category, system, control, requires_party)
CHART = [
    ("1000", "Cash on hand", "asset", "current_asset", True, False, False),
    ("1010", "Bank account (main)", "asset", "current_asset", True, False, False),
    ("1020", "Courier collections (cash on delivery)", "asset", "current_asset", True, False, False),
    ("1100", "Customer receivables", "asset", "receivable", True, True, True),
    ("1110", "Distributor receivables", "asset", "receivable", True, True, True),
    ("1200", "Inventory", "asset", "inventory", True, True, False),
    ("1210", "Stock in transit between locations", "asset", "inventory", True, True, False),
    ("1220", "Goods with couriers, not yet delivered", "asset", "inventory", True, True, False),
    ("1300", "Supplier deposits and advances", "asset", "current_asset", False, False, True),
    ("1400", "Employee purchase receivables", "asset", "receivable", True, True, True),
    ("1500", "Other assets", "asset", "other_asset", False, False, False),
    ("2000", "Supplier payables", "liability", "payable", True, True, True),
    ("2050", "Goods received not invoiced (clearing)", "liability", "payable", True, True, False),
    ("2100", "Customer advances and store credit", "liability", "current_liability", False, False, True),
    ("2200", "Taxes payable", "liability", "tax", True, False, False),
    ("2300", "Other business liabilities", "liability", "current_liability", False, False, False),
    ("3000", "Owner capital", "equity", "equity", True, False, False),
    ("3100", "Retained earnings", "equity", "equity", True, False, False),
    ("3200", "Opening balance equity", "equity", "equity", True, False, False),
    ("4000", "Product sales", "income", "revenue", True, False, False),
    ("4010", "Delivery fee income", "income", "revenue", True, False, False),
    ("4900", "Other operating income", "income", "other_income", True, False, False),
    ("4100", "Sales returns", "contra_income", "revenue", True, False, False),
    ("4200", "Discounts and allowances", "contra_income", "revenue", True, False, False),
    ("5000", "Cost of goods sold", "expense", "cost_of_sales", True, False, False),
    ("5100", "Inventory write-offs (damaged, expired, lost)", "expense", "cost_of_sales", True, False, False),
    ("5110", "Stock adjustments and count variances", "expense", "cost_of_sales", True, False, False),
    ("5200", "Delivery and freight", "expense", "operating_expense", False, False, False),
    ("5300", "Rent", "expense", "operating_expense", False, False, False),
    ("5400", "Utilities", "expense", "operating_expense", False, False, False),
    ("5500", "Salaries and wages", "expense", "operating_expense", False, False, False),
    ("5600", "Marketing", "expense", "operating_expense", False, False, False),
    ("5700", "Packaging", "expense", "operating_expense", False, False, False),
    ("5800", "Bad debts and receivable write-offs", "expense", "operating_expense", True, False, False),
    ("5900", "Other operating expenses", "expense", "operating_expense", False, False, False),
]
DEFAULT_CASH = [("Cash on hand", "cash", "1000"), ("Main bank account", "bank", "1010"), ("Courier collections", "clearing", "1020")]
DEFAULT_METHODS = {"cash": "Cash on hand", "bank_transfer": "Main bank account", "cheque": "Main bank account", "card": "Main bank account",
                   "other": "Main bank account", "cod_courier": "Courier collections"}
OPENING_CONTROL_ALLOWED = {"1100"}      # customer receivables may be opened with a party; other control accounts come from their sub-ledgers


def q2(x) -> Decimal:
    return Decimal(x).quantize(Q2, rounding=ROUND_HALF_UP)


def ensure_chart(db: Session) -> None:
    """Idempotent. Creates the standard chart and cash accounts the first time accounting is used; never edits existing rows."""
    if db.scalar(select(func.count()).select_from(Account)) == 0:
        db.execute(text("select pg_advisory_xact_lock(7600601)"))     # two first requests must not both seed the chart
    if db.scalar(select(func.count()).select_from(Account)) == 0:
        for code, name, typ, cat, system, control, party in CHART:
            db.add(Account(code=code, name=name, type=typ, category=cat, is_system=system, is_control=control, requires_party=party))
        db.flush()
    if db.scalar(select(func.count()).select_from(CashAccount)) == 0:
        by_code = {a.code: a for a in db.scalars(select(Account)).all()}
        for name, kind, code in DEFAULT_CASH:
            if code in by_code:
                db.add(CashAccount(name=name, kind=kind, account_id=by_code[code].id))
        db.flush()


def account_by_code(db: Session, code: str) -> Account:
    a = db.scalar(select(Account).where(Account.code == code))
    if a is None:
        raise BusinessRuleError(f"Ledger account {code} is missing from the chart of accounts; it is needed by the posting rules.")
    return a


def accounts_map(db: Session) -> dict[str, Account]:
    return {a.code: a for a in db.scalars(select(Account)).all()}


# ---- settings -----------------------------------------------------------------------------------------------------------
def accounting_settings(db: Session) -> dict:
    sales_journals = db.scalar(select(func.count()).select_from(JournalEntry).where(JournalEntry.source_type.in_(("shipment", "sales_return")))) or 0
    methods = settings_service.get(db, METHODS_KEY) or {}
    return {"fiscal_year_start_month": int(settings_service.get(db, FISCAL_START_KEY)), "revenue_recognition": settings_service.get(db, REVENUE_KEY),
            "revenue_recognition_locked": sales_journals > 0, "expense_approval_limit": str(settings_service.get(db, EXPENSE_LIMIT_KEY)),
            "ledger_status": settings_service.get(db, LEDGER_STATUS_KEY), "method_accounts": methods, "go_live": settings_service.get(db, GO_LIVE_KEY)}


def set_accounting_settings(db: Session, *, actor: User, values: dict, reason: str | None, ip: str | None) -> dict:
    before = accounting_settings(db)
    if "revenue_recognition" in values and values["revenue_recognition"] is not None and values["revenue_recognition"] != before["revenue_recognition"]:
        if values["revenue_recognition"] not in ("dispatch", "delivery"):
            raise BusinessRuleError("Revenue recognition must be 'dispatch' or 'delivery'.")
        if before["revenue_recognition_locked"]:
            raise ConflictError("Sales have already been posted under the current revenue policy; it cannot be changed without an accountant-led restatement.")
        settings_service._set(db, REVENUE_KEY, values["revenue_recognition"], actor)
    if values.get("fiscal_year_start_month") is not None:
        m = int(values["fiscal_year_start_month"])
        if not 1 <= m <= 12:
            raise BusinessRuleError("The fiscal year start month must be between 1 and 12.")
        if m != before["fiscal_year_start_month"] and (db.scalar(select(func.count()).select_from(JournalEntry)) or 0) > 0:
            raise ConflictError("Journals exist; the fiscal year start cannot be changed.")
        settings_service._set(db, FISCAL_START_KEY, m, actor)
    if values.get("expense_approval_limit") is not None:
        lim = Decimal(str(values["expense_approval_limit"]))
        if lim < 0:
            raise BusinessRuleError("The expense limit cannot be negative.")
        settings_service._set(db, EXPENSE_LIMIT_KEY, str(lim), actor)
    if values.get("method_accounts") is not None:
        ensure_chart(db)
        ids = {c.id for c in db.scalars(select(CashAccount).where(CashAccount.is_active.is_(True))).all()}
        clean = {}
        for m, cid in values["method_accounts"].items():
            if m not in DEFAULT_METHODS:
                raise BusinessRuleError(f"Unknown payment method {m}.")
            if int(cid) not in ids:
                raise BusinessRuleError(f"Cash account {cid} does not exist or is inactive.")
            clean[m] = int(cid)
        merged = {**(settings_service.get(db, METHODS_KEY) or {}), **clean}
        settings_service._set(db, METHODS_KEY, merged, actor)
    after = accounting_settings(db)
    audit_service.record(db, actor=actor, action="accounting.settings_changed", entity_type="setting", entity_id="accounting", reason=reason,
                         before=before, after=after, ip=ip)
    return after


def cash_account_for_method(db: Session, method: str) -> CashAccount:
    ensure_chart(db)
    methods = settings_service.get(db, METHODS_KEY) or {}
    cid = methods.get(method)
    if cid is None:
        name = DEFAULT_METHODS.get(method, DEFAULT_METHODS["other"])
        ca = db.scalar(select(CashAccount).where(CashAccount.name == name))
    else:
        ca = db.get(CashAccount, int(cid))
    if ca is None or not ca.is_active:
        raise BusinessRuleError(f"No active cash or bank account is set for payment method '{method}'. Choose one in Accounting settings.")
    return ca


# ---- chart of accounts ----------------------------------------------------------------------------------------------------
def create_account(db: Session, *, actor: User, code: str, name: str, type: str, category: str | None, description: str | None,
                   requires_party: bool, ip: str | None) -> Account:
    ensure_chart(db)
    code, name = (code or "").strip(), (name or "").strip()
    if not code or not name:
        raise BusinessRuleError("An account needs a code and a name.")
    if type not in ACCOUNT_TYPES:
        raise BusinessRuleError(f"Account type must be one of {', '.join(ACCOUNT_TYPES)}.")
    if db.scalar(select(Account.id).where(Account.code == code)):
        raise ConflictError(f"Account code {code} is already used.")
    if db.scalar(select(Account.id).where(func.lower(Account.name) == name.lower())):
        raise ConflictError(f"An account called '{name}' already exists.")
    default_cat = {"asset": "current_asset", "liability": "current_liability", "equity": "equity", "income": "other_income",
                   "contra_income": "revenue", "expense": "operating_expense"}[type]
    a = Account(code=code, name=name, type=type, category=category or default_cat, description=description, requires_party=requires_party)
    db.add(a)
    db.flush()
    audit_service.record(db, actor=actor, action="account.created", entity_type="account", entity_id=a.id, after={"code": code, "name": name, "type": type}, ip=ip)
    return a


def update_account(db: Session, *, actor: User, account_id: int, name: str | None, description: str | None, is_active: bool | None, ip: str | None) -> Account:
    a = db.get(Account, account_id)
    if a is None:
        raise NotFoundError("Account not found")
    before = {"name": a.name, "is_active": a.is_active}
    if name is not None and name.strip() and name.strip() != a.name:
        if db.scalar(select(Account.id).where(func.lower(Account.name) == name.strip().lower(), Account.id != a.id)):
            raise ConflictError(f"An account called '{name}' already exists.")
        a.name = name.strip()
    if description is not None:
        a.description = description
    if is_active is not None and is_active != a.is_active:
        if not is_active:
            if a.is_system:
                raise BusinessRuleError("A system account is used by the posting rules and cannot be deactivated.")
            if account_balance(db, a.id) != 0:
                raise BusinessRuleError("An account with a balance cannot be deactivated.")
        a.is_active = is_active
    db.flush()
    audit_service.record(db, actor=actor, action="account.updated", entity_type="account", entity_id=a.id, before=before,
                         after={"name": a.name, "is_active": a.is_active}, ip=ip)
    return a


def account_balance(db: Session, account_id: int, *, as_of: date | None = None) -> Decimal:
    """Signed debit-minus-credit over posted entries."""
    q = select(func.coalesce(func.sum(JournalLine.debit - JournalLine.credit), 0)).join(JournalEntry, JournalEntry.id == JournalLine.entry_id) \
        .where(JournalLine.account_id == account_id, JournalEntry.status.in_(("posted", "reversed")))
    if as_of:
        q = q.where(JournalLine.entry_date <= as_of)
    return Decimal(db.scalar(q))


# ---- periods ----------------------------------------------------------------------------------------------------------------
def fiscal_year_of(db: Session, d: date) -> int:
    m = int(settings_service.get(db, FISCAL_START_KEY))
    return d.year + (1 if (m > 1 and d.month >= m) else 0)


def fiscal_year_bounds(db: Session, fy: int) -> tuple[date, date]:
    m = int(settings_service.get(db, FISCAL_START_KEY))
    if m == 1:
        return date(fy, 1, 1), date(fy, 12, 31)
    start = date(fy - 1, m, 1)
    end_m = m - 1
    return start, date(fy, end_m, calendar.monthrange(fy, end_m)[1])


def get_period(db: Session, d: date) -> AccountingPeriod:
    name = d.strftime("%Y-%m")
    p = db.scalar(select(AccountingPeriod).where(AccountingPeriod.name == name))
    if p is None:
        db.execute(pg_insert(AccountingPeriod).values(
            name=name, start_date=date(d.year, d.month, 1), end_date=date(d.year, d.month, calendar.monthrange(d.year, d.month)[1]),
            fiscal_year=fiscal_year_of(db, d), status="open").on_conflict_do_nothing())
        p = db.scalar(select(AccountingPeriod).where(AccountingPeriod.name == name))
    return p


def first_open_date(db: Session, d: date) -> date:
    """The first day on or after d that lies in an open period."""
    cur = d
    for _ in range(36):
        p = get_period(db, cur)
        if p.status == "open":
            return max(cur, p.start_date)
        cur = date(p.end_date.year + (p.end_date.month == 12), (p.end_date.month % 12) + 1, 1)
    raise BusinessRuleError("No open accounting period found.")


def period_view(p: AccountingPeriod) -> dict:
    return {"id": p.id, "name": p.name, "start_date": p.start_date, "end_date": p.end_date, "fiscal_year": p.fiscal_year, "status": p.status,
            "closed_at": p.closed_at, "reopened_count": p.reopened_count}


def list_periods(db: Session, fiscal_year: int | None = None) -> list[AccountingPeriod]:
    """Ensures every month of the fiscal year exists, then lists them."""
    ensure_chart(db)
    fy = fiscal_year or fiscal_year_of(db, date.today())
    start, end = fiscal_year_bounds(db, fy)
    cur = start
    while cur <= end:
        get_period(db, cur)
        cur = date(cur.year + (cur.month == 12), (cur.month % 12) + 1, 1)
    return list(db.scalars(select(AccountingPeriod).where(AccountingPeriod.start_date >= start, AccountingPeriod.end_date <= end)
                           .order_by(AccountingPeriod.start_date)).all())


def close_period(db: Session, *, actor: User, period_id: int, ip: str | None) -> AccountingPeriod:
    from backend.services import accounting_posting as ap
    p = db.scalar(select(AccountingPeriod).where(AccountingPeriod.id == period_id).with_for_update().execution_options(populate_existing=True))
    if p is None:
        raise NotFoundError("Period not found")
    if p.status == "closed":
        raise ConflictError(f"{p.name} is already closed.")
    if p.end_date >= date.today():
        raise BusinessRuleError(f"{p.name} has not ended yet; a period can be closed only after its last day.")
    drafts = db.scalar(select(func.count()).select_from(JournalEntry).where(JournalEntry.period_id == p.id, JournalEntry.status == "draft")) or 0
    if drafts:
        raise BusinessRuleError(f"{drafts} draft journal(s) in {p.name} must be posted or deleted before closing.")
    pending = ap.pending_count(db, up_to=p.end_date)
    if pending:
        raise BusinessRuleError(f"{pending} source document(s) dated up to {p.end_date} have not been posted to the ledger yet. Run posting first.")
    p.status, p.closed_by, p.closed_at = "closed", actor.id, datetime.now(timezone.utc)
    db.flush()
    audit_service.record(db, actor=actor, action="accounting_period.closed", entity_type="accounting_period", entity_id=p.id, after={"name": p.name}, ip=ip)
    return p


def request_reopen(db: Session, *, actor: User, period_id: int, reason: str, ip: str | None) -> PeriodRequest:
    p = db.get(AccountingPeriod, period_id)
    if p is None:
        raise NotFoundError("Period not found")
    if p.status != "closed":
        raise BusinessRuleError(f"{p.name} is not closed.")
    if not (reason or "").strip():
        raise BusinessRuleError("Say why the period must be reopened.")
    if db.scalar(select(PeriodRequest.id).where(PeriodRequest.period_id == p.id, PeriodRequest.status == "pending")):
        raise ConflictError(f"A reopening request for {p.name} is already waiting for a decision.")
    r = PeriodRequest(period_id=p.id, action="reopen", reason=reason.strip(), requested_by=actor.id)
    db.add(r)
    db.flush()
    audit_service.record(db, actor=actor, action="accounting_period.reopen_requested", entity_type="accounting_period", entity_id=p.id, reason=reason, ip=ip)
    return r


def decide_reopen(db: Session, *, actor: User, request_id: int, approve: bool, note: str, ip: str | None) -> PeriodRequest:
    r = db.scalar(select(PeriodRequest).where(PeriodRequest.id == request_id).with_for_update().execution_options(populate_existing=True))
    if r is None:
        raise NotFoundError("Request not found")
    if r.status != "pending":
        raise ConflictError("This request has already been decided.")
    if r.requested_by == actor.id:
        raise SelfApprovalError("You cannot decide a reopening you requested yourself.")
    if not (note or "").strip():
        raise BusinessRuleError("A note is required.")
    r.status, r.decided_by, r.decided_at, r.decision_note = ("approved" if approve else "rejected"), actor.id, datetime.now(timezone.utc), note.strip()
    p = db.get(AccountingPeriod, r.period_id)
    if approve:
        p.status, p.reopened_count = "open", p.reopened_count + 1
    db.flush()
    audit_service.record(db, actor=actor, action=f"accounting_period.reopen_{'approved' if approve else 'rejected'}", entity_type="accounting_period",
                         entity_id=p.id, reason=note, before={"status": "closed"}, after={"status": p.status}, ip=ip)
    return r


# ---- journals ----------------------------------------------------------------------------------------------------------------------
@dataclass
class L:
    """One journal line request. Exactly one of debit/credit is non-zero."""
    account: str | int           # code (str) or id (int)
    debit: Decimal | int | str = 0
    credit: Decimal | int | str = 0
    memo: str | None = None
    party_type: str | None = None
    party_id: int | None = None
    location_id: int | None = None


def _ref(db: Session) -> str:
    return f"JE-{db.scalar(text('select nextval(:s)'), {'s': 'journal_ref_seq'}):06d}"


def _resolve(db: Session, ln: L, cache: dict) -> tuple[Account, Decimal, Decimal]:
    key = ln.account
    a = cache.get(key)
    if a is None:
        a = db.get(Account, key) if isinstance(key, int) else db.scalar(select(Account).where(Account.code == str(key)))
        if a is None:
            raise BusinessRuleError(f"Ledger account {key} does not exist.")
        cache[key] = a
    d, c = q2(ln.debit or 0), q2(ln.credit or 0)
    if d < 0 or c < 0 or (d > 0 and c > 0) or (d == 0 and c == 0):
        raise BusinessRuleError("Each line needs an amount on exactly one side (debit or credit).")
    return a, d, c


def validate_lines(db: Session, lines: list[L], *, manual: bool = False, opening: bool = False) -> list[tuple[Account, Decimal, Decimal, L]]:
    if len(lines) < 2:
        raise BusinessRuleError("A journal entry needs at least two lines.")
    cache: dict = {}
    out = []
    for ln in lines:
        a, d, c = _resolve(db, ln, cache)
        if not a.is_active:
            raise BusinessRuleError(f"{a.code} {a.name} is inactive.")
        if a.requires_party and (not ln.party_type or not ln.party_id) and not manual:
            raise BusinessRuleError(f"{a.code} {a.name} needs a customer, supplier, distributor or employee on each line.")
        if manual and a.is_control and not (opening and a.code in OPENING_CONTROL_ALLOWED):
            raise BusinessRuleError(f"{a.code} {a.name} is filled automatically from its sub-ledger; it cannot be posted to by hand.")
        if (opening or manual) and a.requires_party and not ln.party_id:
            raise BusinessRuleError(f"{a.code} {a.name} needs a party on each line.")
        out.append((a, d, c, ln))
    td, tc = sum((x[1] for x in out), ZERO), sum((x[2] for x in out), ZERO)
    if td != tc:
        raise BusinessRuleError(f"The entry does not balance: debits {td} and credits {tc} differ by {abs(td - tc)}.")
    if td <= 0:
        raise BusinessRuleError("The entry has no value.")
    return out


def find_by_source(db: Session, source_type: str, source_id: int, event: str) -> JournalEntry | None:
    return db.scalar(select(JournalEntry).where(JournalEntry.source_type == source_type, JournalEntry.source_id == source_id, JournalEntry.event == event))


def create_entry(db: Session, *, entry_date: date, memo: str, lines: list[L], kind: str, actor: User | None, status: str = "posted",
                 source: tuple[str, int, str] | None = None, reverses_id: int | None = None, reason: str | None = None,
                 manual: bool = False, opening: bool = False, shift_if_closed: bool = False) -> JournalEntry:
    ensure_chart(db)
    if source is not None:
        ex = find_by_source(db, *source)
        if ex is not None:
            raise ConflictError(f"{source[0]} #{source[1]} ({source[2]}) is already in the ledger as {ex.ref}.")
    checked = validate_lines(db, lines, manual=manual, opening=opening)
    original = None
    period = get_period(db, entry_date)
    if period.status == "closed" and status == "posted":
        if not shift_if_closed:
            raise BusinessRuleError(f"{period.name} is closed. Entries cannot be posted into it; ask for it to be reopened.")
        original = entry_date
        entry_date = first_open_date(db, entry_date)
        period = get_period(db, entry_date)
    elif period.status == "closed" and status == "draft":
        raise BusinessRuleError(f"{period.name} is closed.")
    total = sum((x[1] for x in checked), ZERO)
    now = datetime.now(timezone.utc)
    e = JournalEntry(ref=_ref(db), entry_date=entry_date, original_date=original, period_id=period.id, memo=memo[:300], kind=kind, status=status,
                     source_type=source[0] if source else None, source_id=source[1] if source else None, event=source[2] if source else None,
                     reverses_id=reverses_id, reason=reason, total=total, created_by=actor.id if actor else None,
                     posted_by=actor.id if (actor and status == "posted") else None, posted_at=now if status == "posted" else None)
    for a, d, c, ln in checked:
        e.lines.append(JournalLine(account_id=a.id, debit=d, credit=c, memo=(ln.memo or None) and ln.memo[:200], party_type=ln.party_type,
                                   party_id=ln.party_id, location_id=ln.location_id, entry_date=entry_date))
    db.add(e)
    db.flush()
    return e


def replace_draft_lines(db: Session, e: JournalEntry, lines: list[L], entry_date: date, memo: str) -> None:
    checked = validate_lines(db, lines, manual=True) if lines else []
    period = get_period(db, entry_date)
    if period.status == "closed":
        raise BusinessRuleError(f"{period.name} is closed.")
    e.lines.clear()
    db.flush()
    e.entry_date, e.period_id, e.memo = entry_date, period.id, memo[:300]
    e.total = sum((x[1] for x in checked), ZERO)
    for a, d, c, ln in checked:
        e.lines.append(JournalLine(account_id=a.id, debit=d, credit=c, memo=(ln.memo or None) and ln.memo[:200], party_type=ln.party_type,
                                   party_id=ln.party_id, location_id=ln.location_id, entry_date=entry_date))
    db.flush()


def mirror_lines(e: JournalEntry, factor: int = -1) -> list[L]:
    """Opposite lines of an entry (debits become credits)."""
    return [L(account=ln.account_id, debit=ln.credit, credit=ln.debit, memo=ln.memo, party_type=ln.party_type, party_id=ln.party_id,
              location_id=ln.location_id) for ln in e.lines]


def reverse_entry(db: Session, *, actor: User, entry_id: int, reason: str, ip: str | None) -> JournalEntry:
    e = db.scalar(select(JournalEntry).where(JournalEntry.id == entry_id).with_for_update().execution_options(populate_existing=True))
    if e is None:
        raise NotFoundError("Journal entry not found")
    if e.status != "posted":
        raise ConflictError(f"{e.ref} is {e.status} and cannot be reversed.")
    if e.kind == "system":
        raise BusinessRuleError(f"{e.ref} was generated from a source document. Correct the document (for example reverse the payment); "
                                "the ledger follows it.")
    if e.kind == "reversal":
        raise BusinessRuleError("A reversal cannot itself be reversed; post a new entry instead.")
    if e.posted_by == actor.id:
        raise SelfApprovalError("You cannot reverse an entry you posted yourself. Ask another authorised user.")
    if not (reason or "").strip():
        raise BusinessRuleError("Say why the entry is being reversed.")
    today = date.today()
    rev = create_entry(db, entry_date=today, memo=f"Reversal of {e.ref}: {reason.strip()}"[:300], lines=mirror_lines(e), kind="reversal", actor=actor,
                       reverses_id=e.id, reason=reason.strip())
    e.status = "reversed"
    db.flush()
    audit_service.record(db, actor=actor, action="journal.reversed", entity_type="journal_entry", entity_id=e.id, reason=reason,
                         after={"reversal": rev.ref}, ip=ip)
    return rev


def entry_view(db: Session, e: JournalEntry, with_lines: bool = True) -> dict:
    accts = {a.id: a for a in db.scalars(select(Account).where(Account.id.in_([ln.account_id for ln in e.lines] or [0]))).all()}
    out = {"id": e.id, "ref": e.ref, "entry_date": e.entry_date, "original_date": e.original_date, "memo": e.memo, "kind": e.kind, "status": e.status,
           "source_type": e.source_type, "source_id": e.source_id, "event": e.event, "reverses_id": e.reverses_id, "reason": e.reason,
           "total": str(e.total), "period": db.get(AccountingPeriod, e.period_id).name}
    if with_lines:
        out["lines"] = [{"id": ln.id, "account_id": ln.account_id, "account": f"{accts[ln.account_id].code} {accts[ln.account_id].name}",
                         "debit": str(ln.debit), "credit": str(ln.credit), "memo": ln.memo, "party_type": ln.party_type, "party_id": ln.party_id,
                         "location_id": ln.location_id} for ln in e.lines]
    return out


# ---- manual journals ---------------------------------------------------------------------------------------------------------------
def create_manual(db: Session, *, actor: User, entry_date: date, memo: str, lines: list[L], reason: str, ip: str | None) -> JournalEntry:
    if not (memo or "").strip() or not (reason or "").strip():
        raise BusinessRuleError("A manual journal needs a description and a reason.")
    if entry_date > date.today():
        raise BusinessRuleError("The date cannot be in the future.")
    validate_lines(db, lines, manual=True)
    e = create_entry(db, entry_date=entry_date, memo=memo.strip(), lines=lines, kind="manual", actor=actor, status="draft", reason=reason.strip(), manual=True)
    audit_service.record(db, actor=actor, action="journal.draft_created", entity_type="journal_entry", entity_id=e.id, reason=reason, after={"ref": e.ref}, ip=ip)
    return e


def post_manual(db: Session, *, actor: User, entry_id: int, ip: str | None) -> JournalEntry:
    e = db.scalar(select(JournalEntry).where(JournalEntry.id == entry_id).with_for_update().execution_options(populate_existing=True))
    if e is None:
        raise NotFoundError("Journal entry not found")
    if e.status != "draft" or e.kind != "manual":
        raise ConflictError(f"{e.ref} is {e.status}; only a draft manual journal can be posted.")
    if e.created_by == actor.id:
        raise SelfApprovalError("You cannot post a journal you prepared yourself. Ask another authorised user.")
    ls = [L(account=ln.account_id, debit=ln.debit, credit=ln.credit, memo=ln.memo, party_type=ln.party_type, party_id=ln.party_id,
            location_id=ln.location_id) for ln in e.lines]
    validate_lines(db, ls, manual=True)
    period = get_period(db, e.entry_date)
    if period.status == "closed":
        raise BusinessRuleError(f"{period.name} is closed. Reopen it first or change the date.")
    e.status, e.posted_by, e.posted_at = "posted", actor.id, datetime.now(timezone.utc)
    db.flush()
    audit_service.record(db, actor=actor, action="journal.posted", entity_type="journal_entry", entity_id=e.id, after={"ref": e.ref, "total": str(e.total)}, ip=ip)
    return e


def delete_draft(db: Session, *, actor: User, entry_id: int, ip: str | None) -> None:
    e = db.get(JournalEntry, entry_id)
    if e is None:
        raise NotFoundError("Journal entry not found")
    if e.status != "draft":
        raise ConflictError(f"{e.ref} is {e.status}; only drafts can be deleted.")
    audit_service.record(db, actor=actor, action="journal.draft_deleted", entity_type="journal_entry", entity_id=e.id, after={"ref": e.ref}, ip=ip)
    db.delete(e)
    db.flush()


def normal_side(a_type: str) -> str:
    return "debit" if a_type in DEBIT_NORMAL else "credit"


def json_dumps(x) -> str:
    return json.dumps(x, default=str)
