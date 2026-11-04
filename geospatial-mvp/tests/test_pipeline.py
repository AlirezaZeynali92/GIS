"""End-to-end pipeline behaviour: jobs, products, QC and reports."""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.conftest import upload


def _process(client, sample_dir: Path, filename: str, payload: dict | None = None):
    upload_body = upload(client, sample_dir / filename).json()
    image_id = upload_body["image_id"]
    response = client.post(f"/images/{image_id}/process", json=payload)
    return upload_body, response


def test_geotagged_image_produces_geotiff_and_cog(client, sample_dir: Path):
    _, response = _process(client, sample_dir, "aerial_gps.jpg")
    assert response.status_code == 200
    job_id = response.json()["job_id"]

    outputs = client.get(f"/jobs/{job_id}/outputs").json()["outputs"]
    by_kind = {item["kind"]: item for item in outputs}

    assert by_kind["geotiff"]["status"] == "generated"
    assert by_kind["cog"]["status"] == "generated"
    assert by_kind["preview"]["status"] == "generated"
    assert by_kind["metadata_json"]["status"] == "generated"
    assert by_kind["original"]["status"] == "generated"
    assert by_kind["processing_report"]["status"] == "generated"

    # Every generated output must be downloadable and non-empty.
    for kind in ("geotiff", "cog", "preview", "metadata_json", "processing_report"):
        item = by_kind[kind]
        download = client.get(item["download_url"])
        assert download.status_code == 200, kind
        assert len(download.content) > 0
        assert item["sha256"]


def test_report_distinguishes_success_from_accuracy(client, sample_dir: Path):
    _, response = _process(client, sample_dir, "aerial_gps.jpg")
    report = client.get(response.json()["report_url"]).json()

    assert report["processing_success"] is True
    assert report["geospatial_validity"] == "valid"
    # Single-image GPS georeferencing is an estimate, never verified accuracy.
    assert report["geometric_accuracy"] == "not_assessed"
    assert "NOT an orthophoto" in report["accuracy_statement"]
    assert report["allocation"]["method"] == "gps_centre_footprint"

    checks = {c["name"]: c for c in report["quality_checks"]}
    assert checks["gps_availability"]["status"] == "PASS"
    assert checks["georeferencing_availability"]["status"] == "WARNING"
    assert checks["cog_validation"]["status"] == "PASS"
    assert checks["geometric_accuracy"]["status"] == "NOT_APPLICABLE"
    for check in report["quality_checks"]:
        assert set(check) == {"name", "status", "observed", "expected", "explanation"}


def test_plain_image_reports_missing_georeferencing(client, sample_dir: Path):
    _, response = _process(client, sample_dir, "aerial_no_geo.png")
    assert response.status_code == 200
    report = client.get(response.json()["report_url"]).json()

    assert report["processing_success"] is True
    assert report["geospatial_validity"] == "not_assessable"
    products = {p["kind"]: p for p in report["products"]["products"]}
    assert products["geotiff"]["status"] == "not_generated"
    assert "GPS coordinates" in products["geotiff"]["reason"]
    assert products["cog"]["status"] == "not_generated"
    assert products["preview"]["status"] == "generated"
    # No invented coordinates anywhere.
    assert report["geotiff"] is None
    assert report["allocation"] is None
    assert report["missing_georeferencing_inputs"]


def test_embedded_georeferenced_tiff_uses_its_own_crs(client, sample_dir: Path):
    _, response = _process(client, sample_dir, "aerial_embedded.tif")
    report = client.get(response.json()["report_url"]).json()
    assert report["allocation"]["method"] == "embedded_geotransform"
    assert report["geotiff"]["crs"] == "EPSG:32639"
    assert report["cog"]["is_cog"] is True


def test_explicit_override_georeferences_plain_image(client, sample_dir: Path):
    payload = {
        "crs": "EPSG:32639",
        "origin_x": 430000.0,
        "origin_y": 3950000.0,
        "pixel_size_x": 0.2,
        "pixel_size_y": 0.2,
    }
    _, response = _process(client, sample_dir, "aerial_no_geo.png", payload)
    report = client.get(response.json()["report_url"]).json()
    assert report["allocation"]["method"] == "caller_override"
    assert report["geotiff"]["crs"] == "EPSG:32639"
    assert report["geotiff"]["pixel_size_x"] == pytest.approx(0.2)
    checks = {c["name"]: c for c in report["quality_checks"]}
    assert checks["georeferencing_availability"]["status"] == "PASS"


def test_incomplete_override_is_rejected(client, sample_dir: Path):
    _, response = _process(client, sample_dir, "aerial_no_geo.png", {"crs": "EPSG:32639"})
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "invalid_image"
    assert "missing_fields" in response.json()["error"]["details"]


def test_job_status_and_steps_are_recorded(client, sample_dir: Path):
    _, response = _process(client, sample_dir, "aerial_gps.jpg")
    job = client.get(f"/jobs/{response.json()['job_id']}").json()
    assert job["job"]["status"] == "succeeded"
    step_names = [s["name"] for s in job["job"]["steps"]]
    assert step_names == [
        "copy_original",
        "load_metadata",
        "georeference",
        "write_geotiff",
        "write_cog",
        "render_preview",
        "quality_control",
        "write_report",
    ]
    assert all(s["status"] == "succeeded" for s in job["job"]["steps"])
    assert all(s["duration_ms"] is not None for s in job["job"]["steps"])
    assert job["quality_summary"]["fail"] == 0


def test_image_detail_tracks_latest_job(client, sample_dir: Path):
    body, response = _process(client, sample_dir, "aerial_gps.jpg")
    detail = client.get(f"/images/{body['image_id']}").json()
    assert detail["latest_job"]["job_id"] == response.json()["job_id"]
    assert detail["latest_job"]["status"] == "succeeded"


def test_processing_unknown_image_returns_404(client):
    response = client.post("/images/img_000000000000000000000000/process")
    assert response.status_code == 404


def test_processing_failure_is_reported_not_swallowed(client, sample_dir: Path, settings):
    """Force a raster-stage failure and confirm the job records it."""
    from app.services.processing import pipeline as pipeline_module

    original = pipeline_module.write_geotiff

    def boom(*args, **kwargs):
        raise pipeline_module.RasterError("simulated raster failure")

    pipeline_module.write_geotiff = boom
    try:
        upload_body = upload(client, sample_dir / "aerial_gps.jpg").json()
        image_id = upload_body["image_id"]
        response = client.post(f"/images/{image_id}/process")
    finally:
        pipeline_module.write_geotiff = original

    # The job itself completes (GeoTIFF is a product, not a precondition) ...
    assert response.status_code == 200
    report = client.get(response.json()["report_url"]).json()
    products = {p["kind"]: p for p in report["products"]["products"]}
    assert products["geotiff"]["status"] == "not_generated"
    assert "simulated raster failure" in products["geotiff"]["reason"]
    assert products["cog"]["status"] == "not_generated"
    assert any("simulated raster failure" in w for w in report["warnings"])


def test_unexpected_crash_fails_job_with_details(client, sample_dir: Path):
    from app.services.processing import pipeline as pipeline_module

    original = pipeline_module.render_preview

    def boom(*args, **kwargs):
        raise RuntimeError("unexpected boom")

    # render_preview is caught by the pipeline (preview is non-fatal), so use a
    # stage that is not individually guarded to exercise the crash path.
    original_qc = pipeline_module.input_checks

    def qc_boom(*args, **kwargs):
        raise ValueError("qc exploded")

    pipeline_module.input_checks = qc_boom
    try:
        upload_body = upload(client, sample_dir / "aerial_gps.jpg").json()
        image_id = upload_body["image_id"]
        response = client.post(f"/images/{image_id}/process")
    finally:
        pipeline_module.input_checks = original_qc
        pipeline_module.render_preview = original

    assert response.status_code == 500
    body = response.json()
    assert body["error"]["code"] == "processing_failed"
    job_id = body["error"]["details"]["job_id"]

    job = client.get(f"/jobs/{job_id}").json()["job"]
    assert job["status"] == "failed"
    assert job["error"]["message"]
    assert any(step["status"] == "failed" for step in job["steps"])


def test_report_download_returns_json_file(client, sample_dir: Path):
    _, response = _process(client, sample_dir, "aerial_gps.jpg")
    job_id = response.json()["job_id"]
    image_id = response.json()["image_id"]
    download = client.get(f"/images/{image_id}/jobs/{job_id}/report/download")
    assert download.status_code == 200
    assert "application/json" in download.headers["content-type"]
    assert download.json()["report_version"] == "1.0"


def test_download_rejects_traversal(client, sample_dir: Path):
    _, response = _process(client, sample_dir, "aerial_gps.jpg")
    job_id = response.json()["job_id"]
    image_id = response.json()["image_id"]
    bad = client.get(f"/images/{image_id}/jobs/{job_id}/outputs/..%2F..%2Fimage.json")
    assert bad.status_code == 404
