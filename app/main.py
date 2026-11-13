from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from app.api import (
    auth,
    campaigns,
    dnc,
    duplicates,
    groups,
    health,
    leads,
    logs,
    mailboxes,
    people,
    reports,
    settings,
    users,
)
from app.core.config import get_settings
from app.core.errors import init_error_tracking

SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}
CSRF_HEADER = "x-requested-with"

init_error_tracking("api")

_docs = get_settings().api_docs
app = FastAPI(
    title="FastNexa Outreach API",
    docs_url="/api/docs" if _docs else None,
    redoc_url=None,
    openapi_url="/api/openapi.json" if _docs else None,
)


@app.middleware("http")
async def csrf_guard(request: Request, call_next):
    """Browsers can't add a custom header to a cross-site request without CORS, which we never allow.
    Requiring it on every state-changing request blocks cross-site request forgery."""
    if request.method not in SAFE_METHODS and not request.headers.get(CSRF_HEADER):
        return JSONResponse({"detail": "csrf_header_missing"}, status_code=403)
    return await call_next(request)


for router in (
    auth.router,
    users.router,
    logs.router,
    health.router,
    mailboxes.router,
    settings.router,
    groups.router,
    duplicates.router,
    leads.router,
    dnc.router,
    campaigns.router,
    reports.router,
    people.router,
):
    app.include_router(router, prefix="/api")
