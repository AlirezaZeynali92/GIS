"""Generate a set of synthetic drone-style aerial tiles for a block of Qeshm.

These are procedurally drawn stand-ins for orthophoto tiles (fields, roads,
buildings, coastline) and carry EXIF GPS, altitude and focal length so the
georeferencing path can be exercised. They are not real photographs.

Usage:
    python scripts/make_drone_tiles.py [output_dir]
"""

from __future__ import annotations

import math
import random
import sys
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw
from PIL.TiffImagePlugin import IFDRational

from make_samples import _dms_rational  # reuse the DMS helper

# Approximate centre of Qeshm island.
CENTRE_LAT = 26.9550
CENTRE_LON = 55.9000

ALTITUDE_M = 120.0
FOCAL_MM = 35.0
SENSOR_WIDTH_MM = 36.0
IMAGE_W, IMAGE_H = 1600, 1200
TILE_STEP_M = 90.0  # ground spacing between tile centres, below the footprint width

# Metres per degree; longitude scales with cos(latitude).
M_PER_DEG_LAT = 111_320.0


def _m_to_deg(dx_m: float, dy_m: float, lat: float) -> tuple[float, float]:
    dlat = dy_m / M_PER_DEG_LAT
    dlon = dx_m / (M_PER_DEG_LAT * math.cos(math.radians(lat)))
    return dlat, dlon


def _gps_ifd(lat: float, lon: float, alt: float) -> dict[int, object]:
    return {
        1: "N" if lat >= 0 else "S",
        2: _dms_rational(abs(lat)),
        3: "E" if lon >= 0 else "W",
        4: _dms_rational(abs(lon)),
        5: 0,
        6: IFDRational(alt, 1),
        7: _dms_rational(90),
        29: "WGS-84",
    }


def _exif(lat: float, lon: float) -> Image.Exif:
    exif = Image.Exif()
    exif[0x010F] = "SampleCam"
    exif[0x0110] = "SC-4K PRO"
    exif[0x0131] = "geospatial-mvp"
    exif[0x0112] = 1
    exif[0x0132] = "2024:06:01 09:15:00"
    exif[0x8769] = {
        0x829A: IFDRational(1, 500),
        0x829D: IFDRational(28, 10),
        0x8827: 200,
        0x920A: IFDRational(int(FOCAL_MM), 1),
        0x9003: "2024:06:01 09:15:00",
    }
    exif[0x8825] = _gps_ifd(lat, lon, ALTITUDE_M)
    return exif


def _render_tile(seed: int) -> Image.Image:
    """Draw a nadir-looking scene: sea on one side, fields, a road and buildings."""
    rng = random.Random(seed)
    img = Image.new("RGB", (IMAGE_W, IMAGE_H), (196, 178, 132))
    draw = ImageDraw.Draw(img)

    # Sea along the top edge with a wavy shoreline.
    shore = IMAGE_H // 5 + rng.randint(-40, 40)
    for x in range(0, IMAGE_W, 4):
        y = shore + int(25 * math.sin(x / 90.0 + seed))
        draw.rectangle([x, 0, x + 4, y], fill=(58, 112, 160))

    # Field patches.
    for _ in range(90):
        x0 = rng.randint(0, IMAGE_W - 160)
        y0 = rng.randint(shore, IMAGE_H - 120)
        w, h = rng.randint(60, 160), rng.randint(40, 110)
        shade = rng.choice([(110, 150, 70), (140, 170, 90), (170, 150, 90)])
        draw.rectangle([x0, y0, x0 + w, y0 + h], fill=shade)

    # Road and buildings.
    road_y = shore + rng.randint(200, 300)
    draw.rectangle([0, road_y, IMAGE_W, road_y + 26], fill=(80, 80, 80))
    for _ in range(40):
        bx, by = rng.randint(20, IMAGE_W - 60), rng.randint(shore + 40, IMAGE_H - 40)
        draw.rectangle([bx, by, bx + rng.randint(14, 34), by + rng.randint(14, 30)], fill=(150, 90, 70))

    rng_noise = np.random.default_rng(seed)
    arr = np.asarray(img).astype(np.int16) + rng_noise.integers(-6, 7, size=(IMAGE_H, IMAGE_W, 3))
    return Image.fromarray(np.clip(arr, 0, 255).astype(np.uint8))


def generate(out_dir: Path, count: int = 4) -> list[Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    paths: list[Path] = []
    cols = max(1, int(math.ceil(math.sqrt(count))))
    for index in range(count):
        row, col = divmod(index, cols)
        dx = (col - (cols - 1) / 2) * TILE_STEP_M
        dy = (row - (cols - 1) / 2) * TILE_STEP_M
        dlat, dlon = _m_to_deg(dx, dy, CENTRE_LAT)
        lat, lon = CENTRE_LAT + dlat, CENTRE_LON + dlon
        path = out_dir / f"qeshm_tile_{index + 1:02d}.jpg"
        _render_tile(seed=index + 1).save(path, "JPEG", quality=92, exif=_exif(lat, lon).tobytes())
        paths.append(path)
    return paths


if __name__ == "__main__":
    target = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("data/samples/qeshm")
    for p in generate(target):
        print(p)
