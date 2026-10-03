"""Environment settings. Read once via `get_settings()`."""

import os
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from app.version import runtime_code_version

DEFAULT_COUNTRY_CODE = "SG"


class ConfigError(RuntimeError):
    pass


@dataclass(frozen=True)
class Settings:
    database_url: str
    data_dir: Path
    embedding_model: str
    quality_min_page_confidence: float
    code_version: str


def _required(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise ConfigError(f"environment variable {name} is not set (see .env.example)")
    return value


@lru_cache
def get_settings() -> Settings:
    return Settings(
        database_url=_required("DATABASE_URL"),
        data_dir=Path(os.environ.get("DATA_DIR", "./data")),
        embedding_model=os.environ.get(
            "EMBEDDING_MODEL", "sentence-transformers/all-MiniLM-L6-v2"
        ),
        quality_min_page_confidence=float(
            os.environ.get("QUALITY_MIN_PAGE_CONFIDENCE", "0.80")
        ),
        code_version=runtime_code_version(),
    )
