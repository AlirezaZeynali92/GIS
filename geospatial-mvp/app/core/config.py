"""Application configuration, loaded from environment variables and `.env`."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=PROJECT_ROOT / ".env",
        env_file_encoding="utf-8",
        env_prefix="GEO_",
        extra="ignore",
    )

    app_name: str = "Geospatial MVP"
    app_version: str = "0.1.0"
    environment: str = "local"
    log_level: str = "INFO"

    data_dir: Path = PROJECT_ROOT / "data"
    uploads_dir: Path = PROJECT_ROOT / "data" / "uploads"
    outputs_dir: Path = PROJECT_ROOT / "data" / "outputs"

    max_upload_bytes: int = Field(default=150 * 1024 * 1024, gt=0)
    max_image_pixels: int = Field(default=120_000_000, gt=0)
    thumbnail_max_size: int = Field(default=1024, gt=0)
    allowed_extensions: tuple[str, ...] = (
        ".jpg",
        ".jpeg",
        ".tif",
        ".tiff",
        ".png",
        ".webp",
    )
    cors_origins: tuple[str, ...] = (
        "http://localhost:5173",
        "http://127.0.0.1:5173",
        "http://localhost:4173",
        "http://127.0.0.1:4173",
    )

    @property
    def database_url(self) -> str | None:
        return None

    def ensure_directories(self) -> None:
        for directory in (self.data_dir, self.uploads_dir, self.outputs_dir):
            directory.mkdir(parents=True, exist_ok=True)


@lru_cache
def get_settings() -> Settings:
    settings = Settings()
    settings.ensure_directories()
    return settings
