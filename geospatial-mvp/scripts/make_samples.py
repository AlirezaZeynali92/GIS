"""Generate deterministic sample images for tests and the demo.

These are synthetic renders, not real photographs; ExifTool/Pillow writes the
EXIF we need. They exist so the pipeline can be exercised with known truth:
the GPS fix written here is the value the pipeline must recover.

Usage:
    python scripts/make_samples.py [output_dir]
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw
from PIL.TiffImagePlugin import IFDRational

# The location we stamp into the samples (Tehran outskirts, a common demo area).
SAMPLE_LAT = 35.6892
SAMPLE_LON = 51.3890
SAMPLE_ALT = 120.0
SAMPLE_FOCAL_MM = 35.0


def _dms_rational(value: float) -> tuple[IFDRational, IFDRational, IFDRational]:
    degrees = int(value)
    minutes_float = (value - degrees) * 60
    minutes = int(minutes_float)
    seconds = (minutes_float - minutes) * 60
    return (
        IFDRational(degrees, 1),
        IFDRational(minutes, 1),
        IFDRational(round(seconds * 10_000), 10_000),
    )


def _gps_ifd(lat: float, lon: float, alt: float) -> dict[int, object]:
    return {
        1: "N" if lat >= 0 else "S",
        2: _dms_rational(abs(lat)),
        3: "E" if lon >= 0 else "W",
        4: _dms_rational(abs(lon)),
        5: 0,  # below sea level
        6: IFDRational(alt, 1),
        7: _dms_rational(90),
        29: "WGS-84",
    }


def _exif(lat: float | None, lon: float | None, alt: float | None, focal: float) -> Image.Exif:
    exif = Image.Exif()
    exif[0x010F] = "SampleCam"          # Make
    exif[0x0110] = "SC-4K PRO"          # Model
    exif[0x0131] = "geospatial-mvp"     # Software
    exif[0x0112] = 1                    # Orientation
    exif[0x0132] = "2024:06:01 09:15:00"  # DateTime
    # Exif IFD
    exif[0x8769] = {
        0x9003: "2024:06:01 09:15:00",  # DateTimeOriginal
        0x920A: IFDRational(int(focal * 100), 100),  # FocalLength
        0x829D: IFDRational(28, 10),    # FNumber
        0x829A: IFDRational(1, 500),    # ExposureTime
        0x8827: 200,                    # ISO
    }
    if lat is not None and lon is not None:
        exif[0x8825] = _gps_ifd(lat, lon, alt if alt is not None else 0.0)
    return exif


def _aerial_scene(width: int, height: int, seed: int = 7) -> Image.Image:
    rng = np.random.default_rng(seed)
    base = rng.normal(120, 18, (height, width, 3)).clip(0, 255)
    image = Image.fromarray(base.astype(np.uint8), "RGB")
    draw = ImageDraw.Draw(image)
    # Roads, blocks and parcels so the preview is recognisable as an aerial scene.
    for _ in range(14):
        x0, y0 = rng.integers(0, width - 40), rng.integers(0, height - 40)
        w, h = rng.integers(40, width // 4), rng.integers(40, height // 4)
        colour = tuple(int(c) for c in rng.integers(60, 200, 3))
        draw.rectangle([x0, y0, x0 + w, y0 + h], fill=colour)
    draw.line([(0, height // 2), (width, height // 2)], fill=(90, 90, 90), width=8)
    draw.line([(width // 3, 0), (width // 3, height)], fill=(90, 90, 90), width=6)
    return image


def make_georeferenced_jpeg(target: Path, width: int = 1600, height: int = 1200) -> Path:
    """Aerial JPEG carrying a GPS fix with altitude and focal length."""
    scene = _aerial_scene(width, height)
    target.parent.mkdir(parents=True, exist_ok=True)
    scene.save(
        target,
        format="JPEG",
        quality=92,
        exif=_exif(SAMPLE_LAT, SAMPLE_LON, SAMPLE_ALT, SAMPLE_FOCAL_MM),
    )
    return target


def make_plain_png(target: Path, width: int = 1200, height: int = 900) -> Path:
    """Same scene without any EXIF or georeferencing."""
    target.parent.mkdir(parents=True, exist_ok=True)
    _aerial_scene(width, height, seed=11).save(target, format="PNG")
    return target


def make_embedded_tiff(target: Path, width: int = 1024, height: int = 768) -> Path:
    """A GeoTIFF that already carries CRS and transform (the strongest case)."""
    import rasterio
    from rasterio.crs import CRS
    from rasterio.transform import from_origin

    target.parent.mkdir(parents=True, exist_ok=True)
    scene = _aerial_scene(width, height, seed=3)
    array = np.moveaxis(np.asarray(scene), -1, 0)
    with rasterio.open(
        target,
        "w",
        driver="GTiff",
        width=width,
        height=height,
        count=3,
        dtype="uint8",
        crs=CRS.from_epsg(32639),
        transform=from_origin(430000.0, 3950000.0, 0.25, 0.25),
    ) as ds:
        ds.write(array)
    return target


def main(argv: list[str]) -> int:
    out_dir = Path(argv[1]) if len(argv) > 1 else Path(__file__).resolve().parents[1] / "data" / "samples"
    created = [
        make_georeferenced_jpeg(out_dir / "aerial_gps.jpg"),
        make_plain_png(out_dir / "aerial_no_geo.png"),
        make_embedded_tiff(out_dir / "aerial_embedded_geotiff.tif"),
    ]
    for path in created:
        print(f"{path} ({path.stat().st_size} bytes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
