from fastapi.responses import JSONResponse

ERROR_STATUS = {
    "file_missing": 400,
    "unreadable_file": 422,
    "unsupported_ocr_format": 422,
    "unsupported_document_type": 422,
    "invalid_country_code": 422,
    "invalid_request": 422,
    "not_found": 404,
    "internal_server_error": 500,
}


def error_response(code: str) -> JSONResponse:
    return JSONResponse(status_code=ERROR_STATUS.get(code, 500), content={"error": code})
