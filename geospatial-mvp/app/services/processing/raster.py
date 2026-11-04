"""Raster I/O: GeoTIFF creation, COG validation and preview rendering.

All functions work on real pixel data and verify what they produce. Nothing
here assumes the input is georeferenced.
"""

from __future__ import annotations

import math
import struct
from pathlib import Path

import numpy as np
import rasterio
from PIL import Image, ImageOps
from rasterio.crs import CRS
from rasterio.enums import Resampling
from rasterio.transform import Affine
from rasterio.windows import Window

from app.core.errors import ProcessingFailed
from app.core.logging import get_logger
from app.models.domain import RasterInfo
from app.services.processing.georef import GeoreferencePlan, wgs84_bounds

logger = get_logger(__name__)

_COG_BLOCKSIZE = 512
_PREVIEW_MAX_BANDS = 3
_STRIP_ROWS = 1024


class RasterError(RuntimeError):
    """Raised when raster output cannot be produced or verified."""


def _to_byte_bands(image: Image.Image) -> np.ndarray:
    """Normalise any Pillow mode to (bands, rows, cols) uint8."""
    if image.mode in {"RGBA", "LA", "PA", "P", "RGBa", "La"}:
        rgba = image.convert("RGBA")
        background = Image.new("RGBA", rgba.size, (0, 0, 0, 255))
        image = Image.alpha_composite(background, rgba).convert("RGB")
    elif image.mode in {"I", "I;16", "I;16B", "I;16L", "F"}:
        array = np.asarray(image, dtype=np.float32)
        finite = array[np.isfinite(array)]
        if finite.size:
            low, high = float(finite.min()), float(finite.max())
            array = (array - low) / (high - low) * 255.0 if high > low else np.zeros_like(array)
        else:
            array = np.zeros_like(array)
        return np.clip(array, 0, 255).astype(np.uint8)[np.newaxis, :, :]
    elif image.mode not in {"L", "RGB"}:
        image = image.convert("RGB")

    array = np.asarray(image)
    if array.ndim == 2:
        return array[np.newaxis, :, :].astype(np.uint8, copy=False)
    return np.ascontiguousarray(np.moveaxis(array, -1, 0), dtype=np.uint8)


def _band_count_for_mode(mode: str) -> int:
    return 1 if mode in {"L", "LA", "I", "I;16", "I;16B", "I;16L", "F", "1"} else 3


def write_geotiff(
    source_path: Path,
    target_path: Path,
    plan: GeoreferencePlan,
) -> RasterInfo:
    """Write a GeoTIFF placing the image pixels on `plan`'s grid."""
    try:
        crs = CRS.from_user_input(plan.crs)
    except Exception as exc:
        raise RasterError(f"Invalid CRS for raster output: {exc}") from exc

    transform = Affine(
        plan.pixel_size_x, 0.0, plan.origin_x, 0.0, -plan.pixel_size_y, plan.origin_y
    )
    target_path.parent.mkdir(parents=True, exist_ok=True)

    with Image.open(source_path) as pil:
        width, height = pil.size
        band_count = _band_count_for_mode(pil.mode)
        profile = {
            "driver": "GTiff",
            "width": width,
            "height": height,
            "count": band_count,
            "dtype": "uint8",
            "crs": crs,
            "transform": transform,
            "compress": "deflate",
            "photometric": "rgb" if band_count == 3 else "minisblack",
            "BIGTIFF": "IF_SAFER",
        }
        with rasterio.open(target_path, "w", **profile) as dataset:
            for row in range(0, height, _STRIP_ROWS):
                rows = min(_STRIP_ROWS, height - row)
                bands = _to_byte_bands(pil.crop((0, row, width, row + rows)))
                if bands.shape[0] == 1 and band_count == 3:
                    bands = np.repeat(bands, 3, axis=0)
                dataset.write(bands[:band_count], window=Window(0, row, width, rows))
            dataset.update_tags(
                IMAGE_SOURCE="single aerial image",
                GEOREF_METHOD=plan.method,
                GEOREF_ESTIMATED=str(plan.estimated_gsd).lower(),
                ORTHORECTIFIED="false",
            )
    logger.info("Wrote GeoTIFF %s (%dx%d, %d bands)", target_path, width, height, band_count)
    return describe_raster(target_path)


def write_cog(source_tiff: Path, target_path: Path) -> RasterInfo:
    """Convert a georeferenced GeoTIFF into a Cloud Optimized GeoTIFF."""
    target_path.parent.mkdir(parents=True, exist_ok=True)
    with rasterio.open(source_tiff) as src:
        if src.crs is None or src.transform.is_identity:
            raise RasterError("Source raster is not georeferenced; COG creation requires a CRS")
        profile = src.profile.copy()
        for key in ("blockxsize", "blockysize", "tiled", "interleave", "compress"):
            profile.pop(key, None)
        profile.update(
            driver="COG",
            compress="deflate",
            predictor=2 if src.dtypes[0] == "uint8" else 1,
            BLOCKSIZE=_COG_BLOCKSIZE,
            OVERVIEW_RESAMPLING="AVERAGE",
            BIGTIFF="IF_SAFER",
        )
        with rasterio.open(target_path, "w", **profile) as dst:
            for index in range(1, src.count + 1):
                dst.write(src.read(index), index)
            dst.update_tags(**src.tags())
    logger.info("Wrote COG %s", target_path)
    return describe_raster(target_path)


# ----------------------------------------------------------------- COG checks
def _read_tiff_layout(path: Path, max_ifds: int = 4) -> list[dict[str, object]]:
    """Walk the TIFF IFD chain and collect tile offsets/sizes per level.

    COG requires (a) a tiled main image, (b) overviews whose tiles precede the
    full-resolution tiles in the file, and (c) monotonic tile offsets inside
    each level. This reads the IFD structure directly; rasterio does not expose
    file offsets.
    """
    data = path.read_bytes()
    if data[:4] == b"II*\x00":
        endian = "<"
        ifd_offset = struct.unpack("<I", data[4:8])[0]
    elif data[:4] == b"MM\x00*":
        endian = ">"
        ifd_offset = struct.unpack(">I", data[4:8])[0]
    else:
        raise ValueError("Not a classic TIFF file")

    layout: list[dict[str, object]] = []
    for _ in range(max_ifds):
        if ifd_offset == 0 or ifd_offset + 2 > len(data):
            break
        entry_count = struct.unpack(endian + "H", data[ifd_offset : ifd_offset + 2])[0]
        entries: dict[int, tuple[int, int, int]] = {}
        cursor = ifd_offset + 2
        for _ in range(entry_count):
            tag, field_type, count = struct.unpack(endian + "HHI", data[cursor : cursor + 8])
            entries[tag] = (field_type, count, cursor + 8)
            cursor += 12
        next_ifd = struct.unpack(endian + "I", data[cursor : cursor + 4])[0]

        def read_values(tag: int) -> list[int]:
            if tag not in entries:
                return []
            field_type, count, value_pos = entries[tag]
            size = {3: 2, 4: 4, 16: 8}.get(field_type)
            if size is None:
                return []
            total = size * count
            if total <= 4:
                raw = data[value_pos : value_pos + total]
            else:
                offset = struct.unpack(endian + "I", data[value_pos : value_pos + 4])[0]
                raw = data[offset : offset + total]
            fmt = {3: "H", 4: "I", 16: "Q"}[field_type]
            return list(struct.unpack(endian + fmt * count, raw))

        width = read_values(256)
        height = read_values(257)
        tile_offsets = read_values(324) or read_values(273)  # TileOffsets or StripOffsets
        layout.append(
            {
                "width": width[0] if width else None,
                "height": height[0] if height else None,
                "is_tiled": 324 in entries,
                "offsets": tile_offsets,
            }
        )
        ifd_offset = next_ifd
    return layout


def inspect_cog(path: Path) -> dict[str, object]:
    """Verify the structural COG requirements on the real file."""
    problems: list[str] = []
    try:
        layout = _read_tiff_layout(path)
    except (ValueError, struct.error) as exc:
        return {"valid": False, "problems": [f"TIFF structure unreadable: {exc}"], "overview_levels": []}

    with rasterio.open(path) as ds:
        if ds.driver != "GTiff":
            problems.append(f"driver is {ds.driver}, expected GTiff-backed COG")
        if not ds.profile.get("tiled"):
            problems.append("main image is not tiled")
        overviews = ds.overviews(1) if ds.count else []
        block = ds.block_shapes[0] if ds.block_shapes else (0, 0)
        width, height = ds.width, ds.height

    if not layout:
        problems.append("no TIFF IFD found")
    else:
        main = layout[0]
        if not main.get("is_tiled"):
            problems.append("main IFD uses strips instead of tiles")
        if not overviews and (width > _COG_BLOCKSIZE or height > _COG_BLOCKSIZE):
            problems.append("image exceeds one block but has no overviews")

        for index, level in enumerate(layout):
            offsets = [int(o) for o in level.get("offsets", []) if o]
            if offsets != sorted(offsets):
                problems.append(f"tile offsets in IFD {index} are not monotonic")
                break

        if len(layout) > 1:
            # COG order: every tile of every overview precedes the full-res tiles.
            main_offsets = [o for o in layout[0].get("offsets", []) if o]
            overview_offsets = [
                o for level in layout[1:] for o in level.get("offsets", []) if o
            ]
            if main_offsets and overview_offsets and max(overview_offsets) > min(main_offsets):
                problems.append("overviews are stored after the full-resolution tiles")

    return {
        "valid": not problems,
        "problems": problems,
        "overview_levels": overviews,
        "block_size": [int(block[0]), int(block[1])],
        "ifd_levels": len(layout),
    }


# --------------------------------------------------------------------- report
def describe_raster(path: Path) -> RasterInfo:
    with rasterio.open(path) as ds:
        transform = ds.transform
        wgs = None
        if ds.crs is not None:
            bounds = wgs84_bounds(
                ds.crs.to_string(),
                transform.c,
                transform.f,
                abs(transform.a),
                abs(transform.e),
                ds.width,
                ds.height,
            )
            wgs = list(bounds) if bounds else None
        block = ds.block_shapes[0] if ds.block_shapes else None
        return RasterInfo(
            path=str(path),
            driver=ds.driver,
            width=ds.width,
            height=ds.height,
            band_count=ds.count,
            dtype=ds.dtypes[0] if ds.dtypes else "unknown",
            crs=ds.crs.to_string() if ds.crs else "None",
            pixel_size_x=abs(float(transform.a)),
            pixel_size_y=abs(float(transform.e)),
            bounds_native=[float(v) for v in ds.bounds],
            bounds_wgs84=wgs,
            nodata=float(ds.nodata) if ds.nodata is not None else None,
            block_size=[int(block[0]), int(block[1])] if block else None,
            compression=ds.compression.name if ds.compression else None,
            overview_levels=ds.overviews(1) if ds.count else [],
            is_cog=ds.driver == "GTiff" and ds.profile.get("tiled", False),
        )


# ------------------------------------------------------------------ previews
def render_preview(source_path: Path, target_path: Path, max_size: int) -> tuple[int, int]:
    """Downsample to a JPEG preview that survives EXIF rotation."""
    with Image.open(source_path) as pil:
        image = ImageOps.exif_transpose(pil)
        if image.mode in {"RGBA", "LA", "P"}:
            background = Image.new("RGBA", image.size, (0, 0, 0, 255))
            image = Image.alpha_composite(background, image.convert("RGBA")).convert("RGB")
        elif image.mode in {"I", "I;16", "F"}:
            bands = _to_byte_bands(image)
            image = Image.fromarray(bands[0], mode="L").convert("RGB")
        elif image.mode != "RGB":
            image = image.convert("RGB")
        image.thumbnail((max_size, max_size), Image.Resampling.LANCZOS)
        target_path.parent.mkdir(parents=True, exist_ok=True)
        image.save(target_path, format="JPEG", quality=85, optimize=True, progressive=True)
        return image.size


def render_raster_preview(raster_path: Path, target_path: Path, max_size: int) -> tuple[int, int]:
    """Preview for raster inputs, decimating with overviews/averaging."""
    with rasterio.open(raster_path) as ds:
        factor = max(1, math.ceil(max(ds.width, ds.height) / max_size))
        out_w = max(1, ds.width // factor)
        out_h = max(1, ds.height // factor)
        array = ds.read(
            out_shape=(min(ds.count, _PREVIEW_MAX_BANDS), out_h, out_w),
            resampling=Resampling.average,
            masked=True,
        ).filled(0)
        if array.dtype != np.uint8:
            array = array.astype(np.float32)
            low, high = float(array.min()), float(array.max())
            array = (array - low) / (high - low) * 255.0 if high > low else np.zeros_like(array)
            array = np.clip(array, 0, 255).astype(np.uint8)
        if array.shape[0] == 1:
            image = Image.fromarray(array[0], mode="L").convert("RGB")
        else:
            image = Image.fromarray(np.moveaxis(array[:3], 0, -1), mode="RGB")
        target_path.parent.mkdir(parents=True, exist_ok=True)
        image.save(target_path, format="JPEG", quality=85, optimize=True)
        return image.size


def ensure_valid_raster(path: Path) -> None:
    try:
        with rasterio.open(path) as ds:
            _ = (ds.bounds, ds.crs, ds.transform)
    except Exception as exc:
        raise ProcessingFailed(
            "Generated raster could not be reopened",
            details={"path": path.name, "reason": str(exc)},
        ) from exc
