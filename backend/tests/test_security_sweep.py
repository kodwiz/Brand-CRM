"""Phase 9: security and permission sweep over EVERY route of the running application, so a new endpoint cannot ship unprotected.

1. Static: every route is either on the short public list or depends on authentication; every permission a route names is a real catalogued permission.
2. Dynamic: for every role and every route, a role that lacks the required permissions gets exactly 403; a role that holds them is never refused for permission.
   Only read-only routes are called as the permitted role (mutating routes are only called as the refused role, where nothing can change)."""
import re

import pytest
from fastapi.routing import APIRoute

from backend.deps import current_principal
from backend.main import app
from backend.permissions import DEFAULT_ROLES, PERMISSIONS

PUBLIC = {("POST", "/auth/login"), ("GET", "/health"), ("GET", "/openapi.json"), ("GET", "/docs"), ("GET", "/redoc"), ("GET", "/docs/oauth2-redirect"),
          ("POST", "/auth/password-reset"), ("POST", "/auth/password-reset/confirm")}


def _walk(rs):
    """Newer FastAPI keeps included routers lazily as _IncludedRouter; walk into them. (No router here adds its own prefix or dependencies at include time:
    the paths and dependencies on each route are the real ones. test_the_walk_finds_every_documented_path guards that assumption.)"""
    for r in rs:
        inner = getattr(r, "original_router", None)
        if inner is not None:
            yield from _walk(inner.routes)
        elif isinstance(r, APIRoute):
            yield r


def routes():
    for r in _walk(app.routes):
        for m in r.methods - {"HEAD", "OPTIONS"}:
            yield m, r.path, r


def deps_of(route: APIRoute):
    """All dependency callables beneath a route, depth first."""
    out, stack = [], list(route.dependant.dependencies)
    while stack:
        d = stack.pop()
        out.append(d.call)
        stack.extend(d.dependencies)
    return out


def requirements(route: APIRoute):
    return [(c.required, c.mode) for c in deps_of(route) if hasattr(c, "required")]


def authenticated(route: APIRoute) -> bool:
    return any(c is current_principal for c in deps_of(route))


def allowed(perms: set[str], reqs) -> bool:
    return all((all(p in perms for p in req) if mode == "all" else any(p in perms for p in req)) for req, mode in reqs)


def fill(path: str) -> str:
    return re.sub(r"\{[^}]+\}", "1", path)


ALL = sorted(routes(), key=lambda x: (x[1], x[0]))


def test_there_are_routes_to_check():
    assert len(ALL) > 250


def test_the_walk_finds_every_documented_path():
    """The OpenAPI document is built by the framework itself; if it lists a method+path the walk missed, the sweep would be silently incomplete."""
    doc = {(m.upper(), p) for p, ops in app.openapi()["paths"].items() for m in ops}
    mine = {(m, p) for m, p, _ in ALL}
    assert doc <= mine, sorted(doc - mine)[:10]


def test_every_route_is_public_by_name_or_requires_authentication():
    open_ones = [(m, p) for m, p, r in ALL if (m, p) not in PUBLIC and not authenticated(r)]
    assert not open_ones, f"routes that do not require a login: {open_ones}"


def test_the_public_list_is_really_public_and_short():
    names = {(m, p) for m, p, _ in ALL}
    assert PUBLIC & names, "the public list names routes that exist"
    assert len({x for x in PUBLIC if x in names and x[1] not in ("/docs", "/redoc", "/openapi.json", "/docs/oauth2-redirect", "/health")}) <= 3


def test_every_permission_a_route_names_is_a_real_permission():
    bad = sorted({(p, path) for m, path, r in ALL for req, _ in requirements(r) for p in req if p not in PERMISSIONS})
    assert not bad, f"routes naming permissions that do not exist (nobody can ever pass these): {bad}"


def test_every_default_role_permission_is_catalogued():
    for role, spec in DEFAULT_ROLES.items():
        assert set(spec["permissions"]) <= set(PERMISSIONS), role


def test_no_route_in_a_state_changing_method_is_protected_only_by_login():
    """A POST/PUT/PATCH/DELETE that anyone logged in may call must be a deliberate choice, so the list is explicit."""
    SELF_SERVICE = {("POST", "/auth/logout"), ("POST", "/auth/change-password"), ("PUT", "/auth/me"), ("PATCH", "/auth/me")}
    weak = [(m, p) for m, p, r in ALL if m in ("POST", "PUT", "PATCH", "DELETE") and (m, p) not in PUBLIC and authenticated(r) and not requirements(r)]
    unexpected = [w for w in weak if w not in SELF_SERVICE]
    assert not unexpected, f"state-changing routes that need only a login: {unexpected}"


@pytest.mark.parametrize("role", ["stock", "booker", "accounts", "manager", "ceo"])
def test_every_route_answers_403_exactly_when_the_role_lacks_the_permission(client, auth, role):
    perms = set(DEFAULT_ROLES[{"stock": "stock_keeper", "booker": "order_booker", "accounts": "accounts", "manager": "manager", "ceo": "ceo"}[role]]["permissions"])
    h = auth[role]
    wrong, server_errors, checked = [], [], 0
    for m, path, r in ALL:
        reqs = requirements(r)
        if (m, path) in PUBLIC or not reqs or "logout" in path:
            continue
        ok = allowed(perms, reqs)
        if ok and m != "GET":
            continue                                   # never run a state-changing call that would really be permitted
        resp = client.request(m, fill(path), headers=h, json={} if m != "GET" else None)
        checked += 1
        if not ok and resp.status_code != 403:
            wrong.append((m, path, resp.status_code, "should be 403"))
        if ok and resp.status_code in (401, 403):
            wrong.append((m, path, resp.status_code, "refused although permitted"))
        if resp.status_code >= 500:
            server_errors.append((m, path, resp.status_code))
    assert checked > (100 if role == "ceo" else 150)      # the CEO holds everything, so only read-only routes are called
    assert not wrong, wrong[:15]
    assert not server_errors, server_errors[:15]


def test_a_bad_expired_or_missing_token_never_gets_in(client, auth):
    for headers in ({}, {"Authorization": "Bearer nonsense"}, {"Authorization": "Basic abc"}, {"Authorization": "Bearer "}):
        for m, path, r in ALL[:: max(1, len(ALL) // 40)]:
            if (m, path) in PUBLIC:
                continue
            assert client.request(m, fill(path), headers=headers).status_code in (401, 403), (m, path, headers)


def test_error_responses_do_not_expose_internals(client, auth):
    r = client.get("/orders/not-a-number", headers=auth["ceo"])
    assert r.status_code == 422
    for needle in ("Traceback", "sqlalchemy", "psycopg", "/home/", ".py\""):
        assert needle not in r.text
    r = client.get("/this-route-does-not-exist", headers=auth["ceo"])
    assert r.status_code == 404 and "Traceback" not in r.text


def test_login_is_rate_limited_and_responses_do_not_reveal_which_part_was_wrong(client, users):
    a = client.post("/auth/login", json={"username": "ceo", "password": "wrong-password-1!"})
    b = client.post("/auth/login", json={"username": "no-such-user", "password": "wrong-password-1!"})
    assert a.status_code == b.status_code == 401 and a.json() == b.json()
    codes = [client.post("/auth/login", json={"username": "ceo", "password": f"wrong-password-{i}!"}).status_code for i in range(30)]
    assert 429 in codes, "repeated bad logins must be throttled"
