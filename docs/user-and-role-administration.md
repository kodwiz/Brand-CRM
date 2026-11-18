# User and role administration guide

For the person (normally the CEO) who creates accounts and decides who can do what. The full list of permissions per role is in [role-permission-matrix.md](role-permission-matrix.md); every route and the permission it needs is in [api.md](api.md).

## Principles

* **The server decides.** A button hidden on the desktop is a convenience; the backend refuses the action regardless of what the screen shows.
* **Least access.** Give each person the smallest role and the fewest locations that let them do their job.
* **Separation of duties.** Nobody approves their own work: stock requests, transfers, expenses, opening balances, payments, employee purchases and attendance corrections need a different person to approve. Because of this, **two people** must hold approval rights before you go live.
* **Everything is audited** (who, what, when, from where, with before and after values). Passwords are never logged.

## Default roles

| Role | Locations | Typical person | Can do |
|---|---|---|---|
| CEO | all | Owner | Everything, including users, roles and go-live |
| Manager | all | Operations manager | Operations and sales views and approvals; no accounting approval, no profit statements |
| Stock keeper | assigned only | Warehouse or branch stock person | Receive, count, transfer and dispatch stock at assigned locations |
| Order booker | assigned only | Sales person | Customers and own orders at assigned locations |
| Accounts | all | Finance staff | Ledger, payments, expenses, reconciliation, opening-balance entry; **no** `accounting.approve` |

Defaults are in `backend/permissions.py` and can be changed per role (below).

## Creating a user (Settings > Users, `users.manage`)

1. Username (letters, digits, `.` `_` `-`), full name, role, and **the locations** the person may work in. Roles marked "assigned only" see nothing outside those locations (the server answers "not found").
2. Set a temporary password of at least 10 characters. Tell the person in person, not in a shared chat. They should change it at first sign-in.
3. Do not share accounts. One person, one login; this is what makes the audit trail meaningful.

## Changing access

* **Move a person**: edit their locations (`PUT /users/{id}/locations`). Their open sessions pick the change up on next request.
* **Leave or suspension**: deactivate the account. History is kept; the person can no longer sign in.
* **Forgotten password**: reset it for them (`users.manage`); the old sessions end.
* **Lockout**: five failed sign-ins lock the account for 15 minutes and the attempt is logged.

## Editing a role (Settings > Roles and permissions, `roles.manage`)

Tick the permissions the role should have. Notes:
* `accounting.approve` approves manual journals, expenses, reversals, opening balances and go-live. Keep it to the few people who are accountable for the books.
* `accounting.opening_balances` enters opening batches; with `inventory.opening_stock`, `distributors.manage` and `distributors.approve` it also allows the workbook import.
* Anything that shows cost, profit or statements (`reports.financial`, pricing, `attendance.view`) is sensitive; grant deliberately.
* Roles apply to everyone holding them, immediately. Review the matrix after any edit and regenerate it with `python scripts/gen_permission_matrix.py`.

## Periodic review (suggested monthly)

1. List users; deactivate leavers.
2. Check each person's locations still match their job.
3. Read the audit history for role changes, resets and refused approvals (Reports > Audit history).
4. Confirm two active people still hold `accounting.approve`.

## Typical mistakes

* One shared "admin" login: the audit trail then proves nothing.
* Giving the importer approval rights on their own batch: the system refuses it, so you need a second approver.
* Giving a branch user "all locations": use assignments instead.
