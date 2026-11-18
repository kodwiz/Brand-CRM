class BusinessRuleError(Exception):
    """A request is well-formed but violates a business rule (mapped to HTTP 422/409)."""


class NotFoundError(Exception):
    pass


class ConflictError(Exception):
    pass


class InsufficientStockError(BusinessRuleError):
    """Would make a stock bucket negative, or issue stock that is reserved. Negative stock is not permitted."""


class SelfApprovalError(Exception):
    """The same user tried to request and approve (or post and reverse) a controlled stock action."""
