# Suppliers and purchasing (Phase 3)

## The one rule about money

**A debt to a supplier is created once, when the supplier's invoice is posted. Nothing else creates one.**

- Receiving goods does not create a debt. "Goods received not invoiced" is shown as information (Suppliers > Payables) and is not in any balance.
- Posting an invoice writes one row to the append-only `supplier_ledger`. A unique constraint on (entry type, source document) makes a second booking of the same document impossible, and a database trigger rejects UPDATE and DELETE on the table.
- Receiving first or invoicing first both work and give the same balance. Invoicing ahead of receipt is shown as "Awaiting receipt".
- Payments, reversals, voids, credits and extra charges each add their own ledger row; nothing is edited.
- Supplier balance = sum of the ledger. `GET /purchasing/integrity` (CEO) re-derives every balance from the documents (open invoices - payments on account + unapplied adjustments) and reports any difference.

Accounting journals do **not** exist yet (Phase 6). Every supplier ledger row carries `accounting_status = pending`.

## Flow

1. **Order** (`purchasing.manage` + `products.view_cost`): draft with supplier, delivery location, prices. Payment terms are copied from the supplier at creation.
2. **Approve** (`purchasing.approve`): a different person from the creator, unless the order total is within `purchasing.po_self_approve_limit` (default 0, so never). Approved orders are not edited; cancel and re-issue.
3. **Receive** (`inventory.receive`): against an approved order at its delivery location, items must be on the order. The cost comes from the order; whatever the client sends is ignored. Accepted and damaged units count as received against the order, rejected units are tracked separately and never enter stock. Receiving more than ordered is refused beyond `purchasing.receipt_over_tolerance_pct` (default 0). Reversing a receipt re-opens the order.
4. **Additional costs** on a receipt (freight, duty, handling) are added to the stock cost of the units that entered stock, pro rata by value; the last line takes the rounding remainder so the sum is exact. They are not a supplier debt. If the supplier bills a charge, it goes on their invoice as a charge line.
5. **Invoice** (`purchasing.manage` + `suppliers.view`): the supplier's own invoice number must be unique per supplier (draft, held and posted). Due date = invoice date + terms unless given.
6. **Match** at posting, per line: price above tolerance (`purchasing.invoice_price_tolerance_pct`), quantity invoiced above ordered beyond tolerance (`purchasing.invoice_qty_tolerance_pct`), or a line not on the order. Any of these holds the invoice: status `held`, no liability, cannot be paid. A different person with `purchasing.approve_variance` approves (note required, the invoice then posts and books the debt) or rejects it (note required, the number is free again). Invoices with no order are allowed and labelled "No order".
7. **Pay** (`purchasing.pay`): partial payments, allocated to posted invoices of that supplier. Allocations cannot exceed an invoice's outstanding amount, which is enforced with row locks so simultaneous payments cannot overpay. An amount not allocated needs an explicit "on account" choice and later `allocate`. Cheques need a reference. An idempotency key makes a retried request return the original payment. Reversal needs `purchasing.reverse`, a reason, and a different person from the one who entered it.
8. **Credits, extra charges and returns**: a supplier credit (reduces the debt) or extra charge (increases it) is requested, then approved or rejected by a different person with `purchasing.approve`. A credit tied to an invoice reduces its outstanding amount and cannot exceed it. A return to a supplier removes stock at dispatch at the current average cost and is marked credited when a linked credit is approved.
9. **Void** (`purchasing.reverse`, different person from the poster, reason): only when the invoice has no active payments or credits.

## Three-way matching view

Order detail (Suppliers > Orders > double-click) shows per line: ordered, accepted, damaged, rejected, still to come, invoiced, unit cost and discrepancy flags (short of order, more than ordered, invoiced before received, received not yet invoiced, invoiced above ordered, rejected on receipt), with the invoices on the order below. Double-click an invoice to open it; from an invoice you can reach its order.

## Visibility

| Role | Sees |
|---|---|
| Stock keeper | Orders at their locations, quantities only. No prices, invoices, payments or balances. |
| Manager | Orders, invoices, payments, statements (view); approves orders, variances and credits. Cannot pay, void or reverse. |
| Accounts | Creates suppliers, orders, invoices, payments, credits and returns. Cannot approve orders or variances. |
| CEO | Everything, including reversals and the integrity check. |
| Order booker | No purchasing access. |

Orders and returns are scoped to the user's locations (404 for others). Invoices, payments and balances are company-wide and need `suppliers.view`.

## Settings (`GET/PUT /purchasing/settings`, `settings.manage`)

`po_self_approve_limit`, `receipt_over_tolerance_pct`, `invoice_price_tolerance_pct`, `invoice_qty_tolerance_pct`; all default to 0 (strict).

## Limits of this phase

No accounting journals; no purchase tax logic; no PO amendment after approval; landed costs that arrive after receipt are not re-costed into stock; dispatched supplier returns cannot be reversed; no document attachments; one currency (PKR).
