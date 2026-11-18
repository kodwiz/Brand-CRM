# Customers and orders (Phase 4)

Manual order entry and fulfilment. Everything here is enforced by the backend; the desktop only presents it.

## What is and is not operational

**Operational:** customer records, manual orders from configurable channels, price and promotion engine, discount approval, stock reservation, packing / dispatch / delivery outcomes (partial parcels included), customer payment tracking on the order, customer returns with inspection, refunds and store credit.

**Not operational (later phases):** accounting journals for sales, payments and refunds (Phase 6; nothing is posted to a general ledger); distributor credit and distributor orders (Phase 5); sales reports, exports and dashboards (Phase 8); automatic import from WhatsApp, website, Instagram or Facebook (orders are typed in); courier integrations; "on credit" terms are a label only (no credit limit or receivable ageing).

## Statuses

Three separate statuses, each derived by the server, never typed in:

| Status | Values |
|---|---|
| Order | draft, confirmed, packed, dispatched, partially_delivered, delivered, delivery_failed, refused, cancelled, partially_returned, returned |
| Fulfilment (per parcel) | packed, dispatched, delivered, failed, refused, received_back, cancelled |
| Payment | unpaid, partially_paid, paid, overpaid, refunded, partially_refunded |

The order status is recalculated from its parcels and returns after every change, so it cannot disagree with them.

## Pricing

- The list price is the variant's retail price. The agreed price may be lower (a manual discount), never higher.
- **Promotions** (Settings API `/promotions`, permission `pricing.manage`): percent or fixed amount, date range, optional scope (variant, product, category, customer category), minimum quantity or order value, optional cap (% of price), stackable or not. Non-stackable promotions: the best one applies. Stackable ones add up, still limited by every cap.
- **Manual discount** is measured against the promotional price. Above `sales.max_manual_discount_pct` (default **0**, so every manual discount needs approval until you set a limit) the order cannot be confirmed until someone with `discounts.approve` **other than the order's creator** approves it. A rejection blocks confirmation until the prices are changed.
- Prices are copied onto the order lines. Changing the catalogue price later does not change an existing order. If the list price or a promotion changed between saving a draft and confirming it, confirmation is refused (409) and the draft must be saved again to re-price.
- Tax: one flat rate, `sales.tax_rate_pct` (default 0). No tax categories.

## Stock

- **Confirming** reserves every line at the fulfilment location, all or nothing, with row locks: two orders cannot reserve the same units (tested with concurrent confirmations).
- Cancelling releases the reservation. Reopening a cancelled order returns it to draft; it reserves again on confirmation.
- **Packing** only uses reserved, unpacked stock; a parcel may hold part of the order.
- **Dispatch** consumes the reservation and posts a `customer_sale` stock-out at the current average cost (stored on the parcel lines). Prepaid orders cannot be dispatched until paid.
- **Failed or refused** parcels are not saleable. After "Receive back" they go into the inspection bucket exactly like a customer return.
- Cancelling is refused once goods are dispatched; use delivery outcomes and returns.

## Payments

- Recorded against a confirmed order (`orders.payments`). A payment cannot exceed what is still owed. Bank transfers and cards need a reference. Idempotency keys make a retried request return the first payment.
- Reversal needs `orders.refund`, a reason and a different person from the one who recorded it.
- An overpayment can be refunded (`orders.refund`) up to the amount overpaid.
- Store credit lives in an append-only ledger (`customer_credit_entries`; a trigger rejects UPDATE/DELETE) and can be used as a payment method.

## Customer returns

1. **Request** (booker or manager): only delivered units, within the return window if one is configured (`sales.return_window_days`, default 0 = no limit).
2. **Approve** (`orders.returns`) by someone other than the requester.
3. **Receive** (`returns.receive`) at the location: counts what physically arrived. This stock goes to **inspection, never to saleable stock**.
4. **Inspect** (`returns.receive`): every received unit is assigned to restock, damaged or expired. Only restocked units become saleable.
5. **Settle** (`orders.refund`, not the requester): the refund can never exceed what the customer actually overpaid after the returned value reduces the order. Settled as money or as store credit.

## Who sees what

| Role | Orders | Customers | Money |
|---|---|---|---|
| Order booker | own orders only | customers they created or sold to | prices and totals of own orders |
| Manager / CEO | all in location scope | all | everything |
| Accounts | all | all | payments and refunds |
| Stock keeper | confirmed orders for their locations, parcels | none | **none**: no prices, payments or totals |

Out-of-scope records answer 404, not 403. Role defaults are data; the CEO can change them.

## Configuration

`GET/PUT /sales/settings` (`settings.manage`), channels at `/sales/channels` (WhatsApp, Website, Instagram, Facebook, Walk-in and Other are seeded and editable), promotions at `/promotions`. **There is no desktop screen for these yet**; use the API docs at `/docs` (not served in production) until a settings screen is added.

## Known limitations

- One batch per return line unless the sold batch is chosen; returns that mix batches need the batch given on receipt.
- Prices above list are not supported.
- No price lists per customer category beyond promotions scoped to a customer category.
- Delivery fee is a typed amount; no courier rate tables.
- Customer merge / deduplication beyond the phone-number uniqueness check is not provided.
