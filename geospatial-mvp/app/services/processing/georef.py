"""Deciding how, and whether, an image can be placed in ground coordinates.

This module is deliberately conservative. A single aerial image provides at
most a camera position, not a ground footprint. The only defensible ways to
produce a georeferenced raster are:

1. ``embedded_geotransform`` — the file already carries CRS + affine transform.
2. ``caller_override``      — the caller explicitly supplies CRS + placement.
3. ``gps_centre_footprint`` — a GPS fix plus a *derived* GSD from camera
   focal length, sensor width and altitude, assuming a nadir image.

Case 3 is an estimate, never a survey-grade footprint, and the output says so.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

from pyproj import CRS, Transformer

from app.models.domain import Allocation, ExifSummary, GeoTransformOverride

# 35 mm full-frame diagonal in mm (36 x 24 sensor) — used only for the
# focal-length-only fallback, which yields an unknown real GSD.
FULL_FRAME_DIAGONAL_MM = math.hypot(36.0, 24.0)

_METERS_PER_DEGREE_EQUATOR = 111_319.49079327357


@dataclass
class GeoreferencePlan:
    method: str
    crs: str
    origin_x: float
    origin_y: float
    pixel_size_x: float
    pixel_size_y: float
    notes: list[str] = field(default_factory=list)
    estimated_gsd: bool = False

    def to_allocation(self) -> Allocation:
        return Allocation(
            method=self.method,
            crs=self.crs,
            pixel_size_x=self.pixel_size_x,
            pixel_size_y=self.pixel_size_y,
            origin_x=self.origin_x,
            origin_y=self.origin_y,
            estimated_gsd=self.estimated_gsd,
            notes=list(self.notes),
        )


@dataclass
class GeoreferenceDecision:
    plan: GeoreferencePlan | None
    missing_inputs: list[str]
    notes: list[str]


def _validate_crs(value: str) -> CRS:
    try:
        return CRS.from_user_input(value)
    except Exception as exc:
        raise ValueError(f"Unrecognised CRS '{value}': {exc}") from exc


def plan_from_override(override: GeoTransformOverride, width: int, height: int) -> GeoreferencePlan:
    """Place the image using caller-provided CRS and upper-left pixel origin."""
    crs = _validate_crs(override.crs)
    if crs.is_geographic:
        # Centimetre-scale GSD in degrees is meaningless; demand a projected CRS.
        raise ValueError(
            "Geographic CRS requested for an override; supply a projected CRS (metres) instead"
        )
    if not (crs.is_projected):
        raise ValueError("Override CRS must be a projected CRS")
    return GeoreferencePlan(
        method="caller_override",
        crs=crs.to_string(),
        origin_x=override.origin_x,
        origin_y=override.origin_y,
        pixel_size_x=override.pixel_size_x,
        pixel_size_y=override.pixel_size_y,
        notes=[
            "Placement supplied by the caller; not verified against ground control.",
            f"Raster extent covers {width * override.pixel_size_x:.1f} x "
            f"{height * override.pixel_size_y:.1f} CRS units.",
        ],
        estimated_gsd=False,
    )


def plan_from_embedded(geotiff: dict[str, Any]) -> GeoreferencePlan | None:
    """Use CRS + transform already stored in the raster file."""
    if not geotiff or not geotiff.get("crs"):
        return None
    pixel_size_x = abs(float(geotiff["pixel_size_x"]))
    pixel_size_y = abs(float(geotiff["pixel_size_y"]))
    if pixel_size_x <= 0 or pixel_size_y <= 0:
        return None
    return GeoreferencePlan(
        method="embedded_geotransform",
        crs=str(geotiff["crs"]),
        origin_x=float(geotiff["origin_x"]),
        origin_y=float(geotiff["origin_y"]),
        pixel_size_x=pixel_size_x,
        pixel_size_y=pixel_size_y,
        notes=[
            "CRS and affine transform were read from the source file.",
            "Resolution and georeferencing are limited to whatever the producer stored.",
        ],
        estimated_gsd=False,
    )


def _local_utm_epsg(latitude: float, longitude: float) -> int:
    zone = int((longitude + 180.0) / 6.0) + 1
    zone = min(max(zone, 1), 60)
    return (32600 if latitude >= 0 else 32700) + zone


def estimate_gsd_from_camera(
    focal_length_mm: float,
    altitude_m: float,
    sensor_width_mm: float | None,
    image_width_px: int,
) -> float | None:
    """Nadir GSD in metres/pixel: GSD = altitude * sensor_width / (focal_length * image_width)."""
    if focal_length_mm <= 0 or altitude_m <= 0:
        return None
    if not sensor_width_mm or sensor_width_mm <= 0 or image_width_px <= 0:
        return None
    return (altitude_m * sensor_width_mm) / (focal_length_mm * image_width_px)



def plan_from_gps(
    exif: ExifSummary,
    width: int,
    height: int,
    sensor_width_mm: float | None = None,
) -> tuple[GeoreferencePlan | None, list[str], list[str]]:
    """Centre the image on its GPS fix using a camera-geometry GSD estimate."""
    missing: list[str] = []
    notes: list[str] = []

    gps = exif.gps
    camera = exif.camera
    focal = camera.focal_length_mm if camera else None

    if gps is None:
        missing.append("GPS coordinates")
    elif gps.altitude_m is None:
        missing.append("GPS altitude (needed to derive ground sample distance)")
    if focal is None:
        missing.append("EXIF focal length")

    if missing:
        notes.append(
            "A GPS position alone fixes only the camera location. Without altitude and "
            "focal length the ground footprint cannot be estimated, and a raster "
            "georeferenced at the pixel centre would misstate its location."
        )
        return None, missing, notes

    altitude = float(gps.altitude_m)
    focal = float(focal)  # type: ignore[arg-type]
    gsd = None
    if sensor_width_mm:
        gsd = estimate_gsd_from_camera(focal, altitude, sensor_width_mm, width)
    if gsd is None:
        # No sensor size: use 35 mm-equivalent assumptions, flagged as approximate.
        gsd = estimate_gsd_from_camera(focal, altitude, 36.0, width)
        if gsd is not None:
            notes.append(
                "Sensor width unknown; GSD estimated assuming a 36 mm full-frame sensor. "
                "This can be off by 2x or more for other sensors.",
            )

    if gsd is None or gsd <= 0:
        missing.append("Usable GSD inputs")
        return None, missing, notes

    epsg = _local_utm_epsg(gps.latitude, gps.longitude)
    crs = CRS.from_epsg(epsg)
    transformer = Transformer.from_crs(CRS.from_epsg(4326), crs, always_xy=True)
    centre_x, centre_y = transformer.transform(gps.longitude, gps.latitude)

    origin_x = centre_x - (width / 2.0) * gsd
    origin_y = centre_y + (height / 2.0) * gsd

    notes.extend(
        [
            "Georeferencing is an ESTIMATE: the image was centred on the EXIF GPS fix "
            "and the extent derived from focal length + altitude.",
            f"Assumed nadir view with the principal point at the image centre; "
            f"UTM zone EPSG:{epsg} chosen from the GPS position.",
            "Camera GPS is typically the antenna position, and absolute accuracy is "
            "limited by the receiver (often 5-20 m) plus altitude error.",
            "GeoTIFF is georeferenced, NOT orthorectified: terrain relief, lens "
            "distortion and camera tilt are not corrected.",
        ]
    )
    return (
        GeoreferencePlan(
            method="gps_centre_footprint",
            crs=crs.to_string(),
            origin_x=origin_x,
            origin_y=origin_y,
            pixel_size_x=gsd,
            pixel_size_y=gsd,
            notes=notes,
            estimated_gsd=True,
        ),
        missing,
        notes,
    )


def resolve_georeferencing(
    exif: ExifSummary,
    metadata_details: dict[str, Any],
    width: int,
    height: int,
    override: GeoTransformOverride | None = None,
    sensor_width_mm: float | None = None,
) -> GeoreferenceDecision:
    """Pick the strongest available georeferencing method, in order of trust."""
    notes: list[str] = []

    if override is not None:
        try:
            plan = plan_from_override(override, width, height)
        except ValueError as exc:
            return GeoreferenceDecision(
                plan=None, missing_inputs=[str(exc)], notes=[str(exc)]
            )
        return GeoreferenceDecision(plan=plan, missing_inputs=[], notes=plan.notes)

    embedded = plan_from_embedded(metadata_details.get("geotiff") or {})
    if embedded is not None:
        return GeoreferenceDecision(plan=embedded, missing_inputs=[], notes=embedded.notes)

    plan, missing, gps_notes = plan_from_gps(exif, width, height, sensor_width_mm)
    notes.extend(gps_notes)
    if plan is None:
        notes.extend(
            [
                "No embedded geotransform and no usable camera/GPS geometry: the "
                "GeoTIFF and COG products cannot be produced honestly.",
                "Supply an explicit placement (CRS + origin + pixel size) when the "
                "ground position of the upper-left pixel is known.",
            ]
        )
    return GeoreferenceDecision(plan=plan, missing_inputs=missing, notes=notes)


def wgs84_bounds(
    crs_value: str,
    origin_x: float,
    origin_y: float,
    pixel_x: float,
    pixel_y: float,
    width: int,
    height: int,
) -> tuple[float, float, float, float] | None:
    """Reproject the raster corners to WGS84 and return west, south, east, north."""
    try:
        source = CRS.from_user_input(crs_value)
        transformer = Transformer.from_crs(source, CRS.from_epsg(4326), always_xy=True)
    except Exception:
        return None
    corners = [
        (origin_x, origin_y),
        (origin_x + width * pixel_x, origin_y),
        (origin_x, origin_y - height * pixel_y),
        (origin_x + width * pixel_x, origin_y - height * pixel_y),
    ]
    lons, lats = transformer.transform([c[0] for c in corners], [c[1] for c in corners])
    if any(math.isnan(v) or math.isinf(v) for v in [*lons, *lats]):
        return None
    return (min(lons), min(lats), max(lons), max(lats))
