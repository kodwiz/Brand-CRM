"""Attendance: imported biometric punches, daily and period summaries, exceptions, approved corrections, and the device and policy settings.
Raw punches are never edited or deleted. Everything here is visible only to users holding attendance permissions, and the backend decides
what each person may see or do."""
from __future__ import annotations

import json
from datetime import date, timedelta

from PySide6.QtCore import QDate, QTime
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QFileDialog, QHBoxLayout, QLineEdit, QPlainTextEdit, QSpinBox, QTabWidget, QTimeEdit,
)

from desktop.pages.accounting import NoteDialog
from desktop.pages.base import Page
from desktop.pages.inventory import loc_codes, loc_combo
from desktop.pages.suppliers import _Tab, _cap, _date_edit, _iso, _set_combo
from desktop.session import Session
from desktop.ui.forms import FormDialog, FormGrid
from desktop.ui.widgets import Column, DataTable, PageHeader, StatePanel, button, label
from desktop.ui.worker import run_async

DAY_STATUS = {"present": "ok", "late": "warn", "short": "warn", "incomplete": "error", "no_punch": "error", "weekly_off": "neutral"}
DAY_TEXT = {"present": "Present", "late": "Late", "short": "Short day", "incomplete": "Incomplete", "no_punch": "No punches", "weekly_off": "Weekly off"}
WEEKDAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
STATUS_TEXT = {"pending": "Waiting for approval", "approved": "Approved", "rejected": "Rejected"}
SAMPLE_CONFIG = {"columns": {"user_id": "UserID", "timestamp": "Time", "direction": "Status"}, "timestamp_format": "%Y-%m-%d %H:%M:%S",
                 "timezone": "Asia/Karachi", "direction_map": {"in": ["IN"], "out": ["OUT"]}}


def _mins(v) -> str:
    v = int(v or 0)
    return f"{v // 60}h {v % 60:02d}m" if v else "-"


def _when(v) -> str:
    return str(v or "")[:16].replace("T", " ")


def _range_bar(bar: QHBoxLayout, days_back: int = 6) -> tuple:
    a = _date_edit(QDate.currentDate().addDays(-days_back)); b = _date_edit()
    bar.addWidget(label("From", "fieldLabel")); bar.addWidget(a); bar.addWidget(label("to", "fieldLabel")); bar.addWidget(b)
    return a, b


# ------------------------------------------------------------------------------------------------ dialogs
class CorrectionDialog(FormDialog):
    def __init__(self, session: Session, prefill: dict | None = None, parent=None):
        super().__init__("Request an attendance correction", "Send for approval", parent, 620)
        self.session = session
        g = FormGrid(2)
        self.emp = QComboBox(); self.emp.addItem("Select an employee…", None)
        self.date = _date_edit()
        self.kind = QComboBox(); self.kind.addItem("Add a missing punch", "add_punch"); self.kind.addItem("Ignore a wrong punch", "ignore_punch")
        self.direction = QComboBox(); self.direction.addItem("Check-in", "in"); self.direction.addItem("Check-out", "out")
        self.time = QTimeEdit(); self.time.setDisplayFormat("HH:mm"); self.time.setTime(QTime(9, 0))
        self.punch = QComboBox(); self.punch.addItem("Choose the punch…", None)
        self.reason = QLineEdit(); self.reason.setPlaceholderText("Why is this needed? (required)")
        g.add("emp", "Employee", self.emp, 2); g.add("date", "Day", self.date); g.add("kind", "Correction", self.kind)
        g.add("direction", "Punch type", self.direction); g.add("time", "Time (local)", self.time); g.add("punch", "Punch to ignore", self.punch, 2)
        g.add("reason", "Reason", self.reason, 2)
        self.form = g
        self.body.addWidget(g)
        self.body.addWidget(label("The original punches are never changed. A correction only takes effect after someone else approves it, and both are kept in the audit log.",
                                  "muted", wrap=True))
        self.kind.currentIndexChanged.connect(self._kind)
        self.emp.currentIndexChanged.connect(self._load_punches)
        self.date.dateChanged.connect(lambda _: self._load_punches())
        self._kind()
        c = session.client

        def filled(rows):
            for e in rows:
                self.emp.addItem(f"{e['full_name']}  ({e['code']})", e["id"])
            self._apply(prefill)
        run_async(lambda: c.get("/employees/lookup"), filled, lambda e: None)

    def _apply(self, prefill) -> None:
        if prefill:
            _set_combo(self.emp, prefill.get("employee_id"))
            self.date.setDate(QDate.fromString(prefill["work_date"], "yyyy-MM-dd"))

    def _kind(self) -> None:
        add = self.kind.currentData() == "add_punch"
        self.direction.setEnabled(add); self.time.setEnabled(add); self.punch.setEnabled(not add)

    def _load_punches(self) -> None:
        self.punch.clear(); self.punch.addItem("Choose the punch…", None)
        if self.emp.currentData() is None:
            return
        c, eid, d = self.session.client, self.emp.currentData(), _iso(self.date)
        run_async(lambda: c.get("/attendance/punches", employee_id=eid, work_date=d),
                  lambda rows: [self.punch.addItem(f"{r['local_time']}  {_cap(r['direction'])}  ({r['status']})", r["id"]) for r in rows if r["status"] != "ignored"],
                  lambda e: None)

    def submit(self) -> None:
        self.form.clear_errors()
        if self.emp.currentData() is None:
            self.form.set_error("emp", "Choose the employee."); return self.reenable()
        add = self.kind.currentData() == "add_punch"
        if not add and self.punch.currentData() is None:
            self.form.set_error("punch", "Choose the punch to ignore."); return self.reenable()
        if len(self.reason.text().strip()) < 5:
            self.form.set_error("reason", "Say why, in a few words."); return self.reenable()
        body = {"employee_id": self.emp.currentData(), "work_date": _iso(self.date), "kind": self.kind.currentData(), "reason": self.reason.text().strip(),
                "direction": self.direction.currentData() if add else None, "punch_time": self.time.time().toString("HH:mm:ss") if add else None,
                "punch_id": None if add else self.punch.currentData()}
        c = self.session.client
        run_async(lambda: c.post("/attendance/corrections", json=body), lambda _: self.finish_ok(), lambda e: self.finish_error(e.message))


class DeviceDialog(FormDialog):
    def __init__(self, session: Session, row: dict | None = None, adapters: list | None = None, parent=None):
        super().__init__("Edit device" if row else "New attendance device", "Save device", parent, 760)
        self.session, self.row = session, row
        g = FormGrid(2)
        self.name = QLineEdit(); self.loc = loc_combo(session)
        self.adapter = QComboBox()
        for a in adapters or []:
            self.adapter.addItem(a["label"], a["code"])
        self.brand = QLineEdit(); self.model = QLineEdit(); self.protocol = QLineEdit(); self.fmt = QLineEdit()
        self.active = QCheckBox("In use")
        self.active.setChecked(True)
        g.add("name", "Device name", self.name); g.add("loc", "Location", self.loc); g.add("adapter", "Reads data using", self.adapter, 2)
        g.add("brand", "Brand", self.brand); g.add("model", "Model", self.model); g.add("protocol", "Connection / protocol", self.protocol); g.add("fmt", "Export format", self.fmt)
        self.config = QPlainTextEdit(); self.config.setMinimumHeight(170)
        self.config.setPlainText(json.dumps(row["config"] if row else SAMPLE_CONFIG, indent=2))
        g.add("config", "Settings (JSON)", self.config, 2)
        if row:
            g.add("active", "", self.active, 2)
        self.form = g
        self.body.addWidget(g)
        self.body.addWidget(label("Describe the real device here. Do not put passwords, keys or tokens in these settings; they are refused. The time zone must be the one the "
                                  "device clock uses; it is never assumed. The sample columns above are placeholders: use the column names in your device's export.", "muted", wrap=True))
        if row:
            self.name.setText(row["name"]); _set_combo(self.loc, row["location_id"]); _set_combo(self.adapter, row["adapter"])
            self.brand.setText(row.get("brand") or ""); self.model.setText(row.get("model") or ""); self.protocol.setText(row.get("protocol") or "")
            self.fmt.setText(row.get("export_format") or ""); self.active.setChecked(row["is_active"])

    def submit(self) -> None:
        self.form.clear_errors()
        if not self.name.text().strip():
            self.form.set_error("name", "Name the device."); return self.reenable()
        if self.loc.currentData() is None:
            self.form.set_error("loc", "Choose its location."); return self.reenable()
        try:
            cfg = json.loads(self.config.toPlainText() or "{}")
        except ValueError as e:
            self.form.set_error("config", f"This is not valid JSON ({e})."); return self.reenable()
        body = {"name": self.name.text().strip(), "location_id": self.loc.currentData(), "adapter": self.adapter.currentData(), "brand": self.brand.text().strip() or None,
                "model": self.model.text().strip() or None, "protocol": self.protocol.text().strip() or None, "export_format": self.fmt.text().strip() or None, "config": cfg}
        c = self.session.client
        if self.row:
            body["is_active"] = self.active.isChecked(); body["reason"] = "Changed from the desktop app"
            body.pop("location_id")
            call = lambda: c.patch(f"/attendance/devices/{self.row['id']}", body)  # noqa: E731
        else:
            call = lambda: c.post("/attendance/devices", json=body)  # noqa: E731
        run_async(call, lambda _: self.finish_ok(), lambda e: self.finish_error(e.message))


# ------------------------------------------------------------------------------------------------ tabs
class DaysTab(_Tab):
    def __init__(self, session: Session):
        super().__init__(session)
        bar = QHBoxLayout()
        self.a, self.b = _range_bar(bar)
        self.state = QComboBox(); self.state.addItem("All days", None)
        for k, v in DAY_TEXT.items():
            self.state.addItem(v, k)
        self.state.currentIndexChanged.connect(lambda _: self.refresh())
        self.a.dateChanged.connect(lambda _: self.refresh()); self.b.dateChanged.connect(lambda _: self.refresh())
        bar.addWidget(self.state); bar.addStretch()
        if session.can("attendance.manage"):
            bar.addWidget(button("Recalculate these days", on_click=self._rebuild))
        self.lay.addLayout(bar)
        codes = loc_codes(session)
        self.table = DataTable([Column("work_date", "Date", 100), Column("name", "Employee", 170), Column("location_id", "Loc.", 100, fmt=lambda v: codes.get(v, "")),
                                Column("first_in_local", "In", 70), Column("last_out_local", "Out", 70), Column("punch_count", "Punches", 80, "right"),
                                Column("worked_minutes", "Worked", 90, "right", fmt=_mins), Column("late_minutes", "Late", 80, "right", fmt=_mins),
                                Column("overtime_minutes", "Overtime", 90, "right", fmt=_mins),
                                Column("status", "Day", 120, status=lambda v: DAY_STATUS.get(v, "neutral"), fmt=lambda v: DAY_TEXT.get(v, v)),
                                Column("exception_text", "Needs attention", 230)])
        self.panel = StatePanel(self.table, "No attendance in this period. Import punches, and make sure a policy exists for the location (Devices and policy tab).")
        self.panel.retry.connect(self.refresh)
        self.lay.addWidget(self.panel, 1)

    def refresh(self) -> None:
        if self.b.date() < self.a.date():
            return self.panel.show_error("The end date is before the start date.", False)
        self.panel.show_loading()
        c = self.session.client

        def done(rows):
            self.table.set_rows(rows); self.panel.show_content() if rows else self.panel.show_empty()
        run_async(lambda: c.get("/attendance/days", date_from=_iso(self.a), date_to=_iso(self.b), status=self.state.currentData(), location_id=self.session.current_location_id),
                  done, lambda e: self.panel.show_error(e.message, e.offline))

    def _rebuild(self) -> None:
        c = self.session.client
        run_async(lambda: c.post("/attendance/rebuild", json={"date_from": _iso(self.a), "date_to": _iso(self.b), "location_id": self.session.current_location_id}),
                  lambda _: (self.status.show_message("Days recalculated from the stored punches.", "ok"), self.refresh()), lambda e: self.status.show_message(e.message, "error"))


class PeriodTab(_Tab):
    def __init__(self, session: Session):
        super().__init__(session)
        bar = QHBoxLayout()
        self.a, self.b = _range_bar(bar, 29)
        self.a.dateChanged.connect(lambda _: self.refresh()); self.b.dateChanged.connect(lambda _: self.refresh())
        bar.addStretch()
        self.lay.addLayout(bar)
        self.table = DataTable([Column("code", "Code", 80), Column("name", "Employee", 170), Column("days_present", "Present", 80, "right"), Column("days_late", "Late", 70, "right"),
                                Column("days_short", "Short", 70, "right"), Column("days_incomplete", "Incomplete", 90, "right"), Column("days_no_punch", "No punches", 90, "right"),
                                Column("worked_minutes", "Worked", 100, "right", fmt=_mins), Column("late_minutes", "Late time", 100, "right", fmt=_mins),
                                Column("overtime_minutes", "Overtime", 100, "right", fmt=_mins)])
        self.panel = StatePanel(self.table, "No attendance in this period.")
        self.panel.retry.connect(self.refresh)
        self.lay.addWidget(self.panel, 1)
        self.lay.addWidget(label("Weekly-off days are not counted as absent. Leave, public holidays and night shifts are not modelled in this version, so a day off for those "
                                 "reasons shows as 'No punches'.", "muted", wrap=True))

    def refresh(self) -> None:
        if self.b.date() < self.a.date():
            return self.panel.show_error("The end date is before the start date.", False)
        self.panel.show_loading()
        c = self.session.client

        def done(rows):
            self.table.set_rows(rows); self.panel.show_content() if rows else self.panel.show_empty()
        run_async(lambda: c.get("/attendance/summary", date_from=_iso(self.a), date_to=_iso(self.b), location_id=self.session.current_location_id),
                  done, lambda e: self.panel.show_error(e.message, e.offline))


class ExceptionsTab(_Tab):
    def __init__(self, session: Session):
        super().__init__(session)
        bar = QHBoxLayout()
        self.a, self.b = _range_bar(bar, 13)
        self.a.dateChanged.connect(lambda _: self.refresh()); self.b.dateChanged.connect(lambda _: self.refresh())
        bar.addStretch()
        if session.can("attendance.manage"):
            bar.addWidget(button("Request correction for selected day", "primary", self._correct))
        self.lay.addLayout(bar)
        codes = loc_codes(session)
        self.table = DataTable([Column("work_date", "Date", 100), Column("code", "Code", 100), Column("name", "Employee", 170), Column("location_id", "Loc.", 100, fmt=lambda v: codes.get(v, "")),
                                Column("first_in_local", "In", 70), Column("last_out_local", "Out", 70), Column("punch_count", "Punches", 80, "right"),
                                Column("exception_text", "Problem", 260), Column("corrected", "Corrected", 90, fmt=lambda v: "Yes" if v else "")])
        self.panel = StatePanel(self.table, "No missing or odd punches in this period.")
        self.panel.retry.connect(self.refresh)
        self.lay.addWidget(self.panel, 1)
        self.lay.addWidget(label("Device numbers with no matching employee", "sectionTitle"))
        self.unmatched = DataTable([Column("device_user_id", "Device number", 140), Column("punches", "Punches", 90, "right"), Column("first", "First seen", 160, fmt=_when),
                                    Column("last", "Last seen", 160, fmt=_when)])
        self.unmatched.setMaximumHeight(160)
        self.lay.addWidget(self.unmatched)
        self.lay.addWidget(label("These punches are kept. Enter the device number on the right employee (Employees tab) and they are matched automatically.", "muted", wrap=True))

    def refresh(self) -> None:
        if self.b.date() < self.a.date():
            return self.panel.show_error("The end date is before the start date.", False)
        self.panel.show_loading()
        c = self.session.client

        def done(d):
            self.table.set_rows(d["days"]); self.unmatched.set_rows(d["unmatched_device_ids"])
            self.panel.show_content() if d["days"] else self.panel.show_empty()
        run_async(lambda: c.get("/attendance/exceptions", date_from=_iso(self.a), date_to=_iso(self.b), location_id=self.session.current_location_id),
                  done, lambda e: self.panel.show_error(e.message, e.offline))

    def _correct(self) -> None:
        r = self.table.selected_row()
        if CorrectionDialog(self.session, r, self).exec():
            self.status.show_message("Correction sent for approval.", "ok")


class CorrectionsTab(_Tab):
    def __init__(self, session: Session):
        super().__init__(session)
        bar = QHBoxLayout()
        self.state = QComboBox(); self.state.addItem("All", None)
        for k, v in STATUS_TEXT.items():
            self.state.addItem(v, k)
        self.state.setCurrentIndex(1)
        self.state.currentIndexChanged.connect(lambda _: self.refresh())
        bar.addWidget(self.state); bar.addStretch()
        if session.can("attendance.approve"):
            bar.addWidget(button("Reject…", on_click=lambda: self._decide(False))); bar.addWidget(button("Approve…", "primary", lambda: self._decide(True)))
        if session.can("attendance.manage"):
            bar.addWidget(button("Request correction", on_click=self._new))
        self.lay.addLayout(bar)
        self.table = DataTable([Column("id", "No.", 60), Column("work_date", "Day", 100), Column("code", "Code", 80), Column("name", "Employee", 170),
                                Column("kind", "Correction", 150, fmt=lambda v: "Add punch" if v == "add_punch" else "Ignore punch"),
                                Column("local_time", "Time", 70), Column("direction", "Type", 70, fmt=lambda v: _cap(v) if v else ""), Column("reason", "Reason", 240),
                                Column("requested_by", "Requested by", 110),
                                Column("status", "State", 150, status=lambda v: {"pending": "warn", "approved": "ok"}.get(v, "neutral"), fmt=lambda v: STATUS_TEXT.get(v, v)),
                                Column("decided_by", "Decided by", 110)])
        self.panel = StatePanel(self.table, "No corrections.")
        self.panel.retry.connect(self.refresh)
        self.lay.addWidget(self.panel, 1)

    def refresh(self) -> None:
        self.panel.show_loading()
        c = self.session.client

        def done(rows):
            self.table.set_rows(rows); self.panel.show_content() if rows else self.panel.show_empty()
        run_async(lambda: c.get("/attendance/corrections", status=self.state.currentData()), done, lambda e: self.panel.show_error(e.message, e.offline))

    def _new(self) -> None:
        if CorrectionDialog(self.session, None, self).exec():
            self.status.show_message("Correction sent for approval.", "ok"); self.refresh()

    def _decide(self, approve: bool) -> None:
        r = self.table.selected_row()
        if r is None or r["status"] != "pending":
            return self.status.show_message("Select a correction that is waiting for approval.", "info")
        c = self.session.client
        d = NoteDialog("Approve correction" if approve else "Reject correction",
                       f"{r['name']} on {r['work_date']}: {'add a punch' if r['kind'] == 'add_punch' else 'ignore a punch'}. Reason given: {r['reason']}. "
                       "Someone other than the person who requested it must decide.", "Approve" if approve else "Reject",
                       lambda t: c.post(f"/attendance/corrections/{r['id']}/decision", json={"approve": approve, "note": t}), self)
        if d.exec():
            self.status.show_message("Decision saved.", "ok"); self.refresh()


class ImportTab(_Tab):
    def __init__(self, session: Session):
        super().__init__(session)
        self.path: str | None = None
        self.can_import = session.can("attendance.manage")
        bar = QHBoxLayout()
        self.device = QComboBox(); self.device.setMinimumWidth(260)
        self.file_label = label("No file chosen", "muted")
        bar.addWidget(label("Device", "fieldLabel")); bar.addWidget(self.device); bar.addWidget(self.file_label, 1)
        if self.can_import:
            bar.addWidget(button("Choose file…", on_click=self._choose))
            self.check = button("Check file (nothing is saved)", on_click=lambda: self._send(True)); self.go = button("Import", "primary", lambda: self._send(False))
            bar.addWidget(self.check); bar.addWidget(self.go)
            self.check.setEnabled(False); self.go.setEnabled(False)
        self.lay.addLayout(bar)
        self.result = label("", "muted", wrap=True)
        self.lay.addWidget(self.result)
        self.rejects = DataTable([Column("line", "Line", 70, "right"), Column("reason", "Why it was not read", 320), Column("raw", "Row")])
        self.rejects.setMaximumHeight(150)
        self.lay.addWidget(self.rejects)
        self.lay.addWidget(label("Recent imports", "sectionTitle"))
        self.table = DataTable([Column("created_at", "When", 140, fmt=_when), Column("device", "Device", 160), Column("filename", "File"), Column("rows_total", "Rows", 70, "right"),
                                Column("rows_new", "New", 70, "right"), Column("rows_duplicate", "Already there", 110, "right"), Column("rows_rejected", "Rejected", 80, "right"),
                                Column("rows_unmatched", "No employee", 100, "right"), Column("imported_by", "By", 100)])
        self.panel = StatePanel(self.table, "Nothing imported yet.")
        self.panel.retry.connect(self.refresh)
        self.lay.addWidget(self.panel, 1)

    def refresh(self) -> None:
        self.panel.show_loading()
        c = self.session.client

        def done(d):
            devices, imports = d
            keep = self.device.currentData()
            self.device.clear()
            for x in devices:
                if x["is_active"]:
                    self.device.addItem(x["name"], x["id"])
            _set_combo(self.device, keep)
            self.table.set_rows(imports); self.panel.show_content() if imports else self.panel.show_empty()
            if not devices and self.can_import:
                self.result.setText("Add the attendance device first (Devices and policy tab).")
        run_async(lambda: (c.get("/attendance/devices"), c.get("/attendance/imports")), done, lambda e: self.panel.show_error(e.message, e.offline))

    def _choose(self) -> None:
        p, _ = QFileDialog.getOpenFileName(self, "Choose the exported attendance file", "", "Attendance exports (*.csv *.txt *.xlsx);;All files (*)")
        if p:
            self.path = p
            self.file_label.setText(p.replace("\\", "/").rsplit("/", 1)[-1])
            self.check.setEnabled(True); self.go.setEnabled(True)
            self.result.setText(""); self.rejects.set_rows([])

    def _send(self, dry: bool) -> None:
        if not self.path or self.device.currentData() is None:
            return self.status.show_message("Choose the device and the file first.", "info")
        c, path, dev = self.session.client, self.path, self.device.currentData()
        name = path.replace("\\", "/").rsplit("/", 1)[-1]
        self.check.setEnabled(False); self.go.setEnabled(False)

        def done(r):
            self.check.setEnabled(True); self.go.setEnabled(not dry or True)
            head = "Check only, nothing saved. " if dry else "Imported. "
            self.result.setText(f"{head}{r['rows_total']} rows read: {r['rows_new']} new, {r['rows_duplicate']} already stored, {r['rows_rejected']} not readable, "
                                f"{r['rows_unmatched']} with no matching employee."
                                + (f" Unknown device numbers: {', '.join(r['unmatched_ids'])}." if r.get("unmatched_ids") else "")
                                + (f" Punches from {_when(r['first_punch_at'])} to {_when(r['last_punch_at'])}." if r.get("first_punch_at") else ""))
            self.rejects.set_rows(r.get("rejects", []))
            if not dry:
                self.status.show_message("File imported. Daily summaries are updated.", "ok"); self.refresh()

        def fail(e):
            self.check.setEnabled(True); self.go.setEnabled(True)
            self.status.show_message(e.message, "error")
        run_async(lambda: c.post_file("/attendance/imports", path, device_id=dev, filename=name, dry_run="true" if dry else "false"), done, fail)


class SetupTab(_Tab):
    def __init__(self, session: Session):
        super().__init__(session)
        self.can_edit = session.can("attendance.manage")
        self.adapters: list[dict] = []
        bar = QHBoxLayout()
        bar.addWidget(label("Devices", "sectionTitle")); bar.addStretch()
        if self.can_edit:
            bar.addWidget(button("Edit selected", on_click=self._edit)); bar.addWidget(button("New device", "primary", self._new))
        self.lay.addLayout(bar)
        codes = loc_codes(session)
        self.devices = DataTable([Column("name", "Device", 180), Column("location_id", "Location", 80, fmt=lambda v: codes.get(v, "")), Column("brand", "Brand", 110),
                                  Column("model", "Model", 110), Column("protocol", "Protocol", 120), Column("export_format", "Export", 100), Column("adapter", "Reader", 130),
                                  Column("last_import_at", "Last import", 140, fmt=_when), Column("is_active", "State", 80, status=lambda v: "ok" if v else "neutral", fmt=lambda v: "In use" if v else "Off")])
        self.devices.setMaximumHeight(190)
        self.lay.addWidget(self.devices)
        pb = QHBoxLayout()
        pb.addWidget(label("Attendance policy", "sectionTitle")); pb.addStretch()
        self.lay.addLayout(pb)
        self.policies = DataTable([Column("location_id", "Applies to", 110, fmt=lambda v: codes.get(v, "Everywhere else") if v else "Everywhere else"), Column("timezone", "Time zone", 140),
                                   Column("shift_start", "Starts", 70), Column("shift_end", "Ends", 70), Column("grace_minutes", "Grace (min)", 100, "right"),
                                   Column("min_present_minutes", "Min. for a full day (min)", 170, "right"), Column("overtime_enabled", "Overtime", 90, fmt=lambda v: "On" if v else "Off"),
                                   Column("weekly_off", "Weekly off", 150, fmt=lambda v: ", ".join(WEEKDAYS[i] for i in v) or "None"),
                                   Column("direction_mode", "In/out from", 130, fmt=lambda v: "Device" if v == "device" else "First and last punch")])
        self.policies.setMaximumHeight(170)
        self.lay.addWidget(self.policies)
        if self.can_edit:
            self.lay.addWidget(label("Set or change a policy", "fieldLabel"))
            g = FormGrid(4)
            self.loc = loc_combo(session, required=False)
            self.tz = QLineEdit(); self.tz.setPlaceholderText("e.g. Asia/Karachi")
            self.start = QTimeEdit(); self.start.setDisplayFormat("HH:mm"); self.start.setTime(QTime(9, 0))
            self.end = QTimeEdit(); self.end.setDisplayFormat("HH:mm"); self.end.setTime(QTime(18, 0))
            self.grace = QSpinBox(); self.grace.setRange(0, 600)
            self.minp = QSpinBox(); self.minp.setRange(0, 1440); self.minp.setSingleStep(30)
            self.ot = QCheckBox("Count overtime"); self.ota = QSpinBox(); self.ota.setRange(0, 600)
            self.mode = QComboBox(); self.mode.addItem("Use the device's in/out", "device"); self.mode.addItem("First punch in, last punch out", "first_last")
            g.add("loc", "Location", self.loc); g.add("tz", "Time zone", self.tz); g.add("start", "Shift starts", self.start); g.add("end", "Shift ends", self.end)
            g.add("grace", "Grace (minutes)", self.grace); g.add("minp", "Minutes for a full day", self.minp); g.add("ota", "Overtime after (min)", self.ota); g.add("mode", "In/out from", self.mode)
            self.form = g
            self.lay.addWidget(g)
            row = QHBoxLayout()
            self.days = [QCheckBox(d) for d in WEEKDAYS]
            row.addWidget(label("Weekly off", "fieldLabel"))
            for cb in self.days:
                row.addWidget(cb)
            row.addWidget(self.ot); row.addStretch()
            row.addWidget(button("Save policy", "primary", self._save_policy))
            self.lay.addLayout(row)
            self.policies.row_activated.connect(self._load_policy)
            self.lay.addWidget(label("Double-click a policy above to edit it. Saving recalculates every daily summary the policy affects. Nothing is assumed: until a policy exists, "
                                     "no summaries are produced for that location.", "muted", wrap=True))

    def refresh(self) -> None:
        c = self.session.client

        def done(d):
            self.adapters, devs, pols = d
            self.devices.set_rows(devs); self.policies.set_rows(pols)
        run_async(lambda: (c.get("/attendance/adapters"), c.get("/attendance/devices"), c.get("/attendance/policies")), done, lambda e: self.status.show_message(e.message, "error"))

    def _new(self) -> None:
        if DeviceDialog(self.session, None, self.adapters, self).exec():
            self.status.show_message("Device saved.", "ok"); self.refresh()

    def _edit(self) -> None:
        r = self.devices.selected_row()
        if r is None:
            return self.status.show_message("Select a device first.", "info")
        if DeviceDialog(self.session, r, self.adapters, self).exec():
            self.status.show_message("Device updated.", "ok"); self.refresh()

    def _load_policy(self, r: dict) -> None:
        _set_combo(self.loc, r["location_id"]); self.tz.setText(r["timezone"])
        self.start.setTime(QTime.fromString(r["shift_start"][:5], "HH:mm")); self.end.setTime(QTime.fromString(r["shift_end"][:5], "HH:mm"))
        self.grace.setValue(r["grace_minutes"]); self.minp.setValue(r["min_present_minutes"]); self.ot.setChecked(r["overtime_enabled"]); self.ota.setValue(r["overtime_after_minutes"])
        _set_combo(self.mode, r["direction_mode"])
        for i, cb in enumerate(self.days):
            cb.setChecked(i in r["weekly_off"])

    def _save_policy(self) -> None:
        self.form.clear_errors()
        if not self.tz.text().strip():
            self.form.set_error("tz", "Enter the time zone, for example Asia/Karachi."); return
        body = {"location_id": self.loc.currentData(), "timezone": self.tz.text().strip(), "shift_start": self.start.time().toString("HH:mm:ss"),
                "shift_end": self.end.time().toString("HH:mm:ss"), "grace_minutes": self.grace.value(), "min_present_minutes": self.minp.value(),
                "overtime_enabled": self.ot.isChecked(), "overtime_after_minutes": self.ota.value(), "weekly_off": [i for i, cb in enumerate(self.days) if cb.isChecked()],
                "direction_mode": self.mode.currentData(), "reason": "Set from the desktop app"}
        c = self.session.client
        run_async(lambda: c.put("/attendance/policies", body), lambda _: (self.status.show_message("Policy saved and summaries recalculated.", "ok"), self.refresh()),
                  lambda e: self.status.show_message(e.message, "error"))


class AttendancePage(Page):
    title = "Attendance"

    def __init__(self, session: Session):
        super().__init__(session)
        self.root.addWidget(PageHeader("Attendance", "Biometric punches, daily summaries and exceptions. Visible only to the CEO, managers and specifically authorised users."))
        self.tabs = QTabWidget()
        self.root.addWidget(self.tabs, 1)
        for t, w in (("Daily", DaysTab(session)), ("Period summary", PeriodTab(session)), ("Exceptions", ExceptionsTab(session)),
                     ("Corrections", CorrectionsTab(session)), ("Import", ImportTab(session)), ("Devices and policy", SetupTab(session))):
            self.tabs.addTab(w, t)
        self.tabs.currentChanged.connect(lambda _: self.refresh())

    def refresh(self) -> None:
        w = self.tabs.currentWidget()
        if isinstance(w, _Tab):
            w.refresh()

    def scope_changed(self) -> None:
        self.refresh()
