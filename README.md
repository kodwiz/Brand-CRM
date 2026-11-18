# Cosmetics Operations

An open-source, multi-location business management system for retail and distribution businesses (built around cosmetics, but nothing in it is specific to one brand): inventory, purchasing, sales orders, distributor credit, a double-entry ledger, employee purchases and attendance, dashboards, reports and encrypted backups. FastAPI backend, PostgreSQL, PySide6 desktop client, server-enforced role and location permissions.

**Status: all nine planned phases are built and tested** (344 backend tests, 13 desktop tests, plus live desktop checks at 1366x768 and 1920x1080 against a running backend). See [docs/implementation-checklist.md](docs/implementation-checklist.md) for exactly what exists and what is deliberately not built (for example point-in-time recovery).

> **Read before using with real money.** This software is provided as is, without warranty (see [LICENSE](LICENSE)). It is not accounting, tax or legal advice: have a qualified accountant review the chart of accounts, posting rules and tax treatment before you rely on the books. Run a pilot and a restore drill first ([docs/rollout.md](docs/rollout.md)). The default currency is PKR; change it with `DEFAULT_CURRENCY` in `.env`.

## Try it in five minutes with synthetic data

After the setup in sections 1-3 below, `python scripts/setup_demo.py` builds a separate `cosmetics_demo` database with sample locations, users, products, stock, customers and orders. Everything in it is made up. Never use demo users or data in production.

## Documentation

[Rollout plan](docs/rollout.md) · [User and role administration](docs/user-and-role-administration.md) · [API reference](docs/api.md) · [Backup and recovery](docs/backup-and-recovery.md) · [Accounting](docs/accounting.md) · [Role permission matrix](docs/role-permission-matrix.md)

Contributions and bug reports are welcome. Security problems: see [SECURITY.md](SECURITY.md); please do not open a public issue for them.

## Phase 1 (foundation)

- Authentication: argon2 password hashing, server-side revocable sessions (JWT + `auth_sessions`), account lockout, login rate limit, forced password change for new/reset accounts.
- Role- and location-based authorization enforced in the backend; role permissions are data and editable by the CEO.
- Locations, users, role administration, and an append-only audit log (database trigger rejects UPDATE/DELETE).
- Desktop shell: sign-in, permission-driven navigation, location selector, Overview, Settings (Users, Locations, Roles and permissions, Audit log).
- 54 backend tests + 9 desktop tests (Phase 1).

## What Phase 2 adds (catalogue and inventory only)

- Product catalogue with variants (shade/size), prices, barcodes, batch/expiry flags, images, search/sort/filter/pagination.
- Excel/CSV bulk import: downloadable blank template, validate-before-save review, all-or-nothing commit.
- Location-wise stock ledger with weighted-average costing ([docs/inventory-costing.md](docs/inventory-costing.md)), receiving (accepted / damaged / rejected),
  opening stock, damaged / expired / inspection buckets, reservations (no overselling), counts, approvals, reversals, alerts, valuation.
- Transfers: requested, approved, dispatched, partially received, discrepancy review, received.
- **Not operational yet:** purchasing, sales orders, distributor credit, accounting postings. Stock transactions carry
  `accounting_status = pending` for the Phase 6 hook; nothing is posted to any ledger today. Inventory value shown is
  an operational figure, not an accounting balance.

## What Phase 3 adds (suppliers and purchasing only)

- Suppliers, purchase orders with independent approval, goods receipts against orders (accepted / damaged / rejected, over-receipt blocked), additional receiving costs
  capitalised into stock cost, supplier invoices with three-way matching (ordered / received / invoiced), held invoices for price or quantity variances, partial payments,
  supplier credits and returns, statements, payables aging, goods received not invoiced. Rules and limits: [docs/purchasing.md](docs/purchasing.md).
- **Not operational yet:** accounting journals. Supplier ledger entries and stock movements are flagged `accounting_status = pending` for Phase 6;
  nothing is posted to a general ledger. Sales, customers and distributor credit are Phases 4-5.
- Upgrading an existing database: `alembic upgrade head` (the new permissions are granted to existing roles by the migration; check Settings > Roles).

## What Phase 4 adds (customers and orders only)

- Customer records, manual order entry from configurable channels, a pricing and promotion engine with approval of exceptional discounts, stock reservation at confirmation,
  packing / dispatch / delivery outcomes, customer payment tracking on the order, and customer returns with inspection before restock, refunds and store credit.
  Rules and limits: [docs/orders.md](docs/orders.md).
- **Not operational yet:** accounting journals for sales, payments and refunds (Phase 6); distributor credit (Phase 5); sales reports and exports (Phase 8); automatic import from WhatsApp / website / social channels.
  Promotions, channels and sales settings are managed through the API only; there is no desktop screen for them yet.
- Upgrading an existing database: `python -m alembic upgrade head`. Then check the sales settings: the manual discount limit starts at **0**, so every manual discount needs approval until you set one.

## What Phase 5 adds (distributor credit only)

- Distributor accounts with agreed prices, discount terms, credit limits and payment terms; goods dispatched on credit; returns and exchanges with inspection before credit;
  payments with allocation to specific dispatches (or left on account); approvals for over-limit, back-dated and extra-discount dispatches, back-dated payments and adjustments;
  receivable aging and monthly statements with automatic carry-forward. The balance is computed from an append-only ledger, never typed. Rules and limits: [docs/distributors.md](docs/distributors.md).
- **Not operational yet:** report export and dashboards (Phase 8). No distributor login. (Accounting journals arrived in Phase 6.)
- Upgrading an existing database: `python -m alembic upgrade head`. Then set each distributor's **credit limit** (it starts at **0**, meaning every dispatch waits for approval),
  agreed prices, assigned stock keeper and opening balance from Distributors > Accounts.

## What Phase 6 adds (accounting)

- A chart of accounts and a double-entry ledger fed automatically from stock, suppliers, distributors, customer orders and payments, plus expenses, cash and bank accounts with
  reconciliation, periods with close and approved reopening, opening-balance batches with evidence, a go-live gate, and financial reports (trial balance, profit and loss,
  balance sheet, cash flow, ledgers, ageing, statements, sales, expenses, COGS, inventory valuation, monthly and annual summaries). All figures come from posted journals.
  Rules, controls and limits: [docs/accounting.md](docs/accounting.md).
- (Report export, dashboards and the approvals inbox arrived in Phase 8; employee-purchase postings arrived in Phase 7.) **Tax treatment must be validated by a qualified accountant.**
- Upgrading an existing database: `python -m alembic upgrade head`. Then, in Accounting: press **Post pending records** (this posts the history already in the system), enter and verify
  opening balances, review the **expense approval limit** (starts at **0**: every expense waits for approval), choose revenue recognition before the first sale posts, and have a second
  person declare the ledger official once the checks on the Overview tab are met.

## What Phase 7 adds (employee purchases and attendance)

- **Employee purchases:** staff buy at a staff price set centrally (price-list employee price, category rule or general rule). Each purchase is a sale and a stock movement, posted to
  the ledger (employee receivables, account 1400). Fully paid purchases complete at once; credit waits for a second person to approve before goods are released. Payments, reversals
  and voids are separate, audited records; nobody can record, approve, pay or void their own purchase. Credit is **off** until the company allows it and sets a per-employee limit.
  Rules and limits: [docs/employees.md](docs/employees.md).
- **Attendance:** a device-neutral adapter layer and one **generic CSV / Excel** adapter; punch import with de-duplication (immutable raw punches, enforced in the database); daily and
  period summaries; missing-punch exceptions; corrections that need a second person; per-location policy. Visible only to the CEO, managers and authorised users.
  Rules and limits: [docs/attendance.md](docs/attendance.md).
- **Not operational yet:** a connector for any specific biometric device (brand, model, protocol and export format are needed first, and nothing is assumed); partial returns of employee
  purchases; leave, holidays and night shifts; payroll; employee performance, dashboards and exports (Phase 8). **Have an accountant review the tax treatment of staff sales.**
- Upgrading an existing database: `python -m alembic upgrade head`. Then, in Accounting, press **Post pending records**. In Employees > Discounts and credit set the staff discount rules
  and decide whether credit is allowed; add employees with their device numbers; in Attendance add the device and a policy for each location before importing.

## What Phase 8 adds (dashboards, reports, exports, approvals)

- **Dashboards** with period and location filters, chosen by permission: management (CEO, manager), financial (accounts), operations (stock keeper, own locations) and a personal one for order
  bookers. Revenue, cash collected, receivables, stock value and profit are separate headings. A manager sees sales but not profit.
- **20 reports** (sales, products, stock movement and valuation, expiry and damage, transfers, ageing, statements, expenses, cash, profit and loss, balance sheet, trial balance, attendance,
  employee and booker performance, discounts, audit history), each showing how it reconciles to the ledger.
- **PDF and Excel export**, permission-checked and written to the audit log; formula-injection safe. PDF is Latin-script only.
- **Approvals inbox**: every request you may decide, in one list; decisions are still taken on the owning screen.
- New dependency: `reportlab` (run `pip install -r requirements.txt`). No database migration in this phase.
- Details and limits: [docs/reports.md](docs/reports.md). **Have an accountant review the statements and tax treatment.**
- Not in this phase: scheduled or emailed reports, charts. (Integration tests, backup drills, opening-balance import and rollout planning arrived in Phase 9.)

## What Phase 9 adds (testing, opening balances, backup drill, rollout)

- **Tests:** a one-month cross-module integration test (the books must balance and agree five ways) and a security sweep that calls every route as someone without the permission.
- **Opening balances:** Accounting > Opening balances > *Import workbook…* (check first, nothing saved by a check, all-or-nothing, never a balancing plug). Suppliers can now carry an opening balance.
- **Backups:** optional AES-256-GCM encryption (passphrase only from the `BACKUP_PASSPHRASE` environment variable), verification against ledger and stock totals, and one scheduled command, `run-daily`.
- **`python -m backend.cli readiness`** lists what still blocks go-live.
- New dependency: `cryptography` (run `pip install -r requirements.txt`). No database migration in this phase.
- Guides: [rollout.md](docs/rollout.md), [user-and-role-administration.md](docs/user-and-role-administration.md), [api.md](docs/api.md), [backup-and-recovery.md](docs/backup-and-recovery.md).
- **Not done by software:** your real opening figures, the pilot and the five-location rollout, accountant review, point-in-time recovery.

Blank import template: `templates/product-import-template.xlsx` (also downloadable from Products > Import, or `GET /imports/template`).
Backup and recovery: [docs/backup-and-recovery.md](docs/backup-and-recovery.md).

## Requirements

- Python 3.11+ (developed on 3.13)
- PostgreSQL 14+ reachable by the backend
- A desktop OS able to run PySide6 (Windows / macOS / Linux)

## 1. Set up the environment

```bash
python3 -m venv .venv
source .venv/bin/activate            # Windows: .venv\Scripts\activate
pip install -r requirements.txt -r requirements-desktop.txt -r requirements-dev.txt
cp .env.example .env                 # then edit .env (never commit it)
```

Generate a JWT secret: `python -c "import secrets; print(secrets.token_hex(32))"`

## 2. Create the databases

```bash
sudo -u postgres psql -c "CREATE ROLE cosmetics_app LOGIN PASSWORD 'choose-a-strong-password' CREATEDB;"
sudo -u postgres createdb -O cosmetics_app cosmetics_dev
sudo -u postgres createdb -O cosmetics_app cosmetics_test
```

Put the matching URLs in `.env` (`DATABASE_URL`, `TEST_DATABASE_URL`). **The test suite drops and recreates the schema of
`TEST_DATABASE_URL`; never point it at a database you care about.**

## 3. Migrate and initialise

```bash
alembic upgrade head                 # create/upgrade the schema
python -m backend.cli init           # seed the default roles (idempotent)
python -m backend.cli create-ceo     # first administrator; the password is prompted, never stored in files
```

Optional, development/demo only (refused when `ENVIRONMENT=production`): `python -m backend.cli seed-demo`
creates clearly labelled synthetic locations and users and prints their random passwords once. Synthetic products and
opening stock (`python -m backend.cli seed-demo-catalogue`) are created only when `ENVIRONMENT=demo`.
`python scripts/setup_demo.py` (any OS) builds a separate `cosmetics_demo` database with synthetic locations, users, products, stock, customers and orders. `scripts/reset_demo.sh` rebuilds a separate `cosmetics_demo` database; it refuses any database not named `*_demo`.

## 4. Run

```bash
uvicorn backend.main:app --host 127.0.0.1 --port 8000     # backend; API docs at /docs (disabled in production)
python -m desktop.main                                     # desktop app (new terminal)
```

The desktop client reads `COSMETICS_API_URL` (default `http://127.0.0.1:8000`).

## 5. Test

```bash
python -m pytest                     # backend (real PostgreSQL, schema built by running the migrations) + desktop (offscreen Qt)
```

Render the real screens to PNG for visual review at 1366x768 and 1920x1080 (needs a running backend):

```bash
DEMO_USER=<username> DEMO_PASS=<password> python scripts/render_screens.py ./screens
```

## Project layout

```
backend/            FastAPI app: api/ (routers), services/ (business logic), models/, schemas/, migrations/, tests/
desktop/            PySide6 client: api/ (HTTP only), ui/ (tokens, widgets, shell), pages/, tests/
docs/               architecture, role matrix, implementation checklist
scripts/            doc generation, screen rendering, e2e_desktop.py (drives the real app), backup.py, reset_demo.sh
templates/          blank product import template
```

## Security notes

- The desktop app only ever talks to the backend API; the database is never exposed to clients. Use TLS (reverse proxy) between
  clients and the backend in any non-local deployment.
- Hiding a button is never the control: every endpoint checks permission and location scope server-side.
- Do not run production on the development settings. Set `ENVIRONMENT=production` (disables `/docs`, blocks demo seeding).
- Login rate limiting is in-process (single backend process). For multiple workers, add a limit at the reverse proxy.
