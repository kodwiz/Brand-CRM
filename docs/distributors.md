# Distributor credit (Phase 5)

Distributors buy goods on credit. They are customers of the business, not users of the system: nobody logs in as a distributor.
Everything below is enforced by the backend; the desktop only presents it.

## The one rule that matters

```
Closing receivable = Opening balance + new credit charges - approved credit notes - payments allocated
```

The balance is **never an editable field**. It is the sum of the append-only `distributor_ledger` (a database trigger rejects UPDATE and DELETE,
and a unique `(entry_type, source_type, source_id)` constraint stops a document being booked twice). A correction is a new, opposite entry.
`GET /distributors/integrity` recomputes the balance from the documents (invoices, credit notes, payments, adjustments) and compares it with the
ledger, checks that open invoices less unallocated credit equals the balance, and that nothing is over-allocated.

Goods issued are **not cash**. A dispatch is a receivable, and only a payment that is allocated reduces it.

## Documents

| Document | Created by | Effect on the balance |
|---|---|---|
| Opening balance (`OPN-`) | manager/CEO, once per distributor | + amount, as a dated invoice so payments can be allocated to it |
| Dispatch (`DSP-`) | stock keeper / manager | + total, only when **posted** (stock leaves at the same moment) |
| Debit note (`DBN-`) | approved "extra charge" adjustment | + amount |
| Return (`DRT-`) | stock keeper | none until its credit is **approved** |
| Payment (`DPY-`) | accounts / stock keeper / manager | - amount, when posted |
| Adjustment (`DAD-`) | requested by anyone with transact | write-off and credit: - amount; extra charge: + amount; only when approved |

## Prices and discounts

The dispatch price is the distributor's **agreed price** for the item if one is set, otherwise the item's standard distributor price; the distributor's
discount terms (%) are taken off that. A line may carry a lower price only within the setting `distributors.max_extra_discount_pct` (default 0); above it the
dispatch is held. A price **above** the agreed price is refused. The price is copied onto the dispatch line, so later price changes never alter history.
An item with no distributor price and no agreed price cannot be dispatched.

## Holds (nothing moves, nothing is owed)

A dispatch is **held** instead of posted when any of these is true; a different user holding `distributors.approve` must approve it. Approval posts it
(stock out, ledger charge) and the credit limit is checked again at that moment.

- **Credit limit**: balance + this dispatch would exceed the limit. A limit of **0 means no credit**, so every dispatch is held until a limit is set.
- **Back-dated**: dated earlier than `distributors.backdate_days` (default 7) ago.
- **Extra discount**: price below the agreed price by more than the allowed extra discount.

Concurrent dispatches against the same distributor are serialised by a row lock, so two cannot both squeeze under the limit.
Rejection leaves stock and balance untouched. A payment dated before the back-dating window waits for approval in the same way.

## Returns and exchanges

1. **Request** per dispatch line, capped at what was dispatched less what is already committed to other returns.
2. **Receive**: goods go to the *inspection* bucket. They are **not saleable**.
3. **Inspect**: each unit becomes saleable stock, damaged or expired; the credit quantity is chosen (default: all received).
4. **Decide**: approval by someone who did not request, receive or inspect creates the credit note **at the price on the original dispatch** (not today's price)
   and applies it to that dispatch. Rejection credits nothing.

An **exchange** is a return credit plus a replacement dispatch that links to the credited return. Both documents stay in the history; nothing is edited.

## Payments and allocation

A payment can be allocated to specific dispatches (partial or several at once), auto-allocated oldest-due first, or left **unallocated on account** and allocated
later. Allocation can never exceed what is outstanding on the dispatch or what is left of the payment, even with simultaneous requests. Reversal needs
`distributors.approve` and a different user than the one who recorded it; it posts an opposite ledger entry and releases the allocations. A reference
number is optional; an idempotency key makes retries safe.

## Aging, overdue and statements

- Aging buckets by **due date** (dispatch date + payment terms): not yet due, 1-30, 31-60, 61-90, over 90 days. Overdue = everything past due.
- Monthly statement for any month: opening balance (everything before the month, carried forward automatically), dated entries with a running balance,
  totals, closing balance and a `reconciles` flag proving opening + charges - credits - payments = closing.

## Who can do what

| Permission | Holders | Allows |
|---|---|---|
| `distributors.view` | stock keeper, accounts, manager, CEO | see accounts, documents, statements |
| `distributors.manage` | manager, CEO | records, terms, agreed prices, opening balance |
| `distributors.transact` | stock keeper, manager, CEO | dispatches, return requests/receipt/inspection, adjustment requests |
| `distributors.receive_payment` | stock keeper, accounts, manager, CEO | record payments, allocate |
| `distributors.approve` | manager, CEO | decide holds, return credits, adjustments, backdated payments; reverse payments |
| `settings.manage` | CEO | back-dating window and extra-discount limit; integrity check |

A stock keeper sees distributors **assigned to them or unassigned**, and only dispatches/returns at their own locations; anything else answers 404.
Stock cost on dispatch lines is shown only to roles with `products.view_cost`. The roles are editable in Settings > Roles.

## Settings (API: `GET/PUT /distributors/settings`)

`backdate_days` (default 7) and `max_extra_discount_pct` (default 0). There is no desktop screen for these yet.

## Not in Phase 5

- No accounting journals: ledger entries are flagged `accounting_status = pending` for Phase 6. Nothing is posted to a general ledger, so do not read these
  balances as financial statements.
- No reports, exports or dashboards (Phase 8); the statement and aging are on-screen only.
- No distributor portal or login, no interest/late fees, no multi-currency, no tax on distributor invoices.
- Return credit is per original dispatch line price; a returned line with batches is credited at the dispatched price regardless of batch.
- Real data must be supplied by the business: distributors, credit limits, terms, agreed prices, assigned stock keepers and opening balances.
