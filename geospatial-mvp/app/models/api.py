"""Pydantic models for API request/response payloads."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel

from app.models.domain import (
    CheckResult,
    ExifSummary,
    GpsFix,
    ImageRecord,
    JobRecord,
    JobStatus,
    ValidationSummary,
)


class HealthResponse(BaseModel):
    status: str
    environment: str
    version: str
    checks: dict[str, str]


class UploadResponse(BaseModel):
    image_id: str
    original_filename: str
    size_bytes: int
    content_type: str
    sha256: str
    validation: ValidationSummary
    exif_summary: ExifSummary
    georeferencing_status: str
    warnings: list[str]
    image_url: str
    next_action: str


class ProcessRequest(BaseModel):
    """Optional explicit georeferencing for images without embedded geo data."""

    crs: str | None = None
    origin_x: float | None = None
    origin_y: float | None = None
    pixel_size_x: float | None = None
    pixel_size_y: float | None = None


class ProcessResponse(BaseModel):
    job_id: str
    image_id: str
    status: JobStatus
    report_url: str
    outputs_url: str


class ImageDetailResponse(BaseModel):
    image: ImageRecord
    latest_job: JobRecord | None = None
    georeferencing_status: str
    missing_georeferencing_inputs: list[str]


class JobResponse(BaseModel):
    job: JobRecord
    quality_summary: dict[str, int] | None = None
    report_url: str | None = None


class OutputItem(BaseModel):
    kind: str
    status: str
    filename: str | None = None
    size_bytes: int | None = None
    sha256: str | None = None
    download_url: str | None = None
    reason: str | None = None


class OutputsResponse(BaseModel):
    job_id: str
    image_id: str
    status: JobStatus
    outputs: list[OutputItem]


class ErrorBody(BaseModel):
    code: str
    message: str
    details: dict[str, Any] = {}


class ErrorResponse(BaseModel):
    error: ErrorBody


class GpsPreview(BaseModel):
    gps: GpsFix | None
    checks: list[CheckResult]
