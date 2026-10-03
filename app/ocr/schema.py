"""Pydantic models for the normalised OCR JSON (CLAUDE.md §4)."""

from datetime import datetime
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

EngineName = Literal["aws-textract", "tesseract", "azure-document-intelligence"]
ENGINE_NAMES: tuple[str, ...] = EngineName.__args__

Fraction = Annotated[float, Field(ge=0.0, le=1.0)]


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class NormalisedLine(_Strict):
    line_index: int = Field(ge=0)
    text: str
    bbox: tuple[Fraction, Fraction, Fraction, Fraction]
    confidence: Fraction | None
    confidence_source: Literal["engine_line", "mean_of_words"] | None

    # True when any coordinate was clamped; feeds the `bbox_clamped` quality check.
    bbox_clamped: bool = False

    @model_validator(mode="before")
    @classmethod
    def _clamp(cls, data):
        """Engines can report coordinates slightly off-page (skewed scans); clamp
        each coordinate into [0, 1]. Non-numeric values are left for type validation.
        `bbox_clamped` is only ever set, never reset, so it survives re-validation
        of an already-normalised file."""
        if isinstance(data, dict) and isinstance(data.get("bbox"), (list, tuple)):
            bbox = tuple(data["bbox"])
            clamped = tuple(
                min(max(v, 0.0), 1.0) if isinstance(v, (int, float)) and not isinstance(v, bool) else v
                for v in bbox
            )
            data = {**data, "bbox": clamped, "bbox_clamped": bool(data.get("bbox_clamped")) or clamped != bbox}
        return data

    @field_validator("bbox")
    @classmethod
    def _ordered(cls, bbox: tuple[float, float, float, float]):
        # Checked after clamping: only a genuinely inverted box is rejected.
        x0, y0, x1, y1 = bbox
        if x0 > x1 or y0 > y1:
            raise ValueError(f"bbox must satisfy x0<=x1 and y0<=y1, got {list(bbox)}")
        return bbox


class NormalisedPage(_Strict):
    page_number: int = Field(ge=1)
    width: float | None = Field(gt=0)
    height: float | None = Field(gt=0)
    size_unit: Literal["px", "inch"] | None
    size_reason: str | None
    lines: list[NormalisedLine]

    @model_validator(mode="after")
    def _size_consistent(self):
        sized = [self.width is not None, self.height is not None, self.size_unit is not None]
        if any(sized) and not all(sized):
            raise ValueError("width, height and size_unit must be all set or all null")
        if not any(sized) and not self.size_reason:
            raise ValueError("size_reason is required when page size is null")
        indexes = [line.line_index for line in self.lines]
        if indexes != list(range(len(indexes))):
            raise ValueError("line_index must be 0..n-1 in order")
        return self

    @property
    def mean_confidence(self) -> float | None:
        values = [line.confidence for line in self.lines if line.confidence is not None]
        return sum(values) / len(values) if values else None

    @property
    def bbox_clamped_count(self) -> int:
        return sum(line.bbox_clamped for line in self.lines)


class SourceInfo(_Strict):
    original_filename: str | None = None
    mime_type: str | None = None
    source_sha256: str | None = None
    country_code: str | None = Field(default=None, pattern=r"^[A-Z]{2}$")


class EngineInfo(_Strict):
    name: EngineName
    version: str | None = None
    processed_at: datetime | None = None


class NormalisedDocument(_Strict):
    normaliser_version: str
    normalised_at: datetime
    coordinate_system: Literal["fraction_of_page_0_1_origin_top_left"] = (
        "fraction_of_page_0_1_origin_top_left"
    )
    confidence_scale: Literal["0_1"] = "0_1"
    source: SourceInfo
    engine: EngineInfo
    pages: list[NormalisedPage]
