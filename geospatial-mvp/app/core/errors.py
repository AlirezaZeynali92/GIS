"""Structured application errors that map cleanly onto HTTP responses."""

from __future__ import annotations

from typing import Any


class AppError(Exception):
    """Base error carrying an HTTP status, a stable code and optional details."""

    status_code: int = 500
    code: str = "internal_error"

    def __init__(
        self,
        message: str,
        *,
        details: dict[str, Any] | None = None,
        status_code: int | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.details: dict[str, Any] = details or {}
        if status_code is not None:
            self.status_code = status_code

    def to_payload(self) -> dict[str, Any]:
        return {
            "error": {
                "code": self.code,
                "message": self.message,
                "details": self.details,
            }
        }


class ValidationFailed(AppError):
    status_code = 422
    code = "validation_failed"


class UnsupportedFileType(AppError):
    status_code = 415
    code = "unsupported_file_type"


class PayloadTooLarge(AppError):
    status_code = 413
    code = "payload_too_large"


class InvalidImage(AppError):
    status_code = 422
    code = "invalid_image"


class NotFound(AppError):
    status_code = 404
    code = "not_found"


class Conflict(AppError):
    status_code = 409
    code = "conflict"


class ProcessingFailed(AppError):
    status_code = 500
    code = "processing_failed"
