"""Attendance-device adapter contract.

Devices do not all work the same way: some export files, some expose a vendor SDK or network protocol, and brands differ in column
layout, time format and how they mark check-in versus check-out. Each way of reading a device is one adapter class. The rest of the
system only sees the normalised RawPunch records an adapter returns; it never knows about the device's format.

To support a particular device: find out its brand, model, protocol and supported export (see docs/attendance.md), add a class here
that implements `parse`, and register it in `registry.py`. Until then the generic file adapter reads any delimited or Excel export
whose columns you can name.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime


@dataclass
class RawPunch:
    device_user_id: str
    punched_at: datetime            # timezone-aware
    direction: str                  # 'in' | 'out' | 'unknown'
    raw_ref: str                    # the original line, kept as evidence


@dataclass
class Reject:
    line: int
    reason: str
    raw: str = ""


@dataclass
class ParseResult:
    rows_total: int = 0
    punches: list[RawPunch] = field(default_factory=list)
    rejects: list[Reject] = field(default_factory=list)


class PunchAdapter:
    code: str = ""
    label: str = ""
    description: str = ""
    # Describes the configuration an administrator fills in for a device that uses this adapter (shown in the desktop app).
    config_help: dict[str, str] = {}

    @classmethod
    def validate_config(cls, config: dict) -> dict:
        """Return the normalised configuration or raise BusinessRuleError with a message an administrator can act on."""
        raise NotImplementedError

    def parse(self, content: bytes, filename: str | None, config: dict) -> ParseResult:
        raise NotImplementedError
