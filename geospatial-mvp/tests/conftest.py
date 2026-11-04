"""Shared test fixtures: isolated data dirs and real sample images per test run."""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
if str(PROJECT_ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT / "scripts"))

import make_samples  # noqa: E402  (path manipulation must come first)


@pytest.fixture(scope="session")
def sample_dir(tmp_path_factory: pytest.TempPathFactory) -> Path:
    target = tmp_path_factory.mktemp("samples")
    make_samples.make_georeferenced_jpeg(target / "aerial_gps.jpg")
    make_samples.make_plain_png(target / "aerial_no_geo.png")
    make_samples.make_embedded_tiff(target / "aerial_embedded.tif")
    return target


@pytest.fixture()
def settings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Point the app at a throwaway data directory before singletons are built."""
    from app.core import config

    data_dir = tmp_path / "data"
    monkeypatch.setenv("GEO_DATA_DIR", str(data_dir))
    monkeypatch.setenv("GEO_UPLOADS_DIR", str(data_dir / "uploads"))
    monkeypatch.setenv("GEO_OUTPUTS_DIR", str(data_dir / "outputs"))
    monkeypatch.setenv("GEO_MAX_UPLOAD_BYTES", str(5 * 1024 * 1024))
    config.get_settings.cache_clear()

    from app.api import deps

    deps.reset_singletons()
    created = config.get_settings()
    yield created
    deps.reset_singletons()
    shutil.rmtree(data_dir, ignore_errors=True)


@pytest.fixture()
def client(settings):
    from fastapi.testclient import TestClient

    from app.main import create_app

    with TestClient(create_app()) as test_client:
        yield test_client


@pytest.fixture()
def storage(settings):
    from app.storage.local import LocalStorage

    return LocalStorage(settings)


def upload(client, path: Path, mime: str | None = None):
    mime = mime or {
        ".jpg": "image/jpeg",
        ".png": "image/png",
        ".tif": "image/tiff",
    }[path.suffix.lower()]
    with path.open("rb") as handle:
        return client.post("/images/upload", files={"file": (path.name, handle, mime)})
