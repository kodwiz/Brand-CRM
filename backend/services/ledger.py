"""The stock ledger posting engine.

Every change to stock goes through `post()`. It locks the affected balance and cost rows in a global order
(so concurrent transactions cannot deadlock or oversell), applies weighted-average costing, refuses negative
stock, and writes immutable movement lines. See docs/inventory-costing.md.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import ROUND_HALF_UP, Decimal

from sqlalchemy import select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from backend.models import StockBalance, StockCost, StockMovement, StockTransaction, User
from backend.models.inventory import BUCKETS, VALUED_BUCKETS
from backend.services.errors import BusinessRuleError, ConflictError, InsufficientStockError

Q4 = Decimal("0.0001")
ZERO = Decimal("0")
BUCKET_ATTR = {b: f"qty_{b}" for b in BUCKETS}


def q4(x: Decimal | int | str) -> Decimal:
    return Decimal(x).quantize(Q4, rounding=ROUND_HALF_UP)


@dataclass
class Spec:
    """One requested ledger change.

    mode:
      in        external inflow into a valued bucket (receipt, opening, return, gain). Needs unit_cost or value.
      out       external outflow from a valued bucket (sale, dispatch, write-off, loss) at the current average.
      move      move qty between two valued buckets at the same location at the current average (bucket -> to_bucket).
      transit   change in the transit bucket; `value` is the magnitude (cost fixed when it was dispatched). qty sign = direction.
      raw       exact replay for reversals: signed qty, signed value, explicit cost_effect.
    """

    location_id: int
    variant_id: int
    batch_id: int | None
    bucket: str
    qty: int
    mode: str
    unit_cost: Decimal | None = None
    value: Decimal | None = None
    to_bucket: str | None = None
    cost_effect: str | None = None  # raw mode only


def _key(loc: int, var: int, batch: int | None) -> tuple[int, int, int]:
    return (loc, var, batch or 0)


def ensure_balance(db: Session, loc: int, var: int, batch: int | None) -> None:
    db.execute(pg_insert(StockBalance).values(location_id=loc, variant_id=var, batch_id=batch).on_conflict_do_nothing())


def ensure_cost(db: Session, loc: int, var: int) -> None:
    db.execute(pg_insert(StockCost).values(location_id=loc, variant_id=var).on_conflict_do_nothing())


def lock_balance(db: Session, loc: int, var: int, batch: int | None) -> StockBalance:
    ensure_balance(db, loc, var, batch)
    q = select(StockBalance).where(StockBalance.location_id == loc, StockBalance.variant_id == var)
    q = q.where(StockBalance.batch_id.is_(None)) if batch is None else q.where(StockBalance.batch_id == batch)
    return db.scalars(q.with_for_update().execution_options(populate_existing=True)).one()


def lock_cost(db: Session, loc: int, var: int) -> StockCost:
    ensure_cost(db, loc, var)
    return db.scalars(select(StockCost).where(StockCost.location_id == loc, StockCost.variant_id == var)
                      .with_for_update().execution_options(populate_existing=True)).one()


def avg_value(cost: StockCost, qty: int) -> Decimal:
    """Value of `qty` units at the current weighted average. Taking every unit takes the exact remaining value."""
    if cost.valued_qty <= 0 or qty > cost.valued_qty:
        raise InsufficientStockError("Not enough owned stock at this location to value this movement.")
    if qty == cost.valued_qty:
        return Decimal(cost.total_value)
    return q4(Decimal(qty) * Decimal(cost.total_value) / Decimal(cost.valued_qty))


def current_avg(cost: StockCost | None) -> Decimal | None:
    if cost is None:
        return None
    if cost.valued_qty > 0:
        return q4(Decimal(cost.total_value) / Decimal(cost.valued_qty))
    return cost.last_avg_cost


def next_ref(db: Session, seq: str, prefix: str) -> str:
    return f"{prefix}-{db.scalar(text(f'select nextval(:s)'), {'s': seq}):06d}"


def _apply_cost(cost: StockCost, effect: str, qty: int, value: Decimal) -> None:
    """qty and value are magnitudes. effect: 'in' adds, 'out' subtracts."""
    if effect == "in":
        cost.valued_qty += qty
        cost.total_value = Decimal(cost.total_value) + value
    elif effect == "out":
        if qty > cost.valued_qty or value > Decimal(cost.total_value):
            raise InsufficientStockError("Not enough owned stock or value at this location for this movement.")
        cost.valued_qty -= qty
        cost.total_value = Decimal(cost.total_value) - value
    if cost.valued_qty > 0:
        cost.last_avg_cost = q4(Decimal(cost.total_value) / Decimal(cost.valued_qty))


def _check_bucket(bal: StockBalance, bucket: str, label: str) -> None:
    qty = getattr(bal, BUCKET_ATTR[bucket])
    if qty < 0:
        raise InsufficientStockError(f"Not enough {bucket} stock for {label}: this would make it negative.")
    if bucket == "good" and bal.qty_good < bal.qty_reserved:
        raise InsufficientStockError(
            f"Only {bal.qty_good + 0} good units would remain but {bal.qty_reserved} are reserved; "
            "release the reservation first. Reserved stock cannot be issued.")


def post(
    db: Session,
    *,
    txn_type: str,
    location_id: int,
    specs: list[Spec],
    actor: User | None,
    reason: str | None = None,
    notes: str | None = None,
    idempotency_key: str | None = None,
    source_type: str | None = None,
    source_id: int | None = None,
    reverses_txn_id: int | None = None,
    ref_prefix: str = "STK",
) -> StockTransaction:
    if not specs:
        raise BusinessRuleError("Nothing to post.")
    if idempotency_key:
        existing = db.scalar(select(StockTransaction).where(StockTransaction.idempotency_key == idempotency_key))
        if existing is not None:
            existing.replayed = True  # type: ignore[attr-defined]
            return existing

    # 1) lock every balance, then every cost row, in one global order => no deadlocks, no lost updates
    bal_keys = sorted({_key(s.location_id, s.variant_id, s.batch_id) for s in specs}
                      | {_key(s.location_id, s.variant_id, s.batch_id) for s in specs})
    balances: dict[tuple[int, int, int], StockBalance] = {}
    for loc, var, b in bal_keys:
        balances[(loc, var, b)] = lock_balance(db, loc, var, b or None)
    cost_keys = sorted({(s.location_id, s.variant_id) for s in specs if s.mode in ("in", "out", "move", "raw")})
    costs = {k: lock_cost(db, *k) for k in cost_keys}

    txn = StockTransaction(
        ref=next_ref(db, "stock_txn_ref_seq", ref_prefix), txn_type=txn_type, location_id=location_id, reason=reason,
        notes=notes, idempotency_key=idempotency_key, reverses_txn_id=reverses_txn_id, source_type=source_type,
        source_id=source_id, created_by=actor.id if actor else None)
    db.add(txn)
    try:
        db.flush()
    except IntegrityError:
        db.rollback()
        raise ConflictError("This transaction was already posted (duplicate submission).")

    lines: list[StockMovement] = []

    def add_line(spec_: Spec, bucket: str, qty: int, unit_cost: Decimal, value: Decimal, effect: str) -> None:
        bal = balances[_key(spec_.location_id, spec_.variant_id, spec_.batch_id)]
        setattr(bal, BUCKET_ATTR[bucket], getattr(bal, BUCKET_ATTR[bucket]) + qty)
        _check_bucket(bal, bucket, f"variant {spec_.variant_id}")
        lines.append(StockMovement(
            txn_id=txn.id, location_id=spec_.location_id, variant_id=spec_.variant_id, batch_id=spec_.batch_id,
            bucket=bucket, qty_delta=qty, unit_cost=unit_cost, value_delta=value, cost_effect=effect))

    for s in specs:
        if s.qty == 0:
            raise BusinessRuleError("Quantity cannot be zero.")
        if s.bucket not in BUCKETS or (s.to_bucket and s.to_bucket not in VALUED_BUCKETS):
            raise BusinessRuleError(f"Unknown stock bucket {s.bucket}.")
        cost = costs.get((s.location_id, s.variant_id))
        n = abs(s.qty)
        if s.mode == "in":
            if s.qty < 0 or s.bucket not in VALUED_BUCKETS:
                raise BusinessRuleError("Internal error: inflow must be positive into a physical bucket.")
            value = s.value if s.value is not None else q4(Decimal(n) * (s.unit_cost if s.unit_cost is not None else _need_cost()))
            if value < 0:
                raise BusinessRuleError("Cost cannot be negative.")
            _apply_cost(cost, "in", n, value)
            add_line(s, s.bucket, n, q4(value / n), value, "in")
        elif s.mode == "out":
            if s.qty > 0 or s.bucket not in VALUED_BUCKETS:
                raise BusinessRuleError("Internal error: outflow must be negative from a physical bucket.")
            bal = balances[_key(s.location_id, s.variant_id, s.batch_id)]
            if getattr(bal, BUCKET_ATTR[s.bucket]) < n:
                raise InsufficientStockError(
                    f"Not enough {s.bucket} stock: {getattr(bal, BUCKET_ATTR[s.bucket])} available, {n} requested.")
            value = s.value if s.value is not None else avg_value(cost, n)
            _apply_cost(cost, "out", n, value)
            add_line(s, s.bucket, -n, q4(value / n), -value, "out")
        elif s.mode == "move":
            if s.qty < 0 or not s.to_bucket or s.bucket not in VALUED_BUCKETS or s.bucket == s.to_bucket:
                raise BusinessRuleError("Internal error: invalid bucket move.")
            bal = balances[_key(s.location_id, s.variant_id, s.batch_id)]
            if getattr(bal, BUCKET_ATTR[s.bucket]) < n:
                raise InsufficientStockError(
                    f"Not enough {s.bucket} stock: {getattr(bal, BUCKET_ATTR[s.bucket])} available, {n} requested.")
            value = s.value if s.value is not None else avg_value(cost, n)
            add_line(s, s.bucket, -n, q4(value / n), -value, "none")
            add_line(s, s.to_bucket, n, q4(value / n), value, "none")
        elif s.mode == "transit":
            if s.bucket != "transit" or s.value is None:
                raise BusinessRuleError("Internal error: transit movement needs a fixed value.")
            add_line(s, "transit", s.qty, q4(s.value / n), s.value if s.qty > 0 else -s.value, "none")
        elif s.mode == "raw":
            effect = s.cost_effect or "none"
            if s.value is None:
                raise BusinessRuleError("Internal error: raw replay needs an exact value.")
            if effect in ("in", "out"):
                _apply_cost(cost, effect, n, abs(s.value))
            add_line(s, s.bucket, s.qty, q4(abs(s.value) / n), s.value, effect)
        else:  # pragma: no cover
            raise BusinessRuleError(f"Unknown mode {s.mode}")

    db.add_all(lines)
    try:
        db.flush()
    except IntegrityError as e:  # the CHECK constraints are the last line of defence
        db.rollback()
        raise InsufficientStockError("Stock rules prevented this posting (negative or over-reserved stock).") from e
    txn.replayed = False  # type: ignore[attr-defined]
    return txn


def _need_cost() -> Decimal:
    raise BusinessRuleError("A unit cost is required for this movement.")


# ---- reservations ------------------------------------------------------------------------------
def is_expired(expiry: date | None, today: date | None = None) -> bool:
    return expiry is not None and expiry < (today or date.today())
