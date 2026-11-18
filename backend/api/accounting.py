"""Accounting: chart of accounts, journals, periods, expenses, cash and bank, reconciliation, opening balances, go-live and financial reports.

Permissions: view = accounting.view; posting/running the posting queue = accounting.post; approvals, reversals and go-live = accounting.approve;
chart and settings = accounting.manage_coa; period close/reopen request = accounting.close_period / accounting.reopen_period; reconciliation =
accounting.reconcile; opening balances = accounting.opening_balances; expenses = expenses.manage; reports = reports.financial or accounting.view."""
from __future__ import annotations

from datetime import date

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from backend.api.common import commit_or_http
from backend.db import get_db
from backend.deps import Principal, client_ip, ensure_location_access, require, require_any
from backend.models import (
    Account, AccountingPeriod, CashAccount, CashReconciliation, Expense, JournalEntry, JournalLine, OpeningBatch, PeriodRequest, User,
)
from backend.schemas import accounting as S
from backend.services import accounting_ops_service as ops
from backend.services import opening_import_service
from backend.services import accounting_posting as posting
from backend.services import accounting_reports as rep
from backend.services import accounting_service as acc
from backend.services.accounting_service import L
from backend.services.errors import BusinessRuleError, NotFoundError

router = APIRouter(prefix="/accounting", tags=["accounting"])
VIEW = Depends(require("accounting.view"))


def _nf(fn):
    try:
        return fn()
    except NotFoundError as e:
        raise HTTPException(404, str(e))
    except BusinessRuleError as e:
        raise HTTPException(422, str(e))


def _lines(items: list) -> list[L]:
    return [L(account=i.account_id if i.account_id else (i.account or ""), debit=i.debit, credit=i.credit, memo=i.memo, party_type=i.party_type, party_id=i.party_id,
              location_id=i.location_id) for i in items]


def _acct(a: Account, bal=None) -> dict:
    return {"id": a.id, "code": a.code, "name": a.name, "type": a.type, "category": a.category, "description": a.description, "is_system": a.is_system, "is_control": a.is_control,
            "requires_party": a.requires_party, "is_active": a.is_active, **({"balance": str(acc.q2(bal))} if bal is not None else {})}


def _names(db: Session, ids: set) -> dict[int, str]:
    ids = {i for i in ids if i}
    return {u.id: u.full_name for u in db.scalars(select(User).where(User.id.in_(ids))).all()} if ids else {}


# ---- overview and settings ---------------------------------------------------------------------------------------------------------------------
@router.get("/overview")
def overview(db: Session = Depends(get_db), p: Principal = VIEW):
    acc.ensure_chart(db)
    db.commit()
    cur = acc.get_period(db, date.today())
    db.commit()
    return {"settings": acc.accounting_settings(db), "pending": posting.pending_summary(db), "current_period": acc.period_view(cur),
            "drafts": db.scalar(select(func.count()).select_from(JournalEntry).where(JournalEntry.status == "draft")) or 0,
            "pending_expenses": db.scalar(select(func.count()).select_from(Expense).where(Expense.status == "pending")) or 0,
            "pending_reopen": db.scalar(select(func.count()).select_from(PeriodRequest).where(PeriodRequest.status == "pending")) or 0,
            "go_live": ops.go_live_checks(db)}


@router.get("/settings")
def get_settings(db: Session = Depends(get_db), p: Principal = VIEW):
    acc.ensure_chart(db)
    db.commit()
    return acc.accounting_settings(db)


@router.put("/settings")
def put_settings(body: S.SettingsIn, request: Request, db: Session = Depends(get_db), p: Principal = Depends(require("accounting.manage_coa"))):
    v = body.model_dump(exclude_unset=True)
    reason = v.pop("reason", None)
    return commit_or_http(db, lambda: acc.set_accounting_settings(db, actor=p.user, values=v, reason=reason, ip=client_ip(request)))


# ---- chart of accounts -------------------------------------------------------------------------------------------------------------------------
@router.get("/accounts")
def list_accounts(include_inactive: bool = False, db: Session = Depends(get_db), p: Principal = VIEW):
    acc.ensure_chart(db)
    db.commit()
    q = select(Account).order_by(Account.code)
    if not include_inactive:
        q = q.where(Account.is_active.is_(True))
    return {"items": [_acct(a, acc.account_balance(db, a.id)) for a in db.scalars(q).all()]}


@router.post("/accounts", status_code=201)
def add_account(body: S.AccountIn, request: Request, db: Session = Depends(get_db), p: Principal = Depends(require("accounting.manage_coa"))):
    a = commit_or_http(db, lambda: acc.create_account(db, actor=p.user, ip=client_ip(request), **body.model_dump()))
    return _acct(a)


@router.patch("/accounts/{account_id}")
def patch_account(account_id: int, body: S.AccountPatch, request: Request, db: Session = Depends(get_db), p: Principal = Depends(require("accounting.manage_coa"))):
    a = commit_or_http(db, lambda: acc.update_account(db, actor=p.user, account_id=account_id, ip=client_ip(request), **body.model_dump()))
    return _acct(a)


# ---- journals ------------------------------------------------------------------------------------------------------------------------------------
@router.get("/journals")
def list_journals(status: str | None = None, kind: str | None = None, date_from: date | None = None, date_to: date | None = None, source_type: str | None = None,
                  q: str | None = None, page: int = Query(1, ge=1), size: int = Query(50, ge=1, le=200), db: Session = Depends(get_db), p: Principal = VIEW):
    stmt = select(JournalEntry)
    if status:
        stmt = stmt.where(JournalEntry.status == status)
    if kind:
        stmt = stmt.where(JournalEntry.kind == kind)
    if source_type:
        stmt = stmt.where(JournalEntry.source_type == source_type)
    if date_from:
        stmt = stmt.where(JournalEntry.entry_date >= date_from)
    if date_to:
        stmt = stmt.where(JournalEntry.entry_date <= date_to)
    if q:
        stmt = stmt.where(JournalEntry.ref.ilike(f"%{q}%") | JournalEntry.memo.ilike(f"%{q}%"))
    total = db.scalar(select(func.count()).select_from(stmt.subquery())) or 0
    rows = db.scalars(stmt.order_by(JournalEntry.entry_date.desc(), JournalEntry.id.desc()).offset((page - 1) * size).limit(size)).all()
    return {"items": [acc.entry_view(db, e, with_lines=False) for e in rows], "total": total, "page": page, "size": size}


@router.get("/journals/{entry_id}")
def get_journal(entry_id: int, db: Session = Depends(get_db), p: Principal = VIEW):
    e = db.get(JournalEntry, entry_id)
    if e is None:
        raise HTTPException(404, "Journal entry not found")
    out = acc.entry_view(db, e)
    n = _names(db, {e.created_by, e.posted_by})
    out["created_by"], out["posted_by"] = n.get(e.created_by), n.get(e.posted_by)
    return out


@router.post("/journals/manual", status_code=201)
def manual_journal(body: S.ManualJournalIn, request: Request, db: Session = Depends(get_db), p: Principal = Depends(require("accounting.post"))):
    def go():
        e = acc.create_manual(db, actor=p.user, entry_date=body.entry_date, memo=body.memo, lines=_lines(body.lines), reason=body.reason, ip=client_ip(request))
        return acc.entry_view(db, e)
    return commit_or_http(db, go)


@router.post("/journals/{entry_id}/post")
def post_journal(entry_id: int, request: Request, db: Session = Depends(get_db), p: Principal = Depends(require("accounting.approve"))):
    return commit_or_http(db, lambda: acc.entry_view(db, acc.post_manual(db, actor=p.user, entry_id=entry_id, ip=client_ip(request))))


@router.delete("/journals/{entry_id}", status_code=204)
def delete_journal(entry_id: int, request: Request, db: Session = Depends(get_db), p: Principal = Depends(require("accounting.post"))):
    commit_or_http(db, lambda: acc.delete_draft(db, actor=p.user, entry_id=entry_id, ip=client_ip(request)))


@router.post("/journals/{entry_id}/reverse", status_code=201)
def reverse_journal(entry_id: int, body: S.ReasonIn, request: Request, db: Session = Depends(get_db), p: Principal = Depends(require("accounting.approve"))):
    return commit_or_http(db, lambda: acc.entry_view(db, acc.reverse_entry(db, actor=p.user, entry_id=entry_id, reason=body.reason, ip=client_ip(request))))


@router.get("/posting/pending")
def pending(db: Session = Depends(get_db), p: Principal = VIEW):
    acc.ensure_chart(db)
    db.commit()
    s = posting.pending_summary(db)
    return s


@router.post("/posting/run")
def run_posting(request: Request, db: Session = Depends(get_db), p: Principal = Depends(require("accounting.post"))):
    return commit_or_http(db, lambda: posting.post_pending(db, actor=p.user))


# ---- periods ---------------------------------------------------------------------------------------------------------------------------------------
@router.get("/periods")
def periods(fiscal_year: int | None = None, db: Session = Depends(get_db), p: Principal = VIEW):
    items = [acc.period_view(x) for x in acc.list_periods(db, fiscal_year)]
    db.commit()
    reqs = db.scalars(select(PeriodRequest).where(PeriodRequest.status == "pending").order_by(PeriodRequest.id)).all()
    n = _names(db, {r.requested_by for r in reqs})
    return {"items": items, "fiscal_year": fiscal_year or acc.fiscal_year_of(db, date.today()),
            "pending_requests": [{"id": r.id, "period_id": r.period_id, "reason": r.reason, "requested_by": n.get(r.requested_by), "requested_at": r.requested_at,
                                  "requested_by_id": r.requested_by} for r in reqs]}


@router.post("/periods/{period_id}/close")
def close(period_id: int, request: Request, db: Session = Depends(get_db), p: Principal = Depends(require("accounting.close_period"))):
    return commit_or_http(db, lambda: acc.period_view(acc.close_period(db, actor=p.user, period_id=period_id, ip=client_ip(request))))


@router.post("/periods/{period_id}/reopen-requests", status_code=201)
def reopen_request(period_id: int, body: S.ReasonIn, request: Request, db: Session = Depends(get_db), p: Principal = Depends(require("accounting.reopen_period"))):
    r = commit_or_http(db, lambda: acc.request_reopen(db, actor=p.user, period_id=period_id, reason=body.reason, ip=client_ip(request)))
    return {"id": r.id, "status": r.status}


@router.post("/reopen-requests/{request_id}/decision")
def reopen_decision(request_id: int, body: S.DecisionIn, request: Request, db: Session = Depends(get_db), p: Principal = Depends(require("accounting.approve"))):
    r = commit_or_http(db, lambda: acc.decide_reopen(db, actor=p.user, request_id=request_id, approve=body.approve, note=body.note, ip=client_ip(request)))
    return {"id": r.id, "status": r.status}


# ---- cash and bank ----------------------------------------------------------------------------------------------------------------------------------
@router.get("/cash-accounts")
def cash_accounts(db: Session = Depends(get_db), p: Principal = Depends(require_any("accounting.view", "expenses.manage"))):
    acc.ensure_chart(db)
    db.commit()
    return {"items": [ops.cash_account_view(db, c) for c in db.scalars(select(CashAccount).order_by(CashAccount.id)).all()]}


@router.post("/cash-accounts", status_code=201)
def add_cash_account(body: S.CashAccountIn, request: Request, db: Session = Depends(get_db), p: Principal = Depends(require("accounting.manage_coa"))):
    def go():
        c = ops.create_cash_account(db, actor=p.user, ip=client_ip(request), **body.model_dump())
        return ops.cash_account_view(db, c)
    return commit_or_http(db, go)


@router.post("/cash-accounts/{cash_account_id}/deactivate")
def deactivate_cash(cash_account_id: int, request: Request, db: Session = Depends(get_db), p: Principal = Depends(require("accounting.manage_coa"))):
    return commit_or_http(db, lambda: ops.cash_account_view(db, ops.deactivate_cash_account(db, actor=p.user, cash_account_id=cash_account_id, ip=client_ip(request))))


@router.get("/cash-accounts/{cash_account_id}/unreconciled")
def unreconciled(cash_account_id: int, up_to: date | None = None, db: Session = Depends(get_db), p: Principal = Depends(require("accounting.reconcile"))):
    return {"items": _nf(lambda: ops.unreconciled_lines(db, cash_account_id, up_to)), "cleared_balance": str(ops.cleared_balance(db, cash_account_id))}


@router.post("/cash-accounts/{cash_account_id}/reconcile/preview")
def reconcile_preview(cash_account_id: int, body: S.ReconcileIn, db: Session = Depends(get_db), p: Principal = Depends(require("accounting.reconcile"))):
    return _nf(lambda: ops.reconcile_preview(db, cash_account_id=cash_account_id, statement_date=body.statement_date, statement_balance=body.statement_balance, line_ids=body.line_ids))


@router.post("/cash-accounts/{cash_account_id}/reconcile", status_code=201)
def reconcile(cash_account_id: int, body: S.ReconcileIn, request: Request, db: Session = Depends(get_db), p: Principal = Depends(require("accounting.reconcile"))):
    def go():
        r = ops.reconcile(db, actor=p.user, cash_account_id=cash_account_id, statement_date=body.statement_date, statement_balance=body.statement_balance,
                          line_ids=body.line_ids, notes=body.notes, ip=client_ip(request))
        return {"id": r.id, "statement_date": r.statement_date, "statement_balance": str(r.statement_balance), "status": r.status}
    return commit_or_http(db, go)


@router.get("/cash-accounts/{cash_account_id}/reconciliations")
def reconciliations(cash_account_id: int, db: Session = Depends(get_db), p: Principal = Depends(require("accounting.reconcile"))):
    rows = db.scalars(select(CashReconciliation).where(CashReconciliation.cash_account_id == cash_account_id).order_by(CashReconciliation.id.desc())).all()
    n = _names(db, {r.created_by for r in rows})
    return {"items": [{"id": r.id, "statement_date": r.statement_date, "statement_balance": str(r.statement_balance), "status": r.status, "notes": r.notes,
                       "created_by": n.get(r.created_by), "void_reason": r.void_reason} for r in rows]}


@router.post("/reconciliations/{reconciliation_id}/void")
def void_reconciliation(reconciliation_id: int, body: S.ReasonIn, request: Request, db: Session = Depends(get_db), p: Principal = Depends(require("accounting.approve"))):
    def go():
        r = ops.void_reconciliation(db, actor=p.user, reconciliation_id=reconciliation_id, reason=body.reason, ip=client_ip(request))
        return {"id": r.id, "status": r.status}
    return commit_or_http(db, go)


# ---- expenses ---------------------------------------------------------------------------------------------------------------------------------------
@router.get("/expenses")
def list_expenses(status: str | None = None, date_from: date | None = None, date_to: date | None = None, page: int = Query(1, ge=1), size: int = Query(50, ge=1, le=200),
                  db: Session = Depends(get_db), p: Principal = Depends(require_any("expenses.manage", "accounting.view"))):
    stmt = select(Expense)
    if status:
        stmt = stmt.where(Expense.status == status)
    if date_from:
        stmt = stmt.where(Expense.expense_date >= date_from)
    if date_to:
        stmt = stmt.where(Expense.expense_date <= date_to)
    total = db.scalar(select(func.count()).select_from(stmt.subquery())) or 0
    rows = db.scalars(stmt.order_by(Expense.expense_date.desc(), Expense.id.desc()).offset((page - 1) * size).limit(size)).all()
    return {"items": [ops.expense_view(db, x) for x in rows], "total": total, "page": page, "size": size}


@router.get("/expense-accounts")
def expense_accounts(db: Session = Depends(get_db), p: Principal = Depends(require_any("expenses.manage", "accounting.view"))):
    acc.ensure_chart(db)
    db.commit()
    return {"items": [_acct(a) for a in db.scalars(select(Account).where(Account.type == "expense", Account.is_control.is_(False), Account.is_active.is_(True),
                                                                         Account.category == "operating_expense").order_by(Account.code)).all()]}


@router.post("/expenses", status_code=201)
def add_expense(body: S.ExpenseIn, request: Request, db: Session = Depends(get_db), p: Principal = Depends(require("expenses.manage"))):
    if body.location_id:
        ensure_location_access(p, body.location_id)

    def go():
        x = ops.create_expense(db, actor=p.user, can_approve=p.has("accounting.approve"), ip=client_ip(request), **body.model_dump())
        return ops.expense_view(db, x)
    return commit_or_http(db, go)


@router.post("/expenses/{expense_id}/decision")
def decide_expense(expense_id: int, body: S.DecisionIn, request: Request, db: Session = Depends(get_db), p: Principal = Depends(require("accounting.approve"))):
    return commit_or_http(db, lambda: ops.expense_view(db, ops.decide_expense(db, actor=p.user, expense_id=expense_id, approve=body.approve, note=body.note, ip=client_ip(request))))


@router.post("/expenses/{expense_id}/void")
def void_expense(expense_id: int, body: S.ReasonIn, request: Request, db: Session = Depends(get_db), p: Principal = Depends(require("accounting.approve"))):
    return commit_or_http(db, lambda: ops.expense_view(db, ops.void_expense(db, actor=p.user, expense_id=expense_id, reason=body.reason, ip=client_ip(request))))


# ---- opening balances and go-live --------------------------------------------------------------------------------------------------------------------
@router.get("/opening-batches")
def list_batches(db: Session = Depends(get_db), p: Principal = Depends(require_any("accounting.opening_balances", "accounting.approve", "accounting.view"))):
    return {"items": [ops.batch_view(db, b) for b in db.scalars(select(OpeningBatch).order_by(OpeningBatch.id.desc())).all()]}


@router.post("/opening-batches", status_code=201)
def create_batch(body: S.OpeningBatchIn, request: Request, db: Session = Depends(get_db), p: Principal = Depends(require("accounting.opening_balances"))):
    def go():
        b = ops.create_batch(db, actor=p.user, as_of_date=body.as_of_date, description=body.description, lines=[x.model_dump() for x in body.lines], ip=client_ip(request))
        return ops.batch_view(db, b)
    return commit_or_http(db, go)


@router.put("/opening-batches/{batch_id}")
def update_batch(batch_id: int, body: S.OpeningBatchPatch, request: Request, db: Session = Depends(get_db), p: Principal = Depends(require("accounting.opening_balances"))):
    def go():
        b = ops.update_batch(db, actor=p.user, batch_id=batch_id, as_of_date=body.as_of_date, description=body.description,
                             lines=None if body.lines is None else [x.model_dump() for x in body.lines], ip=client_ip(request))
        return ops.batch_view(db, b)
    return commit_or_http(db, go)


@router.post("/opening-batches/{batch_id}/cancel")
def cancel_batch(batch_id: int, request: Request, db: Session = Depends(get_db), p: Principal = Depends(require("accounting.opening_balances"))):
    return commit_or_http(db, lambda: ops.batch_view(db, ops.cancel_batch(db, actor=p.user, batch_id=batch_id, ip=client_ip(request))))


@router.post("/opening-batches/{batch_id}/approve")
def approve_batch(batch_id: int, request: Request, db: Session = Depends(get_db), p: Principal = Depends(require("accounting.approve"))):
    return commit_or_http(db, lambda: ops.batch_view(db, ops.approve_batch(db, actor=p.user, batch_id=batch_id, ip=client_ip(request))))


OPENING_IMPORT = ("accounting.opening_balances", "inventory.opening_stock", "distributors.manage", "distributors.approve")   # ALL: it enters stock, distributor balances and a batch


@router.get("/opening-import/template")
def opening_import_template(p: Principal = Depends(require(*OPENING_IMPORT))):
    from fastapi.responses import Response
    return Response(opening_import_service.build_template_xlsx(), media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    headers={"Content-Disposition": 'attachment; filename="opening-balances-template.xlsx"', "Cache-Control": "no-store"})


@router.post("/opening-import")
async def opening_import(request: Request, as_of: date = Query(...), dry_run: bool = Query(True), db: Session = Depends(get_db), p: Principal = Depends(require(*OPENING_IMPORT))):
    """The workbook is the raw request body. dry_run (the default) enters everything for real and rolls it back, so a check can never disagree with the real run."""
    body = await request.body()
    return commit_or_http(db, lambda: opening_import_service.run(db, actor=p.user, content=body, as_of=as_of, dry_run=dry_run, ip=client_ip(request)))


@router.get("/integrity")
def integrity(db: Session = Depends(get_db), p: Principal = VIEW):
    return ops.subledger_reconciliation(db)


@router.get("/go-live")
def go_live_status(db: Session = Depends(get_db), p: Principal = VIEW):
    return ops.go_live_checks(db)


@router.post("/go-live")
def go_live(body: S.NoteIn, request: Request, db: Session = Depends(get_db), p: Principal = Depends(require("accounting.approve"))):
    return commit_or_http(db, lambda: ops.go_live(db, actor=p.user, note=body.note, ip=client_ip(request)))


# ---- reports ----------------------------------------------------------------------------------------------------------------------------------------------
def _loc(p: Principal, location_id: int | None):
    if location_id:
        ensure_location_access(p, location_id)
    return location_id


def _range(date_from: date | None, date_to: date | None) -> tuple[date, date]:
    t = date_to or date.today()
    return date_from or date(t.year, t.month, 1), t


RP = Depends(require_any("reports.financial", "accounting.view"))


@router.get("/reports/general-ledger")
def r_gl(account_id: int, date_from: date | None = None, date_to: date | None = None, location_id: int | None = None, db: Session = Depends(get_db), p: Principal = RP):
    f, t = _range(date_from, date_to)
    return _nf(lambda: rep.general_ledger(db, account_id=account_id, date_from=f, date_to=t, location_id=_loc(p, location_id)))


@router.get("/reports/trial-balance")
def r_tb(as_of: date | None = None, location_id: int | None = None, include_zero: bool = False, db: Session = Depends(get_db), p: Principal = RP):
    return rep.trial_balance(db, as_of=as_of or date.today(), location_id=_loc(p, location_id), include_zero=include_zero)


@router.get("/reports/income-statement")
def r_pl(date_from: date | None = None, date_to: date | None = None, location_id: int | None = None, db: Session = Depends(get_db), p: Principal = RP):
    f, t = _range(date_from, date_to)
    return _nf(lambda: rep.income_statement(db, date_from=f, date_to=t, location_id=_loc(p, location_id)))


@router.get("/reports/balance-sheet")
def r_bs(as_of: date | None = None, db: Session = Depends(get_db), p: Principal = RP):
    return rep.balance_sheet(db, as_of=as_of or date.today())


@router.get("/reports/cash-flow")
def r_cf(date_from: date | None = None, date_to: date | None = None, db: Session = Depends(get_db), p: Principal = RP):
    f, t = _range(date_from, date_to)
    return _nf(lambda: rep.cash_flow(db, date_from=f, date_to=t))


@router.get("/reports/aging/{kind}")
def r_aging(kind: str, as_of: date | None = None, db: Session = Depends(get_db), p: Principal = RP):
    return _nf(lambda: rep.aging(db, kind=kind, as_of=as_of or date.today()))


@router.get("/reports/statement")
def r_statement(party_type: str, party_id: int, date_from: date | None = None, date_to: date | None = None, db: Session = Depends(get_db), p: Principal = RP):
    f, t = _range(date_from, date_to)
    return _nf(lambda: rep.party_statement(db, party_type=party_type, party_id=party_id, date_from=f, date_to=t))


@router.get("/reports/sales")
def r_sales(date_from: date | None = None, date_to: date | None = None, location_id: int | None = None, db: Session = Depends(get_db), p: Principal = RP):
    f, t = _range(date_from, date_to)
    return rep.sales_report(db, date_from=f, date_to=t, location_id=_loc(p, location_id))


@router.get("/reports/expenses")
def r_exp(date_from: date | None = None, date_to: date | None = None, location_id: int | None = None, db: Session = Depends(get_db), p: Principal = RP):
    f, t = _range(date_from, date_to)
    return rep.expense_summary(db, date_from=f, date_to=t, location_id=_loc(p, location_id))


@router.get("/reports/cogs")
def r_cogs(date_from: date | None = None, date_to: date | None = None, location_id: int | None = None, db: Session = Depends(get_db), p: Principal = RP):
    f, t = _range(date_from, date_to)
    return rep.cogs_gross_profit(db, date_from=f, date_to=t, location_id=_loc(p, location_id))


@router.get("/reports/inventory-valuation")
def r_inv(location_id: int | None = None, db: Session = Depends(get_db), p: Principal = RP):
    return rep.inventory_valuation(db, location_id=_loc(p, location_id))


@router.get("/reports/monthly")
def r_monthly(fiscal_year: int | None = None, location_id: int | None = None, db: Session = Depends(get_db), p: Principal = RP):
    return rep.monthly_summary(db, fiscal_year=fiscal_year or acc.fiscal_year_of(db, date.today()), location_id=_loc(p, location_id))


@router.get("/reports/annual")
def r_annual(db: Session = Depends(get_db), p: Principal = RP):
    return rep.annual_summary(db)
