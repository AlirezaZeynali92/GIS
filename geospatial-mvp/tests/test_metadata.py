"""Metadata extraction: GPS, camera, embedded raster georeferencing."""

from __future__ import annotations

from pathlib import Path

import pytest
from PIL import Image
from PIL.TiffImagePlugin import IFDRational

from app.services.metadata.extractor import MetadataExtractor, sniff_image_format


def test_gps_is_parsed_from_exif(sample_dir: Path):
    summary, details, warnings = MetadataExtractor().extract_summary(sample_dir / "aerial_gps.jpg")
    assert summary.gps is not None
    assert summary.gps.latitude == pytest.approx(35.6892, abs=1e-4)
    assert summary.gps.longitude == pytest.approx(51.3890, abs=1e-4)
    assert summary.gps.altitude_m == pytest.approx(120.0)
    assert warnings == []
    assert details["exif"]["GPSInfo"]


def test_camera_fields_are_parsed(sample_dir: Path):
    summary, _, _ = MetadataExtractor().extract_summary(sample_dir / "aerial_gps.jpg")
    assert summary.camera is not None
    assert summary.camera.make == "SampleCam"
    assert summary.camera.focal_length_mm == pytest.approx(35.0)
    assert summary.exif_tag_count > 0


def test_image_without_metadata_yields_no_gps(sample_dir: Path):
    summary, details, _ = MetadataExtractor().extract_summary(sample_dir / "aerial_no_geo.png")
    assert summary.gps is None
    assert summary.camera is None
    assert details["exif"] == {}


def test_embedded_geotiff_is_detected(sample_dir: Path):
    _, details, _ = MetadataExtractor().extract_summary(sample_dir / "aerial_embedded.tif")
    geotiff = details["geotiff"]
    assert geotiff is not None
    assert geotiff["crs"] == "EPSG:32639"
    assert geotiff["pixel_size_x"] == pytest.approx(0.25)


def test_southern_and_western_refs_are_negative(tmp_path: Path):
    """Write a fix in the southern/western hemisphere and confirm sign handling."""
    image = Image.new("RGB", (64, 64), (10, 20, 30))
    exif = Image.Exif()
    exif[0x8825] = {
        1: "S",
        2: (IFDRational(33, 1), IFDRational(51, 1), IFDRational(0, 1)),
        3: "W",
        4: (IFDRational(70, 1), IFDRational(30, 1), IFDRational(0, 1)),
        6: IFDRational(800, 1),
    }
    target = tmp_path / "south.jpg"
    image.save(target, exif=exif)

    summary, _, _ = MetadataExtractor().extract_summary(target)
    assert summary.gps is not None
    assert summary.gps.latitude < 0
    assert summary.gps.longitude < 0


def test_out_of_range_gps_is_discarded(tmp_path: Path):
    image = Image.new("RGB", (64, 64), (1, 2, 3))
    exif = Image.Exif()
    exif[0x8825] = {
        1: "N",
        2: (IFDRational(200, 1), IFDRational(0, 1), IFDRational(0, 1)),
        3: "E",
        4: (IFDRational(10, 1), IFDRational(0, 1), IFDRational(0, 1)),
    }
    target = tmp_path / "bad.jpg"
    image.save(target, exif=exif)
    summary, _, warnings = MetadataExtractor().extract_summary(target)
    assert summary.gps is None
    assert any("does not contain a usable lat/lon" in w for w in warnings)


@pytest.mark.parametrize(
    ("filename", "expected"),
    [
        ("aerial_gps.jpg", "JPEG"),
        ("aerial_no_geo.png", "PNG"),
        ("aerial_embedded.tif", "TIFF"),
    ],
)
def test_format_sniffing_uses_magic_bytes(sample_dir: Path, filename: str, expected: str):
    assert sniff_image_format(sample_dir / filename) == expected
