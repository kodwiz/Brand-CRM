"""The adapters this installation can use. Add a new device integration by writing a PunchAdapter and listing it here."""
from __future__ import annotations

from backend.attendance.base import PunchAdapter
from backend.attendance.generic_file import GenericFileAdapter
from backend.services.errors import BusinessRuleError

ADAPTERS: dict[str, type[PunchAdapter]] = {GenericFileAdapter.code: GenericFileAdapter}


def get_adapter(code: str) -> PunchAdapter:
    cls = ADAPTERS.get(code)
    if cls is None:
        raise BusinessRuleError(f"There is no adapter '{code}'. Available: {', '.join(sorted(ADAPTERS))}.")
    return cls()


def describe() -> list[dict]:
    return [{"code": c.code, "label": c.label, "description": c.description, "config_help": c.config_help} for c in ADAPTERS.values()]
