"""Generic delimited-text (CSV/TXT) and Excel reader. It does not know any brand: the administrator tells it which column holds the
device user id, the timestamp (or a date and a time), and optionally the in/out marker, and how the timestamp is written.

Example configuration (all keys, as stored on the device):
  {"columns": {"user_id": "User ID", "timestamp": "Date/Time", "direction": "Status"},
   "timestamp_format": "%d/%m/%Y %H:%M:%S",
   "timezone": "Asia/Karachi",
   "direction_map": {"in": ["IN", "C/In", "0"], "out": ["OUT", "C/Out", "1"]},
   "delimiter": ",", "encoding": "utf-8-sig"}
Use "date" and "time" instead of "timestamp" when the export keeps them in separate columns (timestamp_format then describes "date time"
joined by a space). A column may be given by header name or by its 1-based position.
"""
from __future__ import annotations

import csv
import io
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from backend.attendance.base import ParseResult, PunchAdapter, RawPunch, Reject
from backend.services.errors import BusinessRuleError

MAX_FUTURE = timedelta(days=1)


def _col(spec, header: list[str] | None, name: str) -> int:
    if isinstance(spec, int) or (isinstance(spec, str) and spec.strip().isdigit()):
        i = int(spec) - 1
        if i < 0:
            raise BusinessRuleError(f"Column '{name}': positions start at 1.")
        return i
    if header is None:
        raise BusinessRuleError(f"Column '{name}' is given by name but the file has no header row. Give its position (1, 2, ...) instead.")
    want = str(spec).strip().lower()
    for i, h in enumerate(header):
        if (h or "").strip().lower() == want:
            return i
    raise BusinessRuleError(f"The file has no column called '{spec}' (columns found: {', '.join(h for h in header if h) or 'none'}).")


def _join(d, t):
    """Combine separate date and time cells (Excel typed cells or text)."""
    if d is None or t is None or str(d).strip() == "" or str(t).strip() == "":
        return None
    if isinstance(t, datetime):
        t = t.time()
    if isinstance(d, datetime):
        d = d.date()
    if hasattr(d, "year") and hasattr(t, "hour"):
        return datetime.combine(d, t)
    return f"{d} {t}".strip()


class GenericFileAdapter(PunchAdapter):
    code = "generic_file"
    label = "Generic CSV / Excel export"
    description = "Reads a delimited text file or an Excel sheet exported from any attendance software, using the column names you configure."
    config_help = {
        "columns": 'which columns to read: {"user_id": "...", "timestamp": "..."} or {"user_id", "date", "time"}; optional "direction"',
        "timestamp_format": 'how the time is written, e.g. "%Y-%m-%d %H:%M:%S" (not needed for Excel date cells)',
        "timezone": "the time zone the device clock is set to, e.g. Asia/Karachi",
        "direction_map": 'optional: {"in": ["IN", "0"], "out": ["OUT", "1"]}; anything else is stored as unknown',
        "delimiter": 'optional, default ","', "encoding": 'optional, default "utf-8-sig"', "has_header": "optional, default true",
    }

    @classmethod
    def validate_config(cls, config: dict) -> dict:
        cfg = dict(config or {})
        cols = cfg.get("columns")
        if not isinstance(cols, dict) or not cols.get("user_id"):
            raise BusinessRuleError("Say which column holds the device user id (columns.user_id).")
        if not (cols.get("timestamp") or (cols.get("date") and cols.get("time"))):
            raise BusinessRuleError("Say which column holds the time (columns.timestamp, or both columns.date and columns.time).")
        extra = set(cols) - {"user_id", "timestamp", "date", "time", "direction"}
        if extra:
            raise BusinessRuleError(f"Unknown column setting(s): {', '.join(sorted(extra))}.")
        tz = (cfg.get("timezone") or "").strip()
        if not tz:
            raise BusinessRuleError("Say which time zone the device clock uses (for example Asia/Karachi). It is not assumed.")
        try:
            ZoneInfo(tz)
        except (ZoneInfoNotFoundError, ValueError):
            raise BusinessRuleError(f"'{tz}' is not a known time zone.")
        fmt = cfg.get("timestamp_format")
        if fmt:
            try:
                datetime.strptime(datetime(2026, 1, 2, 3, 4, 5).strftime(fmt), fmt)
            except ValueError:
                raise BusinessRuleError(f"'{fmt}' is not a usable timestamp format.")
        dm = cfg.get("direction_map") or {}
        if dm and (not isinstance(dm, dict) or set(dm) - {"in", "out"}):
            raise BusinessRuleError('direction_map must look like {"in": [...], "out": [...]}.')
        cfg["direction_map"] = {k: [str(v).strip().lower() for v in dm.get(k, [])] for k in ("in", "out")}
        if cols.get("direction") and not (cfg["direction_map"]["in"] and cfg["direction_map"]["out"]):
            raise BusinessRuleError("A direction column needs direction_map with the values that mean in and out.")
        cfg.setdefault("delimiter", ",")
        cfg.setdefault("encoding", "utf-8-sig")
        cfg.setdefault("has_header", True)
        if len(str(cfg["delimiter"])) != 1 and cfg["delimiter"] != "\\t":
            raise BusinessRuleError("The delimiter must be a single character (or \\t for tab).")
        return cfg

    # -- reading ---------------------------------------------------------------------------------------------------------------------
    @staticmethod
    def _rows(content: bytes, filename: str | None, cfg: dict) -> list[list]:
        if (filename or "").lower().endswith((".xlsx", ".xlsm")):
            from openpyxl import load_workbook
            wb = load_workbook(io.BytesIO(content), read_only=True, data_only=True)
            ws = wb.worksheets[0]
            return [list(r) for r in ws.iter_rows(values_only=True)]
        try:
            text = content.decode(cfg["encoding"])
        except (UnicodeDecodeError, LookupError):
            raise BusinessRuleError(f"The file could not be read as {cfg['encoding']} text. Check the encoding in the device settings.")
        delim = "\t" if cfg["delimiter"] == "\\t" else cfg["delimiter"]
        return [row for row in csv.reader(io.StringIO(text), delimiter=delim)]

    def parse(self, content: bytes, filename: str | None, config: dict) -> ParseResult:
        cfg = self.validate_config(config)
        rows = self._rows(content, filename, cfg)
        res = ParseResult()
        header = None
        start = 0
        if cfg["has_header"]:
            while start < len(rows) and not any(str(c or "").strip() for c in rows[start]):
                start += 1
            if start >= len(rows):
                raise BusinessRuleError("The file is empty.")
            header = [str(c or "").strip() for c in rows[start]]
            start += 1
        cols = cfg["columns"]
        iu = _col(cols["user_id"], header, "user_id")
        its = _col(cols["timestamp"], header, "timestamp") if cols.get("timestamp") else None
        idate = _col(cols["date"], header, "date") if cols.get("date") else None
        itime = _col(cols["time"], header, "time") if cols.get("time") else None
        idir = _col(cols["direction"], header, "direction") if cols.get("direction") else None
        tz = ZoneInfo(cfg["timezone"])
        fmt = cfg.get("timestamp_format")
        now = datetime.now(timezone.utc)
        for n, row in enumerate(rows[start:], start=start + 1):
            if not any(str(c or "").strip() for c in row):
                continue
            res.rows_total += 1
            raw = ", ".join("" if c is None else str(c) for c in row)[:400]
            try:
                uid = str(row[iu]).strip() if iu < len(row) and row[iu] is not None else ""
                if uid.endswith(".0") and uid[:-2].isdigit():
                    uid = uid[:-2]                      # Excel stores 17 as 17.0
                if not uid:
                    res.rejects.append(Reject(n, "no user id", raw))
                    continue
                if its is not None:
                    v = row[its] if its < len(row) else None
                else:
                    v = _join(row[idate] if idate < len(row) else None, row[itime] if itime < len(row) else None)
                if isinstance(v, datetime):
                    naive = v
                elif v is None or str(v).strip() == "":
                    res.rejects.append(Reject(n, "no time", raw))
                    continue
                else:
                    if not fmt:
                        res.rejects.append(Reject(n, "the time is text but no timestamp_format is configured", raw))
                        continue
                    naive = datetime.strptime(str(v).strip(), fmt)
                at = naive.replace(tzinfo=tz) if naive.tzinfo is None else naive
                at = at.astimezone(timezone.utc)
                if at - now > MAX_FUTURE:
                    res.rejects.append(Reject(n, "the time is in the future (check the device clock and time zone)", raw))
                    continue
                direction = "unknown"
                if idir is not None and idir < len(row) and row[idir] is not None:
                    dv = str(row[idir]).strip().lower()
                    direction = "in" if dv in cfg["direction_map"]["in"] else "out" if dv in cfg["direction_map"]["out"] else "unknown"
                res.punches.append(RawPunch(device_user_id=uid, punched_at=at, direction=direction, raw_ref=f"line {n}: {raw}"))
            except ValueError:
                res.rejects.append(Reject(n, f"the time '{v}' does not match the format {fmt}", raw))
            except (IndexError, TypeError):
                res.rejects.append(Reject(n, "the row is shorter than expected", raw))
        return res
