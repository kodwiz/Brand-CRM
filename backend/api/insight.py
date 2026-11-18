"""Dashboards, the report catalogue with Excel/PDF export, and the approvals inbox (router prefix /insight).

What a user can open is decided here and in the services from their permissions and locations, never from the screen: a report they may not run answers 404,
as if it did not exist. Every export is written to the audit log with its filters and row count."""
from __future__ import annotations

from datetime import date

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from sqlalchemy.orm import Session

from backend.api.common import commit_or_http
from backend.db import get_db
from backend.deps import Principal, client_ip, current_principal, require
from backend.services import approvals_service, audit_service, dashboard_service, export_service, report_service
from backend.services.errors import BusinessRuleError, NotFoundError
from backend.services.scope import visible_location_ids

router = APIRouter(prefix="/insight", tags=["insight"])

TYPES = {"xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", "pdf": "application/pdf"}


def _guard(fn):
    try:
        return fn()
    except NotFoundError as e:
        raise HTTPException(404, str(e))
    except BusinessRuleError as e:
        raise HTTPException(422, str(e))


@router.get("/dashboard")
def dashboard(date_from: date | None = None, date_to: date | None = None, location_id: int | None = None, p: Principal = Depends(current_principal), db: Session = Depends(get_db)):
    out = _guard(lambda: dashboard_service.build(db, p.user, date_from=date_from, date_to=date_to, location_id=location_id))
    db.commit()                       # building the chart of accounts on first use is the only write
    return out


@router.get("/approvals")
def approvals(p: Principal = Depends(require("approvals.view")), db: Session = Depends(get_db)):
    rows = approvals_service.inbox(db, p.user)
    return {"items": rows, "counts": approvals_service.counts(db, p.user)}


@router.get("/reports")
def catalogue(p: Principal = Depends(current_principal)):
    cat = report_service.available(p.user)
    can_export = p.has("reports.export")
    return {"items": [{**r, "needs": list(r["needs"]), "params": list(r["params"])} for r in cat], "can_export": can_export}


def _params(date_from, date_to, as_of, location_id, group, order, status, q, days, party_type, party_id, action, actor, entity_type) -> dict:
    return {k: v for k, v in dict(date_from=date_from, date_to=date_to, as_of=as_of, location_id=location_id, group=group, order=order, status=status, q=q, days=days, party_type=party_type,
                                  party_id=party_id, action=action, actor=actor, entity_type=entity_type).items() if v not in (None, "")}


Q = None


@router.get("/reports/{key}")
def run_report(key: str, date_from: date | None = Q, date_to: date | None = Q, as_of: date | None = Q, location_id: int | None = Q, group: str | None = Q, order: str | None = Q,
               status: str | None = Q, q: str | None = Q, days: int | None = Query(None, ge=1, le=730), party_type: str | None = Q, party_id: int | None = Q, action: str | None = Q,
               actor: str | None = Q, entity_type: str | None = Q, p: Principal = Depends(current_principal), db: Session = Depends(get_db)):
    rep = _guard(lambda: report_service.run(db, p.user, key, _params(date_from, date_to, as_of, location_id, group, order, status, q, days, party_type, party_id, action, actor, entity_type)))
    db.commit()
    return rep


@router.get("/reports/{key}/export")
def export_report(key: str, request: Request, format: str = Query("xlsx", pattern="^(xlsx|pdf)$"), date_from: date | None = Q, date_to: date | None = Q, as_of: date | None = Q,
                  location_id: int | None = Q, group: str | None = Q, order: str | None = Q, status: str | None = Q, q: str | None = Q, days: int | None = Query(None, ge=1, le=730),
                  party_type: str | None = Q, party_id: int | None = Q, action: str | None = Q, actor: str | None = Q, entity_type: str | None = Q,
                  p: Principal = Depends(require("reports.export")), db: Session = Depends(get_db)):
    params = _params(date_from, date_to, as_of, location_id, group, order, status, q, days, party_type, party_id, action, actor, entity_type)
    rep = _guard(lambda: report_service.run(db, p.user, key, params))
    rep["filters"] = {k: str(v) for k, v in params.items() if k not in ("date_from", "date_to", "as_of")}
    data = _guard(lambda: export_service.to_xlsx(rep) if format == "xlsx" else export_service.to_pdf(rep))
    commit_or_http(db, lambda: audit_service.record(db, actor=p.user, action="report.export", entity_type="report", entity_id=key, location_id=location_id,
                                                    after={"report": rep["title"], "format": format, "rows": len(rep["rows"]), "filters": {k: str(v) for k, v in params.items()}}, ip=client_ip(request)))
    name = export_service.filename(rep, format)
    return Response(content=data, media_type=TYPES[format], headers={"Content-Disposition": f'attachment; filename="{name}"', "Cache-Control": "no-store"})
