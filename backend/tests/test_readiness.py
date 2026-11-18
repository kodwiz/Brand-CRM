"""Phase 9: the read-only go-live readiness check. Synthetic data only."""
import os

from backend import cli
from backend.config import get_settings


def by_name(rows):
    return {r["name"]: r for r in rows}


def test_readiness_flags_demo_users_non_production_and_missing_backup(client, auth, tmp_path, monkeypatch):
    monkeypatch.delenv("BACKUP_PASSPHRASE", raising=False)
    rows = by_name(cli.readiness_checks(str(tmp_path / "none")))
    assert rows["ENVIRONMENT is production"]["ok"] is False          # tests run in development
    assert rows["At least one active CEO account"]["ok"] is True
    assert rows["A backup newer than 2 day(s) exists"]["ok"] is False
    assert rows["Backup passphrase is set in the environment"]["ok"] is False
    assert rows["No demo users exist"]["ok"] is True


def test_readiness_sees_demo_users_and_a_fresh_backup(client, auth, db, tmp_path):
    from backend.tests.conftest import make_user
    make_user(db, "demo.someone", "manager")
    (tmp_path / "cosmetics-20990101").mkdir()
    rows = by_name(cli.readiness_checks(str(tmp_path)))
    assert rows["No demo users exist"]["ok"] is False
    assert rows["A backup newer than 2 day(s) exists"]["ok"] is True


def test_readiness_never_prints_the_secret_and_exit_code_reflects_blockers(client, auth, tmp_path, capsys, monkeypatch):
    import argparse
    monkeypatch.setenv("BACKUP_PASSPHRASE", "a-passphrase-that-must-not-appear")
    code = cli.cmd_readiness(argparse.Namespace(backup_dir=str(tmp_path), max_backup_age_days=2))
    out = capsys.readouterr().out
    assert code == 1
    assert get_settings().jwt_secret not in out and "a-passphrase-that-must-not-appear" not in out
