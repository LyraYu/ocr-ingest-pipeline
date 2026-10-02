"""Engine name → (shape check, normaliser), plus assembly of the normalised document."""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from app.ocr import NORMALISER_VERSION, UnsupportedFormatError, azure_di, tesseract, textract
from app.ocr.schema import EngineInfo, NormalisedDocument, NormalisedPage

if TYPE_CHECKING:
    from app.ocr.envelope import Envelope


@dataclass(frozen=True)
class EngineAdapter:
    check_shape: Callable[[dict], None]
    normalise: Callable[[dict], list[NormalisedPage]]


ENGINES: dict[str, EngineAdapter] = {
    textract.ENGINE: EngineAdapter(textract.check_shape, textract.normalise),
    tesseract.ENGINE: EngineAdapter(tesseract.check_shape, tesseract.normalise),
    azure_di.ENGINE: EngineAdapter(azure_di.check_shape, azure_di.normalise),
}


def get_engine(name: str) -> EngineAdapter:
    try:
        return ENGINES[name]
    except KeyError:
        raise UnsupportedFormatError(
            f"unknown ocr.engine {name!r}; supported: {sorted(ENGINES)}"
        ) from None


def normalise_envelope(envelope: "Envelope", now: datetime | None = None) -> NormalisedDocument:
    pages = get_engine(envelope.ocr.engine).normalise(envelope.raw_output)
    return NormalisedDocument(
        normaliser_version=NORMALISER_VERSION,
        normalised_at=now or datetime.now(UTC),
        source=envelope.source,
        engine=EngineInfo(
            name=envelope.ocr.engine,
            version=envelope.ocr.engine_version,
            processed_at=envelope.ocr.processed_at,
        ),
        pages=pages,
    )
