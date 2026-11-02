from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from app.api import auth, health, logs, users

SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}
CSRF_HEADER = "x-requested-with"

app = FastAPI(title="FastNexa Outreach API", docs_url="/api/docs", openapi_url="/api/openapi.json")


@app.middleware("http")
async def csrf_guard(request: Request, call_next):
    """Browsers can't add a custom header to a cross-site request without CORS, which we never allow.
    Requiring it on every state-changing request blocks cross-site request forgery."""
    if request.method not in SAFE_METHODS and not request.headers.get(CSRF_HEADER):
        return JSONResponse({"detail": "csrf_header_missing"}, status_code=403)
    return await call_next(request)


for router in (auth.router, users.router, logs.router, health.router):
    app.include_router(router, prefix="/api")
