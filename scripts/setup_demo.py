"""Build a SEPARATE demo database (cosmetics_demo) with synthetic locations, users, products, stock, customers and a handful of orders.
Works on Windows, macOS and Linux. Your real database (DATABASE_URL in .env) is never touched: this only ever drops and recreates a
database whose name ends in _demo.

    python scripts/setup_demo.py

The demo users' random passwords are printed once and saved to demo_credentials.txt (demo data only; delete the file any time).
Then run the backend against the demo database (see the commands printed at the end)."""
import logging
import os
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from dotenv import dotenv_values  # noqa: E402
from sqlalchemy import create_engine, text  # noqa: E402
from sqlalchemy.engine import make_url  # noqa: E402


def main() -> int:
    base = os.environ.get("DATABASE_URL") or dotenv_values(ROOT / ".env").get("DATABASE_URL")
    if not base:
        print("DATABASE_URL is not set in .env"); return 1
    u = make_url(base)
    demo = u.set(database="cosmetics_demo")
    assert demo.database.endswith("_demo")
    admin = create_engine(u.set(database="postgres").render_as_string(hide_password=False), isolation_level="AUTOCOMMIT")
    with admin.connect() as c:
        c.execute(text(f'DROP DATABASE IF EXISTS "{demo.database}" WITH (FORCE)'))
        c.execute(text(f'CREATE DATABASE "{demo.database}"'))
    print("Created empty database cosmetics_demo")
    url = demo.render_as_string(hide_password=False)
    env = {**os.environ, "DATABASE_URL": url, "ENVIRONMENT": "demo"}
    run = lambda *a: subprocess.run([sys.executable, *a], cwd=ROOT, env=env, check=True, capture_output=True, text=True).stdout  # noqa: E731
    run("-m", "alembic", "upgrade", "head"); print("Tables created")
    run("-m", "backend.cli", "init")
    seeded = run("-m", "backend.cli", "seed-demo")
    run("-m", "backend.cli", "seed-demo-catalogue"); print("Products and opening stock created")
    pw = dict(re.findall(r"^\s+(demo\.\w+)\s+(\S+)\s*$", seeded, re.M))
    if len(pw) < 5:
        print("Could not read the demo passwords from the seed output:\n" + seeded); return 1
    (ROOT / "demo_credentials.txt").write_text("DEMO DATA ONLY (synthetic). Safe to delete.\n" + "\n".join(f"{k}  {v}" for k, v in pw.items()) + "\n", encoding="utf-8")

    os.environ.update(env)
    os.environ.setdefault("JWT_SECRET", dotenv_values(ROOT / ".env").get("JWT_SECRET") or "x" * 40)
    from fastapi.testclient import TestClient
    from backend.main import app
    logging.disable(logging.INFO)
    cl = TestClient(app)

    def hdr(name):
        r = cl.post("/auth/login", json={"username": name, "password": pw[name]})
        assert r.status_code == 200, (name, r.text)
        return {"Authorization": f"Bearer {r.json()['access_token']}"}
    ceo, booker, stock, acc = hdr("demo.ceo"), hdr("demo.booker"), hdr("demo.stock"), hdr("demo.accounts")

    def ok(r, what):
        assert r.status_code in (200, 201), f"{what}: {r.status_code} {r.text}"
        return r.json()
    locs = {l["code"]: l["id"] for l in ok(cl.get("/locations", headers=ceo), "locations")}
    chan = ok(cl.get("/sales/channels", headers=booker), "channels")[0]["id"]
    skus = {}
    for item in ok(cl.get("/products", headers=ceo, params={"page_size": 100}), "products")["items"]:
        for v in ok(cl.get(f"/products/{item['id']}", headers=ceo), "product")["variants"]:
            skus[v["sku"]] = v["id"]
    from datetime import date
    today = date.today().isoformat()
    names = [("Ayesha Khan", "0300 1000001"), ("Sana Malik", "0300 1000002"), ("Hira Ahmed", "0300 1000003")]
    custs = [ok(cl.post("/customers", headers=booker, json={"name": n, "phone": p}), "customer")["id"] for n, p in names]

    def order(ci, lines, pay=None, ship=None):
        o = ok(cl.post("/orders", headers=booker, json={
            "customer_id": custs[ci], "channel_id": chan, "location_id": locs["DEMO-B1"], "order_date": today, "payment_terms": "cod",
            "delivery": {"name": names[ci][0], "phone": names[ci][1], "address": "Demo Street 1", "city": "Demo City A"},
            "lines": [{"variant_id": skus[s], "qty": q} for s, q in lines]}), "order")
        if o["status"] == "draft":
            o = ok(cl.post(f"/orders/{o['id']}/confirm", headers=booker), "confirm")
        if pay:
            ok(cl.post(f"/orders/{o['id']}/payments", headers=acc, json={"amount": str(pay if pay != "full" else o["total"]), "method": "cash"}), "payment")
        if ship:
            s = ok(cl.post(f"/orders/{o['id']}/shipments", headers=stock, json={"lines": [{"order_line_id": l["id"], "qty": l["to_ship"]} for l in o["lines"]]}), "shipment")
            ok(cl.post(f"/shipments/{s['id']}/dispatch", headers=stock, json={"courier": "Demo Courier", "tracking_no": "DEMO-TRK-1"}), "dispatch")
            if ship == "delivered":
                ok(cl.post(f"/shipments/{s['id']}/outcome", headers=stock, json={"result": "delivered"}), "delivery")
        return o
    order(0, [("DEMO-LIP-01", 2), ("DEMO-LIP-02", 1)], "full", "delivered")
    order(1, [("DEMO-FDN-10", 1)], "full", "delivered")
    order(2, [("DEMO-MSC-BK", 2)], 500, "delivered")
    order(0, [("DEMO-LIP-03", 3)], "full", "dispatched")
    order(1, [("DEMO-FDN-20", 1), ("DEMO-LIP-01", 1)])
    order(2, [("DEMO-LIP-02", 2)])
    ok(cl.post("/accounting/posting/run", headers=acc), "posting run")
    print("Created 3 customers and 6 orders (delivered, part-paid, dispatched, confirmed) and posted them to the ledger")
    print("\nDemo users (also saved in demo_credentials.txt):")
    for k, v in pw.items():
        print(f"  {k}  {v}")
    print("\nTo use the demo data, open .env and change these two lines (keep a note of the old values to switch back):")
    print("  DATABASE_URL=" + demo.render_as_string(hide_password=True).replace("***", "<same password as before>"))
    print("  ENVIRONMENT=demo")
    print("Then restart the backend (python -m uvicorn backend.main:app --host 127.0.0.1 --port 8000), start the desktop app and sign in as demo.ceo.")
    print("To go back to your own data: set DATABASE_URL back to cosmetics_dev and ENVIRONMENT=development.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
