"""Image ingestion: persist an upload, validate it, and record what we learned."""

from __future__ import annotations

import shutil
from typing import BinaryIO

from app.core.config import Settings
from app.core.logging import get_logger
from app.models.domain import ExifSummary, GeoBounds, ImageRecord, ValidationSummary
from app.services.metadata.extractor import MetadataExtractor
from app.services.processing.georef import (
    GeoreferenceDecision,
    resolve_georeferencing,
    wgs84_bounds,
)
from app.services.quality.validator import FileValidator
from app.storage.local import LocalStorage, new_id, sanitize_filename

logger = get_logger(__name__)


def _footprint_of(decision: GeoreferenceDecision, width: int, height: int) -> GeoBounds | None:
    plan = decision.plan
    if plan is None:
        return None
    bounds = wgs84_bounds(
        plan.crs,
        plan.origin_x,
        plan.origin_y,
        plan.pixel_size_x,
        plan.pixel_size_y,
        width,
        height,
    )
    if bounds is None:
        return None
    return GeoBounds(west=bounds[0], south=bounds[1], east=bounds[2], north=bounds[3])


class IngestionService:
    def __init__(self, settings: Settings, storage: LocalStorage) -> None:
        self.settings = settings
        self.storage = storage
        self.validator = FileValidator(settings)
        self.extractor = MetadataExtractor()

    def ingest(
        self,
        filename: str,
        stream: BinaryIO,
        content_type: str | None,
    ) -> tuple[ImageRecord, ExifSummary, GeoreferenceDecision]:
        image_id = new_id("img")
        stored_path, size, digest = self.storage.save_upload(
            image_id, filename, stream, self.settings.max_upload_bytes
        )
        logger.info("Validating upload %s (%s, %d bytes)", image_id, filename, size)

        try:
            outcome = self.validator.validate(stored_path, filename, content_type)
            exif_summary, details, meta_warnings = self.extractor.extract_summary(stored_path)
        except Exception:
            shutil.rmtree(self.storage.image_dir(image_id), ignore_errors=True)
            raise

        warnings = [*outcome.warnings, *meta_warnings]
        decision = resolve_georeferencing(exif_summary, details, outcome.width, outcome.height)
        if decision.plan is None:
            warnings.append(
                "No usable georeferencing: GeoTIFF/COG will be skipped when processing "
                "unless an explicit placement is supplied."
            )

        footprint = _footprint_of(decision, outcome.width, outcome.height)
        record = ImageRecord(
            image_id=image_id,
            original_filename=sanitize_filename(filename),
            stored_filename=stored_path.name,
            content_type=content_type or f"image/{outcome.detected_format.lower()}",
            size_bytes=size,
            sha256=digest,
            validation=ValidationSummary(
                content_type=content_type or "application/octet-stream",
                detected_format=outcome.detected_format,
                extension=outcome.extension,
                file_size_bytes=size,
                sha256=digest,
                width=outcome.width,
                height=outcome.height,
                mode=outcome.mode,
                is_georeferenced=decision.plan is not None,
                warnings=warnings,
            ),
            exif=exif_summary,
            metadata_path="metadata.json",
            warnings=warnings,
            footprint=footprint,
        )

        self.storage.write_json(
            self.storage.image_dir(image_id) / "metadata.json",
            {
                "image_id": image_id,
                "original_filename": record.original_filename,
                "extracted": details,
                "summary": exif_summary.model_dump(mode="json"),
            },
        )
        self.storage.write_json(
            self.storage.image_record_path(image_id), record.model_dump(mode="json")
        )
        return record, exif_summary, decision
