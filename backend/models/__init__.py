"""Import every model so Alembic autogenerate and metadata.create_all see them."""
from backend.models.audit import AuditLog
from backend.models.auth import AuthSession, Role, RolePermission, User, UserLocation
from backend.models.catalog import Batch, Category, ImportJob, Product, ProductVariant, SystemSetting
from backend.models.inventory import (
    StockBalance, StockChangeLine, StockChangeRequest, StockCost, StockCount, StockCountLine, StockMovement,
    StockReceipt, StockReceiptLine, StockReservation, StockTransaction, Transfer, TransferEvent, TransferLine,
)
from backend.models.location import Location
from backend.models.purchasing import (
    PaymentAllocation, PurchaseOrder, PurchaseOrderLine, ReceiptCost, Supplier, SupplierAdjustment, SupplierInvoice,
    SupplierInvoiceCharge, SupplierInvoiceLine, SupplierLedgerEntry, SupplierPayment, SupplierReturn, SupplierReturnLine,
)
from backend.models.sales import (
    Customer, CustomerAddress, CustomerCreditEntry, Order, OrderEvent, OrderLine, OrderPayment, Promotion, SalesChannel,
    SalesReturn, SalesReturnLine, Shipment, ShipmentLine,
)

__all__ = [
    "Customer", "CustomerAddress", "CustomerCreditEntry", "Order", "OrderEvent", "OrderLine", "OrderPayment", "Promotion",
    "SalesChannel", "SalesReturn", "SalesReturnLine", "Shipment", "ShipmentLine",
    "AuditLog", "AuthSession", "Batch", "Category", "ImportJob", "Location", "Product", "ProductVariant", "Role",
    "RolePermission", "StockBalance", "StockChangeLine", "StockChangeRequest", "StockCost", "StockCount",
    "StockCountLine", "StockMovement", "StockReceipt", "StockReceiptLine", "StockReservation", "StockTransaction",
    "SystemSetting", "PaymentAllocation", "PurchaseOrder", "PurchaseOrderLine", "ReceiptCost", "Supplier", "SupplierAdjustment",
    "SupplierInvoice", "SupplierInvoiceCharge", "SupplierInvoiceLine", "SupplierLedgerEntry", "SupplierPayment",
    "SupplierReturn", "SupplierReturnLine", "Transfer", "TransferEvent", "TransferLine", "User", "UserLocation",
]
from backend.models.distributors import (  # noqa: E402,F401
    Distributor, DistributorAdjustment, DistributorAllocation, DistributorInvoice, DistributorInvoiceLine, DistributorLedgerEntry,
    DistributorPayment, DistributorPrice, DistributorReturn, DistributorReturnLine,
)
from backend.models.accounting import (  # noqa: E402,F401
    Account, AccountingPeriod, CashAccount, CashReconciliation, Expense, JournalEntry, JournalLine, OpeningBatch, OpeningLine, PeriodRequest,
)
from backend.models.people import (  # noqa: E402,F401
    AttendanceCorrection, AttendanceDay, AttendanceDevice, AttendanceImport, AttendancePolicy, AttendancePunch, Employee, EmployeeDiscountRule,
    EmployeePurchase, EmployeePurchaseLine, EmployeePurchasePayment,
)
