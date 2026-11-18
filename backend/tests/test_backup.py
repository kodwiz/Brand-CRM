"""Phase 9: backup, encryption, restore, verification and retention. Uses the real pg_dump / pg_restore against the test database."""
import importlib.util
import json
import os
import shutil
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text

from backend.tests.conftest import ROOT, TEST_URL, line, receive
from backend.tests.test_accounting import run_posting

SCRIPT = ROOT / "scripts" / "backup.py"
PASS = "a-long-test-passphrase"

pytestmark = pytest.mark.skipif(not shutil.which("pg_dump") or not shutil.which("pg_restore") or not shutil.which("psql"), reason="PostgreSQL client tools not installed")


@pytest.fixture(scope="module")
def bk():
    spec = importlib.util.spec_from_file_location("backup_script", SCRIPT)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def tool(*args, passphrase=PASS, check=True):
    env = dict(os.environ, DATABASE_URL=TEST_URL, BACKUP_PASSPHRASE=passphrase)
    r = subprocess.run([sys.executable, str(SCRIPT), *args], cwd=ROOT, env=env, capture_output=True, text=True)
    if check:
        assert r.returncode == 0, r.stdout + r.stderr
    return r


from backend.tests.test_sales import ctx, delivered_paid_order  # noqa: E402,F401  (ctx is a fixture)


@pytest.fixture
def business(client, auth, ctx):
    """Real committed records in the test database: stock received, a sale delivered and paid, everything posted to the ledger."""
    delivered_paid_order(client, auth, ctx)
    run_posting(client, auth)


def drop(dbname):
    e = create_engine(TEST_URL.replace("postgresql+psycopg", "postgresql+psycopg").rsplit("/", 1)[0] + "/postgres", isolation_level="AUTOCOMMIT")
    with e.connect() as c:
        c.execute(text(f'DROP DATABASE IF EXISTS "{dbname}" WITH (FORCE)'))
    e.dispose()


# ---- encryption ------------------------------------------------------------------------------------------------------------
def test_encryption_round_trips_and_rejects_wrong_passphrase_tampering_and_truncation(bk, tmp_path, monkeypatch):
    monkeypatch.setattr(bk, "CHUNK", 1000)
    data = os.urandom(4500)
    src, enc, out = tmp_path / "a.bin", tmp_path / "a.enc", tmp_path / "a.out"
    src.write_bytes(data)
    bk.encrypt_file(src, enc, b"passphrase-one-1")
    assert data[:50] not in enc.read_bytes()
    bk.decrypt_file(enc, out, b"passphrase-one-1")
    assert out.read_bytes() == data
    with pytest.raises(SystemExit):
        bk.decrypt_file(enc, tmp_path / "x", b"wrong-passphrase!")
    raw = bytearray(enc.read_bytes())
    raw[len(raw) // 2] ^= 1
    (tmp_path / "t.enc").write_bytes(bytes(raw))
    with pytest.raises(SystemExit):
        bk.decrypt_file(tmp_path / "t.enc", tmp_path / "x", b"passphrase-one-1")
    (tmp_path / "short.enc").write_bytes(enc.read_bytes()[:-1016])           # a whole chunk missing from the end
    with pytest.raises(SystemExit):
        bk.decrypt_file(tmp_path / "short.enc", tmp_path / "x", b"passphrase-one-1")
    body = enc.read_bytes()
    head, chunks = body[:32], [body[32 + i * 1016:32 + (i + 1) * 1016] for i in range(5)]
    chunks[0], chunks[1] = chunks[1], chunks[0]
    (tmp_path / "swap.enc").write_bytes(head + b"".join(chunks))
    with pytest.raises(SystemExit):
        bk.decrypt_file(tmp_path / "swap.enc", tmp_path / "x", b"passphrase-one-1")
    empty = tmp_path / "e"; empty.write_bytes(b"")
    bk.encrypt_file(empty, tmp_path / "e.enc", b"passphrase-one-1")
    bk.decrypt_file(tmp_path / "e.enc", tmp_path / "e.out", b"passphrase-one-1")
    assert (tmp_path / "e.out").read_bytes() == b""


def test_a_missing_or_short_passphrase_is_refused(bk, monkeypatch):
    monkeypatch.delenv("BACKUP_PASSPHRASE", raising=False)
    with pytest.raises(SystemExit):
        bk._passphrase()
    monkeypatch.setenv("BACKUP_PASSPHRASE", "short")
    with pytest.raises(SystemExit):
        bk._passphrase()


# ---- retention ---------------------------------------------------------------------------------------------------------------
def test_retention_keeps_14_daily_8_weekly_12_monthly_and_always_the_newest(bk, tmp_path):
    now = datetime(2026, 11, 4, 2, 0, tzinfo=timezone.utc)
    folders = []
    for i in range(0, 500):
        d = now - timedelta(days=i)
        p = tmp_path / f"db-{d:%Y%m%dT%H%M%SZ}"
        p.mkdir()
        folders.append(p)
    kept, gone = bk.plan_retention(folders)
    assert folders[0] in kept
    assert all(folders[i] in kept for i in range(14)), "the last 14 days are all kept"
    assert 14 <= len(kept) <= 14 + 8 + 12
    assert len(kept) + len(gone) == 500
    assert folders[100] in gone and folders[499] in gone
    kept_months = {(bk._stamp_of(p).year, bk._stamp_of(p).month) for p in kept}
    assert len(kept_months) >= 12
    k2, g2 = bk.plan_retention([tmp_path / "db-20260101T000000Z"])
    assert k2 and not g2
    (tmp_path / "notabackup").mkdir()
    assert bk._stamp_of(tmp_path / "notabackup") is None


# ---- the real thing ------------------------------------------------------------------------------------------------------------------
def test_backup_verify_and_restore_reproduce_the_business_exactly(client, auth, business, tmp_path):
    out = tmp_path / "b"
    tool("backup", "--out", str(out))
    bdir = next(out.iterdir())
    m = json.loads((bdir / "manifest.json").read_text())
    assert not m["encrypted"] and m["table_counts"]["journal_lines"] > 0
    assert m["fingerprint"]["journal_debits"] == m["fingerprint"]["journal_credits"] != "0"
    v = tool("verify", str(bdir))
    assert "RESTORE VERIFIED" in v.stdout and "totals match exactly" in v.stdout
    name = "restore_check_plain"
    drop(name)
    try:
        tool("restore", str(bdir), "--into", name)
        e = create_engine(TEST_URL.rsplit("/", 1)[0] + "/" + name)
        with e.connect() as c:
            assert c.execute(text("select count(*) from journal_lines")).scalar_one() == m["table_counts"]["journal_lines"]
            assert str(c.execute(text("select coalesce(sum(debit),0) from journal_lines")).scalar_one()) == m["fingerprint"]["journal_debits"]
        e.dispose()
        again = tool("restore", str(bdir), "--into", name, check=False)
        assert again.returncode != 0, "restore must never overwrite an existing database"
    finally:
        drop(name)


def test_encrypted_backup_needs_the_passphrase_and_restores_with_it(client, auth, business, tmp_path):
    out = tmp_path / "b"
    tool("backup", "--out", str(out), "--encrypt")
    bdir = next(out.iterdir())
    assert (bdir / "database.dump.enc").exists() and not (bdir / "database.dump").exists()
    assert b"PGDMP" not in (bdir / "database.dump.enc").read_bytes()[:64]
    assert "RESTORE VERIFIED" in tool("verify", str(bdir)).stdout
    bad = tool("verify", str(bdir), passphrase="another-long-passphrase", check=False)
    assert bad.returncode != 0 and "passphrase" in (bad.stdout + bad.stderr).lower()
    no = tool("backup", "--out", str(out), "--encrypt", passphrase="", check=False)
    assert no.returncode != 0


def test_a_damaged_or_tampered_backup_is_rejected_before_anything_is_restored(client, auth, business, tmp_path):
    out = tmp_path / "b"
    tool("backup", "--out", str(out))
    bdir = next(out.iterdir())
    with (bdir / "database.dump").open("ab") as f:
        f.write(b"x")
    r = tool("verify", str(bdir), check=False)
    assert r.returncode != 0 and "checksum" in (r.stdout + r.stderr).lower()
    r2 = tool("restore", str(bdir), "--into", "never_created", check=False)
    assert r2.returncode != 0
    drop("never_created")
    assert tool("verify", str(tmp_path), check=False).returncode != 0


def test_run_daily_backs_up_verifies_copies_off_machine_and_prunes(client, auth, business, tmp_path):
    out, off = tmp_path / "local", tmp_path / "offsite"
    off.mkdir()
    r = tool("run-daily", "--out", str(out), "--copy-to", str(off))
    assert "RESTORE VERIFIED" in r.stdout and "Copied to" in r.stdout
    assert len(list(off.iterdir())) == 1
    missing = tool("run-daily", "--out", str(out), "--copy-to", str(tmp_path / "drive-not-connected"), check=False)
    assert missing.returncode != 0 and "not available" in (missing.stdout + missing.stderr)
    nocopy = tool("run-daily", "--out", str(tmp_path / "x"), check=False)
    assert "WARNING" in nocopy.stdout
    p = tool("prune", "--out", str(out))
    assert "kept" in p.stdout


def test_verify_detects_a_restore_that_loses_money_even_when_row_counts_match(client, auth, business, tmp_path, bk):
    """The fingerprint exists because a table can have the right number of rows and still be wrong."""
    out = tmp_path / "b"
    tool("backup", "--out", str(out))
    bdir = next(out.iterdir())
    mp = bdir / "manifest.json"
    m = json.loads(mp.read_text())
    m["fingerprint"]["journal_debits"] = "1.00"
    mp.write_text(json.dumps(m))
    r = tool("verify", str(bdir), check=False)
    assert r.returncode != 0 and "differ" in r.stdout and "VERIFICATION FAILED" in r.stdout
