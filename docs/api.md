# API reference

Generated from the running application by `python scripts/gen_api_docs.py` (285 operations). Do not edit by hand; regenerate after changing routes.

* Interactive documentation (Swagger UI) is at `/docs` and the machine-readable schema at `/openapi.json` **when ENVIRONMENT is not production**; both are disabled in production.
* Authenticate with `POST /auth/login` (`{username, password}`) and send `Authorization: Bearer <token>` on every call.
* **Permission** column: `a and b` means ALL are required, `a or b` means ANY. `login only` means any signed-in user (further filtering is by location scope inside the handler). The server enforces these; the desktop hiding a button is a convenience only.
* Errors: 401 not signed in · 403 permission or separation-of-duties refusal · 404 not found **or outside your locations** · 409 conflict (already done, duplicate) · 422 a business rule or invalid input.
* Money is sent and returned as decimal strings. Dates are ISO `YYYY-MM-DD`.

## accounting

| Method | Path | Permission | Summary |
|---|---|---|---|
| GET | `/accounting/accounts` | accounting.view | List Accounts |
| POST | `/accounting/accounts` | accounting.manage_coa | Add Account |
| PATCH | `/accounting/accounts/{account_id}` | accounting.manage_coa | Patch Account |
| GET | `/accounting/cash-accounts` | accounting.view or expenses.manage | Cash Accounts |
| POST | `/accounting/cash-accounts` | accounting.manage_coa | Add Cash Account |
| POST | `/accounting/cash-accounts/{cash_account_id}/deactivate` | accounting.manage_coa | Deactivate Cash |
| POST | `/accounting/cash-accounts/{cash_account_id}/reconcile` | accounting.reconcile | Reconcile |
| POST | `/accounting/cash-accounts/{cash_account_id}/reconcile/preview` | accounting.reconcile | Reconcile Preview |
| GET | `/accounting/cash-accounts/{cash_account_id}/reconciliations` | accounting.reconcile | Reconciliations |
| GET | `/accounting/cash-accounts/{cash_account_id}/unreconciled` | accounting.reconcile | Unreconciled |
| GET | `/accounting/expense-accounts` | expenses.manage or accounting.view | Expense Accounts |
| GET | `/accounting/expenses` | expenses.manage or accounting.view | List Expenses |
| POST | `/accounting/expenses` | expenses.manage | Add Expense |
| POST | `/accounting/expenses/{expense_id}/decision` | accounting.approve | Decide Expense |
| POST | `/accounting/expenses/{expense_id}/void` | accounting.approve | Void Expense |
| GET | `/accounting/go-live` | accounting.view | Go Live Status |
| POST | `/accounting/go-live` | accounting.approve | Go Live |
| GET | `/accounting/integrity` | accounting.view | Integrity |
| GET | `/accounting/journals` | accounting.view | List Journals |
| POST | `/accounting/journals/manual` | accounting.post | Manual Journal |
| DELETE | `/accounting/journals/{entry_id}` | accounting.post | Delete Journal |
| GET | `/accounting/journals/{entry_id}` | accounting.view | Get Journal |
| POST | `/accounting/journals/{entry_id}/post` | accounting.approve | Post Journal |
| POST | `/accounting/journals/{entry_id}/reverse` | accounting.approve | Reverse Journal |
| GET | `/accounting/opening-batches` | accounting.opening_balances or accounting.approve or accounting.view | List Batches |
| POST | `/accounting/opening-batches` | accounting.opening_balances | Create Batch |
| PUT | `/accounting/opening-batches/{batch_id}` | accounting.opening_balances | Update Batch |
| POST | `/accounting/opening-batches/{batch_id}/approve` | accounting.approve | Approve Batch |
| POST | `/accounting/opening-batches/{batch_id}/cancel` | accounting.opening_balances | Cancel Batch |
| POST | `/accounting/opening-import` | accounting.opening_balances and inventory.opening_stock and distributors.manage and distributors.approve | Opening Import |
| GET | `/accounting/opening-import/template` | accounting.opening_balances and inventory.opening_stock and distributors.manage and distributors.approve | Opening Import Template |
| GET | `/accounting/overview` | accounting.view | Overview |
| GET | `/accounting/periods` | accounting.view | Periods |
| POST | `/accounting/periods/{period_id}/close` | accounting.close_period | Close |
| POST | `/accounting/periods/{period_id}/reopen-requests` | accounting.reopen_period | Reopen Request |
| GET | `/accounting/posting/pending` | accounting.view | Pending |
| POST | `/accounting/posting/run` | accounting.post | Run Posting |
| POST | `/accounting/reconciliations/{reconciliation_id}/void` | accounting.approve | Void Reconciliation |
| POST | `/accounting/reopen-requests/{request_id}/decision` | accounting.approve | Reopen Decision |
| GET | `/accounting/reports/aging/{kind}` | reports.financial or accounting.view | R Aging |
| GET | `/accounting/reports/annual` | reports.financial or accounting.view | R Annual |
| GET | `/accounting/reports/balance-sheet` | reports.financial or accounting.view | R Bs |
| GET | `/accounting/reports/cash-flow` | reports.financial or accounting.view | R Cf |
| GET | `/accounting/reports/cogs` | reports.financial or accounting.view | R Cogs |
| GET | `/accounting/reports/expenses` | reports.financial or accounting.view | R Exp |
| GET | `/accounting/reports/general-ledger` | reports.financial or accounting.view | R Gl |
| GET | `/accounting/reports/income-statement` | reports.financial or accounting.view | R Pl |
| GET | `/accounting/reports/inventory-valuation` | reports.financial or accounting.view | R Inv |
| GET | `/accounting/reports/monthly` | reports.financial or accounting.view | R Monthly |
| GET | `/accounting/reports/sales` | reports.financial or accounting.view | R Sales |
| GET | `/accounting/reports/statement` | reports.financial or accounting.view | R Statement |
| GET | `/accounting/reports/trial-balance` | reports.financial or accounting.view | R Tb |
| GET | `/accounting/settings` | accounting.view | Get Settings |
| PUT | `/accounting/settings` | accounting.manage_coa | Put Settings |

## attendance

| Method | Path | Permission | Summary |
|---|---|---|---|
| GET | `/attendance/adapters` | attendance.view | Adapters |
| GET | `/attendance/corrections` | attendance.view | Corrections |
| POST | `/attendance/corrections` | attendance.manage | Request Correction |
| POST | `/attendance/corrections/{correction_id}/decision` | attendance.approve | Decide Correction |
| GET | `/attendance/days` | attendance.view | Days |
| GET | `/attendance/devices` | attendance.view | Devices |
| POST | `/attendance/devices` | attendance.manage | Create Device |
| PATCH | `/attendance/devices/{device_id}` | attendance.manage | Patch Device |
| GET | `/attendance/exceptions` | attendance.view | Exceptions |
| GET | `/attendance/imports` | attendance.view | Imports |
| POST | `/attendance/imports` | attendance.manage | Import Punches |
| GET | `/attendance/policies` | attendance.view | Policies |
| PUT | `/attendance/policies` | attendance.manage | Put Policy |
| GET | `/attendance/punches` | attendance.view | Punches |
| POST | `/attendance/rebuild` | attendance.manage | Rebuild |
| GET | `/attendance/summary` | attendance.view | Summary |

## audit

| Method | Path | Permission | Summary |
|---|---|---|---|
| GET | `/audit` | audit.view | List Audit |

## auth

| Method | Path | Permission | Summary |
|---|---|---|---|
| POST | `/auth/change-password` | login only | Change Password |
| POST | `/auth/login` | public | Login |
| POST | `/auth/logout` | login only | Logout |
| GET | `/auth/me` | login only | Me |

## catalogue

| Method | Path | Permission | Summary |
|---|---|---|---|
| GET | `/categories` | products.view | Categories |
| POST | `/categories` | products.manage | Create Category |
| GET | `/imports/template` | products.import | Import Template |
| POST | `/imports/validate` | products.import | Validate Import |
| GET | `/imports/{job_id}` | products.import | Get Import |
| POST | `/imports/{job_id}/commit` | products.import | Commit Import |
| GET | `/products` | products.view | List Products |
| POST | `/products` | products.manage | Create Product |
| GET | `/products/brands` | products.view | Product Brands |
| GET | `/products/{product_id}` | products.view | Get Product |
| PATCH | `/products/{product_id}` | products.manage | Update Product |
| GET | `/products/{product_id}/image` | products.view | Get Image |
| PUT | `/products/{product_id}/image` | products.manage | Upload Image |
| POST | `/products/{product_id}/variants` | products.manage | Add Variant |
| GET | `/variants/lookup` | products.view | Lookup Variants |
| PATCH | `/variants/{variant_id}` | products.manage | Update Variant |

## distributors

| Method | Path | Permission | Summary |
|---|---|---|---|
| GET | `/distributor-adjustments` | distributors.view | List Adjustments |
| POST | `/distributor-adjustments` | distributors.transact | Request Adjustment |
| POST | `/distributor-adjustments/{adjustment_id}/decide` | distributors.approve | Decide Adjustment |
| GET | `/distributor-dispatches` | distributors.view | List Dispatches |
| POST | `/distributor-dispatches` | distributors.transact | Create Dispatch |
| GET | `/distributor-dispatches/{invoice_id}` | distributors.view | Get Dispatch |
| PUT | `/distributor-dispatches/{invoice_id}` | distributors.transact | Update Dispatch |
| POST | `/distributor-dispatches/{invoice_id}/cancel` | distributors.transact | Cancel Dispatch |
| POST | `/distributor-dispatches/{invoice_id}/decide` | distributors.approve | Decide Dispatch |
| POST | `/distributor-dispatches/{invoice_id}/post` | distributors.transact | Post Dispatch |
| GET | `/distributor-payments` | distributors.view | List Payments |
| POST | `/distributor-payments` | distributors.receive_payment | Record Payment |
| GET | `/distributor-payments/{payment_id}` | distributors.view | Get Payment |
| POST | `/distributor-payments/{payment_id}/decide` | distributors.approve | Decide Payment |
| POST | `/distributor-payments/{payment_id}/reverse` | distributors.approve | Reverse Payment |
| GET | `/distributor-returns` | distributors.view | List Returns |
| POST | `/distributor-returns` | distributors.transact | Request Return |
| GET | `/distributor-returns/{return_id}` | distributors.view | Get Return |
| POST | `/distributor-returns/{return_id}/cancel` | distributors.transact | Cancel Return |
| POST | `/distributor-returns/{return_id}/decide` | distributors.approve | Decide Return |
| POST | `/distributor-returns/{return_id}/inspect` | distributors.transact | Inspect Return |
| POST | `/distributor-returns/{return_id}/receive` | distributors.transact | Receive Return |
| GET | `/distributors` | distributors.view | List Distributors |
| POST | `/distributors` | distributors.manage | Create Distributor |
| GET | `/distributors/integrity` | settings.manage or distributors.approve | Integrity |
| GET | `/distributors/settings` | distributors.view or settings.manage | Get Settings |
| PUT | `/distributors/settings` | settings.manage | Put Settings |
| GET | `/distributors/{distributor_id}` | distributors.view | Get Distributor |
| PUT | `/distributors/{distributor_id}` | distributors.manage | Update Distributor |
| GET | `/distributors/{distributor_id}/aging` | distributors.view | Get Aging |
| POST | `/distributors/{distributor_id}/allocate` | distributors.receive_payment | Allocate |
| POST | `/distributors/{distributor_id}/opening-balance` | distributors.manage and distributors.approve | Opening Balance |
| GET | `/distributors/{distributor_id}/prices` | distributors.view | List Prices |
| PUT | `/distributors/{distributor_id}/prices/{variant_id}` | distributors.manage | Put Price |
| GET | `/distributors/{distributor_id}/statement` | distributors.view | Get Statement |

## insight

| Method | Path | Permission | Summary |
|---|---|---|---|
| GET | `/insight/approvals` | approvals.view | Approvals |
| GET | `/insight/dashboard` | login only | Dashboard |
| GET | `/insight/reports` | login only | Catalogue |
| GET | `/insight/reports/{key}` | login only | Run Report |
| GET | `/insight/reports/{key}/export` | reports.export | Export Report |

## inventory

| Method | Path | Permission | Summary |
|---|---|---|---|
| GET | `/inventory/alerts` | inventory.view | Alerts |
| GET | `/inventory/batches` | inventory.view or transfers.view | List Batches |
| GET | `/inventory/counts` | inventory.count | List Counts |
| POST | `/inventory/counts` | inventory.count | Create Count |
| GET | `/inventory/counts/{count_id}` | inventory.count | Get Count |
| POST | `/inventory/counts/{count_id}/cancel` | inventory.count | Cancel Count |
| PUT | `/inventory/counts/{count_id}/entries` | inventory.count | Enter Counts |
| POST | `/inventory/counts/{count_id}/submit` | inventory.count | Submit Count |
| GET | `/inventory/integrity` | settings.manage | Integrity |
| GET | `/inventory/movements` | inventory.view | Movements |
| POST | `/inventory/moves` | inventory.adjust | Move |
| POST | `/inventory/opening-stock` | inventory.opening_stock | Opening Stock |
| GET | `/inventory/receipts` | inventory.view | List Receipts |
| POST | `/inventory/receipts` | inventory.receive | Create Receipt |
| GET | `/inventory/receipts/{receipt_id}` | inventory.view | Get Receipt |
| POST | `/inventory/receipts/{receipt_id}/cancel` | inventory.receive | Cancel Receipt |
| POST | `/inventory/receipts/{receipt_id}/post` | inventory.receive | Post Receipt |
| GET | `/inventory/requests` | inventory.view | List Requests |
| POST | `/inventory/requests` | inventory.adjust | Create Request |
| POST | `/inventory/requests/{request_id}/approve` | inventory.view | Approve Request |
| POST | `/inventory/requests/{request_id}/cancel` | inventory.adjust | Cancel Request |
| POST | `/inventory/requests/{request_id}/reject` | inventory.view | Reject Request |
| GET | `/inventory/settings` | inventory.view | Get Settings  |
| PUT | `/inventory/settings/costing` | settings.manage | Set Costing |
| PUT | `/inventory/settings/thresholds` | settings.manage | Set Thresholds |
| GET | `/inventory/stock` | inventory.view | Stock |
| GET | `/inventory/stock/{variant_id}/batches` | inventory.view | Stock Batches |
| POST | `/inventory/transactions/{txn_id}/reverse` | inventory.reverse | Reverse |
| GET | `/inventory/valuation` | inventory.view_valuation | Valuation |
| GET | `/transfers` | transfers.view | List Transfers |
| POST | `/transfers` | transfers.request | Create Transfer |
| GET | `/transfers/locations` | transfers.view | Transfer Locations |
| GET | `/transfers/{transfer_id}` | transfers.view | Get Transfer |
| POST | `/transfers/{transfer_id}/approve` | transfers.approve | Approve Transfer |
| POST | `/transfers/{transfer_id}/cancel` | transfers.request | Cancel Transfer |
| POST | `/transfers/{transfer_id}/dispatch` | transfers.dispatch | Dispatch Transfer |
| POST | `/transfers/{transfer_id}/flag-discrepancy` | transfers.receive | Flag Discrepancy |
| POST | `/transfers/{transfer_id}/receive` | transfers.receive | Receive Transfer |
| POST | `/transfers/{transfer_id}/reject` | transfers.approve | Reject Transfer |
| POST | `/transfers/{transfer_id}/resolve-discrepancy` | transfers.approve_discrepancy | Resolve Discrepancy |

## locations

| Method | Path | Permission | Summary |
|---|---|---|---|
| GET | `/locations` | locations.view | List Locations |
| POST | `/locations` | locations.manage | Create Location |
| PATCH | `/locations/{location_id}` | locations.manage | Update Location |

## meta

| Method | Path | Permission | Summary |
|---|---|---|---|
| GET | `/health` | public | Health |

## people

| Method | Path | Permission | Summary |
|---|---|---|---|
| GET | `/employees` | employees.view | List Employees |
| POST | `/employees` | employees.manage | Create Employee |
| GET | `/employees/discount-rules` | employees.manage_discounts or employees.purchase_view or employees.purchase | List Rules |
| POST | `/employees/discount-rules` | employees.manage_discounts | Create Rule |
| PATCH | `/employees/discount-rules/{rule_id}` | employees.manage_discounts | Patch Rule |
| GET | `/employees/lookup` | employees.purchase or employees.purchase_view or employees.view | Lookup |
| GET | `/employees/purchases` | employees.purchase_view or employees.purchase | List Purchases |
| POST | `/employees/purchases` | employees.purchase | Create Purchase |
| POST | `/employees/purchases/payments/{payment_id}/reverse` | employees.purchase_pay | Reverse Pay |
| POST | `/employees/purchases/quote` | employees.purchase | Quote |
| GET | `/employees/purchases/{purchase_id}` | employees.purchase_view or employees.purchase | Get Purchase |
| POST | `/employees/purchases/{purchase_id}/decision` | employees.purchase_approve | Decide |
| POST | `/employees/purchases/{purchase_id}/payments` | employees.purchase_pay | Pay |
| POST | `/employees/purchases/{purchase_id}/void` | employees.purchase_approve | Void |
| GET | `/employees/settings` | employees.manage_discounts or employees.purchase_view or employees.purchase | Get Settings |
| PUT | `/employees/settings` | employees.manage_discounts | Put Settings |
| GET | `/employees/{employee_id}` | employees.view | Get Employee |
| PATCH | `/employees/{employee_id}` | employees.manage | Patch Employee |

## purchasing

| Method | Path | Permission | Summary |
|---|---|---|---|
| GET | `/purchase-orders` | purchasing.view | List Pos |
| POST | `/purchase-orders` | purchasing.manage and products.view_cost | Create Po |
| GET | `/purchase-orders/{po_id}` | purchasing.view | Get Po |
| PUT | `/purchase-orders/{po_id}` | purchasing.manage and products.view_cost | Update Po |
| POST | `/purchase-orders/{po_id}/approve` | purchasing.approve | Approve Po |
| POST | `/purchase-orders/{po_id}/cancel` | purchasing.manage | Cancel Po |
| POST | `/purchase-orders/{po_id}/close` | purchasing.manage | Close Po |
| GET | `/purchase-orders/{po_id}/matching` | purchasing.view | Po Matching |
| GET | `/purchasing/grni` | suppliers.view and purchasing.view | Grni |
| GET | `/purchasing/integrity` | settings.manage | Purchasing Integrity |
| GET | `/purchasing/payables` | suppliers.view | Payables |
| GET | `/purchasing/settings` | purchasing.view or suppliers.view | Get Purchasing Settings |
| PUT | `/purchasing/settings` | settings.manage | Set Purchasing Settings |
| GET | `/supplier-adjustments` | suppliers.view | List Adjustments |
| POST | `/supplier-adjustments` | purchasing.manage and suppliers.view | Request Adjustment |
| POST | `/supplier-adjustments/{adj_id}/approve` | purchasing.approve and suppliers.view | Approve Adjustment |
| POST | `/supplier-adjustments/{adj_id}/cancel` | purchasing.manage | Cancel Adjustment |
| POST | `/supplier-adjustments/{adj_id}/reject` | purchasing.approve and suppliers.view | Reject Adjustment |
| GET | `/supplier-invoices` | suppliers.view | List Invoices |
| POST | `/supplier-invoices` | purchasing.manage and suppliers.view | Create Invoice |
| GET | `/supplier-invoices/{invoice_id}` | suppliers.view | Get Invoice |
| POST | `/supplier-invoices/{invoice_id}/cancel` | purchasing.manage | Cancel Invoice |
| POST | `/supplier-invoices/{invoice_id}/decide` | purchasing.approve_variance | Decide Invoice |
| POST | `/supplier-invoices/{invoice_id}/post` | purchasing.manage | Post Invoice |
| POST | `/supplier-invoices/{invoice_id}/void` | purchasing.reverse | Void Invoice |
| GET | `/supplier-payments` | suppliers.view | List Payments |
| POST | `/supplier-payments` | purchasing.pay and suppliers.view | Create Payment |
| GET | `/supplier-payments/{payment_id}` | suppliers.view | Get Payment |
| POST | `/supplier-payments/{payment_id}/allocate` | purchasing.pay | Allocate Payment |
| POST | `/supplier-payments/{payment_id}/reverse` | purchasing.reverse | Reverse Payment |
| GET | `/supplier-returns` | purchasing.view | List Returns |
| POST | `/supplier-returns` | purchasing.manage | Create Return |
| GET | `/supplier-returns/{return_id}` | purchasing.view | Get Return |
| POST | `/supplier-returns/{return_id}/cancel` | purchasing.manage | Cancel Return |
| POST | `/supplier-returns/{return_id}/dispatch` | purchasing.manage | Dispatch Return |
| GET | `/suppliers` | suppliers.view or purchasing.view | List Suppliers |
| POST | `/suppliers` | suppliers.manage | Create Supplier |
| GET | `/suppliers/{supplier_id}` | suppliers.view | Get Supplier |
| PUT | `/suppliers/{supplier_id}` | suppliers.manage | Update Supplier |
| POST | `/suppliers/{supplier_id}/opening-balance` | accounting.opening_balances | Supplier Opening Balance |
| GET | `/suppliers/{supplier_id}/statement` | suppliers.view | Statement |

## roles

| Method | Path | Permission | Summary |
|---|---|---|---|
| GET | `/roles` | users.view | List Roles |
| GET | `/roles/permission-catalogue` | roles.manage | Permission Catalogue |
| PUT | `/roles/{role_id}/permissions` | roles.manage | Set Role Permissions |

## sales

| Method | Path | Permission | Summary |
|---|---|---|---|
| GET | `/customers` | customers.view | List Customers |
| POST | `/customers` | customers.manage | Create Customer |
| GET | `/customers/{customer_id}` | customers.view | Get Customer |
| PUT | `/customers/{customer_id}` | customers.manage | Update Customer |
| GET | `/customers/{customer_id}/orders` | customers.view | Customer Orders |
| POST | `/order-payments/{payment_id}/reverse` | orders.refund | Reverse Order Payment |
| GET | `/orders` | orders.view_all or orders.view_own or orders.fulfil | List Orders |
| POST | `/orders` | orders.create | Create Order |
| GET | `/orders/my-summary` | orders.view_own or orders.view_all | My Summary |
| POST | `/orders/quote` | orders.create | Quote |
| GET | `/orders/{order_id}` | orders.view_all or orders.view_own or orders.fulfil | Get Order |
| PUT | `/orders/{order_id}` | orders.create | Update Order |
| POST | `/orders/{order_id}/cancel` | orders.update_status | Cancel Order |
| POST | `/orders/{order_id}/confirm` | orders.create | Confirm Order |
| POST | `/orders/{order_id}/discount-decision` | discounts.approve | Discount Decision |
| POST | `/orders/{order_id}/payments` | orders.payments | Record Payment |
| POST | `/orders/{order_id}/refunds` | orders.refund | Refund Overpayment |
| POST | `/orders/{order_id}/reopen` | orders.view_all and orders.update_status | Reopen Order |
| POST | `/orders/{order_id}/reserve-remaining` | orders.update_status | Reserve Remaining |
| POST | `/orders/{order_id}/returns` | orders.update_status or orders.returns | Request Return |
| POST | `/orders/{order_id}/shipments` | orders.fulfil | Pack Order |
| GET | `/promotions` | pricing.manage or orders.create or orders.view_all | List Promotions |
| POST | `/promotions` | pricing.manage | Create Promotion |
| PUT | `/promotions/{promotion_id}` | pricing.manage | Update Promotion |
| GET | `/sales-returns` | orders.view_all or orders.view_own or returns.receive | List Returns |
| GET | `/sales-returns/{return_id}` | orders.view_all or orders.view_own or returns.receive | Get Return |
| POST | `/sales-returns/{return_id}/cancel` | orders.update_status or orders.returns | Cancel Return |
| POST | `/sales-returns/{return_id}/decide` | orders.returns | Decide Return |
| POST | `/sales-returns/{return_id}/inspect` | returns.receive | Inspect Return |
| POST | `/sales-returns/{return_id}/receive` | returns.receive | Receive Return |
| POST | `/sales-returns/{return_id}/settle` | orders.refund | Settle Return |
| GET | `/sales/channels` | orders.create or orders.view_all or orders.view_own or customers.view or settings.manage | Channels |
| POST | `/sales/channels` | settings.manage | Create Channel |
| PUT | `/sales/channels/{channel_id}` | settings.manage | Update Channel |
| GET | `/sales/integrity` | settings.manage | Sales Integrity |
| GET | `/sales/settings` | orders.create or orders.view_all or settings.manage or pricing.manage | Get Sales Settings |
| PUT | `/sales/settings` | settings.manage | Put Sales Settings |
| GET | `/shipments` | orders.fulfil | List Shipments |
| POST | `/shipments/{shipment_id}/dispatch` | orders.fulfil | Dispatch |
| POST | `/shipments/{shipment_id}/outcome` | orders.fulfil or orders.update_status | Shipment Outcome |
| POST | `/shipments/{shipment_id}/receive-back` | orders.fulfil and returns.receive | Receive Back |
| POST | `/shipments/{shipment_id}/redispatch` | orders.fulfil | Redispatch |
| POST | `/shipments/{shipment_id}/unpack` | orders.fulfil | Unpack |

## users

| Method | Path | Permission | Summary |
|---|---|---|---|
| GET | `/users` | users.view | List Users |
| POST | `/users` | users.manage | Create User |
| PATCH | `/users/{user_id}` | users.manage | Update User |
| PUT | `/users/{user_id}/locations` | users.manage | Set User Locations |
| POST | `/users/{user_id}/reset-password` | users.manage | Reset Password |
