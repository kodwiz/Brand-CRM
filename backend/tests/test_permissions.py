import pytest
from sqlalchemy import select

from backend.models import AuditLog, Role
from backend.permissions import DEFAULT_ROLES, PERMISSIONS
from backend.tests.conftest import login


def test_default_matrix_only_uses_catalogued_permissions():
    for code, spec in DEFAULT_ROLES.items():
        unknown = spec["permissions"] - set(PERMISSIONS)
        assert not unknown, f"{code} references unknown permissions {unknown}"


def test_matrix_separation_of_duties():
    # Order bookers never see financials, attendance or distributor ledgers.
    booker = DEFAULT_ROLES["order_booker"]["permissions"]
    assert not {p for p in booker if p.startswith(("accounting.", "attendance.", "distributors.", "reports.financial"))}
    # Stock keepers cannot approve their own exceptional adjustments.
    stock = DEFAULT_ROLES["stock_keeper"]["permissions"]
    assert not {"inventory.writeoff_approve", "distributors.approve", "approvals.decide"} & stock
    # Accounts staff see what employees owe (purchases and payments) but have no attendance or personnel-record access.
    acct = DEFAULT_ROLES["accounts"]["permissions"]
    assert not {p for p in acct if p.startswith("attendance.")}
    assert not {"employees.view", "employees.manage", "employees.purchase", "employees.purchase_approve"} & acct
    # Only the CEO can reopen a closed period or edit role permissions by default.
    for code in ("manager", "stock_keeper", "order_booker", "accounts"):
        assert "accounting.reopen_period" not in DEFAULT_ROLES[code]["permissions"]
        assert "roles.manage" not in DEFAULT_ROLES[code]["permissions"]


def test_manager_default_restrictions():
    """Managers can oversee and approve stock matters, but by default cannot touch the books, periods, users or roles."""
    m = DEFAULT_ROLES["manager"]["permissions"]
    forbidden = {"accounting.post", "accounting.manage_coa", "accounting.close_period", "accounting.reopen_period",
                 "users.manage", "roles.manage", "settings.manage", "inventory.adjust", "inventory.opening_stock"}
    assert not forbidden & m
    # what they should see
    assert {"dashboard.executive", "inventory.view", "inventory.view_valuation", "attendance.view", "reports.summaries",
            "distributors.view", "suppliers.view", "approvals.view"} <= m


def test_manager_cannot_self_approve_or_administer(client, auth, catalog, locations):
    """Enforced by the backend, not the UI: a manager who is also the requester is refused (needs a permission to even try)."""
    assert client.put("/inventory/settings/costing", headers=auth["manager"], json={"method": "weighted_average"}).status_code == 403
    assert client.post("/inventory/opening-stock", headers=auth["manager"],
                       json={"dry_run": True, "lines": [{"location_id": locations["WH"].id, "variant_id": catalog["rose"],
                                                         "qty": 1, "unit_cost": "1"}]}).status_code == 403
    assert client.post("/inventory/requests", headers=auth["manager"], json={"kind": "adjustment",
        "location_id": locations["WH"].id, "reason": "x", "lines": [{"variant_id": catalog["rose"], "qty_delta": 1}]}).status_code == 403
    assert client.get("/inventory/valuation", headers=auth["manager"]).status_code == 200
    assert client.get("/inventory/integrity", headers=auth["manager"]).status_code == 403


@pytest.mark.parametrize(
    "who, method, path, expected",
    [
        ("booker", "get", "/users", 403),
        ("booker", "get", "/audit", 403),
        ("booker", "post", "/locations", 403),
        ("stock", "get", "/users", 403),
        ("stock", "get", "/roles", 403),
        ("accounts", "get", "/audit", 403),
        ("manager", "post", "/users", 403),          # manager cannot administer users
        ("manager", "get", "/audit", 200),
        ("manager", "get", "/users", 200),
        ("ceo", "get", "/users", 200),
        ("ceo", "get", "/audit", 200),
        ("ceo", "get", "/roles/permission-catalogue", 200),
        ("manager", "get", "/roles/permission-catalogue", 403),
    ],
)
def test_endpoint_permissions_enforced_server_side(client, users, who, method, path, expected):
    h = login(client, who)
    kwargs = {"json": {}} if method == "post" else {}
    assert getattr(client, method)(path, headers=h, **kwargs).status_code == expected


# ---- location scoping ------------------------------------------------------------
def test_scoped_user_only_sees_assigned_locations(client, users, locations):
    codes = lambda who: sorted(l["code"] for l in client.get("/locations", headers=login(client, who)).json())
    assert codes("ceo") == ["B1", "B2", "WH"]
    assert codes("manager") == ["B1", "B2", "WH"]
    assert codes("stock") == ["B1", "WH"]
    assert codes("booker") == ["B1"]


def test_user_cannot_modify_location_without_permission(client, users, locations):
    r = client.patch(f"/locations/{locations['B2'].id}", headers=login(client, "stock"), json={"name": "Hacked"})
    assert r.status_code == 403


def test_out_of_scope_location_not_confirmed_to_exist(client, users, locations, db):
    # Grant a scoped role locations.manage: it still may not touch an unassigned location.
    role = db.scalar(select(Role).where(Role.code == "stock_keeper"))
    from backend.models import RolePermission

    role.permissions.append(RolePermission(role_id=role.id, permission="locations.manage"))
    db.commit()
    h = login(client, "stock")
    assert client.patch(f"/locations/{locations['B2'].id}", headers=h, json={"name": "x"}).status_code == 404
    assert client.patch(f"/locations/{locations['B1'].id}", headers=h, json={"name": "Renamed"}).status_code == 200


def test_scoped_role_requires_location_assignment(client, users, locations):
    h = login(client, "ceo")
    role_id = next(r["id"] for r in client.get("/roles", headers=h).json() if r["code"] == "stock_keeper")
    body = {"username": "newstock", "full_name": "New Stock", "password": "Str0ngPassw0rd!", "role_id": role_id, "location_ids": []}
    assert client.post("/users", headers=h, json=body).status_code == 422
    body["location_ids"] = [locations["B2"].id]
    r = client.post("/users", headers=h, json=body)
    assert r.status_code == 201
    assert r.json()["location_ids"] == [locations["B2"].id]
    assert r.json()["must_change_password"] is True


def test_unknown_location_rejected(client, users):
    h = login(client, "ceo")
    r = client.put(f"/users/{users['stock'].id}/locations", headers=h, json={"location_ids": [9999]})
    assert r.status_code == 422


def test_reassigning_locations_changes_visibility_immediately(client, users, locations):
    ceo = login(client, "ceo")
    stock = login(client, "stock")
    assert len(client.get("/locations", headers=stock).json()) == 2
    r = client.put(f"/users/{users['stock'].id}/locations", headers=ceo,
                   json={"location_ids": [locations["B2"].id], "reason": "transferred"})
    assert r.status_code == 200
    assert [l["code"] for l in client.get("/locations", headers=stock).json()] == ["B2"]


# ---- role administration ---------------------------------------------------------
def test_ceo_can_edit_role_permissions_and_it_takes_effect(client, users):
    ceo = login(client, "ceo")
    booker = login(client, "booker")
    assert client.get("/users", headers=booker).status_code == 403
    roles = {r["code"]: r for r in client.get("/roles", headers=ceo).json()}
    perms = roles["order_booker"]["permissions"] + ["users.view"]
    r = client.put(f"/roles/{roles['order_booker']['id']}/permissions", headers=ceo,
                   json={"permissions": perms, "reason": "pilot"})
    assert r.status_code == 200
    assert client.get("/users", headers=booker).status_code == 200


def test_unknown_permission_rejected(client, users):
    ceo = login(client, "ceo")
    rid = next(r["id"] for r in client.get("/roles", headers=ceo).json() if r["code"] == "manager")
    assert client.put(f"/roles/{rid}/permissions", headers=ceo, json={"permissions": ["made.up"]}).status_code == 422


def test_ceo_cannot_lock_company_out_of_administration(client, users):
    ceo = login(client, "ceo")
    rid = next(r["id"] for r in client.get("/roles", headers=ceo).json() if r["code"] == "ceo")
    r = client.put(f"/roles/{rid}/permissions", headers=ceo, json={"permissions": ["users.view"]})
    assert r.status_code == 422


def test_cannot_deactivate_or_demote_self(client, users):
    ceo = login(client, "ceo")
    assert client.patch(f"/users/{users['ceo'].id}", headers=ceo, json={"is_active": False}).status_code == 422
    mgr_role = next(r["id"] for r in client.get("/roles", headers=ceo).json() if r["code"] == "manager")
    assert client.patch(f"/users/{users['ceo'].id}", headers=ceo, json={"role_id": mgr_role}).status_code == 422


def test_role_change_revokes_sessions(client, users):
    ceo = login(client, "ceo")
    booker = login(client, "booker")
    mgr_role = next(r["id"] for r in client.get("/roles", headers=ceo).json() if r["code"] == "manager")
    assert client.patch(f"/users/{users['booker'].id}", headers=ceo, json={"role_id": mgr_role}).status_code == 200
    assert client.get("/auth/me", headers=booker).status_code == 401


def test_duplicate_username_and_location_code_conflict(client, users, locations):
    ceo = login(client, "ceo")
    role_id = next(r["id"] for r in client.get("/roles", headers=ceo).json() if r["code"] == "accounts")
    body = {"username": "CEO", "full_name": "Dup", "password": "Str0ngPassw0rd!", "role_id": role_id}
    assert client.post("/users", headers=ceo, json=body).status_code == 409
    loc = {"code": "wh", "name": "Dup", "city": "X"}
    assert client.post("/locations", headers=ceo, json=loc).status_code == 409


def test_invalid_location_type_rejected(client, users):
    ceo = login(client, "ceo")
    assert client.post("/locations", headers=ceo, json={"code": "ZZ", "name": "n", "city": "c", "location_type": "kiosk"}).status_code == 422


def test_admin_password_reset_forces_change_and_revokes(client, users):
    ceo = login(client, "ceo")
    booker = login(client, "booker")
    r = client.post(f"/users/{users['booker'].id}/reset-password", headers=ceo, json={"new_password": "Reset1234Pass"})
    assert r.status_code == 204
    assert client.get("/auth/me", headers=booker).status_code == 401
    t = client.post("/auth/login", json={"username": "booker", "password": "Reset1234Pass"})
    assert t.status_code == 200 and t.json()["must_change_password"] is True


# ---- audit -----------------------------------------------------------------------
def test_admin_actions_write_audit_with_before_after(client, users, locations, db):
    ceo = login(client, "ceo")
    client.patch(f"/locations/{locations['B1'].id}", headers=ceo, json={"name": "Renamed B1", "reason": "rebrand"})
    entry = db.scalar(select(AuditLog).where(AuditLog.action == "location.updated"))
    assert entry.actor_username == "ceo"
    assert entry.reason == "rebrand"
    assert entry.before["name"] == "Test B1" and entry.after["name"] == "Renamed B1"
    assert entry.location_id == locations["B1"].id


def test_audit_filtering_and_pagination(client, users, locations):
    ceo = login(client, "ceo")
    for i in range(3):
        client.patch(f"/locations/{locations['B1'].id}", headers=ceo, json={"name": f"N{i}"})
    r = client.get("/audit", headers=ceo, params={"action": "location.updated", "page_size": 2}).json()
    assert r["total"] == 3 and len(r["items"]) == 2
    r2 = client.get("/audit", headers=ceo, params={"entity_type": "location", "entity_id": str(locations["B1"].id)}).json()
    assert r2["total"] == 3


def test_failed_service_call_rolls_back_change_and_audit(client, users, db):
    ceo = login(client, "ceo")
    before = db.query(AuditLog).filter_by(action="user.updated").count()
    r = client.patch(f"/users/{users['ceo'].id}", headers=ceo, json={"is_active": False, "full_name": "Changed"})
    assert r.status_code == 422
    db.expire_all()
    assert db.query(AuditLog).filter_by(action="user.updated").count() == before
    assert db.get(type(users["ceo"]), users["ceo"].id).full_name != "Changed"
