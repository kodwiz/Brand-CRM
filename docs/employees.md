# Employee purchases

Staff buy company stock at a staff price. Each purchase is a real sale and a real stock movement, posted to the ledger like any other. Rules are enforced by the server; the
desktop only collects input. Who may see or do what is in [role-permission-matrix.md](role-permission-matrix.md).

## Who can do what

| Action | Permission | Default holders |
|---|---|---|
| Record a purchase at the counter | `employees.purchase` | CEO, stock keeper |
| See purchases and balances | `employees.purchase_view` | CEO, manager, accounts |
| Approve or reject credit; void a purchase | `employees.purchase_approve` | CEO, manager |
| Record or reverse a payment | `employees.purchase_pay` | CEO, accounts |
| Staff discount rules, credit switch, monthly cap | `employees.manage_discounts` | CEO, manager |
| Employee records (name, device number, credit limit, linked user) | `employees.view`, `employees.manage` | CEO, manager |

A person with only `employees.purchase` gets a **name list** of active staff at their own locations, and sees only the purchases they recorded. Order bookers have none of these.
Accounts see purchases and balances but not personnel records or attendance.

## How a purchase works

1. Choose the employee, the location the goods come from, the date and the items. The screen shows the **live quote** from the server (list price, discount, tax, amount to pay).
2. **Paid in full:** the purchase completes at once and the stock is issued.
3. **Part or all on credit:** the purchase is `pending_approval` and **no goods are released**. A different person approves; approval issues the goods and leaves the unpaid
   amount on the employee's account. A rejected purchase never touches stock.
4. Later payments are recorded separately (cash, bank, card, other), each with its own reference, and can be reversed by someone other than the person who recorded them.
5. **Void** returns the goods to stock, reverses every payment in the books and reverses the sale. Money already handed over must be returned outside the system.

Credit is refused unless the company switch **Allow employees to buy on credit** is on **and** the employee has a credit limit above 0. The switch starts **off**. An optional monthly
cap limits what one employee may buy per month (0 = no cap).

## Discounts

In order of precedence for each item: the price list's **employee price** (when lower than retail), then the **category rule**, then the **all-products rule**, then no discount.
Rules are central (Discounts and credit tab), have a percentage above 0 and up to 100, and can be switched off. Every change is audited. No discount is typed per sale.

## Separation of duties (server-enforced)

* An employee linked to a user account cannot record, approve, pay or void **their own** purchase.
* Credit is approved by someone other than the person who recorded it.
* A void or a payment reversal is done by someone other than the recorder; the stock reversal inside a void also refuses the person who issued the stock.

## Ledger

Dr 1400 Employee receivables (total, party = employee) / Cr 4000 Sales (list price) / Dr 4200 Discounts / Cr 2200 Tax payable; cost of goods and inventory come from the stock
movement (Dr 5000 / Cr inventory). A payment is Dr cash or bank / Cr 1400. Voids and payment reversals post reversing journals. The Accounting integrity view has an
**Employee receivables** row that must agree with the sum of what employees owe. Tax uses the existing `sales.tax_rate_pct`; **have an accountant confirm the tax and statutory
treatment of sales to staff.**

## Limits of this version

* A void cancels the **whole** purchase. There are no partial returns of employee purchases.
* Employee **opening balances** (amounts owed before go-live) are not migrated, and the opening-balance batch is not wired to individual employee accounts. Agree with your accountant how any such balances are brought in.
* Deduction from payroll is not built; payments are recorded by hand.
* Reports are on-screen. Employee performance views and exports belong to Phase 8.
