"""Shared FastAPI dependencies (process-wide singletons)."""

from __future__ import annotations

from functools import lru_cache

from app.core.config import Settings, get_settings
from app.services.ingestion import IngestionService
from app.services.processing.pipeline import ProcessingPipeline
from app.storage.local import LocalStorage


@lru_cache
def get_storage() -> LocalStorage:
    return LocalStorage(get_settings())


@lru_cache
def get_ingestion_service() -> IngestionService:
    return IngestionService(get_settings(), get_storage())


@lru_cache
def get_pipeline() -> ProcessingPipeline:
    return ProcessingPipeline(get_settings(), get_storage())


def reset_singletons() -> None:
    """Test hook: drop cached instances after settings change."""
    get_storage.cache_clear()
    get_ingestion_service.cache_clear()
    get_pipeline.cache_clear()
    get_settings.cache_clear()


__all__ = [
    "Settings",
    "get_settings",
    "get_storage",
    "get_ingestion_service",
    "get_pipeline",
    "reset_singletons",
]
