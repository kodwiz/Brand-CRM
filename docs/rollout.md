# Rollout plan: testing, pilot at one location, then all five

This is a plan and a set of gates. **Software cannot run the pilot or the rollout for you.** Everything below marked *you* needs your people, your documents and your decisions; everything marked *system* is built, tested and described in this repository.

## 0. What has been verified before any real data goes in

| Area | How it was checked (all with synthetic data) |
|---|---|
| Every financial transaction posts and fails cleanly | per-module tests in `backend/tests/` (success and rollback), plus `test_integration.py`: one month across stock, suppliers, transfers, orders, distributors, employee purchases and expenses, after which the trial balance balances and the ledger equals every sub-ledger |
| Nothing posts twice | repeat posting runs and repeat requests are tested for idempotence |
| Permissions | `test_security_sweep.py` calls **every** route (285) as a user who lacks the permission and requires a refusal; a separate test proves the sweep really sees every OpenAPI path; location scope answers 404 |
| Backup and restore | `test_backup.py` (checksums, restore into a new database, row counts, tamper and truncation rejection, wrong passphrase) and a real drill on the demo database |
| Opening-balance import | `test_opening_import.py` (14 tests) and a live desktop check at 1366x768 and 1920x1080 |
| Desktop screens | live scripts in `scripts/e2e_*_desktop.py` drive the real screens against the real backend |

## 1. Before the pilot (*you* with *system*)

1. **Production setup.** Own server or PC, PostgreSQL, `.env` with `ENVIRONMENT=production` and a random `JWT_SECRET`, `python -m alembic upgrade head`, `python -m backend.cli init`, `python -m backend.cli create-ceo`. Do **not** run `seed-demo`; it refuses in production.
2. **Run `python -m backend.cli readiness`.** It lists what blocks go-live (demo users or locations present, default secret, no recent backup, ledger status). Exit code 1 means a blocker remains. It changes nothing and prints no secrets.
3. **Create the real locations and users** ([user-and-role-administration.md](user-and-role-administration.md)). You need **two people able to approve** (`accounting.approve`): by default only the CEO role has it, so create a second CEO-role account for a trusted co-owner or finance lead. The person who enters opening balances cannot approve them.
4. **Schedule backups and prove a restore** ([backup-and-recovery.md](backup-and-recovery.md)). Store the backup passphrase where two people can find it; without it an encrypted backup cannot be opened.
5. **Chart of accounts, cash and bank accounts, fiscal-year start, revenue policy, expense approval limit.** An accountant should review posting rules and tax treatment first.
6. **Load the catalogue** (product import template) and **suppliers, customers, distributors**.

## 2. Opening balances (the gate to an official ledger)

1. Count and collect documents: stock count per location with verified unit costs, supplier statements, distributor balances, customer balances, cash count, bank statements, other assets and liabilities.
2. Download the template (Accounting > Opening balances > *Import workbook…* > *Download template*). Fill it **only** from documents. Every row needs an evidence reference and `yes` in the verified column. Leave a row out rather than estimate.
3. Choose the "as at" date and press *Check file*. A check enters everything for real and rolls it back, so what you see is what will happen. Every problem is listed by sheet and row. If anything is wrong **nothing** is imported.
4. If declared owner capital does not equal assets minus liabilities, the import is refused and the difference is shown. **The system never posts a plug.** Find the cause (a missing balance, a wrong cost) or have the owner record a documented decision.
5. *Import* creates a draft batch (stock, supplier and distributor balances take effect when pending records are posted). A second authorised person reviews the evidence and approves it.
6. Run *Post pending records*, then open Accounting > Overview. Every line of the go-live checklist must be green.
7. Someone other than the importer writes the reconciliation statement (who checked what, against which documents, any differences) and presses *Make the ledger official*.

## 3. Pilot at one location

Choose the location that is least risky and best supervised, with a person who will report problems.

**Scope:** that location's real stock, orders and payments only. Other locations stay on paper.
**Length:** at least two full weeks, covering a month-end if possible.

**Daily during the pilot (*you*):**
* Compare the system's end-of-day cash and stock to the paper register or a physical count. Record differences; do not hide them with adjustments.
* Run *Post pending records*; read the Integrity view. Non-zero differences are investigated the same day.
* Take the backup; confirm `run-daily` succeeded.
* Keep a short issue log: what went wrong, who, when, what was done.

**Exit criteria (all must hold):**
1. Ten consecutive working days where cash and stock counted at the location equal the system, or each difference is explained and documented.
2. Integrity view shows no unexplained difference on any of those days; trial balance balanced.
3. A restore from the pilot's backup has been tested into a scratch database and verified.
4. Staff can do their daily tasks without help; no open issue that loses or changes money.
5. The accountant has reviewed at least one month's statements.
6. A named person signs off in writing.

**Stop criteria:** any unexplained money or stock difference above a threshold you set in advance, a double posting, or a permission failure. Stop adding locations, restore from the last good backup if data was damaged, and fix before continuing.

## 4. Controlled rollout to the remaining locations

One location at a time, in the order you choose, never two in the same week at first:

1. Count and verify that location's stock; enter its opening stock through the workbook (dated the day it starts).
2. Create its users with only that location assigned; train them on the screens they will use (see the role matrix).
3. Run in parallel with paper for the first week, then paper stops.
4. Apply the same daily checks and a shorter exit review (five working days) before starting the next location.
5. After the fifth location, hold a review of issues and permissions; remove any access that was only for the rollout.

## 5. Rollback

* **Before** the ledger is declared official: restore the pre-import backup into a new database, point `DATABASE_URL` at it, and redo the import after fixing the file.
* **After** it is official: posted entries are never deleted. Mistakes are corrected by reversal or adjustment entries that are approved and audited. Restore from backup only for data damage, and accept that work after the backup is lost (no point-in-time recovery).

## 6. What this plan does not do

It cannot decide your opening figures, set your tax treatment, obtain your biometric device's export format, run the pilot, or train your staff. Those are inputs only you can supply (see the checklist in `implementation-checklist.md`).
