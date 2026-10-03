from typing import Annotated, Literal

from fastapi import APIRouter
from pydantic import BaseModel, ConfigDict, Field, StrictInt, StringConstraints, field_validator

from app.chunking import CHUNKING_VERSION
from app.db import chunk_embeddings, embedding_models
from app.db.connection import connect
from app.embedding import get_embedder

router = APIRouter()


class SearchFilters(BaseModel):
    model_config = ConfigDict(extra="forbid")

    document_type: Literal["referral_letter", "medical_certificate", "receipt"] | None = None
    country_code: Annotated[str, StringConstraints(pattern=r"^[A-Za-z]{2}$")] | None = None

    @field_validator("country_code")
    @classmethod
    def _upper(cls, value: str | None) -> str | None:
        return value.upper() if value else value


class SearchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    query: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=2000)]
    top_k: Annotated[StrictInt, Field(ge=1, le=50)] = 5
    filters: SearchFilters | None = None


@router.post("/search")
def search(request: SearchRequest):
    # Invalid bodies never reach here: RequestValidationError → 422 invalid_request (app.main).
    filters = request.filters or SearchFilters()
    with connect(autocommit=True) as conn:
        active = embedding_models.get_active(conn)
        if active is None:  # nothing embedded yet
            return {"results": []}
        embedder = get_embedder(active["model_name"])
        [query_vector] = embedder.encode([request.query])
        rows = chunk_embeddings.search(
            conn, query_vector, active["model_name"], active["model_version"], CHUNKING_VERSION,
            request.top_k, filters.document_type, filters.country_code,
        )
    return {
        "results": [
            {
                "chunk_id": str(r["chunk_id"]),
                "document_id": str(r["document_id"]),
                "document_type": r["document_type"],
                "page": r["page"],
                "score": 1 - r["distance"],
                "text": r["text"],
                "bbox": [r["x0"], r["y0"], r["x1"], r["y1"]],
                "embedding_model": r["model_name"],
            }
            for r in rows
        ]
    }
