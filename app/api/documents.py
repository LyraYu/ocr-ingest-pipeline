from uuid import UUID

from fastapi import APIRouter, File, Form, UploadFile
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse

from app.api.errors import error_response
from app.db import document_pages, document_stages, documents, extracted_fields, quality_checks
from app.db.connection import connect
from app.pipeline.runner import run_pipeline
from app.pipeline.stages import normalise_country_code

router = APIRouter()


@router.post("/documents")
def create_document(
    file: UploadFile | None = File(None),
    country_code: str | None = Form(None),
):
    # Sync handler: FastAPI runs it in the threadpool, so blocking DB/disk I/O is fine.
    if file is None:
        return error_response("file_missing")
    file_bytes = file.file.read()
    if not file_bytes:
        return error_response("file_missing")
    try:
        country = normalise_country_code(country_code)
    except ValueError:
        return error_response("invalid_country_code")

    result = run_pipeline(file_bytes, file.filename, country, "ingest")
    if result.error_code:
        # Also for a duplicate of a document that failed earlier: same bytes, same answer.
        return error_response(result.error_code)
    return JSONResponse(status_code=200 if result.duplicate else 201, content=result.response_body())


@router.get("/documents/{document_id}")
def get_document(document_id: str):
    try:
        doc_id = UUID(document_id)
    except ValueError:
        return error_response("not_found")
    with connect(autocommit=True) as conn:
        doc = documents.get(conn, doc_id)
        if doc is None:
            return error_response("not_found")
        body = {
            **doc,
            "stages": document_stages.list_for_document(conn, doc_id),
            "quality_checks": quality_checks.list_for_document(conn, doc_id),
            "extracted_fields": extracted_fields.list_for_document(conn, doc_id),
            "pages": document_pages.list_for_document(conn, doc_id),
        }
    return JSONResponse(content=jsonable_encoder(body))
