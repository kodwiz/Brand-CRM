"""Shared router helpers."""
from fastapi import HTTPException

from backend.services.errors import BusinessRuleError, ConflictError, NotFoundError, SelfApprovalError


def commit_or_http(db, fn):
    """Run a service call, commit on success, and map domain errors to HTTP. Always rolls back on failure so
    a failed stock operation leaves nothing behind (including its audit entry)."""
    try:
        result = fn()
        db.commit()
        return result
    except NotFoundError as e:
        db.rollback()
        raise HTTPException(404, str(e))
    except SelfApprovalError as e:
        db.rollback()
        raise HTTPException(403, str(e))
    except ConflictError as e:
        db.rollback()
        raise HTTPException(409, str(e))
    except BusinessRuleError as e:
        db.rollback()
        raise HTTPException(422, str(e))
    except Exception:
        db.rollback()
        raise
