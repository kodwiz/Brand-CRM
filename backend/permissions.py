"""Permission catalogue and the default role/permission matrix.

Permissions are code-defined strings (so typos fail fast); which role holds which
permission is data (role_permissions table) and can be reconfigured by the CEO.
`seed_roles` is idempotent: it creates missing roles/permissions but never strips
permissions the company has since customised on an existing role.
"""
from __future__ import annotations

PERMISSIONS: dict[str, str] = {
    # dashboards
    "dashboard.executive": "Company-wide executive overview",
    "dashboard.stock": "Stock keeper dashboard",
    "dashboard.sales": "Order booker dashboard",
    "dashboard.finance": "Accounts dashboard",
    # administration
    "users.view": "View user accounts",
    "users.manage": "Create/edit users and assign locations",
    "roles.manage": "Edit role permissions",
    "locations.view": "View locations",
    "locations.manage": "Create/edit locations",
    "audit.view": "View audit history",
    "settings.manage": "Change system settings",
    # catalogue and stock
    "products.view": "View products and variants",
    "products.manage": "Create/edit products and variants",
    "products.view_cost": "See product cost prices",
    "products.view_prices": "See employee and distributor prices",
    "products.manage_prices": "Change product prices",
    "products.import": "Bulk-import products from Excel/CSV",
    "inventory.view": "View stock balances and movements",
    "inventory.receive": "Record incoming goods",
    "inventory.adjust": "Record stock adjustments, damage and expiry",
    "inventory.count": "Run physical stock counts",
    "inventory.writeoff_approve": "Approve stock write-offs",
    "inventory.adjust_approve": "Approve stock adjustments and count variances (never your own)",
    "inventory.view_valuation": "See inventory costs and valuation",
    "inventory.reverse": "Reverse a posted stock transaction (never your own)",
    "inventory.opening_stock": "Enter verified opening stock",
    "transfers.view": "View stock transfers",
    "transfers.request": "Request stock transfers",
    "transfers.approve": "Approve stock transfers",
    "transfers.dispatch": "Dispatch stock transfers",
    "transfers.receive": "Receive stock transfers",
    "transfers.approve_discrepancy": "Approve transfer shortages (never your own)",
    # purchasing
    "suppliers.view": "View suppliers and liabilities",
    "suppliers.manage": "Create/edit suppliers",
    "purchasing.view": "View purchase orders, supplier invoices and payments (costs need products.view_cost)",
    "purchasing.manage": "Create purchase orders, supplier invoices and returns",
    "purchasing.approve": "Approve purchase orders and supplier adjustments (never your own)",
    "purchasing.approve_variance": "Approve supplier invoices held for price/quantity variances (never your own)",
    "purchasing.pay": "Record supplier payments",
    "purchasing.reverse": "Void supplier invoices and reverse supplier payments (never your own)",
    # sales
    "customers.view": "View customers",
    "customers.manage": "Create/edit customers",
    "customers.view_all": "View every customer (not only those you created or sold to)",
    "orders.view_own": "View own orders",
    "orders.view_all": "View all orders in scope",
    "orders.create": "Create orders",
    "orders.update_status": "Update order and delivery status",
    "orders.returns": "Approve customer returns",
    "orders.fulfil": "Pack, dispatch and receive back customer parcels at assigned locations",
    "orders.payments": "Record customer receipts on orders",
    "orders.refund": "Settle refunds and store credit; reverse customer payments",
    "returns.receive": "Physically receive and inspect customer returns",
    "pricing.manage": "Manage price lists and promotions",
    "discounts.approve": "Approve exceptional discounts",
    # distributors
    "distributors.view": "View distributor accounts and ledgers",
    "distributors.manage": "Create/edit distributor records and terms",
    "distributors.transact": "Record dispatches, returns and exchanges",
    "distributors.receive_payment": "Record distributor payments",
    "distributors.approve": "Approve distributor write-offs and adjustments",
    # accounting
    "accounting.view": "View ledgers and financial statements",
    "accounting.post": "Post journal entries and allocations",
    "accounting.manage_coa": "Manage chart of accounts",
    "accounting.close_period": "Close accounting periods",
    "accounting.reopen_period": "Reopen closed accounting periods",
    "accounting.approve": "Approve manual journals, expenses, reversals, opening balances and go-live",
    "accounting.reconcile": "Reconcile cash and bank accounts",
    "accounting.opening_balances": "Enter opening-balance batches",
    "expenses.manage": "Record and manage expenses",
    # people
    "employees.view": "View employee records and performance",
    "employees.manage": "Create and edit employee records",
    "employees.purchase": "Record employee purchases at assigned locations (sees only a name list of active staff)",
    "employees.purchase_view": "View employee purchases and balances",
    "employees.purchase_pay": "Record and reverse payments on employee purchases",
    "employees.purchase_approve": "Approve employee credit, reject or void employee purchases (never your own)",
    "employees.manage_discounts": "Set staff discount rules and employee-credit settings",
    "attendance.view": "View attendance",
    "attendance.manage": "Manage devices and policy, import punches and request corrections",
    "attendance.approve": "Approve or reject attendance corrections (never your own)",
    # reporting / approvals
    "reports.operational": "Operational reports",
    "reports.summaries": "Sales, gross-profit and expense summaries",
    "reports.financial": "Financial reports",
    "reports.export": "Export reports to PDF/Excel",
    "approvals.view": "View pending approvals",
    "approvals.decide": "Approve or reject requests",
}

# Roles that hold these can never be left without them (lock-out protection).
CRITICAL_ADMIN_PERMISSIONS = frozenset({"users.manage", "roles.manage"})

_ALL = set(PERMISSIONS)

DEFAULT_ROLES: dict[str, dict] = {
    "ceo": {
        "name": "CEO",
        "description": "Company-wide access to every module and setting.",
        "scope_all_locations": True,
        "permissions": set(_ALL),
    },
    "manager": {
        "name": "Manager",
        "description": "Company-wide operational oversight. No ledger posting or user administration.",
        "scope_all_locations": True,
        "permissions": {
            "dashboard.executive", "locations.view", "audit.view", "users.view",
            "products.view", "products.view_cost", "products.view_prices", "products.manage", "products.import",
            "inventory.view", "inventory.view_valuation", "inventory.writeoff_approve", "inventory.adjust_approve",
            "inventory.reverse", "transfers.view", "transfers.approve", "transfers.approve_discrepancy",
            "reports.summaries",
            "suppliers.view", "purchasing.view", "purchasing.approve", "purchasing.approve_variance",
            "customers.view", "customers.view_all", "customers.manage", "orders.create", "pricing.manage",
            "orders.view_all", "orders.update_status", "orders.returns", "orders.fulfil",
            "orders.payments", "orders.refund", "returns.receive", "discounts.approve", "distributors.view", "distributors.manage", "distributors.approve",
            "employees.view", "employees.manage", "employees.purchase_view", "employees.purchase_approve", "employees.manage_discounts",
            "attendance.view", "attendance.manage", "attendance.approve",
            "reports.operational", "reports.export", "approvals.view", "approvals.decide",
        },
    },
    "stock_keeper": {
        "name": "Stock keeper",
        "description": "Inventory, transfers and distributor accounts for assigned locations.",
        "scope_all_locations": False,
        "permissions": {
            "dashboard.stock", "locations.view", "products.view", "products.view_prices",
            "inventory.view", "inventory.receive", "inventory.adjust", "inventory.count", "purchasing.view",
            "transfers.view", "transfers.request", "transfers.dispatch", "transfers.receive",
            "distributors.view", "distributors.transact", "distributors.receive_payment",
            "orders.fulfil", "returns.receive", "employees.purchase", "reports.operational",
        },
    },
    "order_booker": {
        "name": "Order booker",
        "description": "Customers and orders. Sees only their own orders.",
        "scope_all_locations": False,
        "permissions": {
            "dashboard.sales", "locations.view", "products.view", "inventory.view",
            "customers.view", "customers.manage",
            "orders.view_own", "orders.create", "orders.update_status",
        },
    },
    "accounts": {
        "name": "Accounts",
        "description": "Optional finance role: ledgers, payables, receivables, expenses, period close.",
        "scope_all_locations": True,
        "permissions": {
            "dashboard.finance", "locations.view",
            "accounting.view", "accounting.post", "accounting.manage_coa", "accounting.close_period",
            "accounting.reconcile", "accounting.opening_balances", "expenses.manage", "suppliers.view", "suppliers.manage", "purchasing.view", "purchasing.manage", "purchasing.pay",
            "purchasing.reverse", "customers.view", "customers.view_all", "orders.view_all", "orders.payments", "orders.refund",
            "distributors.view", "distributors.receive_payment", "employees.purchase_view", "employees.purchase_pay",
            "reports.financial", "reports.export", "reports.summaries", "products.view", "products.view_cost",
            "products.view_prices", "inventory.view", "inventory.view_valuation",
        },
    },
}


def validate_permissions(codes: set[str] | list[str]) -> list[str]:
    """Return the codes that are not in the catalogue."""
    return sorted(c for c in codes if c not in PERMISSIONS)
