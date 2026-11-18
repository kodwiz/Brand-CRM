# Attendance

Attendance is built from **punches** imported from a biometric device. Raw punches are never edited or deleted; corrections are separate, approved records. Attendance is visible only
to the **CEO, managers and specifically authorised users** (`attendance.view`). Order bookers, stock keepers and accounts have no access by default, and the server refuses them.

## What is and is not built

* **Built:** a device-neutral adapter layer; one adapter, **Generic CSV / Excel export**, that reads a file using column names you configure; punch import with de-duplication;
  daily and period summaries; missing-punch exceptions; approved corrections; attendance policy.
* **Not built:** a connector for any specific device brand or protocol, and any live connection to a device. The brand, model, protocol and export format are **not assumed**. Once
  they are known, a new adapter is one small class in `backend/attendance/` registered in `registry.py`.

## Setting up

1. **Devices and policy tab > New device.** Name, location, brand/model/protocol/export format (free text), and the adapter settings as JSON:
   `columns` (`user_id` plus `timestamp`, or `date` and `time`; optional `direction`), `timestamp_format`, **`timezone`** (the device clock's zone; required, never assumed),
   optional `direction_map`, `delimiter`, `encoding`, `has_header`. Settings containing keys such as password, token or secret are refused: **never store credentials here.**
2. **Attendance policy** per location, or one default: time zone, shift start and end, grace minutes, minutes for a full day, optional overtime, weekly-off days, and how in/out is
   decided (**device** direction, or **first punch in / last punch out**). **No summaries are produced until a policy exists.** Saving a policy recalculates affected days.
3. **Employees** need the **device number** enrolled on the machine (Employees tab). Punches for numbers with no employee are kept and shown under Exceptions; entering the number on an
   employee (new or existing) matches them automatically.

## Importing

Import tab: choose the device and the exported file (CSV, TXT or XLSX), press **Check file** to see what would happen without saving, then **Import**. The result gives rows read, new,
already stored, unreadable (with the line and reason) and unmatched device numbers.

* The **same file** (by content) cannot be imported twice.
* A punch is identified by device, device user and exact time, so overlapping exports add **no duplicates**.
* Times in the future and unparseable rows are rejected, not guessed.

## Daily results

Each employee-day is one of: **Present, Late** (first check-in after start + grace), **Short** (worked less than the full-day minimum), **Incomplete** (a pairing problem), **No punches**,
or **Weekly off**. Exceptions: check-in without check-out, check-out without check-in, a single punch, or punches with no in/out information. Incomplete days show the check-in that exists.
The **current day is provisional**: until people punch, it shows "No punches".

## Corrections

Exceptions > *Request correction*, or Corrections > *Request correction*: **add a punch** (type and local time) or **ignore a wrong punch**, with a reason. It changes nothing until
**someone other than the requester** with `attendance.approve` approves it. The original punches stay stored and both steps are audited.

## Limits of this version

* No leave, public-holiday or roster model: a day off for those reasons shows as "No punches" unless it is a weekly-off day.
* No night shifts that cross midnight: a day is a calendar day in the policy time zone.
* One policy per location (plus a default); no per-employee shifts.
* No payroll, salary deduction or export. Reports are on-screen; exports and dashboards belong to Phase 8.
