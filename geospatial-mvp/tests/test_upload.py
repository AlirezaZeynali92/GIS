"""Upload validation and error-handling tests."""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.conftest import upload


def test_health_reports_dependencies(client):
    response = client.get("/health")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["checks"]["gdal"]
    assert body["checks"]["storage"] == "ok"


def test_valid_jpeg_upload_returns_metadata(client, sample_dir: Path):
    response = upload(client, sample_dir / "aerial_gps.jpg")
    assert response.status_code == 201
    body = response.json()
    assert body["image_id"].startswith("img_")
    assert body["validation"]["detected_format"] == "JPEG"
    assert body["validation"]["width"] == 1600
    assert body["exif_summary"]["gps"]["latitude"] == pytest.approx(35.6892, abs=1e-4)
    assert body["georeferencing_status"] == "estimated_from_gps"
    assert len(body["sha256"]) == 64


def test_upload_without_geo_reports_missing_inputs(client, sample_dir: Path):
    body = upload(client, sample_dir / "aerial_no_geo.png").json()
    assert body["georeferencing_status"] == "missing"
    assert body["exif_summary"]["gps"] is None

    detail = client.get(f"/images/{body['image_id']}").json()
    assert detail["georeferencing_status"] == "none"
    assert "GPS coordinates" in detail["missing_georeferencing_inputs"]


def test_unsupported_extension_is_rejected(client, tmp_path: Path):
    target = tmp_path / "notes.txt"
    target.write_text("this is not an image")
    with target.open("rb") as handle:
        response = client.post(
            "/images/upload", files={"file": ("notes.txt", handle, "text/plain")}
        )
    assert response.status_code == 415
    assert response.json()["error"]["code"] == "unsupported_file_type"


def test_extension_content_mismatch_is_rejected(client, sample_dir: Path):
    # A PNG announcing itself as a JPEG must not be accepted.
    with (sample_dir / "aerial_no_geo.png").open("rb") as handle:
        response = client.post(
            "/images/upload", files={"file": ("fake.jpg", handle, "image/jpeg")}
        )
    assert response.status_code == 415
    assert response.json()["error"]["details"]["detected_format"] == "PNG"


def test_corrupted_image_is_rejected(client, tmp_path: Path):
    broken = tmp_path / "broken.jpg"
    broken.write_bytes(b"\xff\xd8\xff\xe0" + b"\x00" * 512)
    response = upload(client, broken)
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "invalid_image"
    # Nothing should be left behind on disk.
    assert list((tmp_path / "data" / "uploads").glob("img_*")) == []


def test_empty_upload_is_rejected(client, tmp_path: Path):
    empty = tmp_path / "empty.jpg"
    empty.write_bytes(b"")
    response = upload(client, empty)
    assert response.status_code == 422


def test_oversized_upload_is_rejected(client, tmp_path: Path):
    big = tmp_path / "big.jpg"
    big.write_bytes(b"\xff\xd8\xff\xe0" + b"\x00" * (6 * 1024 * 1024))
    response = upload(client, big)
    assert response.status_code == 413
    assert response.json()["error"]["code"] == "payload_too_large"


def test_unknown_image_returns_structured_404(client):
    response = client.get("/images/img_000000000000000000000000")
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "not_found"


def test_path_traversal_id_is_rejected(client):
    response = client.get("/images/..%2F..%2Fetc")
    assert response.status_code == 404


def test_unknown_route_returns_structured_error(client):
    response = client.get("/nope")
    assert response.status_code == 404
    assert set(response.json()) == {"error"}
