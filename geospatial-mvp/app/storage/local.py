"""Local filesystem storage for uploaded images, outputs and job records."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, BinaryIO

from app.core.config import Settings
from app.core.errors import AppError, NotFound, PayloadTooLarge
from app.core.logging import get_logger

logger = get_logger(__name__)

_CHUNK = 1024 * 1024
_ID_PATTERN = re.compile(r"^[a-z]+_[a-f0-9]{8,64}$")
_SAFE_NAME = re.compile(r"[^A-Za-z0-9._-]+")


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def new_id(prefix: str) -> str:
    return f"{prefix}_{os.urandom(12).hex()}"


def is_valid_id(value: str) -> bool:
    """Ids are generated locally; anything else must never reach the filesystem."""
    return bool(_ID_PATTERN.match(value))


def sanitize_filename(name: str | None) -> str:
    base = Path(name or "upload").name
    cleaned = _SAFE_NAME.sub("_", base).strip("._") or "upload"
    return cleaned[:120]


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(_CHUNK), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _replace_with_retry(source: Path, target: Path, attempts: int = 6) -> None:
    """`os.replace` that tolerates transient Windows sharing violations."""
    last_error: OSError | None = None
    for attempt in range(attempts):
        try:
            os.replace(source, target)
            return
        except PermissionError as exc:
            last_error = exc
            time.sleep(0.02 * (2**attempt))
    raise last_error if last_error else RuntimeError("replace failed")


@dataclass(frozen=True)
class StoredJobFile:
    """A job artefact on disk, addressed by a stable download name."""

    name: str
    path: Path
    content_type: str


class LocalStorage:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.settings.ensure_directories()
        self.uploads_dir = settings.uploads_dir
        self.outputs_dir = settings.outputs_dir

    # ------------------------------------------------------------------ paths
    def image_dir(self, image_id: str) -> Path:
        if not is_valid_id(image_id):
            raise NotFound("Image not found", details={"image_id": image_id})
        return self.uploads_dir / image_id

    def job_dir(self, image_id: str, job_id: str) -> Path:
        if not is_valid_id(job_id):
            raise NotFound("Job not found", details={"job_id": job_id})
        return self.image_dir(image_id) / "jobs" / job_id

    # ------------------------------------------------------------------ writes
    def save_upload(
        self,
        image_id: str,
        filename: str,
        stream: BinaryIO,
        max_bytes: int,
    ) -> tuple[Path, int, str]:
        """Stream the upload to a temp file, enforcing the size cap atomically."""
        target_dir = self.image_dir(image_id)
        target_dir.mkdir(parents=True, exist_ok=True)
        final_path = target_dir / sanitize_filename(filename)

        digest = hashlib.sha256()
        total = 0
        fd, tmp_name = tempfile.mkstemp(dir=target_dir, prefix=".upload-")
        try:
            with os.fdopen(fd, "wb") as tmp:
                while chunk := stream.read(_CHUNK):
                    total += len(chunk)
                    if total > max_bytes:
                        raise PayloadTooLarge(
                            f"Upload exceeds the {max_bytes} byte limit",
                            details={"limit_bytes": max_bytes, "received_bytes": total},
                        )
                    digest.update(chunk)
                    tmp.write(chunk)
                tmp.flush()
                os.fsync(tmp.fileno())
            if total == 0:
                raise AppError(
                    "Uploaded file is empty",
                    status_code=422,
                    details={"field": "file"},
                )
            _replace_with_retry(Path(tmp_name), final_path)
        except BaseException:
            Path(tmp_name).unlink(missing_ok=True)
            shutil.rmtree(target_dir, ignore_errors=True)
            raise
        logger.info("Stored upload %s (%d bytes)", final_path, total)
        return final_path, total, digest.hexdigest()

    def write_json(self, path: Path, payload: Any) -> None:
        """Write JSON atomically.

        On Windows `os.replace` fails with WinError 5 when another process
        (antivirus, OneDrive sync) briefly holds the destination open, so the
        rename is retried with backoff before giving up.
        """
        path.parent.mkdir(parents=True, exist_ok=True)
        data = json.dumps(payload, indent=2, ensure_ascii=False, default=str)
        tmp = path.with_name(f".{path.name}.{os.urandom(4).hex()}.tmp")
        with open(tmp, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            _replace_with_retry(tmp, path)
        finally:
            tmp.unlink(missing_ok=True)

    def read_json(self, path: Path) -> Any:
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError as exc:
            raise NotFound("Record not found", details={"file": path.name}) from exc
        except json.JSONDecodeError as exc:
            raise AppError(
                "Stored record is corrupt",
                status_code=500,
                details={"file": path.name, "reason": str(exc)},
            ) from exc

    # ---------------------------------------------------------------- records
    def image_record_path(self, image_id: str) -> Path:
        return self.image_dir(image_id) / "image.json"

    def job_record_path(self, image_id: str, job_id: str) -> Path:
        return self.job_dir(image_id, job_id) / "job.json"

    def _require(self, path: Path, message: str, key: str, value: str) -> None:
        if not path.is_file():
            raise NotFound(message, details={key: value})

    def read_image_record(self, image_id: str) -> dict[str, Any]:
        path = self.image_record_path(image_id)
        self._require(path, "Image not found", "image_id", image_id)
        return self.read_json(path)

    def read_job_record(self, image_id: str, job_id: str) -> dict[str, Any]:
        path = self.job_record_path(image_id, job_id)
        self._require(path, "Job not found", "job_id", job_id)
        return self.read_json(path)

    def find_job(self, job_id: str) -> tuple[str, dict[str, Any]]:
        """Locate a job by id alone (the image id is part of the path)."""
        if not is_valid_id(job_id):
            raise NotFound("Job not found", details={"job_id": job_id})
        for image_dir in self.uploads_dir.iterdir():
            if not image_dir.is_dir():
                continue
            job_record = image_dir / "jobs" / job_id / "job.json"
            if job_record.is_file():
                return image_dir.name, self.read_json(job_record)
        raise NotFound("Job not found", details={"job_id": job_id})

    def image_exists(self, image_id: str) -> bool:
        return is_valid_id(image_id) and self.image_record_path(image_id).is_file()

    # --------------------------------------------------------------- downloads
    def resolve_download(self, image_id: str, job_id: str, filename: str) -> StoredJobFile:
        base = self.job_dir(image_id, job_id).resolve()
        candidate = (base / sanitize_filename(filename)).resolve()
        if base not in candidate.parents or not candidate.is_file():
            raise NotFound("Output file not found", details={"filename": filename})
        return StoredJobFile(
            name=candidate.name,
            path=candidate,
            content_type=_content_type(candidate),
        )


_CONTENT_TYPES = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".tif": "image/tiff",
    ".tiff": "image/tiff",
    ".webp": "image/webp",
    ".json": "application/json",
}


def _content_type(path: Path) -> str:
    return _CONTENT_TYPES.get(path.suffix.lower(), "application/octet-stream")
