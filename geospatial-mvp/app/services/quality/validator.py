"""File validation performed before anything is stored or processed."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from app.core.config import Settings
from app.core.errors import InvalidImage, UnsupportedFileType
from app.core.logging import get_logger
from app.services.metadata.extractor import MetadataExtractor, sniff_image_format

logger = get_logger(__name__)

# Extensions we accept, mapped to the container format Pillow must detect.
_EXTENSION_FORMATS = {
    ".jpg": "JPEG",
    ".jpeg": "JPEG",
    ".png": "PNG",
    ".tif": "TIFF",
    ".tiff": "TIFF",
    ".webp": "WEBP",
}

_MIN_DIMENSION = 16


@dataclass
class ValidationOutcome:
    detected_format: str
    extension: str
    width: int
    height: int
    mode: str
    warnings: list[str] = field(default_factory=list)


class FileValidator:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.extractor = MetadataExtractor()

    def validate(self, path: Path, filename: str, content_type: str | None) -> ValidationOutcome:
        extension = Path(filename).suffix.lower()
        allowed = {ext.lower() for ext in self.settings.allowed_extensions}
        if extension not in allowed:
            raise UnsupportedFileType(
                f"Extension '{extension or '(none)'}' is not supported",
                details={"extension": extension, "allowed": sorted(allowed)},
            )

        sniffed = sniff_image_format(path)
        detected = sniffed or ""
        if not detected:
            raise InvalidImage(
                "File content is not a recognised image format",
                details={"extension": extension, "content_type": content_type},
            )

        expected = _EXTENSION_FORMATS[extension]
        warnings: list[str] = []
        if detected != expected:
            if {detected, expected} == {"JPEG", "TIFF"}:
                warnings.append(f"Extension {extension} covers a {detected} container")
            else:
                raise UnsupportedFileType(
                    f"Content is {detected} but the filename claims {expected}",
                    details={"extension": extension, "detected_format": detected},
                )

        try:
            width, height, mode = self.extractor.decode_header(path)
        except InvalidImage:
            raise
        except Exception as exc:  # decode failures must never be silent
            raise InvalidImage(
                "Header decoded but pixel data could not be read",
                details={"reason": str(exc)},
            ) from exc

        if width < _MIN_DIMENSION or height < _MIN_DIMENSION:
            raise InvalidImage(
                "Image is too small to process",
                details={"width": width, "height": height, "minimum": _MIN_DIMENSION},
            )

        pixels = width * height
        if pixels > self.settings.max_image_pixels:
            raise InvalidImage(
                "Image exceeds the configured pixel budget",
                details={
                    "width": width,
                    "height": height,
                    "pixels": pixels,
                    "max_pixels": self.settings.max_image_pixels,
                },
            )

        return ValidationOutcome(
            detected_format=detected,
            extension=extension,
            width=width,
            height=height,
            mode=mode,
            warnings=warnings,
        )
