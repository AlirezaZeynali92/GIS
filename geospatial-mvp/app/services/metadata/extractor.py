"""EXIF, XMP and embedded georeferencing extraction.

Only values that are actually present in the file are returned. Missing data
stays ``None`` — the pipeline never fabricates coordinates, CRS or resolution.
"""

from __future__ import annotations

import math
import struct
from pathlib import Path
from typing import Any

from PIL import ExifTags, Image, UnidentifiedImageError

from app.core.errors import InvalidImage
from app.core.logging import get_logger
from app.models.domain import CameraInfo, ExifSummary, GpsFix

logger = get_logger(__name__)

# Pillow's TIFF/EXIF tag ids used below.
_TAG_GPSINFO = 0x8825
_TAG_FOCAL_LENGTH = 0x920A
_TAG_FOCAL_LENGTH_35MM = 0xA405
_TAG_EXPOSURE = 0x829A
_TAG_FNUMBER = 0x829D
_TAG_ISO = 0x8827
_TAG_LENS_MODEL = 0xA434
_TAG_SOFTWARE = 0x0131

_XMP_NAMESPACE = b"http://ns.adobe.com/xap/1.0/\x00"
_GEOKEY_TIFF = b"GeoKeyDirectoryTag"
_TIFF_HEADER = b"II*\x00"


def _jsonable(value: Any) -> Any:
    """EXIF values include IFD objects, rationals and bytes; make them JSON-safe."""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    if isinstance(value, tuple):
        return [_jsonable(v) for v in value]
    if isinstance(value, (int, float, str, bool)) or value is None:
        return value
    if hasattr(value, "numerator") and hasattr(value, "denominator"):
        return float(value)
    if hasattr(value, "items"):
        return {str(k): _jsonable(v) for k, v in value.items()}
    return str(value)


def _rational(value: Any) -> float | None:
    """EXIF rationals arrive as IFDRational, (num, den) tuples or plain numbers."""
    if value is None:
        return None
    if isinstance(value, (list, tuple)):
        if len(value) == 2 and all(isinstance(v, (int, float)) for v in value):
            num, den = value
            return float(num) / float(den) if den else None
        if len(value) == 1:
            return _rational(value[0])
        return None
    if hasattr(value, "numerator") and hasattr(value, "denominator"):
        den = value.denominator
        return float(value.numerator) / float(den) if den else None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def _dms_to_decimal(dms: Any, ref: str | None) -> float | None:
    """Convert an EXIF (deg, min, sec) tuple to a signed decimal degree."""
    if not dms or len(dms) != 3:
        return None
    parts = [_rational(part) for part in dms]
    if any(part is None for part in parts):
        return None
    degrees, minutes, seconds = parts  # type: ignore[misc]
    # A zeroed GPS tuple is a classic "no fix" writer artefact.
    if degrees == 0 and minutes == 0 and seconds == 0 and not ref:
        return None
    decimal = degrees + minutes / 60.0 + seconds / 3600.0  # type: ignore[operator]
    if ref and str(ref).upper() in {"S", "W"}:
        decimal = -decimal
    return round(decimal, 9)


def _ifd_get(ifd: dict[str, Any], name: str, tag_id: int) -> Any:
    """GPS IFDs are keyed by name normally, by tag id when Pillow lacks a name."""
    if name in ifd:
        return ifd[name]
    return ifd.get(str(tag_id))


def _parse_gps(ifd: dict[str, Any] | None) -> GpsFix | None:
    if not ifd:
        return None
    lat = _dms_to_decimal(
        _ifd_get(ifd, "GPSLatitude", 2), _ifd_get(ifd, "GPSLatitudeRef", 1)
    )
    lon = _dms_to_decimal(
        _ifd_get(ifd, "GPSLongitude", 4), _ifd_get(ifd, "GPSLongitudeRef", 3)
    )
    if lat is None or lon is None:
        return None
    if not (-90.0 <= lat <= 90.0 and -180.0 <= lon <= 180.0):
        logger.warning("Discarding out-of-range GPS fix lat=%s lon=%s", lat, lon)
        return None

    altitude = _rational(_ifd_get(ifd, "GPSAltitude", 6))
    alt_ref = _ifd_get(ifd, "GPSAltitudeRef", 5)
    if altitude is not None and (alt_ref in {1, b"\x01", "\x01", "1"}):
        altitude = -abs(altitude)
    datum = _ifd_get(ifd, "GPSMapDatum", 29)
    return GpsFix(
        latitude=lat,
        longitude=lon,
        altitude_m=round(altitude, 3) if altitude is not None else None,
        datum=str(datum) if datum else None,
        direction_ref=_ifd_get(ifd, "GPSImgDirectionRef", 16),
    )


def _parse_camera(exif: dict[str, Any]) -> CameraInfo:
    # Sub-IFD values may arrive under friendly names or numeric tag ids.
    sub = exif.get("Exif") or {}
    iso_value = sub.get("ISOSpeedRatings", sub.get(_TAG_ISO))
    if isinstance(iso_value, (list, tuple)):
        iso_value = iso_value[0] if iso_value else None
    return CameraInfo(
        make=exif.get("Make"),
        model=exif.get("Model"),
        lens_model=sub.get("LensModel", sub.get(_TAG_LENS_MODEL)),
        focal_length_mm=_rational(sub.get("FocalLength", sub.get(_TAG_FOCAL_LENGTH))),
        focal_length_35mm_eq=_rational(
            sub.get("FocalLengthIn35mmFilm", sub.get(_TAG_FOCAL_LENGTH_35MM))
        ),
        exposure_time_s=_rational(sub.get("ExposureTime", sub.get(_TAG_EXPOSURE))),
        f_number=_rational(sub.get("FNumber", sub.get(_TAG_FNUMBER))),
        iso=int(iso_value) if isinstance(iso_value, (int, float)) else None,
        software=exif.get("Software", exif.get(_TAG_SOFTWARE)),
    )


def _profile_exif(image: Image.Image) -> dict[str, Any]:
    exif = image.getexif()
    if not exif:
        return {}
    # Pillow does not name GPS tags in ExifTags.TAGS; GPSTAGS has the mapping.
    merged: dict[str, Any] = {
        ExifTags.TAGS.get(tag, str(tag)): _jsonable(value) for tag, value in exif.items()
    }
    ifd_map = {"Exif": ExifTags.IFD.Exif, "GPSInfo": ExifTags.IFD.GPSInfo}
    for ifd_name, ifd_id in ifd_map.items():
        try:
            sub = exif.get_ifd(ifd_id)
        except Exception:  # Pillow raises various errors on malformed IFDs
            logger.warning("Could not read %s IFD; continuing without it", ifd_name)
            continue
        if not sub:
            continue
        names = ExifTags.GPSTAGS if ifd_name == "GPSInfo" else ExifTags.TAGS
        merged[ifd_name] = {names.get(k, str(k)): _jsonable(v) for k, v in sub.items()}
    return merged


def _read_xmp(image: Image.Image, raw: bytes) -> dict[str, Any] | None:
    """Return embedded XMP as raw text (parsing XMP is out of scope for the MVP)."""
    try:
        xmp = image.info.get("xmp")
        if isinstance(xmp, bytes):
            return {"raw": xmp.decode("utf-8", errors="replace")[:20000], "length": len(xmp)}
        start = raw.find(_XMP_NAMESPACE)
        if start != -1:
            end = raw.find(b"<?xpacket end", start)
            packet = raw[start + len(_XMP_NAMESPACE) : end + 40 if end != -1 else start + 20000]
            return {"raw": packet.decode("utf-8", errors="replace")[:20000], "length": len(packet)}
    except Exception:
        logger.warning("XMP packet present but unreadable")
    return None


def _embedded_georeferencing(path: Path) -> dict[str, Any] | None:
    """Read CRS and affine transform from a raster that already carries them."""
    try:
        import rasterio
    except ImportError:  # pragma: no cover - dependency is required in practice
        return None
    try:
        with rasterio.open(path) as dataset:
            if dataset.crs is None and dataset.transform.is_identity:
                return None
            transform = dataset.transform
            return {
                "crs": dataset.crs.to_string() if dataset.crs else None,
                "crs_wkt": dataset.crs.to_wkt() if dataset.crs else None,
                "transform": list(transform)[:6],
                "origin_x": transform.c,
                "origin_y": transform.f,
                "pixel_size_x": transform.a,
                "pixel_size_y": -transform.e,
                "width": dataset.width,
                "height": dataset.height,
                "band_count": dataset.count,
                "dtype": dataset.dtypes[0] if dataset.dtypes else None,
                "nodata": dataset.nodata,
                "driver": dataset.driver,
            }
    except Exception:
        logger.warning("Raster probe failed for %s", path, exc_info=True)
        return None


def _tiff_geokeys(path: Path) -> dict[str, Any] | None:
    """Detect GeoTIFF tags without a decodable CRS (partial georeferencing)."""
    try:
        head = path.read_bytes()[:4096] if path.stat().st_size < 50_000_000 else b""
    except OSError:
        return None
    if not head.startswith(_TIFF_HEADER):
        return None
    if _GEOKEY_TIFF in head or b"GeoAsciiParamsTag" in head:
        return {"present": True, "note": "TIFF contains GeoTIFF tags"}
    return None


def _png_geo_tags(image: Image.Image) -> dict[str, Any] | None:
    text = getattr(image, "text", None) or {}
    geo = {k: text[k] for k in text if str(k).startswith("geo.")}
    return geo or None


class MetadataExtractor:
    """Extracts everything we can learn about an image without inventing data."""

    def extract_summary(self, path: Path) -> tuple[ExifSummary, dict[str, Any], list[str]]:
        warnings: list[str] = []
        try:
            raw = path.read_bytes()
            image = Image.open(path)
            image.load()
        except (UnidentifiedImageError, OSError) as exc:
            raise InvalidImage(
                "File could not be decoded as an image",
                details={"reason": str(exc)},
            ) from exc

        exif = _profile_exif(image)
        gps_ifd = exif.get("GPSInfo") or {}
        gps = _parse_gps(gps_ifd)
        camera = _parse_camera(exif)
        has_camera = any(
            getattr(camera, field)
            for field in ("make", "model", "lens_model", "focal_length_mm", "focal_length_35mm_eq")
        )
        xmp = _read_xmp(image, raw)
        geotiff = _embedded_georeferencing(path)
        geokeys = _tiff_geokeys(path) if geotiff is None else None
        png_geo = _png_geo_tags(image)

        if gps is None and gps_ifd:
            warnings.append("GPS IFD present but does not contain a usable lat/lon fix")
        if gps is not None and gps.altitude_m is None:
            warnings.append("GPS fix has no altitude; no absolute scale reference available")

        summary = ExifSummary(
            gps=gps,
            camera=camera if has_camera else None,
            datetime_original=exif.get("DateTimeOriginal") or exif.get("DateTime"),
            orientation=exif.get("Orientation"),
            exif_tag_count=sum(
                1 for key, value in exif.items() if key not in {"Exif", "GPSInfo", "GPSInfo"}
            ),
            has_embedded_xmp=xmp is not None,
        )

        details: dict[str, Any] = {
            "format": image.format,
            "mode": image.mode,
            "width": image.width,
            "height": image.height,
            "exif": exif,
            "xmp": xmp,
            "geotiff": geotiff,
            "geokeys": geokeys,
            "png_geo_tags": png_geo,
            "icc_profile_present": "icc_profile" in image.info,
            "dpi": image.info.get("dpi"),
            "image_info_keys": sorted(k for k in image.info if isinstance(k, str)),
        }
        return summary, details, warnings

    @staticmethod
    def decode_header(path: Path) -> tuple[int, int, str]:
        """Cheap dimension check used by the validation layer."""
        try:
            with Image.open(path) as image:
                return image.width, image.height, image.mode
        except (UnidentifiedImageError, OSError) as exc:
            raise InvalidImage(
                "File could not be decoded as an image",
                details={"reason": str(exc)},
            ) from exc


def sniff_image_format(path: Path) -> str | None:
    """Magic-byte format detection, independent of the filename extension."""
    try:
        head = path.read_bytes()[:16]
    except OSError:
        return None
    if head.startswith(b"\xff\xd8\xff"):
        return "JPEG"
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return "PNG"
    if head.startswith((b"II*\x00", b"MM\x00*")):
        return "TIFF"
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "WEBP"
    if head[4:8] == b"ftyp" or struct.unpack(">H", head[:2])[0] == 0:
        return None
    return None
