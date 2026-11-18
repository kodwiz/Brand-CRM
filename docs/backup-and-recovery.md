# Backup and recovery

## Commands (`scripts/backup.py`)

| Command | What it does |
|---|---|
| `backup [--out backups] [--encrypt]` | `pg_dump` (custom format) of the configured database, a tar.gz of the media folder, and `manifest.json` with SHA-256 checksums, per-table row counts and a ledger/stock fingerprint. With `--encrypt` the database dump (and media) are written as `.enc` files. |
| `verify BACKUP_DIR` | Checks checksums, decrypts if needed, restores into a temporary database, compares row counts with the manifest, compares ledger, stock, supplier and distributor totals, runs the stock-ledger integrity check, then drops the temporary database. Prints `RESTORE VERIFIED` only if everything matches. |
| `restore BACKUP_DIR --into NEWDB` | Restores into a **new** database. It refuses to overwrite an existing one. Point `DATABASE_URL` at the new database once you are satisfied. |
| `prune [--out backups] [--apply]` | Retention: keeps 14 daily, 8 weekly and 12 monthly backups. Without `--apply` it only shows what it would delete. |
| `run-daily [--out backups] [--copy-to DIR] [--encrypt]` | The one command to schedule: backup, verify, copy to another drive or folder (checksums re-checked there), prune. Refuses to count a backup as good unless verification passed. If `--copy-to` is missing (drive not connected) it says so loudly and reports the copy as NOT done. |

### Secrets

* The database password reaches `pg_dump` / `pg_restore` through the child process environment, never on a command line.
* The encryption passphrase is read **only** from the `BACKUP_PASSPHRASE` environment variable (at least 12 characters). It is never accepted as an argument and never logged.
* Encryption is AES-256-GCM in 1 MiB chunks with a key derived by scrypt. Each chunk is authenticated with its position and whether it is the last, so a damaged, reordered or truncated file is rejected rather than half-restored. A wrong passphrase is rejected.
* **If the passphrase is lost, the backups cannot be opened. There is no recovery.** Keep it in a password manager or sealed envelope that at least two trusted people can reach, separate from the backups themselves.

## Scheduling (nothing is scheduled for you)

**Windows (Task Scheduler)** — create a basic task, daily at a quiet hour, action "Start a program":

```
Program:   C:\cosmetics-ops\.venv\Scripts\python.exe
Arguments: scripts\backup.py run-daily --out D:\backups --copy-to E:\offsite --encrypt
Start in:  C:\cosmetics-ops
```
Set `BACKUP_PASSPHRASE` as a system environment variable for the account that runs the task (System Properties > Environment Variables), not in the task arguments. Tick "Run whether user is logged on or not" and "Run task as soon as possible after a scheduled start is missed".

**Linux / macOS (cron)**:

```
30 2 * * *  cd /opt/cosmetics-ops && . ./.venv/bin/activate && python scripts/backup.py run-daily --out /var/backups/cosmetics --copy-to /mnt/offsite --encrypt >> /var/log/cosmetics-backup.log 2>&1
```
Export `BACKUP_PASSPHRASE` for that user (for example from a root-readable file loaded by the cron environment); do not write it in the crontab line.

Then check weekly that the newest backup is under a day old: `python -m backend.cli readiness` reports this.

## Recovering after a loss

1. Stop the backend. Do not delete anything yet.
2. `python scripts/backup.py verify BACKUP_DIR` on the backup you intend to use (the off-machine copy if the machine is gone). Proceed only on `RESTORE VERIFIED`.
3. `python scripts/backup.py restore BACKUP_DIR --into cosmetics_restored`.
4. Set `DATABASE_URL` to the restored database; `python -m alembic upgrade head` (applies any newer migrations; does nothing if current).
5. Start the backend, sign in, open Accounting > Overview and the Integrity view; confirm the trial balance balances and every sub-ledger agrees.
6. Re-enter or re-import work done after the backup time (it is lost) and tell staff what time the data goes back to.
7. Media (product images) is restored from the same backup folder into the media directory.

## What was actually tested

* Automated (`backend/tests/test_backup.py`, 8 tests): backup, verify and restore reproduce a business with money, stock and orders exactly; encrypted backup needs the passphrase and restores with it; encryption round-trip, wrong passphrase, tampering and truncation are rejected; a missing or short passphrase is refused; a damaged backup is rejected before anything is restored; `run-daily` backs up, verifies, copies and prunes; verify detects a restore that loses money even when row counts match; retention keeps 14 daily, 8 weekly, 12 monthly and always the newest.
* Drill on the demo database (PostgreSQL 16): `run-daily --encrypt --copy-to` produced an encrypted backup, restored it into a scratch database, matched row counts on all 85 tables and the ledger/stock totals, copied it to a second folder and re-checked checksums there; verifying with a wrong passphrase was refused.

## Not covered (decide before real use)

* **No point-in-time recovery.** WAL archiving is not implemented. Recovery is to the moment of the last backup; anything after it is lost. Running `run-daily` more than once a day (for example also at midday) shortens the exposure.
* Off-machine storage is only as good as where you point `--copy-to`. A second drive in the same room does not protect against fire or theft; consider a cloud-synced folder (the files are encrypted, so this is safe if the passphrase is separate).
* The backup is of the database and media folder, not of `.env` or the desktop installation. Keep a copy of `.env` (JWT secret, database URL) somewhere safe; without the JWT secret all users simply sign in again, but you need the database credentials.
* Restore drills should be repeated: at least before the pilot, after any upgrade and quarterly.
