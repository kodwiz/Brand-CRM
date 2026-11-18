# Inventory costing policy

**Method: weighted-average cost (WAC), maintained per product variant per location.**
Status: implemented in Phase 2. Accounting postings that consume these figures arrive in Phase 6.

## 1. Model

For every (location, variant) the system keeps `stock_costs`:

| field | meaning |
|---|---|
| `valued_qty` | units physically at the location and owned by the company: good + damaged + expired + inspection-pending, across all batches |
| `total_value` | their total cost value (4 decimal places) |
| average cost | **derived** as `total_value / valued_qty`; never stored as an editable number |
| `last_avg_cost` | last known average, kept for reference when `valued_qty` drops to 0 |

Every ledger line (`stock_movements`) stores the `unit_cost` and the exact `value_delta` it used. Those two columns are
written once and never updated. Valuation reports sum `value_delta`; they do not recompute from current prices.

Stock **in transit** is owned by the company but is not at either location. Its value is carried on the movements into the
`transit` bucket at the destination and is excluded from the destination average until receipt.

Invariant (tested): for every location and variant, `valued_qty` and `total_value` equal the sums over the ledger of the
valued buckets, and company valuation = sum of `stock_costs.total_value` + value of open in-transit lines.

## 2. Rules by event

| Event | Cost used | Effect on location average |
|---|---|---|
| **Supplier receipt** (accepted qty) | Unit cost entered on the receipt line (purchase price; landed costs are added in Phase 3) | New avg = (existing value + qty x unit cost) / (existing qty + qty) |
| **Supplier receipt, damaged qty** | Same unit cost; goes to the `damaged` bucket | Included in the average (we own it until returned or written off) |
| **Supplier receipt, rejected qty** | Not received into stock; no cost recorded | None |
| **Opening stock** | Verified unit cost supplied by an authorised person (required; never guessed) | Same as a receipt |
| **Transfer dispatch** | Sender's average cost at the moment of dispatch; fixed on the transfer line | Sender average unchanged (stock leaves at average) |
| **Transfer receipt** | The fixed dispatch cost, **not** the receiver's average | Receiver average is recalculated with the incoming cost, like a receipt |
| **Transfer shortage** (approved loss) | Dispatch cost of the missing units | Transit value removed; loss recorded (accounting hook) |
| **Customer / distributor return** | **Original cost of goods sold** on the returned sale/dispatch, passed in by the calling module | Enters the `inspection` bucket; valued like a receipt. Not saleable until inspected |
| **Return disposition** | Value moves with the units between buckets at the location average | Average unchanged |
| **Supplier return** (Phase 3) | Average cost at the time of return | Average unchanged |
| **Damaged / expired marking** | Units move good -> damaged/expired at current average | Unchanged (still owned) |
| **Write-off** (approved) | Current average cost | Units and value leave; average unchanged. Loss recorded (accounting hook) |
| **Stock adjustment, gain** | Current average (or `last_avg_cost` if location stock is 0; otherwise a cost must be supplied) | Unchanged when at average |
| **Stock adjustment, loss / count shortage** | Current average | Unchanged |
| **Reversal** | The **original** `unit_cost` and exact `value_delta`, negated | Restores the pre-transaction state exactly, provided the stock still exists |

When an outflow equals all remaining units, it takes the exact remaining `total_value`, so no rounding residue is left.
Intermediate values round half-up to 4 decimal places.

## 3. Guarantees

- **No negative stock.** Every bucket is checked inside the posting transaction under a row lock, and the database has
  CHECK constraints (`qty >= 0`, `reserved <= good`). A negative-stock exception would require a written, approved business
  rule and a code change; none exists.
- **Reserved stock cannot be issued**: good minus reserved must remain >= 0 for any outflow other than consuming that reservation.
- **History is immutable.** Posted movements are never updated or deleted. Mistakes are corrected with a reversal
  transaction (linked to the original, exactly one allowed per transaction) or a new adjustment.
- **No silent recalculation.** Changing a variant's catalogue "purchase cost" does not touch stock value. Stock value
  comes only from posted transactions.
- **Duplicate protection.** Idempotency keys on direct postings, status-guarded document workflows, and a unique
  reversal link.

## 4. Policy changes

`inventory.costing_method` is stored in `system_settings` (default `weighted_average`). It can be changed only while **no stock
transaction has ever been posted**. After the first posted transaction the API refuses the change (HTTP 409); changing
method afterwards needs a controlled data migration with accounting approval, which is deliberately not built.

## 5. Accounting hooks (Phase 6)

Each `stock_transactions` row carries `accounting_status` (`pending` until Phase 6 posts it). The expected postings are:
receipt -> Dr Inventory / Cr GRNI or Payables (Phase 3); write-off, shortage and adjustment loss -> Dr Inventory write-off
expense / Cr Inventory; adjustment gain -> reverse. Phase 2 records the quantities and values those entries need,
but **posts nothing to a ledger**: no ledger exists yet.

## 6. Known simplifications

- Average is per location and variant, not per batch. Batches carry expiry and traceability, not separate cost.
- Variant-level reorder threshold only (no per-location override yet).
- Landed costs (freight, duties) are not apportioned until purchasing exists (Phase 3).
