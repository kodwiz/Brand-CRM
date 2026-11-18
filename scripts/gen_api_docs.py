"""Generate docs/api.md from the application's own OpenAPI schema, with each route's required permissions from the dependency tags.

    python scripts/gen_api_docs.py
"""
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ.setdefault("JWT_SECRET", "x" * 40)
os.environ.setdefault("DATABASE_URL", "postgresql+psycopg://x:x@localhost/x")
from backend.main import app  # noqa: E402


def walk(routes):
    for r in routes:
        inner = getattr(r, "original_router", None)
        if inner is not None:
            yield from walk(inner.routes)
        else:
            yield r


def perms(route) -> str:
    out = []
    for d in route.dependant.dependencies:
        c = d.call
        if hasattr(c, "required"):
            out.append((" and " if c.mode == "all" else " or ").join(c.required))
    return "; ".join(out) or ("login only" if any(getattr(d.call, "__name__", "") == "current_principal" for d in route.dependant.dependencies) else "public")


def main() -> None:
    schema = app.openapi()
    rows: dict[str, list] = {}
    for r in walk(app.routes):
        if not hasattr(r, "methods") or not getattr(r, "include_in_schema", True):
            continue
        tag = (r.tags[0] if r.tags else r.path.strip("/").split("/")[0] or "root")
        for m in sorted(r.methods - {"HEAD", "OPTIONS"}):
            op = schema["paths"].get(r.path, {}).get(m.lower(), {})
            rows.setdefault(tag, []).append((r.path, m, perms(r), (op.get("summary") or "").replace("|", "/")))
    n = sum(len(v) for v in rows.values())
    lines = ["# API reference", "",
             f"Generated from the running application by `python scripts/gen_api_docs.py` ({n} operations). Do not edit by hand; regenerate after changing routes.", "",
             "* Interactive documentation (Swagger UI) is at `/docs` and the machine-readable schema at `/openapi.json` **when ENVIRONMENT is not production**; both are disabled in production.",
             "* Authenticate with `POST /auth/login` (`{username, password}`) and send `Authorization: Bearer <token>` on every call.",
             "* **Permission** column: `a and b` means ALL are required, `a or b` means ANY. `login only` means any signed-in user (further filtering is by location scope inside the handler). The server enforces these; the desktop hiding a button is a convenience only.",
             "* Errors: 401 not signed in · 403 permission or separation-of-duties refusal · 404 not found **or outside your locations** · 409 conflict (already done, duplicate) · 422 a business rule or invalid input.",
             "* Money is sent and returned as decimal strings. Dates are ISO `YYYY-MM-DD`.", ""]
    for tag in sorted(rows):
        lines += [f"## {tag}", "", "| Method | Path | Permission | Summary |", "|---|---|---|---|"]
        for path, m, pm, sm in sorted(rows[tag]):
            lines.append(f"| {m} | `{path}` | {pm} | {sm} |")
        lines.append("")
    (ROOT / "docs" / "api.md").write_text("\n".join(lines), encoding="utf-8")
    print(f"written {n} operations in {len(rows)} groups")


if __name__ == "__main__":
    main()
