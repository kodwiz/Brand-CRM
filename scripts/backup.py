"""Database + media backup, restore, restore verification, retention and the daily routine.

    python scripts/backup.py backup  [--out backups] [--encrypt]   # pg_dump (custom format) + media archive + manifest
    python scripts/backup.py verify  BACKUP_DIR                    # restore into a scratch database and prove it matches what was backed up
    python scripts/backup.py restore BACKUP_DIR --into NEWDB       # restore into a NEW database (never overwrites the live one)
    python scripts/backup.py prune   [--out backups] [--apply]     # retention: 14 daily, 8 weekly, 12 monthly (shows what it would delete unless --apply)
    python scripts/backup.py run-daily [--out backups] [--copy-to DIR] [--encrypt]   # backup, verify, copy off-machine, prune: the one command to schedule

Secrets never appear on a command line: the database password goes to the PostgreSQL tools through the child process environment, and the
encryption passphrase is read from the BACKUP_PASSPHRASE environment variable. Scheduling is described in docs/backup-and-recovery.md; nothing is scheduled by this file.

Encryption: AES-256-GCM in 1 MiB chunks, key from the passphrase with scrypt. Every chunk is authenticated together with its position and whether it is the last,
so a damaged, reordered or truncated file is rejected, never silently restored. LOSE THE PASSPHRASE AND THE BACKUP CANNOT BE OPENED: there is no recovery."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import struct
import subprocess
import sys
import tarfile
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from sqlalchemy import create_engine, text  # noqa: E402
from sqlalchemy.engine import make_url  # noqa: E402

from backend.config import get_settings  # noqa: E402

MAGIC = b"COSBKP1\n"
CHUNK = 1 << 20
RETENTION = {"daily": 14, "weekly": 8, "monthly": 12}


# ---- helpers -----------------------------------------------------------------------------------------------------------------------
def _conn_env(url) -> dict:
    env = dict(os.environ)
    if url.password:
        env["PGPASSWORD"] = url.password
    return env


def _args(url, dbname: str | None = None) -> list[str]:
    a = ["-h", url.host or "localhost", "-p", str(url.port or 5432)]
    if url.username:
        a += ["-U", url.username]
    return a + ["-d", dbname or url.database]


def _sha(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _sqla_url(url) -> str:
    return url.set(drivername="postgresql+psycopg").render_as_string(hide_password=False)


def _table_counts(db_url) -> dict[str, int]:
    e = create_engine(db_url)
    with e.connect() as c:
        tables = [r[0] for r in c.execute(text("select tablename from pg_tables where schemaname='public' order by 1"))]
        out = {t: c.execute(text(f'select count(*) from "{t}"')).scalar_one() for t in tables}
    e.dispose()
    return out


FINGERPRINT_SQL = {
    "journal_debits": "select coalesce(sum(debit),0) from journal_lines",
    "journal_credits": "select coalesce(sum(credit),0) from journal_lines",
    "stock_value": "select coalesce(sum(value_delta),0) from stock_movements",
    "stock_units": "select coalesce(sum(qty_delta),0) from stock_movements",
    "supplier_ledger": "select coalesce(sum(amount),0) from supplier_ledger",
    "distributor_ledger": "select coalesce(sum(amount),0) from distributor_ledger",
    "audit_entries": "select count(*) from audit_log",
}


def _fingerprint(db_url) -> dict[str, str]:
    """Money and quantity totals that a restore must reproduce exactly: a table can have the right number of rows and still be wrong."""
    e = create_engine(db_url)
    out = {}
    with e.connect() as c:
        for k, sql in FINGERPRINT_SQL.items():
            try:
                out[k] = str(c.execute(text(sql)).scalar_one())
            except Exception:
                c.rollback()
                out[k] = "n/a"
    e.dispose()
    return out


# ---- encryption --------------------------------------------------------------------------------------------------------------------
def _passphrase() -> bytes:
    p = os.environ.get("BACKUP_PASSPHRASE", "")
    if len(p) < 12:
        raise SystemExit("Set the BACKUP_PASSPHRASE environment variable (at least 12 characters). It is never accepted on the command line.")
    return p.encode("utf-8")


def _key(passphrase: bytes, salt: bytes) -> bytes:
    return hashlib.scrypt(passphrase, salt=salt, n=2 ** 15, r=8, p=1, dklen=32, maxmem=128 * 1024 * 1024)


def _gcm():
    try:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    except ImportError:
        raise SystemExit("Encryption needs the 'cryptography' package: pip install -r requirements.txt")
    return AESGCM


def encrypt_file(src: Path, dst: Path, passphrase: bytes) -> None:
    AESGCM = _gcm()
    salt, prefix = os.urandom(16), os.urandom(8)
    aead = AESGCM(_key(passphrase, salt))
    size = src.stat().st_size
    with src.open("rb") as fi, dst.open("wb") as fo:
        fo.write(MAGIC + salt + prefix)
        i, done = 0, 0
        while True:
            block = fi.read(CHUNK)
            done += len(block)
            last = done >= size
            nonce = prefix + struct.pack(">I", i)
            fo.write(aead.encrypt(nonce, block, struct.pack(">I?", i, last)))
            i += 1
            if last:
                break


def decrypt_file(src: Path, dst: Path, passphrase: bytes) -> None:
    from cryptography.exceptions import InvalidTag
    AESGCM = _gcm()
    with src.open("rb") as fi:
        head = fi.read(len(MAGIC) + 24)
        if not head.startswith(MAGIC) or len(head) != len(MAGIC) + 24:
            raise SystemExit(f"{src.name} is not a file made by this tool.")
        salt, prefix = head[len(MAGIC):len(MAGIC) + 16], head[len(MAGIC) + 16:]
        aead = AESGCM(_key(passphrase, salt))
        total = src.stat().st_size - len(head)
        i, read = 0, 0
        try:
            with dst.open("wb") as fo:
                while read < total:
                    block = fi.read(CHUNK + 16)
                    read += len(block)
                    last = read >= total
                    fo.write(aead.decrypt(prefix + struct.pack(">I", i), block, struct.pack(">I?", i, last)))
                    i += 1
        except InvalidTag:
            dst.unlink(missing_ok=True)
            raise SystemExit("Could not decrypt: the passphrase is wrong, or the backup is damaged, reordered or truncated.")


# ---- backup --------------------------------------------------------------------------------------------------------------------------
def cmd_backup(a) -> int:
    s = get_settings()
    url = make_url(s.database_url.replace("+psycopg", ""))
    pw = _passphrase() if a.encrypt else None
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out = Path(a.out) / f"{url.database}-{stamp}"
    out.mkdir(parents=True, exist_ok=False)
    with tempfile.TemporaryDirectory() as tmp:
        dump = Path(tmp) / "database.dump"
        r = subprocess.run(["pg_dump", "-Fc", "--no-owner", "-f", str(dump), *_args(url)], env=_conn_env(url), capture_output=True, text=True)
        if r.returncode != 0:
            print("pg_dump failed:", r.stderr.strip(), file=sys.stderr)
            shutil.rmtree(out, ignore_errors=True)
            return 1
        parts = [dump]
        media = Path(s.media_dir)
        if media.exists() and any(media.rglob("*")):
            media_tar = Path(tmp) / "media.tar.gz"
            with tarfile.open(media_tar, "w:gz") as t:
                t.add(media, arcname="media")
            parts.append(media_tar)
        files = {}
        for p in parts:
            if pw:
                dst = out / (p.name + ".enc")
                encrypt_file(p, dst, pw)
            else:
                dst = out / p.name
                shutil.copy2(p, dst)
            files[dst.name] = {"sha256": _sha(dst), "bytes": dst.stat().st_size}
    manifest = {"created_utc": stamp, "database": url.database, "environment": s.environment, "encrypted": bool(pw), "files": files,
                "table_counts": _table_counts(s.database_url), "fingerprint": _fingerprint(s.database_url)}
    (out / "manifest.json").write_text(json.dumps(manifest, indent=2))
    print(f"Backup written to {out}{' (encrypted)' if pw else ' (NOT encrypted: store it somewhere access-controlled)'}")
    for n, m in files.items():
        print(f"  {n}  {m['bytes']:,} bytes  sha256 {m['sha256'][:16]}...")
    print("Copy this folder OFF this machine; a backup on the same disk does not protect against disk or machine loss.")
    return 0


def _check_files(bdir: Path) -> dict:
    mf = bdir / "manifest.json"
    if not mf.exists():
        raise SystemExit(f"{bdir} has no manifest.json: this is not a backup folder.")
    m = json.loads(mf.read_text())
    for name, meta in m["files"].items():
        if not (bdir / name).exists():
            raise SystemExit(f"{name} is missing from the backup.")
        if _sha(bdir / name) != meta["sha256"]:
            raise SystemExit(f"Checksum mismatch for {name}: the backup is damaged or was modified.")
    return m


def _plain_dump(bdir: Path, m: dict, tmp: Path) -> Path:
    if m.get("encrypted"):
        out = tmp / "database.dump"
        decrypt_file(bdir / "database.dump.enc", out, _passphrase())
        return out
    return bdir / "database.dump"


def _restore(bdir: Path, dbname: str, m: dict) -> None:
    s = get_settings()
    url = make_url(s.database_url.replace("+psycopg", ""))
    env = _conn_env(url)
    admin = _args(url, "postgres")
    with tempfile.TemporaryDirectory() as tmp:
        dump = _plain_dump(bdir, m, Path(tmp))              # decrypt first so a wrong passphrase fails before anything is created
        r = subprocess.run(["psql", *admin, "-v", "ON_ERROR_STOP=1", "-c", f'CREATE DATABASE "{dbname}"'], env=env, capture_output=True, text=True)
        if r.returncode != 0:
            raise SystemExit(f"Could not create {dbname}: {r.stderr.strip()}")
        r = subprocess.run(["pg_restore", "--no-owner", "--exit-on-error", *_args(url, dbname), str(dump)], env=env, capture_output=True, text=True)
        if r.returncode != 0:
            raise SystemExit(f"pg_restore failed: {r.stderr.strip()[:500]}")


def cmd_restore(a) -> int:
    bdir = Path(a.backup)
    m = _check_files(bdir)
    _restore(bdir, a.into, m)
    print(f"Restored into new database '{a.into}'. The live database was not touched. Point DATABASE_URL at it to use it.")
    return 0


def cmd_verify(a) -> int:
    """Restore into a throwaway database and prove it matches the manifest: files, row counts, money and quantity totals, and the stock-ledger check."""
    s = get_settings()
    bdir = Path(a.backup)
    m = _check_files(bdir)
    scratch = f"verify_{datetime.now(timezone.utc).strftime('%H%M%S%f')}"
    url = make_url(s.database_url.replace("+psycopg", ""))
    _restore(bdir, scratch, m)
    ok = True
    try:
        restored_url = _sqla_url(url.set(database=scratch))
        counts = _table_counts(restored_url)
        bad = {t: (m["table_counts"].get(t), n) for t, n in counts.items() if m["table_counts"].get(t) != n}
        if bad:
            ok = False
            print("Row-count differences (backup manifest vs restored):", bad)
        else:
            print(f"Row counts match for all {len(counts)} tables.")
        fp = _fingerprint(restored_url)
        diff = {k: (m.get("fingerprint", {}).get(k), v) for k, v in fp.items() if m.get("fingerprint", {}).get(k) != v}
        if diff:
            ok = False
            print("Totals differ between the backup manifest and the restored copy:", diff)
        else:
            print("Ledger, stock, supplier and distributor totals match exactly.")
        os.environ["DATABASE_URL"] = restored_url
        from importlib import reload
        import backend.db as dbm
        reload(dbm)
        from backend.services import stock_query_service
        with dbm.get_session_factory()() as db:
            problems = stock_query_service.integrity_check(db)
        if problems:
            ok = False
            print("Stock ledger integrity problems in restored copy:", problems[:5])
        else:
            print("Stock ledger reconciles with balances and costs in the restored copy.")
    finally:
        admin = create_engine(_sqla_url(url.set(database="postgres")), isolation_level="AUTOCOMMIT")
        with admin.connect() as c:
            c.execute(text(f'DROP DATABASE IF EXISTS "{scratch}" WITH (FORCE)'))
        admin.dispose()
    print("RESTORE VERIFIED" if ok else "RESTORE VERIFICATION FAILED")
    return 0 if ok else 1


# ---- retention ---------------------------------------------------------------------------------------------------------------------------------
def _stamp_of(p: Path):
    try:
        return datetime.strptime(p.name.rsplit("-", 1)[1], "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)
    except (IndexError, ValueError):
        return None


def plan_retention(folders: list[Path], keep: dict | None = None) -> tuple[list[Path], list[Path]]:
    """Keep the newest backup of each of the last N days, ISO weeks and months. Everything else can go. The very newest is always kept."""
    keep = keep or RETENTION
    dated = sorted(((_stamp_of(p), p) for p in folders if _stamp_of(p)), key=lambda x: x[0], reverse=True)
    keepers: set[Path] = set()
    for label, n, key in (("daily", keep["daily"], lambda d: d.date()), ("weekly", keep["weekly"], lambda d: d.isocalendar()[:2]), ("monthly", keep["monthly"], lambda d: (d.year, d.month))):
        seen = []
        for d, p in dated:
            k = key(d)
            if k not in seen:
                seen.append(k)
                if len(seen) <= n:
                    keepers.add(p)
    if dated:
        keepers.add(dated[0][1])
    return [p for _, p in dated if p in keepers], [p for _, p in dated if p not in keepers]


def cmd_prune(a) -> int:
    root = Path(a.out)
    folders = [p for p in root.iterdir() if p.is_dir() and (p / "manifest.json").exists()] if root.exists() else []
    kept, gone = plan_retention(folders)
    print(f"{len(kept)} backup(s) kept, {len(gone)} outside the retention policy (14 daily, 8 weekly, 12 monthly).")
    for p in gone:
        print(("  deleting " if a.apply else "  would delete ") + p.name)
        if a.apply:
            shutil.rmtree(p)
    if gone and not a.apply:
        print("Nothing was deleted. Run again with --apply to delete them.")
    return 0


def cmd_run_daily(a) -> int:
    """The routine to schedule: backup, prove it restores, copy it somewhere else, then prune. Any failure stops and returns non-zero, so the scheduler shows it."""
    before = set(Path(a.out).glob("*")) if Path(a.out).exists() else set()
    if cmd_backup(argparse.Namespace(out=a.out, encrypt=a.encrypt)) != 0:
        return 1
    new = sorted(set(Path(a.out).glob("*")) - before)
    if len(new) != 1:
        print("Could not find the backup that was just written.", file=sys.stderr)
        return 1
    latest = new[0]
    if cmd_verify(argparse.Namespace(backup=str(latest))) != 0:
        print("The new backup did NOT verify. Do not rely on it.", file=sys.stderr)
        return 1
    if a.copy_to:
        dest = Path(a.copy_to)
        if not dest.exists():
            print(f"The off-machine folder {dest} is not available (is the drive connected?). The backup was made and verified but NOT copied.", file=sys.stderr)
            return 1
        shutil.copytree(latest, dest / latest.name)
        _check_files(dest / latest.name)                 # exits non-zero if the copy is not identical
        print(f"Copied to {dest / latest.name} and checksums re-checked there.")
    else:
        print("WARNING: no --copy-to folder given. This backup exists only on this machine.")
    return cmd_prune(argparse.Namespace(out=a.out, apply=True))


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("backup"); b.add_argument("--out", default="backups"); b.add_argument("--encrypt", action="store_true"); b.set_defaults(fn=cmd_backup)
    v = sub.add_parser("verify"); v.add_argument("backup"); v.set_defaults(fn=cmd_verify)
    r = sub.add_parser("restore"); r.add_argument("backup"); r.add_argument("--into", required=True); r.set_defaults(fn=cmd_restore)
    pr = sub.add_parser("prune"); pr.add_argument("--out", default="backups"); pr.add_argument("--apply", action="store_true"); pr.set_defaults(fn=cmd_prune)
    d = sub.add_parser("run-daily"); d.add_argument("--out", default="backups"); d.add_argument("--copy-to"); d.add_argument("--encrypt", action="store_true"); d.set_defaults(fn=cmd_run_daily)
    a = p.parse_args()
    return a.fn(a)


if __name__ == "__main__":
    raise SystemExit(main())
