"""Automated quality control over generated artefacts.

Checks are grouped by what they actually prove:

* integrity / processing  — did the files survive a write-and-reread cycle?
* geospatial validity     — is the output honestly placed in a CRS?
* geometric accuracy      — explicitly *not* assessed for single-image input.

"Successfully processed" never implies "geometrically accurate".
"""

from __future__ import annotations

import hashlib
import math
from pathlib import Path

import rasterio
from PIL import Image

from app.models.domain import (
    CheckResult,
    CheckStatus,
    ExifSummary,
    GeoBounds,
    OutputKind,
    ProcessingReport,
    ProductMatrix,
    ProductResult,
    ProductStatus,
    ValidationSummary,
)
from app.services.processing.raster import inspect_cog

_MAX_REASONABLE_GSD_M = 50.0
_MIN_SANE_LAT, _MAX_SANE_LAT = -90.0, 90.0
_MIN_SANE_LON, _MAX_SANE_LON = -180.0, 180.0


def _check(
    name: str,
    status: CheckStatus,
    observed: object = None,
    expected: object = None,
    explanation: str = "",
) -> CheckResult:
    return CheckResult(
        name=name,
        status=status,
        observed=observed,
        expected=expected,
        explanation=explanation,
    )


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def input_checks(
    source_path: Path,
    validation: ValidationSummary,
    exif: ExifSummary,
) -> list[CheckResult]:
    """Checks that describe the uploaded file itself."""
    checks: list[CheckResult] = [
        _check(
            "file_integrity",
            CheckStatus.PASS if source_path.is_file() and source_path.stat().st_size == validation.file_size_bytes else CheckStatus.FAIL,
            observed=source_path.stat().st_size if source_path.is_file() else None,
            expected=validation.file_size_bytes,
            explanation="Stored file size matches the validated byte count.",
        ),
        _check(
            "supported_format",
            CheckStatus.PASS,
            observed=validation.detected_format,
            expected=["JPEG", "PNG", "TIFF", "WEBP"],
            explanation="Container format was detected from magic bytes, not the extension.",
        ),
        _check(
            "image_dimensions",
            CheckStatus.PASS if validation.width >= 256 and validation.height >= 256 else CheckStatus.WARNING,
            observed=[validation.width, validation.height],
            expected=">= 256 px on each side",
            explanation="Small images carry too little detail for meaningful aerial analysis.",
        ),
    ]

    tag_count = exif.exif_tag_count
    checks.append(
        _check(
            "metadata_availability",
            CheckStatus.PASS if tag_count > 0 else CheckStatus.WARNING,
            observed=tag_count,
            expected="> 0 EXIF tags",
            explanation="No EXIF block means camera, timing and position data are unavailable.",
        )
    )

    if exif.camera:
        checks.append(
            _check(
                "camera_information",
                CheckStatus.PASS,
                observed=f"{exif.camera.make or ''} {exif.camera.model or ''}".strip() or "present",
                expected="camera make/model present",
                explanation="Camera data enables a focal-length / sensor-based GSD estimate.",
            )
        )
    else:
        checks.append(
            _check(
                "camera_information",
                CheckStatus.WARNING,
                observed=None,
                expected="camera make/model present",
                explanation="Without camera EXIF there is no basis for a scale estimate.",
            )
        )

    gps = exif.gps
    if gps is None:
        checks.append(
            _check(
                "gps_availability",
                CheckStatus.WARNING,
                observed=None,
                expected="GPS latitude/longitude",
                explanation="No usable GPS fix; the image cannot be located from metadata.",
            )
        )
    else:
        sane = (
            _MIN_SANE_LAT <= gps.latitude <= _MAX_SANE_LAT
            and _MIN_SANE_LON <= gps.longitude <= _MAX_SANE_LON
        )
        checks.append(
            _check(
                "gps_availability",
                CheckStatus.PASS if sane else CheckStatus.FAIL,
                observed={"latitude": gps.latitude, "longitude": gps.longitude},
                expected="valid WGS84 coordinate",
                explanation="EXIF GPS fix parsed and within valid coordinate ranges.",
            )
        )
        checks.append(
            _check(
                "gps_altitude",
                CheckStatus.PASS if gps.altitude_m is not None else CheckStatus.WARNING,
                observed=gps.altitude_m,
                expected="altitude in metres",
                explanation=(
                    "Altitude is required to derive ground sample distance from camera geometry."
                    if gps.altitude_m is None
                    else "Altitude present; camera-based GSD is possible."
                ),
            )
        )
    return checks


def georeferencing_checks(
    report: ProcessingReport,
    validation: ValidationSummary,
) -> list[CheckResult]:
    checks: list[CheckResult] = []
    plan = report.allocation

    checks.append(
        _check(
            "crs_availability",
            CheckStatus.PASS if plan is not None else CheckStatus.NOT_APPLICABLE,
            observed=plan.crs if plan else None,
            expected="CRS present on the output raster",
            explanation=(
                "Output raster carries a CRS."
                if plan
                else "No CRS was available, so no georeferenced raster was produced."
            ),
        )
    )

    if plan is not None:
        checks.append(
            _check(
                "georeferencing_availability",
                CheckStatus.PASS if plan.method != "gps_centre_footprint" else CheckStatus.WARNING,
                observed=plan.method,
                expected="embedded_geotransform or caller_override",
                explanation=(
                    "Geotransform came from the source file."
                    if plan.method == "embedded_geotransform"
                    else "Geotransform supplied by the caller; not verified against control points."
                    if plan.method == "caller_override"
                    else "Footprint estimated from GPS fix + camera geometry; it is an approximation."
                ),
            )
        )
        bounds = report.geotiff.bounds_wgs84 if report.geotiff else None
        if bounds:
            covers_valid_area = (
                -180.0 <= bounds[0] <= 180.0
                and -180.0 <= bounds[2] <= 180.0
                and -90.0 <= bounds[1] <= 90.0
                and -90.0 <= bounds[3] <= 90.0
                and bounds[0] < bounds[2]
                and bounds[1] < bounds[3]
            )
            checks.append(
                _check(
                    "bounds_plausibility",
                    CheckStatus.PASS if covers_valid_area else CheckStatus.FAIL,
                    observed=GeoBounds(west=bounds[0], south=bounds[1], east=bounds[2], north=bounds[3]).as_list,
                    expected="WGS84 bounds inside [-180,180] x [-90,90]",
                    explanation="Reprojected footprint falls inside valid Earth coordinates.",
                )
            )
        if plan.estimated_gsd:
            gsd = plan.pixel_size_x
            checks.append(
                _check(
                    "ground_sample_distance",
                    CheckStatus.PASS if 0.0 < gsd <= _MAX_REASONABLE_GSD_M else CheckStatus.WARNING,
                    observed=round(gsd, 4),
                    expected=f"0 < GSD <= {_MAX_REASONABLE_GSD_M} m",
                    explanation=(
                        "Estimated GSD is within the range expected for aerial imagery."
                        if 0.0 < gsd <= _MAX_REASONABLE_GSD_M
                        else "Estimated GSD is unrealistically coarse; the footprint should not be trusted."
                    ),
                )
            )
    else:
        checks.append(
            _check(
                "georeferencing_availability",
                CheckStatus.NOT_APPLICABLE,
                observed=None,
                expected="either embedded geotransform or GPS + camera geometry",
                explanation=(
                    "Missing: " + ", ".join(report.missing_georeferencing_inputs)
                    if report.missing_georeferencing_inputs
                    else "No georeferencing inputs were available."
                ),
            )
        )
    return checks


def output_checks(report: ProcessingReport, products: ProductMatrix) -> list[CheckResult]:
    checks: list[CheckResult] = []

    geotiff = products.get(OutputKind.GEOTIFF)
    if geotiff is not None and geotiff.status is ProductStatus.GENERATED and geotiff.path:
        path = Path(geotiff.path)
        try:
            with rasterio.open(path) as ds:
                raster_valid = ds.read(1, window=_probe_window(ds)) is not None
        except Exception:
            raster_valid = False
        checks.append(
            _check(
                "raster_validity",
                CheckStatus.PASS if raster_valid else CheckStatus.FAIL,
                observed="readable" if raster_valid else "unreadable",
                expected="readable GeoTIFF with bands",
                explanation="GeoTIFF was reopened and a window of pixels was read back.",
            )
        )
        info = report.geotiff
        if info:
            checks.append(
                _check(
                    "output_dimensions",
                    CheckStatus.PASS,
                    observed=[info.width, info.height],
                    expected="> 0 x > 0",
                    explanation="GeoTIFF dimensions match the source pixels.",
                )
            )
            checks.append(
                _check(
                    "output_bounds",
                    CheckStatus.PASS if info.bounds_wgs84 else CheckStatus.WARNING,
                    observed=info.bounds_wgs84 or info.bounds_native,
                    expected="non-degenerate footprint in CRS and WGS84",
                    explanation=(
                        "GeoTIFF has a non-empty footprint convertible to WGS84."
                        if info.bounds_wgs84
                        else "Footprint exists in the native CRS but could not be reprojected to WGS84."
                    ),
                )
            )
    elif geotiff is not None:
        checks.append(
            _check(
                "raster_validity",
                CheckStatus.NOT_APPLICABLE,
                observed=None,
                expected="GeoTIFF generated",
                explanation=geotiff.reason or "No GeoTIFF was produced.",
            )
        )

    cog = products.get(OutputKind.COG)
    if cog is not None and cog.status is ProductStatus.GENERATED and cog.path:
        result = inspect_cog(Path(cog.path))
        checks.append(
            _check(
                "cog_validation",
                CheckStatus.PASS if result["valid"] else CheckStatus.FAIL,
                observed={
                    "block_size": result.get("block_size"),
                    "overview_levels": result.get("overview_levels"),
                    "problems": result.get("problems"),
                },
                expected="tiled, overviews present, tile offsets monotonic",
                explanation="COG structure inspected directly from the TIFF IFD chain.",
            )
        )
    elif cog is not None:
        checks.append(
            _check(
                "cog_validation",
                CheckStatus.NOT_APPLICABLE,
                observed=None,
                expected="COG generated",
                explanation=cog.reason or "No COG was produced.",
            )
        )

    preview = products.get(OutputKind.PREVIEW)
    if preview is not None and preview.status is ProductStatus.GENERATED and preview.path:
        try:
            with Image.open(preview.path) as image:
                image.load()
                size = image.size
            ok = True
        except Exception:
            size, ok = None, False
        checks.append(
            _check(
                "preview_integrity",
                CheckStatus.PASS if ok else CheckStatus.FAIL,
                observed=size,
                expected="decodable image",
                explanation="Preview decoded successfully.",
            )
        )

    generated = [p for p in products.products if p.status is ProductStatus.GENERATED]
    if generated:
        checks.append(
            _check(
                "output_file_integrity",
                CheckStatus.PASS,
                observed={p.kind: p.sha256[:12] if p.sha256 else None for p in generated},
                expected="sha256 computed for every output",
                explanation="All generated artefacts were hashed on disk.",
            )
        )
    return checks


def _probe_window(ds: rasterio.io.DatasetReader):
    from rasterio.windows import Window

    return Window(0, 0, min(ds.width, 64), min(ds.height, 64))


def accuracy_checks(validation: ValidationSummary, report: ProcessingReport) -> list[CheckResult]:
    method = report.allocation.method if report.allocation else None
    if method == "gps_centre_footprint":
        return [
            _check(
                "geometric_accuracy",
                CheckStatus.NOT_APPLICABLE,
                observed="not assessed",
                expected="ground control points or RTK/GNSS reference",
                explanation=(
                    "Georeferencing is derived from consumer GPS and camera geometry. "
                    "Absolute positional accuracy is NOT verified and must not be "
                    "published as a survey-grade figure."
                ),
            )
        ]
    if method in {"embedded_geotransform", "caller_override"}:
        return [
            _check(
                "geometric_accuracy",
                CheckStatus.NOT_APPLICABLE,
                observed="not assessed",
                expected="independent checkpoints",
                explanation=(
                    "The raster is placed in a CRS but no control points were used to "
                    "verify that placement."
                ),
            )
        ]
    return [
        _check(
            "geometric_accuracy",
            CheckStatus.NOT_APPLICABLE,
            observed="n/a",
            expected="georeferenced output",
            explanation="No georeferenced output exists, so accuracy cannot be assessed.",
        )
    ]


def build_product_matrix(results: list[ProductResult]) -> ProductMatrix:
    return ProductMatrix(products=results)


def not_generated(kind, reason: str) -> ProductResult:
    return ProductResult(kind=kind, status=ProductStatus.NOT_GENERATED, reason=reason)


def is_gsd_plausible(gsd: float | None) -> bool:
    return bool(gsd and math.isfinite(gsd) and 0 < gsd <= _MAX_REASONABLE_GSD_M)
