"""Georeferencing decisions and raster generation."""

from __future__ import annotations

from pathlib import Path

import pytest
import rasterio
from PIL import Image

from app.models.domain import CameraInfo, ExifSummary, GeoTransformOverride, GpsFix
from app.services.metadata.extractor import MetadataExtractor
from app.services.processing.georef import (
    estimate_gsd_from_camera,
    plan_from_gps,
    resolve_georeferencing,
    wgs84_bounds,
)
from app.services.processing.raster import (
    inspect_cog,
    render_preview,
    write_cog,
    write_geotiff,
)


def _exif(lat: float = 35.0, lon: float = 51.0, alt: float | None = 120.0, focal: float = 35.0) -> ExifSummary:
    return ExifSummary(
        gps=GpsFix(latitude=lat, longitude=lon, altitude_m=alt),
        camera=CameraInfo(make="X", model="Y", focal_length_mm=focal),
    )


def test_gsd_formula():
    # 120 m altitude, 36 mm sensor, 35 mm lens, 1600 px wide -> 0.0771 m/px
    gsd = estimate_gsd_from_camera(35.0, 120.0, 36.0, 1600)
    assert gsd == pytest.approx(0.077142857, rel=1e-6)


def test_gsd_needs_positive_inputs():
    assert estimate_gsd_from_camera(0, 100, 36, 1000) is None
    assert estimate_gsd_from_camera(35, -5, 36, 1000) is None


def test_gps_without_altitude_cannot_georeference():
    plan, missing, _ = plan_from_gps(_exif(alt=None), 100, 80)
    assert plan is None
    assert "GPS altitude (needed to derive ground sample distance)" in missing


def test_gps_without_focal_length_cannot_georeference():
    exif = ExifSummary(gps=GpsFix(latitude=1, longitude=2, altitude_m=50), camera=None)
    plan, missing, _ = plan_from_gps(exif, 100, 80)
    assert plan is None
    assert any("focal length" in m for m in missing)


def test_centre_footprint_is_centred_on_the_fix():
    plan, _, notes = plan_from_gps(_exif(), 1600, 1200)
    assert plan is not None
    assert plan.method == "gps_centre_footprint"
    assert plan.estimated_gsd is True
    assert plan.crs.startswith("EPSG:326")
    bounds = wgs84_bounds(
        plan.crs, plan.origin_x, plan.origin_y, plan.pixel_size_x, plan.pixel_size_y, 1600, 1200
    )
    assert bounds is not None
    assert (bounds[1] + bounds[3]) / 2 == pytest.approx(35.0, abs=1e-6)
    assert (bounds[0] + bounds[2]) / 2 == pytest.approx(51.0, abs=1e-6)
    assert any("ESTIMATE" in n for n in notes)


def test_override_is_preferred():
    override = GeoTransformOverride(
        crs="EPSG:32636", origin_x=500000, origin_y=3500000, pixel_size_x=0.5, pixel_size_y=0.5
    )
    decision = resolve_georeferencing(ExifSummary(), {}, 100, 100, override=override)
    assert decision.plan is not None
    assert decision.plan.method == "caller_override"
    assert decision.plan.estimated_gsd is False


def test_geographic_override_is_rejected():
    override = GeoTransformOverride(
        crs="EPSG:4326",
        origin_x=51.3,
        origin_y=35.6,
        pixel_size_x=0.00001,
        pixel_size_y=0.00001,
    )
    decision = resolve_georeferencing(ExifSummary(), {}, 100, 100, override=override)
    assert decision.plan is None
    assert any("projected CRS" in m for m in decision.missing_inputs)


def test_no_inputs_reports_everything_missing():
    decision = resolve_georeferencing(ExifSummary(), {}, 100, 100)
    assert decision.plan is None
    assert "GPS coordinates" in decision.missing_inputs
    assert "EXIF focal length" in decision.missing_inputs


def test_embedded_geotransform_is_used(sample_dir: Path):
    summary, details, _ = MetadataExtractor().extract_summary(sample_dir / "aerial_embedded.tif")
    decision = resolve_georeferencing(summary, details, 1024, 768)
    assert decision.plan is not None
    assert decision.plan.method == "embedded_geotransform"


def test_geotiff_conversion_preserves_pixels(sample_dir: Path, tmp_path: Path):
    plan, _, _ = plan_from_gps(_exif(), 1600, 1200)
    target = tmp_path / "out.tif"
    info = write_geotiff(sample_dir / "aerial_gps.jpg", target, plan)

    assert (info.width, info.height) == (1600, 1200)
    assert info.crs.startswith("EPSG:326")
    assert info.band_count == 3
    with rasterio.open(target) as ds:
        assert ds.crs is not None
        assert ds.count == 3
        assert ds.read(1).std() > 0  # real pixel variance, not a blank raster


def test_cog_is_valid_and_plain_tiff_is_not(sample_dir: Path, tmp_path: Path):
    plan, _, _ = plan_from_gps(_exif(), 1600, 1200)
    geotiff = tmp_path / "plain.tif"
    write_geotiff(sample_dir / "aerial_gps.jpg", geotiff, plan)
    cog = tmp_path / "out_cog.tif"
    info = write_cog(geotiff, cog)

    assert info.driver == "GTiff"
    result = inspect_cog(cog)
    assert result["valid"] is True, result["problems"]
    assert result["block_size"] == [512, 512]
    assert result["overview_levels"] == [2, 4]

    assert inspect_cog(geotiff)["valid"] is False


def test_preview_is_downsampled(sample_dir: Path, tmp_path: Path):
    target = tmp_path / "preview.jpg"
    size = render_preview(sample_dir / "aerial_gps.jpg", target, 512)
    assert max(size) <= 512
    assert target.is_file()


def test_grayscale_png_converts_to_one_band(sample_dir: Path, tmp_path: Path):
    gray = tmp_path / "gray.png"
    Image.open(sample_dir / "aerial_no_geo.png").convert("L").save(gray)
    plan, _, _ = plan_from_gps(_exif(), 1200, 900)
    info = write_geotiff(gray, tmp_path / "gray.tif", plan)
    assert info.band_count == 1
