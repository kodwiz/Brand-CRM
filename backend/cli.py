"""Administrative commands.

    python -m backend.cli init                 # seed default roles (idempotent)
    python -m backend.cli create-ceo           # first administrator (password prompted)
    python -m backend.cli seed-demo            # synthetic demo data (refused in production)
    python -m backend.cli seed-demo-catalogue  # synthetic products + stock (ENVIRONMENT=demo only)
    python -m backend.cli readiness            # pre-go-live checks; exit code 1 if anything blocks (read-only)
"""
from __future__ import annotations

import argparse
import getpass
import secrets
import sys

from sqlalchemy import func, select

from backend.config import get_settings
from backend.db import get_session_factory
from backend.models import Location, Role, User, UserLocation
from backend.security import hash_password, password_problems
from backend.services import audit_service
from backend.services.role_service import seed_roles


def cmd_init(_: argparse.Namespace) -> int:
    with get_session_factory()() as db:
        seed_roles(db)
        db.commit()
        n = len(db.scalars(select(Role)).all())
    print(f"Roles ready ({n} total).")
    return 0


def cmd_create_ceo(args: argparse.Namespace) -> int:
    username = (args.username or input("CEO username: ")).strip().lower()
    full_name = args.full_name or input("Full name: ").strip()
    pw = getpass.getpass("Password: ")
    if pw != getpass.getpass("Repeat password: "):
        print("Passwords do not match.", file=sys.stderr)
        return 1
    problems = password_problems(pw)
    if problems:
        print("Password " + "; ".join(problems) + ".", file=sys.stderr)
        return 1
    with get_session_factory()() as db:
        seed_roles(db)
        if db.scalar(select(User.id).where(User.username == username)):
            print("That username already exists.", file=sys.stderr)
            return 1
        ceo = db.scalar(select(Role).where(Role.code == "ceo"))
        user = User(username=username, full_name=full_name, password_hash=hash_password(pw), role_id=ceo.id)
        db.add(user)
        db.flush()
        audit_service.record(db, actor=None, actor_username="cli", action="user.created",
                             entity_type="user", entity_id=user.id, after={"username": username, "role": "ceo"})
        db.commit()
    print(f"CEO account '{username}' created.")
    return 0


DEMO_LOCATIONS = [
    ("DEMO-WH", "Demo Warehouse (synthetic)", "Demo City A", "both"),
    ("DEMO-B1", "Demo Branch 1 (synthetic)", "Demo City A", "branch"),
    ("DEMO-B2", "Demo Branch 2 (synthetic)", "Demo City A", "branch"),
    ("DEMO-B3", "Demo Branch 3 (synthetic)", "Demo City B", "branch"),
    ("DEMO-B4", "Demo Branch 4 (synthetic)", "Demo City C", "branch"),
]


def cmd_seed_demo(_: argparse.Namespace) -> int:
    s = get_settings()
    if s.is_production:
        print("Refusing to seed demo data when ENVIRONMENT=production.", file=sys.stderr)
        return 1
    created: list[tuple[str, str]] = []
    with get_session_factory()() as db:
        seed_roles(db)
        locs = {}
        for code, name, city, kind in DEMO_LOCATIONS:
            loc = db.scalar(select(Location).where(Location.code == code))
            if loc is None:
                loc = Location(code=code, name=name, city=city, location_type=kind)
                db.add(loc)
                db.flush()
            locs[code] = loc
        roles = {r.code: r for r in db.scalars(select(Role)).all()}
        demo_users = [
            ("demo.ceo", "Demo CEO", "ceo", []),
            ("demo.manager", "Demo Manager", "manager", []),
            ("demo.stock", "Demo Stock Keeper", "stock_keeper", ["DEMO-WH", "DEMO-B1"]),
            ("demo.booker", "Demo Order Booker", "order_booker", ["DEMO-B1"]),
            ("demo.accounts", "Demo Accounts", "accounts", []),
        ]
        for uname, full, role, loc_codes in demo_users:
            if db.scalar(select(User.id).where(User.username == uname)):
                continue
            pw = secrets.token_urlsafe(12) + "aA1"
            u = User(username=uname, full_name=full, password_hash=hash_password(pw), role_id=roles[role].id)
            u.location_links = [UserLocation(location_id=locs[c].id) for c in loc_codes]
            db.add(u)
            created.append((uname, pw))
        db.commit()
    print("DEMO DATA ONLY - synthetic locations and users. Never use in production.")
    for uname, pw in created:
        print(f"  {uname}  {pw}")
    if not created:
        print("  (demo users already existed; passwords are not re-displayed)")
    return 0


DEMO_PRODUCTS = [
    # code, name, brand, category, batches/expiry, [(sku, variant, hex, size, retail, cost)]
    ("DEMO-LIP", "Demo Velvet Lipstick", "Demo Brand", "Lips", False,
     [("DEMO-LIP-01", "Shade One", "#B76E79", "3.5g", "1200", "480"), ("DEMO-LIP-02", "Shade Two", "#8E3B46", "3.5g", "1200", "480"),
      ("DEMO-LIP-03", "Shade Three", "#D8A39D", "3.5g", "1200", "480")]),
    ("DEMO-FDN", "Demo Skin Foundation", "Demo Brand", "Face", True,
     [("DEMO-FDN-10", "Light", "#F1D0B5", "30ml", "2400", "950"), ("DEMO-FDN-20", "Medium", "#D9A982", "30ml", "2400", "950")]),
    ("DEMO-MSC", "Demo Lash Mascara", "Demo Brand", "Eyes", True, [("DEMO-MSC-BK", "Black", "#1E1A1A", "9ml", "1500", "600")]),
]


def cmd_seed_demo_catalogue(_: argparse.Namespace) -> int:
    """Synthetic products, opening stock and costs. Only allowed when ENVIRONMENT=demo."""
    s = get_settings()
    if s.environment != "demo":
        print("Refusing: demo products and stock are only created when ENVIRONMENT=demo.", file=sys.stderr)
        return 1
    from datetime import date, timedelta
    from backend.models import Category, Product
    from backend.services import catalog_service, inventory_service
    with get_session_factory()() as db:
        ceo = db.scalar(select(User).where(User.username == "demo.ceo"))
        if ceo is None:
            print("Run seed-demo first.", file=sys.stderr)
            return 1
        if db.scalar(select(Product.id).where(Product.code == "DEMO-LIP")):
            print("Demo catalogue already exists.")
            return 0
        variants = {}
        for code, name, brand, cat, tracked, vs in DEMO_PRODUCTS:
            c = catalog_service.get_or_create_category(db, ceo, cat, None)
            p = catalog_service.create_product(db, actor=ceo, can_price=True, ip=None, data={
                "code": code, "name": name, "brand": brand, "category_id": c.id, "tracks_batches": tracked,
                "tracks_expiry": tracked, "variants": [
                    {"sku": sku, "name": f"{vn} {size}", "shade_name": vn, "shade_hex": hx, "size": size,
                     "retail_price": retail, "employee_price": str(float(retail) * 0.8), "distributor_price": str(float(retail) * 0.7),
                     "purchase_cost": cost, "reorder_threshold": 10} for sku, vn, hx, size, retail, cost in vs]})
            for v in p.variants:
                variants[v.sku] = v
        wh = db.scalar(select(Location).where(Location.code == "DEMO-WH"))
        b1 = db.scalar(select(Location).where(Location.code == "DEMO-B1"))
        lines = []
        for sku, v in variants.items():
            batched = v.product.tracks_batches
            for loc, qty in ((wh, 60), (b1, 12)):
                lines.append({"location_id": loc.id, "variant_id": v.id, "qty": qty, "unit_cost": v.purchase_cost,
                              "batch_no": "DEMO-B001" if batched else None,
                              "expiry_date": date.today() + timedelta(days=400) if batched else None})
        res = inventory_service.opening_stock(db, actor=ceo, lines=lines, dry_run=False, idempotency_key="demo-open", ip=None)
        if not res["ok"]:
            db.rollback()
            print(res["errors"], file=sys.stderr)
            return 1
        db.commit()
    print("DEMO DATA ONLY - synthetic products, opening stock and costs.")
    return 0


def readiness_checks(backup_dir: str | None = None, max_backup_age_days: int = 2) -> list[dict]:
    """Read-only checks to run before a pilot or a location joins. Each is {name, ok, blocking, detail}. Never prints secrets."""
    import os
    from datetime import datetime, timedelta
    from pathlib import Path
    from backend.models import Account
    from backend.services import accounting_ops_service as ops
    s = get_settings()
    out: list[dict] = []

    def add(name, ok, detail, blocking=True):
        out.append({"name": name, "ok": bool(ok), "blocking": blocking, "detail": detail})
    add("ENVIRONMENT is production", s.is_production, f"currently '{s.environment}'")
    add("JWT secret is not a placeholder", len(s.jwt_secret) >= 32 and "change" not in s.jwt_secret.lower() and len(set(s.jwt_secret)) > 8,
        "length and variety checked; value not shown")
    with get_session_factory()() as db:
        demo = db.scalars(select(User.username).where(User.username.like("demo.%"))).all()
        add("No demo users exist", not demo, f"{len(demo)} demo user(s) found" if demo else "none")
        demo_loc = db.scalars(select(Location.code).where(Location.code.like("DEMO-%"))).all()
        add("No demo locations exist", not demo_loc, f"{len(demo_loc)} found" if demo_loc else "none")
        ceos = db.scalars(select(User).join(Role).where(Role.code == "ceo", User.is_active.is_(True))).all()
        add("At least one active CEO account", len(ceos) >= 1, f"{len(ceos)} active")
        locs = db.scalars(select(Location).where(Location.is_active.is_(True))).all()
        add("Locations are configured", len(locs) >= 1, f"{len(locs)} active location(s)")
        if db.scalar(select(func.count()).select_from(Account)):
            chk = ops.go_live_checks(db)
            for c in chk["checks"]:
                add("Ledger: " + c["name"], c["ok"], c["detail"], blocking=False)
            add("Ledger is declared official", chk["ledger_status"] == "official", f"status '{chk['ledger_status']}'", blocking=False)
        else:
            add("Chart of accounts exists", False, "no accounts; run the accounting set-up", blocking=False)
    d = Path(backup_dir or os.environ.get("BACKUP_DIR", "backups"))
    newest = None
    if d.is_dir():
        for f in d.iterdir():
            if f.is_dir() or f.suffix in (".enc", ".zip", ".backup"):
                m = datetime.fromtimestamp(f.stat().st_mtime)
                newest = m if newest is None or m > newest else newest
    fresh = newest is not None and datetime.now() - newest <= timedelta(days=max_backup_age_days)
    add("A backup newer than %d day(s) exists" % max_backup_age_days, fresh, f"newest {newest:%Y-%m-%d %H:%M}" if newest else f"none found in {d}")
    add("Backup passphrase is set in the environment", bool(os.environ.get("BACKUP_PASSPHRASE")), "set" if os.environ.get("BACKUP_PASSPHRASE") else "not set (needed to take and restore encrypted backups)", blocking=False)
    return out


def cmd_readiness(args: argparse.Namespace) -> int:
    rows = readiness_checks(args.backup_dir, args.max_backup_age_days)
    for r in rows:
        mark = "PASS" if r["ok"] else ("FAIL" if r["blocking"] else "WARN")
        print(f"[{mark}] {r['name']}: {r['detail']}")
    blockers = [r for r in rows if not r["ok"] and r["blocking"]]
    print(f"\n{len(blockers)} blocking problem(s), {sum(1 for r in rows if not r['ok'] and not r['blocking'])} warning(s).")
    return 1 if blockers else 0


def main() -> int:
    parser = argparse.ArgumentParser(prog="backend.cli")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("init").set_defaults(fn=cmd_init)
    c = sub.add_parser("create-ceo")
    c.add_argument("--username")
    c.add_argument("--full-name")
    c.set_defaults(fn=cmd_create_ceo)
    sub.add_parser("seed-demo").set_defaults(fn=cmd_seed_demo)
    sub.add_parser("seed-demo-catalogue").set_defaults(fn=cmd_seed_demo_catalogue)
    r = sub.add_parser("readiness")
    r.add_argument("--backup-dir")
    r.add_argument("--max-backup-age-days", type=int, default=2)
    r.set_defaults(fn=cmd_readiness)
    args = parser.parse_args()
    return args.fn(args)


if __name__ == "__main__":
    raise SystemExit(main())
