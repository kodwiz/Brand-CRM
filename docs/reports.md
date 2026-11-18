# Dashboards, reports, exports and the approvals inbox (Phase 8)

Everything here is **read-only**: it reads posted records and changes nothing. It never shows a figure the backend cannot supply.

## Who sees what

The server decides, from permissions rather than role names.

| Dashboard view | Chosen when the user holds | Typical roles |
|---|---|---|
| Management | `reports.operational` and `reports.summaries` | CEO, Manager |
| Financial | `reports.financial` without `reports.operational` | Accounts |
| Operations | inventory or fulfilment permissions | Stock keeper (assigned locations only) |
| My dashboard | anyone else with `orders.view_own` | Order booker (own orders only) |

Inside a view each section appears only if its permission is held. Profit, expenses and ledger balances need `reports.financial`, so a **manager sees sales but not profit**.
A location-scoped user can only choose their own locations; anything else answers "not found", as everywhere else in the system.

**Revenue, cash collected, receivables and payables, stock value, cash balances and profit are shown under separate headings** and are never added together:
revenue is what was sold, cash is what was received, receivables are what is still owed, stock value is stock at weighted-average cost, profit is what is left after cost and expenses.

Money figures come from **posted journals**. Operational counts (damaged units, delayed orders, transfers waiting) come from the operational records.

Fixed thresholds, shown in the alert text: an order confirmed more than **2 days** ago and not dispatched is *delayed*; a transfer on the road for more than **3 days** is *slow*.
A distributor is *overdue* when part of its balance is older than its own payment terms (it is measured with the ageing buckets, so it is accurate to the bucket).

## Reports

20 reports, each only offered to users holding its permissions. A report you may not run answers **404**, exactly as if it did not exist.

| Group | Report | Needs |
|---|---|---|
| Sales | Sales by date and location; Product performance; Discounts and promotions | `reports.summaries` |
| Sales | Customer orders and returns | `reports.operational` |
| Stock | Inventory movement; Expiry and damage; Transfer reconciliation | `reports.operational` + the stock permission |
| Stock | Stock valuation | `reports.financial` + `inventory.view_valuation` |
| People | Order booker performance | `reports.operational` |
| People | Employee performance; Attendance | `employees.view` / `attendance.view` |
| Money | Receivables ageing; Payables ageing and supplier balances; Customer, distributor, supplier or employee statement; Expenses; Cash and bank movements | `reports.financial` |
| Statements | Profit and loss; Balance sheet; Trial balance | `reports.financial` |
| Control | Audit history (capped at 5,000 rows) | `audit.view` |

Company-wide financial reports are for users who can see every location. A location-scoped user must choose one of their locations for reports that allow it.

### Reconciliation

Reports built from operational records carry a **reconciliation block** showing the report total, the ledger total and the difference. It is never hidden, and a difference is shown as a difference.
Sales by date and location and Product performance are checked against the ledger's sales report; Stock valuation against the general ledger's stock account.
A difference usually means records not yet posted (press **Post pending records** in Accounting) or the timing of a return.

## Export (PDF and Excel)

* Needs `reports.export` **and** the permission for that report. The server checks again on every export.
* Every export is recorded in the audit log as `report.export`, with the report, the filters and the row count.
* Responses are `Content-Disposition: attachment` and `Cache-Control: no-store`.
* Excel cells whose text begins with `=`, `+`, `-` or `@` are stored as text, so a customer or product name cannot run as a formula when the file is opened.
* At most **50,000 rows** per export; narrow the filters beyond that.
* PDF uses standard Latin fonts. Text in other scripts (Urdu, Arabic) will **not** render correctly in the PDF; use the Excel export for those.

## Approvals inbox

One list of every request waiting for a decision that **the signed-in user is allowed to decide**: stock adjustments and write-offs, transfers and transfer shortages, order discounts,
customer returns, held distributor dispatches/payments/adjustments, held supplier invoices and adjustments, expenses, period reopen/close requests, manual journals, opening balances,
employee credit purchases and attendance corrections.

The inbox only **lists**. A decision is taken on the screen that owns the request (**Open on its screen**), so each module's own rules (a different person must approve, limits, stock
checks) stay in one place. Requests you raised are marked **You**, because someone else has to decide them.

## Not in Phase 8

* No scheduled or emailed reports; no saved report definitions.
* No charts: figures and tables only.
* Employee performance reports attendance and staff purchases; there is no per-employee shift or leave model, so lateness is measured against the location policy only.
* PDF cannot render non-Latin scripts (above).
* Tax and statutory treatment (sales tax, withholding) is not modelled. **Have an accountant review the statements before relying on them.**
