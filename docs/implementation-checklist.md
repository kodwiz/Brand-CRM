# Implementation checklist

Legend: [x] built and tested, [~] partial, [ ] not started.

## Phase 1: Foundation
- [x] Project structure, config from environment, `.env.example`, dependency files
- [x] PostgreSQL schema through Alembic (upgrade/downgrade round-trip tested)
- [x] Authentication: hashing, sessions, lockout, rate limit, password change/reset
- [x] Role + location authorization, configurable role permissions
- [x] Locations, users, roles administration (API + desktop)
- [x] Append-only audit log, with filtering and before/after viewer
- [x] Desktop shell: sign-in, navigation by permission, location selector, Overview, Settings
- [x] Design tokens and reusable components (table, states, forms)
- [~] Sidebar icons (text labels only)
- [ ] TLS / deployment packaging

## Phase 2: Products, inventory and stock movement
- [x] Category / product / variant catalogue: validation, unique SKU and barcode, search, sort, filter, pagination, price gating, audited price changes
- [x] Excel/CSV bulk import: blank template, validate -> review -> confirm, all-or-nothing commit, summary
- [x] Location-wise stock buckets (good, damaged, expired, inspection, in transit), reserved/available, batch and expiry (FEFO reservation)
- [x] Append-only movement ledger with per-movement unit cost; weighted-average cost per variant per location (docs/inventory-costing.md)
- [x] Goods receiving (received / rejected / damaged), opening stock (dry-run, all-or-nothing)
- [x] Transfers: request -> approve -> dispatch -> partial receipt -> shortage approval; no duplicate posting
- [x] Stock counts, adjustments, write-offs with independent approval; reversals instead of edits
- [x] Damaged / expired / inspection workflows, low-stock and expiry alerts, valuation and ledger integrity check
- [x] Desktop pages: Products, Inventory (7 tabs), Transfers; checked at 1366x768 and 1920x1080 against the live backend
- [x] Backup / verified restore script (docs/backup-and-recovery.md)
- [~] Not in Phase 2 (depends on later phases): purchase orders and supplier costs/landed cost (3), sales orders and stock reservation from orders (4), distributor dispatch/credit (5), accounting journals from stock movements - movements are flagged `accounting_status=pending` only (6), reports/export (8)
- [~] Limitations: average cost is per location and variant, not per batch; reorder threshold per variant only; no scheduled backups; opening-stock screen exists but real stock data must be supplied by the business
## Phase 3: Suppliers and purchasing
- [x] Suppliers: unique code and name, payment terms, active/inactive (cannot deactivate with a balance or open orders)
- [x] Purchase orders: draft -> approved -> partially received -> received / closed / cancelled; approval by someone other than the creator (self-approval limit is a setting, default 0)
- [x] Goods receipts against an order: accepted / damaged / rejected quantities, cost taken from the order, over-receipt blocked (tolerance setting), wrong location and off-order items refused, reversal re-opens the order
- [x] Additional costs on a receipt (freight, duty, handling) capitalised into stock cost, allocated exactly (no rounding loss); never a supplier liability
- [x] Supplier invoices: receiving and invoicing are independent events, either may come first; non-order invoices allowed and labelled; duplicate supplier invoice numbers refused
- [x] Three-way matching (ordered / received / invoiced) per line with discrepancy flags; price, quantity and off-order variances hold the invoice until a different person approves or rejects it
- [x] Supplier liability created exactly once, at invoice posting, in an append-only supplier ledger (database trigger blocks UPDATE/DELETE; unique source constraint blocks double booking)
- [x] Payment terms and due dates, partial payments, allocation to invoices, payments on account, outstanding balances, aging buckets, overpayment blocked under concurrency, payment reversal, idempotency key
- [x] Supplier credits / extra charges (approval by another person), supplier returns (stock out at current average cost on dispatch) linked to their credit
- [x] Supplier statement with running balance; payables aging; goods received not invoiced (information only); purchasing integrity check
- [x] Desktop: Suppliers module (Orders, Invoices, Payments, Credits and returns, Suppliers, Payables) with PO detail showing ordered / received / invoiced and links to related records; checked at 1366x768 and 1920x1080 against the live backend
- [x] Cost visibility: stock keepers see order quantities but no prices, invoices, payments or balances
- [~] Not in Phase 3 (depends on later phases): accounting journals for stock receipts, supplier invoices, payments (Phase 6, ledger entries are flagged `accounting_status=pending`); sales and customer returns (4); distributor credit (5); reports and exports (8)
- [~] Limitations: purchase orders cannot be amended after approval (cancel and re-issue); landed costs arriving after goods were received are not re-costed into stock already sold or held; no purchase tax logic (tax is a typed amount); a dispatched supplier return cannot be reversed (record a correcting receipt); invoices are not location-scoped; single currency (PKR); supplier invoice attachments not stored
## Phase 4: Customers and orders
- [x] Customers: unique phone (normalised, any format), categories, addresses, active/inactive; order bookers see only customers they created or sold to
- [x] Manual order entry from configurable channels (WhatsApp, website, Instagram, Facebook, walk-in, other); drafts, edit, confirm, cancel, reopen; idempotent creation
- [x] Pricing engine: list price, configurable promotions (scope, dates, minimums, caps, stacking), manual discounts above a limit (default 0) need approval by a different user; prices preserved on the order; stale-price check at confirmation
- [x] Stock reserved at confirmation (all-or-nothing, concurrency-safe), released on cancel; dispatch consumes reservation and posts stock-out at average cost
- [x] Separate order, fulfilment (parcel) and payment statuses derived by the server; partial parcels and partial deliveries; prepaid orders held until paid; failed/refused parcels received back into inspection
- [x] Customer payments tracked on the order (no overpayment, references, idempotency, reversal by another user), refunds of overpayments, store credit ledger (append-only)
- [x] Customer returns: request, approval by another user, physical receipt into inspection, inspection dispositions (restock / damaged / expired), settlement capped at what was actually overpaid; saleable stock never restored before inspection
- [x] Fulfilment view for stock keepers without prices, payments or totals; own-orders-only view and "My sales" summary for bookers
- [x] Desktop: Orders (Orders, Parcels, Returns, My sales) and Customers modules with order entry, price preview, order detail with actions, return dialogs; checked at 1366x768 and 1920x1080 against the live backend
- [~] Not in Phase 4 (later phases): accounting journals for sales, payments and refunds (6); distributor orders and credit (5); sales reports, exports, dashboards (8); automatic channel import; courier integrations
- [~] Limitations: no desktop screen for promotions, channels or sales settings (API only); "on credit" terms are a label with no credit limit; flat tax rate; prices above list not supported; single batch per return line unless the batch is given; see docs/orders.md
## Phase 5: Distributor credit
- [x] Distributor records: name/contact, discount terms, credit limit, payment terms, assigned stock keeper, active/inactive (not with a balance), notes; unique name
- [x] Agreed price list per distributor; dispatch price = agreed or standard distributor price less discount terms; prices preserved on the posted dispatch; price above agreement refused
- [x] Dispatches on credit: location, products, quantities, prices, discounts, total; stock out at average cost (FEFO) and ledger charge booked exactly once, on posting
- [x] Holds with approval by a different user: credit limit exceeded (limit 0 = no credit), back-dated beyond the allowance, extra discount beyond the allowance; limit check is concurrency-safe
- [x] Opening balance (once per distributor, as a dated invoice), receivable ledger append-only (trigger + unique source constraint), balance always computed, integrity check
- [x] Returns: request, receive into inspection (not saleable), inspect (restock / damaged / expired), credit approval by someone uninvolved, credit at original dispatch price applied to that dispatch
- [x] Exchanges: return credit plus linked replacement dispatch, original history preserved
- [x] Payments: partial, several dispatches, auto-allocate oldest first, unallocated on account then allocated later, over-allocation blocked under concurrency, back-dated payment approval, reversal by another user, idempotency
- [x] Adjustments: write-off / credit / extra charge, all approved by another user; capped at what is outstanding
- [x] Aging buckets and overdue, monthly statements with automatic carry-forward and a reconciliation flag
- [x] Stock keeper scoping (assigned or unassigned distributors, own locations); cost hidden without products.view_cost; every action audited
- [x] Desktop: Distributors module (Accounts, Dispatches, Returns, Payments, Adjustments, Aging) with account detail, dialogs and permission-driven buttons; checked at 1366x768 and 1920x1080 against the live backend
- [~] Not in Phase 5 (later phases): accounting journals (6, entries are flagged pending); reports, exports, dashboards (8); distributor portal; interest/late fees
- [~] Limitations: no desktop screen for the back-dating and extra-discount settings (API only); statements and aging are on-screen only; credit is at the dispatched price regardless of batch; see docs/distributors.md
## Phase 6: Accounting
- [x] Chart of accounts seeded on first use, extendable by `accounting.manage_coa`; system accounts protected; control accounts (inventory, payables, distributor receivables, GRNI, transit) closed to manual entries
- [x] Double-entry journals: debits = credits enforced in the service and by a deferred database trigger; posted entries and lines immutable at database level; corrections by reversal only
- [x] Automatic postings from existing records, once each (unique source key; concurrency-tested): goods receipts (Dr Inventory / Cr GRNI), supplier invoices, payments, credits and adjustments, customer sales at dispatch (or delivery, by setting) with COGS at weighted-average cost, customer payments / refunds / reversals, customer returns, distributor dispatches / payments / credits / adjustments / opening balances, write-offs, count variances, adjustments, transfers, opening stock, reversals
- [x] Cash and bank accounts (own ledger account each), payment-method mapping, expenses (approval limit, second-person approval, void by a third person), manual journals (draft, posted by a different person, reversed by someone other than the poster)
- [x] Fiscal year start, monthly periods, close (only after the month ends, no drafts, no pending records), reopen by request with reason and a different approver; system postings into a closed month shift to the first open month and keep their original date
- [x] Bank / cash reconciliation that refuses to complete unless the ticked lines equal the statement balance exactly; no plug entries; voidable by another user
- [x] Opening-balance batches with evidence and verified flag per line; unverified or unbalanced batches cannot be approved; approval by someone other than the author; go-live gate (pending = 0, ledger = sub-ledgers, trial balance balanced, equity cleared, written reconciliation statement, different person); provisional notice on every report until then
- [x] Reports from posted journals only: trial balance, P&L, balance sheet (retained / current-year earnings derived), direct cash flow, general ledger, receivables and payables ageing (FIFO), customer / supplier / distributor statements, sales-returns-discounts, expense summary, COGS and gross profit, inventory valuation (ledger vs stock), monthly and annual summaries; location filter where meaningful
- [x] Integrity view: ledger vs stock valuation, stock in transit, supplier ledger, distributor ledger, trial balance, pending records
- [x] Every action audited; permissions `accounting.approve`, `accounting.reconcile`, `accounting.opening_balances` added (CEO has all; Accounts has no `accounting.approve`)
- [x] Desktop: Accounting module (Overview and posting, Reports, Journal, Chart of accounts, Cash and bank, Expenses, Periods, Opening balances) with permission-driven buttons; checked at 1366x768 and 1920x1080 against the live backend
- [~] Not in Phase 6 (later phases or out of scope): tax computation and filing; employee-purchase receivables posting (7); report export, dashboards, approvals inbox (8); bank-statement import and auto-matching; year-end closing entries; depreciation, accruals, foreign currency; see docs/accounting.md
- [~] Limitations: stock movements post on the day they were recorded (not the document's received date); the payment-method-to-account mapping and the revenue-recognition policy are API settings (no desktop screen); report columns are fixed; reports are on-screen only
- [!] **Needs an accountant** before the figures are relied on: posting rules, tax treatment, opening balances and their evidence, who holds approve / close / reopen
## Phase 7: Employee purchases and attendance
- [x] Employee records (generated code, home location, optional linked user, device number, credit limit); a person can be linked to one employee; device numbers are unique
- [x] Employee purchases: sale plus stock movement (FEFO, expired and reserved stock skipped), idempotent, receipt reference; paid in full completes at once; credit waits for approval and releases goods only then
- [x] Central staff discount rules (price-list employee price, then category, then general); credit off by default and needs a limit; optional monthly cap
- [x] Payments and reversals recorded separately; whole-purchase void; a person cannot record, approve, pay or void their own purchase; recorder cannot approve, void or reverse
- [x] Ledger postings (Dr 1400 / Cr 4000 / Dr 4200 / Cr 2200, COGS from the stock movement, payments Dr cash Cr 1400, reversals); integrity row for employee receivables; employee statements and ageing
- [x] Attendance devices with no secrets in settings and an explicit time zone; adapter interface and registry; one generic CSV / TXT / XLSX adapter
- [x] Punch import: dry run, file hash refusal, de-duplication by device, user and instant, rejected rows with reasons, unmatched device numbers kept and matched when the employee is created or edited
- [x] Raw punches immutable (database trigger); policy per location (time zone, shift, grace, full-day minimum, overtime, weekly off, in/out mode); daily summaries, period summary, exceptions
- [x] Corrections (add or ignore a punch) requested then approved by a different person; audit of every step
- [x] Attendance visible only to attendance.view holders (CEO, manager by default); permissions employees.* and attendance.* added, with migration grants and downgrade
- [x] Desktop: Employees (Employees, Purchases, Discounts and credit) and Attendance (Daily, Period summary, Exceptions, Corrections, Import, Devices and policy) with permission-driven tabs and buttons; 88 live checks against the running backend at 1366x768 and 1920x1080
- [~] Not in Phase 7: a connector for a specific biometric device (brand, model, protocol, export format unknown); live device polling; partial returns of employee purchases; employee opening balances; leave, holidays, rosters, night shifts; payroll deduction; employee performance, dashboards, exports (8)
- [!] **Needs input:** biometric device details; attendance policy per location; employee credit decision and limits; staff discount rules; employees with device numbers; an accountant's review of tax treatment on staff sales
## Phase 8: Dashboards, reports, exports, approvals - [x]
- [x] Backend: report catalogue (20 reports, permission-gated, hidden = 404, location-scoped), reconciliation blocks, role-based dashboards, approvals inbox (16 kinds), PDF and Excel export with audit and formula guard; `/insight/*`
- [x] Desktop: Dashboard (replaces Overview for report users), Reports (catalogue, filters, reconciliation, export), Approvals (list, filter, open owning screen); live checks at 1366x768 and 1920x1080
- [x] Fixed during Phase 8: a shared FastAPI `Query` object made the location filter on reports ignore the chosen location for scoped users (caught by a test)
- [~] Not in Phase 8: scheduled or emailed reports; charts; non-Latin text in PDF; per-employee shifts and leave; decisions inside the inbox (taken on the owning screen)
- [!] **Needs input:** which reports finance wants as scheduled exports; accountant review of statements and tax treatment; the lateness and overdue thresholds you want (2 days delayed order, 3 days slow transfer)
## Phase 9: Testing, opening balances, backup drill, rollout - [x] (tooling and tests); [!] pilot and rollout need you
- [x] Cross-module integration test (`test_integration.py`): supplier, transfer, orders, distributor, employee purchase and expense in one month; trial balance balances; ledger equals every sub-ledger; balance sheet, P&L and reports agree; a refused operation changes nothing; posting twice changes nothing; every dashboard and report runs for every role
- [x] Security sweep (`test_security_sweep.py`): every one of the 285 routes is called as a user lacking the permission and must refuse; a test proves the sweep sees every OpenAPI path; permission tags (`require`, `require_any`) are the source of the API reference
- [x] Opening-balance workbook import (template, check = real run rolled back, row-level errors, all-or-nothing, capital must equal assets minus liabilities, no plug entry, same file refused twice, audited); supplier opening balance mechanism (there was none); desktop dialog checked live at 1366x768 and 1920x1080
- [x] Backup: encrypted (AES-256-GCM, passphrase from environment only), verify compares row counts and ledger/stock totals, `run-daily` (backup, verify, off-machine copy, prune), retention; real drill on the demo database passed
- [x] `python -m backend.cli readiness`: read-only go-live checks (production mode, secret, no demo users or locations, CEO exists, backup freshness, ledger status)
- [x] Docs: `rollout.md`, `user-and-role-administration.md`, `api.md` (generated), `backup-and-recovery.md`, accounting opening-balance section, regenerated permission matrix
- [~] Not built: WAL archiving or point-in-time recovery; automated scheduling (snippets are provided, you install them); load testing beyond the existing concurrency tests; a signed installer for the desktop client
- [!] **Needs you (cannot be done by software):** real opening balances with documents; an accountant's review of posting rules and tax; the pilot at one location and the staged rollout to all five, with the exit criteria in rollout.md; a second person with `accounting.approve`; biometric device details; custody of the backup passphrase; off-machine backup storage
