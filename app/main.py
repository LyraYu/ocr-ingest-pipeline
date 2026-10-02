"""FastAPI app: POST /documents, GET /documents/{id}. Search arrives in phase 4."""

import logging

from fastapi import FastAPI, Request

from app.api import documents
from app.api.errors import error_response

log = logging.getLogger(__name__)

app = FastAPI(title="Document Ingestion & Embedding Pipeline")
app.include_router(documents.router)


@app.exception_handler(Exception)
async def unhandled_error(request: Request, exc: Exception):
    log.exception("unhandled error on %s %s", request.method, request.url.path)
    return error_response("internal_server_error")


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}
