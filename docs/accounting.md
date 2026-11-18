# Accounting (Phase 6)

What is built, the rules it follows, and what it deliberately does **not** do. Local tax and statutory treatment (sales tax / GST, withholding, filing formats, fiscal-year rules) **must be validated by a qualified accountant** before the figures are relied on; the system records the tax rates already captured on orders and supplier invoices but does not compute or file returns.

## The model in one paragraph

Operational modules (stock, suppliers, distributors, customer orders and payments, expenses) remain the source of truth for *what happened*. The posting engine reads those records and writes balanced, immutable double-entry journals. Every figure in every financial report is calculated from **posted journal lines only**. A journal is keyed by `(source type, source id, event)` with a unique database index, so a record can reach the ledger **once**: re-running posting, retrying after a failure, or importing again cannot double-post.

## Chart of accounts

Seeded the first time accounting is used; never edited afterwards by the system. Accounts added by users (permission `accounting.manage_coa`) are ordinary rows. Accounts used by automatic posting are marked *system* and cannot be switched off. *Control* accounts (inventory, supplier payables, distributor receivables, GRNI, stock in transit, goods with couriers, employee receivables) are filled only by their own modules; a manual journal cannot touch them (customer receivables 1100 may be opened with a named customer through an opening batch).

| Code | Account | Notes |
|---|---|---|
| 1000 / 1010 / 1020 | Cash on hand / Main bank account / Courier collections | Cash and bank accounts; more can be added (1011-1099) |
| 1100 | Customer receivables | per customer; credit balances are shown as advances in the balance sheet |
| 1110 | Distributor receivables | per distributor, matches the distributor ledger |
| 1200 / 1210 / 1220 | Inventory / stock in transit / goods with couriers | inventory at weighted-average cost |
| 1300 | Supplier deposits and advances | |
| 1400 | Employee purchase receivables | control account; filled by employee purchases and their payments |
| 2000 / 2050 | Supplier payables / Goods received not invoiced (GRNI) | |
| 2100 | Customer advances and store credit | |
| 2200 | Taxes payable | |
| 3000 / 3100 / 3200 | Owner capital / Retained earnings / Opening balance equity | |
| 4000 / 4010 / 4900 | Product sales / Delivery fee income / Other income | |
| 4100 / 4200 | Sales returns / Discounts and allowances | contra-revenue |
| 5000 / 5100 / 5110 | Cost of goods sold / Inventory write-offs / Stock adjustments and count variances | |
| 5200-5900 | Operating expenses | |

## Automatic postings

| Event | Entry |
|---|---|
| Goods receipt (stock in) | Dr Inventory, Cr GRNI at the receipt cost |
| Supplier invoice posted | Dr GRNI (+ Taxes payable), Cr Supplier payables, by supplier |
| Supplier payment / credit / adjustment | Dr Supplier payables, Cr Cash or bank (method-mapped) |
| Dispatch of a customer order (default) | Dr Customer receivables (total incl. tax and delivery fee); Cr Product sales at list price; Dr Discounts for the difference to the price charged; Cr Taxes payable; Cr Delivery fee income. COGS: Dr Cost of goods sold, Cr Inventory at the average cost taken from the stock ledger at that moment |
| Same, with revenue recognised on delivery | Dispatch moves cost Dr Goods with couriers, Cr Inventory; delivery posts the sale and releases the cost to COGS. Failed delivery returns the goods |
| Customer payment / refund / reversal | Dr Cash or bank (by payment method), Cr Customer receivables; refunds and reversals mirror it. Store-credit movements post nothing; they are changes of form, not of money |
| Customer return settled | Dr Sales returns (+ Taxes payable, + delivery fee if refunded), Cr Customer receivables. Stock re-entering inventory reverses COGS at cost; damaged units stay in the stock ledger's damaged bucket (still an asset) until a write-off is approved |
| Distributor dispatch | Dr Distributor receivables (net), Dr Discounts (terms discount), Cr Product sales (gross); COGS as above |
| Distributor payment / return credit / adjustment / opening balance | Dr Cash or bank / Cr Distributor receivables; credits and write-offs against Sales returns / Bad debts; opening distributor balance Cr Opening balance equity |
| Stock write-off, count variance, approved adjustment | Dr Inventory write-offs or Stock adjustments, Cr Inventory (gain: the reverse) |
| Transfer | Out: Dr Stock in transit, Cr Inventory. In: Dr Inventory, Cr Stock in transit. Shortage on receipt: Dr Inventory write-offs, Cr Stock in transit |
| Opening stock | Dr Inventory, Cr Opening balance equity |
| Expense | Dr expense account, Cr the cash or bank account it was paid from |
| Reversals | A reversing stock/payment record produces the mirror image of the original journal |

**Dates.** Supplier ledger, distributor ledger, payments and shipments post on their business date. *Stock movements post on the day they were recorded in the system*; a receipt entered today with a back-dated "received" date is an inventory entry of today.

**Closed periods.** A system posting whose business date falls in a closed period is posted into the first open period; the journal keeps `original_date` so the shift is visible. Manual journals are refused outright.

**Which records are skipped.** Pure bucket moves with no value change (inspection, damage into the damaged bucket, transit status) post nothing; they are marked `skipped`, not lost.

## Posting queue

Operational records carry `accounting_status` (`pending`, `posted`, `skipped`). *Post pending records* (Accounting > Overview, or `POST /accounting/posting/run`, permission `accounting.post`) processes every pending record in its own savepoint: one failure is reported and leaves only that record pending. Nothing is posted in the background; an accountant decides when. A period cannot be closed while records dated in it are pending.

## Controls

* Journals balance (debits = credits) in the service **and** in a deferred database trigger; a posted entry and its lines cannot be updated or deleted at the database level. Corrections are reversals.
* Manual journals: saved as a draft by `accounting.post`, posted by a different person holding `accounting.approve`. Reversal by someone other than the poster. Journals generated from a source document cannot be reversed by hand; correct the document (for example reverse the payment) and the ledger follows.
* Expenses: up to the *expense approval limit* (default **0**, so every expense waits) they post immediately; above it a different person approves. Voiding needs someone who neither recorded nor approved it, and is refused once the expense has been matched on a bank reconciliation.
* Periods: monthly, created on demand; fiscal year start month configurable until journals exist. Closing is refused before the month ends, with drafts in it, or with pending records dated in it. Reopening needs a reason from someone with `accounting.reopen_period` and a decision by a different person with `accounting.approve`; both are audited.
* Revenue-recognition policy (dispatch or delivery) is changeable until the first sale is posted, then locked; changing it afterwards would need an accountant-led restatement.
* All actions are written to the audit log with before/after and reason.

## Cash and bank reconciliation

Each cash/bank account is its own ledger account. To reconcile, tick the ledger lines that appear on the statement and enter the statement closing balance. The backend completes the reconciliation **only if** previously cleared balance + ticked lines equals the statement balance exactly; otherwise it reports the difference and changes nothing. No plug entry is ever created. A mismatch means a transaction is missing, duplicated or wrong; fix it with a proper entry. Only the most recent reconciliation of an account can be voided, by a different person.

## Opening balances and going live

Opening balances are entered as **batches** of lines, each carrying its evidence reference and a **verified** flag.

* Unverified lines may be saved but a batch containing any cannot be approved.
* A batch must balance on its own. The system never adds a balancing line. If the figures do not balance, find out why, or add a documented owner-capital line.
* Approval by a user other than the author posts one `opening` journal.
* Opening *stock*, *supplier payables* and *distributor balances* are entered in their own modules (they must agree with the sub-ledgers) and appear in the ledger through the normal posting rules, against *Opening balance equity*. Customer receivables, cash, bank, other assets/liabilities and owner capital come through opening batches.
* **Workbook import (Phase 9).** Accounting > Opening balances > *Import workbook…* loads stock, distributor, supplier and customer balances, cash and bank, other assets and liabilities, and the owner's declared capital from one Excel file (template: `GET /accounting/opening-import/template`). Every row needs an evidence reference and `yes` in the verified column. *Check file* enters everything for real inside a transaction and rolls it back, so a check cannot disagree with the real import; any error anywhere imports nothing. Declared capital must equal assets minus liabilities; otherwise the import is refused and the difference shown. **No balancing entry is ever posted.** The file's hash is recorded and the same file cannot be imported twice; a supplier or distributor that already has an opening balance is refused (correct it by an approved adjustment). Requires `accounting.opening_balances`, `inventory.opening_stock`, `distributors.manage` and `distributors.approve` (CEO by default).
* **Supplier opening balance.** What you owed a supplier before the system is posted once per supplier (`POST /suppliers/{id}/opening-balance`, `accounting.opening_balances`): Dr Opening balance equity (3200) / Cr Trade payables (2000), on the supplier's own ledger, and it ages in the payables report from its date plus the supplier's terms. A supplier "increase payable" adjustment cannot be used for this (it posts to a different account).
* **Making the ledger official** (Accounting > Overview, `accounting.approve`) requires: no pending records; ledger = stock valuation, supplier ledger and distributor ledger; trial balance balanced; at least one approved opening batch and none in draft; **Opening balance equity cleared** to owner capital by a documented batch; a written reconciliation statement of at least a sentence; and someone other than the person who entered the opening batches. Until then every report carries a notice that figures are provisional.

The system cannot know what was true before you started: history is never invented. Figures that cannot be supported by a document should be left out or entered as a clearly described estimate by the owner, not marked verified.

## Reports

All are on screen only (PDF/Excel export and dashboards are Phase 8): trial balance, profit and loss, balance sheet, direct-method cash flow, general ledger by account (optionally by party), receivables and payables ageing (first-in-first-out from the ledger), customer / supplier / distributor statements, sales-returns-discounts, expense summary, COGS and gross profit, inventory valuation (ledger vs stock sub-ledger), monthly summary and annual summary. Most accept a location filter (journal lines carry the location of the sale, stock or expense).

* Retained earnings and current-year earnings are *derived* from income and expense accounts for the fiscal year; there are no year-end closing entries, and the owner's draw/dividend treatment needs accountant input.
* The balance sheet presents party balances by sign: a customer with a credit balance appears as a liability (advance), a supplier with a debit balance as an asset.
* The integrity view compares the ledger with the stock valuation, stock in transit, supplier ledger and distributor ledger; any non-zero difference is shown, never hidden.

## Permissions

`accounting.view` read ledgers and reports (also `reports.financial` for reports) · `accounting.post` run posting, create/delete manual drafts · `accounting.approve` post drafts, reverse, decide expenses/reopening, approve opening batches, void reconciliations, go live · `accounting.manage_coa` chart, settings, cash accounts · `accounting.close_period` / `accounting.reopen_period` · `accounting.reconcile` · `accounting.opening_balances` · `expenses.manage`. Default roles: CEO has all; Accounts has everything except `accounting.approve` and `accounting.reopen_period`; Manager has none.

## Not in Phase 6 (and why)

* Tax computation, returns and statutory forms; withholding; fixed-asset depreciation; foreign currency; multi-entity consolidation; budgets.
* Payroll. Employee purchases are posted since Phase 7 ([employees.md](employees.md)); deduction from payroll is not built.
* Report export, dashboards and the approvals inbox arrived in Phase 8 (see reports.md).
* Bank-statement file import and automatic matching: reconciliation is manual and by exact agreement.
* Automatic accruals, prepayments, and year-end closing entries.
* Costing method other than weighted average ([inventory-costing.md](inventory-costing.md)). Changing the method after stock has been valued needs an accountant-led conversion.

## What you must provide before relying on the numbers

1. An accountant's review of the posting rules above and of tax treatment.
2. Opening balances with documents: cash count, bank statements, customer and distributor balances, supplier balances, other assets/liabilities, owner capital.
3. Your cash and bank accounts and which one each payment method uses (Accounting > Cash and bank; method mapping via `PUT /accounting/settings`).
4. Fiscal-year start month and the revenue-recognition choice (before the first sale is posted).
5. The expense approval limit, and which people hold `accounting.approve`, close and reopen.
