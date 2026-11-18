from datetime import date
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field


class _In(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class SettingsIn(_In):
    revenue_recognition: str | None = None
    fiscal_year_start_month: int | None = Field(default=None, ge=1, le=12)
    expense_approval_limit: Decimal | None = Field(default=None, ge=0)
    method_accounts: dict[str, int] | None = None
    reason: str | None = Field(default=None, max_length=500)


class AccountIn(_In):
    code: str = Field(min_length=1, max_length=12)
    name: str = Field(min_length=1, max_length=120)
    type: str
    category: str | None = Field(default=None, max_length=40)
    description: str | None = Field(default=None, max_length=300)
    requires_party: bool = False


class AccountPatch(_In):
    name: str | None = Field(default=None, max_length=120)
    description: str | None = Field(default=None, max_length=300)
    is_active: bool | None = None


class LineIn(_In):
    account_id: int | None = None
    account: str | None = None
    debit: Decimal = Field(default=Decimal(0), ge=0)
    credit: Decimal = Field(default=Decimal(0), ge=0)
    memo: str | None = Field(default=None, max_length=200)
    party_type: str | None = None
    party_id: int | None = None
    location_id: int | None = None


class ManualJournalIn(_In):
    entry_date: date
    memo: str = Field(min_length=1, max_length=300)
    reason: str = Field(min_length=1, max_length=500)
    lines: list[LineIn] = Field(min_length=2, max_length=60)


class ReasonIn(_In):
    reason: str = Field(min_length=1, max_length=500)


class NoteIn(_In):
    note: str = Field(min_length=1, max_length=1000)


class DecisionIn(_In):
    approve: bool
    note: str = Field(min_length=1, max_length=500)


class ExpenseIn(_In):
    expense_date: date
    account_id: int
    cash_account_id: int
    amount: Decimal = Field(gt=0)
    payee: str | None = Field(default=None, max_length=120)
    description: str = Field(min_length=1, max_length=300)
    receipt_ref: str | None = Field(default=None, max_length=80)
    location_id: int | None = None
    idempotency_key: str | None = Field(default=None, max_length=64)


class CashAccountIn(_In):
    name: str = Field(min_length=1, max_length=80)
    kind: str
    bank_name: str | None = Field(default=None, max_length=80)
    account_ref: str | None = Field(default=None, max_length=60)


class ReconcileIn(_In):
    statement_date: date
    statement_balance: Decimal
    line_ids: list[int] = Field(default_factory=list, max_length=2000)
    notes: str | None = Field(default=None, max_length=500)


class OpeningLineIn(_In):
    account_id: int | None = None
    account: str | None = None
    debit: Decimal = Field(default=Decimal(0), ge=0)
    credit: Decimal = Field(default=Decimal(0), ge=0)
    party_type: str | None = None
    party_id: int | None = None
    evidence: str | None = Field(default=None, max_length=300)
    verified: bool = False
    note: str | None = Field(default=None, max_length=200)


class OpeningBatchIn(_In):
    as_of_date: date
    description: str = Field(min_length=1, max_length=300)
    lines: list[OpeningLineIn] = Field(default_factory=list, max_length=500)


class OpeningBatchPatch(_In):
    as_of_date: date | None = None
    description: str | None = Field(default=None, max_length=300)
    lines: list[OpeningLineIn] | None = Field(default=None, max_length=500)
