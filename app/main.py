"""FastAPI app: POST /documents, GET /documents/{id}, POST /search."""

import logging

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from app.api import documents, search
from app.api.errors import error_response

log = logging.getLogger(__name__)

app = FastAPI(title="Document Ingestion & Embedding Pipeline")
app.include_router(documents.router)
app.include_router(search.router)


@app.exception_handler(RequestValidationError)
async def invalid_request(request: Request, exc: RequestValidationError):
    """Every request-validation error uses the {"error": ...} shape. Input values are
    left out of `detail` (they may be PII)."""
    detail = [
        {"loc": [str(p) for p in err["loc"]], "msg": err["msg"], "type": err["type"]}
        for err in exc.errors()
    ]
    return JSONResponse(status_code=422, content={"error": "invalid_request", "detail": detail})


@app.exception_handler(Exception)
async def unhandled_error(request: Request, exc: Exception):
    log.exception("unhandled error on %s %s", request.method, request.url.path)
    return error_response("internal_server_error")


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}
