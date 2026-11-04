"""HTTP routes. Business logic lives in `app.services`; this module only adapts."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, File, UploadFile, status
from fastapi.responses import FileResponse

from app.api.deps import get_ingestion_service, get_pipeline, get_storage
from app.core.config import Settings, get_settings
from app.core.errors import AppError, Conflict, NotFound
from app.core.logging import get_logger
from app.models.api import (
    HealthResponse,
    ImageDetailResponse,
    JobResponse,
    OutputsResponse,
    OutputItem,
    ProcessRequest,
    ProcessResponse,
    UploadResponse,
)
from app.models.domain import (
    ImageRecord,
    JobRecord,
    JobStatus,
    ProcessingReport,
    ProductStatus,
)
from app.services.ingestion import IngestionService
from app.services.processing.pipeline import ProcessingPipeline
from app.storage.local import LocalStorage, is_valid_id

logger = get_logger(__name__)

router = APIRouter()
SettingsDep = Annotated[Settings, Depends(get_settings)]
StorageDep = Annotated[LocalStorage, Depends(get_storage)]

_GEOREF_LABELS = {
    "embedded_geotransform": "embedded",
    "caller_override": "override",
    "gps_centre_footprint": "estimated_from_gps",
}


def _georef_label(image: ImageRecord) -> str:
    return "available" if image.validation.is_georeferenced else "none"


def _job_urls(job: JobRecord) -> dict[str, str]:
    base = f"/jobs/{job.job_id}"
    return {"job": base, "outputs": f"{base}/outputs", "report": f"/images/{job.image_id}/jobs/{job.job_id}/report"}


# --------------------------------------------------------------------- health
@router.get("/health", response_model=HealthResponse, tags=["system"])
def health(settings: SettingsDep, storage: StorageDep) -> HealthResponse:
    import rasterio

    checks: dict[str, str] = {}
    try:
        checks["storage"] = "ok" if storage.uploads_dir.is_dir() and storage.outputs_dir.is_dir() else "fail"
    except Exception:
        checks["storage"] = "fail"
    try:
        checks["gdal"] = rasterio.__gdal_version__
    except Exception as exc:  # pragma: no cover
        checks["gdal"] = f"unavailable: {exc}"
    try:
        from importlib.metadata import version

        checks["proj"] = version("pyproj")
    except Exception:
        checks["proj"] = "unknown"
    healthy = all(value != "fail" for value in checks.values())
    return HealthResponse(
        status="ok" if healthy else "degraded",
        environment=settings.environment,
        version=settings.app_version,
        checks=checks,
    )


# --------------------------------------------------------------------- upload
@router.post(
    "/images/upload",
    response_model=UploadResponse,
    status_code=status.HTTP_201_CREATED,
    tags=["images"],
    summary="Upload and validate an aerial image",
)
def upload_image(
    file: Annotated[UploadFile, File(description="JPEG, PNG, TIFF or WEBP image")],
    ingestion: Annotated[IngestionService, Depends(get_ingestion_service)],
) -> UploadResponse:
    record, exif_summary, decision = ingestion.ingest(
        file.filename or "upload", file.file, file.content_type
    )
    if decision.plan is not None:
        georef_status = _GEOREF_LABELS.get(decision.plan.method, "available")
        next_action = f"POST /images/{record.image_id}/process"
    else:
        georef_status = "missing"
        next_action = (
            f"POST /images/{record.image_id}/process (optionally with crs/origin/pixel_size)"
        )
    return UploadResponse(
        image_id=record.image_id,
        original_filename=record.original_filename,
        size_bytes=record.size_bytes,
        content_type=record.content_type,
        sha256=record.sha256,
        validation=record.validation,
        exif_summary=exif_summary,
        georeferencing_status=georef_status,
        warnings=record.warnings,
        image_url=f"/images/{record.image_id}/source",
        next_action=next_action,
    )


@router.post(
    "/images/batch",
    status_code=status.HTTP_201_CREATED,
    tags=["images"],
    summary="Upload several aerial images at once; each is validated independently",
)
def upload_batch(
    files: Annotated[list[UploadFile], File(description="One or more aerial images")],
    ingestion: Annotated[IngestionService, Depends(get_ingestion_service)],
) -> dict[str, list[dict[str, object]]]:
    accepted: list[dict[str, object]] = []
    rejected: list[dict[str, object]] = []
    for upload in files:
        try:
            record, _, decision = ingestion.ingest(
                upload.filename or "upload", upload.file, upload.content_type
            )
        except AppError as exc:
            rejected.append({"filename": upload.filename, "code": exc.code, "message": exc.message})
            continue
        accepted.append(
            {
                "image_id": record.image_id,
                "original_filename": record.original_filename,
                "georeferenced": decision.plan is not None,
                "footprint": record.footprint.model_dump() if record.footprint else None,
            }
        )
    return {"accepted": accepted, "rejected": rejected}


@router.get("/images/footprints", tags=["images"], summary="Footprints of all georeferenced images")
def list_footprints(storage: StorageDep) -> dict[str, list[dict[str, object]]]:
    items: list[dict[str, object]] = []
    for image_dir in storage.uploads_dir.iterdir():
        if not image_dir.is_dir() or not is_valid_id(image_dir.name):
            continue
        record_path = storage.image_record_path(image_dir.name)
        if not record_path.is_file():
            continue
        record = ImageRecord.model_validate(storage.read_json(record_path))
        if record.footprint is not None:
            items.append({"image_id": record.image_id, "footprint": record.footprint.model_dump()})
    return {"items": items}


# ------------------------------------------------------------------ image CRUD
@router.get("/images/{image_id}", response_model=ImageDetailResponse, tags=["images"])
def get_image(image_id: str, storage: StorageDep) -> ImageDetailResponse:
    image = ImageRecord.model_validate(storage.read_image_record(image_id))
    if not (storage.image_dir(image_id) / image.stored_filename).is_file():
        raise NotFound("Stored image file is missing", details={"image_id": image_id})

    latest_job = None
    if image.latest_job_id:
        try:
            latest_job = JobRecord.model_validate(
                storage.read_job_record(image_id, image.latest_job_id)
            )
        except NotFound:
            latest_job = None

    missing: list[str] = []
    if not image.validation.is_georeferenced:
        gps = image.exif.gps
        if gps is None:
            missing.append("GPS coordinates")
        elif gps.altitude_m is None:
            missing.append("GPS altitude")
        if image.exif.camera is None or image.exif.camera.focal_length_mm is None:
            missing.append("EXIF focal length")

    return ImageDetailResponse(
        image=image,
        latest_job=latest_job,
        georeferencing_status=_georef_label(image),
        missing_georeferencing_inputs=missing,
    )


@router.get("/images/{image_id}/source", tags=["images"], response_class=FileResponse)
def get_image_source(image_id: str, storage: StorageDep) -> FileResponse:
    image = ImageRecord.model_validate(storage.read_image_record(image_id))
    path = storage.image_dir(image_id) / image.stored_filename
    if not path.is_file():
        raise NotFound("Stored image file is missing", details={"image_id": image_id})
    return FileResponse(path, media_type=image.content_type, filename=image.original_filename)


@router.post(
    "/images/{image_id}/process",
    response_model=ProcessResponse,
    tags=["processing"],
    summary="Run the processing pipeline synchronously",
)
def process_image(
    image_id: str,
    storage: StorageDep,
    pipeline: Annotated[ProcessingPipeline, Depends(get_pipeline)],
    payload: ProcessRequest | None = None,
) -> ProcessResponse:
    if not storage.image_exists(image_id):
        raise NotFound("Image not found", details={"image_id": image_id})

    running = _find_running_job(storage, image_id)
    if running is not None:
        raise Conflict(
            "A job for this image is already running",
            details={"job_id": running.job_id},
        )

    if (payload and payload.crs) and not _complete_override(payload):
        missing = [
            name
            for name, value in (
                ("crs", payload.crs),
                ("origin_x", payload.origin_x),
                ("origin_y", payload.origin_y),
                ("pixel_size_x", payload.pixel_size_x),
                ("pixel_size_y", payload.pixel_size_y),
            )
            if value is None
        ]
        from app.core.errors import InvalidImage

        raise InvalidImage(
            "Georeferencing override requires crs, origin_x, origin_y, pixel_size_x and pixel_size_y",
            details={"missing_fields": missing},
        )

    job, _report = pipeline.process(image_id, payload)
    logger.info("Processed image %s as job %s", image_id, job.job_id)
    return ProcessResponse(
        job_id=job.job_id,
        image_id=image_id,
        status=job.status,
        report_url=_job_urls(job)["report"],
        outputs_url=_job_urls(job)["outputs"],
    )


@router.get("/images/{image_id}/metadata", response_model=None, tags=["images"])
def get_image_metadata(image_id: str, storage: StorageDep) -> dict:
    """Full extracted metadata (EXIF, XMP, GeoTIFF tags) for an upload."""
    path = storage.image_dir(image_id) / "metadata.json"
    if not path.is_file():
        raise NotFound("Metadata not found", details={"image_id": image_id})
    return storage.read_json(path)


# ----------------------------------------------------------------- job queries
@router.get("/jobs/{job_id}", response_model=JobResponse, tags=["jobs"])
def get_job(job_id: str, storage: StorageDep) -> JobResponse:
    image_id, raw = storage.find_job(job_id)
    job = JobRecord.model_validate(raw)
    summary = None
    report_url = None
    report_path = storage.job_dir(image_id, job_id) / "processing_report.json"
    if report_path.is_file():
        report = ProcessingReport.model_validate(storage.read_json(report_path))
        summary = report.qc_summary()
        report_url = f"/images/{image_id}/jobs/{job_id}/report"
    return JobResponse(job=job, quality_summary=summary, report_url=report_url)


@router.get("/jobs/{job_id}/outputs", response_model=OutputsResponse, tags=["jobs"])
def get_job_outputs(job_id: str, storage: StorageDep) -> OutputsResponse:
    image_id, raw = storage.find_job(job_id)
    job = JobRecord.model_validate(raw)
    items: list[OutputItem] = []
    for product in job.outputs:
        filename = None
        download_url = None
        if product.status is ProductStatus.GENERATED and product.path:
            filename = product.path.replace("\\", "/").rsplit("/", 1)[-1]
            download_url = f"/images/{image_id}/jobs/{job_id}/outputs/{filename}"
        items.append(
            OutputItem(
                kind=product.kind.value,
                status=product.status.value,
                filename=filename,
                size_bytes=product.size_bytes,
                sha256=product.sha256,
                download_url=download_url,
                reason=product.reason,
            )
        )
    return OutputsResponse(job_id=job.job_id, image_id=image_id, status=job.status, outputs=items)


@router.get("/images/{image_id}/jobs/{job_id}/outputs/{filename}", tags=["jobs"])
def download_output(image_id: str, job_id: str, filename: str, storage: StorageDep) -> FileResponse:
    stored = storage.resolve_download(image_id, job_id, filename)
    return FileResponse(stored.path, media_type=stored.content_type, filename=stored.name)


@router.get("/images/{image_id}/jobs/{job_id}/report", tags=["jobs"])
def get_job_report(image_id: str, job_id: str, storage: StorageDep) -> dict:
    path = storage.job_dir(image_id, job_id) / "processing_report.json"
    if not path.is_file():
        raise NotFound("Processing report not found", details={"job_id": job_id})
    return storage.read_json(path)


@router.get(
    "/images/{image_id}/jobs/{job_id}/report/download",
    tags=["jobs"],
    response_class=FileResponse,
)
def download_job_report(image_id: str, job_id: str, storage: StorageDep) -> FileResponse:
    path = storage.job_dir(image_id, job_id) / "processing_report.json"
    if not path.is_file():
        raise NotFound("Processing report not found", details={"job_id": job_id})
    return FileResponse(path, media_type="application/json", filename=f"{job_id}_report.json")


# ------------------------------------------------------------------- helpers
def _complete_override(payload: ProcessRequest) -> bool:
    return all(
        value is not None
        for value in (
            payload.crs,
            payload.origin_x,
            payload.origin_y,
            payload.pixel_size_x,
            payload.pixel_size_y,
        )
    )


def _find_running_job(storage: LocalStorage, image_id: str) -> JobRecord | None:
    jobs_dir = storage.image_dir(image_id) / "jobs"
    if not jobs_dir.is_dir():
        return None
    for record_path in jobs_dir.glob("*/job.json"):
        raw = storage.read_json(record_path)
        if raw.get("status") in {JobStatus.RUNNING.value, JobStatus.PENDING.value}:
            return JobRecord.model_validate(raw)
    return None
