"""Domain models shared by the API, processing pipeline and reporting layer."""

from __future__ import annotations

from datetime import datetime, timezone
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class JobStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


class CheckStatus(StrEnum):
    PASS = "PASS"
    FAIL = "FAIL"
    WARNING = "WARNING"
    NOT_APPLICABLE = "NOT_APPLICABLE"


class ProductStatus(StrEnum):
    GENERATED = "generated"
    NOT_GENERATED = "not_generated"
    NOT_APPLICABLE = "not_applicable"


class OutputKind(StrEnum):
    ORIGINAL = "original"
    METADATA = "metadata_json"
    GEOTIFF = "geotiff"
    COG = "cog"
    PREVIEW = "preview"
    REPORT = "processing_report"


class GeoBounds(BaseModel):
    """WGS84 bounding box built from the raster corner coordinates."""

    west: float
    south: float
    east: float
    north: float

    @property
    def as_list(self) -> list[float]:
        return [self.west, self.south, self.east, self.north]


class GpsFix(BaseModel):
    latitude: float
    longitude: float
    altitude_m: float | None = None
    datum: str | None = None
    direction_ref: str | None = None


class CameraInfo(BaseModel):
    make: str | None = None
    model: str | None = None
    lens_model: str | None = None
    focal_length_mm: float | None = None
    focal_length_35mm_eq: float | None = None
    exposure_time_s: float | None = None
    f_number: float | None = None
    iso: int | None = None
    software: str | None = None


class GeoTransformOverride(BaseModel):
    """Caller-supplied placement for images without embedded georeferencing."""

    crs: str = Field(description="EPSG code or PROJ string, e.g. 'EPSG:4326'")
    origin_x: float = Field(description="Centre X of the upper-left pixel")
    origin_y: float = Field(description="Centre Y of the upper-left pixel")
    pixel_size_x: float = Field(gt=0, description="Ground sample distance along X, CRS units/px")
    pixel_size_y: float = Field(gt=0, description="Ground sample distance along Y, CRS units/px")


class ValidationSummary(BaseModel):
    content_type: str
    detected_format: str
    extension: str
    file_size_bytes: int
    sha256: str
    width: int
    height: int
    mode: str
    is_georeferenced: bool
    warnings: list[str] = Field(default_factory=list)


class ExifSummary(BaseModel):
    """EXIF values used by the pipeline (full dump lives in metadata.json)."""

    gps: GpsFix | None = None
    camera: CameraInfo | None = None
    datetime_original: str | None = None
    orientation: int | None = None
    exif_tag_count: int = 0
    has_embedded_xmp: bool = False


class ImageRecord(BaseModel):
    image_id: str
    original_filename: str
    stored_filename: str
    content_type: str
    size_bytes: int
    sha256: str
    uploaded_at: datetime = Field(default_factory=utc_now)
    validation: ValidationSummary
    exif: ExifSummary
    metadata_path: str
    latest_job_id: str | None = None
    warnings: list[str] = Field(default_factory=list)
    footprint: GeoBounds | None = None


class CheckResult(BaseModel):
    name: str
    status: CheckStatus
    observed: Any = None
    expected: Any = None
    explanation: str = ""


class Allocation(BaseModel):
    """How the image pixels were placed in map space."""

    method: str = Field(description="gps_centre_footprint | embedded_geotransform | caller_override")
    crs: str
    pixel_size_x: float
    pixel_size_y: float
    origin_x: float
    origin_y: float
    estimated_gsd: bool = Field(
        default=False,
        description="True when the pixel size is derived from camera geometry, not measured",
    )
    notes: list[str] = Field(default_factory=list)


class RasterInfo(BaseModel):
    path: str
    driver: str
    width: int
    height: int
    band_count: int
    dtype: str
    crs: str
    pixel_size_x: float
    pixel_size_y: float
    bounds_native: list[float]
    bounds_wgs84: list[float] | None = None
    nodata: float | None = None
    block_size: list[int] | None = None
    compression: str | None = None
    overview_levels: list[int] = Field(default_factory=list)
    is_cog: bool = False


class ProductResult(BaseModel):
    kind: OutputKind
    status: ProductStatus
    path: str | None = None
    download_url: str | None = None
    size_bytes: int | None = None
    sha256: str | None = None
    reason: str | None = None


class ProductMatrix(BaseModel):
    products: list[ProductResult]

    def get(self, kind: OutputKind) -> ProductResult:
        return next(p for p in self.products if p.kind == kind)


class ProcessingReport(BaseModel):
    report_version: str = "1.0"
    generated_at: datetime = Field(default_factory=utc_now)
    image_id: str
    job_id: str

    processing_success: bool
    geospatial_validity: str = Field(description="valid | invalid | not_assessable")
    geometric_accuracy: str = Field(
        description="not_assessed | plausible | questionable",
    )
    accuracy_statement: str

    allocation: Allocation | None = None
    geotiff: RasterInfo | None = None
    cog: RasterInfo | None = None
    products: ProductMatrix
    quality_checks: list[CheckResult]
    missing_georeferencing_inputs: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)

    def qc_summary(self) -> dict[str, int]:
        return {
            "pass": sum(1 for c in self.quality_checks if c.status is CheckStatus.PASS),
            "warning": sum(1 for c in self.quality_checks if c.status is CheckStatus.WARNING),
            "fail": sum(1 for c in self.quality_checks if c.status is CheckStatus.FAIL),
            "not_applicable": sum(
                1 for c in self.quality_checks if c.status is CheckStatus.NOT_APPLICABLE
            ),
        }


class JobStep(BaseModel):
    name: str
    status: JobStatus
    started_at: datetime
    finished_at: datetime | None = None
    duration_ms: int | None = None
    message: str | None = None


class JobRecord(BaseModel):
    job_id: str
    image_id: str
    status: JobStatus = JobStatus.PENDING
    created_at: datetime = Field(default_factory=utc_now)
    started_at: datetime | None = None
    finished_at: datetime | None = None
    steps: list[JobStep] = Field(default_factory=list)
    outputs: list[ProductResult] = Field(default_factory=list)
    report_path: str | None = None
    error: dict[str, Any] | None = None
    warnings: list[str] = Field(default_factory=list)
