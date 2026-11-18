"""Cash and bank accounts, expenses, bank reconciliation, opening-balance batches, sub-ledger reconciliation and go-live."""
from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal

from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from backend.models import (
    Account, CashAccount, CashReconciliation, Customer, Distributor, DistributorLedgerEntry, Expense, JournalEntry, JournalLine, OpeningBatch,
    OpeningLine, EmployeePurchase, EmployeePurchasePayment, StockCost, StockMovement, Supplier, SupplierLedgerEntry, User,
)
from backend.services import accounting_posting as ap
from backend.services import accounting_service as acc
from backend.services import audit_service, settings_service
from backend.services.accounting_service import L, q2
from backend.services.errors import BusinessRuleError, ConflictError, NotFoundError, SelfApprovalError

ZERO = Decimal(0)


# ---- cash and bank accounts ---------------------------------------------------------------------------------------------------------
def cash_account_view(db: Session, c: CashAccount) -> dict:
    a = db.get(Account, c.account_id)
    unrec = db.scalar(select(func.count()).select_from(JournalLine).join(JournalEntry, JournalEntry.id == JournalLine.entry_id).where(
        JournalLine.account_id == c.account_id, JournalLine.reconciliation_id.is_(None), JournalEntry.status.in_(("posted", "reversed")))) or 0
    last = db.scalar(select(CashReconciliation).where(CashReconciliation.cash_account_id == c.id, CashReconciliation.status == "completed")
                     .order_by(CashReconciliation.id.desc()).limit(1))
    return {"id": c.id, "name": c.name, "kind": c.kind, "account_id": c.account_id, "account_code": a.code, "bank_name": c.bank_name,
            "account_ref": c.account_ref, "is_active": c.is_active, "balance": str(q2(acc.account_balance(db, c.account_id))),
            "unreconciled_lines": unrec, "last_reconciled_to": last.statement_date if last else None}


def create_cash_account(db: Session, *, actor: User, name: str, kind: str, bank_name: str | None, account_ref: str | None, ip: str | None) -> CashAccount:
    acc.ensure_chart(db)
    name = (name or "").strip()
    if not name:
        raise BusinessRuleError("Give the account a name.")
    if kind not in ("cash", "bank"):
        raise BusinessRuleError("A new account is either cash or bank.")
    if db.scalar(select(CashAccount.id).where(func.lower(CashAccount.name) == name.lower())):
        raise ConflictError(f"An account called '{name}' already exists.")
    used = {a.code for a in db.scalars(select(Account)).all()}
    code = next((str(n) for n in range(1011, 1100) if str(n) not in used), None)
    if code is None:
        raise BusinessRuleError("No free ledger code remains between 1011 and 1099.")
    gl = acc.create_account(db, actor=actor, code=code, name=name, type="asset", category="current_asset", description=f"{kind} account", requires_party=False, ip=ip)
    ca = CashAccount(name=name, kind=kind, account_id=gl.id, bank_name=bank_name, account_ref=(account_ref or None) and account_ref[:60])
    db.add(ca)
    db.flush()
    audit_service.record(db, actor=actor, action="cash_account.created", entity_type="cash_account", entity_id=ca.id, after={"name": name, "kind": kind}, ip=ip)
    return ca


def deactivate_cash_account(db: Session, *, actor: User, cash_account_id: int, ip: str | None) -> CashAccount:
    ca = db.get(CashAccount, cash_account_id)
    if ca is None:
        raise NotFoundError("Cash account not found")
    if acc.account_balance(db, ca.account_id) != 0:
        raise BusinessRuleError("An account with a balance cannot be deactivated. Move the money out first.")
    methods = settings_service.get(db, acc.METHODS_KEY) or {}
    if ca.id in [int(v) for v in methods.values()] or ca.name in acc.DEFAULT_METHODS.values() and not methods:
        raise BusinessRuleError("This account receives payments for a payment method. Choose another account for it in settings first.")
    ca.is_active = False
    db.flush()
    audit_service.record(db, actor=actor, action="cash_account.deactivated", entity_type="cash_account", entity_id=ca.id, ip=ip)
    return ca


# ---- expenses -----------------------------------------------------------------------------------------------------------------------------
def expense_view(db: Session, x: Expense) -> dict:
    a, c = db.get(Account, x.account_id), db.get(CashAccount, x.cash_account_id)
    names = {u.id: u.full_name for u in db.scalars(select(User).where(User.id.in_([i for i in (x.created_by, x.decided_by, x.voided_by) if i] or [0]))).all()}
    return {"id": x.id, "ref": x.ref, "expense_date": x.expense_date, "account_id": x.account_id, "account": f"{a.code} {a.name}", "cash_account_id": x.cash_account_id,
            "paid_from": c.name, "amount": str(x.amount), "payee": x.payee, "description": x.description, "receipt_ref": x.receipt_ref, "location_id": x.location_id,
            "status": x.status, "created_by": names.get(x.created_by), "decided_by": names.get(x.decided_by), "decision_note": x.decision_note,
            "void_reason": x.void_reason}


def _post_expense(db: Session, x: Expense, actor: User) -> JournalEntry:
    ca = db.get(CashAccount, x.cash_account_id)
    return acc.create_entry(db, entry_date=x.expense_date, memo=f"{x.ref} {x.description}"[:300], kind="expense", actor=actor, source=("expense", x.id, "posted"),
                            lines=[L(account=x.account_id, debit=x.amount, location_id=x.location_id, memo=x.payee),
                                   L(account=ca.account_id, credit=x.amount, location_id=x.location_id)])


def create_expense(db: Session, *, actor: User, expense_date: date, account_id: int, cash_account_id: int, amount, payee: str | None, description: str,
                   receipt_ref: str | None, location_id: int | None, idempotency_key: str | None, can_approve: bool, ip: str | None) -> Expense:
    acc.ensure_chart(db)
    if idempotency_key:
        ex = db.scalar(select(Expense).where(Expense.idempotency_key == idempotency_key))
        if ex is not None:
            return ex
    amt = q2(amount)
    if amt <= 0:
        raise BusinessRuleError("The amount must be greater than zero.")
    if expense_date > date.today():
        raise BusinessRuleError("The expense date cannot be in the future.")
    if not (description or "").strip():
        raise BusinessRuleError("Describe the expense.")
    a, c = db.get(Account, account_id), db.get(CashAccount, cash_account_id)
    if a is None or a.type != "expense" or not a.is_active or a.is_control:
        raise BusinessRuleError("Choose an active expense account.")
    if c is None or not c.is_active:
        raise BusinessRuleError("Choose the cash or bank account it was paid from.")
    if acc.get_period(db, expense_date).status == "closed":
        raise BusinessRuleError(f"{acc.get_period(db, expense_date).name} is closed; expenses cannot be dated into it.")
    x = Expense(ref=f"EXP-{db.scalar(text('select nextval(:s)'), {'s': 'expense_ref_seq'}):06d}", expense_date=expense_date, account_id=a.id, cash_account_id=c.id,
                amount=amt, payee=payee, description=description.strip(), receipt_ref=receipt_ref, location_id=location_id, idempotency_key=idempotency_key,
                created_by=actor.id, status="pending")
    db.add(x)
    db.flush()
    limit = Decimal(str(settings_service.get(db, acc.EXPENSE_LIMIT_KEY) or 0))
    if amt <= limit:
        _post_expense(db, x, actor)
        x.status, x.decided_by, x.decided_at, x.decision_note = "posted", actor.id, datetime.now(timezone.utc), "Within the approval-free limit"
    audit_service.record(db, actor=actor, action="expense.created", entity_type="expense", entity_id=x.id, after={"ref": x.ref, "amount": str(amt), "status": x.status}, ip=ip)
    return x


def decide_expense(db: Session, *, actor: User, expense_id: int, approve: bool, note: str, ip: str | None) -> Expense:
    x = db.scalar(select(Expense).where(Expense.id == expense_id).with_for_update().execution_options(populate_existing=True))
    if x is None:
        raise NotFoundError("Expense not found")
    if x.status != "pending":
        raise ConflictError(f"{x.ref} is {x.status}.")
    if x.created_by == actor.id:
        raise SelfApprovalError("You cannot approve an expense you recorded yourself.")
    if not (note or "").strip():
        raise BusinessRuleError("A note is required.")
    x.decided_by, x.decided_at, x.decision_note = actor.id, datetime.now(timezone.utc), note.strip()
    if approve:
        _post_expense(db, x, actor)
        x.status = "posted"
    else:
        x.status = "rejected"
    db.flush()
    audit_service.record(db, actor=actor, action=f"expense.{'approved' if approve else 'rejected'}", entity_type="expense", entity_id=x.id, reason=note, ip=ip)
    return x


def void_expense(db: Session, *, actor: User, expense_id: int, reason: str, ip: str | None) -> Expense:
    x = db.scalar(select(Expense).where(Expense.id == expense_id).with_for_update().execution_options(populate_existing=True))
    if x is None:
        raise NotFoundError("Expense not found")
    if x.status != "posted":
        raise ConflictError(f"{x.ref} is {x.status}; only a posted expense can be voided.")
    if x.created_by == actor.id or x.decided_by == actor.id:
        raise SelfApprovalError("You cannot void an expense you recorded or approved. Ask another authorised user.")
    if not (reason or "").strip():
        raise BusinessRuleError("Say why the expense is being voided.")
    orig = acc.find_by_source(db, "expense", x.id, "posted")
    if any(ln.reconciliation_id for ln in orig.lines):
        raise BusinessRuleError(f"{x.ref} has already been matched on a bank reconciliation. Void that reconciliation first.")
    acc.create_entry(db, entry_date=date.today(), memo=f"{x.ref} voided: {reason.strip()}"[:300], kind="reversal", actor=actor, source=("expense", x.id, "void"),
                     lines=acc.mirror_lines(orig), reverses_id=orig.id, reason=reason.strip())
    orig.status = "reversed"
    x.status, x.voided_by, x.void_reason = "void", actor.id, reason.strip()
    db.flush()
    audit_service.record(db, actor=actor, action="expense.voided", entity_type="expense", entity_id=x.id, reason=reason, ip=ip)
    return x


# ---- bank reconciliation -------------------------------------------------------------------------------------------------------------------
def unreconciled_lines(db: Session, cash_account_id: int, up_to: date | None = None) -> list[dict]:
    ca = db.get(CashAccount, cash_account_id)
    if ca is None:
        raise NotFoundError("Cash account not found")
    q = select(JournalLine, JournalEntry).join(JournalEntry, JournalEntry.id == JournalLine.entry_id).where(
        JournalLine.account_id == ca.account_id, JournalLine.reconciliation_id.is_(None), JournalEntry.status.in_(("posted", "reversed")))
    if up_to:
        q = q.where(JournalLine.entry_date <= up_to)
    rows = db.execute(q.order_by(JournalLine.entry_date, JournalLine.id)).all()
    return [{"line_id": ln.id, "entry_date": ln.entry_date, "ref": e.ref, "memo": ln.memo or e.memo, "debit": str(ln.debit), "credit": str(ln.credit),
             "amount": str(q2(Decimal(ln.debit) - Decimal(ln.credit)))} for ln, e in rows]


def cleared_balance(db: Session, cash_account_id: int) -> Decimal:
    ca = db.get(CashAccount, cash_account_id)
    return q2(Decimal(db.scalar(select(func.coalesce(func.sum(JournalLine.debit - JournalLine.credit), 0)).where(
        JournalLine.account_id == ca.account_id, JournalLine.reconciliation_id.is_not(None)))))


def reconcile_preview(db: Session, *, cash_account_id: int, statement_date: date, statement_balance, line_ids: list[int]) -> dict:
    ca = db.get(CashAccount, cash_account_id)
    if ca is None:
        raise NotFoundError("Cash account not found")
    ids = sorted(set(line_ids))
    rows = db.scalars(select(JournalLine).where(JournalLine.id.in_(ids or [0])).with_for_update()).all()
    if len(rows) != len(ids):
        raise BusinessRuleError("A selected line does not exist.")
    for ln in rows:
        if ln.account_id != ca.account_id or ln.reconciliation_id is not None:
            raise BusinessRuleError("A selected line does not belong to this account or is already reconciled.")
        if ln.entry_date > statement_date:
            raise BusinessRuleError("A selected line is dated after the statement date.")
    base = cleared_balance(db, cash_account_id)
    picked = q2(sum((Decimal(ln.debit) - Decimal(ln.credit) for ln in rows), ZERO))
    expected = q2(base + picked)
    diff = q2(Decimal(str(statement_balance)) - expected)
    return {"previously_cleared": str(base), "selected_total": str(picked), "expected_statement_balance": str(expected),
            "statement_balance": str(q2(Decimal(str(statement_balance)))), "difference": str(diff), "balanced": diff == 0, "lines": len(rows)}


def reconcile(db: Session, *, actor: User, cash_account_id: int, statement_date: date, statement_balance, line_ids: list[int], notes: str | None,
              ip: str | None) -> CashReconciliation:
    if statement_date > date.today():
        raise BusinessRuleError("The statement date cannot be in the future.")
    if not line_ids:
        raise BusinessRuleError("Select the lines that appear on the statement.")
    pv = reconcile_preview(db, cash_account_id=cash_account_id, statement_date=statement_date, statement_balance=statement_balance, line_ids=line_ids)
    if not pv["balanced"]:
        raise BusinessRuleError(f"The selected lines do not agree with the statement: the difference is {pv['difference']}. "
                                "Find the missing or extra transaction; nothing is adjusted automatically.")
    r = CashReconciliation(cash_account_id=cash_account_id, statement_date=statement_date, statement_balance=q2(Decimal(str(statement_balance))),
                           cleared_balance=Decimal(pv["expected_statement_balance"]), notes=notes, created_by=actor.id)
    db.add(r)
    db.flush()
    for ln in db.scalars(select(JournalLine).where(JournalLine.id.in_(set(line_ids)))).all():
        ln.reconciliation_id = r.id
    db.flush()
    audit_service.record(db, actor=actor, action="cash_reconciliation.completed", entity_type="cash_account", entity_id=cash_account_id,
                         after={"statement_date": str(statement_date), "balance": pv["statement_balance"], "lines": pv["lines"]}, ip=ip)
    return r


def void_reconciliation(db: Session, *, actor: User, reconciliation_id: int, reason: str, ip: str | None) -> CashReconciliation:
    r = db.scalar(select(CashReconciliation).where(CashReconciliation.id == reconciliation_id).with_for_update().execution_options(populate_existing=True))
    if r is None:
        raise NotFoundError("Reconciliation not found")
    if r.status != "completed":
        raise ConflictError("This reconciliation is already voided.")
    later = db.scalar(select(CashReconciliation.id).where(CashReconciliation.cash_account_id == r.cash_account_id, CashReconciliation.status == "completed",
                                                          CashReconciliation.id > r.id))
    if later:
        raise BusinessRuleError("Only the most recent reconciliation of an account can be voided.")
    if not (reason or "").strip():
        raise BusinessRuleError("Say why the reconciliation is being voided.")
    for ln in db.scalars(select(JournalLine).where(JournalLine.reconciliation_id == r.id)).all():
        ln.reconciliation_id = None
    r.status, r.voided_by, r.voided_at, r.void_reason = "voided", actor.id, datetime.now(timezone.utc), reason.strip()
    db.flush()
    audit_service.record(db, actor=actor, action="cash_reconciliation.voided", entity_type="cash_account", entity_id=r.cash_account_id, reason=reason, ip=ip)
    return r


# ---- opening-balance batches ------------------------------------------------------------------------------------------------------------------------
PARTY_MODEL = {"customer": Customer, "supplier": Supplier, "distributor": Distributor}


def batch_view(db: Session, b: OpeningBatch) -> dict:
    accts = {a.id: a for a in db.scalars(select(Account).where(Account.id.in_([ln.account_id for ln in b.lines] or [0]))).all()}
    d = sum((Decimal(ln.debit) for ln in b.lines), ZERO)
    c = sum((Decimal(ln.credit) for ln in b.lines), ZERO)
    return {"id": b.id, "ref": b.ref, "as_of_date": b.as_of_date, "description": b.description, "status": b.status, "total_debit": str(q2(d)), "total_credit": str(q2(c)),
            "difference": str(q2(d - c)), "unverified": sum(1 for ln in b.lines if not ln.verified), "journal_id": b.journal_id,
            "lines": [{"id": ln.id, "account_id": ln.account_id, "account": f"{accts[ln.account_id].code} {accts[ln.account_id].name}", "debit": str(ln.debit),
                       "credit": str(ln.credit), "party_type": ln.party_type, "party_id": ln.party_id, "evidence": ln.evidence, "verified": ln.verified,
                       "note": ln.note} for ln in b.lines]}


def _opening_lines(db: Session, lines: list[dict]) -> list[OpeningLine]:
    out = []
    for raw in lines:
        a = db.get(Account, raw["account_id"]) if raw.get("account_id") else db.scalar(select(Account).where(Account.code == str(raw.get("account"))))
        if a is None:
            raise BusinessRuleError("An opening line names an account that does not exist.")
        if not a.is_active:
            raise BusinessRuleError(f"{a.code} {a.name} is inactive.")
        if a.is_control and a.code not in acc.OPENING_CONTROL_ALLOWED:
            raise BusinessRuleError(f"{a.code} {a.name} is filled from its own module (stock, suppliers or distributors). Enter the opening there, not here.")
        d, c = q2(raw.get("debit") or 0), q2(raw.get("credit") or 0)
        if d < 0 or c < 0 or (d > 0) == (c > 0):
            raise BusinessRuleError(f"{a.code}: enter either a debit or a credit amount.")
        pt, pid = raw.get("party_type"), raw.get("party_id")
        if a.requires_party:
            if pt not in PARTY_MODEL or not pid or db.get(PARTY_MODEL[pt], pid) is None:
                raise BusinessRuleError(f"{a.code} {a.name} needs an existing customer, supplier or distributor on each line.")
        else:
            pt = pid = None
        out.append(OpeningLine(account_id=a.id, debit=d, credit=c, party_type=pt, party_id=pid, evidence=(raw.get("evidence") or None),
                               verified=bool(raw.get("verified")), note=raw.get("note")))
    return out


def create_batch(db: Session, *, actor: User, as_of_date: date, description: str, lines: list[dict], ip: str | None) -> OpeningBatch:
    acc.ensure_chart(db)
    if not (description or "").strip():
        raise BusinessRuleError("Describe the batch, for example 'Opening balances at 1 July from the paper registers'.")
    if as_of_date > date.today():
        raise BusinessRuleError("Opening balances cannot be dated in the future.")
    b = OpeningBatch(ref=f"OPB-{db.scalar(text('select nextval(:s)'), {'s': 'opening_ref_seq'}):04d}", as_of_date=as_of_date, description=description.strip(), created_by=actor.id)
    b.lines = _opening_lines(db, lines)
    db.add(b)
    db.flush()
    audit_service.record(db, actor=actor, action="opening_batch.created", entity_type="opening_batch", entity_id=b.id, after={"ref": b.ref}, ip=ip)
    return b


def update_batch(db: Session, *, actor: User, batch_id: int, as_of_date: date | None, description: str | None, lines: list[dict] | None, ip: str | None) -> OpeningBatch:
    b = db.scalar(select(OpeningBatch).where(OpeningBatch.id == batch_id).with_for_update().execution_options(populate_existing=True))
    if b is None:
        raise NotFoundError("Batch not found")
    if b.status != "draft":
        raise ConflictError(f"{b.ref} is {b.status}; only a draft can be edited.")
    if as_of_date:
        b.as_of_date = as_of_date
    if description:
        b.description = description.strip()
    if lines is not None:
        b.lines.clear()
        db.flush()
        b.lines = _opening_lines(db, lines)
    db.flush()
    audit_service.record(db, actor=actor, action="opening_batch.updated", entity_type="opening_batch", entity_id=b.id, ip=ip)
    return b


def cancel_batch(db: Session, *, actor: User, batch_id: int, ip: str | None) -> OpeningBatch:
    b = db.get(OpeningBatch, batch_id)
    if b is None:
        raise NotFoundError("Batch not found")
    if b.status != "draft":
        raise ConflictError(f"{b.ref} is {b.status}; only a draft can be cancelled.")
    b.status = "cancelled"
    db.flush()
    audit_service.record(db, actor=actor, action="opening_batch.cancelled", entity_type="opening_batch", entity_id=b.id, ip=ip)
    return b


def approve_batch(db: Session, *, actor: User, batch_id: int, ip: str | None) -> OpeningBatch:
    b = db.scalar(select(OpeningBatch).where(OpeningBatch.id == batch_id).with_for_update().execution_options(populate_existing=True))
    if b is None:
        raise NotFoundError("Batch not found")
    if b.status != "draft":
        raise ConflictError(f"{b.ref} is {b.status}.")
    if b.created_by == actor.id:
        raise SelfApprovalError("You cannot approve opening balances you entered yourself. Ask another authorised user.")
    if not b.lines:
        raise BusinessRuleError("The batch has no lines.")
    unverified = [ln for ln in b.lines if not ln.verified]
    if unverified:
        raise BusinessRuleError(f"{len(unverified)} line(s) are not marked verified. Verify each against its source document, or remove it; unverified figures are not posted.")
    v = batch_view(db, b)
    if Decimal(v["difference"]) != 0:
        raise BusinessRuleError(f"The batch does not balance: debits {v['total_debit']}, credits {v['total_credit']}. Add the owner's capital or opening equity line "
                                "that explains the difference; nothing is plugged automatically.")
    lines = [L(account=ln.account_id, debit=ln.debit, credit=ln.credit, memo=ln.note or ln.evidence, party_type=ln.party_type, party_id=ln.party_id) for ln in b.lines]
    e = acc.create_entry(db, entry_date=b.as_of_date, memo=f"{b.ref} {b.description}", lines=lines, kind="opening", actor=actor, source=("opening_batch", b.id, "posted"),
                         manual=True, opening=True)
    b.status, b.approved_by, b.approved_at, b.journal_id = "posted", actor.id, datetime.now(timezone.utc), e.id
    db.flush()
    audit_service.record(db, actor=actor, action="opening_batch.posted", entity_type="opening_batch", entity_id=b.id, after={"ref": b.ref, "journal": e.ref}, ip=ip)
    return b


# ---- sub-ledger reconciliation and go-live ------------------------------------------------------------------------------------------------------------------
def subledger_reconciliation(db: Session) -> dict:
    acc.ensure_chart(db)
    amap = acc.accounts_map(db)
    gl = lambda code: q2(acc.account_balance(db, amap[code].id))  # noqa: E731
    inv_value = q2(Decimal(db.scalar(select(func.coalesce(func.sum(StockCost.total_value), 0)))))
    transit = q2(Decimal(db.scalar(select(func.coalesce(func.sum(StockMovement.value_delta), 0)).where(StockMovement.bucket == "transit"))))
    sup = q2(Decimal(db.scalar(select(func.coalesce(func.sum(SupplierLedgerEntry.amount), 0)))))
    dist = q2(Decimal(db.scalar(select(func.coalesce(func.sum(DistributorLedgerEntry.amount), 0)))))
    emp_owed = Decimal(db.scalar(select(func.coalesce(func.sum(EmployeePurchase.total), 0)).where(EmployeePurchase.status == "completed")))
    emp_paid = Decimal(db.scalar(select(func.coalesce(func.sum(EmployeePurchasePayment.amount), 0)).join(
        EmployeePurchase, EmployeePurchase.id == EmployeePurchasePayment.purchase_id).where(
        EmployeePurchasePayment.status == "posted", EmployeePurchase.status == "completed")))
    emp = q2(emp_owed - emp_paid)
    td = Decimal(db.scalar(select(func.coalesce(func.sum(JournalLine.debit), 0))))
    tc = Decimal(db.scalar(select(func.coalesce(func.sum(JournalLine.credit), 0))))
    rows = [{"name": "Inventory (stock ledger valuation)", "ledger": str(gl("1200")), "subledger": str(inv_value)},
            {"name": "Stock in transit", "ledger": str(gl("1210")), "subledger": str(transit)},
            {"name": "Supplier payables (supplier ledger)", "ledger": str(-gl("2000")), "subledger": str(sup)},
            {"name": "Distributor receivables (distributor ledger)", "ledger": str(gl("1110")), "subledger": str(dist)},
            {"name": "Employee receivables (employee purchases)", "ledger": str(gl("1400")), "subledger": str(emp)}]
    for r in rows:
        r["difference"] = str(q2(Decimal(r["ledger"]) - Decimal(r["subledger"])))
        r["agrees"] = Decimal(r["difference"]) == 0
    pending = ap.pending_summary(db)
    return {"rows": rows, "trial_balance": {"debits": str(q2(td)), "credits": str(q2(tc)), "balanced": q2(td) == q2(tc)}, "pending_sources": pending,
            "consistent": all(r["agrees"] for r in rows) and q2(td) == q2(tc) and pending["total"] == 0}


def go_live_checks(db: Session) -> dict:
    sr = subledger_reconciliation(db)
    amap = acc.accounts_map(db) if db.scalar(select(func.count()).select_from(Account)) else {}
    obe = q2(acc.account_balance(db, amap["3200"].id)) if "3200" in amap else ZERO
    drafts = db.scalar(select(func.count()).select_from(OpeningBatch).where(OpeningBatch.status == "draft")) or 0
    posted = db.scalar(select(func.count()).select_from(OpeningBatch).where(OpeningBatch.status == "posted")) or 0
    checks = [
        {"name": "Every operational record is posted to the ledger", "ok": sr["pending_sources"]["total"] == 0, "detail": f"{sr['pending_sources']['total']} pending"},
        {"name": "Ledger agrees with stock, supplier, distributor and employee records", "ok": all(r["agrees"] for r in sr["rows"]), "detail": ", ".join(r["name"] for r in sr["rows"] if not r["agrees"]) or "all agree"},
        {"name": "Trial balance balances", "ok": sr["trial_balance"]["balanced"], "detail": f"{sr['trial_balance']['debits']} / {sr['trial_balance']['credits']}"},
        {"name": "At least one opening-balance batch has been approved", "ok": posted > 0, "detail": f"{posted} posted"},
        {"name": "No draft opening-balance batches are waiting", "ok": drafts == 0, "detail": f"{drafts} draft"},
        {"name": "Opening balance equity is cleared to owner capital", "ok": obe == 0, "detail": f"balance {obe}; enter a documented line moving it to Owner capital or equity"},
    ]
    return {"checks": checks, "ready": all(c["ok"] for c in checks), "ledger_status": settings_service.get(db, acc.LEDGER_STATUS_KEY)}


def go_live(db: Session, *, actor: User, note: str, ip: str | None) -> dict:
    if settings_service.get(db, acc.LEDGER_STATUS_KEY) == "official":
        raise ConflictError("The ledger is already official.")
    if len((note or "").strip()) < 20:
        raise BusinessRuleError("Write the reconciliation statement: who checked the opening balances against which documents (at least a sentence).")
    chk = go_live_checks(db)
    if not chk["ready"]:
        failing = "; ".join(c["name"] for c in chk["checks"] if not c["ok"])
        raise BusinessRuleError(f"The ledger cannot become official yet: {failing}.")
    drafts_by = db.scalars(select(OpeningBatch.created_by).where(OpeningBatch.status == "posted")).all()
    if drafts_by and all(u == actor.id for u in drafts_by):
        raise SelfApprovalError("Someone other than the person who entered the opening balances must declare the ledger official.")
    settings_service._set(db, acc.LEDGER_STATUS_KEY, "official", actor)
    settings_service._set(db, acc.GO_LIVE_KEY, {"by": actor.id, "at": datetime.now(timezone.utc).isoformat(), "note": note.strip()}, actor)
    audit_service.record(db, actor=actor, action="accounting.go_live", entity_type="setting", entity_id="accounting", reason=note, ip=ip)
    return go_live_checks(db)
