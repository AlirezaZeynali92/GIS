"""The processing pipeline and its job wrapper.

The pipeline is a plain class with no FastAPI imports so it can be driven from
tests or a worker later. Jobs run synchronously today but are recorded as
first-class entities, which is what makes async execution a drop-in change.
"""

from __future__ import annotations

import shutil
import traceback
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.core.config import Settings
from app.core.errors import AppError, InvalidImage, ProcessingFailed
from app.core.logging import get_logger
from app.models.domain import (
    CheckResult,
    ImageRecord,
    JobRecord,
    JobStatus,
    JobStep,
    OutputKind,
    ProcessingReport,
    ProductResult,
    ProductStatus,
    RasterInfo,
)
from app.services.metadata.extractor import MetadataExtractor
from app.services.processing.georef import GeoreferenceDecision, resolve_georeferencing
from app.services.processing.raster import (
    RasterError,
    render_preview,
    write_cog,
    write_geotiff,
)
from app.services.quality.report import (
    accuracy_checks,
    build_product_matrix,
    georeferencing_checks,
    input_checks,
    not_generated,
    output_checks,
    sha256_of,
)
from app.storage.local import LocalStorage, new_id

logger = get_logger(__name__)


def _now() -> datetime:
    return datetime.now(timezone.utc)


class ProcessingPipeline:
    """Turns a stored image into products, a QC report and a status record."""

    def __init__(self, settings: Settings, storage: LocalStorage) -> None:
        self.settings = settings
        self.storage = storage
        self.extractor = MetadataExtractor()

    # ------------------------------------------------------------------ entry
    def process(
        self,
        image_id: str,
        override_request: Any | None = None,
    ) -> tuple[JobRecord, ProcessingReport]:
        image = ImageRecord.model_validate(self.storage.read_image_record(image_id))
        job_id = new_id("job")
        job = JobRecord(job_id=job_id, image_id=image_id, status=JobStatus.RUNNING, started_at=_now())
        job_dir = self.storage.job_dir(image_id, job_id)
        job_dir.mkdir(parents=True, exist_ok=True)
        self._save_job(job)

        source_path = self.storage.image_dir(image_id) / image.stored_filename
        try:
            report = self._run(image, job, job_dir, source_path, override_request)
        except AppError as exc:
            logger.error("Job %s failed: %s", job_id, exc.message)
            job.status = JobStatus.FAILED
            job.finished_at = _now()
            job.error = {"code": exc.code, "message": exc.message, "details": exc.details}
            self._save_job(job)
            raise
        except Exception as exc:
            logger.exception("Job %s crashed", job_id)
            job.status = JobStatus.FAILED
            job.finished_at = _now()
            job.error = {
                "code": "processing_failed",
                "message": "Unexpected error during processing",
                "details": {
                    "type": type(exc).__name__,
                    "trace": traceback.format_exc(limit=4).splitlines()[-4:],
                },
            }
            self._save_job(job)
            raise ProcessingFailed(
                "Processing failed with an unexpected error",
                details={"job_id": job_id, "type": type(exc).__name__, "reason": str(exc)},
            ) from exc

        job.status = JobStatus.SUCCEEDED
        job.finished_at = _now()
        job.outputs = list(report.products.products)
        job.report_path = str(job_dir / "processing_report.json")
        job.warnings = report.warnings
        self._save_job(job)

        image_record = self.storage.read_image_record(image_id)
        image_record["latest_job_id"] = job_id
        self.storage.write_json(self.storage.image_record_path(image_id), image_record)
        return job, report

    # ------------------------------------------------------------------- steps
    def _run(
        self,
        image: ImageRecord,
        job: JobRecord,
        job_dir: Path,
        source_path: Path,
        override_request: Any | None,
    ) -> ProcessingReport:
        products: list[ProductResult] = []
        warnings = list(image.warnings)

        with self._step(job, "copy_original"):
            products.append(self._copy_original(job_dir, source_path))

        with self._step(job, "load_metadata"):
            exif_summary, details, meta_warnings = self.extractor.extract_summary(source_path)
            warnings.extend(meta_warnings)
            metadata_path = job_dir / "metadata.json"
            self.storage.write_json(
                metadata_path,
                {
                    "image": image.model_dump(mode="json"),
                    "extracted": details,
                    "summary": exif_summary.model_dump(mode="json"),
                },
            )
            products.append(self._artifact(OutputKind.METADATA, metadata_path))

        decision = resolve_georeferencing(
            exif_summary,
            details,
            image.validation.width,
            image.validation.height,
            override=_override_from_request(override_request),
        )

        geotiff_info: RasterInfo | None = None
        cog_info: RasterInfo | None = None
        geotiff_path: Path | None = None

        with self._step(job, "georeference"):
            if decision.plan is None:
                reason = self._no_georef_reason(decision)
                products.append(not_generated(OutputKind.GEOTIFF, reason))
                products.append(not_generated(OutputKind.COG, reason))
                logger.info("Skipping GeoTIFF/COG for %s: %s", image.image_id, reason)
            else:
                logger.info("Georeferencing via %s (%s)", decision.plan.method, decision.plan.crs)
                warnings.extend(n for n in decision.notes if n not in warnings)

        with self._step(job, "write_geotiff"):
            if decision.plan is not None:
                geotiff_path = job_dir / "image_georeferenced.tif"
                try:
                    geotiff_info = write_geotiff(source_path, geotiff_path, decision.plan)
                    products.append(self._artifact(OutputKind.GEOTIFF, geotiff_path))
                except (RasterError, OSError) as exc:
                    logger.error("GeoTIFF creation failed: %s", exc)
                    products.append(
                        not_generated(OutputKind.GEOTIFF, f"Raster creation failed: {exc}")
                    )
                    warnings.append(f"GeoTIFF creation failed: {exc}")

        with self._step(job, "write_cog"):
            if geotiff_info is not None and geotiff_path is not None:
                cog_path = job_dir / "image_georeferenced_cog.tif"
                try:
                    cog_info = write_cog(geotiff_path, cog_path)
                    products.append(self._artifact(OutputKind.COG, cog_path))
                except (RasterError, OSError) as exc:
                    logger.error("COG creation failed: %s", exc)
                    products.append(not_generated(OutputKind.COG, f"COG creation failed: {exc}"))
                    warnings.append(f"COG creation failed: {exc}")
            elif decision.plan is None:
                pass  # already recorded as not_generated alongside the GeoTIFF
            else:
                products.append(
                    not_generated(OutputKind.COG, "Requires a valid georeferenced GeoTIFF")
                )

        with self._step(job, "render_preview"):
            preview_path = job_dir / "preview.jpg"
            try:
                size = render_preview(source_path, preview_path, self.settings.thumbnail_max_size)
                products.append(self._artifact(OutputKind.PREVIEW, preview_path, note=f"{size[0]}x{size[1]}"))
            except Exception as exc:
                logger.warning("Preview rendering failed: %s", exc)
                products.append(not_generated(OutputKind.PREVIEW, f"Preview failed: {exc}"))

        report = ProcessingReport(
            image_id=image.image_id,
            job_id=job.job_id,
            processing_success=True,
            geospatial_validity="valid" if geotiff_info else "not_assessable",
            geometric_accuracy="not_assessed",
            accuracy_statement=self._accuracy_statement(decision, geotiff_info),
            allocation=decision.plan.to_allocation() if decision.plan else None,
            geotiff=geotiff_info,
            cog=cog_info,
            products=build_product_matrix(products),
            quality_checks=[],
            missing_georeferencing_inputs=decision.missing_inputs,
            warnings=warnings,
            notes=decision.notes,
        )

        with self._step(job, "quality_control"):
            checks: list[CheckResult] = []
            checks.extend(input_checks(source_path, image.validation, exif_summary))
            checks.extend(georeferencing_checks(report, image.validation))
            checks.extend(output_checks(report, report.products))
            checks.extend(accuracy_checks(image.validation, report))
            report.quality_checks = checks
            self._attach_hashes(report)

        with self._step(job, "write_report"):
            report_path = job_dir / "processing_report.json"
            report.products = build_product_matrix([p for p in products if p.kind is not OutputKind.REPORT])
            self.storage.write_json(report_path, report.model_dump(mode="json"))
            report.products.products.append(self._artifact(OutputKind.REPORT, report_path))
        return report

    # ----------------------------------------------------------------- helpers
    def _copy_original(self, job_dir: Path, source_path: Path) -> ProductResult:
        target = job_dir / f"original{source_path.suffix.lower()}"
        shutil.copy2(source_path, target)
        return self._artifact(OutputKind.ORIGINAL, target)

    def _artifact(self, kind: OutputKind, path: Path, note: str | None = None) -> ProductResult:
        return ProductResult(
            kind=kind,
            status=ProductStatus.GENERATED,
            path=str(path),
            size_bytes=path.stat().st_size,
            sha256=sha256_of(path),
            reason=note,
        )

    def _attach_hashes(self, report: ProcessingReport) -> None:
        for product in report.products.products:
            if product.status is ProductStatus.GENERATED and product.path:
                candidate = Path(product.path)
                if candidate.is_file():
                    product.size_bytes = candidate.stat().st_size
                    product.sha256 = sha256_of(candidate)

    def _no_georef_reason(self, decision: GeoreferenceDecision) -> str:
        if decision.missing_inputs:
            return "Missing georeferencing inputs: " + ", ".join(decision.missing_inputs)
        return "No georeferencing information available in the source image"

    def _accuracy_statement(
        self, decision: GeoreferenceDecision, geotiff: RasterInfo | None
    ) -> str:
        if geotiff is None:
            return (
                "No georeferenced raster was produced. This image therefore has no "
                "positional information and must not be used as a map layer."
            )
        method = decision.plan.method if decision.plan else "unknown"
        if method == "gps_centre_footprint":
            return (
                "The raster is georeferenced by an ESTIMATE derived from the EXIF GPS "
                "fix and camera geometry (nadir assumption). It is NOT an orthophoto and "
                "has not been orthorectified. Positional accuracy was not measured."
            )
        if method == "embedded_geotransform":
            return (
                "The raster inherits the CRS and transform stored in the source file. "
                "Its positional accuracy is only as good as that original georeferencing, "
                "which this pipeline did not verify."
            )
        return (
            "The raster placement was supplied by the caller and not verified against "
            "control points. It is NOT an orthophoto."
        )

    def _step(self, job: JobRecord, name: str):
        return _StepContext(self, job, name)

    def _save_job(self, job: JobRecord) -> None:
        self.storage.write_json(
            self.storage.job_record_path(job.image_id, job.job_id),
            job.model_dump(mode="json"),
        )


class _StepContext:
    """Records per-step timing into the job record, persisting as it goes."""

    def __init__(self, pipeline: ProcessingPipeline, job: JobRecord, name: str) -> None:
        self.pipeline = pipeline
        self.job = job
        self.name = name
        self.step: JobStep | None = None

    def __enter__(self) -> JobStep:
        self.step = JobStep(name=self.name, status=JobStatus.RUNNING, started_at=_now())
        self.job.steps.append(self.step)
        self.pipeline._save_job(self.job)
        logger.info("job=%s step=%s start", self.job.job_id, self.name)
        return self.step

    def __exit__(self, exc_type, exc, tb) -> bool:
        assert self.step is not None
        self.step.finished_at = _now()
        self.step.duration_ms = int(
            (self.step.finished_at - self.step.started_at).total_seconds() * 1000
        )
        if exc is None:
            self.step.status = JobStatus.SUCCEEDED
        else:
            self.step.status = JobStatus.FAILED
            self.step.message = f"{type(exc).__name__}: {exc}"
        logger.info(
            "job=%s step=%s %s in %dms",
            self.job.job_id,
            self.name,
            self.step.status.value,
            self.step.duration_ms,
        )
        self.pipeline._save_job(self.job)
        return False


def _override_from_request(payload: Any | None):
    if payload is None:
        return None
    from app.models.domain import GeoTransformOverride

    values = {
        "crs": getattr(payload, "crs", None),
        "origin_x": getattr(payload, "origin_x", None),
        "origin_y": getattr(payload, "origin_y", None),
        "pixel_size_x": getattr(payload, "pixel_size_x", None),
        "pixel_size_y": getattr(payload, "pixel_size_y", None),
    }
    if not any(value is not None for value in values.values()):
        return None
    missing = [key for key, value in values.items() if value is None]
    if missing:
        raise InvalidImage(
            "Incomplete georeferencing override",
            details={"missing_fields": missing},
        )
    return GeoTransformOverride(**values)
