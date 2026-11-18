import logging

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from backend.api import accounting, admin, auth, catalog, distributors, insight, inventory, people, purchasing, sales
from backend.config import get_settings

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("cosmetics.api")


def create_app() -> FastAPI:
    s = get_settings()
    app = FastAPI(
        title="Cosmetics Operations API",
        version="0.1.0",
        docs_url="/docs" if not s.is_production else None,
        redoc_url=None,
        openapi_url="/openapi.json" if not s.is_production else None,
    )
    app.include_router(auth.router)
    app.include_router(admin.router)
    app.include_router(catalog.router)
    app.include_router(inventory.router)
    app.include_router(purchasing.router)
    app.include_router(sales.router)
    app.include_router(distributors.router)
    app.include_router(accounting.router)
    app.include_router(people.router)
    app.include_router(people.att_router)
    app.include_router(insight.router)

    @app.get("/health", tags=["meta"])
    def health():
        return {"status": "ok", "environment": s.environment}

    @app.exception_handler(Exception)
    async def unhandled(request: Request, exc: Exception):
        # Log the detail server-side; never leak internals to the client.
        log.exception("Unhandled error on %s %s", request.method, request.url.path)
        return JSONResponse(status_code=500, content={"detail": "Internal server error"})

    return app


app = create_app()
