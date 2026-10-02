"""FastAPI app. Phase 1 exposes only a health check; routers arrive in phase 2."""

from fastapi import FastAPI

app = FastAPI(title="Document Ingestion & Embedding Pipeline")


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}
